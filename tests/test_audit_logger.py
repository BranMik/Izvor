"""
Comprehensive test suite for the non-repudiable audit logging system in Izvor.
"""

import datetime
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from core.integrity_checks import AppIntegrityValidator
from orchestration.system_settings import is_path_write_blocked
from services.audit_logger import (
    GENESIS_HASH,
    AuditLogger,
    canonical_serialize,
    check_hmac_secret_continuity,
    compute_entry_hash,
    log_audit_event,
    send_admin_audit_alert,
    verify_audit_log_integrity,
)
from services.mcp_tools import write_document


@pytest.fixture
def temp_audit_dir(tmp_path: Path):
    """Provides an isolated temporary audit directory for testing."""
    test_dir = tmp_path / "audit"
    test_dir.mkdir(parents=True, exist_ok=True)
    logger = AuditLogger(audit_dir=test_dir)
    logger.invalidate_cache()
    return test_dir, logger


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
    assert is_path_write_blocked("audit/audit_2026_10.jsonl") is True
    assert is_path_write_blocked("audit/backups/snapshot.jsonl") is True
    assert is_path_write_blocked("system_resources") is True
    assert is_path_write_blocked("system_resources/audit/tamper.txt") is True

    # write_document should reject writing into audit or system_resources
    res_str = write_document("audit/tamper.txt", "malicious_content")
    res_dict = json.loads(res_str)
    assert "error" in res_dict
    assert "Permission denied" in res_dict["error"]

    res_sys = write_document("system_resources/audit/tamper.txt", "malicious_content")
    res_sys_dict = json.loads(res_sys)
    assert "error" in res_sys_dict
    assert "Permission denied" in res_sys_dict["error"]


def test_send_admin_audit_alert_mock(monkeypatch):
    """Verifies send_admin_audit_alert handles mock and dispatch cleanly."""
    import asyncio

    monkeypatch.setenv("ADMIN_SLACK_USER_ID", "U_ADMIN_TEST")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-your-bot-user-oauth-token")

    success = asyncio.run(
        send_admin_audit_alert(
            title="Test Security Breach",
            message="This is an automated test alert in English.",
            details={"line": 42, "file": "audit_2026_10.jsonl"},
        )
    )
    assert success is True


def test_hmac_continuity_empty_records(tmp_path: Path):
    """Verifies that an empty audit directory reports NO_RECORDS and passes."""
    empty_dir = tmp_path / "empty_audit"
    empty_dir.mkdir()
    res = check_hmac_secret_continuity(audit_dir=empty_dir, secret_key=b"test-secret")
    assert res["has_records"] is False
    assert res["matches"] is True
    assert res["status"] == "NO_RECORDS"


def test_hmac_continuity_key_matches(temp_audit_dir, monkeypatch):
    """Verifies that matching HMAC secret reports MATCH."""
    test_dir, logger = temp_audit_dir
    monkeypatch.setattr("services.audit_logger.AUDIT_HMAC_SECRET", b"secret-v1")

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


def test_hmac_continuity_key_changed(temp_audit_dir, monkeypatch):
    """Verifies that changing the HMAC secret key is detected as SECRET_CHANGED."""
    test_dir, logger = temp_audit_dir
    monkeypatch.setattr("services.audit_logger.AUDIT_HMAC_SECRET", b"secret-original")

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


def test_hmac_continuity_secret_added_to_plain_chain(temp_audit_dir, monkeypatch):
    """Verifies that adding a secret when previous logs were plain SHA-256 reports SECRET_ADDED."""
    test_dir, logger = temp_audit_dir
    # Write record without HMAC secret
    monkeypatch.setattr("services.audit_logger.AUDIT_HMAC_SECRET", None)

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


def test_hmac_continuity_secret_removed(temp_audit_dir, monkeypatch):
    """Verifies that removing the secret when previous logs were HMAC signed reports SECRET_REMOVED."""
    test_dir, logger = temp_audit_dir
    # Write record with HMAC secret
    monkeypatch.setattr("services.audit_logger.AUDIT_HMAC_SECRET", b"original-secret")

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

