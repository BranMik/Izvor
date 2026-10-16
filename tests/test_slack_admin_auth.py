import json
import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from core.config import get_admin_slack_user_ids, is_slack_admin
from main import app
from services.audit_logger import get_audit_trail

client = TestClient(app)


def test_is_slack_admin_config_resolution(monkeypatch):
    """Test resolution of admin user IDs from SLACK_ADMIN_USER_IDS env var."""
    # 1. Fail-closed when no env vars configured
    monkeypatch.delenv("SLACK_ADMIN_USER_IDS", raising=False)
    assert get_admin_slack_user_ids() == set()
    assert is_slack_admin("U_ANY") is False
    assert is_slack_admin(None) is False
    assert is_slack_admin("") is False

    # 2. Single admin via SLACK_ADMIN_USER_IDS
    monkeypatch.setenv("SLACK_ADMIN_USER_IDS", "U_CHIEF_ADMIN")
    assert get_admin_slack_user_ids() == {"U_CHIEF_ADMIN"}
    assert is_slack_admin("U_CHIEF_ADMIN") is True
    assert is_slack_admin("U_RANDOM") is False

    # 3. Multiple admins via SLACK_ADMIN_USER_IDS comma-separated
    monkeypatch.setenv("SLACK_ADMIN_USER_IDS", "U_ADMIN_A, U_ADMIN_B,  U_ADMIN_C ")
    admin_ids = get_admin_slack_user_ids()
    assert admin_ids == {"U_ADMIN_A", "U_ADMIN_B", "U_ADMIN_C"}
    assert is_slack_admin("U_ADMIN_B") is True
    assert is_slack_admin("U_REGULAR") is False


def test_non_admin_blocked_from_system_actions(monkeypatch):
    """Test that non-admin users receive 403 Forbidden and audit event on admin Block Kit actions."""
    monkeypatch.setenv("SLACK_ADMIN_USER_IDS", "U_REAL_ADMIN")
    monkeypatch.setattr("adapters.slack.interactivity.verify_slack_signature", lambda body, headers: True)
    mock_ephemeral = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("adapters.slack.interactivity.slack_async_client.chat_postEphemeral", mock_ephemeral)

    payload = {
        "type": "block_actions",
        "user": {"id": "U_INTRUDER_99"},
        "channel": {"id": "C_TEST_CHANNEL"},
        "actions": [
            {
                "action_id": "toggle_agent_radio:agent-10011",
                "selected_option": {
                    "value": "agent-10011:off",
                    "text": {"type": "plain_text", "text": "🔴 OFF"},
                },
            }
        ],
    }

    resp = client.post("/slack/actions", json=payload)
    assert resp.status_code == 403

    # Wait briefly for background thread to write audit record
    time.sleep(0.05)

    # Verify audit event emitted
    trail = get_audit_trail(limit=10)
    denied_events = [
        e for e in trail
        if e.get("event_type") == "ADMIN_ACTION_DENIED"
        and e.get("actor", {}).get("id") == "U_INTRUDER_99"
    ]
    assert len(denied_events) > 0
    assert denied_events[-1]["severity"] == "CRITICAL"
    assert denied_events[-1]["action"] == "toggle_agent_radio:agent-10011"


def test_admin_allowed_system_actions(monkeypatch):
    """Test that configured admin users successfully perform settings actions."""
    monkeypatch.setenv("SLACK_ADMIN_USER_IDS", "U_REAL_ADMIN")
    monkeypatch.setattr("adapters.slack.interactivity.verify_slack_signature", lambda body, headers: True)
    monkeypatch.setattr("adapters.slack.interactivity.publish_app_home", AsyncMock(return_value={"ok": True}))

    payload = {
        "type": "block_actions",
        "user": {"id": "U_REAL_ADMIN"},
        "actions": [
            {
                "action_id": "toggle_agent_radio:agent-10011",
                "selected_option": {
                    "value": "agent-10011:off",
                    "text": {"type": "plain_text", "text": "🔴 OFF"},
                },
            }
        ],
    }

    resp = client.post("/slack/actions", json=payload)
    assert resp.status_code == 200


def test_non_admin_blocked_from_workspace_modal(monkeypatch):
    """Test that modal submission and opening is restricted to admins."""
    monkeypatch.setenv("SLACK_ADMIN_USER_IDS", "U_LEGIT_ADMIN")
    monkeypatch.setattr("adapters.slack.interactivity.verify_slack_signature", lambda body, headers: True)

    # 1. Open modal attempt by non-admin
    open_payload = {
        "type": "block_actions",
        "user": {"id": "U_NON_ADMIN"},
        "actions": [{"action_id": "open_edit_workspace_context_modal"}],
        "trigger_id": "trig_123",
    }
    resp = client.post("/slack/actions", json=open_payload)
    assert resp.status_code == 403

    # 2. Submit modal attempt by non-admin
    submit_payload = {
        "type": "view_submission",
        "user": {"id": "U_NON_ADMIN"},
        "view": {
            "callback_id": "submit_workspace_context_modal",
            "state": {
                "values": {
                    "workspace_context_input_block": {
                        "workspace_context_input": {"value": "Malicious context override"}
                    }
                }
            },
        },
    }
    resp_sub = client.post("/slack/actions", json=submit_payload)
    assert resp_sub.status_code == 403
