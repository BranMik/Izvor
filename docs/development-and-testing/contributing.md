# Contributing & Engineering Guidelines

This document outlines the engineering principles, architectural constraints, and contribution policies for developers working on **Izvor**.

---

## 📜 Core Engineering Policies

When contributing to Izvor, you must strictly follow these established core policies:

### 1. Test Refactoring & Verification Rule
When removing, renaming, or modifying any codebase feature, **always check whether it is used or referenced in the test suite**. If it is, refactor or rewrite the tests accordingly. Code changes must never leave failing or obsolete tests in `tests/`.

### 2. Implementation Planning Rule
Always create an implementation plan first before writing code or modifying existing systems, unless the change is strictly trivial (e.g. fixing a typo or comment). If a change affects multiple files, modifies APIs/interfaces, adds new features, or refactors architecture, a documented plan is mandatory.

### 3. Production vs. Local Code Refactoring Rule
- **Local Code**: If a refactoring task targets local development code, do not preserve unnecessary backward compatibility shims or obsolete code paths. Refactor all references cleanly, and rewrite tests directly.
- **Production Code**: If refactoring touches production systems or public API surfaces, backward compatibility must be evaluated and detailed explicitly in the implementation plan.

### 4. Architectural Antipattern & Security Guard
If a requested solution represents poor engineering practice, an architectural antipattern, a security risk (e.g. path traversal, unverified webhooks), or a runaway token cost risk, **do not implement it blindly**. Warn about the downsides, explain why it is inadvisable, and propose 1–2 safer alternatives.

### 5. Strict Layer Boundaries & Async LLM Enforcement
- Lower architectural layers (`core`, `nlp`, `pipeline`, `services`) must never import from `orchestration` or `adapters` (enforced by [`tests/unit/test_architecture_layers.py`](file:///tests/unit/test_architecture_layers.py)).
- All runtime LLM and embedding invocations must use async `litellm.acompletion` and `litellm.aembedding` (enforced by [`tests/unit/test_async_llm_enforcement.py`](file:///tests/unit/test_async_llm_enforcement.py)).

---

### 6. 3-Tier Separation of Concerns (`config/`, `engine/`, `data/`)
- **Implementer Configuration (`config/`)**: Debate personas (`config/personas/`), debate panel settings (`config/settings/agent_settings.json`), system tuning (`config/settings/system_settings.json`), custom workflows (`config/workflows/`), agent tool skills (`config/skills/`), avatars (`config/avatars/`), and RAG documents (`config/rag_knowledge/`) belong in `config/`.
- **Immutable Engine (`engine/`)**: Core Python packages (`engine/core`, `engine/orchestration`, `engine/pipeline`, `engine/nlp`, `engine/adapters`, `engine/services`, `engine/prompts`, `engine/skills`), system agents (`engine/settings/system_agent_settings.json`), built-in modifier/control skills (`engine/skills/builtin/`), and seed workflows (`engine/workflows/`) belong in `engine/`.
- **Runtime & UI State (`data/`)**: Dynamic Slack UI settings (`data/state/app_settings.json`), SQLite databases (`data/izvor.db`), audit logs (`data/audit/`), backups (`data/backups/`), and exported transcripts (`data/flat-histories/`) belong in `data/`.

---

## ➕ Adding or Customizing a Persona

### A. Debate Panel Persona (`config/`)
To introduce or customize a debate panel persona:
1. **System Prompt**: Add a dedicated markdown prompt file in [`config/personas/`](file:///config/personas) (e.g., `config/personas/agent-10018.md`) defining the persona's core perspective, biases, and tone.
2. **Avatar Image**: Place the avatar PNG in [`config/avatars/`](file:///config/avatars).
3. **Roster Registration**: Register the agent entry in [`config/settings/agent_settings.json`](file:///config/settings/agent_settings.json) under `"agents"` (and optionally add its ID to `"enabled_agents"`):
   - `username`: Canonical identifier (e.g. `DataScientist`).
   - `displayname`: Formatted title (e.g. `Data Scientist`).
   - `model`: Default LLM router string.
   - `instruction`: Relative prompt path (e.g. `config/personas/agent-10018.md`).
   - `avatar`: Avatar filename (e.g. `image_18.png`).
   - `skills`: Initial tool permissions (e.g. `["knowledge_base_rag", "web_research"]`).
   - `special_role`: Dialectical role (`"NONE"`, `"CHALLENGER"`, or `"ARBITRATOR"`).
4. **Entity Resolution**: Add aliases or nickname mappings to [`engine/pipeline/entity_resolver`](file:///engine/pipeline/entity_resolver).
5. **Tests**: Add unit test verification to [`tests/unit/test_agent_settings_ssot.py`](file:///tests/unit/test_agent_settings_ssot.py).

### B. Engine System Agent (`engine/`)
Internal support agents (`Orchestrator`, `Challenger`, `Arbitrator`, `Discussion_Scribe`, `Intent_Extractor`, `Language_Detector`, `Pricing_Agent`, `Thread_Notes_Agent`, `Usage_Agent`, `InternalRevisor`, `List_Skills`) are part of the core engine:
- Their configuration resides in [`engine/settings/system_agent_settings.json`](file:///engine/settings/system_agent_settings.json).
- Their prompt templates reside in [`engine/prompts/system_agents/`](file:///engine/prompts/system_agents).

---

## 🛠️ Adding a New Dynamic Skill or Directive

 [`SkillRegistry`](file:///engine/skills/loader.py) discovers skills from two directories:
- **Implementer Tool Skills (`config/skills/<skill_name>/`)**: Tool-backed capabilities (`scope: agent`) that debate personas invoke during deliberations (imported as `config.skills.<skill_name>.tools`).
- **Engine Built-in Directives & Modifiers (`engine/skills/builtin/<skill_name>/`)**: Core orchestration actions (`scope: orchestrator`), prompt modifiers (`scope: prompt_modifier`), and `control_plane` tools (imported as `skills.builtin.<skill_name>.action` or `.tools`).

To implement a new skill:
1. **Directory Structure**:
   - For a new agent tool skill, create [`config/skills/<skill_name>/`](file:///config/skills) (with an `__init__.py`, `SKILL.md`, and `tools.py`).
   - For a core engine directive or prompt modifier, create [`engine/skills/builtin/<skill_name>/`](file:///engine/skills/builtin).
2. **`SKILL.md` Manifest**: Define YAML frontmatter with `name`, `display_name`, `description`, `scope`, mandatory `requires_admin: true/false`, `triggers`, and tool schemas.
3. **Execution Logic**:
   - If tool-backed (`scope: agent`), implement `tools.py` and ensure `ToolPrincipal` authorization is respected.
   - If a slash directive, register the handler in [`engine/orchestration/stages/directive_handlers.py`](file:///engine/orchestration/stages/directive_handlers.py) (privileged directives set `requires_admin: true` in `SKILL.md` frontmatter which is centrally enforced by the Stage 1 gate).
   - If adding a Slack interactive button or modal, register an `ActionSpec` in [`engine/adapters/slack/interactivity.py`](file:///engine/adapters/slack/interactivity.py) and implement the handler under [`engine/adapters/slack/actions/`](file:///engine/adapters/slack/actions/).
4. **Validation Test**: Add corresponding test cases in [`tests/unit/test_agent_skills.py`](file:///tests/unit/test_agent_skills.py).

---

## 🎨 Code Style & Quality

- Python 3.11+ syntax with explicit type hints (`typing` / built-in generics) configured in [`pyproject.toml`](file:///pyproject.toml).
- Format and lint code using `ruff` and adhere to PEP 8 standards.
- Run `cmd /c .venv\Scripts\python.exe -u -m pytest` before finalizing any changes.
