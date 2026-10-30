# System Design & Lifecycle Architecture

This document details the core architectural design, module boundaries, and execution lifecycle of **Izvor**.

---

## 🏛️ Architecture Overview

Izvor is built as an asynchronous, event-driven service leveraging Python 3.11+, [FastAPI](https://fastapi.tiangolo.com/), and [Slack Bolt / SDK](https://slack.dev/python-slack-sdk/). It is structured into clear, decoupled layers:

```mermaid
graph TD
    subgraph Ingress Layer
        SlackWebhook["POST /slack/events"] --> SignatureGuard["verify_slack_signature (HMAC-SHA256)"]
        SlackInteractivity["POST /slack/interactivity"] --> SignatureGuard
        SignatureGuard --> Dedup["Event Deduplication (SQLite-backed EventDeduplicationCache)"]
    end

    subgraph Adapters Layer
        Dedup --> EventDispatcher["handle_slack_events (adapters/slack/events.py)"]
        EventDispatcher --> AsyncWorker["asyncio.create_task(process_and_respond)"]
    end

    subgraph Orchestration Engine
        AsyncWorker --> AgentRouter["orchestration/agent_router.py"]
        AgentRouter --> DiscussionLoop["DiscussionLoop (orchestration/discussion_loop.py)"]
        DiscussionLoop --> TurnPipeline["TurnPipeline (orchestration/turn_pipeline.py)"]
        TurnPipeline --> Stages["5 Modular Pipeline Stages (orchestration/stages/)"]
    end

    subgraph Persistence Layer
        Stages --> DBManager["DatabaseManager (core/database.py - SQLite WAL)"]
        DBManager --> SQLiteDB[("data/izvor.db (11 Tables)")]
        Stages --> FlatHistories["services/flat_history_recorder.py (flat-histories/)"]
    end

    subgraph Background Services
        Lifespan["FastAPI Lifespan (main.py)"]
        Lifespan --> DBInit["Database Schema Migrations & WAL Init"]
        Lifespan --> PricingWarmup["Pricing Cache Warm-up"]
        Lifespan --> ThreadPurge["Thread State TTL Purge (Every 10m)"]
        Lifespan --> AuditGuard["Audit Log Integrity Guard (Every 12h)"]
        Lifespan --> RetentionPrune["Data Retention Pruner (Every 24h)"]
        Lifespan --> RAGReconcile["RAG Knowledge Reconciliation"]
    end
```

---

## 🔄 The Ingress Lifecycle

### 1. Slack Signature Verification
Every inbound HTTP request arriving at `/slack/*` must pass cryptographic validation in [`core/slack_security.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/core/slack_security.py):
- The `X-Slack-Signature` and `X-Slack-Request-Timestamp` headers are inspected.
- The timestamp is verified to prevent replay attacks (tolerance window: 300 seconds).
- An HMAC-SHA256 signature is calculated using `SLACK_SIGNING_SECRET` over `v0:{timestamp}:{raw_body}`.
- Constant-time comparison (`hmac.compare_digest`) prevents timing side-channel attacks.

### 2. URL Verification Challenge
Slack sends a `url_verification` challenge during initial webhook setup. The handler immediately returns `{"challenge": req.challenge}` with HTTP 200 without invoking agent pipelines.

### 3. Event Deduplication
Slack re-delivers events if a webhook does not respond within 3 seconds. To prevent duplicate executions:
- [`adapters/slack/events.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/adapters/slack/events.py) uses [`EventDeduplicationCache`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/adapters/slack/events.py).
- The unique `event_id` is recorded in the SQLite `event_dedup` table and kept in an in-memory TTL set. If seen within the window, the request is immediately acknowledged with HTTP 200 and discarded.

### 4. Non-Blocking Fast Acknowledgment
To satisfy Slack’s strict 3-second webhook timeout:
- The HTTP handler acknowledges the webhook immediately (`HTTP 200 OK`).
- The multi-agent debate is scheduled as an unawaited background task using `asyncio.create_task(process_and_respond(...))`.

### 5. Amplification Defense & Sliding-Window Rate Limiting
To protect against denial-of-service attempts or audit-log disk exhaustion attacks:
- Failed signature verifications (`AUTH_FAILURE`) are throttled using a 60-second sliding-window burst limiter.
- Up to 5 discrete failure events are logged per client within each 60-second window, after which subsequent attempts are suppressed and emitted as an aggregated summary event.

---

## ⚙️ FastAPI Lifespan Background Services

Izvor uses the modern async context manager lifespan pattern in [`main.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/main.py). During application startup and shutdown, several continuous support services are initialized:

### 1. Database Schema Initialization & Numbered Migrations
- **File**: [`core/database.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/core/database.py)
- **Behavior**: Initializes `data/izvor.db` (configurable via `IZVOR_DB_PATH`) with WAL journal mode, 5,000ms (5.0s) busy timeout (managed via `db_busy_timeout_seconds` in system settings), dead-thread connection eviction, nested-transaction guards (`RuntimeError` on nested `transaction()` calls), and `PRAGMA user_version` numbered `MIGRATIONS` across **11 schema tables**:
  1. `thread_sessions` (thread state & generation counters)
  2. `thread_context` (key-value dynamic session context)
  3. `spend_ledger` (token and USD cost ledger)
  4. `hitl_approvals` (atomic Human-in-the-Loop approval records)
  5. `thread_skills` (ephemeral thread-scoped skill grants)
  6. `event_dedup` (Slack webhook event deduplication)
  7. `thread_usage_meta` (thread cost & message metadata managed by `ThreadUsageTracker`)
  8. `thread_notes_state` (Scribe live & conclusion note state managed by `ThreadNotesAgent`)
  9. `turn_checkpoints` (durable mid-turn snapshots for HITL and circuit-breaker pauses)
  10. `turn_traces` (stage-by-stage execution telemetry spans with write-behind flushing)
  11. `audit_anchors` (cryptographic head-hash anchors for monthly JSONL audit logs)

### 2. Windows Event Loop Heartbeat (`keep_loop_alive`)
- **File**: [`main.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/main.py)
- **Purpose**: On Windows systems running `ProactorEventLoop`, an idle event loop with no active socket operations can freeze. A lightweight periodic coroutine wakes the loop every 500ms.

### 3. Periodic Thread State Cleanup (`periodic_thread_state_cleanup`)
- **File**: [`main.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/main.py)
- **Schedule**: Dynamically loaded from `thread_cleanup_interval_seconds` in `system_settings.json` (default: 600.0 seconds / 10 minutes).
- **Behavior**: Calls [`thread_state_manager.cleanup_expired_threads()`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/orchestration/thread_state.py) and `cleanup_old_downloads()`. Frees in-memory session contexts and cached file downloads that have exceeded their idle time-to-live (TTL).

### 4. Audit Log Integrity Guard (`periodic_audit_integrity_guard`)
- **File**: [`main.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/main.py)
- **Schedule**: Dynamically loaded from `audit_integrity_check_interval_seconds` in `system_settings.json` (default: 43200.0 seconds / 12 hours).
- **Behavior**: Executes [`AppIntegrityValidator.check_audit_log_integrity()`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/orchestration/integrity_checks.py). Verifies hash continuity and `audit_anchors` head-hash consistency across active and historical audit files. If a discrepancy or HMAC secret mismatch is found, a high-severity alert is dispatched to all configured Slack admins (`SLACK_ADMIN_USER_IDS`).

### 5. Data Retention & Privacy Pruner (`periodic_retention_prune`)
- **File**: [`services/retention_service.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/services/retention_service.py)
- **Schedule**: Runs immediately at startup and every 24 hours (`86400` seconds).
- **Behavior**: Executes `prune_expired_records()`, redacting sensitive `messages_json` and `pending_tool_calls_json` from resolved `turn_checkpoints` rows immediately and deleting expired rows from `turn_checkpoints` (`retention_checkpoints_days`: 7d), `turn_traces` (`retention_traces_days`: 30d), and `event_dedup` (`retention_event_dedup_days`: 1d). Also provides GDPR-compliant `forget_thread(thread_ts)` erasure.

### 6. Model Pricing Cache Warmup (`warm_up_pricing_cache`)
- **File**: [`services/pricing_service.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/services/pricing_service.py)
- **Behavior**: Validates whether `data/pricing/model_pricing_cache.json` exists and is fresher than 7 days. If missing or expired, triggers background pricing research asynchronously without blocking server readiness.

### 7. RAG Knowledge Reconciliation (`rag_sync_engine.reconcile`)
- **File**: [`services/rag/sync_engine.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/engine/services/rag/sync_engine.py)
- **Behavior**: Scans `rag_knowledge/` for new, changed, or deleted documents, updating manifest hashes and index vectors.

---

## 🧩 Module Directory Boundaries

```
Izvor/
├── adapters/slack/             # Slack Webhook listeners, event routing, WorkspaceAdapter port
│   └── actions/                # Modular Block Kit action handlers (agent_actions, hitl_actions, workflow_actions)
├── agents/                     # System support agents (Scribe notes, Usage tracker, Pricing, Revisor)
├── core/                       # Core configuration, domain models, SQLite manager, cryptographic security
├── nlp/                        # Pre-flight intent & language extractor, entity & directive parsers
├── orchestration/              # Agent router, DiscussionLoop, TurnPipeline, settings stores, integrity checks
│   ├── stages/                 # 5 pipeline stages + directive_handlers.py & tool_status.py
│   └── state/                  # Domain state stores (discussion_store, context_store, evidence_store)
├── pipeline/                   # Modular pipeline subsystems (entity resolver, scribe, spend governance)
├── prompts/                    # Persona system prompts and system agent prompt templates
├── services/                   # Pricing, audit logger, HITL, retention, telemetry, checkpoint store, web/MCP tools
│   ├── rag/                    # Hybrid LanceDB/BM25 RAG engine, chunker, PII masker, sync engine
│   ├── slack/                  # Modular Slack WebClient wrapper (client_core, messages, files, views)
│   └── slack_home/             # Modular App Home tab builders (agents_tab, workflows_tab, governance_tab)
├── skills/                     # 21 dynamic built-in skills (including control_plane), loader & registry
├── tests/                      # Unit, integration, security, and regression test suites (88 files, 717 tests)
└── e2e_testing_harness/        # Behavioral testing suite, scenario runners, and synthetic Slack mock
```
