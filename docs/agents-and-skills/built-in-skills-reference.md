# Built-in Skills & Directives Reference

This document provides a complete reference guide for all **22 platform skills and directives** (**4 configurable** in `config/skills/` and **18 built-in** in `engine/skills/builtin/`) supported by **Izvor**, including their tool names, slash command syntax, parameter constraints, and security privilege requirements.

---

## 📚 Overview Table

| Skill Name | Type | Purpose / Description | Slash Syntax | Bracketed Syntax | Privilege Required |
|---|---|---|---|---|---|
| [`control_plane`](#control_plane) | Global Tools | Structured orchestration tools (`nominate_next_speaker`, `request_user_clarification`, `signal_consensus_convergence`). | *(Global to all agents)* | *(Global to all agents)* | Global Built-in |
| [`web_research`](#web_research) | Tool | Searches the live internet (`web_search`) and scrapes web pages (`fetch_web_page`) with SSRF/DNS-rebinding defenses. | `/web_research` | `[grant: web_research to Agent]` | Standard (Invoke) / **Slack Admin** (Grant) |
| [`knowledge_base_rag`](#knowledge_base_rag) | Tool | Retrieves internal hybrid LanceDB vector + Tantivy BM25 knowledge (`search_knowledge_rag`) from `config/rag_knowledge/` (clamped 1–10). | `/rag` | `[grant: knowledge_base_rag to Agent]` | Standard (Invoke) / **Slack Admin** (Grant) |
| [`document_reader`](#document_reader) | Tool | Lists (`list_documents`) and reads (`read_document`) workspace or attached files (PDF via `pypdf`, MD, TXT, CSV, JSON, YAML). | `/read_file` | `[grant: document_reader to Agent]` | Standard (Invoke) / **Slack Admin** (Grant) |
| [`document_authoring`](#document_authoring) | Tool (HITL) | Writes markdown/text reports (`write_document`) to `agents_resources/` with mandatory Human-in-the-Loop approval. | `/document_authoring` | `[grant: document_authoring to Agent]` | **HITL Approval** (Execute) / **Slack Admin** (Grant) |
| [`min_words`](#min_words) | Constraint | Enforces a minimum response word count for debate personas (`off` requires Slack Admin). | `/min_words 100` | `[min_words: 100]` | Standard (`off` = **Slack Admin**) |
| [`max_words`](#max_words) | Constraint | Enforces a maximum response word count (clamped $\le 2000$; `off` requires Slack Admin). | `/max_words 250` | `[max_words: 250]` | Standard (`off` = **Slack Admin**) |
| [`rounds`](#rounds) | Constraint | Sets the number of debate rounds in Sandwich mode (clamped 1–5). | `/rounds 3` | `[rounds: 3]` | **Slash Origin Only** |
| [`first`](#first) | Directive | Designates the opening speaker while preserving normal workflow chain progression. | `/first Agent` | `[first: Agent]` | **Slash Origin Only** |
| [`open_debate`](#open_debate) | Directive | Designates opening speaker and runs open debate with dynamic peer nominations (bypassing fixed chains). | `/open_debate Agent` | `[open_debate: Agent]` | **Slash Origin Only** |
| [`stop_discussion`](#stop_discussion) | Directive | Immediately halts automated agent debate turns in the thread. | `/stop` | `[stop_discussion]` or `[stop]` | **Slash Origin Only** |
| [`scribe_min_words`](#scribe_min_words) | Constraint | Sets a minimum word count specifically for the Scribe closing synthesis (`off` requires Slack Admin). | `/scribe_min_words 300` | `[scribe_min_words: 300]` | Standard (`off` = **Slack Admin**) |
| [`scribe_max_words`](#scribe_max_words) | Constraint | Sets a maximum word count specifically for the Scribe closing synthesis (`off` requires Slack Admin). | `/scribe_max_words 600` | `[scribe_max_words: 600]` | Standard (`off` = **Slack Admin**) |
| [`thread_history`](#thread_history) | Constraint | Constrains the Tier 1 history slicing window for turn context (clamped $\le 50$). | `/history 10` | `[history: 10]` | Standard |
| [`grant_skill`](#grant_skill) | Directive | Grants a specific skill or tool to a single designated persona. | `/grant_skill skill to Agent` | `[grant: skill to Agent]` | **Slack Admin + Slash Origin** |
| [`grant_skill_for_all`](#grant_skill_for_all) | Directive | Grants a skill or tool to all participating personas in the thread. | `/grant_skill_for_all skill` | `[grant_all: skill]` | **Slack Admin + Slash Origin** |
| [`revoke_skill_for_all`](#revoke_skill_for_all) | Directive | Revokes a skill or tool from all participating personas in the thread. | `/revoke_skill_for_all skill` | `[revoke_all: skill]` | **Slack Admin + Slash Origin** |
| [`revoke_constraint`](#revoke_constraint) | Directive | Clears active word count, round limit, or include/exclude constraints. | `/revoke_constraint name` | `[revoke_constraint: name]` | **Slack Admin + Slash Origin** |
| [`include_agents`](#include_agents) | Directive | Adds specific personas to the active debate pool (`off` requires Slack Admin). | `/include AgentA, AgentB` | `[include: AgentA, AgentB]` | Standard (`off` = **Slack Admin**) |
| [`exclude_agents`](#exclude_agents) | Directive | Removes specific personas from the active debate pool (`off` requires Slack Admin). | `/exclude AgentA` | `[exclude: AgentA]` | Standard (`off` = **Slack Admin**) |
| [`list_skills`](#list_skills) | Directive | Posts a list of all currently available skills and tool schemas in Slack. | `/list_skills` | `[list_skills]` | **Slash Origin Only** |
| [`forget_thread`](#forget_thread) | GDPR Purge | Permanently purges persisted SQLite records, flat histories, thread notes, downloads, and caches for the current thread. | `/forget_thread` (`/forget-thread`) | — *(Slash Only)* | **Slack Admin + Slash Origin** |

---

## 🛠️ Detailed Skill Specifications & Clamps

### `control_plane`
- **Location**: [`skills/control_plane/SKILL.md`](file:///engine/skills/builtin/control_plane/SKILL.md)
- **Scope**: `orchestrator` (`global: true` — automatically injected for all personas)
- **Tools**:
  - `nominate_next_speaker(agent_name: str, reason: str)`: Structured handoff to nominate the next persona in the deliberation.
  - `request_user_clarification(question: str, blocking_reason: str)`: Suspends automated debate turns and prompts the human user in Slack when critical requirements are ambiguous.
  - `signal_consensus_convergence(summary_of_agreement: str, confidence: float)`: Signals that the panel has converged on consensus; when consecutive calls reach `convergence_consecutive_threshold` (`2`), triggers the Challenger + Arbitrator stress-test gate.

### `web_research`
- **Location**: [`skills/web_research/SKILL.md`](file:///config/skills/web_research/SKILL.md)
- **Tools**:
  - `web_search(query: str, max_results: int = 5)`: Searches the live web via multi-engine `ddgs` search (`max_results` clamped 1–10).
  - `fetch_web_page(url: str, max_chars: int = 4000)`: Fetches and extracts clean text from an HTTP/HTTPS URL (`max_chars` clamped up to 15,000), protected by `PinnedIPTransport` DNS-rebinding defense and a per-turn budget of 5 requests (`max_fetches_per_turn`).

### `knowledge_base_rag`
- **Location**: [`skills/knowledge_base_rag/SKILL.md`](file:///config/skills/knowledge_base_rag/SKILL.md)
- **Tool Name**: `search_knowledge_rag(query: str, scope: str = "all", max_results: int = 6)`
- **Description**: Performs hybrid LanceDB semantic vector search and Tantivy BM25 full-text search with Reciprocal Rank Fusion (RRF) across `config/rag_knowledge/shared/` and agent-private folders (`scope`: `"all"`, `"shared"`, or `"self"`).
- **Result Clamping**: `max_results` is strictly clamped between `1` and `10` chunks to prevent context overflow. Cached in `RoundEvidenceStore` scoped by `agent_key`.

### `document_reader`
- **Location**: [`skills/document_reader/SKILL.md`](file:///config/skills/document_reader/SKILL.md)
- **Tools**:
  - `list_documents(rel_path: str = ".")`: Lists non-escaping files and subdirectories inside `agents_resources/`.
  - `read_document(rel_path: str)`: Extracts text from PDFs (`pypdf`), Markdown, CSVs, JSON/YAML, and UTF-8 text files in `agents_resources/` or thread attachments (bounded to `MAX_READ_DOCUMENT_CHARS = 50000`). Wrapped in `<untrusted_file_content>` tags.

### `document_authoring`
- **Location**: [`skills/document_authoring/SKILL.md`](file:///config/skills/document_authoring/SKILL.md)
- **Tool Name**: `write_document(rel_path: str, content: str)`
- **Security & HITL Gate**: Configured with `requires_human_ITL: true`. Invoking `write_document` pauses the turn via `TurnCheckpointStore` and posts an interactive Approve/Deny prompt to Slack. Writes atomically via temp file + `os.replace()`, confined to `agents_resources/`, blocked from writing to `DEFAULT_PROTECTED_WRITE_PATHS` or Windows reserved device names, and capped at `max_write_bytes` (1 MB).

### `min_words` & `max_words`
- **Locations**: [`skills/min_words/SKILL.md`](file:///engine/skills/builtin/min_words/SKILL.md), [`skills/max_words/SKILL.md`](file:///engine/skills/builtin/max_words/SKILL.md)
- **Bounds**: `max_words` is clamped to $\le 2000$ words to prevent runaway token expenditure.
- **Admin Gate on `off`**: Setting `/max_words off` or `/min_words off` requires Slack Admin privileges (`is_slack_admin`).

### `rounds`
- **Location**: [`skills/rounds/SKILL.md`](file:///engine/skills/builtin/rounds/SKILL.md)
- **Bounds**: Clamped between `1` and `5` rounds. System enforces a global maximum limit of `MAX_DISCUSSION_TURNS = 20`.
- **Privilege Boundary**: Must have `origin: "slash"` (cannot be activated by inferred conversational text).

### `first`
- **Location**: [`skills/first/SKILL.md`](file:///engine/skills/builtin/first/SKILL.md)
- **Trigger**: `/first <Agent>`
- **Description**: Sets the specified agent as the initial speaker to start the panel discussion, after which the debate continues in its normal workflow chain order.

### `open_debate`
- **Location**: [`skills/open_debate/SKILL.md`](file:///engine/skills/builtin/open_debate/SKILL.md)
- **Triggers**: `/open_debate <Agent>`, `/open-debate <Agent>`, or `/opendebate <Agent>`
- **Description**: Designates the specified agent as the opening speaker and bypasses fixed start/end workflow chains. All agents participate in an open dynamic debate (up to 3 rounds) where each speaker nominates the next peer.

### `grant_skill`, `grant_skill_for_all`, `revoke_skill_for_all`, `revoke_constraint` & `forget_thread`
- **Locations**: [`skills/grant_skill/SKILL.md`](file:///engine/skills/builtin/grant_skill/SKILL.md), [`skills/grant_skill_for_all/SKILL.md`](file:///engine/skills/builtin/grant_skill_for_all/SKILL.md), [`skills/revoke_skill_for_all/SKILL.md`](file:///engine/skills/builtin/revoke_skill_for_all/SKILL.md), [`skills/revoke_constraint/SKILL.md`](file:///engine/skills/builtin/revoke_constraint/SKILL.md), [`skills/forget_thread/SKILL.md`](file:///engine/skills/builtin/forget_thread/SKILL.md)
- **Privilege Boundary**: Defined declaratively via `requires_admin: true` and `auto_detect: false` in `SKILL.md` frontmatter and enforced centrally at Stage 1 in [`orchestration/stages/directive_handlers.py`](file:///engine/orchestration/stages/directive_handlers.py). Requires both `origin: "slash"` and **Slack Workspace Admin** membership (`is_slack_admin`).
- **Persistence & Auditing**: Skill grants write to SQLite table `thread_skills` (`THREAD_SKILL_GRANTED`, `THREAD_SKILL_REVOKED`); `/forget_thread` (`/forget-thread`) executes channel-scoped `forget_thread(channel_id, thread_ts)` GDPR erasure (`THREAD_DATA_PURGED`, `GDPR_THREAD_FORGOTTEN`); unauthorized attempts emit `UNAUTHORIZED_DIRECTIVE_ATTEMPT`.
