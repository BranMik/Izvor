# Thread State Management & Memory Safety

This document describes how **Izvor** maintains stateful conversation context in active Slack threads while preventing memory leaks, guaranteeing durability across restarts, and eliminating concurrency race conditions.

---

## 🧠 Hybrid Architecture: In-Memory Speed & SQLite Durability

Izvor employs a hybrid state management architecture:
1. **Runtime Fast-Path (In-Memory)**: Coordinates active turns, generation-based cancellations, and atomic lock management via [`ThreadStateManager`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/orchestration/thread_state.py).
2. **Persistent ACID Engine (SQLite WAL)**: Provides zero-infrastructure, crash-resilient persistence via [`DatabaseManager`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/core/database.py) located at `data/izvor.db`.

```mermaid
graph TD
    SlackEvent([Slack Message Event]) --> GenCounter["Increment Generation Counter"]
    GenCounter --> ThreadState["ThreadStateManager (In-Memory Runtime)"]
    
    subgraph Persistent SQLite Engine (data/izvor.db)
        DBManager["DatabaseManager (WAL Mode)"]
        TableSessions["thread_sessions (Turns, Status, Sequencing)"]
        TableContext["thread_context (Constraints, Word Limits, Files)"]
        TableSpend["spend_ledger (Transactional Token & USD Ledger)"]
        TableHITL["hitl_approvals (Pending & Approved Tool Actions)"]
        TableSkills["thread_skills (Thread-Scoped Dynamic Grants)"]
        TableDedup["event_dedup (Idempotent Slack Event Cache)"]
    end
    
    ThreadState --> DBManager
    DBManager --> TableSessions
    DBManager --> TableContext
    DBManager --> TableSpend
    DBManager --> TableHITL
    DBManager --> TableSkills
    DBManager --> TableDedup
```

---

## 🗄️ SQLite State Schema Reference

The SQLite state engine is initialized on startup in write-ahead logging (`WAL`) mode with `busy_timeout = 5000ms`, `synchronous = NORMAL`, and `foreign_keys = ON`:

| Table | Purpose | Managed By |
|---|---|---|
| `thread_sessions` | Tracks thread lifecycle (`active`, `paused`, `completed`), remaining agent queue, current turn number, custom debate topologies, and round sequences. | [`DiscussionStateStore`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/orchestration/state/discussion_store.py) |
| `thread_context` | Persists word limits (`min_words`, `max_words`), history limits, detected thread language, included/excluded agents, and attached file contexts. | [`ThreadContextStore`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/orchestration/state/context_store.py) |
| `spend_ledger` | Append-only financial ledger recording prompt tokens, completion tokens, and dollar cost per model invocation. | [`SpendGovernance`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/pipeline/spend_governance/circuit_breaker.py) |
| `hitl_approvals` | Stores pending, approved, denied, or consumed Human-In-The-Loop approval requests with cryptographic parameter hashing. | [`HITLService`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/skills/hitl.py) |
| `thread_skills` | Thread-scoped dynamic skill grants (`skill_name`, `agent_id`, `expires_at`). | [`ThreadSkillsManager`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/orchestration/thread_skills_manager.py) |
| `event_dedup` | Persists event IDs and receipt timestamps for idempotent Slack webhook retries. | [`EventDeduplicationCache`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/adapters/slack/events.py) |

---

## 🛑 Generation-Based Superseding

In an asynchronous multi-agent system, a common race condition occurs when an agent is halfway through a slow LLM inference or tool execution, and a human user posts a new directive, correction, or question into the Slack thread.

If left unchecked, the obsolete agent turn would complete and post its answer, confusing the human and desynchronizing the conversation.

### How Izvor Solves This

Izvor utilizes a **Thread Generation Counter**:

1. **Incoming Message Increments Generation**:
   Whenever a human user posts a message in an existing thread, [`adapters/slack/events.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/adapters/slack/events.py) calls:
   ```python
   thread_state_manager.increment_generation(channel_id, thread_ts)
   ```
2. **Turn Captures Local Generation Snapshot**:
   When an agent turn begins in [`TurnPipeline`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/orchestration/turn_pipeline.py), it records `my_generation` from the manager:
   ```python
   ctx.my_generation = thread_state_manager.get_generation(channel_id, thread_ts)
   ```
3. **Continuous Generation Checks**:
   At each stage of the pipeline (before LLM call, after tool execution, and before posting to Slack), the pipeline evaluates `ctx.is_superseded()`:
   ```python
   if ctx.is_superseded():
       logger.info("Generation mismatch or task cancelled. Aborting stale turn.")
       return
   ```
4. **Clean Discard Without Race Conditions**:
   The stale background task terminates cleanly. The newly received human message takes over with its fresh generation number.

---

## 🧹 Memory Safety & Dual Pruning

Because Slack workspaces can run for months with thousands of transient threads, unboundedly accumulating state in memory would cause memory bloat and degrade performance.

Izvor enforces a two-layer bounded memory strategy:

### 1. Opportunistic LRU Capacity Pruning
- **`max_tracked_threads`** (default: `500`):
  [`ThreadStateManager`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/orchestration/thread_state.py) enforces a soft upper bound on the number of concurrently tracked threads in memory.
- When capacity is exceeded, the least recently active threads (LRU) are evicted opportunistically upon inserting new state.
- Because durable state resides in SQLite, evicting an in-memory thread does not destroy historical context; if a message arrives later for an evicted thread, its state is lazily reloaded from SQLite.

### 2. Scheduled Inactive TTL Pruning
- **Background Worker**: Clean-up task in `main.py` wakes up every 10 minutes (600s).
- **TTL Window**: Threads with no incoming messages or agent turns for more than 2 hours are pruned from the in-memory cache.
- **Persistent Archival**: Before any thread state is evicted, its final transcript is permanently written to the disk-backed flat history store in `flat-histories/` via [`services/flat_history_recorder.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/services/flat_history_recorder.py).
