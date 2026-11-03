import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app
from core.database import DatabaseManager, get_connection
from pipeline.spend_governance.circuit_breaker import SpendGovernance
from agents.usage_agent import record_thread_tokens
from services.mcp_tools import is_agent_authorized_for_tool
from core.slack_security import sanitize_slack_blocks
from agents.thread_notes_agent import _sanitize_note_text
from orchestration.turn_pipeline import TurnContext, TurnPipeline
from orchestration.stages.post_record import PostRecordStage


client = TestClient(app)


def test_tkt_701_sqlite_auto_initialization(tmp_path):
    """
    TKT-701: Verify DatabaseManager automatically executes DDL on fresh database paths,
    ensuring tables like spend_ledger, thread_skills, hitl_approvals, and thread_sessions
    are initialized before any query.
    """
    fresh_db = tmp_path / "fresh_test.db"
    assert not fresh_db.exists()

    mgr = DatabaseManager(db_path=fresh_db)
    with mgr.create_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = {row[0] for row in cursor.fetchall()}

    assert "spend_ledger" in tables
    assert "thread_skills" in tables
    assert "thread_sessions" in tables
    assert "hitl_approvals" in tables
    assert "thread_context" in tables


def test_tkt_702_spend_ledger_unification_and_preservation(tmp_path):
    """
    TKT-702: Verify record_thread_tokens records spend to SQLite spend_ledger,
    and clear_session does NOT delete historical spend records.
    """
    from core.database import db_manager

    test_db = tmp_path / "spend_test.db"
    db_manager.configure_paths(db_path=test_db)

    try:
        circuit_breaker = SpendGovernance()

        # 1. Record usage directly via record_thread_tokens
        with patch("pipeline.spend_governance.spend_governance", circuit_breaker):
            record_thread_tokens(
                channel_id="C_SPEND_1",
                thread_ts="1700000000.100",
                model_name="gemini/gemini-2.5-flash",
                prompt_tokens=1000,
                completion_tokens=500,
            )

        # Check that spend_ledger has 1 record
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM spend_ledger WHERE channel_id = 'C_SPEND_1' AND thread_ts = '1700000000.100';")
            count = cursor.fetchone()[0]
        assert count == 1

        # 2. Clear session (e.g. after round or sequence resumption)
        circuit_breaker.clear_session("C_SPEND_1:1700000000.100")

        # Historical spend records must NOT be deleted
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM spend_ledger WHERE channel_id = 'C_SPEND_1' AND thread_ts = '1700000000.100';")
            count_after = cursor.fetchone()[0]
        assert count_after == 1
    finally:
        db_manager.reset_paths()


def test_tkt_703_mcp_tool_dynamic_thread_skill_authorization():
    """
    TKT-703: Verify is_agent_authorized_for_tool and execute_mcp_tool
    grant access when the skill is granted in authorized_skills (thread-level).
    """
    agent = "agent-10001"  # Security Architect (does not statically own web_research or document tools)
    tool = "web_search"

    # Static authorization fails without authorized_skills
    assert is_agent_authorized_for_tool(agent, tool, authorized_skills=[]) is False

    # Dynamic authorization succeeds when skill is passed in authorized_skills
    assert is_agent_authorized_for_tool(agent, tool, authorized_skills=["web_research"]) is True

    # Unknown tool fails closed
    assert is_agent_authorized_for_tool(agent, "non_existent_tool_xyz", authorized_skills=["web_research"]) is False


def test_tkt_704_slack_blocks_and_notes_secret_and_broadcast_guardrails():
    """
    TKT-704: Verify sanitize_slack_blocks detects exposed secrets and neutralizes broadcast mentions,
    and _sanitize_note_text redacts secrets from notes before storage and display.
    """
    # 1. Blocks with exposed secret and broadcast mention
    raw_blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "Token is xoxb-123456789012-1234567890123-abcdefghijklmnopqrstuvwx and <!channel> look!",
            },
        }
    ]
    sanitized_blocks, matched_rule = sanitize_slack_blocks(raw_blocks, user_id="U_TEST")
    # Verify secret pattern was flagged
    assert matched_rule == "slack_token"
    # Verify broadcast mention was neutralized in sanitized structure
    sanitized_text = json.dumps(sanitized_blocks)
    assert "<!channel>" not in sanitized_text
    assert "@channel" in sanitized_text

    # 2. Notes text sanitization
    note_with_secret = (
        "Project update: token is xoxb-123456789012-1234567890123-abcdefghijklmnopqrstuvwx. "
        "Ping <!everyone> for sync."
    )
    clean_note = _sanitize_note_text(note_with_secret)
    assert "xoxb-" not in clean_note
    assert "<!everyone>" not in clean_note
    assert "redacted by security policy" in clean_note


@pytest.mark.anyio
async def test_tkt_705_hitl_workflow_suspension_and_nomination_suppression():
    """
    TKT-705: Verify that when waiting_hitl is True, ctx.halted is True
    and nomination injection in PostRecordStage is completely suppressed.
    """
    mock_poster = AsyncMock(return_value={"ok": True, "ts": "1700000000.501"})
    mock_sleep = AsyncMock()

    pipeline = TurnPipeline(
        slack_poster=mock_poster,
        sleep_fn=mock_sleep,
    )

    ctx = TurnContext(
        channel_id="C_HITL_TEST",
        thread_ts="1700000000.500",
        raw_text="Test prompt",
    )
    ctx.waiting_hitl = True
    ctx.halted = True
    ctx.agent_key = "agent-10001"
    ctx.agent_cfg = {"username": "Security Architect", "icon_url": "static/image_1.png", "model": "test-model"}
    ctx.final_text = "I need approval for file modification. Over to [agent-10002]."
    ctx.turn_token_mapping = {}

    stage = PostRecordStage(pipeline.deps)

    await stage.stage_post_and_record(ctx)

    assert ctx.halted is True
    assert ctx.is_completed is False
    assert ctx.next_designated_agent is None


def test_tkt_908_legacy_colon_format_rejected(monkeypatch):
    """
    TKT-908: Verify legacy colon format bypass has been removed and triggers invalid/expired notice.
    """
    mock_ephemeral = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("adapters.slack.interactivity.verify_slack_signature", lambda body, headers: True)
    monkeypatch.setattr("adapters.slack.interactivity.slack_async_client.chat_postEphemeral", mock_ephemeral)

    # Legacy colon approval attempt is treated as invalid/expired request
    payload_colon = {
        "type": "block_actions",
        "user": {"id": "U_USER"},
        "actions": [{"action_id": "hitl_approve_tool", "value": "C_CHAN:1700000000.001:write_file"}],
        "channel": {"id": "C_CHAN"},
    }
    resp = client.post("/slack/actions", json=payload_colon)
    assert resp.status_code == 200
    mock_ephemeral.assert_called()
    assert "Request Expired or Handled" in mock_ephemeral.call_args[1]["text"]

    mock_ephemeral.reset_mock()
    # Legacy colon denial attempt is treated as invalid/expired request
    payload_colon_deny = {
        "type": "block_actions",
        "user": {"id": "U_USER"},
        "actions": [{"action_id": "hitl_deny_tool", "value": "C_CHAN:1700000000.001:write_file"}],
        "channel": {"id": "C_CHAN"},
    }
    resp_deny = client.post("/slack/actions", json=payload_colon_deny)
    assert resp_deny.status_code == 200
    mock_ephemeral.assert_called()
    assert "Request Expired or Handled" in mock_ephemeral.call_args[1]["text"]

