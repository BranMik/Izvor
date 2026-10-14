"""
Dedicated test suite verifying Phase 3 Hardening & Remediation:
- [S4] Audit tail caching, reverse seek, and AUTH_FAILURE burst suppression.
- [S7] Fail-closed tool execution and ALL_INTERNAL_SKILLS sentinel.
- [B2 & B3] Lossless atomic settings persistence (agent, system, and app settings).
- [S11] Logging credential redaction filter & tool argument unmasking.
"""

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.logging_filters import CredentialRedactionFilter, redact_text
from core.slack_security import _record_auth_failure_audit, verify_slack_signature
from orchestration.agent_settings import AgentSettingsManager
from orchestration.app_settings import AppSettingsManager
from orchestration.stages.execution import _unmask_argument_values
from orchestration.system_settings import SystemSettingsManager
from services.audit_logger import GENESIS_HASH, AuditLogger
from skills import ALL_INTERNAL_SKILLS, skill_registry


# ---------------------------------------------------------------------------
# 1. [S4] Audit Tail Caching & Reverse Seek
# ---------------------------------------------------------------------------

def test_audit_tail_caching_and_reverse_seek(tmp_path: Path):
    test_dir = tmp_path / "audit"
    test_dir.mkdir(parents=True, exist_ok=True)
    audit = AuditLogger(audit_dir=test_dir)
    audit.invalidate_cache()

    # Empty audit directory returns genesis hash
    seq, last_hash = audit.get_last_state()
    assert seq == 0
    assert last_hash == GENESIS_HASH

    # First log event updates cache
    rec1 = audit.log_event(
        event_type="TEST_EVENT_1",
        actor={"type": "system", "id": "test_actor"},
        action="action_1",
        target="target_1",
    )
    assert rec1["seq"] == 1
    assert audit._cached_last_state == (1, rec1["entry_hash"])

    # Second log event uses cached state for previous hash
    rec2 = audit.log_event(
        event_type="TEST_EVENT_2",
        actor={"type": "system", "id": "test_actor"},
        action="action_2",
        target="target_2",
    )
    assert rec2["seq"] == 2
    assert rec2["prev_hash"] == rec1["entry_hash"]
    assert audit._cached_last_state == (2, rec2["entry_hash"])

    # Invalidate cache and verify reverse binary seek reloads correct last state
    audit.invalidate_cache()
    assert audit._cached_last_state is None

    seq_cold, hash_cold = audit.get_last_state()
    assert seq_cold == 2
    assert hash_cold == rec2["entry_hash"]
    assert audit._cached_last_state == (2, rec2["entry_hash"])


# ---------------------------------------------------------------------------
# 2. [S4] AUTH_FAILURE Amplification Defense
# ---------------------------------------------------------------------------

def test_auth_failure_burst_suppression(monkeypatch, tmp_path: Path):
    from core import slack_security

    test_dir = tmp_path / "audit"
    test_dir.mkdir(parents=True, exist_ok=True)
    audit = AuditLogger(audit_dir=test_dir)
    audit.invalidate_cache()

    logged_events = []

    def mock_log_audit_event(**kwargs):
        logged_events.append(kwargs)

    monkeypatch.setattr("services.audit_logger.log_audit_event", mock_log_audit_event)

    with slack_security._auth_failure_lock:
        slack_security._auth_failure_tracker.clear()

    client_id = "test_attacker_ip_1"

    # Simulate 8 rapid failed attempts within the 60s window
    for _ in range(8):
        _record_auth_failure_audit(client_id)

    # Max burst limit is 5: only 5 individual events should be logged
    assert len(logged_events) == 5
    for ev in logged_events:
        assert ev["event_type"] == "AUTH_FAILURE"
        assert ev["actor"]["id"] == client_id

    # Advance time beyond 60s window to trigger aggregated summary
    with slack_security._auth_failure_lock:
        slack_security._auth_failure_tracker[client_id]["window_start"] -= 65.0

    # Next attempt triggers the summary of the 3 suppressed events + 1 new event
    _record_auth_failure_audit(client_id)

    assert len(logged_events) == 7
    summary_event = logged_events[5]
    assert summary_event["details"]["suppressed_attempts_count"] == 3
    assert "burst aggregation" in summary_event["details"]["reason"]


# ---------------------------------------------------------------------------
# 3. [S7] Fail-Closed Tool Authorization
# ---------------------------------------------------------------------------

def test_fail_closed_tool_authorization():
    # Calling with authorized_skills=None must fail closed
    res_none = skill_registry.execute_tool(
        "read_document", {"file_path": "confidential.txt"}
    )
    data_none = json.loads(res_none)
    assert "Permission denied: Agent is not authorized to execute tool 'read_document'" in data_none["error"]

    # Calling with empty list must fail closed
    res_empty = skill_registry.execute_tool(
        "read_document", {"file_path": "confidential.txt"}, authorized_skills=[]
    )
    data_empty = json.loads(res_empty)
    assert "Permission denied: Agent is not authorized to execute tool 'read_document'" in data_empty["error"]

    # Calling with internal sentinel allows execution
    with patch("skills.loader.execute_mcp_tool", return_value=json.dumps({"contents": "unrestricted"})):
        res_internal = skill_registry.execute_tool(
            "read_document", {"file_path": "doc.txt"}, authorized_skills=ALL_INTERNAL_SKILLS
        )
        assert json.loads(res_internal) == {"contents": "unrestricted"}

    # Calling load_skill with None fails closed
    load_none = skill_registry.execute_tool("load_skill", {"skill_name": "web_research"})
    data_load = json.loads(load_none)
    assert "Permission denied: Agent is not authorized to load skill 'web_research'" in data_load["error"]


# ---------------------------------------------------------------------------
# 4. [B2 & B3] Lossless Settings Persistence
# ---------------------------------------------------------------------------

def test_agent_settings_lossless_save(tmp_path: Path):
    settings_file = tmp_path / "data" / "settings" / "agent_settings.json"
    backup_file = tmp_path / "data" / "backups" / "agent_settings_backup.json"
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    backup_file.parent.mkdir(parents=True, exist_ok=True)

    # Seed with custom unmanaged fields
    initial_payload = {
        "workflow_config": "default_review_flow",
        "custom_top_level_meta": {"version": 42, "tenant": "corp"},
        "enabled_agents": ["agent-10001"],
        "agents": {
            "agent-10001": {
                "username": "Visionary",
                "model": "gemini/gemini-2.5-pro",
                "displayname": "Visionary",
                "icon_url": "static/image_1.png",
                "is_short_output": False,
                "skills": ["web_research"],
                "unmanaged_agent_field": "preserved_value",
            }
        },
        "orchestrator": {
            "fast_model": "gemini/gemini-3.8-flash",
            "reasoning_model": "mistral/mistral-large-latest",
        },
    }
    settings_file.write_text(json.dumps(initial_payload, indent=2), encoding="utf-8")

    mgr = AgentSettingsManager(settings_file=settings_file, backup_file=backup_file, auto_load=True)

    # Modify an existing config property and save
    mgr.agents_config["agent-10001"]["model"] = "gemini/gemini-3.0-ultra"
    mgr.save()

    # Inspect persisted disk JSON
    saved_data = json.loads(settings_file.read_text(encoding="utf-8"))
    assert saved_data["custom_top_level_meta"] == {"version": 42, "tenant": "corp"}
    assert saved_data["agents"]["agent-10001"]["unmanaged_agent_field"] == "preserved_value"
    assert saved_data["agents"]["agent-10001"]["model"] == "gemini/gemini-3.0-ultra"


def test_system_and_app_settings_lossless_save(tmp_path: Path):
    sys_file = tmp_path / "system_settings.json"
    sys_backup = tmp_path / "system_settings_backup.json"
    sys_initial = {
        "default_fast_model": "gemini/gemini-3.8-flash",
        "default_reasoning_model": "mistral/mistral-large-latest",
        "unmanaged_enterprise_policy": {"audit_retention_days": 365},
    }
    sys_file.write_text(json.dumps(sys_initial, indent=2), encoding="utf-8")

    sys_mgr = SystemSettingsManager(settings_file=sys_file, backup_file=sys_backup, auto_load=True)
    sys_mgr.set_default_fast_model("gemini/gemini-3.8-pro", save=True)

    saved_sys = json.loads(sys_file.read_text(encoding="utf-8"))
    assert saved_sys["unmanaged_enterprise_policy"] == {"audit_retention_days": 365}
    assert saved_sys["default_fast_model"] == "gemini/gemini-3.8-pro"

    # App settings lossless test
    app_file = tmp_path / "app_settings.json"
    app_backup = tmp_path / "app_settings_backup.json"
    app_initial = {
        "thread_skill_expiration_time": 24.0,
        "circuit_breaker_enabled": True,
        "unmanaged_ui_theme": "dark_mode_high_contrast",
    }
    app_file.write_text(json.dumps(app_initial, indent=2), encoding="utf-8")

    app_mgr = AppSettingsManager(settings_file=app_file, backup_file=app_backup, auto_load=True)
    app_mgr.thread_skill_expiration_time = 48.0
    app_mgr.save()

    saved_app = json.loads(app_file.read_text(encoding="utf-8"))
    assert saved_app["unmanaged_ui_theme"] == "dark_mode_high_contrast"
    assert saved_app["thread_skill_expiration_time"] == 48.0


# ---------------------------------------------------------------------------
# 5. [S11] Credential Redaction & Tool-Arg Unmasking
# ---------------------------------------------------------------------------

def test_credential_redaction_filter():
    redactor = CredentialRedactionFilter()

    # Redact Slack Token
    log_record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="Connecting with token xoxb-123456789012-123456789012-abcdefghijklmnopqrstuvwx",
        args=(),
        exc_info=None,
    )
    redactor.filter(log_record)
    assert "xoxb-" not in log_record.msg
    assert "[REDACTED_SLACK_TOKEN]" in log_record.msg

    # Redact OpenAI API Key in args
    log_record_args = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="API call failed with key %s and url %s",
        args=("sk-proj-1234567890123456789012345678", "https://api.openai.com"),
        exc_info=None,
    )
    redactor.filter(log_record_args)
    assert "sk-proj-" not in log_record_args.args[0]
    assert "[REDACTED_API_KEY]" in log_record_args.args[0]


def test_tool_argument_unmasking():
    mapping = {
        "<EMAIL_0>": "alice.cooper@example.com",
        "<PHONE_0>": "+1-555-0199",
    }
    masked_args = {
        "recipient": "<EMAIL_0>",
        "contact_info": {
            "phone": "<PHONE_0>",
            "fallback": "none",
        },
        "tags": ["urgent", "<EMAIL_0>"],
    }

    unmasked = _unmask_argument_values(masked_args, mapping)
    assert unmasked["recipient"] == "alice.cooper@example.com"
    assert unmasked["contact_info"]["phone"] == "+1-555-0199"
    assert unmasked["tags"] == ["urgent", "alice.cooper@example.com"]
