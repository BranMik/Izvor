# Personas Catalog & Roster

This document provides a comprehensive catalog of all pre-configured debate personas, system agents, and prompt construction mechanics in **Izvor**.

---

## 👥 Persona Philosophy

In Izvor, personas are not simply "chatbots" with different system instructions. Each persona represents an executive or engineering stakeholder with:
1. A distinct domain perspective (e.g., technical risk vs. commercial ROI).
2. A specific default reasoning model (e.g., Gemini 3.8 Flash, Mistral Large, Claude Opus, GPT-5).
3. A set of authorized tools and skills (e.g., web search, RAG document retrieval).
4. Custom visual identities (avatar icons and display names) inside Slack.

---

## 📋 The Complete Persona Roster

The roster is managed dynamically in [`data/settings/agent_settings.json`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/data/settings/agent_settings.json) and configured via [`orchestration/agent_settings.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/orchestration/agent_settings.py):

| Agent ID | Display Name | Username Tag | Default LLM Model | Default Assigned Skills | Output Style |
|---|---|---|---|---|---|
| `agent-10001` | **Visionary** | `Visionary` | `gemini/gemini-3.8-flash` | None | Full |
| `agent-10002` | **Innovator** | `Innovator` | `anthropic/claude-opus-5` | None | Full |
| `agent-10003` | **Sceptic** | `Sceptic` | `anthropic/claude-opus-5` | None | Full |
| `agent-10004` | **Compliance, Privacy & Governance Officer** | `CompliancePrivacyAndGovernanceOfficer` | `mistral/mistral-large-latest` | None | Short / Concise |
| `agent-10005` | **Scope Optimizer** | `ScopeOptimizer` | `mistral/mistral-large-latest` | None | Short / Concise |
| `agent-10006` | **ROI Strategist** | `ROIStrategist` | `gemini/gemini-3.8-flash` | `document_authoring`, `document_reader`, `knowledge_base_rag` | Full |
| `agent-10007` | **User Advocate** | `UserAdvocate` | `openai/gpt-5.6-terra` | None | Full |
| `agent-10008` | **Security Architect** | `SecurityArchitect` | `mistral/mistral-large-latest` | `document_reader`, `knowledge_base_rag` | Full |
| `agent-10009` | **Market & Competition Research Expert** | `MarketAndCompetitionResearchExpert` | `mistral/mistral-large-latest` | `web_research` | Full |
| `agent-10010` | **Sales Advisor** | `SalesAdvisor` | `gemini/gemini-3.8-flash` | `knowledge_base_rag`, `web_research` | Short / Concise |
| `agent-10011` | **Legal Advisor** | `LegalAdvisor` | `gemini/gemini-3.8-flash` | `knowledge_base_rag`, `web_research` | Short / Concise |
| `agent-10012` | **Product Strategist** | `ProductStrategist` | `openai/gpt-5.6-terra` | None | Full |
| `agent-10013` | **Risk Auditor** | `RiskAuditor` | `gemini/gemini-3.8-flash` | None | Full |
| `agent-10014` | **Tactician** | `Tactician` | `gemini/gemini-3.8-flash` | None | Full |
| `agent-10015` | **Revisor** | `Revisor` | `gemini/gemini-3.8-flash` | `web_research`, `knowledge_base_rag` | Full |
| `agent-10016` | **Creative Advisor** | `CreativeAdvisor` | `gemini/gemini-3.8-flash` | `document_authoring`, `document_reader`, `knowledge_base_rag`, `web_research` | Full |

---

## 🛠️ System Agents

In addition to the debate roster, Izvor runs dedicated system-level agents:

### 1. The Orchestrator (`Orchestrator`)
- **Role**: Evaluates inbound user prompts, detects directives, selects the initial speaker chain, and manages debate transitions.
- **Models**: Configured with a fast model (`gemini/gemini-3.8-flash`) for routing and an advanced reasoning model (`mistral/mistral-large-latest`) for complex structural planning.

### 2. Pricing Research Agent (`Pricing_Agent`)
- **Prompt**: [`prompts/system_agents/Pricing_Agent.md`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/prompts/system_agents/Pricing_Agent.md)
- **Role**: Dispatched in the background to inspect official provider documentation and determine token pricing for new models.

### 3. Usage Reporter Agent (`Usage_Agent`)
- **Prompt**: [`prompts/system_agents/Usage_Agent.md`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/prompts/system_agents/Usage_Agent.md)
- **Role**: Posts token consumption and spend summaries to the dedicated usage channel (configured via `USAGE_CHANNEL_NAME`, default `#izvor-usage` or `izvor-usage`).

### 4. Scribe (`Scribe`)
- **Role**: Synthesizes end-of-discussion agreements, action items, and generates downloadable flat markdown exports.

### 5. Internal Revisor (`InternalRevisor`)
- **Role**: Inline validation agent evaluating candidate responses against user constraints and directives prior to dispatch, breaking endless tag loops and ensuring format fidelity.
- **Service**: Implemented in [`orchestration/revisor_service.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/orchestration/revisor_service.py).

### 6. Thread Notes Agent (`Thread_Notes_Agent`)
- **Role**: Asynchronous background synthesis agent generating and pinning live strategic synthesis notes and final dialectical conclusion notes to discussion threads.
- **Implementation**: Managed in [`agents/thread_notes_agent.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/agents/thread_notes_agent.py).

---

## 📝 Prompt Composition Engine

Personas' effective system prompts are assembled dynamically during **Stage 3: Context Preparation** ([`orchestration/stages/context_prep.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/orchestration/stages/context_prep.py)) using [`prompts/builder.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/prompts/builder.py):

1. **Base System Prompt**: Loaded from [`prompts/`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/prompts) or dynamic `AgentSettingsManager` overrides.
2. **Behavioral Guidelines**: Rules prohibiting self-repetition, requiring peer engagement, and enforcing civil intellectual tension.
3. **Word Constraints**: If `is_short_output: true` or if a directive (`[max_words: N]`, `[min_words: N]`) is active, instructions are clamped (`max_words ≤ 2000`) and injected into the prompt.
4. **File Attachments**: Any thread-attached files are safely sanitized and wrapped with `<untrusted_file_content>` delimiters.
5. **Active Tools & Skills**: Schemas of granted tools (e.g. search, document reader) are queried from [`ThreadSkillsManager`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/orchestration/thread_skills_manager.py) and appended to the LiteLLM tool definition payload.
