import time
from unittest.mock import AsyncMock

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
    """Test that modal submission and opening is restricted to admins with modal feedback for UI actions."""
    monkeypatch.setenv("SLACK_ADMIN_USER_IDS", "U_LEGIT_ADMIN")
    monkeypatch.setattr("adapters.slack.interactivity.verify_slack_signature", lambda body, headers: True)
    mock_views_open = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("adapters.slack.actions.agent_actions.slack_async_client.views_open", mock_views_open)
    monkeypatch.setattr("services.slack.views.publish_app_home", AsyncMock(return_value={"ok": True}))

    # 1. Interactive action attempt by non-admin (trigger_id present -> returns 200 and pops modal)
    open_payload = {
        "type": "block_actions",
        "user": {"id": "U_NON_ADMIN"},
        "actions": [{"action_id": "open_edit_workspace_context_modal"}],
        "trigger_id": "trig_123",
    }
    resp = client.post("/slack/actions", json=open_payload)
    assert resp.status_code == 200
    mock_views_open.assert_called_once()
    modal_arg = mock_views_open.call_args.kwargs
    assert modal_arg["trigger_id"] == "trig_123"
    assert modal_arg["view"]["title"]["text"] == "Admin Access Required"

    # 2. Headless action attempt by non-admin (no trigger_id -> returns 403 fail-closed)
    headless_payload = {
        "type": "block_actions",
        "user": {"id": "U_NON_ADMIN"},
        "actions": [{"action_id": "open_edit_workspace_context_modal"}],
    }
    resp_headless = client.post("/slack/actions", json=headless_payload)
    assert resp_headless.status_code == 403

    # 3. Submit modal attempt by non-admin
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


def test_workflow_dropdown_non_admin_opens_admin_required_modal(monkeypatch):
    """Test that selecting workflow preset as non-admin opens Admin Access Required modal and logs audit event."""
    monkeypatch.setenv("SLACK_ADMIN_USER_IDS", "U_LEGIT_ADMIN")
    monkeypatch.setattr("adapters.slack.interactivity.verify_slack_signature", lambda body, headers: True)
    mock_views_open = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("adapters.slack.actions.agent_actions.slack_async_client.views_open", mock_views_open)
    monkeypatch.setattr("services.slack.views.publish_app_home", AsyncMock(return_value={"ok": True}))

    wf_payload = {
        "type": "block_actions",
        "user": {"id": "U0BGW69LT08"},
        "trigger_id": "trig_wf_select_999",
        "actions": [
            {
                "action_id": "select_workflow_preset",
                "selected_option": {
                    "value": "wf:default_sandwich",
                    "text": {"type": "plain_text", "text": "🥪 Default Sandwich"},
                },
            }
        ],
    }
    resp = client.post("/slack/actions", json=wf_payload)
    assert resp.status_code == 200

    mock_views_open.assert_called_once()
    modal_call = mock_views_open.call_args.kwargs
    assert modal_call["trigger_id"] == "trig_wf_select_999"
    modal_view = modal_call["view"]
    assert modal_view["title"]["text"] == "Admin Access Required"
    # Ensure User ID is embedded in modal instructions
    block_texts = [b["text"]["text"] for b in modal_view["blocks"] if "text" in b and isinstance(b["text"], dict)]
    assert any("U0BGW69LT08" in t for t in block_texts)
    assert any("Select Workflow Preset" in t for t in block_texts)


def test_workflow_dropdown_non_admin_expired_trigger_id_fallback_dm(monkeypatch):
    """Test that when trigger_id expires on views.open, the server does not crash, sends a fallback DM, and re-syncs App Home."""
    from slack_sdk.errors import SlackApiError

    monkeypatch.setenv("SLACK_ADMIN_USER_IDS", "U_LEGIT_ADMIN")
    monkeypatch.setattr("adapters.slack.interactivity.verify_slack_signature", lambda body, headers: True)

    mock_resp = {"ok": False, "error": "expired_trigger_id"}
    mock_views_open = AsyncMock(side_effect=SlackApiError(message="expired_trigger_id", response=mock_resp))
    monkeypatch.setattr("adapters.slack.actions.agent_actions.slack_async_client.views_open", mock_views_open)

    mock_open_dm = AsyncMock(return_value="D_TEST_USER_DM")
    mock_post_msg = AsyncMock(return_value={"ok": True})
    mock_publish_home = AsyncMock(return_value={"ok": True})

    monkeypatch.setattr("adapters.slack.actions.agent_actions.open_dm_channel", mock_open_dm)
    monkeypatch.setattr("adapters.slack.actions.agent_actions.post_message_to_slack", mock_post_msg)
    monkeypatch.setattr("services.slack.views.publish_app_home", mock_publish_home)

    wf_payload = {
        "type": "block_actions",
        "user": {"id": "U0BGW69LT08"},
        "trigger_id": "trig_wf_expired_123",
        "actions": [
            {
                "action_id": "select_workflow_preset",
                "selected_option": {
                    "value": "wf:default_sandwich",
                    "text": {"type": "plain_text", "text": "🥪 Default Sandwich"},
                },
            }
        ],
    }
    resp = client.post("/slack/actions", json=wf_payload)
    assert resp.status_code == 200

    # 1. views.open was attempted
    mock_views_open.assert_called_once()
    assert mock_views_open.call_args.kwargs["trigger_id"] == "trig_wf_expired_123"

    # 2. Fallback DM was sent because modal failed with expired_trigger_id
    mock_open_dm.assert_called_once_with("U0BGW69LT08")
    mock_post_msg.assert_called_once()
    dm_call_kwargs = mock_post_msg.call_args.kwargs
    assert dm_call_kwargs["channel_id"] == "D_TEST_USER_DM"
    assert "U0BGW69LT08" in dm_call_kwargs["text"]
    assert "Permission Denied" in dm_call_kwargs["text"]
    assert "Select Workflow Preset" in dm_call_kwargs["text"]

    # 3. App Home view was still safely published/refreshed
    mock_publish_home.assert_called_once_with(user_id="U0BGW69LT08")


def test_non_admin_in_channel_action_sends_ephemeral_on_modal_failure(monkeypatch):
    """Test that non-admin action inside a channel sends ephemeral denial if trigger_id is missing or views_open fails."""
    monkeypatch.setenv("SLACK_ADMIN_USER_IDS", "U_LEGIT_ADMIN")
    monkeypatch.setattr("adapters.slack.interactivity.verify_slack_signature", lambda body, headers: True)

    mock_ephemeral = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("adapters.slack.actions.agent_actions.post_ephemeral_to_slack", mock_ephemeral)

    payload = {
        "type": "block_actions",
        "user": {"id": "U_CHANNEL_USER"},
        "channel": {"id": "C_DEV_CHANNEL"},
        "actions": [
            {
                "action_id": "toggle_circuit_breaker_radio",
                "selected_option": {
                    "value": "cb:off",
                    "text": {"type": "plain_text", "text": "🔴 OFF"},
                },
            }
        ],
    }
    resp = client.post("/slack/actions", json=payload)
    assert resp.status_code == 403

    mock_ephemeral.assert_called_once()
    assert mock_ephemeral.call_args.kwargs["channel_id"] == "C_DEV_CHANNEL"
    assert mock_ephemeral.call_args.kwargs["user_id"] == "U_CHANNEL_USER"

