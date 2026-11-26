# REST API Reference

This document provides complete technical specifications for the HTTP endpoints exposed by **Izvor** in [`engine/api/router.py`](file:///engine/api/router.py) (mounted by [`main.py`](file:///main.py)) and [`engine/adapters/slack/router.py`](file:///engine/adapters/slack/router.py).

## Table of Contents

- [Authentication](#authentication)
- [Endpoints Overview](#endpoints-overview)
- [Detailed Endpoint Specifications](#detailed-endpoint-specifications)
  - [1. Public Health Check](#1-public-health-check)
  - [1.1 Public Readiness Probe](#11-public-readiness-probe)
  - [2. Authenticated Admin Health Check](#2-authenticated-admin-health-check)
  - [3. Registered Skills Catalog](#3-registered-skills-catalog)
  - [4. RAG Knowledge Status](#4-rag-knowledge-status)
  - [5. Trigger RAG Reconciliation](#5-trigger-rag-reconciliation)
  - [6. Audit Log Cryptographic Integrity Status](#6-audit-log-cryptographic-integrity-status)
  - [7. Forget Thread Data (GDPR Channel-Scoped Erasure)](#7-forget-thread-data-gdpr-channel-scoped-erasure)
  - [7.1 Authenticated Subsystem Readiness](#71-authenticated-subsystem-readiness)
  - [7.2 Turn Execution Traces](#72-turn-execution-traces)
  - [7.3 Prometheus Metrics](#73-prometheus-metrics)
  - [8. Slack Inbound Webhooks](#8-slack-inbound-webhooks)
- [cURL Examples](#curl-examples)

---

## Authentication

- **Public Endpoints** (`/`, `/health`, `/health/ready`): Require no authentication. Hardened to return minimal status without exposing internal agent identifiers or configuration topology.
- **Administrative Endpoints** (`/api/*`): Require **HTTP Basic Authentication**.
  - Username: Set by `ADMIN_USERNAME` in environment/.env (default: `admin`).
  - Password: Set by `ADMIN_PASSWORD` in environment/.env.
  - Pass credentials in standard `Authorization: Basic <base64>` header.
  - Validated using `secrets.compare_digest` to defend against timing attacks. Emits structured non-repudiable audit events (`AUTH_SUCCESS`, `AUTH_FAILURE`).

---

## Endpoints Overview

| Method | Endpoint | Access Level | Description |
|---|---|---|---|
| `GET` | `/` | Public | Lightweight health check status. |
| `GET` | `/health` | Public | Lightweight health check status. |
| `GET` | `/health/ready` | Public | Minimal readiness probe (`200` for `ready`/`degraded`, `503` for `starting`/`unhealthy`). |
| `POST` | `/slack/events` | Slack HMAC | Inbound Slack Events API webhook (app mentions, thread replies, file-only DM uploads, URL verification challenge). |
| `POST` | `/slack/actions` | Slack HMAC | Inbound Slack interactive shortcuts and action handlers. |
| `POST` | `/slack/interactivity` | Slack HMAC | Inbound Slack Block Kit actions, App Home configuration modals, and HITL approvals. |
| `GET` | `/api/health` | Admin | Authenticated health check exposing active registered persona keys (`16` OOB personas). |
| `GET` | `/api/health/ready` | Admin | Detailed subsystem readiness breakdown (`database`, `audit_integrity`, `pricing_cache`, `rag_sync`, `retention_prune`, `pii_masker`, `mcp_discovery`). |
| `GET` | `/api/traces` | Admin | Query recorded `TurnPipeline` stage execution spans (`session_id`, `channel_id`, `thread_ts`, `agent_key`, `status`, `limit`). |
| `GET` | `/api/metrics` | Admin | Prometheus `text/plain; version=0.0.4` metrics exposition (readiness, active tasks, telemetry, sessions, spend, checkpoints). |
| `GET` | `/api/skills` | Admin | Returns all `22` registered dynamic skills (`4` configurable + `18` built-in), descriptions, and schemas. |
| `GET` | `/api/rag/status` | Admin | Returns RAG manifest status and total indexed documents. |
| `POST` | `/api/rag/reconcile` | Admin | Triggers an immediate on-demand reconciliation of `config/rag_knowledge/`. |
| `GET` | `/api/audit/status` | Admin | Returns cryptographic integrity, SQLite anchor, and HMAC keyring verification diagnostics. |
| `DELETE` | `/api/threads/{channel_id}/{thread_ts}` | Admin | Permanently purges persisted DB rows, flat histories, thread notes, downloads, and caches for a thread (GDPR erasure). |

---

## Detailed Endpoint Specifications

### 1. Public Health Check
- **Endpoint**: `GET /` or `GET /health`
- **Authentication**: None
- **Response** (`200 OK`):
```json
{
  "status": "healthy",
  "service": "Izvor Multi-Agent Hub"
}
```

---

### 1.1 Public Readiness Probe
- **Endpoint**: `GET /health/ready`
- **Authentication**: None
- **Behavior**: Returns HTTP `200 OK` when startup is complete and critical subsystems (`database`) are healthy (`status` is `"ready"` or `"degraded"` for non-fatal warmup failures). Returns HTTP `503 Service Unavailable` when `"starting"` or `"unhealthy"`.
- **Response** (`200 OK` / `503 Service Unavailable`):
```json
{
  "status": "ready",
  "service": "Izvor Multi-Agent Hub"
}
```

---

### 2. Authenticated Admin Health Check
- **Endpoint**: `GET /api/health`
- **Authentication**: Admin Basic Auth
- **Response** (`200 OK`):
```json
{
  "status": "healthy",
  "service": "Izvor Multi-Agent Hub",
  "agents": [
    "agent-10001",
    "agent-10002",
    "agent-10003",
    "agent-10004",
    "agent-10005",
    "agent-10006",
    "agent-10007",
    "agent-10008",
    "agent-10009",
    "agent-10010",
    "agent-10011",
    "agent-10012",
    "agent-10013",
    "agent-10014",
    "agent-10015",
    "agent-10016"
  ]
}
```

---

### 3. Registered Skills Catalog
- **Endpoint**: `GET /api/skills`
- **Authentication**: Admin Basic Auth
- **Response** (`200 OK`):
```json
{
  "skills": [
    {
      "name": "web_research",
      "display_name": "Web Research",
      "description": "Searches the web and fetches web page content.",
      "tools": ["web_search", "fetch_web_page"],
      "schemas": [
        {
          "type": "function",
          "function": {
            "name": "web_search",
            "description": "Search the live web via multi-engine DDGS search for up-to-date information",
            "parameters": {
              "type": "object",
              "properties": {
                "query": { "type": "string" },
                "max_results": { "type": "integer" }
              },
              "required": ["query"]
            }
          }
        }
      ]
    }
  ]
}
```

---

### 4. RAG Knowledge Status
- **Endpoint**: `GET /api/rag/status`
- **Authentication**: Admin Basic Auth
- **Response** (`200 OK`):
```json
{
  "status": "ready",
  "total_documents": 3,
  "documents": [
    {
      "path": "shared/system_overview.md",
      "hash": "b47c8f...",
      "last_modified": 1790455185
    }
  ]
}
```

---

### 5. Trigger RAG Reconciliation
- **Endpoint**: `POST /api/rag/reconcile`
- **Authentication**: Admin Basic Auth
- **Audit Event**: Emits `RAG_RECONCILE_COMPLETED` event with `INFO` severity (or `RAG_RECONCILE_FAILED` with `CRITICAL` severity on error).
- **Response** (`200 OK`):
```json
{
  "status": "reconciliation_complete",
  "stats": {
    "new": 0,
    "modified": 0,
    "deleted": 0,
    "unchanged": 3
  }
}
```

---

### 6. Audit Log Cryptographic Integrity Status
- **Endpoint**: `GET /api/audit/status`
- **Authentication**: Admin Basic Auth
- **Response** (`200 OK`):
```json
{
  "valid": true,
  "errors": [],
  "scanned_files": ["data/audit/audit_2026_10.jsonl"],
  "total_records": 482,
  "hmac_continuity": {
    "matches": true,
    "status": "VALID",
    "last_seq": 482
  }
}
```

---

### 7. Forget Thread Data (GDPR Channel-Scoped Erasure)
- **Endpoint**: `DELETE /api/threads/{channel_id}/{thread_ts}`
- **Authentication**: Admin Basic Auth
- **Audit Event**: Emits `THREAD_DATA_PURGED` (`retention_service`) and `GDPR_THREAD_FORGOTTEN` (`admin_user`, `WARNING` severity).
- **Behavior**: Invokes `forget_thread(channel_id, thread_ts)` in a worker thread to permanently purge SQLite thread rows, channel-scoped flat histories (`data/flat-histories/`), Scribe thread notes (`data/thread_notes/`), thread-scoped attachment folders (`data/downloads/<cid>_<tts>/`), guardrail embedding LRU cache, spend session state, and registered in-memory thread purge hooks.
- **Response** (`200 OK`):
```json
{
  "status": "forgotten",
  "channel_id": "C0123456789",
  "thread_ts": "1790455185.000100",
  "deleted": {
    "turn_checkpoints": 0,
    "turn_traces": 4,
    "thread_notes_state": 1,
    "thread_usage_meta": 1,
    "thread_context": 1,
    "thread_sessions": 1,
    "thread_skills": 0,
    "hitl_approvals": 0,
    "spend_ledger": 4,
    "flat_histories": 1,
    "thread_notes": 2,
    "downloads": 0
  }
}
```

---

### 7.1 Authenticated Subsystem Readiness
- **Endpoint**: `GET /api/health/ready`
- **Authentication**: Admin Basic Auth
- **Behavior**: Returns HTTP `200 OK` (`status`: `"ready"` or `"degraded"` for non-fatal warmup failures) or HTTP `503 Service Unavailable` (`"starting"` or `"unhealthy"` if `database` fails), with per-component details (`database`, `audit_integrity`, `pricing_cache`, `rag_sync`, `retention_prune`, `pii_masker`, `mcp_discovery`).
- **Response** (`200 OK` / `503 Service Unavailable`, `AdminReadinessResponse`):
```json
{
  "status": "ready",
  "service": "Izvor Multi-Agent Hub",
  "startup_complete": true,
  "components": {
    "database": {
      "status": "ready",
      "detail": "",
      "critical": true,
      "updated_at": 1790455180.12
    },
    "rag_sync": {
      "status": "ready",
      "detail": "",
      "critical": false,
      "updated_at": 1790455181.45
    }
  }
}
```

### 7.2 Turn Execution Traces
- **Endpoint**: `GET /api/traces`
- **Authentication**: Admin Basic Auth
- **Query Parameters**: `session_id`, `channel_id`, `thread_ts`, `agent_key`, `status`, `limit` (`1..500`, default `50`).
- **Behavior**: Flushes buffered telemetry spans and returns matching `TurnPipeline` stage execution spans (`validation`, `resolution`, `context_prep`, `execution`, `post_record`).
- **Response** (`200 OK`, `TurnTracesResponse`):
```json
{
  "count": 1,
  "traces": [
    {
      "trace_id": "3f9a7d2c-8c1e-4b6b-9f41-9d2a1b4e5c01",
      "session_id": "C0123456789:1790455185.000100",
      "channel_id": "C0123456789",
      "thread_ts": "1790455185.000100",
      "agent_key": "agent-10001",
      "stage_name": "execution",
      "duration_ms": 842.5,
      "prompt_tokens": 1120,
      "completion_tokens": 145,
      "tools_called": ["search_knowledge_rag"],
      "status": "SUCCESS",
      "error_message": null,
      "created_at": 1790455190.25
    }
  ]
}
```

### 7.3 Prometheus Metrics
- **Endpoint**: `GET /api/metrics`
- **Authentication**: Admin Basic Auth
- **Response** (`200 OK`, `text/plain; version=0.0.4; charset=utf-8`): Exposes `izvor_up`, `izvor_readiness_component_status`, `izvor_active_background_tasks`, `izvor_telemetry_*`, `izvor_thread_sessions_total`, `izvor_turn_traces_total`, `izvor_turn_trace_duration_ms_*`, `izvor_spend_usd_total`, `izvor_tokens_total`, and `izvor_turn_checkpoints_total`.
```text
# HELP izvor_up Whether Izvor is critically ready to serve traffic (1 = ready/degraded, 0 = starting/unhealthy).
# TYPE izvor_up gauge
izvor_up 1
# HELP izvor_active_background_tasks Number of currently active tracked background asyncio tasks.
# TYPE izvor_active_background_tasks gauge
izvor_active_background_tasks 0
# HELP izvor_spend_usd_total Total recorded LLM spend in USD across the spend_ledger.
# TYPE izvor_spend_usd_total counter
izvor_spend_usd_total 0.14285
```

---

### 8. Slack Inbound Webhooks
- **Endpoints**:
  - `POST /slack/events`: Primary Events API endpoint. Handles Slack URL verification challenges (`url_verification`), app mentions, file-only DM uploads, and conversational thread message events.
  - `POST /slack/actions`: Handles Slack message shortcut actions and interactive component triggers.
  - `POST /slack/interactivity`: Handles Block Kit button clicks (HITL approvals, circuit breaker "Continue Sequence" actions) and App Home modal views (`view_submission`).
- **Authentication**: Slack HMAC-SHA256 signature verification via headers `X-Slack-Request-Timestamp` and `X-Slack-Signature`.
- **Response** (`200 OK`):
```json
{
  "status": "ok"
}
```
*(or challenge response `{"challenge": "..."}` during initial URL verification)*.

---

## cURL Examples

```bash
# Public health & readiness checks
curl http://localhost:8000/health
curl http://localhost:8000/health/ready

# Authenticated admin health & subsystem readiness (authenticated)
curl -u admin:YourAdminPassword http://localhost:8000/api/health
curl -u admin:YourAdminPassword http://localhost:8000/api/health/ready

# Query turn traces & Prometheus metrics (authenticated)
curl -u admin:YourAdminPassword "http://localhost:8000/api/traces?limit=20"
curl -u admin:YourAdminPassword http://localhost:8000/api/metrics

# Query registered skills (authenticated)
curl -u admin:YourAdminPassword http://localhost:8000/api/skills

# Check audit log integrity (authenticated)
curl -u admin:YourAdminPassword http://localhost:8000/api/audit/status

# Trigger RAG reconciliation (authenticated)
curl -X POST -u admin:YourAdminPassword http://localhost:8000/api/rag/reconcile

# Permanently forget a thread (GDPR erasure, authenticated)
curl -X DELETE -u admin:YourAdminPassword http://localhost:8000/api/threads/C0123456789/1790455185.000100
```
