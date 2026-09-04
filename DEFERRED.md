# Deferred Technical Debt & Refactoring Tasks

This document tracks identified architectural improvements and technical debt items that were surfaced during codebase analysis (e.g. by Opus review) and intentionally deferred to avoid disrupting active feature delivery.

---

## 1. `agent_router.py` Re-Export Façade Decoupling

### Current State
[`orchestration/agent_router.py`](file:///c:/Users/Hp/Documents/agy2-projects/SlackAgenticPanel/orchestration/agent_router.py) contains ~100 lines (lines 54–153) of re-exports:
```python
from .thread_state import (
    THREAD_STATE,
    ThreadStateManager,
    get_generation,
    set_generation,
    ...
)
from .agent_settings import (
    AgentSettingsManager,
    AGENTS_CONFIG,
    SYSTEM_AGENTS_CONFIG,
    ...
)
```
The actual router logic (`process_and_respond`, `orchestrate_discussion`) is only ~170 lines, but the file serves as a monolithic umbrella module for [`main.py`](file:///c:/Users/Hp/Documents/agy2-projects/SlackAgenticPanel/main.py) and various legacy tests.

### Target Refactoring
1. Update [`main.py`](file:///c:/Users/Hp/Documents/agy2-projects/SlackAgenticPanel/main.py) and external call sites to import directly from their true home modules:
   - `orchestration.thread_state` for thread state management
   - `orchestration.agent_settings` for agent settings and SSOT persistence
   - `orchestration.turn_pipeline` for turn execution
2. Trim [`orchestration/agent_router.py`](file:///c:/Users/Hp/Documents/agy2-projects/SlackAgenticPanel/orchestration/agent_router.py) to purely contain high-level Slack routing and discussion coordination.
3. Eliminate redundant backward-compatibility shims once all test imports are updated.

---

## 2. `ThreadStateManager` Abstraction Decomposition

### Current State
[`orchestration/thread_state.py`](file:///c:/Users/Hp/Documents/agy2-projects/SlackAgenticPanel/orchestration/thread_state.py) (~651 lines) is a God Object managing 12+ separate in-memory dictionaries:
- `_generations`
- `_remaining_agents`
- `_retry_queue`
- `_has_503_error`
- `_thread_files`
- `_word_limits`
- `_history_limits`
- `_active_discussion_tasks`
- `_custom_orchestration_flows`
- `_stop_signals`
- `_lock` (threading lock)

### Target Refactoring
Decompose into cohesive domain stores under `orchestration/state/`:
- `DiscussionStateStore`: tracking remaining agents, active tasks, custom orchestration flows, stop signals.
- `TurnRetryStateStore`: tracking 503 error backoff, retry queues, generations.
- `ThreadContextStore`: tracking thread files, word limits, history limits.
Wrap with a facade if needed, or maintain thread-safe concurrency abstractions if the service transitions to multi-worker / async task distribution.

---

## 3. `TurnPipeline` Hook Pattern Replacement with Dependency Injection

### Current State
In [`orchestration/turn_pipeline.py`](file:///c:/Users/Hp/Documents/agy2-projects/SlackAgenticPanel/orchestration/turn_pipeline.py), `_get_hook` dynamically inspects `sys.modules["agent_router"]` at runtime:
```python
exec_retry_fn = self._get_hook("execute_llm_with_retry", execute_llm_with_retry)
post_resp_fn = self._get_hook("post_agent_response", post_agent_response)
```
This was created to support test monkeypatching without explicit dependency injection.

### Target Refactoring
Provide explicit dependencies via `TurnPipeline.__init__(...)` with defaults:
```python
def __init__(
    self,
    llm_executor: Callable = execute_llm_with_retry,
    slack_poster: Callable = post_agent_response,
    skill_registry: SkillRegistry | None = None,
): ...
```
This removes invisible module-level coupling and makes unit tests cleaner and isolated.

---

## 4. `prompts/builder.py` Hardcoded System Agent Skip List

### Current State
In [`prompts/builder.py:71-76`](file:///c:/Users/Hp/Documents/agy2-projects/SlackAgenticPanel/prompts/builder.py#L71-L76):
```python
if agent_key in ("Thread_Notes_Agent", "Usage_Agent", "Pricing_Agent"):
    return f"\n{persona_content}\n\n"
```
Adding or renaming system agents requires modifying code logic in `builder.py`.

### Target Refactoring
Drive system agent behavior from configuration metadata (e.g. `"is_system_agent": true` in `agent_settings.json` or `SYSTEM_AGENTS_CONFIG`), eliminating identity checks.
