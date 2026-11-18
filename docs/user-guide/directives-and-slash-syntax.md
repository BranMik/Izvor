# Directives & Slash Command Syntax User Guide

This guide details slash command syntax, parameter clamping boundaries, Slack Admin privilege gates, and natural language controls supported by **Izvor** for steering multi-agent debates in Slack.

---

## 🔤 Slash Command Syntax

Users control debates using explicit slash commands embedded within messages or standalone turns. Multiple directives can be combined in a single turn.

### 1. Word Count Constraints
Control verbosity and prevent runaway token expenditure. Clamping bounds are enforced centrally by `INT_MODIFIER_BOUNDS` in [`core/models.py`](file:///engine/core/models.py#L147-L153).

| Slash Command | Registered Slash Aliases | Clamping Limits | Privilege Level | Description |
|---|---|---|---|---|
| `/max_words 150` | `/max-words`, `/maxwords`, `/max` | $1 \le N \le 2000$ | Standard (`off` = **Slack Admin**) | Enforces an upper limit of words per persona turn. |
| `/min_words 75` | `/min-words`, `/minwords`, `/min` | $1 \le N \le 1000$ | Standard (`off` = **Slack Admin**) | Requires personas to elaborate with at least $N$ words. |
| `/scribe_max_words 400` | `/scribe-max-words`, `/scribemaxwords` | $1 \le N \le 2000$ | Standard (`off` = **Slack Admin**) | Restricts the Scribe synthesis word count ceiling. |
| `/scribe_min_words 200` | `/scribe-min-words`, `/scribeminwords` | $1 \le N \le 2000$ | Standard (`off` = **Slack Admin**) | Ensures the Scribe synthesis has at least $N$ words. |

> [!NOTE]
> **Compact Override & Word Limit Grace Buffer**:
> - When `max_words < 60`, [`prompts/builder.py`](file:///engine/prompts/builder.py#L222-L233) activates **Compact Override Mode**, stripping multi-section `<output_contract>` formatting so agents respond in a single concise paragraph.
> - Post-generation mechanical truncation ([`apply_mechanical_word_limit`](file:///engine/orchestration/stages/post_record.py#L61-L99)) applies a configurable `word_limit_grace_buffer` (default: `50` words in [`config/settings/system_settings.json`](file:///config/settings/system_settings.json), or **`0` words** when `max_words < 20`) while preserving trailing `/[Agent]` nomination tags.

*Examples:*
```
/max 120 Evaluate using Stripe vs. Adyen for international multi-currency processing.
```
```
@Izvor /max_words 120 /min_words 60 What are the tradeoffs between CockroachDB and Spanner?
```

---

### 2. Discussion Rounds & History Slicing
Control how many debate cycles take place before synthesis, and slice conversational history.

| Slash Command | Registered Slash Aliases | Limits & Bounds | Privilege Level |
|---|---|---|---|
| `/rounds 2` | `/round`, `/next_round`, `/next-round`, `/nextround` | Clamped $1 \le N \le 5$ (Global cap: `20` turns) | **Slash Origin Only** (`auto_detect: false`) |
| `/history 15` | — | Clamped $0 \le N \le 50$ (`/history 0` ignores all prior messages) | Standard (`auto_detect: true`) |

> [!IMPORTANT]
> To prevent adversarial prompt injection from escalating debate costs or locking a thread in an infinite loop, `/rounds` directives require explicit slash syntax (`origin: "slash"`). When `/history N` slices earlier messages, Izvor automatically injects the Scribe's `<episodic_thread_summary>` so historical context is preserved.

---

### 3. Agent Inclusion, Exclusion & Speaker Selection
Selectively invite personas, dismiss personas, nominate next speakers, or trigger an open dynamic debate:

| Slash Command | Registered Slash Aliases | Privilege Level | Effect |
|---|---|---|---|
| `/include SecurityArchitect LegalAdvisor` | — | Standard (`off` = **Slack Admin**) | Adds specific personas to the active debate pool (`auto_detect: true`). |
| `/exclude Sceptic` | — | Standard (`off` = **Slack Admin**) | Excludes a persona from participating in this thread (`auto_detect: true`). |
| `/first Visionary` | — | **Slash Origin Only** | Designates the opening speaker, continuing in normal workflow order. |
| `/open_debate Visionary` | `/open-debate`, `/opendebate` | **Slash Origin Only** | Designates the opening speaker and runs an open debate without fixed start/end chains. |
| `/[Visionary]` | `/[Visionary] /[SecurityArchitect]` | Standard | Explicit in-message tag syntax to direct a single-turn query (1 tag) or custom sequential chain (multiple tags). |

---

### 4. Dynamic Tool & Skill Assignment (Slack Admin Gated)
Workspace administrators (`SLACK_ADMIN_USER_IDS`) can grant or revoke tool capabilities dynamically in an active thread, or clear active thread constraints:

| Slash Command | Registered Slash Aliases | Privilege Required |
|---|---|---|
| `/grant_skill web_research /[Visionary]` | `/grant-skill` | **Slack Admin + Slash Origin** (`requires_admin: true`) |
| `/grant_skill_for_all web_research` | `/grant-skill-for-all`, `/grant_for_all`, `/grant-for-all` | **Slack Admin + Slash Origin** (`requires_admin: true`) |
| `/revoke_skill_for_all web_research` | `/revoke-skill-for-all`, `/revoke_for_all` | **Slack Admin + Slash Origin** (`requires_admin: true`) |
| `/revoke_constraint max_words` | `/revoke-constraint`, `/revoke` (targets: `max_words`, `min_words`, `scribe_max_words`, `scribe_min_words`, `exclude`, `include`, `language`, `modifiers`, `all`) | **Slack Admin + Slash Origin** (`requires_admin: true`) |

> [!CAUTION]
> Governed declaratively by `requires_admin: true` in `SKILL.md` frontmatter and enforced centrally in [`orchestration/stages/directive_handlers.py`](file:///engine/orchestration/stages/directive_handlers.py), `/grant_skill`, `/grant_skill_for_all`, `/revoke_skill_for_all`, `/revoke_constraint`, and disabling constraints via `=off` require **Slack Workspace Admin** privileges (`is_slack_admin(user_id)`) AND explicit slash invocation (`origin: "slash"`). Unauthorized attempts are rejected and emit an `UNAUTHORIZED_DIRECTIVE_ATTEMPT` security audit log; authorized executions emit `ADMIN_DIRECTIVE_EXECUTED`.

---

### 5. Control, Utility & Agent Tool Skill Directives

| Slash Command | Registered Slash Aliases | Scope | Effect |
|---|---|---|---|
| `/stop` | — | `orchestrator` (`terminal: true`) | Immediately halts automated agent debate turns in the thread. |
| `/list_skills` | `/list-skills`, `/skills` | `orchestrator` | Invokes the `ListSkills` system agent to post a thread-aware directory of skills and authorized personas. |
| `/web_research <query>` | `/webresearch` | `agent` ([`config/skills/web_research/SKILL.md`](file:///config/skills/web_research/SKILL.md)) | Requests live web search (`web_search`) and page extraction (`fetch_web_page`). |
| `/rag <query>` | `/search_docs`, `/knowledge` | `agent` ([`config/skills/knowledge_base_rag/SKILL.md`](file:///config/skills/knowledge_base_rag/SKILL.md)) | Queries the hybrid LanceDB + Tantivy BM25 knowledge base (`search_knowledge_rag`). |
| `/read_file <filename>` | `/read-file`, `/file`, `/attachment`, `/document_reader` | `agent` ([`config/skills/document_reader/SKILL.md`](file:///config/skills/document_reader/SKILL.md)) | Reads an attached Slack file or document in `agents_resources/` (`read_document`, `list_documents`). |
| `/document_authoring <filename>` | `/authoring`, `/write_doc` | `agent` ([`config/skills/document_authoring/SKILL.md`](file:///config/skills/document_authoring/SKILL.md)) | Requests authoring/saving a file to `agents_resources/` (`write_document`, gated by Slack HITL approval). |

---

## 🗣️ Unified Pre-Flight Intent & Language Extraction

Izvor's [`nlp/intent_extractor.py`](file:///engine/nlp/intent_extractor.py) extracts safe conversational directives (`auto_detect: true`), `is_pure_skill_task`, and the user's `detected_language` in a single pre-flight LLM pass:

- *"Please keep all answers under 100 words"* ➔ Automatically parses as `max_words = 100` (`origin: "inferred"`).
- *"Have the security architect look at this"* ➔ Automatically includes `SecurityArchitect` (`include_agents`).
- *"Search our internal docs for the rollback runbook"* ➔ Automatically infers action `knowledge_base_rag` and prioritizes RAG-equipped agents.

**Privilege Boundary Safeguard**: Directives inferred from natural language are tagged with `origin: "inferred"`. Any skill configured with `requires_admin: true` or `auto_detect: false` (such as `/grant_skill`, `/rounds`, `/stop`, `/first`, `/open_debate`, `/revoke_constraint`) strictly requires `origin: "slash"` and fails closed if inferred from natural language.
