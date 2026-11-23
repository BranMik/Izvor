"""
Unit and integration tests for Slack Interactivity ActionSpec Registry (TKT-915).
Covers:
- _validate_chain_agent_keys rejecting unknown agent IDs
- Whole-payload pre-authorization (mixed non-admin / admin actions fail fast with 403)
- Audit event emission for previously unaudited actions
- Strict dispatch via ACTION_REGISTRY and PREFIX_ACTION_REGISTRY
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.background import BackgroundTasks
from starlette.requests import Request

from adapters.slack.actions import _validate_chain_agent_keys
from adapters.slack.interactivity import handle_slack_interactivity
from core.config import set_admin_slack_user_ids_override
from config_store.agent_settings import (
    get_fixed_config,
)


def test_validate_chain_agent_keys_filters_unknown():
    """Verifies that _validate_chain_agent_keys accepts only configured agents."""
    raw = "agent-10001, unknown_hacker_agent, agent-10002, bad-agent-999"
    valid, invalid = _validate_chain_agent_keys(raw)

    assert "agent-10001" in valid
    assert "agent-10002" in valid
    assert "unknown_hacker_agent" in invalid
    assert "bad-agent-999" in invalid
    assert len(valid) == 2
    assert len(invalid) == 2


@pytest.mark.anyio
async def test_whole_payload_pre_authorization_blocks_mixed_actions():
    """
    Verifies that if a non-admin user sends a multi-action payload containing
    both a non-admin action and an admin action, the request is rejected with 403
    before ANY action executes.
    """
    set_admin_slack_user_ids_override("U_ADMIN")

    executed_scribe = []
    logged_events = []

    def record_event(**kwargs):
        logged_events.append(kwargs)

    with patch("adapters.slack.interactivity.verify_slack_signature", return_value=True), \
         patch("adapters.slack.actions.scribe_actions._handle_scribe_download", lambda **kwargs: executed_scribe.append(kwargs)), \
         patch("adapters.slack.actions.agent_actions.log_audit_event", side_effect=record_event), \
         patch("adapters.slack.actions.governance_actions.log_audit_event_async", AsyncMock(side_effect=record_event)), \
         patch("adapters.slack.interactivity.log_audit_event_async", AsyncMock(side_effect=record_event)), \
         patch("services.audit_logger.log_audit_event", side_effect=record_event):

        # Payload has non-admin action first, then admin action
        payload = {
            "type": "block_actions",
            "user": {"id": "U_NON_ADMIN"},
            "actions": [
                {"action_id": "download_scribe_notes_txt", "value": "C_TEST:123.456"},
                {"action_id": "toggle_circuit_breaker_radio", "selected_option": {"value": "cb:off"}},
            ],
            "channel": {"id": "C_TEST"},
        }

        req = MagicMock(spec=Request)
        req.body = AsyncMock(return_value=json.dumps(payload).encode("utf-8"))
        req.headers = {}
        bg = BackgroundTasks()

        resp = await handle_slack_interactivity(req, bg)

        assert resp.status_code == 403
        assert len(executed_scribe) == 0  # Scribe download must NOT have executed!
        assert any(
            e.get("event_type") == "ADMIN_ACTION_DENIED"
            and e.get("action") == "toggle_circuit_breaker_radio"
            for e in logged_events
        )


@pytest.mark.anyio
async def test_chain_mutation_rejects_unknown_agent_keys():
    """Verifies that saving sandwich or fixed chains strips unknown agent IDs."""
    set_admin_slack_user_ids_override("U_ADMIN")

    logged_events = []

    def record_event(**kwargs):
        logged_events.append(kwargs)

    with patch("adapters.slack.interactivity.verify_slack_signature", return_value=True), \
         patch("services.slack.views.publish_app_home", AsyncMock()), \
         patch("adapters.slack.actions.workflow_presets.log_audit_event_async", AsyncMock(side_effect=record_event)), \
         patch("services.audit_logger.log_audit_event", side_effect=record_event):

        payload = {
            "type": "block_actions",
            "user": {"id": "U_ADMIN"},
            "actions": [{"action_id": "save_fixed_chain_button"}],
            "view": {
                "state": {
                    "values": {
                        "fixed_block": {
                            "set_fixed_chain_input": {
                                "value": "agent-10001, invalid_agent_xyz, agent-10002, agent-10003, agent-10014, agent-10012"
                            }
                        }
                    }
                }
            },
        }

        req = MagicMock(spec=Request)
        req.body = AsyncMock(return_value=json.dumps(payload).encode("utf-8"))
        req.headers = {}
        bg = BackgroundTasks()

        resp = await handle_slack_interactivity(req, bg)
        assert resp.status_code == 200

        cfg = get_fixed_config()
        assert "agent-10001" in cfg["chain"]
        assert "agent-10002" in cfg["chain"]
        assert cfg["chain"] == [
            "agent-10001",
            "agent-10002",
            "agent-10003",
            "agent-10014",
            "agent-10012",
        ]
        assert "invalid_agent_xyz" not in cfg["chain"]

        audit_entry = next((e for e in logged_events if e.get("action") == "save_fixed_chain_button"), None)
        assert audit_entry is not None
        assert audit_entry.get("event_type") == "ADMIN_CONFIG_CHANGE"
        assert "invalid_agent_xyz" in audit_entry.get("details", {}).get("invalid_keys", [])


@pytest.mark.anyio
async def test_newly_audited_actions_emit_audit_log():
    """Verifies that select_orchestration_mode and expiry preset emit ADMIN_CONFIG_CHANGE."""
    set_admin_slack_user_ids_override("U_ADMIN")

    logged_events = []

    def record_event(**kwargs):
        logged_events.append(kwargs)

    with patch("adapters.slack.interactivity.verify_slack_signature", return_value=True), \
         patch("services.slack.views.publish_app_home", AsyncMock()), \
         patch("adapters.slack.actions.governance_actions.log_audit_event_async", AsyncMock(side_effect=record_event)), \
         patch("adapters.slack.actions.workflow_presets.log_audit_event_async", AsyncMock(side_effect=record_event)), \
         patch("services.audit_logger.log_audit_event", side_effect=record_event):

        # 1. select_orchestration_mode
        payload_mode = {
            "type": "block_actions",
            "user": {"id": "U_ADMIN"},
            "actions": [
                {
                    "action_id": "select_orchestration_mode",
                    "selected_option": {"value": "mode:sandwich"},
                }
            ],
        }
        req1 = MagicMock(spec=Request)
        req1.body = AsyncMock(return_value=json.dumps(payload_mode).encode("utf-8"))
        req1.headers = {}
        await handle_slack_interactivity(req1, BackgroundTasks())

        mode_audit = next((e for e in logged_events if e.get("action") == "select_orchestration_mode"), None)
        assert mode_audit is not None
        assert mode_audit["event_type"] == "ADMIN_CONFIG_CHANGE"
        assert mode_audit["details"]["mode"] == "sandwich"

        # 2. set_thread_skill_expiry_preset
        payload_expiry = {
            "type": "block_actions",
            "user": {"id": "U_ADMIN"},
            "actions": [{"action_id": "set_thread_skill_expiry_preset:24"}],
        }
        req2 = MagicMock(spec=Request)
        req2.body = AsyncMock(return_value=json.dumps(payload_expiry).encode("utf-8"))
        req2.headers = {}
        await handle_slack_interactivity(req2, BackgroundTasks())

        expiry_audit = next((e for e in logged_events if e.get("action") == "set_thread_skill_expiry_preset"), None)
        assert expiry_audit is not None
        assert expiry_audit["event_type"] == "ADMIN_CONFIG_CHANGE"
        assert expiry_audit["details"]["hours"] == 24.0
