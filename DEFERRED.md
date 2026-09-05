# Deferred Technical Debt & Refactoring Tasks

This document tracks identified architectural improvements and technical debt items that were surfaced during codebase analysis (e.g. by Opus review) and intentionally deferred to avoid disrupting active feature delivery.

---

## 1. `agent_router.py` Re-Export Façade Decoupling — [COMPLETED]

### Status: COMPLETED
- Migrated all test and application imports off `orchestration.agent_router` to their canonical homes (`orchestration.thread_state`, `orchestration.agent_settings`, `orchestration.nomination`, `core.llm_utils`, `orchestration.turn_pipeline`).
- Updated `tests/conftest.py` to patch `orchestration.agent_settings.SETTINGS_FILE` instead of the re-exported symbol.
- Removed dynamic fallback shims (`__getattr__` and `__dir__`) and obsolete re-exports from [`orchestration/agent_router.py`](file:///c:/Users/Hp/Documents/agy2-projects/SlackAgenticPanel/orchestration/agent_router.py).
- [`orchestration/agent_router.py`](file:///c:/Users/Hp/Documents/agy2-projects/SlackAgenticPanel/orchestration/agent_router.py) is now strictly focused on high-level routing coordination (`process_and_respond`, `orchestrate_discussion`) and explicit pipeline wiring.
- Verified with all 221 tests passing across the repository.

---

## 2. `ThreadStateManager` Abstraction Decomposition — [COMPLETED]

### Status: COMPLETED
- Decomposed monolithic thread state management into cohesive domain stores under `orchestration/state/`:
  - `DiscussionStateStore` ([orchestration/state/discussion_store.py](file:///c:/Users/Hp/Documents/agy2-projects/SlackAgenticPanel/orchestration/state/discussion_store.py)): active thread tasks, remaining agent queues, custom flow tags, and stop flags.
  - `TurnRetryStateStore` ([orchestration/state/retry_store.py](file:///c:/Users/Hp/Documents/agy2-projects/SlackAgenticPanel/orchestration/state/retry_store.py)): rate-limit retries, 503 high-demand error tracking, and thread generation counters.
  - `ThreadContextStore` ([orchestration/state/context_store.py](file:///c:/Users/Hp/Documents/agy2-projects/SlackAgenticPanel/orchestration/state/context_store.py)): history limits, word count limits, locked languages, and file context attachments.
- Refactored `ThreadStateManager` in [`orchestration/thread_state.py`](file:///c:/Users/Hp/Documents/agy2-projects/SlackAgenticPanel/orchestration/thread_state.py) into a clean Facade composing all three domain stores.
- Preserved 100% backward compatibility for all module-level variables, convenience functions, and property references.
- Verified with dedicated unit tests in [`tests/test_domain_state_stores.py`](file:///c:/Users/Hp/Documents/agy2-projects/SlackAgenticPanel/tests/test_domain_state_stores.py) and 225/225 passed tests across the entire repository.

---

## 3. `TurnPipeline` Hook Pattern Replacement with Dependency Injection — [COMPLETED]

### Status: COMPLETED
- `TurnPipeline.__init__` in [`orchestration/turn_pipeline.py`](file:///c:/Users/Hp/Documents/agy2-projects/SlackAgenticPanel/orchestration/turn_pipeline.py) now accepts explicit dependencies (`llm_executor`, `slack_poster`, `history_fetcher`, `files_fetcher`, `token_recorder`, `notes_updater`, `usage_updater`, `sleep_fn`, etc.) with fallback to canonical defaults.
- `_get_hook()` prioritizes constructor-injected callables before falling back to monkeypatches or defaults.
- Verified with dedicated unit tests in [`tests/test_turn_pipeline_di.py`](file:///c:/Users/Hp/Documents/agy2-projects/SlackAgenticPanel/tests/test_turn_pipeline_di.py).

---

## 4. `prompts/builder.py` Hardcoded System Agent Skip List — [COMPLETED]

### Status: COMPLETED
- System agent identification is now driven dynamically by configuration metadata (`is_system_agent: true` / `agent_key` in `data/agent_settings.json` and `SYSTEM_AGENTS_CONFIG`) via `is_system_agent(agent_key)` in [`prompts/builder.py`](file:///c:/Users/Hp/Documents/agy2-projects/SlackAgenticPanel/prompts/builder.py).
- Hardcoded tuple checks were removed and covered by automated tests in [`tests/test_prompts.py`](file:///c:/Users/Hp/Documents/agy2-projects/SlackAgenticPanel/tests/test_prompts.py).

