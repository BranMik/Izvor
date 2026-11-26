# Audit Logging & Cryptographic Integrity

This document details **Izvor**'s non-repudiable audit logging system, cryptographic hash-chaining architecture, SQLite head-hash anchoring (`audit_anchors`), and automated integrity validation guards.

---

## 🔒 Architectural Overview

Enterprise governance requires an immutable record of system events, administrative changes, and security actions. Izvor implements a hash-chained, HMAC-signed JSON Lines (JSONL) audit log managed by [`services/audit_logger.py`](file:///engine/services/audit_logger.py), paired with SQLite head-hash anchors in `data/izvor.db`.

```mermaid
graph LR
    subgraph Audit Chaining, HMAC Keyring & SQLite Anchoring
        EntryN1["Audit Entry #N-1"] --> HashN1["Hash: HMAC-SHA256(key[key_id], prev_hash + canonical_n-1)"]
        HashN1 --> EntryN["Audit Entry #N (includes prev_hash, key_id)"]
        Keyring["AUDIT_HMAC_KEYS_JSON / AUDIT_HMAC_ACTIVE_KEY_ID"] --> HashN["Hash: HMAC-SHA256(key[key_id], HashN-1 + canonical_n)"]
        EntryN --> HashN
        HashN --> AnchorDB[("SQLite audit_anchors (month, seq, entry_hash)")]
    end
```

Every audit file is organized monthly (e.g., `data/audit/audit_2026_10.jsonl`). Each line is a self-contained JSON object bound cryptographically to its predecessor, optionally tagged with `key_id` for seamless key rotation, and the latest `(month, seq, entry_hash)` is anchored in the SQLite `audit_anchors` table.

---

## ⛓️ Hash Chaining, HMAC Keyring Rotation & SQLite Anchors

### 1. Sequential Hash Binding
Each record contains:
- `seq`: Monotonically increasing 64-bit integer sequence number.
- `prev_hash`: The cryptographic hash of the immediately preceding log line. For the first record of a log file, `prev_hash` is initialized with a known genesis seed (`"0" * 64`).
- `key_id`: Identifier of the active HMAC key used when signing the record (when a keyring or custom `SystemSettings.audit_hmac_key_id` is active).
- `entry_hash`: Calculated via HMAC-SHA256 when an HMAC key is configured (or SHA-256 otherwise):
  $$\text{entry\_hash} = \text{HMAC-SHA256}_{K_{\text{key\_id}}}(\text{prev\_hash} + \text{canonical\_json\_payload})$$

### 2. HMAC Key Continuity & Zero-Downtime Keyring Rotation
Izvor supports both single-key (`AUDIT_HMAC_SECRET`) and multi-key rotation via `AUDIT_HMAC_KEYS_JSON` + `AUDIT_HMAC_ACTIVE_KEY_ID` (with fallback to `SystemSettings.audit_hmac_key_id`):
- New audit records are signed with the active `(key_id, secret)` from `get_active_audit_hmac_key()` and record `key_id` in the canonical JSON payload.
- During verification, `get_hmac_key_for_id(record.key_id)` resolves the historical key that signed that record and verifies signatures in constant time via `hmac.compare_digest()`.
- Retaining older keys alongside the active key in `AUDIT_HMAC_KEYS_JSON` allows clean key rotation **without** invalidating historical audit logs.

### 3. SQLite Head-Hash Anchoring (`audit_anchors`)
A classic weakness of file-only hash chains is **tail truncation** (where an attacker deletes the last $K$ lines of a JSONL file or deletes the entire file so the remaining prefix still looks internally valid).
- Every write to `audit_YYYY_MM.jsonl` updates the `audit_anchors` table in SQLite (`data/izvor.db`) with `(month, seq, entry_hash, anchored_at)`.
- During integrity verification, `verify_audit_logs()` compares the JSONL file's final `(seq, entry_hash)` against the SQLite anchor. If the file has fewer records than `audit_anchors.seq`, lacks a matching `entry_hash`, or is missing on disk while an anchor exists, verification immediately flags a critical **Tail Truncation / Anchor Mismatch** violation.
- **Safe Closed-Month Anchor Compaction**: When `retention_audit_anchors_compact_enabled` is `true` (default), `prune_expired_records()` in [`services/retention_service.py`](file:///engine/services/retention_service.py) compacts intermediate rows for closed past months while always preserving the terminal `MAX(seq)` anchor row for every existing on-disk `audit_YYYY_MM.jsonl` file, skipping the active UTC month, and removing orphaned anchor rows whose JSONL files no longer exist.

---

## 🛡️ Continuous Integrity Validation Guards

To detect tampering, file truncation, or key discrepancies immediately, Izvor runs two validation layers:

### 1. Startup Verification
During server boot in [`main.py`](file:///main.py), [`AppIntegrityValidator.check_audit_log_integrity()`](file:///engine/orchestration/integrity_checks.py) scans all historical and active audit files:
- Verifies line-by-line hash continuity and sequence monotonicity.
- Verifies `audit_anchors` head-hash consistency against SQLite.
- Confirms HMAC keyring / secret continuity across all recorded `key_id` values via constant-time `hmac.compare_digest()`.
- If corruption, truncation, or key mismatches are discovered, startup raises a critical alert and notifies all configured Slack administrators immediately.

### 2. Scheduled Background Guard (`periodic_audit_integrity_guard`)
- Configured in [`main.py`](file:///main.py).
- Runs periodically (default: **12 hours** / `43200.0` seconds, dynamically loaded from `audit_integrity_check_interval_seconds` in `system_settings.json`).
- Validates that logs written during runtime remain unaltered and match SQLite `audit_anchors`.

---

## 🚨 Emergency Alerts on Integrity Breaches

If any cryptographic, anchor, or hash continuity violation occurs, [`send_admin_audit_alert()`](file:///engine/services/audit_logger.py) formats and dispatches a high-priority incident notice.

### 1. Multi-Admin Alert Routing & Delivery Hierarchy
Audit alerts follow a strict three-tier routing strategy:
1. **Primary Delivery (Direct Message to All Configured Slack Admins)**:
   - Izvor iterates through **all** administrator IDs configured in `SLACK_ADMIN_USER_IDS`, opens a DM channel (`open_dm_channel(admin_id)`), and delivers the security alert to every administrator.
2. **Fallback Delivery (`SLACK_TEST_CHANNEL_ID` / `#bot-testing`)**:
   - If `SLACK_ADMIN_USER_IDS` is unset or empty, or if all DM deliveries fail, the alert automatically falls back to `SLACK_TEST_CHANNEL_ID` (`#bot-testing`) so that breach notices are never lost.
3. **Application Logs & Admin API**:
   - Every breach triggers a `logger.critical()` entry and surfaces in `GET /api/audit/status`.

### 2. Exact Slack Message Layout
Alerts sent to Slack are posted by the `Security Audit Guard` persona and follow this exact structure:

```text
🚨 *[SECURITY CRITICAL] Periodic Audit Guard Integrity Breach*
Periodic scan identified corruption or tampering in the active audit trail.
• *Violations:* `Hash mismatch at sequence #142 in audit_2026_10.jsonl`
• *Files:* `data/audit/audit_2026_10.jsonl`
👉 _Action Required: Run `python -m services.audit_logger --verify` for forensic diagnostics._
```

### 3. Differentiating Production Alerts vs. Test Payloads
Use the following matrix to identify real production breach alerts and critical security audit events:

| Alert Category | Title / Identifier | Payload Characteristics | Origin | Action Required |
|:---|:---|:---|:---|:---|
| **Production Breach** | `Periodic Audit Guard Integrity Breach` | Contains verified `Violations` and corrupted `Files` paths | [`main.py`](file:///main.py) (`periodic_audit_integrity_guard`) | **CRITICAL**: Immediate forensic inspection and snapshot comparison. |
| **Prompt / Tool Guardrail** | `PROMPT_INJECTION_BLOCKED` / `INDIRECT_INJECTION_DETECTED` / `GUARDRAIL_FAIL_CLOSED_BLOCKED` | Heuristic pre-filter match, sliding-window embedding match, indirect tool output neutralization, or fail-closed block | [`services/security/guardrail.py`](file:///engine/services/security/guardrail.py) | **HIGH**: Review blocked prompt, tool source, and threat actor profile. |
| **Privilege & IDOR Alerts** | `UNAUTHORIZED_DIRECTIVE_ATTEMPT` / `ADMIN_ACTION_DENIED` / `UNAUTHORIZED_SCRIBE_DOWNLOAD` | Non-admin user attempted `/grant_skill`, `/revoke_constraint`, `/forget_thread`, `=off`, App Home admin control, or mismatched channel Scribe download | [`directive_handlers.py`](file:///engine/orchestration/stages/directive_handlers.py), [`interactivity.py`](file:///engine/adapters/slack/interactivity.py), [`scribe_actions.py`](file:///engine/adapters/slack/actions/scribe_actions.py) | **MEDIUM/HIGH**: Review user activity and channel payload integrity. |
| **GDPR Thread Erasure** | `GDPR_THREAD_FORGOTTEN` / `THREAD_DATA_PURGED` | Thread state, flat histories, notes, and downloads purged via `/forget_thread` or `DELETE /api/threads/{channel_id}/{thread_ts}` | [`engine/api/router.py`](file:///engine/api/router.py), [`retention_service.py`](file:///engine/services/retention_service.py) | **INFO/WARNING**: Expected compliance erasure confirmation. |
| **Production Breach** | `Startup Audit Integrity Alert: Secret Mismatch (...)` | Contains `Status`, `Warning`, `Last File`, `Last Seq`, and `Errors` | [`main.py`](file:///main.py) (startup boot check) | **CRITICAL**: Confirm `AUDIT_HMAC_KEYS_JSON` / `AUDIT_HMAC_SECRET` keyring configuration or restore audit logs. |
| **Production Warning** | `AUDIT_HMAC_SECRET Discrepancy (...)` | Contains `Status`, `Warning`, `Last File`, and `Last Seq` | [`main.py`](file:///main.py) (startup boot check) | **HIGH**: Key mismatch detected between historic records (`key_id`) and active keyring/environment. |
| **Production Breach** | `Startup Audit Log Integrity Violation` | Contains `Violations` list and `Files` | [`main.py`](file:///main.py) (startup boot check) | **CRITICAL**: Immediate disk inspection and chain repair. |
| **Synthetic Test** | `Test Security Breach` | Message: `This is an automated test alert in English.` Details: `line: 42`, `file: audit_2026_10.jsonl` | [`tests/unit/test_audit_logger.py`](file:///tests/unit/test_audit_logger.py) | None (test fixture isolated via `set_audit_hmac_keyring_override` / `configure_paths` / `IZVOR_*`). |
| **E2E Scenario** | `E2E Test Runner` scenarios | Contains scenario markers like `E2E-101`, test persona names, and simulation tags | [`tests/e2e/test_e2e_suite.py`](file:///tests/e2e/test_e2e_suite.py) | None (automated E2E verification traffic in `#bot-testing`). |

---

## 🔍 Forensic CLI Verification & Diagnostics

When an alert fires or during compliance auditing, administrators can run the built-in audit CLI directly:

### 1. Integrity Verification (`--verify`)
Scans all monthly JSONL files in the audit directory, checking hash chaining, monotonically increasing sequence numbers, SQLite `audit_anchors`, and HMAC keyring signatures (`key_id` lookup + constant-time `hmac.compare_digest`):

```bash
python -m services.audit_logger --verify
```

**Expected Healthy Output:**
```text
=== Izvor Audit Log Integrity Verification ===
Scanned Files:    audit_2026_10.jsonl
Total Records:    1520
Last Seq Number:  1520
Last Entry Hash:  a8b79f2c140938...
HMAC Key Status:  MATCH (keyring / AUDIT_HMAC_SECRET valid across all records)

[VERIFIED] All cryptographic audit chains are fully intact. No tampering detected.
```

To restrict verification to a specific month:
```bash
python -m services.audit_logger --verify --month 2026-10
```

### 2. Secret Key Continuity Check (`--check-secret`)
```bash
python -m services.audit_logger --check-secret
```

### 3. Log Statistics Overview (`--stats`)
```bash
python -m services.audit_logger --stats
```

### 4. Create Archival Snapshot (`--snapshot`)
```bash
python -m services.audit_logger --snapshot
```

---

## ⚡ Non-Blocking Async Logging & Amplification Defense

### 1. Non-Blocking `log_audit_event_async` & Tail Caching
- **Async Offloading**: Hot-path async callers can use `log_audit_event_async()` to offload disk writes and SQLite anchor updates to a background thread without blocking the asyncio event loop.
- **Tail Caching & Reverse Seek**: `AuditLogger` caches `(seq, entry_hash)` in memory (`_cached_last_state`) for $O(1)$ sequential appends and uses reverse byte seek on cold boot.

### 2. Amplification Defense & Sliding-Window Burst Suppression
- Configured via `security_burst_limits` in `system_settings.json` and enforced in [`core/slack_security.py`](file:///engine/core/slack_security.py).
- Up to 5 discrete `AUTH_FAILURE` events are logged per client IP within each 60-second window (and 25 globally); subsequent failures are aggregated into a consolidated summary event upon window expiration.
