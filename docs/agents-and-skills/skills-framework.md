# Skills Framework & Dynamic Registration

This document details the architecture of **Izvor**'s dynamic skills system, explaining how capabilities are discovered, registered, granted, revoked, persisted in SQLite, and safely executed by personas.

---

## 🧩 Architectural Overview

In Izvor, a **Skill** is a modular capability that can be dynamically attached to or detached from personas, either statically via configuration or dynamically during conversation turns via user directives and slash commands.

Skills fall into two distinct categories:
1. **Tool-Backed Skills**: Provide executable tools (such as web search, file reading, or RAG queries) that the LLM invokes via function calling.
2. **Behavioral Constraint Skills**: Inject deterministic prompt guidelines or mechanical limits (such as minimum/maximum word counts or round caps).

```mermaid
graph TD
    Directory["skills/<skill_name>/"] --> Loader["skills/loader.py"]
    Loader --> Registry["SkillRegistry (core/registry.py)"]
    Registry --> SchemaGen["Tool Schema Generator (JSON Schema for LiteLLM)"]
    
    subgraph Execution & Persistence
        UserDirective["User Slash Command: /grant_skill or /grant_skill_for_all"] --> OriginGuard{"origin == 'slash'?"}
        OriginGuard -->|No (inferred)| BlockPriv["Block Privilege Escalation"]
        OriginGuard -->|Yes| Manager["ThreadSkillsManager"]
        Manager --> SQLiteTable[("SQLite: thread_skills")]
        Manager --> AuditLog["Log THREAD_SKILL_GRANTED"]
        SQLiteTable --> ContextPrep["Stage 3: ContextPrepStage"]
        ContextPrep --> LLM["LiteLLM Inference with Registered Tool Schemas"]
    end
```

---

## 🧠 The Two Inference Layers in Izvor

A critical architectural distinction in Izvor is that **"inference" occurs at two completely separate layers**:

```mermaid
graph TD
    UserMsg["Slack User Message"] --> L1["Layer 1: Orchestrator Pre-Inference (IntentExtractor)"]
    
    subgraph Layer 1: Orchestrator Ingress
        L1 --> L1Check{"triggers.auto_detect == True?"}
        L1Check -->|Yes| InferredAct["ActionDirective (origin='inferred')"]
        L1Check -->|No| DropInference["Ignore Conversational Chatter (Slash Only)"]
        SlashCmd["User Slash Command: /web_research"] --> SlashAct["ActionDirective (origin='slash')"]
    end

    InferredAct --> ContextPrep["Stage 3: ContextPrepStage"]
    SlashAct --> ContextPrep
    DropInference --> ContextPrep

    subgraph Layer 2: Agent Execution
        ContextPrep --> AuthCheck{"Is Skill in Agent's effective_skills?"}
        AuthCheck -->|Yes| Catalog["Inject into Agent's Skills Catalog (load_skill)"]
        AuthCheck -->|No| NoAccess["Omit / Mark Permission Restricted"]
        Catalog --> AgentTurn["Agent LLM Execution Turn"]
        AgentTurn --> ToolCall["Agent calls load_skill / custom tool autonomously"]
    end
```

### The Separation of Concerns:
1. **`triggers.auto_detect` ONLY governs Layer 1 (Orchestrator Pre-Inference):**
   - It controls whether free-form user chatter in Slack can automatically pre-arm or infer an `ActionDirective`.
   - **Default is `false` (fail-closed / secure-by-default).**
   - It **does NOT** stop an agent from using an authorized tool during Layer 2.
2. **Layer 2 (Agent Function Calling) is governed by permissions:**
   - As long as a skill is authorized for an agent (via `agent_settings.json` or `/grant_skill`), the agent can autonomously invoke `load_skill` and execute its tools, even if `auto_detect: false`.

---

## 📜 Complete `SKILL.md` Manifest Specification

Every skill is defined by a `SKILL.md` file located in `skills/<skill_name>/SKILL.md`. The file consists of a YAML frontmatter header followed by Markdown operational instructions/guidelines.

### 1. Top-Level Frontmatter Properties

| Property | Type | Default | What It Does | Where It Applies | Where It Does NOT Apply |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `name` | `string` | Directory name | Unique technical identifier for the skill (e.g., `web_research`). | Everywhere (Registry, Directives, Tool calls). | N/A |
| `display_name` | `string` | Capitalized name | User-facing display name in Slack messages and UI directory. | Slack formatted messages and directory listings. | Internal code routing. |
| `description` | `string` | `""` | Description of what the skill does. | Injected into agent Skills Catalogs and `IntentExtractor` prompt. | Tool argument validation. |
| `scope` | `string` | `"agent"` | Architectural domain of the skill (`agent`, `orchestrator`, or `prompt_modifier`). | Determines pipeline execution path. See Scope Matrix below. | Does not restrict tool parameter types. |
| `tools` | `list[str]` | `[]` | List of tool names exposed by this skill (implemented in `tools.py`). | Agent function-calling loop and sandbox authorization. | Orchestrator actions (`action.py`). |
| `requires_human_ITL` | `boolean` | `false` | Human-in-the-Loop gate. If `true`, tool execution pauses and sends a confirmation button to Slack. | Layer 2 tool execution in `execution.py`. | Orchestrator pipeline actions or Layer 1 parsing. |
| `global` (or `all_agents`) | `boolean` | `false` | If `true`, automatically available to all agents across all threads without needing `/grant_skill`. | Agent authorization checks in `loader.py`. | Does not bypass `requires_human_ITL`. |
| `handles_files` | `boolean` | `false` | Marks this skill as the designated file content processor for attached Slack files. | File attachment routing in `DirectiveParser` & `ContextPrepStage`. | Pure text prompts without attachments. |
| `extraction_rule` | `string` | `""` | Natural language rule instructing the micro-LLM how to recognize and extract this skill. | Micro-LLM system prompt in `IntentExtractor`. | Deterministic slash command parsing. |

---

### 2. `triggers` Object Properties

The `triggers:` dictionary configures how the skill is detected from user input in Slack.

| Property | Type | Default | What It Does | Where It Applies | Where It Does NOT Apply |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `auto_detect` | `boolean` | **`false`** | **Orchestrator Pre-Inference Toggle.** When `true`, allows the micro-LLM `IntentExtractor` to infer this skill from natural user conversation (`origin="inferred"`). | **Layer 1 only:** `IntentExtractor` prompt & `ValidationStage` privilege checks. | **Layer 2:** Does NOT restrict agents from calling authorized tools via `load_skill`. |
| `slash` | `list[str]` | `[]` | List of slash command aliases (e.g. `["/web_research", "/webresearch"]`). | Deterministic slash parser in `DirectiveParser` (`origin="slash"`). | Natural language prompts without slash prefixes. |
| `arg_type` | `string` | `"string"` | Parser strategy for extracting arguments following the slash command. Supported: `terminal_flag`, `int`, `int_or_off`, `agent_list`, `token`, `agent_token`, `grant_skill`, `list`, `file_target`, `none`. | `DirectiveParser` parameter extraction. | Agent LLM internal reasoning. |
| `param_name` | `string` | `None` | Parameter key under which parsed argument is stored in `ActionDirective.params`. | Parameter dictionary construction in `DirectiveParser`. | Unparameterized flag skills. |
| `terminal` | `boolean` | `false` | If `true`, halts debate immediately upon detection (e.g. `/stop`). | Short-circuit stop check in `DirectiveParser` & Slack event adapter. | Non-terminal conversational skills. |

---

### 3. Scope Matrix (`scope`)

| Scope | Executed By | Handler File | Storage in TurnContext | Agent Catalog Injection |
| :--- | :--- | :--- | :--- | :--- |
| **`agent`** | Debate Agent LLM | `tools.py` | `ctx.actions` (`ActionDirective`) | **Yes** (Listed under `AVAILABLE SKILLS` via `load_skill`) |
| **`orchestrator`** | Turn Pipeline Stage | `action.py` | `ctx.actions` (`ActionDirective`) | **No** (Hidden from agents; run by pipeline) |
| **`prompt_modifier`** | System Prompt Builder | Markdown body | `ctx.modifiers` (Key-value dict) | **No** (Directly alters agent prompt constraints) |

---

## 🔍 Discovery & Registration Lifecycle

### 1. Skill Discovery ([`skills/loader.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/skills/loader.py))
During application boot, the loader scans the [`skills/`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/skills) root directory. Every subdirectory containing a valid `SKILL.md` file is evaluated.

### 2. Manifest & Schema Parsing
The loader extracts all top-level and trigger metadata, validates required fields, registers custom tools from `tools.py`, and checks for action handlers in `action.py`.

### 3. Registry Storage ([`core/registry.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/core/registry.py))
Valid skills are registered into the global `SkillRegistry`. They can be queried via the administrative REST endpoint (`GET /api/skills`) or listed inside Slack via `/list_skills` or `[list_skills]`.

---

## 🔄 Dynamic Grant & Revoke Mechanics

Izvor provides fine-grained control over skill assignment through [`ThreadSkillsManager`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/orchestration/thread_skills_manager.py):

### 1. Static Configuration (Baseline)
Personas can be assigned permanent baseline skills in [`data/settings/agent_settings.json`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/data/settings/agent_settings.json) (e.g., `SecurityArchitect` defaults with `knowledge_base_rag` and `document_reader`).

### 2. Runtime Dynamic Granting
Users can grant skills on the fly within an active thread using slash syntax or bracketed directives:
- `/grant_skill web_research to Visionary` (or `[grant: web_research to Visionary]`)
- `/grant_skill_for_all web_research` (or `[grant_all: web_research]`)

### 3. Runtime Revocation
- `/revoke_skill_for_all web_research` (or `[revoke_all: web_research]`) — Revokes the tool thread-wide from all participating personas.
- `[revoke_constraint: max_words]` — Clears active word limit constraints.

### 4. Privilege Boundary Enforcement
- **Origin Boundary**: Directives inferred heuristically from free-form user chatter (`origin: "inferred"`) are strictly prohibited from granting or revoking skills (`grant_skill_for_all`, `revoke_skill_for_all`).
- Privileged operations require explicit user invocation via slash command or deliberate bracketed syntax (`origin: "slash"`).
- Any attempt to trigger privileged skills via inferred directives fails closed with an audit warning.

### 5. SQLite Persistence & Audit Logging
All dynamic grants are persisted into the SQLite database (`data/izvor.db`) in table `thread_skills`:
```sql
CREATE TABLE IF NOT EXISTS thread_skills (
    channel_id TEXT NOT NULL,
    thread_ts TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    skill_name TEXT NOT NULL,
    granted_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    PRIMARY KEY (channel_id, thread_ts, agent_id, skill_name)
);
```
Every grant and revocation emits a non-repudiable audit event to `audit/`:
- `THREAD_SKILL_GRANTED`
- `THREAD_SKILL_REVOKED`

---

## 🛡️ Tool Sandboxing & Confinement

Tool execution occurs inside a protected sandbox:
- File paths are validated against `agents_resources/` and prevented from writing to `DEFAULT_PROTECTED_WRITE_PATHS` or `system_resources/`.
- Network requests from web research tools enforce timeouts and domain filtering.
- Non-blocking asynchronous handlers prevent worker pool starvation.
