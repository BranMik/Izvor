# REST API Reference

This document provides complete technical specifications for the HTTP endpoints exposed by **Izvor** in [`main.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/main.py).

---

## 🔒 Authentication

- **Public Endpoints** (`/`, `/health`): Require no authentication. Hardened to return minimal status without exposing internal agent identifiers or configuration topology.
- **Administrative Endpoints** (`/api/*`): Require **HTTP Basic Authentication**.
  - Username: Set by `ADMIN_USERNAME` in environment/.env (default: `admin`).
  - Password: Set by `ADMIN_PASSWORD` in environment/.env.
  - Pass credentials in standard `Authorization: Basic <base64>` header.
  - Validated using `secrets.compare_digest` to defend against timing attacks. Emits structured non-repudiable audit events (`AUTH_SUCCESS`, `AUTH_FAILURE`).

---

## 📋 Endpoints Overview

| Method | Endpoint | Access Level | Description |
|---|---|---|---|
| `GET` | `/` | Public | Lightweight health check status. |
| `GET` | `/health` | Public | Lightweight health check status. |
| `GET` | `/api/health` | Admin | Authenticated health check exposing active registered persona keys. |
| `GET` | `/api/skills` | Admin | Returns all registered dynamic skills, descriptions, and schemas. |
| `GET` | `/api/rag/status` | Admin | Returns RAG manifest status and total indexed documents. |
| `POST` | `/api/rag/reconcile` | Admin | Triggers an immediate on-demand reconciliation of `rag_knowledge/`. |
| `GET` | `/api/audit/status` | Admin | Returns cryptographic integrity and HMAC hash chain verification diagnostics. |

---

## 📑 Detailed Endpoint Specifications

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

### 2. Authenticated Admin Health Check
- **Endpoint**: `GET /api/health`
- **Authentication**: Admin Basic Auth
- **Response** (`200 OK`):
```json
{
  "status": "healthy",
  "service": "Izvor Multi-Agent Hub",
  "agents": [
    "Visionary",
    "Innovator",
    "Sceptic",
    "CompliancePrivacyAndGovernanceOfficer",
    "ScopeOptimizer",
    "ROIStrategist",
    "UserAdvocate",
    "SecurityArchitect",
    "MarketAndCompetitionResearchExpert",
    "SalesAdvisor",
    "LegalAdvisor",
    "ProductStrategist",
    "RiskAuditor",
    "Tactician",
    "Revisor",
    "CreativeAdvisor"
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
      "description": "Searches the web for up-to-date benchmarks and news.",
      "tools": ["web_research"],
      "schemas": [
        {
          "type": "function",
          "function": {
            "name": "web_research",
            "description": "Search DuckDuckGo or Google",
            "parameters": {
              "type": "object",
              "properties": {
                "query": { "type": "string" }
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
- **Audit Event**: Emits `RAG_RECONCILE_TRIGGERED` event with `WARNING` severity.
- **Response** (`200 OK`):
```json
{
  "status": "reconciliation_complete",
  "stats": {
    "scanned": 3,
    "indexed": 0,
    "unchanged": 3,
    "removed": 0
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
  "scanned_files": ["audit/audit_2026_10.jsonl"],
  "total_records": 482,
  "hmac_continuity": {
    "matches": true,
    "status": "VALID",
    "last_seq": 482
  }
}
```

---

## 💻 cURL Examples

```bash
# Public health check
curl http://localhost:8000/health

# Authenticated admin health check
curl -u admin:YourAdminPassword http://localhost:8000/api/health

# Query registered skills (authenticated)
curl -u admin:YourAdminPassword http://localhost:8000/api/skills

# Check audit log integrity (authenticated)
curl -u admin:YourAdminPassword http://localhost:8000/api/audit/status

# Trigger RAG reconciliation (authenticated)
curl -X POST -u admin:YourAdminPassword http://localhost:8000/api/rag/reconcile
```
