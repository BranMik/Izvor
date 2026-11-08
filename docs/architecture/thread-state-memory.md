# Thread State Management, 3-Tier Memory & Data Retention

This document describes how **Izvor** maintains stateful conversation context in active Slack threads while preventing memory leaks, guaranteeing durability across restarts, eliminating concurrency race conditions, and enforcing GDPR-compliant data retention.

---

## 🧠 Hybrid Architecture: In-Memory Speed & SQLite Durability

Izvor employs a hybrid state management architecture:
1. **Runtime Fast-Path (In-Memory)**: Coordinates active turns, generation-based cancellations, and atomic lock management via [`ThreadStateManager`](file:///engine/orchestration/thread_state.py).
2. **Persistent ACID Engine (SQLite WAL)**: Provides zero-infrastructure, crash-resilient persistence via [`DatabaseManager`](file:///engine/core/database.py) located at `data/izvor.db`.

```mermaid
graph TD
    SlackEvent([Slack Message Event]) --> GenCounter["Increment Generation Counter"]
    GenCounter --> ThreadState["ThreadStateManager (In-Memory Runtime)"]
    
    subgraph Persistent SQLite Engine (data/izvor.db - 11 Tables)
        DBManager["DatabaseManager (WAL Mode + Numbered MIGRATIONS)"]
        TableSessions["thread_sessions (Turns, Status, Sequencing)"]
        TableContext["thread_context (Constraints, Word Limits, Files)"]
        TableSpend["spend_ledger (Transactional Token & USD Ledger)"]
        TableHITL["hitl_approvals (Atomic Pending/Approved Tool Actions)"]
        TableSkills["thread_skills (Thread-Scoped Dynamic Grants)"]
        TableDedup["event_dedup (Idempotent Slack Event Cache)"]
        TableUsage["thread_usage_meta (Thread Cost & Message Metadata)"]
        TableNotes["thread_notes_state (Scribe Live & Conclusion Note State)"]
        TableCheckpoints["turn_checkpoints (Mid-Turn HITL & CB Snapshots)"]
        TableTraces["turn_traces (Stage Execution Telemetry Spans)"]
        TableAnchors["audit_anchors (Monthly Audit Head-Hash Anchors)"]
    end
    
    ThreadState --> DBManager
    DBManager --> TableSessions
    DBManager --> TableContext
    DBManager --> TableSpend
    DBManager --> TableHITL
    DBManager --> TableSkills
    DBManager --> TableDedup
    DBManager --> TableUsage
    DBManager --> TableNotes
    DBManager --> TableCheckpoints
    DBManager --> TableTraces
    DBManager --> TableAnchors
```

---

## 🗄️ SQLite State Schema & Migration Reference

The SQLite state engine is initialized on startup in write-ahead logging (`WAL`) mode with `busy_timeout = 5000ms` (5.0s, managed via `db_busy_timeout_seconds` in `system_settings.json`), `synchronous = NORMAL`, and `foreign_keys = ON`.

[`DatabaseManager`](file:///engine/core/database.py) enforces:
- **Numbered Schema Migrations (`MIGRATIONS`)**: Tracked via `PRAGMA user_version`, automatically applying incremental DDL upgrades inside atomic transactions.
- **Nested Transaction Guard**: Calling `db_manager.transaction()` inside an already active transaction raises a `RuntimeError` to prevent silent partial commits.
- **Dead-Thread Connection Eviction**: Automatically closes and evicts `sqlite3.Connection` handles belonging to terminated worker threads (`not thread.is_alive()`).

| Table | Purpose | Managed By |
|---|---|---|
| `thread_sessions` | Tracks thread lifecycle (`active`, `paused`, `completed`), remaining agent queue, current turn number, custom debate topologies, and round sequences. | [`DiscussionStateStore`](file:///engine/orchestration/state/discussion_store.py) |
| `thread_context` | Persists word limits (`min_words`, `max_words`), history limits, detected thread language, included/excluded agents, and attached file contexts. | [`ThreadContextStore`](file:///engine/orchestration/state/context_store.py) |
| `spend_ledger` | Append-only financial ledger recording prompt tokens, completion tokens, and dollar cost per model invocation. | [`SpendGovernance`](file:///engine/pipeline/spend_governance/circuit_breaker.py) |
| `hitl_approvals` | Stores pending, approved, denied, or consumed Human-In-The-Loop approval requests with cryptographic parameter hashing and atomic single-statement transitions. | [`services.hitl`](file:///engine/services/hitl.py) |
| `thread_skills` | Thread-scoped dynamic skill grants (`skill_name`, `agent_id`, `expires_at`). | [`ThreadSkillsManager`](file:///engine/orchestration/thread_skills_manager.py) |
| `event_dedup` | Persists event IDs and receipt timestamps for idempotent Slack webhook retries. | [`EventDeduplicationCache`](file:///engine/adapters/slack/events.py) |
| `thread_usage_meta` | Stores per-thread cost summaries and Slack usage report message timestamps (replacing legacy JSON file writes). | [`ThreadUsageTracker`](file:///engine/agents/usage_agent.py) |
| `thread_notes_state` | Persists live Scribe note timestamps, final conclusion note timestamps, synthesized content, and pin status per thread. | [`ThreadNotesAgent`](file:///engine/agents/thread_notes_agent.py) |
| `turn_checkpoints` | Stores durable point-in-turn conversation snapshots (`messages_json`, `pending_tool_calls_json`, `iteration`) when a turn pauses for HITL approval or circuit-breaker confirmation. | [`TurnCheckpointStore`](file:///engine/services/turn_checkpoint_store.py) |
| `turn_traces` | Persists structured stage-by-stage execution spans (`stage_name`, `duration_ms`, `status`, `metadata_json`) via a non-blocking write-behind batch queue. | [`TurnTraceRecorder`](file:///engine/services/telemetry.py) |
| `audit_anchors` | Stores cryptographic head-hash anchors `(month, seq, entry_hash, anchored_at)` to detect tail truncation or file deletion of monthly JSONL audit logs. | [`services.audit_logger`](file:///engine/services/audit_logger.py) |

---

## 🏛️ 3-Tier Hierarchical Memory & Shared Round Evidence

To maintain deep long-horizon coherence without blowing up LLM context windows or repeating expensive tool calls, Izvor constructs agent context in **Stage 3 (`ContextPrepStage`)** using a 3-tier memory hierarchy:

1. **Tier 1 — Sliding Turn Window (`/history N`)**:
   - Retains the most recent $N$ full messages in chronological order so personas have verbatim access to immediate exchanges.
2. **Tier 2 — Episodic Thread Summary (`<episodic_thread_summary>`)**:
   - Whenever the total thread length exceeds the Tier 1 sliding window, `ContextPrepStage` retrieves the latest synthesized note from `thread_notes_state` (generated by `ThreadNotesAgent`) and injects it inside `<episodic_thread_summary>` tags.
3. **Tier 3 — Shared Round Evidence (`<shared_round_evidence>`)**:
   - Managed by [`RoundEvidenceStore`](file:///engine/orchestration/state/evidence_store.py), an in-memory bounded LRU store (`max_entries=256` threads, `max_items_per_thread=32`).
   - Read-only tool outputs (`web_search`, `fetch_web_page`, `list_documents`, `read_document`, and agent-scoped `search_knowledge_rag`) executed during a round are cached and injected into `<shared_round_evidence>`, preventing redundant tool invocations across consecutive personas.

---

## 🛑 Generation-Based Superseding

In an asynchronous multi-agent system, a common race condition occurs when an agent is halfway through a slow LLM inference or tool execution, and a human user posts a new directive, correction, or question into the Slack thread.

### How Izvor Solves This

Izvor utilizes a **Thread Generation Counter**:

1. **Incoming Message Increments Generation**:
   Whenever a human user posts a message in an existing thread, [`adapters/slack/events.py`](file:///engine/adapters/slack/events.py) calls:
   ```python
   thread_state_manager.increment_generation(channel_id, thread_ts)
   ```
2. **Turn Captures Local Generation Snapshot**:
   When an agent turn begins in [`TurnPipeline`](file:///engine/orchestration/turn_pipeline.py), it records `my_generation` from the manager:
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

## 🧹 Memory Safety, Retention & Right-to-Forget

Izvor enforces multi-layered memory and disk hygiene:

### 1. Opportunistic LRU Capacity Pruning
- **`max_tracked_threads`** (default: `500`):
  [`ThreadStateManager`](file:///engine/orchestration/thread_state.py) enforces a soft upper bound on the number of concurrently tracked threads in memory, evicting least-recently-used entries when capacity is exceeded.

### 2. Scheduled Inactive TTL Pruning (`periodic_thread_state_cleanup`)
- **Background Worker**: Wakes up every **600 seconds (10 minutes)** (configured via `thread_cleanup_interval_seconds` in `system_settings.json`).
- **TTL Window & Liveness Check**: Threads idle for more than 1 hour (`THREAD_STATE_TTL_SECONDS = 3600.0`) without an active `asyncio.Task` (`task and not task.done()`) are evicted from RAM, and expired rows in `thread_sessions` and `thread_context` are cleaned up.
- **Persistent Archival**: Completed turn transcripts are archived to `data/flat-histories/` (`FLAT_HISTORIES_DIR` in [`core/paths.py`](file:///engine/core/paths.py)) via [`services/flat_history_recorder.py`](file:///engine/services/flat_history_recorder.py).

### 3. Automated Database Retention & GDPR Erasure ([`services/retention_service.py`](file:///engine/services/retention_service.py))
- **`prune_expired_records()` (Runs at Startup & Every 24 Hours)**:
  - Immediately scrubs sensitive `messages_json` and `pending_tool_calls_json` payloads from `turn_checkpoints` rows once their status transitions out of `paused`.
  - Deletes resolved `turn_checkpoints` older than `retention_checkpoints_days` (default: `7` days).
  - Deletes `turn_traces` spans older than `retention_traces_days` (default: `30` days).
  - Deletes `event_dedup` entries older than `retention_event_dedup_days` (default: `1` day).
- **`forget_thread(thread_ts)` (Right-to-Forget)**:
  - Atomically purges all records for a given `thread_ts` across all SQLite tables (`turn_checkpoints`, `turn_traces`, `thread_notes_state`, `thread_usage_meta`, `thread_context`, `thread_sessions`, `thread_skills`, `hitl_approvals`, `spend_ledger`), evicts `RoundEvidenceStore` cache entries, deletes matching markdown files in `data/flat-histories/`, and logs a non-repudiable `THREAD_DATA_PURGED` audit event.
