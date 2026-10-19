# Spend Governance & Pricing Engine

This document details how **Izvor** monitors LLM token usage, calculates real-time inference expenses across multiple providers, logs transactions to SQLite, and enforces two-tiered budget circuit breakers.

---

## 💰 Architecture Overview

Izvor orchestrates multiple LLM personas across providers such as OpenRouter, OpenAI, Anthropic, Google Gemini, and Mistral. To protect against runaway costs or looped tool executions, the platform includes an autonomous spend governance subsystem:

```mermaid
graph TD
    Turn[Completed Agent Turn] --> PostRecord[Stage 5: Post-Record & Accounting]
    PostRecord --> Usage[Extract Token Counts: Prompt & Completion]
    Usage --> PricingService[PricingService (services/pricing_service.py)]
    
    subgraph Pricing Cache & Researcher
        PricingService --> CacheCheck{Cache < 7 Days?}
        CacheCheck -->|Yes| DiskCache[Read data/pricing/model_pricing_cache.json]
        CacheCheck -->|No / Miss| Researcher[Autonomous Pricing Researcher Agent]
        Researcher --> WebQuery[Query Provider Official Rate Tables]
        Researcher --> UpdateCache[Save Updated Rates to Disk Cache]
    end
    
    PricingService --> ComputeUSD[Compute Exact USD Cost for Turn]
    ComputeUSD --> SQLiteLedger[(SQLite spend_ledger)]
    SQLiteLedger --> DailyAccumulator[Update Daily Spend USD]
    
    subgraph Pre-Turn Resolution Check (Stage 2)
        NextTurn[Incoming Turn] --> ResolutionStage[Stage 2: ResolutionStage]
        ResolutionStage --> CBCheck{Circuit Breaker Enabled?}
        CBCheck -->|Yes| Evaluate{Thread Spend >= Threshold OR Today Spend >= Daily Budget?}
        Evaluate -->|Yes| TripBreaker[Halt Debate, Log Audit Event & Post Slack Alert]
        Evaluate -->|No| Proceed[Proceed to Turn Execution]
        CBCheck -->|No| Proceed
    end
```

---

## 🤖 Autonomous Pricing Researcher Agent

Unlike static cost tables that go out of date as providers adjust their prices, Izvor features an autonomous pricing researcher agent:

1. **Prompt Template**: Defined in [`prompts/system_agents/Pricing_Agent.md`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/prompts/system_agents/Pricing_Agent.md).
2. **Execution**: Dispatched by [`services/pricing_service.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/services/pricing_service.py) during application startup or when an unindexed model is encountered.
3. **Web Research**: Utilizes web search tools to locate official provider pricing tables (per 1,000 or per 1,000,000 tokens for prompt and completion).
4. **Caching**: Results are stored in `data/pricing/model_pricing_cache.json` with a 7-day TTL.
5. **Hardcoded Fallbacks**: If web access is unavailable, baseline fallbacks for standard flagship models (Claude 3.5 Sonnet, GPT-4o, Gemini 1.5 Pro) are loaded safely.

---

## 📊 Transactional Ledger (`spend_ledger`)

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

After each completed agent turn, [`orchestration/stages/post_record.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/orchestration/stages/post_record.py) records:
- **`prompt_tokens`**: Input tokens processed by the LLM.
- **`completion_tokens`**: Output tokens generated in the reply.
- **`model_name`**: The exact model identifier invoked.
- **`cost_usd`**: Calculated using:
  $$\text{Cost} = \frac{\text{Prompt Tokens} \times \text{Rate}_{\text{prompt}} + \text{Completion Tokens} \times \text{Rate}_{\text{completion}}}{1{,}000{,}000}$$

---

## ⚡ Two-Tiered Circuit Breaker Protection

To safeguard budgets, Izvor enforces both thread-level and daily workspace-level spending limits:

### 1. Configuration Parameters
Managed in `data/settings/app_settings.json` and configurable dynamically via Slack App Home:
- **`circuit_breaker_enabled`**: `true` (Enabled by default).
- **`circuit_breaker_threshold_usd`**: Maximum allowed USD cost per individual thread (e.g., `$1.50`).
- **`daily_budget_usd`**: Global cap on total workspace spending across all threads within a 24-hour UTC window (e.g., `$10.00`).

### 2. Pre-Turn Gatekeeping in Stage 2 (`ResolutionStage`)
Before allocating compute or dispatching to an LLM, [`orchestration/stages/resolution.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/orchestration/stages/resolution.py) invokes `spend_governance.is_budget_exceeded()`:
1. Queries the SQLite `spend_ledger` for thread cumulative spend.
2. Queries the SQLite `spend_ledger` for today's UTC aggregate spend (`timestamp >= midnight_utc`).
3. If either threshold is breached:
   - Emits a structured audit log event (`CIRCUIT_BREAKER_TRIGGERED`).
   - Posts a warning block to the Slack thread informing participants:
     > *⚠️ EXPENSE LIMIT REACHED FOR THE THREAD. ENDING CALLS*
     > *(or: Daily workspace budget of $10.00 exceeded)*
   - Attaches an interactive Block Kit button: `"Continue Sequence"` (`action_id: "resume_sequence_action"`).
   - Terminates the turn immediately without dispatching an LLM call.

### 3. Administrative Override & Resume Action
When a deliberation is paused by the circuit breaker, authorized workspace administrators can manually override and resume:
- Handled by [`adapters/slack/interactivity.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/adapters/slack/interactivity.py) upon receiving the `resume_sequence_action` payload.
- Verifies that the clicking user has Slack workspace admin privileges (`is_admin: true`). Non-admin attempts are rejected with an ephemeral warning.
- Upon admin confirmation, updates the thread state to active, unblocks the remaining sequence, and dispatches the next agent turn.

### 4. Intra-Turn Tool-Loop Governance in Stage 4 (`ExecutionStage`)
To prevent multi-step tool iterations from exceeding budgets mid-turn, [`orchestration/stages/execution.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/orchestration/stages/execution.py) calculates incremental token spend after each tool invocation. If the circuit breaker threshold or daily budget is breached during execution, the loop halts gracefully before the next LLM call.
