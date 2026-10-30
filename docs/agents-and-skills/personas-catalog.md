# Personas Catalog & Roster

This document provides a comprehensive catalog of all **17 pre-configured debate personas**, dialectical special roles, system agents, and prompt construction mechanics in **Izvor**.

---

## 👥 Persona Philosophy

In Izvor, personas are not simply "chatbots" with different system instructions. Each persona represents an executive or engineering stakeholder with:
1. A distinct domain perspective (e.g., technical risk vs. commercial ROI).
2. A specific default reasoning model (e.g., Gemini 3.8 Flash, Mistral Large, Claude Opus, GPT-5).
3. A set of authorized tools and skills (e.g., web search, RAG document retrieval).
4. An optional dialectical `special_role` (`NONE`, `CHALLENGER`, or `REBUTTAL`) used during consensus convergence stress-testing.
5. Custom visual identities (avatar icons and display names) inside Slack.

---

## 📋 The Complete 17-Persona Roster

The debate panel roster is managed dynamically in [`config/settings/agent_settings.json`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/config/settings/agent_settings.json) and configured via [`orchestration/agent_settings.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/orchestration/agent_settings.py):

| Agent ID | Display Name | Username Tag | Default LLM Model | Special Role | Default Assigned Skills | Output Style |
|---|---|---|---|---|---|---|
| `agent-10001` | **Visionary** | `Visionary` | `gemini/gemini-3.8-flash` | `NONE` | None | Full |
| `agent-10002` | **Innovator** | `Innovator` | `anthropic/claude-opus-5` | `CHALLENGER` | None | Full |
| `agent-10003` | **Sceptic** | `Sceptic` | `anthropic/claude-opus-5` | `CHALLENGER` | None | Full |
| `agent-10004` | **Compliance, Privacy & Governance Officer** | `CompliancePrivacyAndGovernanceOfficer` | `mistral/mistral-large-latest` | `NONE` | None | Short / Concise |
| `agent-10005` | **Scope Optimizer** | `ScopeOptimizer` | `mistral/mistral-large-latest` | `NONE` | None | Short / Concise |
| `agent-10006` | **ROI Strategist** | `ROIStrategist` | `gemini/gemini-3.8-flash` | `NONE` | `document_authoring`, `document_reader`, `knowledge_base_rag` | Full |
| `agent-10007` | **User Advocate** | `UserAdvocate` | `openai/gpt-5.6-terra` | `NONE` | None | Full |
| `agent-10008` | **Security Architect** | `SecurityArchitect` | `mistral/mistral-large-latest` | `NONE` | `document_reader`, `knowledge_base_rag` | Full |
| `agent-10009` | **Market & Competition Research Expert** | `MarketAndCompetitionResearchExpert` | `mistral/mistral-large-latest` | `NONE` | `web_research` | Full |
| `agent-10010` | **Sales Advisor** | `SalesAdvisor` | `gemini/gemini-3.8-flash` | `NONE` | `knowledge_base_rag`, `web_research` | Short / Concise |
| `agent-10011` | **Legal Advisor** | `LegalAdvisor` | `gemini/gemini-3.8-flash` | `NONE` | `knowledge_base_rag`, `web_research` | Short / Concise |
| `agent-10012` | **Product Strategist** | `ProductStrategist` | `openai/gpt-5.6-terra` | `NONE` | None | Full |
| `agent-10013` | **Risk Auditor** | `RiskAuditor` | `gemini/gemini-3.8-flash` | `NONE` | None | Full |
| `agent-10014` | **Tactician** | `Tactician` | `gemini/gemini-3.8-flash` | `NONE` | None | Full |
| `agent-10015` | **Revisor** | `Revisor` | `gemini/gemini-3.8-flash` | `NONE` | `web_research`, `knowledge_base_rag` | Full |
| `agent-10016` | **Creative Advisor** | `CreativeAdvisor` | `gemini/gemini-3.8-flash` | `NONE` | `document_authoring`, `document_reader`, `knowledge_base_rag`, `web_research` | Full |
| `agent-10017` | **Rebuttal & Defense Advocate** | `RebuttalAdvocate` | `mistral/mistral-large-latest` | `REBUTTAL` | `document_reader`, `knowledge_base_rag` | Full |

### Dialectical `special_role` Values
- **`NONE`**: Standard deliberation participant.
- **`CHALLENGER`** (`Innovator`, `Sceptic`): Automatically selected when consecutive convergence signals reach `convergence_consecutive_threshold` (`2`) to stress-test emerging consensus for blind spots.
- **`REBUTTAL`** (`RebuttalAdvocate`, prompt at [`config/personas/agent-10017.md`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/config/personas/agent-10017.md)): Immediately follows the Challenger during convergence stress-testing to defend the proposal and emit a binding verdict (`[VERDICT: A — CONSENSUS HOLDS]` to fast-forward to `end_chain`, or `[VERDICT: B — FLAWS EXPOSED]` to reset convergence and resume middle-pool debate).

---

## 🛠️ System Agents

In addition to the 17-persona debate roster in `config/settings/agent_settings.json`, Izvor runs dedicated engine-level system agents configured in [`engine/settings/system_agent_settings.json`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/settings/system_agent_settings.json):

### 1. The Orchestrator (`Orchestrator`)
- **Role**: Evaluates inbound user prompts, detects directives and language in a single pre-flight pass ([`nlp/intent_extractor.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/nlp/intent_extractor.py)), selects the initial speaker chain, and manages debate transitions.
- **Models**: Configured with a fast model (`gemini/gemini-3.8-flash`) for routing and an advanced reasoning model (`mistral/mistral-large-latest`) for complex structural planning.

### 2. Pricing Research Agent (`Pricing_Agent`)
- **Prompt**: [`prompts/system_agents/Pricing_Agent.md`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/prompts/system_agents/Pricing_Agent.md)
- **Role**: Dispatched in the background to inspect official provider documentation and determine token pricing for new models (validated within `$0.005–$500.00/1M` bounds).

### 3. Usage Reporter Agent (`Usage_Agent`)
- **Prompt**: [`prompts/system_agents/Usage_Agent.md`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/prompts/system_agents/Usage_Agent.md)
- **Role**: Posts token consumption and spend summaries to the dedicated usage channel (configured via `USAGE_CHANNEL_NAME`, default `#izvor-usage` or `izvor-usage`), persisting thread metadata in SQLite table `thread_usage_meta`.

### 4. Scribe (`Scribe`)
- **Role**: Synthesizes end-of-discussion agreements, action items, and generates downloadable flat markdown exports.

### 5. Internal Revisor (`InternalRevisor`)
- **Role**: Inline validation barrier running at the end of `TurnPipeline.run()` and `resume_from_checkpoint()` with a 3.5s timeout, evaluating responses against user constraints and injecting `[Internal Revisor Warning]` feedback into accumulated history for subsequent speakers.
- **Service**: Implemented in [`orchestration/revisor_service.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/orchestration/revisor_service.py).

### 6. Thread Notes Agent (`Thread_Notes_Agent`)
- **Role**: Asynchronous background synthesis agent generating and pinning live strategic synthesis notes and final dialectical conclusion notes to discussion threads, persisted in SQLite table `thread_notes_state` and injected as Tier 2 `<episodic_thread_summary>` memory.
- **Implementation**: Managed in [`agents/thread_notes_agent.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/agents/thread_notes_agent.py).

---

## 📝 Prompt Composition Engine

Personas' effective system prompts are assembled dynamically during **Stage 3: Context Preparation** ([`orchestration/stages/context_prep.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/orchestration/stages/context_prep.py)) using [`prompts/builder.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/prompts/builder.py):

1. **Base System Prompt**: Loaded from [`config/personas/`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/config/personas) (for debate personas) or [`engine/prompts/system_agents/`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/prompts/system_agents) (for system agents).
2. **Behavioral Guidelines**: Rules prohibiting self-repetition, requiring peer engagement, and enforcing civil intellectual tension.
3. **3-Tier Memory Context**: Injects `<episodic_thread_summary>` (when history is sliced) and `<shared_round_evidence>` (cached read-only tool outputs from `RoundEvidenceStore`).
4. **Word Constraints**: If `is_short_output: true` or if a directive (`[max_words: N]`, `[min_words: N]`) is active, instructions are clamped (`max_words ≤ 2000`) and injected into the prompt.
5. **File Attachments**: Any thread-attached files are safely sanitized and wrapped with `<untrusted_file_content>` delimiters.
6. **Active Tools & Control Plane Skills**: Schemas of granted tools (e.g. search, document reader) plus global `control_plane` tools (`nominate_next_speaker`, `request_user_clarification`, `signal_consensus_convergence`) are queried from [`ThreadSkillsManager`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/orchestration/thread_skills_manager.py) and appended to the LiteLLM tool definition payload.
