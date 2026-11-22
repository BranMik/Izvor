"""
Comprehensive test suite for the non-repudiable audit logging system in Izvor.
"""

import datetime
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestration.integrity_checks import AppIntegrityValidator
from orchestration.system_settings import is_path_write_blocked
from services.audit_logger import (
    GENESIS_HASH,
    AuditLogger,
    audit_logger,
    check_hmac_secret_continuity,
    send_admin_audit_alert,
    verify_audit_log_integrity,
)
from core.domain import SYSTEM_PRINCIPAL
from services.local_tools import write_document


@pytest.fixture
def temp_audit_dir(tmp_path: Path):
    """Provides an isolated temporary audit directory for testing."""
    test_dir = tmp_path / "audit"
    test_dir.mkdir(parents=True, exist_ok=True)
    audit_logger.configure_paths(audit_dir=test_dir)
    try:
        yield test_dir, audit_logger
    finally:
        audit_logger.reset_paths()


def test_sequential_hash_chaining(temp_audit_dir):
    """Verifies that audit records form a strict SHA-256 cryptographic chain."""
    test_dir, logger = temp_audit_dir

    rec1 = logger.log_event(
        event_type="ADMIN_CONFIG_CHANGE",
        actor={"type": "slack_admin", "id": "U001"},
        action="toggle_circuit_breaker",
        target="circuit_breaker.enabled",
        details={"enabled": True},
    )
    rec2 = logger.log_event(
        event_type="ADMIN_CONFIG_CHANGE",
        actor={"type": "slack_admin", "id": "U001"},
        action="set_circuit_breaker_threshold",
        target="circuit_breaker.threshold",
        details={"threshold": 25.0},
    )
    rec3 = logger.log_event(
        event_type="TOOL_EXECUTE_FILE_WRITE",
        actor={"type": "agent", "id": "agent-10001"},
        action="write_document",
        target="agents_resources/report.txt",
        details={"bytes_written": 128},
    )

    assert rec1["seq"] == 1
    assert rec1["prev_hash"] == GENESIS_HASH
    assert rec1["entry_hash"] != ""

    assert rec2["seq"] == 2
    assert rec2["prev_hash"] == rec1["entry_hash"]

    assert rec3["seq"] == 3
    assert rec3["prev_hash"] == rec2["entry_hash"]

    # Verify integrity passes
    result = verify_audit_log_integrity(audit_dir=test_dir)
    assert result.valid is True
    assert result.total_records == 3
    assert result.last_verified_seq == 3
    assert len(result.errors) == 0


def test_monthly_partitioning_and_chain_continuity(temp_audit_dir):
    """Verifies that chain continuity is strictly preserved across monthly files."""
    test_dir, logger = temp_audit_dir

    dt_october = datetime.datetime(2026, 10, 15, 12, 0, 0, tzinfo=datetime.timezone.utc)
    dt_november = datetime.datetime(2026, 11, 1, 10, 0, 0, tzinfo=datetime.timezone.utc)

    rec_oct = logger.log_event(
        event_type="ADMIN_CONFIG_CHANGE",
        actor={"type": "slack_admin", "id": "U001"},
        action="october_action",
        target="sys",
        timestamp=dt_october,
    )
    rec_nov = logger.log_event(
        event_type="ADMIN_CONFIG_CHANGE",
        actor={"type": "slack_admin", "id": "U001"},
        action="november_action",
        target="sys",
        timestamp=dt_november,
    )

    oct_file = test_dir / "audit_2026_10.jsonl"
    nov_file = test_dir / "audit_2026_11.jsonl"

    assert oct_file.exists()
    assert nov_file.exists()

    assert rec_nov["seq"] == 2
    assert rec_nov["prev_hash"] == rec_oct["entry_hash"]

    # Full archive verification across files
    result = verify_audit_log_integrity(audit_dir=test_dir)
    assert result.valid is True
    assert result.total_records == 2
    assert len(result.scanned_files) == 2


def test_tamper_detection_content_modification(temp_audit_dir):
    """Verifies that editing a field in an audit log line triggers a content tampering error."""
    test_dir, logger = temp_audit_dir

    logger.log_event(
        event_type="ADMIN_CONFIG_CHANGE",
        actor={"type": "slack_admin", "id": "U001"},
        action="set_circuit_breaker_threshold",
        target="circuit_breaker.threshold",
        details={"threshold": 10.0},
    )
    logger.log_event(
        event_type="ADMIN_CONFIG_CHANGE",
        actor={"type": "slack_admin", "id": "U001"},
        action="set_circuit_breaker_threshold",
        target="circuit_breaker.threshold",
        details={"threshold": 20.0},
    )

    month_file = logger.get_month_file_path()
    lines = month_file.read_text(encoding="utf-8").splitlines()

    # Tamper with the first line: change 10.0 to 999.0 without updating entry_hash
    first_record = json.loads(lines[0])
    first_record["details"]["threshold"] = 999.0
    lines[0] = json.dumps(first_record)
    month_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = verify_audit_log_integrity(audit_dir=test_dir)
    assert result.valid is False
    assert any("Content tampered" in err for err in result.errors)


def test_tamper_detection_line_deletion(temp_audit_dir):
    """Verifies that deleting an audit log record triggers sequence/chain mismatch."""
    test_dir, logger = temp_audit_dir

    for i in range(1, 4):
        logger.log_event(
            event_type="ADMIN_CONFIG_CHANGE",
            actor={"type": "slack_admin", "id": "U001"},
            action=f"action_{i}",
            target="sys",
        )

    month_file = logger.get_month_file_path()
    lines = month_file.read_text(encoding="utf-8").splitlines()

    # Remove the second line (seq: 2)
    tampered_lines = [lines[0], lines[2]]
    month_file.write_text("\n".join(tampered_lines) + "\n", encoding="utf-8")

    result = verify_audit_log_integrity(audit_dir=test_dir)
    assert result.valid is False
    assert any("Sequence mismatch" in err or "Chain broken" in err for err in result.errors)


def test_snapshot_backup_and_manifest(temp_audit_dir):
    """Verifies creation of archival snapshots and manifest.sha256 entries."""
    test_dir, logger = temp_audit_dir

    logger.log_event(
        event_type="ADMIN_CONFIG_CHANGE",
        actor={"type": "slack_admin", "id": "U001"},
        action="create_backup_target",
        target="sys",
    )

    snapshot_path = logger.create_snapshot()
    assert snapshot_path is not None
    assert snapshot_path.exists()
    assert snapshot_path.parent == test_dir / "backups"

    manifest_file = test_dir / "manifest.sha256"
    assert manifest_file.exists()
    manifest_content = manifest_file.read_text(encoding="utf-8")
    assert snapshot_path.name in manifest_content


def test_app_integrity_validator_integration(temp_audit_dir):
    """Verifies that AppIntegrityValidator.check_audit_log_integrity validates correctly."""
    test_dir, logger = temp_audit_dir

    logger.log_event(
        event_type="SECURITY_POLICY_VIOLATION",
        actor={"type": "agent", "id": "agent-10002"},
        action="execute_tool",
        target="tool:forbidden_tool",
        details={"reason": "Permission denied"},
        severity="CRITICAL",
        status="BLOCKED",
    )

    status = AppIntegrityValidator.check_audit_log_integrity(audit_dir=test_dir)
    assert status["valid"] is True
    assert status["total_records"] == 1
    assert status["last_verified_seq"] == 1
    assert len(status["errors"]) == 0
    assert "hmac_continuity" in status
    assert status["hmac_continuity"]["matches"] is True


def test_sandbox_audit_write_protection():
    """Verifies that system_settings blocks tool writes targeting the audit directory."""
    # is_path_write_blocked should return True for any audit path or system_resources
    assert is_path_write_blocked("audit") is True
    assert is_path_write_blocked("data/audit") is True
    assert is_path_write_blocked("audit/audit_2026_10.jsonl") is True
    assert is_path_write_blocked("data/audit/audit_2026_10.jsonl") is True
    assert is_path_write_blocked("audit/backups/snapshot.jsonl") is True
    assert is_path_write_blocked("data/audit/backups/snapshot.jsonl") is True
    assert is_path_write_blocked("system_resources") is True
    assert is_path_write_blocked("system_resources/audit/tamper.txt") is True

    # write_document sandbox prevents escaping agents_resources to audit or system_resources
    res_str = write_document("../audit/tamper.txt", "malicious_content", principal=SYSTEM_PRINCIPAL)
    res_dict = json.loads(res_str)
    assert "error" in res_dict
    assert "Access denied" in res_dict["error"] or "Permission denied" in res_dict["error"]

    res_sys = write_document("../system_resources/audit/tamper.txt", "malicious_content", principal=SYSTEM_PRINCIPAL)
    res_sys_dict = json.loads(res_sys)
    assert "error" in res_sys_dict
    assert "Access denied" in res_sys_dict["error"] or "Permission denied" in res_sys_dict["error"]


def test_send_admin_audit_alert_mock():
    """Verifies send_admin_audit_alert handles mock and dispatch cleanly."""
    import asyncio
    from core.config import set_admin_slack_user_ids_override
    from services.slack.client_core import configure_token, reset_token

    set_admin_slack_user_ids_override("U_ADMIN_TEST")
    configure_token("xoxb-your-bot-user-oauth-token")

    try:
        success = asyncio.run(
            send_admin_audit_alert(
                title="Test Security Breach",
                message="This is an automated test alert in English.",
                details={"line": 42, "file": "audit_2026_10.jsonl"},
            )
        )
        assert success is True
    finally:
        set_admin_slack_user_ids_override(None)
        reset_token()


def test_hmac_continuity_empty_records(tmp_path: Path):
    """Verifies that an empty audit directory reports NO_RECORDS and passes."""
    empty_dir = tmp_path / "empty_audit"
    empty_dir.mkdir()
    res = check_hmac_secret_continuity(audit_dir=empty_dir, secret_key=b"test-secret")
    assert res["has_records"] is False
    assert res["matches"] is True
    assert res["status"] == "NO_RECORDS"


def test_hmac_continuity_key_matches(temp_audit_dir):
    """Verifies that matching HMAC secret reports MATCH."""
    from services.audit_logger import set_audit_hmac_secret_override

    test_dir, logger = temp_audit_dir
    set_audit_hmac_secret_override(b"secret-v1")

    try:
        logger.log_event(
            event_type="ADMIN_CONFIG_CHANGE",
            actor={"type": "admin", "id": "admin"},
            action="update_settings",
            target="sys",
        )

        res = check_hmac_secret_continuity(audit_dir=test_dir, secret_key=b"secret-v1")
        assert res["has_records"] is True
        assert res["matches"] is True
        assert res["status"] == "MATCH"
        assert res["last_seq"] == 1
    finally:
        set_audit_hmac_secret_override(None)


def test_hmac_continuity_key_changed(temp_audit_dir):
    """Verifies that changing the HMAC secret key is detected as SECRET_CHANGED."""
    from services.audit_logger import set_audit_hmac_secret_override

    test_dir, logger = temp_audit_dir
    set_audit_hmac_secret_override(b"secret-original")

    try:
        logger.log_event(
            event_type="ADMIN_CONFIG_CHANGE",
            actor={"type": "admin", "id": "admin"},
            action="update_settings",
            target="sys",
        )

        # Validate with a different key
        res = check_hmac_secret_continuity(audit_dir=test_dir, secret_key=b"secret-changed-new")
        assert res["has_records"] is True
        assert res["matches"] is False
        assert res["status"] == "SECRET_CHANGED"
        assert "does not match the HMAC key" in res["message"]
    finally:
        set_audit_hmac_secret_override(None)


def test_hmac_continuity_secret_added_to_plain_chain(temp_audit_dir):
    """Verifies that adding a secret when previous logs were plain SHA-256 reports SECRET_ADDED."""
    from services.audit_logger import set_audit_hmac_secret_override

    test_dir, logger = temp_audit_dir
    # Write record without HMAC secret
    set_audit_hmac_secret_override(b"")

    try:
        logger.log_event(
            event_type="ADMIN_CONFIG_CHANGE",
            actor={"type": "admin", "id": "admin"},
            action="update_settings",
            target="sys",
        )

        # Validate with a newly configured secret
        res = check_hmac_secret_continuity(audit_dir=test_dir, secret_key=b"newly-added-secret")
        assert res["has_records"] is True
        assert res["matches"] is False
        assert res["status"] == "SECRET_ADDED"
        assert "plain SHA-256" in res["message"]
    finally:
        set_audit_hmac_secret_override(None)


def test_hmac_continuity_secret_removed(temp_audit_dir):
    """Verifies that removing the secret when previous logs were HMAC signed reports SECRET_REMOVED."""
    from services.audit_logger import set_audit_hmac_secret_override

    test_dir, logger = temp_audit_dir
    # Write record with HMAC secret
    set_audit_hmac_secret_override(b"original-secret")

    try:
        logger.log_event(
            event_type="ADMIN_CONFIG_CHANGE",
            actor={"type": "admin", "id": "admin"},
            action="update_settings",
            target="sys",
        )

        # Validate with empty/None secret
        res = check_hmac_secret_continuity(audit_dir=test_dir, secret_key=b"")
        assert res["has_records"] is True
        assert res["matches"] is False
        assert res["status"] == "SECRET_REMOVED"
        assert "AUDIT_HMAC_SECRET is not set" in res["message"]
    finally:
        set_audit_hmac_secret_override(None)


def test_audit_logger_last_record_exceeding_8kb_preserves_chain(temp_audit_dir):
    """Verifies that audit records exceeding 8KB do not corrupt reverse seeking or reset the hash chain."""
    test_dir, logger = temp_audit_dir

    large_content = "X" * 16000  # 16KB record
    rec1 = logger.log_event(
        event_type="LARGE_RECORD_EVENT",
        actor={"type": "system", "id": "sys"},
        action="large_dump",
        target="audit_system",
        details={"blob": large_content},
    )
    assert rec1["seq"] == 1
    assert len(json.dumps(rec1)) > 16000

    # Invalidate in-memory cache to force reverse seek from disk
    logger.invalidate_cache()
    last_seq, last_hash = logger.get_last_state()

    assert last_seq == 1
    assert last_hash == rec1["entry_hash"]
    assert last_hash != GENESIS_HASH

    # Subsequent record chains correctly
    rec2 = logger.log_event(
        event_type="FOLLOW_UP_EVENT",
        actor={"type": "system", "id": "sys"},
        action="next_step",
        target="audit_system",
        details={"status": "ok"},
    )
    assert rec2["seq"] == 2
    assert rec2["prev_hash"] == rec1["entry_hash"]

    # Verify overall integrity
    integrity = verify_audit_log_integrity(audit_dir=test_dir)
    assert integrity.valid is True
    assert integrity.total_records == 2


@pytest.mark.anyio
async def test_audit_logger_log_event_synchronous_return_in_async_context(temp_audit_dir):
    """Verifies that log_event synchronously writes and returns the record dict when an event loop is active."""
    test_dir, logger = temp_audit_dir

    rec = logger.log_event(
        event_type="ASYNC_CALL_EVENT",
        actor={"type": "user", "id": "U123"},
        action="async_action",
        target="test",
        details={"async": True},
    )
    assert isinstance(rec, dict)
    assert rec.get("seq") == 1
    assert rec.get("event_type") == "ASYNC_CALL_EVENT"
    assert rec.get("entry_hash") is not None


def test_hmac_secret_optional_when_unset(caplog):
    """Verifies that get_audit_hmac_secret returns b'' and validates without crashing when unset."""
    from services.audit_logger import get_audit_hmac_secret, set_audit_hmac_secret_override, validate_audit_configuration

    set_audit_hmac_secret_override(b"")
    try:
        secret = get_audit_hmac_secret()
        assert secret == b""

        with caplog.at_level("WARNING"):
            validate_audit_configuration()
        assert "AUDIT_HMAC_SECRET is not configured" in caplog.text
    finally:
        set_audit_hmac_secret_override(None)
    assert "AUDIT_HMAC_SECRET is not configured" in caplog.text


def test_audit_anchor_persisted_and_tail_truncation_detected(temp_audit_dir):
    """Verifies that anchors are stored in SQLite and detect tail truncation when log entries are removed."""
    from core.database import db_manager

    test_dir, logger = temp_audit_dir

    # Log 10 events so seq 10 triggers an anchor
    for i in range(1, 11):
        logger.log_event(
            event_type="BATCH_TEST_EVENT",
            actor={"type": "system", "id": "test_actor"},
            action=f"action_{i}",
            target=f"target_{i}",
            details={"index": i},
            severity="INFO",
        )

    # Confirm anchor exists in SQLite for seq 10
    with db_manager.get_connection() as conn:
        row = conn.execute("SELECT seq, entry_hash FROM audit_anchors WHERE seq = 10").fetchone()
        assert row is not None
        assert row["seq"] == 10
        assert row["entry_hash"] is not None

    # Integrity verification passes before truncation
    res_clean = verify_audit_log_integrity(audit_dir=test_dir, check_anchors=True)
    assert res_clean.valid is True
    assert res_clean.total_records == 10

    # Simulate tail truncation: truncate audit file by removing the last 2 lines (leaving seq 1..8)
    files = list(test_dir.glob("audit_*.jsonl"))
    assert len(files) == 1
    audit_file = files[0]
    with open(audit_file, "r", encoding="utf-8") as f:
        lines = [line for line in f if line.strip()]

    assert len(lines) == 10
    truncated_lines = lines[:8]
    with open(audit_file, "w", encoding="utf-8") as f:
        f.writelines(truncated_lines)

    # Verification with check_anchors=False only sees intact chain for 8 records
    res_no_anchor = verify_audit_log_integrity(audit_dir=test_dir, check_anchors=False)
    assert res_no_anchor.valid is True
    assert res_no_anchor.total_records == 8

    # Verification with check_anchors=True MUST flag tail truncation!
    res_truncated = verify_audit_log_integrity(audit_dir=test_dir, check_anchors=True)
    assert res_truncated.valid is False
    assert any("Tail truncation or anchor mismatch detected" in err for err in res_truncated.errors)


def test_log_audit_event_async(temp_audit_dir):
    """Verifies log_audit_event_async asynchronously writes records via thread pool."""
    import asyncio
    from services.audit_logger import log_audit_event_async

    test_dir, logger_inst = temp_audit_dir

    async def run_async_test():
        rec = await log_audit_event_async(
            event_type="ASYNC_EVENT",
            actor={"type": "system", "id": "async_sys"},
            action="async_write",
            target="target_res",
            details={"foo": "bar"},
            severity="CRITICAL",
        )
        assert rec["seq"] == 1
        assert rec["severity"] == "CRITICAL"
        assert rec["event_type"] == "ASYNC_EVENT"

    asyncio.run(run_async_test())


@pytest.mark.anyio
async def test_send_admin_audit_alert_severity_tiers():
    """Verifies that send_admin_audit_alert formats CRITICAL, WARNING, and INFO tiers correctly."""
    from core.config import configure_token, reset_token
    from services.audit_logger import send_admin_audit_alert

    configure_token("xoxb-real-token-test")
    try:
        with patch("core.config.get_admin_slack_user_ids", return_value={"U_ADMIN"}), \
             patch("services.slack.open_dm_channel", new_callable=AsyncMock, return_value="D_ADMIN"), \
             patch("services.slack.post_agent_response", new_callable=AsyncMock, return_value={"ok": True}) as mock_post:

            # Tier 1: CRITICAL
            await send_admin_audit_alert(
                title="TAMPER_DETECTED",
                message="Hash chain broken",
                details={"seq": 10},
                severity="CRITICAL",
            )
            crit_text = mock_post.call_args[1]["text"]
            assert "🚨 *[SECURITY CRITICAL] TAMPER_DETECTED*" in crit_text
            assert "👉 _Action Required: Run `python -m services.audit_logger --verify` for forensic diagnostics._" in crit_text
            assert "• *seq:* `10`" in crit_text

            # Tier 2: WARNING (Option A validation action)
            mock_post.reset_mock()
            await send_admin_audit_alert(
                title="ADMIN_CONFIG_CHANGE",
                message="Skill filter disabled",
                details={"enabled": False},
                severity="WARNING",
            )
            warn_text = mock_post.call_args[1]["text"]
            assert "⚠️ *[SECURITY WARNING] ADMIN_CONFIG_CHANGE*" in warn_text
            assert "🔍 _Validation Action: Run `python -m services.audit_logger --verify` to verify audit trail integrity._" in warn_text
            assert "• *enabled:* `False`" in warn_text

            # Tier 3: INFO
            mock_post.reset_mock()
            await send_admin_audit_alert(
                title="ADMIN_CONFIG_CHANGE",
                message="Skill filter enabled",
                details={"enabled": True},
                severity="INFO",
            )
            info_text = mock_post.call_args[1]["text"]
            assert "ℹ️ *[ADMIN NOTICE] ADMIN_CONFIG_CHANGE*" in info_text
            assert "📋 _Audit Trail: Event signed and recorded._" in info_text
    finally:
        reset_token()


def test_audit_logger_dispatch_severity_filtering(temp_audit_dir):
    """Verifies AuditLogger.log_event dispatches alerts for CRITICAL and WARNING, and suppresses for INFO."""
    test_dir, logger_inst = temp_audit_dir

    with patch("services.audit_logger._dispatch_security_alert") as mock_dispatch, \
         patch("services.audit_logger._persist_audit_anchor") as mock_anchor:

        # 1. Log INFO: should NOT trigger alert dispatch, should NOT anchor (seq=1)
        rec_info = logger_inst.log_event(
            event_type="ADMIN_CONFIG_CHANGE",
            actor={"type": "slack_admin", "id": "U_ADMIN"},
            action="toggle_skill_filter",
            target="skill_routing",
            severity="INFO",
        )
        assert rec_info["severity"] == "INFO"
        mock_dispatch.assert_not_called()
        mock_anchor.assert_not_called()

        # 2. Log WARNING: MUST trigger alert dispatch and MUST persist anchor
        rec_warn = logger_inst.log_event(
            event_type="ADMIN_CONFIG_CHANGE",
            actor={"type": "slack_admin", "id": "U_ADMIN"},
            action="toggle_skill_filter",
            target="skill_routing",
            severity="WARNING",
        )
        assert rec_warn["severity"] == "WARNING"
        assert mock_dispatch.call_count == 1
        assert mock_anchor.call_count == 1

        # 3. Log CRITICAL: MUST trigger alert dispatch and MUST persist anchor
        mock_dispatch.reset_mock()
        mock_anchor.reset_mock()
        rec_crit = logger_inst.log_event(
            event_type="TAMPER_DETECTED",
            actor={"type": "system", "id": "guard"},
            action="verify_trail",
            target="audit_log",
            severity="CRITICAL",
        )
        assert rec_crit["severity"] == "CRITICAL"
        assert mock_dispatch.call_count == 1
        assert mock_anchor.call_count == 1


def test_create_snapshot_writes_clean_manifest_without_nul_bytes(temp_audit_dir):
    """TKT-1207 (C12): create_snapshot must use sidecar .manifest.lock and never corrupt manifest.sha256 with NUL bytes."""
    test_dir, logger_inst = temp_audit_dir

    logger_inst.log_event(
        event_type="ADMIN_CONFIG_CHANGE",
        actor={"type": "slack_admin", "id": "U001"},
        action="snapshot_test",
        target="config",
    )
    snap_path = logger_inst.create_snapshot()
    assert snap_path is not None and snap_path.exists()

    manifest_path = test_dir / "manifest.sha256"
    assert manifest_path.exists()
    raw_bytes = manifest_path.read_bytes()
    assert b"\x00" not in raw_bytes, "Windows file lock must never write NUL bytes into manifest.sha256"
    assert (test_dir / ".manifest.lock").exists()

    lines = manifest_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    parts = lines[0].split("  ")
    assert len(parts) == 2
    assert len(parts[0]) == 64
    assert parts[1] == snap_path.name


def test_cross_process_month_rollover_preserves_chain(temp_audit_dir):
    """TKT-1207 (H15): Month rollover must invalidate cache when another process appended to the prior month."""
    import datetime

    test_dir, logger_inst = temp_audit_dir
    dt_oct = datetime.datetime(2026, 10, 31, 23, 50, tzinfo=datetime.timezone.utc)
    dt_nov = datetime.datetime(2026, 11, 1, 0, 5, tzinfo=datetime.timezone.utc)

    rec1 = logger_inst.log_event(
        event_type="SYSTEM_EVENT",
        actor={"type": "system", "id": "proc_a"},
        action="evt1",
        target="sys",
        timestamp=dt_oct,
    )
    assert rec1["seq"] == 1

    # Simulate an independent second process writing seq 2 to October BEFORE November rollover
    # Create an isolated non-singleton instance state without mutating logger_inst's cache
    other_proc_logger = object.__new__(AuditLogger)
    import threading

    other_proc_logger._write_lock = threading.RLock()
    other_proc_logger._cached_last_state = None
    other_proc_logger._cached_file_size = 0
    other_proc_logger._cached_file_path = None
    other_proc_logger._audit_dir = test_dir
    other_proc_logger.backup_dir = test_dir / "backups"
    other_proc_logger.manifest_file = test_dir / "manifest.sha256"
    other_proc_logger._initialized = True

    rec2 = other_proc_logger.log_event(
        event_type="SYSTEM_EVENT",
        actor={"type": "system", "id": "proc_b"},
        action="evt2",
        target="sys",
        timestamp=dt_oct,
    )
    assert rec2["seq"] == 2

    # First process still holds stale Oct cache; rolling over to Nov must invalidate cache and continue from seq 2
    rec3 = logger_inst.log_event(
        event_type="SYSTEM_EVENT",
        actor={"type": "system", "id": "proc_a"},
        action="evt3",
        target="sys",
        timestamp=dt_nov,
    )
    assert rec3["seq"] == 3
    assert rec3["prev_hash"] == rec2["entry_hash"]

    res = verify_audit_log_integrity(audit_dir=test_dir, check_anchors=False)
    assert res.valid is True
    assert res.total_records == 3


def test_verify_audit_log_integrity_anchor_db_error_fails_closed(tmp_path):
    """TKT-1207 (H15): Anchor verification database unavailability must fail CLOSED (valid=False)."""
    from core.database import db_manager

    audit_dir = tmp_path / "audit_fail_closed"
    audit_dir.mkdir()
    isolated_db = tmp_path / "no_anchors.db"

    db_manager.configure_paths(db_path=isolated_db)
    try:
        logger_inst = AuditLogger(audit_dir=audit_dir)
        logger_inst.invalidate_cache()
        logger_inst.log_event(
            event_type="ADMIN_CONFIG_CHANGE",
            actor={"type": "slack_admin", "id": "U001"},
            action="test_fail_closed",
            target="sys",
        )
        # Drop audit_anchors to force OperationalError during anchor check
        with db_manager.get_connection() as conn:
            conn.execute("DROP TABLE audit_anchors")

        res = verify_audit_log_integrity(audit_dir=audit_dir, check_anchors=True)
        assert res.valid is False
        assert any("Audit anchor verification failed" in err for err in res.errors)
    finally:
        db_manager.reset_paths()




