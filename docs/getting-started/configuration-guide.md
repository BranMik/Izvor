# Configuration & Customization Guide

This guide is the complete implementer reference for customizing **Izvor** — covering how to add or modify **Debate Agents**, **Agent Skills**, **External MCP Servers**, **Orchestration Workflows**, **RAG Knowledge Documents**, and **Agent Workspace Resources**.

## Table of Contents

- [1. Configuration Boundary & Reload Matrix](#1-configuration-boundary--reload-matrix)
  - [Quick-Reference Configuration Matrix](#quick-reference-configuration-matrix)
- [2. Adding & Modifying Debate Agents](#2-adding--modifying-debate-agents)
  - [2.1 `config/settings/agent_settings.json` Schema & Field Impact](#21-configsettingsagent_settingsjson-schema--field-impact)
  - [2.2 Example: Adding a New Debate Persona (`agent-10017`)](#22-example-adding-a-new-debate-persona-agent-10017)
- [3. Adding & Modifying Configurable Skills (`config/skills/`)](#3-adding--modifying-configurable-skills-configskills)
  - [3.1 Complete `SKILL.md` YAML Frontmatter Specification](#31-complete-skillmd-yaml-frontmatter-specification)
  - [3.2 Example A: Custom Python Tool-Backed Skill (`config/skills/jira_lookup/`)](#32-example-a-custom-python-tool-backed-skill-configskillsjira_lookup)
  - [3.3 Example B: Modifying an Existing Skill (`config/skills/`)](#33-example-b-modifying-an-existing-skill-configskills)
- [4. Connecting External MCP Servers](#4-connecting-external-mcp-servers)
  - [4.1 `mcp_servers` Configuration Reference (`config/settings/system_settings.json`)](#41-mcp_servers-configuration-reference-configsettingssystem_settingsjson)
  - [4.2 End-to-End Walkthrough: Connecting a Local (`stdio`) & Remote (`sse`) MCP Server](#42-end-to-end-walkthrough-connecting-a-local-stdio--remote-sse-mcp-server)
- [5. Adding & Modifying Workflows (`config/workflows/`)](#5-adding--modifying-workflows-configworkflows)
  - [5.1 `WorkflowPreset` JSON Schema & Field Impact](#51-workflowpreset-json-schema--field-impact)
  - [5.2 Workflow Examples (All 3 Modes)](#52-workflow-examples-all-3-modes)
  - [5.3 Activating & Editing Workflows](#53-activating--editing-workflows)
- [6. Adding & Modifying RAG Knowledge Documents (`config/rag_knowledge/`)](#6-adding--modifying-rag-knowledge-documents-configrag_knowledge)
  - [6.1 Directory Layout & Retrieval Scoping](#61-directory-layout--retrieval-scoping)
  - [6.2 Triggering Indexing (Live or on Startup)](#62-triggering-indexing-live-or-on-startup)
  - [6.3 Tuning RAG & Security Parameters (`config/settings/system_settings.json`)](#63-tuning-rag--security-parameters-configsettingssystem_settingsjson)
- [7. Managing Agent Workspace Resources (`agents_resources/`)](#7-managing-agent-workspace-resources-agents_resources)
  - [7.1 `agents_resources/` vs. `config/rag_knowledge/`](#71-agents_resources-vs-configrag_knowledge)
  - [7.2 Adding Files & Security Constraints](#72-adding-files--security-constraints)

---

## 1. Configuration Boundary & Reload Matrix

Izvor enforces a strict **3-Pillar separation** ([`engine/core/paths.py`](file:///engine/core/paths.py)):

- **`config/` & `agents_resources/` (Implementer-Owned — Customize Freely):** All debate panel personas, skills, workflows, RAG documents, external MCP server definitions, and operational settings live here.
- **`engine/` (Immutable Core Engine — Do Not Modify):** Core runtime Python modules, built-in system prompts, built-in directive skills, and protected seed workflows. Leave `engine/` untouched during deployment and configuration.
- **`data/` (Runtime State & Backups — Managed Automatically):** SQLite WAL database (`data/izvor.db`), LanceDB/Tantivy RAG indices (`data/.rag_cache/`), Slack App Home runtime state (`data/state/app_settings.json`), Presidio skill exclusions (`data/state/presidio_skill_exclusions.json`), and automatic rolling backups (`data/backups/`).

> [!TIP]
> You can relocate any pillar or file path at runtime via `IZVOR_*` environment variables in `.env` (for example `IZVOR_CONFIG_DIR`, `IZVOR_DATA_DIR`, `IZVOR_AGENTS_RESOURCES_DIR`, `IZVOR_RAG_KNOWLEDGE_DIR`). Relative paths resolve from the project root.

### Quick-Reference Configuration Matrix

| Target | Primary Location(s) | Slack UI / API Control | Reload Behavior | Automatic Backup |
|---|---|---|---|---|
| **Debate Agents** | [`config/settings/agent_settings.json`](file:///config/settings/agent_settings.json), [`config/personas/`](file:///config/personas), [`config/avatars/`](file:///config/avatars) | **App Home → Agents Tab** (`agents_tab.py`) | Hot-reloads on file mtime change (`sync_settings_if_modified`) for `agent_settings.json`; prompt edits reload on next settings sync or restart | `data/backups/agent_settings_backup.json` |
| **Configurable Skills** | [`config/skills/`](file:///config/skills) (`<skill_name>/SKILL.md`, optional `tools.py`, `action.py`) | `GET /api/skills`, `@Izvor /list_skills`, `/grant_skill` | Loaded on startup and re-synced periodically (`skill_exclusions_sync_interval_seconds`); restart recommended for immediate `tools.py` / `SKILL.md` changes | Presidio allowlist synced to `data/state/presidio_skill_exclusions.json` |
| **External MCP Servers** | `mcp_servers` in [`config/settings/system_settings.json`](file:///config/settings/system_settings.json) + `SKILL.md` binding | Bound via skill assignment in App Home or `/grant_skill` | Discovered on server startup (`discover_all_enabled_servers()`); restart server after adding/editing `mcp_servers` | `data/backups/system_settings_backup.json` |
| **Workflows** | [`config/workflows/`](file:///config/workflows) (`<workflow_id>.json`) | **App Home → Workflows Tab** (`workflows_tab.py`) | Hot-reloads active preset on file mtime change; App Home changes persist atomically | `data/backups/workflows/` |
| **RAG Knowledge Base** | [`config/rag_knowledge/`](file:///config/rag_knowledge) (`shared/` and `<agent_id>/`) | `GET /api/rag/status`, `POST /api/rag/reconcile` | Incremental SHA-256 sync on startup **or live** via `POST /api/rag/reconcile` (no restart needed) | Manifest at `data/.rag_cache/file_manifest.json` |
| **Agent Workspace Files** | [`agents_resources/`](file:///agents_resources) | Used via `document_reader` & `document_authoring` skills | Live filesystem access on every tool call | Protected by `workspace_write_blacklist_additional` & HITL |
| **System Settings** | [`config/settings/system_settings.json`](file:///config/settings/system_settings.json) | File-managed only | Hot-reloads on mtime change for dynamic getters; restart recommended for RAG/MCP startup settings. Full key table in [installation.md](installation.md#system-settings-configsettingssystem_settingsjson) | `data/backups/system_settings_backup.json` |

---

## 2. Adding & Modifying Debate Agents

Debate panel personas are configured in [`config/settings/agent_settings.json`](file:///config/settings/agent_settings.json), paired with Markdown persona prompts in [`config/personas/`](file:///config/personas) and avatar icons in [`config/avatars/`](file:///config/avatars).

### 2.1 `config/settings/agent_settings.json` Schema & Field Impact

#### Top-Level Fields

| Field | Type | Required | Impact |
|---|---|---|---|
| `workflow_config` | `string` | Yes | Active workflow preset ID (matching `<id>.json` in `config/workflows/` or a built-in default like `"default_review_flow"`). If missing on disk, `AppIntegrityValidator` falls back to `"default_review_flow"`. |
| `enabled_agents` | `list[string]` | Yes | Canonical IDs (e.g., `["agent-10001", "agent-10002"]`) of agents eligible for panel debates, Sandwich/Scatter-Gather middle pools, and `/open_debate`. Agents omitted here remain registered for explicit `/[Agent]` or `/include` calls unless invalid. If the key is omitted entirely, all valid agents in `agents` default to enabled. |
| `agents` | `object` | Yes | Dictionary mapping canonical agent IDs to their configuration objects. Keys **must** match `^agent-\d+$` (e.g., `agent-10017`) — malformed keys are dropped on load by `AppIntegrityValidator.check_agent_id_format()`. |

#### Per-Agent Fields (`agents["agent-XXXXX"]`)

| Field | Type | Required | Validation & Runtime Impact |
|---|---|---|---|
| `username` | `string` | **Yes** | Short handle without spaces (e.g., `"DataEngineer"`). Used for Slack `/[DataEngineer]` tags, `/include`, `/exclude`, `/first`, peer `nominate_next_speaker` calls, and Slack message author overrides. Duplicate usernames emit a startup warning. |
| `displayname` | `string` | **Yes** | Human-readable display name shown in Slack message headers, App Home, and `/list_skills` (e.g., `"Data Engineer"`). Resolved flexibly alongside `username` and `agent-XXXXX`. |
| `model` | `string` | **Yes** | LiteLLM `provider/model` string (e.g., `"gemini/gemini-3.8-flash"`, `"anthropic/claude-opus-5"`, `"mistral/mistral-large-latest"`, `"openai/gpt-5.6-terra"`). Missing `/` logs a warning. If the provider's API key is missing from `.env`, Izvor automatically falls back to `default_reasoning_model` (if configured) or the first available key in `.env` and appends a fallback note in Slack. |
| `icon_url` | `string` | **Yes** | Relative path served from the `/avatars` static mount (e.g., `"avatars/image_43.png"`). FastAPI searches `config/avatars/` first, then built-in avatars, and Slack fetches `${BASE_AVATAR_URL}${icon_url}`. |
| `is_short_output` | `bool` | No (`false`) | Controls prompt word-limit ceiling from `output_constraints` in [`config/settings/system_settings.json`](file:///config/settings/system_settings.json): `true` applies `short_max_words` (shipped `70`, fallback `100`) and concise formatting instructions; `false` applies `default_max_words` (shipped `150`, fallback `200`). |
| `skills` | `list[string]` | No (`[]`) | Baseline skill names from `config/skills/` (or global skills) permanently authorized for this agent (e.g., `["knowledge_base_rag", "document_reader"]`). Combined at runtime with any thread-level `/grant_skill` grants. |

> [!IMPORTANT]
> **Mandatory Persona Prompt File (`config/personas/<agent_id>.md`)**:
> Every key in `agents` **must** have a matching Markdown file at `config/personas/<agent_id>.md` (for example [`config/personas/agent-10001.md`](file:///config/personas/agent-10001.md)). During validation (`AgentSettingsManager._validate_agent_entry()` and `AppIntegrityValidator.check_missing_prompt_files()`), any agent entry without a matching `.md` file — or missing `model`, `username`, `displayname`, or `icon_url` — is **removed from active registration**.

### 2.2 Example: Adding a New Debate Persona (`agent-10017`)

1. **Create the persona prompt file** at `config/personas/agent-10017.md`:
   ```markdown
   <agent_specification id="agent-10017" role="The Data Engineer" cluster="Cluster 2: Architecture & Data">
     <cognitive_objective>
       Evaluate data pipelines, storage engines, schema evolution, ETL/ELT reliability, and analytics latency tradeoffs.
     </cognitive_objective>

     <evaluation_lens>
       1. Ingestion & Storage Fit: Assess throughput, partitioning, compaction, and consistency guarantees.
       2. Pipeline Operability: Scrutinize schema drift, backfill recovery, lineage, and data-quality SLAs.
     </evaluation_lens>

     <operational_rules>
       1. Zero theatrical preamble or persona self-reference.
       2. Ground claims in concrete throughput, latency, or storage-cost mechanics.
     </operational_rules>

     <output_contract>
       - Core Data Verdict: 1 sentence summarizing data architecture viability.
       - Key Risks & Mitigations: 2–3 concise bullets.
     </output_contract>
   </agent_specification>
   ```
2. **Add an avatar image** (optional, or reuse an existing icon) at `config/avatars/data_engineer.png`.
3. **Register and enable the agent** in [`config/settings/agent_settings.json`](file:///config/settings/agent_settings.json):
   ```json
   {
     "workflow_config": "default_review_flow",
     "enabled_agents": [
       "agent-10001",
       "agent-10017"
     ],
     "agents": {
       "agent-10017": {
         "username": "DataEngineer",
         "model": "gemini/gemini-3.8-flash",
         "displayname": "Data Engineer",
         "icon_url": "avatars/data_engineer.png",
         "is_short_output": false,
         "skills": [
           "knowledge_base_rag",
           "document_reader"
         ]
       }
     }
   }
   ```
4. **Modify existing agents anytime** either by editing [`config/settings/agent_settings.json`](file:///config/settings/agent_settings.json) and `config/personas/<agent_id>.md`, or live in Slack via **Slack App Home → Agents Tab** (enable/disable toggles, model selector, system prompt modal, skills selector, and concise-output toggle).

---

## 3. Adding & Modifying Configurable Skills (`config/skills/`)

Custom agent capabilities live in [`config/skills/`](file:///config/skills), with one subdirectory per skill:

```text
config/skills/
└── <skill_name>/
    ├── SKILL.md     # Required: YAML frontmatter + Markdown guidelines for the agent
    ├── tools.py     # Optional: Custom Python TOOL_SCHEMAS & TOOL_HANDLERS
    └── action.py    # Optional: Orchestrator action handler (`async def run(...)`)
```

> [!WARNING]
> **Built-in Skill Shadowing Protection**:
> Do not reuse the `name` of any built-in skill (such as `control_plane`, `max_words`, `rounds`, `grant_skill`, `stop_discussion`). During discovery, `SkillRegistry` rejects any skill in `config/skills/` that attempts to shadow a built-in skill name and logs a critical `SKILL_SHADOWING_BLOCKED` audit event.

### 3.1 Complete `SKILL.md` YAML Frontmatter Specification

Every `SKILL.md` starts with YAML frontmatter (`--- ... ---`) followed by Markdown `instructions` returned verbatim when an agent invokes `load_skill(skill_name)`.

#### Top-Level Frontmatter Fields

| Field | Type | Default / Req. | Impact on Runtime Behavior |
|---|---|---|---|
| `name` | `string` | Directory name (Req.) | Canonical identifier used in `agent_settings.json` (`skills`), `load_skill(skill_name)`, `/grant_skill`, and `ActionDirective.name`. |
| `display_name` | `string` | Title-cased `name` (Req.) | Human-readable label displayed in `/list_skills` and Slack messages. |
| `description` | `string` | `""` (Req.) | Shown to authorized agents in the `AVAILABLE SKILLS (ON DEMAND VIA load_skill)` system prompt section and to `IntentExtractor` when `auto_detect: true`. Keep it concise and specific so LLMs know when to load the skill. |
| `scope` | `string` | `"agent"` (Req.) | **`"agent"`** *(standard for `config/skills/`)*: Skill is listed in `AVAILABLE SKILLS` and unlocks tools via `load_skill`.<br>**`"orchestrator"`**: Executed as a pipeline/directive action (via `action.py` or pipeline stage); hidden from `AVAILABLE SKILLS` unless `global: true` with tools.<br>**`"prompt_modifier"`**: Writes key-value constraints into `ctx.modifiers` (e.g., word limits) rather than unlocking tools. |
| `requires_admin` | `boolean` | **MANDATORY** | **Fail-Closed SSOT Access Control.** Missing or non-boolean values abort startup with `SkillIntegrityError`.<br>• **Layer 1 (Directives):** If `true`, slash invocations require `SLACK_ADMIN_USER_IDS` membership.<br>• **Layer 2 (Agent Tools):** If `true`, the skill is withheld from non-admin users' turns unless explicitly thread-granted by an admin via `/grant_skill` or `/grant_skill_for_all`. |
| `requires_human_ITL` | `boolean` | `false` (Req.) | **Human-in-the-Loop (HITL) Gate.** If `true`, executing any tool belonging to this skill pauses the turn, saves a `TurnCheckpoint` in SQLite, and posts **Approve / Deny** Block Kit buttons in Slack before running the tool. |
| `global` | `boolean` | `false` | If `true`, automatically available to **all** debate agents in **all** threads without listing it under `skills` in `agent_settings.json` (still gated by `requires_admin` if `true`). |
| `tools` | `list[string]` | `[]` | Tool function names unlocked when `load_skill` runs. Matches names in the skill's `tools.py` (`TOOL_SCHEMAS`/`TOOL_HANDLERS`) or built-in local tools (`web_search`, `fetch_web_page`, `list_documents`, `read_document`, `write_document`, `search_knowledge_rag`). |
| `mcp_server` | `string` | `""` | Server key from `mcp_servers` in `config/settings/system_settings.json`. Links this skill to an external MCP server. |
| `mcp_tools` | `list[string]` | `[]` | Raw MCP tool names from `mcp_server` to expose (e.g., `["get_issue", "search_code"]`), or `["*"]` to expose all tools discovered from that server. Auto-namespaced as `mcp__<server>__<tool>`. |
| `extraction_rule` | `string` | `""` | Natural-language guidance injected into `IntentExtractor`'s `SPECIAL RULES` prompt block when `triggers.auto_detect: true`. Supports `{name}` placeholder substitution. |

#### `triggers` Sub-Object Fields

| Field | Type | Default | Impact on Runtime Behavior |
|---|---|---|---|
| `auto_detect` | `boolean` | `false` | **Layer 1 Natural-Language Inference Toggle.**<br>• `true`: `IntentExtractor` can infer this skill from natural-language Slack prompts (`origin="inferred"`), and `pii_masker` auto-extracts safe keywords into `data/state/presidio_skill_exclusions.json`.<br>• `false` *(fail-closed default)*: Skill can only be triggered explicitly via `slash` aliases or autonomously by an already-authorized agent calling `load_skill` in Layer 2. |
| `slash` | `list[string]` | `[]` | Deterministic slash triggers parsed by `DirectiveParser` (e.g., `["/jira", "/ticket"]`). Leading `/` is normalized automatically; matched case-insensitively with `origin="slash"`. |
| `arg_type` | `string` | `"string"` | Determines how `DirectiveParser` extracts arguments after the slash command:<br>• `string` / `token` / `str`: Single identifier or quoted/unquoted token stored under `param_name` (default `"target"`).<br>• `int` / `integer` / `int_or_off`: Integer (or `"off"` to clear); stored in `modifiers` for `prompt_modifier` scope or `params[param_name]` otherwise.<br>• `file_target` / `file`: Filename or `[file]`/`(file)` target stored under `param_name` (default `"target_file"`).<br>• `agent_token`: Single `[Agent]` or `@Agent` handle stored under `param_name` (default `"first_agent"`).<br>• `agent_list`: Space-separated list of `[Agent1] [Agent2]` or `off` stored under `param_name` (default `"agents"`).<br>• `grant_skill`: `<skill_name> [Agent1] [Agent2]...` syntax.<br>• `list`: Whitespace-separated list of skill/item tokens.<br>• `terminal_flag`: Bare flag that immediately ends execution when paired with `terminal: true`.<br>• `none` / `flag`: Bare slash command with no arguments. |
| `param_name` | `string` | `None` | Key name used inside `ActionDirective.params` when `DirectiveParser` extracts the slash argument (e.g., `query`, `target_file`, `ticket_id`). |
| `keywords` | `list[string]` | `[]` | Explicit trigger terms allowlisted against Presidio NER over-redaction when `auto_detect: true` so domain terms (e.g., `"Jira"`, `"Confluence"`) are not masked as `PERSON`/`ORG`. |
| `handles_files` | `boolean` | `false` | Designates this skill as the handler when a user attaches a file in Slack or references an uploaded filename in their prompt. |
| `terminal` | `boolean` | `false` | If `true`, matching any of its `slash` triggers short-circuits `DirectiveParser` and immediately halts active thread turns. |

#### Markdown Body (`instructions`)

Everything below the closing `---` is stored as `SkillDefinition.instructions`. When an agent calls `load_skill(skill_name)`, Izvor returns `{ "status": "loaded", "unlocked_tools": [...], "operational_guidelines": "<markdown body>" }` and appends the unlocked tool schemas to the active LiteLLM tool loop.

### 3.2 Example A: Custom Python Tool-Backed Skill (`config/skills/jira_lookup/`)

1. **Create `config/skills/jira_lookup/SKILL.md`**:
   ```markdown
   ---
   name: jira_lookup
   display_name: Jira Issue Lookup
   description: Fetch Jira issue status, priority, and acceptance criteria by issue key.
   scope: agent
   triggers:
     auto_detect: true
     slash:
       - /jira
     arg_type: token
     param_name: issue_key
     keywords:
       - Jira
       - ticket
   extraction_rule: "If the user asks to inspect a Jira ticket or issue key (e.g. PROJ-123), include action '{name}' with param 'issue_key'."
   tools:
     - fetch_jira_issue
   requires_human_ITL: false
   requires_admin: false
   ---

   # Jira Issue Lookup Guidelines

   1. Call `fetch_jira_issue(issue_key="<KEY>")` before citing ticket details.
   2. Always cite the issue key and current status in your response.
   ```

2. **Create `config/skills/jira_lookup/tools.py`** exporting `TOOL_SCHEMAS` (list of OpenAI/LiteLLM function schemas) and `TOOL_HANDLERS` (dict mapping tool name → sync or `async` handler taking `(arguments: dict, agent_name: str = "") -> str`):
   ```python
   import json
   import os
   from typing import Any

   import httpx

   TOOL_SCHEMAS = [
       {
           "type": "function",
           "function": {
               "name": "fetch_jira_issue",
               "description": "Retrieve summary, status, and priority for a Jira issue key.",
               "parameters": {
                   "type": "object",
                   "properties": {
                       "issue_key": {
                           "type": "string",
                           "description": "Jira issue key, e.g. ENG-42.",
                       }
                   },
                   "required": ["issue_key"],
               },
           },
       }
   ]


   async def fetch_jira_issue(arguments: dict[str, Any], agent_name: str = "") -> str:
       issue_key = str(arguments.get("issue_key", "")).strip().upper()
       if not issue_key:
           return json.dumps({"error": "issue_key is required"})

       base_url = os.getenv("JIRA_BASE_URL", "").rstrip("/")
       token = os.getenv("JIRA_API_TOKEN", "")
       if not base_url or not token:
           return json.dumps({"error": "JIRA_BASE_URL or JIRA_API_TOKEN is not configured in .env"})

       async with httpx.AsyncClient(timeout=10.0) as client:
           resp = await client.get(
               f"{base_url}/rest/api/3/issue/{issue_key}",
               headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
           )
           resp.raise_for_status()
           data = resp.json()
           fields = data.get("fields", {})
           return json.dumps({
               "key": issue_key,
               "summary": fields.get("summary"),
               "status": (fields.get("status") or {}).get("name"),
               "priority": (fields.get("priority") or {}).get("name"),
           })


   TOOL_HANDLERS = {
       "fetch_jira_issue": fetch_jira_issue,
   }
   ```

3. **Authorize the skill** by adding `"jira_lookup"` to an agent's `"skills"` list in [`config/settings/agent_settings.json`](file:///config/settings/agent_settings.json) (or via **Slack App Home → Agents Tab**, or per-thread via `@Izvor /grant_skill jira_lookup /[Tactician]`).

### 3.3 Example B: Modifying an Existing Skill (`config/skills/`)

To customize any of the 4 shipped skills ([`web_research`](file:///config/skills/web_research/SKILL.md), [`knowledge_base_rag`](file:///config/skills/knowledge_base_rag/SKILL.md), [`document_reader`](file:///config/skills/document_reader/SKILL.md), [`document_authoring`](file:///config/skills/document_authoring/SKILL.md)):
- Edit its `SKILL.md` frontmatter (for example, set `requires_human_ITL: true` to require Slack Approve/Deny confirmation, set `requires_admin: true` to restrict it to Slack admins, or add custom slash aliases under `triggers.slash`).
- Edit its Markdown body to tune how agents formulate queries, format citations, or follow domain rules.
- Restart the server (or wait for the periodic skill sync) so `SkillRegistry` and Presidio exclusions pick up the updated frontmatter hash.

---

## 4. Connecting External MCP Servers

Izvor includes a built-in **Model Context Protocol (MCP) Client** (`McpClientManager`) that connects to external MCP servers, discovers their tools on startup, translates them into LiteLLM function schemas under the namespace `mcp__<server_name>__<raw_tool_name>`, and protects every call with `ToolPrincipal` ACLs, outbound PII masking, inbound secret scanning, and HITL gates.

### 4.1 `mcp_servers` Configuration Reference (`config/settings/system_settings.json`)

Add server entries under `"mcp_servers"` in [`config/settings/system_settings.json`](file:///config/settings/system_settings.json). String fields (`command`, `args`, `env`, `cwd`, `url`, `headers`) automatically expand `${VAR}` and `$VAR` placeholders from `.env` / OS environment variables.

| Field | Type | Default | Applies To | Description & Impact |
|---|---|---|---|---|
| `enabled` | `boolean` | `true` | All transports | Enables or disables discovery and tool calls for this server without removing its config. |
| `transport` | `string` | `"stdio"` | All | `"stdio"` (spawns local subprocess), `"sse"`, or `"streamable_http"` (connects over HTTP/SSE to `url`). |
| `command` | `string` | `""` | `stdio` | Executable to launch (e.g., `"npx"`, `"uvx"`, `"python"`). |
| `args` | `list[string]` | `[]` | `stdio` | Command-line arguments passed to `command` (supports `${VAR}` expansion). |
| `env` | `dict[str, str]` | `{}` | `stdio` | Extra environment variables injected into the child process (e.g., `{"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"}`). **Security:** `build_safe_stdio_env()` inherits only safe OS vars and unconditionally strips Izvor secrets (`SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `SLACK_APP_TOKEN`, `AUDIT_HMAC_SECRET`, `ADMIN_PASSWORD`, `INTERNAL_API_KEY`). On Windows, child processes are bound to a Win32 Job Object (`KILL_ON_JOB_CLOSE`) so they terminate cleanly when Izvor stops. |
| `cwd` | `string \| null` | `null` | `stdio` | Working directory for the spawned MCP server subprocess. |
| `url` | `string` | `""` | `sse` / `streamable_http` | Remote MCP endpoint URL (required when `transport` is `"sse"` or `"streamable_http"`). |
| `headers` | `dict[str, str]` | `{}` | `sse` / `streamable_http` | HTTP headers sent to the remote MCP endpoint (supports `${VAR}` expansion, e.g., `{"Authorization": "Bearer ${MCP_API_TOKEN}"}`). |
| `timeout_seconds` | `float` | `30.0` | All | Timeout in seconds applied to tool discovery and individual MCP tool invocations. |
| `mutating_tools` | `list[string]` | `[]` | All | Raw tool names (or `["*"]`) that modify external state. Any listed tool — or any MCP tool advertising `annotations.destructiveHint: true` — automatically triggers the **Slack HITL Approve / Deny** gate before execution. |
| `outbound_pii_masking` | `boolean` | `true` | All | When `true`, `sanitize_outbound_mcp_arguments()` masks PII/sensitive entities via `PIIMasker` before arguments leave Izvor. All inbound MCP responses are also scanned for leaked secrets (`scan_output_for_secrets()`) and PII-masked. |

### 4.2 End-to-End Walkthrough: Connecting a Local (`stdio`) & Remote (`sse`) MCP Server

**Step 1 — Add credentials to `.env`:**
```ini
GITHUB_TOKEN=ghp_your_personal_access_token
LINEAR_MCP_TOKEN=lin_api_your_token
```

**Step 2 — Register the servers in [`config/settings/system_settings.json`](file:///config/settings/system_settings.json):**
```json
{
  "mcp_servers": {
    "github": {
      "enabled": true,
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"
      },
      "mutating_tools": ["create_issue", "create_pull_request", "push_files"],
      "timeout_seconds": 30.0,
      "outbound_pii_masking": true
    },
    "linear": {
      "enabled": true,
      "transport": "sse",
      "url": "https://mcp.linear.app/sse",
      "headers": {
        "Authorization": "Bearer ${LINEAR_MCP_TOKEN}"
      },
      "mutating_tools": ["create_issue", "update_issue"],
      "timeout_seconds": 30.0,
      "outbound_pii_masking": true
    }
  }
}
```

**Step 3 — Bind each MCP server to a Skill in `config/skills/`:**

Create `config/skills/github_ops/SKILL.md`:
```markdown
---
name: github_ops
display_name: GitHub MCP Integration
description: Search repositories, inspect issues/PRs, and create GitHub issues via external MCP.
scope: agent
mcp_server: github
mcp_tools:
  - search_repositories
  - get_issue
  - create_issue
triggers:
  auto_detect: true
  slash:
    - /github
  arg_type: string
  param_name: query
  keywords:
    - GitHub
    - repository
    - pull request
extraction_rule: "If the user asks to inspect or update GitHub repositories or issues, include action '{name}' with param 'query'."
requires_human_ITL: false
requires_admin: false
---

# GitHub MCP Guidelines

1. Load `github_ops` before calling `mcp__github__search_repositories`, `mcp__github__get_issue`, or `mcp__github__create_issue`.
2. Mutating actions (`create_issue`) pause for human approval in Slack before execution.
```
*(Tip: Use `mcp_tools: ["*"]` if you want the skill to expose every tool advertised by the MCP server, and set `requires_admin: true` if only Slack admins should be able to trigger it without an explicit `/grant_skill`.)*

**Step 4 — Assign & Restart:**
1. Add `"github_ops"` to an agent's `"skills"` array in [`config/settings/agent_settings.json`](file:///config/settings/agent_settings.json) (or assign it in **Slack App Home → Agents Tab**).
2. Restart Izvor (`uvicorn main:app --host 0.0.0.0 --port 8000 --reload`) so startup runs `mcp_client_manager.discover_all_enabled_servers()`.
3. Verify discovered tools via `GET /api/skills` or `@Izvor /list_skills` in Slack.

---

## 5. Adding & Modifying Workflows (`config/workflows/`)

Custom deliberation workflows live as JSON files in [`config/workflows/`](file:///config/workflows) (see [`idea_architecture_feasibility_review.json`](file:///config/workflows/idea_architecture_feasibility_review.json) for an existing example).

> [!NOTE]
> **Protected Default Workflow IDs**:
> The three built-in default workflow IDs (`default_review_flow`, `default_sandwich`, and `parallel_dialectic_flow`) are reserved system baselines. Custom presets in `config/workflows/` cannot overwrite or shadow those IDs — choose a unique `id` / filename for custom presets.

### 5.1 `WorkflowPreset` JSON Schema & Field Impact

| Field | Type | Required | Impact |
|---|---|---|---|
| `id` | `string` | Yes | Unique slug identifier (also used as `<id>.json` filename and in `agent_settings.json` `"workflow_config"`). Lowercase letters, digits, and underscores. |
| `name` | `string` | Yes | Human-readable preset name shown in **Slack App Home → Workflows Tab**. |
| `mode` | `string` | Yes | Orchestration topology:<br>• `"fixed"`: Executes `fixed_config.chain` strictly in sequence.<br>• `"sandwich"`: Runs `sandwich_config.start_chain` → open **Middle Pool** (all other enabled agents via peer nominations and Challenger/Arbitrator convergence checks) → `sandwich_config.end_chain`.<br>• `"scatter_gather"`: Runs Turn 1 from `sandwich_config.start_chain`, fans out remaining enabled agents concurrently in isolated branches via `asyncio.TaskGroup`, then gathers all outputs into `sandwich_config.end_chain`. |
| `description` | `string` | No (`""`) | Short summary of when to use the preset. |
| `fixed_config` | `object` | When `mode == "fixed"` | `{"chain": ["agent-10001", "agent-10008", ...]}`. Agents are executed in exact array order (an agent ID may appear more than once if needed). |
| `sandwich_config` | `object` | When `mode != "fixed"` | `{"start_chain": ["agent-10001"], "end_chain": ["agent-10012"]}`. Any enabled agent not in `start_chain` or `end_chain` becomes eligible for the Middle Pool (in `sandwich`) or parallel scatter branches (in `scatter_gather`). |

### 5.2 Workflow Examples (All 3 Modes)

**1. Fixed Sequential Chain (`config/workflows/security_release_gate.json`):**
```json
{
  "id": "security_release_gate",
  "name": "Security Release Gate",
  "mode": "fixed",
  "description": "Deterministic review by Security Architect, Risk Auditor, Compliance Officer, and Tactician.",
  "fixed_config": {
    "chain": [
      "agent-10008",
      "agent-10013",
      "agent-10004",
      "agent-10014"
    ]
  }
}
```

**2. Sandwich Mode (`config/workflows/executive_sandwich.json`):**
```json
{
  "id": "executive_sandwich",
  "name": "Executive Sandwich Debate",
  "mode": "sandwich",
  "description": "Visionary & Product Strategist frame the problem; enabled specialists debate; ROI Strategist synthesizes.",
  "sandwich_config": {
    "start_chain": ["agent-10001", "agent-10012"],
    "end_chain": ["agent-10006"]
  }
}
```

**3. Parallel Dialectic / Scatter-Gather (`config/workflows/parallel_architecture_audit.json`):**
```json
{
  "id": "parallel_architecture_audit",
  "name": "Parallel Architecture Audit",
  "mode": "scatter_gather",
  "description": "Visionary frames Turn 1; enabled specialists investigate concurrently without anchoring bias; Product Strategist synthesizes.",
  "sandwich_config": {
    "start_chain": ["agent-10001"],
    "end_chain": ["agent-10012"]
  }
}
```

### 5.3 Activating & Editing Workflows

- **Via Config File:** Set `"workflow_config": "security_release_gate"` in [`config/settings/agent_settings.json`](file:///config/settings/agent_settings.json). Changes to the active workflow JSON file are detected automatically by `sync_settings_if_modified()`.
- **Via Slack App Home:** Open **App Home → Workflows Tab** to select/load presets, edit chains, and save/delete custom presets (**Slack Admin only** — `select_workflow_preset` and `load_workflow_preset_button` enforce `admin_required=True`). Note that each Slack thread freezes a workflow snapshot at Turn 1 so switching presets mid-flight never disrupts running threads.

---

## 6. Adding & Modifying RAG Knowledge Documents (`config/rag_knowledge/`)

Izvor's hybrid RAG engine indexes documents from [`config/rag_knowledge/`](file:///config/rag_knowledge) into **LanceDB** (dense vector embeddings) and **Tantivy** (BM25 full-text search) merged with Reciprocal Rank Fusion.

### 6.1 Directory Layout & Retrieval Scoping

| Folder Path | `agent_id` Tag | Who Can Retrieve It (`knowledge_base_rag` skill) |
|---|---|---|
| `config/rag_knowledge/shared/...` *(or files directly in `config/rag_knowledge/`)* | `"shared"` | Every agent authorized for `knowledge_base_rag` when calling `search_knowledge_rag(query, scope="all")` *(default)*. |
| `config/rag_knowledge/<agent_id>/...` *(e.g. [`config/rag_knowledge/agent-10008/`](file:///config/rag_knowledge/agent-10008))* | `"<agent_id>"` | Only that specific persona (`agent-10008`) in both `scope="self"` and `scope="all"`. Ideal for role-specific playbooks, compliance checklists, or security runbooks. |

- **Supported extensions:** `.md`, `.markdown`, `.txt`, and `.pdf` (parsed per page with `pypdf`; code-fence-aware Markdown parser tracks ` ``` ` / `~~~` blocks so `#` comments inside fenced code never create fake section splits, while preserving heading hierarchy as `section_header` citations).
- **Collision-free IDs & consistent ingestion PII masking:** Chunk IDs use SHA-256 `sanitize_id()`, and when `pii_masking: "AT_INGESTION"`, a single document-level `token_mapping` is shared across all chunks of a document so masked aliases stay consistent.
- **Incremental reconciliation & config-drift re-indexing:** `SyncReconciliationEngine` tracks `SHA-256` hashes, `mtime`, chunk IDs, and `__meta__.config_hash` (computed over `embedding_model`, `pii_masking`, and chunk bounds) in `data/.rag_cache/file_manifest.json`, automatically re-indexing documents if chunk/PII/embedding configuration drifts.

### 6.2 Triggering Indexing (Live or on Startup)

1. Drop, edit, or delete `.md`, `.txt`, or `.pdf` files under `config/rag_knowledge/shared/` or `config/rag_knowledge/<agent_id>/`.
2. Either **restart the server** (startup runs `rag_sync_engine.reconcile()`) **or trigger a live sync with zero downtime**:
   ```bash
   # Trigger incremental re-indexing immediately
   curl -X POST -u admin:YourAdminPassword http://localhost:8000/api/rag/reconcile

   # Inspect indexed documents and SHA-256 manifest records
   curl -u admin:YourAdminPassword http://localhost:8000/api/rag/status
   ```
   *(Make sure the embedding provider key matching `rag.embedding_model` — `GEMINI_API_KEY` by default — is set in `.env`.)*

### 6.3 Tuning RAG & Security Parameters (`config/settings/system_settings.json`)

Under `"rag"` in [`config/settings/system_settings.json`](file:///config/settings/system_settings.json):

| Key | Shipped Value | Impact |
|---|---|---|
| `max_chunk_chars` | `1500` | Maximum characters per indexed chunk (schema fallback `3500`). |
| `chunk_overlap_chars` | `300` | Sliding character overlap between adjacent chunks to preserve cross-boundary context. |
| `min_chunk_chars` | `80` | Minimum chunk size threshold during recursive splitting. |
| `default_top_k` | `3` | Default number of fused results returned per search (schema fallback `6`). |
| `embedding_model` | `"gemini/gemini-embedding-001"` | LiteLLM embedding model identifier. If changed (or vector dimension changes), `__meta__.config_hash` triggers automatic re-indexing. |
| `embedding_dim` | `3072` | Vector dimensionality stored in LanceDB. |
| `embedding_batch_size` | `32` | Number of chunks embedded per API batch call. |
| `pii_masking` | `"AT_RETRIEVAL"` | `"AT_RETRIEVAL"` (masks PII when excerpts enter agent context), `"AT_INGESTION"` (masks before embedding with shared per-doc `token_mapping`), or `"NONE"`. |
| `guardrail_threshold` | `0.85` | Cosine similarity threshold for prompt-injection guardrail checks. |

Top-level P2/P3 guardrail, PII NER, audit keyring, and retention keys configurable in `system_settings.json` (with `SystemSettingsModel` defaults):
- `guardrail_chunk_chars` (`800`), `guardrail_chunk_overlap_chars` (`200`), `guardrail_max_chunks` (`8`), `guardrail_indirect_embedding_enabled` (`false`), `pii_ner_models` (`["en_core_web_md"]` — if changed, also update the spaCy model wheel in [`pyproject.toml`](file:///pyproject.toml) / [`requirements.txt`](file:///requirements.txt) and install the model), `audit_hmac_key_id` (`"default"`), `skill_exclusions_sync_interval_seconds` (`86400.0`).
- **Scheduled Retention (`retention_*`):** `retention_checkpoints_days` (`7`), `retention_traces_days` (`30`), `retention_event_dedup_days` (`1`), `retention_expired_grace_days` (`7`), `retention_thread_sessions_days` (`90`), `retention_downloads_hours` (`24`), `retention_audit_anchors_compact_enabled` (`true`), `retention_prune_interval_seconds` (`86400.0`), plus opt-in archive windows defaulting to `0` (disabled): `retention_spend_ledger_days` (`0`) and `retention_flat_histories_days` (`0`).
- **Full `system_settings.json` Reference:** See the complete top-level and nested (`web_security`, `security_burst_limits`, `dialectical_stress_test`) parameter tables in [installation.md](installation.md#system-settings-configsettingssystem_settingsjson).

---

## 7. Managing Agent Workspace Resources (`agents_resources/`)

[`agents_resources/`](file:///agents_resources) is the sandboxed working directory for direct file inspection and document authoring via the [`document_reader`](file:///config/skills/document_reader/SKILL.md) (`list_documents`, `read_document`) and [`document_authoring`](file:///config/skills/document_authoring/SKILL.md) (`write_document`) skills.

### 7.1 `agents_resources/` vs. `config/rag_knowledge/`

| Dimension | `agents_resources/` | `config/rag_knowledge/` |
|---|---|---|
| **Primary Purpose** | Direct full-file reading, listing, and agent-authored deliverables (reports, summaries, specs). | Semantic + BM25 search across large knowledge bases and role runbooks. |
| **Skill & Tools** | `document_reader` (`list_documents`, `read_document`) & `document_authoring` (`write_document`). | `knowledge_base_rag` (`search_knowledge_rag`). |
| **How Content Is Loaded** | Reads full text or page-by-page PDF content directly on demand; also accessible by filename. | Split into chunks, embedded in LanceDB + Tantivy, and retrieved as top-K cited excerpts. |
| **Can Agents Write Here?** | **Yes** (with `document_authoring` + Slack **HITL Approve/Deny**). | **No** (read-only knowledge base managed by operators). |
| **Indexing Step Needed?** | **No** — files added here are immediately visible to `list_documents` and `read_document`. | **Yes** — reconciled on startup or via `POST /api/rag/reconcile`. |

### 7.2 Adding Files & Security Constraints

- **Adding reference files:** Place `.txt`, `.md`, `.csv`, `.json`, `.yaml`, `.pdf`, or other UTF-8 files directly inside `agents_resources/` (or subfolders like `agents_resources/reports/`). Agents pass relative paths such as `"sys_spec.txt"` or `"reports/q4.pdf"`.
- **Overwrite vs. Append (`write_document`):** `write_document(rel_path, content, mode="overwrite")` supports both `mode="overwrite"` (default, atomic full-file write) and `mode="append"` (preserves existing content and appends cleanly on a newline boundary, creating the file if missing, while still enforcing `max_write_bytes` on the combined output).
- **Size & path limits** (configured in [`config/settings/system_settings.json`](file:///config/settings/system_settings.json)):
  - `max_file_size_mb`: `50.0` (files larger than 50 MB are rejected by `read_document`).
  - `max_write_bytes`: `1048576` (1 MB cap on `write_document`).
  - **Filename & write confinement:** `validate_resource_filename()` blocks `..` traversal, absolute paths, `:`, NUL bytes, and Windows reserved device names (`CON`, `PRN`, `AUX`, `NUL`, `COM1–9`, `LPT1–9`). `write_document` also unconditionally blocks `.py` files, `.env*`, `.git/`, and settings files, and respects any extra glob/path patterns added to `workspace_write_blacklist_additional` in `system_settings.json`.
