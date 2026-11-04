import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app
from core.database import get_db_transaction
from services.audit_logger import get_audit_trail
from services.hitl import (
    approve_hitl_request,
    consume_tool_approval,
    deny_hitl_request,
    get_pending_request,
    register_hitl_request,
)

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_hitl_registry():
    with get_db_transaction(immediate=True) as conn:
        conn.execute("DELETE FROM hitl_approvals;")
    yield
    with get_db_transaction(immediate=True) as conn:
        conn.execute("DELETE FROM hitl_approvals;")


def test_hitl_request_registration_and_hash():
    """Test UUID4 token creation, canonical hash calculation, and retrieval."""
    fn_args = {"path": "src/main.py", "content": "print('hello')"}
    req = register_hitl_request(
        channel_id="C_HITL",
        thread_ts="1700000000.001",
        fn_name="write_file",
        fn_args=fn_args,
        agent_name="SecurityArchitect",
        skill_name="code_modifier",
        initiator_user_id="U_INITIATOR_1",
    )

    assert req.request_id is not None
    assert len(req.request_id) == 36  # UUID4 standard length
    assert req.initiator_user_id == "U_INITIATOR_1"
    assert req.status == "PENDING"
    assert not req.is_expired()

    # Verify retrieval
    found = get_pending_request(req.request_id)
    assert found is not None
    assert found.request_id == req.request_id
    assert found.fn_name == "write_file"


def test_hitl_single_use_token_and_tamper_resistance():
    """Test that approval consumption is single-use and rejects tampered arguments."""
    fn_args = {"path": "config.yaml", "mode": "write"}
    req = register_hitl_request(
        channel_id="C_HITL",
        thread_ts="1700000000.002",
        fn_name="save_config",
        fn_args=fn_args,
        initiator_user_id="U_USER_A",
    )

    # Approve request
    approved = approve_hitl_request(req.request_id)
    assert approved is not None
    assert approved.status == "APPROVED"

    # Attempt 1: Tampered args should be rejected
    tampered_args = {"path": "config.yaml", "mode": "write", "malicious": True}
    assert (
        consume_tool_approval("C_HITL", "1700000000.002", "save_config", tampered_args)
        is False
    )

    # Attempt 2: Exact matching args should be consumed
    assert (
        consume_tool_approval("C_HITL", "1700000000.002", "save_config", fn_args)
        is True
    )

    # Attempt 3: Second consumption attempt of single-use token must FAIL
    assert (
        consume_tool_approval("C_HITL", "1700000000.002", "save_config", fn_args)
        is False
    )


def test_hitl_expiration_ttl():
    """Test that requests expire after TTL and cannot be approved or consumed."""
    req = register_hitl_request(
        channel_id="C_EXPIRE",
        thread_ts="1700000000.003",
        fn_name="reboot_system",
        fn_args={},
        initiator_user_id="U_ADMIN",
        ttl_seconds=-1.0,
    )

    assert req.is_expired()

    # Expired request cannot be retrieved
    assert get_pending_request(req.request_id) is None

    # Expired request cannot be consumed
    assert (
        consume_tool_approval("C_EXPIRE", "1700000000.003", "reboot_system", {})
        is False
    )


def test_hitl_slack_interactivity_authorization():
    """Test Slack Block Kit approval/denial authorization: only initiator or admin can approve/deny."""
    from core.config import set_admin_slack_user_ids_override

    set_admin_slack_user_ids_override("U_WORKSPACE_ADMIN")
    try:
        with (
            patch("adapters.slack.interactivity.verify_slack_signature", return_value=True),
            patch("adapters.slack.interactivity.slack_async_client.chat_postEphemeral", AsyncMock(return_value={"ok": True})),
            patch("adapters.slack.interactivity.slack_async_client.chat_update", AsyncMock(return_value={"ok": True})),
        ):
            req = register_hitl_request(
                channel_id="C_SLACK_HITL",
                thread_ts="1700000000.004",
                fn_name="execute_migration",
                fn_args={"target": "db_v2"},
                initiator_user_id="U_ORIGINAL_USER",
            )

            # 1. Random user attempts to approve -> 403 Forbidden
            payload_intruder = {
                "type": "block_actions",
                "user": {"id": "U_SNOOPING_USER"},
                "actions": [{"action_id": "hitl_approve_tool", "value": req.request_id}],
                "channel": {"id": "C_SLACK_HITL"},
            }
            resp = client.post("/slack/actions", json=payload_intruder)
            assert resp.status_code == 403

            # Wait briefly for background executor to write audit record
            time.sleep(0.05)

            # Audit trail verifies UNAUTHORIZED attempt
            trail = get_audit_trail(limit=10)
            unauth_events = [e for e in trail if e.get("event_type") == "HITL_UNAUTHORIZED_ATTEMPT"]
            assert len(unauth_events) > 0
            assert unauth_events[-1]["actor"]["id"] == "U_SNOOPING_USER"

            # 2. Initiator approves -> 200 OK
            payload_initiator = {
                "type": "block_actions",
                "user": {"id": "U_ORIGINAL_USER"},
                "actions": [{"action_id": "hitl_approve_tool", "value": req.request_id}],
                "channel": {"id": "C_SLACK_HITL"},
                "message": {"ts": "1700000000.005"},
            }
            resp_init = client.post("/slack/actions", json=payload_initiator)
            assert resp_init.status_code == 200

            # 3. Verify single-use execution consumed approval
            with get_db_transaction() as conn:
                row = conn.execute(
                    "SELECT status FROM hitl_approvals WHERE request_id = ?;", (req.request_id,)
                ).fetchone()
                assert row is not None
                assert row["status"] == "CONSUMED"
    finally:
        set_admin_slack_user_ids_override(None)


def test_hitl_atomic_status_transition():
    """TKT-908: Verify atomic status transitions in SQLite prevent double approvals/denials."""
    req = register_hitl_request(
        channel_id="C_ATOMIC",
        thread_ts="1700000000.010",
        fn_name="drop_table",
        fn_args={"table": "users"},
        initiator_user_id="U_ADMIN",
    )

    # First approve succeeds
    app1 = approve_hitl_request(req.request_id)
    assert app1 is not None
    assert app1.status == "APPROVED"

    # Second approve fails (already approved, not PENDING)
    app2 = approve_hitl_request(req.request_id)
    assert app2 is None

    # Deny on approved request fails (not PENDING)
    den = deny_hitl_request(req.request_id)
    assert den is None


def test_hitl_slack_action_expired_or_handled_notice():
    """TKT-908: Post ephemeral warning when approval/denial action references non-existent or handled request."""
    mock_ephemeral = AsyncMock(return_value={"ok": True})
    with (
        patch("adapters.slack.interactivity.verify_slack_signature", return_value=True),
        patch("adapters.slack.interactivity.slack_async_client.chat_postEphemeral", mock_ephemeral),
    ):
        payload_missing = {
            "type": "block_actions",
            "user": {"id": "U_ANY_USER"},
            "actions": [{"action_id": "hitl_approve_tool", "value": "non-existent-or-expired-id"}],
            "channel": {"id": "C_SLACK_HITL"},
        }
        resp = client.post("/slack/actions", json=payload_missing)
        assert resp.status_code == 200

        time.sleep(0.05)
        mock_ephemeral.assert_called_once()
        assert "Request Expired or Handled" in mock_ephemeral.call_args[1]["text"]

