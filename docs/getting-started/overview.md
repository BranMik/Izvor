# Izvor Platform Overview

**Izvor** (`v0.9.0`, Python `>=3.11`) is an asynchronous **Multi-Agent Dialectical Deliberation & Slack Orchestration Platform** that connects Slack workspaces to specialized LLM personas using [LiteLLM](https://github.com/BerriAI/litellm), standard-library SQLite (`WAL` mode) persistence, LanceDB + Tantivy BM25 hybrid RAG, Microsoft Presidio privacy filters, and [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) tools.

---

## 🎯 Vision & Purpose

In modern collaborative environments, complex engineering, security, and strategic decisions require diverse perspectives. Izvor replaces isolated single-prompt chat interactions with structured, multi-persona deliberations directly inside Slack threads.

Instead of one generic assistant answering every prompt, Izvor orchestrates **multiple specialized debate personas** (16 default OOB in [`config/settings/agent_settings.json`](file:///config/settings/agent_settings.json), such as *Visionary*, *Innovator*, *Sceptic*, *Security Architect*, *ROI Strategist*, *Legal Advisor*, *Sales Advisor*, and *Tactician*) alongside **5 built-in system agents** (*Discussion Scribe*, *Internal Revisor*, *Dialectical Challenger*, *Arbitrator*, and *ListSkills* in [`engine/settings/system_agent_settings.json`](file:///engine/settings/system_agent_settings.json)) that deliberate, critique, invoke sandboxed tools, and synthesize decisions using defined orchestration topologies.

---

## 🏗️ High-Level System Architecture

```mermaid
graph TD
    User([Slack User / Admin]) -->|Mentions, Slash Directives & Interactive Actions| SlackAPI[Slack Platform]
    SlackAPI -->|HTTP POST /slack/events or /slack/interactivity| FastAPIApp[FastAPI Server in main.py]

    subgraph Ingress & Deduplication
        FastAPIApp --> Signature[Slack HMAC-SHA256 Verification]
        Signature --> EventDedup[SQLite Event Deduplication & Generation Check]
    end

    subgraph 5-Stage Orchestration Pipeline
        EventDedup --> Stage1[Stage 1: Validation, Guardrail & Directives]
        Stage1 --> Stage2[Stage 2: Agent Resolution & Spend Governance]
        Stage2 --> Stage3[Stage 3: 3-Tier Context Memory & Prompt Building]
        Stage3 --> Stage4[Stage 4: Execution, PII Masking & Tool Loop]
        Stage4 --> Stage5[Stage 5: Post-Record, Word Cutoff & Slack Dispatch]
        Stage5 --> Revisor[Two-Stage InternalRevisor Verification Barrier]
    end

    subgraph Hybrid Persistence, RAG & Governance
        Stage2 --> SpendGov[Spend Governance: Thread & Daily Budget Caps]
        Stage2 & Stage4 & Stage5 --> SQLiteDB[(SQLite WAL State Engine: data/izvor.db)]
        Stage4 <--> ToolsRAG[Sandboxed Tools, Hybrid LanceDB/BM25 RAG & MCP]
        Stage4 --> InPlaceStatus[5s Delayed Progress & In-Place Edit]
        Stage5 --> AsyncFlatHistory[Async Scribe Notes, Usage & Flat History Archival]
    end

    subgraph LLM Layer
        Stage4 & Revisor --> LiteLLM[LiteLLM Provider Router]
        LiteLLM --> Models[Gemini / Anthropic / OpenAI / Mistral / OpenRouter]
    end

    Stage5 -->|Final In-Place Edit / Thread Post| SlackAPI
```

---

## ✨ Key Architectural Pillars

### 1. Multi-Persona Discussion Topologies
Managed by [`WorkflowManager`](file:///engine/orchestration/workflow_manager.py) and [`SandwichOrchestrator`](file:///engine/pipeline/orchestrator/sandwich.py), Izvor supports distinct workflow topologies:
- **🥪 Sandwich Mode (`sandwich`)**: Starts with designated initial thinkers (*Start Chain*), transitions through an open debate or peer-nominated pool (*Middle Pool*), and concludes with an authoritative synthesis by designated closers (*End Chain*).
- **⚡ Parallel Dialectic / Scatter-Gather (`scatter_gather`)**: After Turn 1 framing, specialist agents explore concurrently in isolated `asyncio.TaskGroup` branches with zero sequential anchoring bias and atomic budget pre-reservations, followed by dialectical synthesis by the designated closing agent.
- **🎯 Fixed Mode (`fixed`) & Custom Ad-Hoc Flows**: Executes an ordered, sequential chain of personas defined by a workflow preset or explicit user tags in Slack (e.g., `/[Visionary] /[SecurityArchitect]`).
- **⚡ Open Debate Mode (`/open_debate`)**: Designates an initial speaker and runs an open peer-nominated debate across the active panel without fixed start/end chains.
- **⚡ Consensus Convergence & Adversarial Stress-Testing**: Tracks consecutive agreement streaks (`signal_consensus_convergence`). Before allowing premature consensus, Izvor injects a mandatory **Challenger → Arbitrator** stress-test gate ([`agent_router.py`](file:///engine/orchestration/agent_router.py)); the *Arbitrator* either upholds consensus (**Verdict A**, fast-forwarding to the *End Chain*) or exposes flaws (**Verdict B**, reopening specialist debate).
- **Dynamic Peer Nomination**: Personas pass the baton via the `nominate_next_speaker` control-plane tool or unicode-safe tags (e.g., `/[Security Architect]`, `/[Scope Optimizer]`, `/[Tactician]`), resolved by [`EntityResolver`](file:///engine/pipeline/entity_resolver/resolver.py).

### 2. 3-Tier Context Memory, Epistemic Integrity & Hybrid RAG
- **3-Tier Thread Memory**: Combines (1) *Episodic Summaries* (`<episodic_thread_summary>`) maintained by [`DiscussionScribe`](file:///engine/pipeline/discussion_scribe/scribe.py), (2) *Shared Round Evidence* (`<shared_round_evidence>`) deduplicating read-only tool outputs across turns in [`RoundEvidenceStore`](file:///engine/orchestration/state/evidence_store.py), and (3) *Sliding-Window History* normalized by [`message_builder.py`](file:///engine/nlp/message_builder.py).
- **Two-Stage `InternalRevisor`**: After every turn, [`revisor_service.py`](file:///engine/orchestration/revisor_service.py) screens the agent's claims strictly against isolated ground truth (`<initial_user_prompt>` and `<retrieved_evidence>`) using a fast model (`mistral/mistral-small-latest`) and confirms any flagged hallucination with a stronger reasoning model (`mistral/mistral-large-latest`) before posting a warning.
- **Hybrid Local RAG**: Combines dense vector embeddings in LanceDB with Tantivy BM25 full-text search using Reciprocal Rank Fusion ([`services/rag/service.py`](file:///engine/services/rag/service.py)), incrementally synchronized via SHA-256 manifest scanning over [`config/rag_knowledge/`](file:///config/rag_knowledge) ([`services/rag/sync_engine.py`](file:///engine/services/rag/sync_engine.py)) with retrieval-time PII masking and structured citations.

### 3. Declarative Skills, Progressive Disclosure & Non-Blocking Slack UX
- **Declarative `SKILL.md` Engine**: Dynamically loads a configurable, extensible agent tool skillset (4 OOB in [`config/skills/`](file:///config/skills): `web_research`, `document_reader`, `document_authoring`, `knowledge_base_rag`), **17 built-in directive/control-plane skills** ([`engine/skills/builtin/`](file:///engine/skills/builtin)), and external **MCP servers** ([`services/mcp/client_manager.py`](file:///engine/services/mcp/client_manager.py)).
- **Progressive Disclosure & `ToolPrincipal` ACLs**: Agents start with `load_skill` ([`skills/loader.py`](file:///engine/skills/loader.py)) and unlock authorized tool schemas on demand within a bounded 60-second tool loop (`tool_call_loop_timeout_seconds` in [`config/settings/system_settings.json`](file:///config/settings/system_settings.json)), enforced by fail-closed `ToolPrincipal` permissions.
- **5-Second Delayed Status & In-Place Editing**: If a tool call takes longer than 5 seconds, Izvor posts an intermediate placeholder ([`tool_status.py`](file:///engine/orchestration/stages/tool_status.py)) and edits it in-place once the final response resolves.

### 4. Thread State, Durable Checkpointing & Spend Governance
- **Hybrid In-Memory + ACID SQLite Persistence**: [`ThreadStateManager`](file:///engine/orchestration/thread_state.py) coordinates four domain stores backed by **11 tables** in `data/izvor.db` (`WAL` mode, [`core/database.py`](file:///engine/core/database.py)). Inactive in-memory states are pruned periodically by [`periodic_thread_state_cleanup`](file:///main.py).
- **Generation-Based Superseding**: When a human user replies mid-debate, the thread generation counter increments and obsolete in-flight agent turns abort cleanly without race conditions.
- **Durable `TurnCheckpoint` Serialization**: Mid-turn pauses for Human-in-the-Loop (HITL) tool approvals ([`services/hitl.py`](file:///engine/services/hitl.py)) or budget circuit breakers are persisted in the `turn_checkpoints` table ([`services/turn_checkpoint_store.py`](file:///engine/services/turn_checkpoint_store.py)) for exact resumption.
- **Spend Governance & Autonomous Pricing**: Prompt and completion tokens are recorded in SQLite `spend_ledger` via [`services/pricing_service.py`](file:///engine/services/pricing_service.py) (backed by a 7-day pricing cache in [`data/pricing/model_pricing_cache.json`](file:///data/pricing/model_pricing_cache.json)), enforcing per-thread (`circuit_breaker_threshold_usd`) and daily workspace (`daily_budget_usd`) caps ([`circuit_breaker.py`](file:///engine/pipeline/spend_governance/circuit_breaker.py)).

### 5. Defense-in-Depth Security, Slack App Home & Audit Integrity
- **Privacy & Guardrails**: Protects prompts and outputs with LanceDB cosine-similarity prompt-injection guardrails ([`services/security/guardrail.py`](file:///engine/services/security/guardrail.py)), two-way Microsoft Presidio + regex PII masking ([`services/security/pii_masker.py`](file:///engine/services/security/pii_masker.py)), and DNS-pinned SSRF-safe web fetching ([`services/web_tools.py`](file:///engine/services/web_tools.py)).
- **Interactive Slack App Home**: Workspace admins (`is_slack_admin(user_id)`) configure workflows, agent rosters, models, skills, and governance limits directly in the Slack App Home UI ([`services/slack_home/`](file:///engine/services/slack_home/__init__.py)), persisted atomically to [`config/settings/agent_settings.json`](file:///config/settings/agent_settings.json), `data/settings/system_agent_settings.json` (seeded from [`engine/settings/system_agent_settings.json`](file:///engine/settings/system_agent_settings.json)), and [`data/state/app_settings.json`](file:///data/state/app_settings.json) with rolling backups in `data/backups/`.
- **Non-Repudiable Cryptographic Audit Logging**: Every administrative action, security event, and RAG sync is recorded in an HMAC-SHA256 hash-chained monthly JSONL audit log ([`services/audit_logger.py`](file:///engine/services/audit_logger.py)) anchored in SQLite `audit_anchors` and verified by [`AppIntegrityValidator`](file:///engine/orchestration/integrity_checks.py).

---

## 🔗 Next Steps

- Proceed to the [Installation Guide](file:///docs/getting-started/installation.md) to set up Izvor locally.
- Configure your Slack Bot using the [Slack App Setup Guide](file:///docs/getting-started/slack-app-setup.md).
