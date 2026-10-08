# Izvor Platform Overview

**Izvor** is an enterprise-ready, asynchronous multi-agent deliberation platform that connects Slack workspaces to specialized LLM personas using [LiteLLM](https://github.com/BerriAI/litellm) and [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) tools.

---

## 🎯 Vision & Purpose

In modern collaborative environments, complex engineering, security, and strategic decisions require diverse perspectives. Izvor replaces isolated single-prompt chat interactions with structured, multi-persona deliberations directly inside Slack threads.

Instead of one generic assistant answering every prompt, Izvor orchestrates specialized personas (such as *Visionary*, *Pragmatist*, *Skeptic*, *Security Architect*, *LegalAdvisor*, and *Scribe*) that deliberate, critique, and synthesize decisions using defined orchestration topologies.

---

## 🏗️ High-Level System Architecture

```mermaid
graph TD
    User([Slack User]) -->|Mentions / Slash Directives| SlackAPI[Slack Platform]
    SlackAPI -->|HTTP POST /slack/events| FastAPIApp[FastAPI Server in main.py]

    subgraph Core Pipeline
        FastAPIApp --> Signature[Slack Signature Verification]
        Signature --> EventDedup[Event Deduplication & Generation Check]
        EventDedup --> DirectiveParser[Directive & Tag Parser]
        DirectiveParser --> TurnPipeline[Turn Execution Pipeline]
    end

    subgraph Orchestration Layer
        TurnPipeline --> AgentSettings[AgentSettingsManager - SSOT]
        TurnPipeline --> ThreadState[ThreadStateManager - TTL Eviction]
        TurnPipeline --> Topology[SandwichOrchestrator - Sandwich / Fixed]
        TurnPipeline --> EntityResolver[Entity Resolver - Persona Matching]
    end

    subgraph Tooling & Governance
        TurnPipeline --> MCPTools[Safe MCP Workspace & Web Tools]
        TurnPipeline --> InPlaceStatus[5s Delayed Progress & In-Place Edit]
        TurnPipeline --> SpendGov[Spend Governance & Circuit Breakers]
        TurnPipeline --> PricingService[Pricing Research Agent & 7-Day Cache]
    end

    subgraph LLM Layer
        TurnPipeline --> LiteLLM[LiteLLM Provider Router]
        LiteLLM --> Models[OpenRouter / Anthropic / OpenAI / Gemini / Mistral]
    end

    TurnPipeline -->|Final In-Place Edit / Dispatch| SlackAPI
```

---

## ✨ Key Architectural Pillars

### 1. Multi-Persona Discussion Topologies
Izvor does not rely on rigid turn-taking. It supports:
- **🥪 Sandwich Mode**: Starts with designated initial thinkers (*Start Chain*), transitions through an open debate or peer-nominated pool (*Middle Pool*), and concludes with an authoritative synthesis by designated closers (*End Chain*).
- **🎯 Fixed Mode**: Executes an ordered, sequential chain of personas defined by the operator or preset.
- **Dynamic Peer Nomination**: Personas naturally pass the baton by referencing peers using bracket notation (e.g., `[Security Architect]`, `[Pragmatist]`), parsed automatically by the [`EntityResolver`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/pipeline/entity_resolver).

### 2. Thread State & Memory Safety
- **Bounded In-Memory State**: Managed by [`ThreadStateManager`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/orchestration/thread_state.py). Inactive threads are evicted after an idle TTL via background pruning ([`periodic_thread_state_cleanup`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/main.py#L55-L69)).
- **Generation-Based Superseding**: If a human user replies to an ongoing deliberation, the thread generation counter increments. Obsolete background agent turns detect generation divergence and gracefully abort without race conditions.

### 3. Asynchronous Tool Execution & Non-Blocking Slack UX
- **Wall-Clock Guard**: Tool loops are bounded by `TOOL_CALL_LOOP_TIMEOUT_SECONDS` (default: 60s) to prevent runaway recursive calls.
- **5-Second Delayed Status Indicator**: If a tool call takes longer than 5 seconds, Izvor posts an intermediate placeholder (e.g., `_🔍 *Visionary* is searching the web..._`). Fast calls (<5s) complete silently without UI jitter.
- **In-Place Message Editing**: When tool output resolves, the placeholder is edited in-place with the final response, avoiding cluttered Slack threads.

### 4. Spend Governance & Model Pricing
- **Autonomous Token & Cost Tracking**: Detailed prompt and completion tokens are recorded for each turn via [`services/pricing_service.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/services/pricing_service.py).
- **Disk-Backed 7-Day Pricing Cache**: An autonomous pricing agent dynamically checks official model provider pricing tables, caching per-token rates in `data/model_pricing_cache.json`.
- **Circuit Breaker**: When cumulative thread expense exceeds configured USD thresholds, the debate is safely paused, and a spend warning is posted.

### 5. Interactive Slack App Home & Administrative SSOT
- **Live Configuration**: Slack workspace admins can configure debate presets, agent rosters, circuit breaker thresholds, and topologies directly in the Slack App Home tab ([`services/slack_home_views.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/services/slack_home_views.py)).
- **Resilient Persistence**: Changes are committed atomically to `data/agent_settings.json` and `data/app_settings.json`, backed by automated 7-day emergency failover archives.

### 6. Non-Repudiable Cryptographic Audit Logging
- Every administrative action, security event, and RAG synchronization is recorded in a cryptographically chained, HMAC-verified JSONL audit log ([`services/audit_logger.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/services/audit_logger.py)).
- Continuous integrity scans ([`core/integrity_checks.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/core/integrity_checks.py)) detect any tampering or key mismatch, instantly dispatching high-priority admin alerts.

---

## 🔗 Next Steps

- Proceed to the [Installation Guide](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/docs/getting-started/installation.md) to set up Izvor locally.
- Configure your Slack Bot using the [Slack App Setup Guide](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/docs/getting-started/slack-app-setup.md).
