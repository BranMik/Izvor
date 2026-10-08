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

### 5. Git Commit Policy
Do not automatically create git commits after making changes. Commits should be explicitly requested or reviewed by the repository owner.

---

## ➕ Adding a New Persona

To introduce a new debate persona:

1. **System Prompt**: Add a dedicated markdown prompt file in [`prompts/`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/prompts) defining the persona's core perspective, biases, and tone.
2. **Roster Registration**: Register the agent entry in [`data/settings/agent_settings.json`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/data/settings/agent_settings.json) with:
   - `username`: Canonical identifier (e.g. `DataScientist`).
   - `displayname`: Formatted title (e.g. `Data Scientist`).
   - `model`: Default LLM router string.
   - `icon_url`: Avatar asset path.
   - `skills`: Initial tool permissions.
3. **Entity Resolution**: Add aliases or nickname mappings to [`pipeline/entity_resolver`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/pipeline/entity_resolver).
4. **Tests**: Add unit test verification to [`tests/test_agent_settings_ssot.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/tests/test_agent_settings_ssot.py).

---

## 🛠️ Adding a New Dynamic Skill

To implement a new skill:

1. **Directory Structure**: Create a new folder under [`skills/<skill_name>/`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/skills).
2. **`SKILL.md` Manifest**: Define YAML frontmatter with `name`, `display_name`, `description`, and parameter schemas.
3. **Execution Logic**: If tool-backed, implement the execution handler and register it with the skill registry.
4. **Validation Test**: Add corresponding test cases in [`tests/test_agent_skills.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/tests/test_agent_skills.py).

---

## 🎨 Code Style & Quality

- Python 3.11+ syntax with explicit type hints (`typing` / built-in generics).
- Format code using `ruff` and adhere to PEP 8 standards.
- Run `pytest tests/test_code_hygiene.py` before finalizing any changes.
