# Built-in Skills & Directives Reference

This document provides a complete reference guide for all **18 built-in skills and directives** supported by **Izvor**.

---

## 📚 Overview Table

| Skill Name | Type | Purpose / Description | Example Directive Syntax |
|---|---|---|---|
| [`web_research`](#web_research) | Tool | Searches the live internet for up-to-date benchmarks, news, and technical references. | `[grant: web_research to Visionary]` |
| [`knowledge_base_rag`](#knowledge_base_rag) | Tool | Retrieves internal documentation and enterprise knowledge from `rag_knowledge/`. | `[grant: knowledge_base_rag to SecurityArchitect]` |
| [`document_reader`](#document_reader) | Tool | Inspects and parses attached files (PDFs, Markdown, source code, text). | `[grant: document_reader to ROIStrategist]` |
| [`document_authoring`](#document_authoring) | Tool | Formats and outputs structured multi-page documentation or reports. | `[grant: document_authoring to CreativeAdvisor]` |
| [`min_words`](#min_words) | Constraint | Enforces a minimum response word count for debate personas. | `[min_words: 100]` |
| [`max_words`](#max_words) | Constraint | Enforces a maximum response word count to ensure conciseness. | `[max_words: 250]` |
| [`rounds`](#rounds) | Constraint | Sets the maximum number of discussion rounds before moving to closure. | `[rounds: 3]` |
| [`stop_discussion`](#stop_discussion) | Directive | Immediately halts automated agent debate turns in the thread. | `[stop_discussion]` or `[stop]` |
| [`scribe_min_words`](#scribe_min_words) | Constraint | Sets a minimum word count specifically for the Scribe closing synthesis. | `[scribe_min_words: 300]` |
| [`scribe_max_words`](#scribe_max_words) | Constraint | Sets a maximum word count specifically for the Scribe closing synthesis. | `[scribe_max_words: 600]` |
| [`grant_skill`](#grant_skill) | Directive | Grants a specific skill or tool to a single designated persona. | `[grant: web_research to Innovator]` |
| [`grant_skill_for_all`](#grant_skill_for_all) | Directive | Grants a skill or tool to all participating personas in the thread. | `[grant_all: web_research]` |
| [`revoke_skill_for_all`](#revoke_skill_for_all) | Directive | Revokes a skill or tool from all participating personas in the thread. | `[revoke_all: web_research]` |
| [`revoke_constraint`](#revoke_constraint) | Directive | Clears active word count or round limit constraints. | `[revoke_constraint: max_words]` |
| [`include_agents`](#include_agents) | Directive | Adds specific personas to the active debate pool. | `[include: SecurityArchitect, LegalAdvisor]` |
| [`exclude_agents`](#exclude_agents) | Directive | Removes specific personas from the active debate pool. | `[exclude: Sceptic]` |
| [`list_skills`](#list_skills) | Directive | Posts a list of all currently available skills and tool schemas in Slack. | `[list_skills]` |
| [`thread_history`](#thread_history) | Tool | Allows an agent to fetch and analyze earlier turns in long threads. | Built-in context helper |

---

## 🛠️ Detailed Skill Specifications

### `web_research`
- **Location**: [`skills/web_research/SKILL.md`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/skills/web_research/SKILL.md)
- **Tool Name**: `web_research`
- **Description**: Connects the LLM to DuckDuckGo/Google search backends to fetch external facts, documentation, or pricing tables.
- **Input Parameters**: `{"query": "string", "num_results": "integer"}`

### `knowledge_base_rag`
- **Location**: [`skills/knowledge_base_rag/SKILL.md`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/skills/knowledge_base_rag/SKILL.md)
- **Tool Name**: `search_knowledge_base`
- **Description**: Performs semantic and keyword queries against files in `rag_knowledge/shared/` and persona-specific knowledge directories (e.g., `rag_knowledge/agent-10008/`).
- **Input Parameters**: `{"query": "string", "limit": "integer"}`

### `document_reader`
- **Location**: [`skills/document_reader/SKILL.md`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/skills/document_reader/SKILL.md)
- **Tool Name**: `read_document`
- **Description**: Parses files uploaded to Slack threads or local workspace documents. Extracts text from PDFs, Markdown, CSVs, and code files.
- **Input Parameters**: `{"file_id": "string", "file_path": "string"}`

### `document_authoring`
- **Location**: [`skills/document_authoring/SKILL.md`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/skills/document_authoring/SKILL.md)
- **Description**: Guides the agent to produce formally structured deliverables (Executive Summaries, Architecture Review Boards, RFCs).

### `min_words` & `max_words`
- **Locations**: [`skills/min_words/SKILL.md`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/skills/min_words/SKILL.md), [`skills/max_words/SKILL.md`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/skills/max_words/SKILL.md)
- **Mechanism**: Injects strong system prompt guardrails into Stage 2 Context Prep, and enforces post-processing mechanical truncation if an agent significantly overshoots bounds.

### `rounds`
- **Location**: [`skills/rounds/SKILL.md`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/skills/rounds/SKILL.md)
- **Mechanism**: Configures the total number of debate passes in Sandwich mode before transitioning from the middle pool to the end chain.

### `grant_skill` & `grant_skill_for_all`
- **Locations**: [`skills/grant_skill/SKILL.md`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/skills/grant_skill/SKILL.md), [`skills/grant_skill_for_all/SKILL.md`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/skills/grant_skill_for_all/SKILL.md)
- **Mechanism**: Modifies thread-scoped skill permissions inside [`ThreadSkillsManager`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/orchestration/thread_skills_manager.py). Tool definitions become immediately available on the subsequent agent turn.
