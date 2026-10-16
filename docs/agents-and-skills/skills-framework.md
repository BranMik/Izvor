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

## 🔍 Discovery & Registration Lifecycle

### 1. Skill Discovery ([`skills/loader.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/skills/loader.py))
During application boot, the loader scans the [`skills/`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/skills) root directory. Every subdirectory containing a valid `SKILL.md` file is evaluated.

### 2. Manifest & Schema Parsing
The loader extracts:
- `name`: Unique skill identifier (e.g., `web_research`).
- `display_name`: Human-readable label for Slack UI.
- `description`: Explanatory text injected into LLM system prompts or tool definitions.
- `parameters`: JSON schema specifying expected input arguments.

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
- `/revoke_skill web_research from Visionary` (or `[revoke: web_research from Visionary]`)
- `/revoke_skill_for_all web_research` (or `[revoke_all: web_research]`)
- `[revoke_constraint: max_words]` -> Clears active word limit constraints.

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
