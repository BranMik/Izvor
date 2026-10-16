# Built-in Skills & Directives Reference

This document provides a complete reference guide for all **18 built-in skills and directives** supported by **Izvor**, including their slash command syntax, parameter constraints, and security privilege requirements.

---

## 📚 Overview Table

| Skill Name | Type | Purpose / Description | Slash Syntax | Bracketed Syntax | Privilege Required |
|---|---|---|---|---|---|
| [`web_research`](#web_research) | Tool | Searches the live internet for benchmarks, news, and technical references. | `/grant_skill web_research to Agent` | `[grant: web_research to Agent]` | Standard |
| [`knowledge_base_rag`](#knowledge_base_rag) | Tool | Retrieves internal documentation and enterprise knowledge from `rag_knowledge/` (results clamped 1–10). | `/grant_skill knowledge_base_rag to Agent` | `[grant: knowledge_base_rag to Agent]` | Standard |
| [`document_reader`](#document_reader) | Tool | Inspects and parses attached files (PDFs, Markdown, source code, text). | `/grant_skill document_reader to Agent` | `[grant: document_reader to Agent]` | Standard |
| [`document_authoring`](#document_authoring) | Tool | Formats and outputs structured multi-page documentation or reports. | `/grant_skill document_authoring to Agent` | `[grant: document_authoring to Agent]` | Standard |
| [`min_words`](#min_words) | Constraint | Enforces a minimum response word count for debate personas. | `/min_words 100` | `[min_words: 100]` | Standard |
| [`max_words`](#max_words) | Constraint | Enforces a maximum response word count (clamped $\le 2000$). | `/max_words 250` | `[max_words: 250]` | Standard |
| [`rounds`](#rounds) | Constraint | Sets the number of debate rounds in Sandwich mode (clamped 1–5). | `/rounds 3` | `[rounds: 3]` | **Slash Origin Only** |
| [`stop_discussion`](#stop_discussion) | Directive | Immediately halts automated agent debate turns in the thread. | `/stop` | `[stop_discussion]` or `[stop]` | Standard |
| [`scribe_min_words`](#scribe_min_words) | Constraint | Sets a minimum word count specifically for the Scribe closing synthesis. | `/scribe_min_words 300` | `[scribe_min_words: 300]` | Standard |
| [`scribe_max_words`](#scribe_max_words) | Constraint | Sets a maximum word count specifically for the Scribe closing synthesis. | `/scribe_max_words 600` | `[scribe_max_words: 600]` | Standard |
| [`history`](#history) | Constraint | Constrains the history slicing window for turn context (clamped $\le 50$). | `/history 10` | `[history: 10]` | Standard |
| [`grant_skill`](#grant_skill) | Directive | Grants a specific skill or tool to a single designated persona. | `/grant_skill skill to Agent` | `[grant: skill to Agent]` | Standard |
| [`grant_skill_for_all`](#grant_skill_for_all) | Directive | Grants a skill or tool to all participating personas in the thread. | `/grant_skill_for_all skill` | `[grant_all: skill]` | **Slash Origin Only** |
| [`revoke_skill_for_all`](#revoke_skill_for_all) | Directive | Revokes a skill or tool from all participating personas in the thread. | `/revoke_skill_for_all skill` | `[revoke_all: skill]` | **Slash Origin Only** |
| [`revoke_constraint`](#revoke_constraint) | Directive | Clears active word count or round limit constraints. | `/revoke_constraint name` | `[revoke_constraint: name]` | Standard |
| [`include_agents`](#include_agents) | Directive | Adds specific personas to the active debate pool. | `/include AgentA, AgentB` | `[include: AgentA, AgentB]` | Standard |
| [`exclude_agents`](#exclude_agents) | Directive | Removes specific personas from the active debate pool. | `/exclude AgentA` | `[exclude: AgentA]` | Standard |
| [`list_skills`](#list_skills) | Directive | Posts a list of all currently available skills and tool schemas in Slack. | `/list_skills` | `[list_skills]` | Standard |

---

## 🛠️ Detailed Skill Specifications & Clamps

### `web_research`
- **Location**: [`skills/web_research/SKILL.md`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/skills/web_research/SKILL.md)
- **Tool Name**: `web_research`
- **Description**: Connects the LLM to DuckDuckGo/Google search backends to fetch external facts, documentation, or pricing tables.
- **Input Parameters**: `{"query": "string", "num_results": "integer"}`

### `knowledge_base_rag`
- **Location**: [`skills/knowledge_base_rag/SKILL.md`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/skills/knowledge_base_rag/SKILL.md)
- **Tool Name**: `search_knowledge_base`
- **Description**: Performs semantic vector and SQLite FTS full-text search against files in `rag_knowledge/shared/` and persona directories.
- **Result Clamping**: Query results are strictly clamped between `1` and `10` chunks to prevent context overflow.
- **Indexing**: Full-text search (FTS) indexes are rebuilt in batched chunks to maintain low latency.
- **Input Parameters**: `{"query": "string", "limit": "integer"}`

### `document_reader`
- **Location**: [`skills/document_reader/SKILL.md`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/skills/document_reader/SKILL.md)
- **Tool Name**: `read_document`
- **Description**: Parses files uploaded to Slack threads or local workspace documents. Extracts text from PDFs, Markdown, CSVs, and code files. Wrapped in `<untrusted_file_content>` tags.

### `min_words` & `max_words`
- **Locations**: [`skills/min_words/SKILL.md`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/skills/min_words/SKILL.md), [`skills/max_words/SKILL.md`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/skills/max_words/SKILL.md)
- **Bounds**: `max_words` is clamped to $\le 2000$ words to prevent runaway token expenditure.
- **Mechanism**: Injects prompt instructions into Stage 3 Context Preparation and applies post-generation checks.

### `rounds`
- **Location**: [`skills/rounds/SKILL.md`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/skills/rounds/SKILL.md)
- **Bounds**: Clamped between `1` and `5` rounds. System enforces a global maximum limit of `MAX_DISCUSSION_TURNS = 20`.
- **Privilege Boundary**: Must have `origin: "slash"` (cannot be activated by inferred conversational text).

### `grant_skill_for_all` & `revoke_skill_for_all`
- **Locations**: [`skills/grant_skill_for_all/SKILL.md`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/skills/grant_skill_for_all/SKILL.md), [`skills/revoke_skill_for_all/SKILL.md`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/skills/revoke_skill_for_all/SKILL.md)
- **Privilege Boundary**: Must have `origin: "slash"`. Any inferred attempt fails closed with a warning.
- **Persistence**: Writes grants to SQLite table `thread_skills` and emits audit events (`THREAD_SKILL_GRANTED`, `THREAD_SKILL_REVOKED`).
