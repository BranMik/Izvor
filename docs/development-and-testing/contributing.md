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
- Lower architectural layers (`core`, `nlp`, `pipeline`, `services`) must never import from `orchestration` or `adapters` (enforced by [`tests/test_architecture_layers.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/tests/test_architecture_layers.py)).
- All runtime LLM and embedding invocations must use async `litellm.acompletion` and `litellm.aembedding` (enforced by [`tests/test_async_llm_enforcement.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/tests/test_async_llm_enforcement.py)).

---

## ➕ Adding a New Persona

To introduce a new debate persona:

1. **System Prompt**: Add a dedicated markdown prompt file in [`prompts/`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/prompts) defining the persona's core perspective, biases, and tone.
2. **Roster Registration**: Register the agent entry in [`data/settings/agent_settings.json`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/config/settings/agent_settings.json) with:
   - `username`: Canonical identifier (e.g. `DataScientist`).
   - `displayname`: Formatted title (e.g. `Data Scientist`).
   - `model`: Default LLM router string.
   - `icon_url`: Avatar asset path.
   - `skills`: Initial tool permissions.
   - `special_role`: Dialectical role (`"NONE"`, `"CHALLENGER"`, or `"REBUTTAL"`).
3. **Entity Resolution**: Add aliases or nickname mappings to [`pipeline/entity_resolver`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/pipeline/entity_resolver).
4. **Tests**: Add unit test verification to [`tests/test_agent_settings_ssot.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/tests/test_agent_settings_ssot.py).

---

## 🛠️ Adding a New Dynamic Skill or Directive

To implement a new skill or directive:

1. **Directory Structure**: Create a new folder under [`skills/<skill_name>/`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/config/skills).
2. **`SKILL.md` Manifest**: Define YAML frontmatter with `name`, `display_name`, `description`, `scope`, `triggers`, and tool schemas.
3. **Execution Logic**:
   - If tool-backed (`scope: agent`), implement `tools.py` and ensure `ToolPrincipal` authorization is respected.
   - If a slash directive, register the handler in [`orchestration/stages/directive_handlers.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/orchestration/stages/directive_handlers.py) and gate privileged mutations with `require_directive_admin`.
   - If adding a Slack interactive button or modal, register an `ActionSpec` in [`adapters/slack/interactivity.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/adapters/slack/interactivity.py) and implement the handler under [`adapters/slack/actions/`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/adapters/slack/actions/).
4. **Validation Test**: Add corresponding test cases in [`tests/test_agent_skills.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/tests/test_agent_skills.py).

---

## 🎨 Code Style & Quality

- Python 3.11+ syntax with explicit type hints (`typing` / built-in generics) configured in [`pyproject.toml`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/pyproject.toml).
- Format and lint code using `ruff` and adhere to PEP 8 standards.
- Run `cmd /c .venv\Scripts\python.exe -u -m pytest` before finalizing any changes.
