# Spend Governance & Pricing Engine

This document details how **Izvor** monitors LLM token usage, calculates real-time inference expenses across multiple providers, logs transactions to SQLite, reserves parallel branch budgets, and enforces two-tiered budget circuit breakers.

---

## 💰 Architecture Overview

Izvor orchestrates multiple LLM personas across providers such as OpenRouter, OpenAI, Anthropic, Google Gemini, and Mistral. To protect against runaway costs or looped tool executions, the platform includes an autonomous spend governance subsystem:

```mermaid
graph TD
    Turn[Completed Agent Turn / Embedding / Pricing Call] --> PostRecord[Token & Spend Accounting]
    PostRecord --> Usage[Extract Token Counts: Prompt & Completion]
    Usage --> PricingService[PricingService (services/pricing_service.py)]
    
    subgraph Pricing Lookup & Researcher
        PricingService --> LiteLLMCost{In litellm.model_cost?}
        LiteLLMCost -->|Yes (Within Bounds)| ValidateBounds["Validate Bounds ($0.005 - $500.00 / 1M)"]
        LiteLLMCost -->|No| CacheCheck{Disk Cache < 7 Days?}
        CacheCheck -->|Yes| DiskCache[Read data/pricing/model_pricing_cache.json]
        CacheCheck -->|No / Miss| Researcher[Autonomous Pricing Researcher Agent]
        Researcher --> WebQuery[Query Provider Official Rate Tables]
        Researcher --> ValidateBounds
        ValidateBounds -->|Out of Bounds / Offline| FailExpensive["Fail-Expensive Default ($15.00 in / $75.00 out per 1M)"]
    end
    
    PricingService --> ComputeUSD[Compute Exact USD Cost]
    ComputeUSD --> SQLiteLedger[(SQLite spend_ledger)]
    SQLiteLedger --> DailyAccumulator[Update Daily & Thread Spend USD]
    
    subgraph Pre-Turn & Intra-Turn Resolution Check
        NextTurn[Incoming Turn / Parallel Branch] --> DailyCheck{Today Spend + Reserved >= Daily Budget?}
        DailyCheck -->|Yes (Unconditional)| TripBreaker[Halt Debate, Save Checkpoint, Log Audit & Post Alert]
        DailyCheck -->|No| CBCheck{Thread Circuit Breaker Enabled?}
        CBCheck -->|Yes| ThreadCheck{Thread Spend + Reserved >= Thread Threshold?}
        ThreadCheck -->|Yes| TripBreaker
        ThreadCheck -->|No| Proceed[Proceed to Turn Execution]
        CBCheck -->|No| Proceed
    end
```

---

## 🤖 Multi-Tier Pricing Resolution & Fail-Expensive Guardrails

Implemented in [`services/pricing_service.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/services/pricing_service.py), Izvor resolves per-token costs through a hardened multi-tier lookup pipeline:

1. **Tier 1 — Primary Registry Lookup (`litellm.model_cost`)**:
   - Queries LiteLLM's built-in `model_cost` registry first (checking both the full `provider/model` string and the normalized model slug) for immediate, zero-latency pricing.
2. **Tier 2 — Disk Cache & Autonomous Pricing Researcher Agent**:
   - Checks `data/pricing/model_pricing_cache.json` (TTL configured via `pricing_cache_ttl_days`, default `7` days).
   - For unindexed models, dispatches the autonomous **Pricing Researcher Agent** ([`prompts/system_agents/Pricing_Agent.md`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/prompts/system_agents/Pricing_Agent.md)), which researches rates from `pricing_benchmark_url` (`"https://artificialanalysis.ai/models"`). Token usage incurred by the Pricing Researcher Agent itself is also recorded in the spend ledger.
3. **Strict Price Bounds Validation (`$0.005` – `$500.00` per 1M tokens)**:
   - Any researched or cached rate outside the sanity bounds of **`$0.005` to `$500.00` per 1,000,000 tokens** (`MIN_VALID_PRICE_PER_1M` / `MAX_VALID_PRICE_PER_1M`) is rejected to prevent poisoned web results or zero-cost bypasses.
4. **Conservative Fail-Expensive Fallback (`$15.00` / `$75.00` per 1M)**:
   - If a model is unknown and live research fails or returns out-of-bounds values, `PricingService` defaults to a conservative **fail-expensive** rate of **`$15.00` per 1M input tokens** and **`$75.00` per 1M output tokens**, ensuring unknown models can never silently bypass budget circuit breakers.

---

## 📊 Transactional Ledger (`spend_ledger`) & Comprehensive Tracking

All token usage and USD costs are written immediately to the SQLite database (`data/izvor.db`, configurable via `IZVOR_DB_PATH`) in table `spend_ledger`:

```sql
CREATE TABLE IF NOT EXISTS spend_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    thread_ts TEXT NOT NULL,
    model_name TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    timestamp REAL NOT NULL
);
```

In addition to standard persona turns recorded by [`orchestration/stages/post_record.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/orchestration/stages/post_record.py), Izvor also records spend for:
- **RAG Embedding Calls** ([`services/rag/embeddings.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/services/rag/embeddings.py))
- **Autonomous Pricing Researcher Calls** ([`services/pricing_service.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/services/pricing_service.py))

$$\text{Cost} = \frac{\text{Prompt Tokens} \times \text{Rate}_{\text{prompt}} + \text{Completion Tokens} \times \text{Rate}_{\text{completion}}}{1{,}000{,}000}$$

---

## ⚡ Two-Tiered Circuit Breaker & Parallel Reservation Protection

Managed by [`SpendGovernance`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/pipeline/spend_governance/circuit_breaker.py) and configured via `data/state/app_settings.json` (or Slack App Home):
- **`daily_budget_usd`**: Global cap on total workspace spending across all threads within a 24-hour UTC window (e.g., `$10.00`). **Enforced unconditionally on every turn**, even if the per-thread `circuit_breaker_enabled` toggle is set to `false`.
- **`circuit_breaker_enabled`**: `true` (Enabled by default; controls per-thread threshold enforcement).
- **`circuit_breaker_threshold_usd`**: Maximum allowed USD cost per individual thread (e.g., `$1.00`).

### 1. Pre-Turn Gatekeeping in Stage 2 (`ResolutionStage`)
Before allocating compute or dispatching to an LLM, [`orchestration/stages/resolution.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/orchestration/stages/resolution.py) invokes `spend_governance.is_budget_exceeded()`:
1. Evaluates today's UTC aggregate spend (`timestamp >= midnight_utc`) + active in-flight reservations against `daily_budget_usd` (**unconditional**).
2. If `circuit_breaker_enabled` is `true`, evaluates cumulative thread spend + active thread reservations against `circuit_breaker_threshold_usd`.
3. If either threshold is breached:
   - Emits a structured audit log event (`CIRCUIT_BREAKER_TRIGGERED`).
   - Posts an interactive warning block to the Slack thread with a `"Continue Sequence"` (`action_id: "resume_sequence_action"`) button.
   - Terminates the turn immediately without dispatching an LLM call.

### 2. Parallel Dialectic Budget Reservations (`reserve_budget` / `release_reservation`)
When running `OrchestrationMode.SCATTER_GATHER` in [`DiscussionLoop`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/orchestration/discussion_loop.py), multiple specialist branches execute concurrently in an `asyncio.TaskGroup`. To prevent N parallel branches from simultaneously passing the pre-turn check and overshooting the budget:
- Each branch calls `reserve_id = spend_governance.reserve_budget(session_id, estimated_cost_usd)` prior to LLM execution.
- In-flight reservations are included in all thread and daily budget checks.
- Reservations are deterministically released in a `finally` block via `spend_governance.release_reservation(session_id, reserve_id)` once actual spend is committed to `spend_ledger`.

### 3. Administrative Override & Resume Action
When a deliberation is paused by the circuit breaker, authorized workspace administrators (`is_slack_admin`) can click `"Continue Sequence"` (`resume_sequence_action`), handled via [`adapters/slack/actions/workflow_actions.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/adapters/slack/actions/workflow_actions.py), to grant an incremental budget override and resume the paused checkpoint or remaining agent queue.

### 4. Intra-Turn Tool-Loop Governance in Stage 4 (`ExecutionStage`)
To prevent multi-step tool iterations from exceeding budgets mid-turn, [`orchestration/stages/execution.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/orchestration/stages/execution.py) calculates incremental token spend after each tool invocation. If the circuit breaker threshold or daily budget is breached during execution, `ExecutionStage` saves a durable snapshot in `TurnCheckpointStore` and halts gracefully before the next LLM call.
