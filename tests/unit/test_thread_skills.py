"""
Tests for Thread-Scoped Global Skills, ThreadSkillsManager, /grant_for_all, /revoke_for_all, and TTL expiration.
Pure SQLite backed.
"""

import json
import time
from unittest.mock import patch

import pytest

from core.database import get_connection
from orchestration.thread_skills_manager import ThreadSkillsManager
from pipeline.directive_parser import directive_parser
from core.registry import get_skill_registry
skill_registry = get_skill_registry()


@pytest.fixture(autouse=True)
def clean_background_worker_tasks():
    from agents.thread_notes_agent import _RUNNING_TASKS
    from agents.usage_agent import usage_tracker

    yield
    for task in list(_RUNNING_TASKS.values()):
        if not task.done():
            task.cancel()
    _RUNNING_TASKS.clear()
    for task in list(usage_tracker.running_usage_tasks.values()):
        if not task.done():
            task.cancel()
    usage_tracker.running_usage_tasks.clear()


def test_thread_skills_manager_grant_and_active():
    mgr = ThreadSkillsManager()

    channel_id = "C_TEST"
    thread_ts = "1726000000.123456"

    # Initially no skills
    assert mgr.get_active_skills(channel_id, thread_ts) == []
    assert mgr.get_expired_skills(channel_id, thread_ts) == []

    # Grant skills
    active, exp_ts = mgr.grant_skills(channel_id, thread_ts, ["web_research", "document_reader"])
    assert "web_research" in active
    assert "document_reader" in active
    assert exp_ts > time.time()

    # Verify across instances with shared SQLite DB
    mgr2 = ThreadSkillsManager()
    assert set(mgr2.get_active_skills(channel_id, thread_ts)) == {"web_research", "document_reader"}


def test_thread_skills_manager_revocation():
    mgr = ThreadSkillsManager()

    channel_id = "C_TEST"
    thread_ts = "1726000000.123456"

    mgr.grant_skills(channel_id, thread_ts, ["web_research", "document_reader"])

    # Revoke one
    remaining = mgr.revoke_skills(channel_id, thread_ts, ["web_research"])
    assert remaining == ["document_reader"]
    assert mgr.get_active_skills(channel_id, thread_ts) == ["document_reader"]

    # Revoke all
    remaining = mgr.revoke_skills(channel_id, thread_ts, ["all"])
    assert remaining == []
    assert mgr.get_active_skills(channel_id, thread_ts) == []


def test_thread_skills_manager_ttl_expiration():
    mgr = ThreadSkillsManager()

    channel_id = "C_TEST"
    thread_ts = "1726000000.123456"

    mgr.grant_skills(channel_id, thread_ts, ["web_research"])
    assert mgr.get_active_skills(channel_id, thread_ts) == ["web_research"]
    assert mgr.get_expired_skills(channel_id, thread_ts) == []

    # Simulate 13 hours passing (default TTL is 12 hours)
    future_time = time.time() + (13 * 3600)
    with patch("time.time", return_value=future_time):
        # Now active_skills is empty, and expired_skills has web_research
        assert mgr.get_active_skills(channel_id, thread_ts) == []
        assert mgr.get_expired_skills(channel_id, thread_ts) == ["web_research"]

        status = mgr.get_expiration_status(channel_id, thread_ts)
        assert status["has_skills"] is True
        assert status["active"] is False
        assert status["skills"] == ["web_research"]


@pytest.mark.anyio
async def test_directive_parser_grant_and_revoke_for_all():
    raw_grant = "/grant_skill_for_all web_research document_reader [agent-10008] inspect repo"
    parsed_grant = await directive_parser.parse(raw_grant)

    assert any(a.name == "grant_skill_for_all" for a in parsed_grant.actions)
    grant_action = next(a for a in parsed_grant.actions if a.name == "grant_skill_for_all")
    assert grant_action.params["skills"] == ["web_research", "document_reader"]
    assert "[agent-10008] inspect repo" in parsed_grant.cleaned_prompt
    assert "/grant_skill_for_all" not in parsed_grant.cleaned_prompt

    raw_revoke = "/revoke_skill_for_all web_research"
    parsed_revoke = await directive_parser.parse(raw_revoke)

    assert any(a.name == "revoke_skill_for_all" for a in parsed_revoke.actions)
    revoke_action = next(a for a in parsed_revoke.actions if a.name == "revoke_skill_for_all")
    assert revoke_action.params["skills"] == ["web_research"]
    assert parsed_revoke.cleaned_prompt == ""


@pytest.mark.anyio
async def test_execute_tool_expired_skill_warning():
    res = await skill_registry.execute_tool_async(
        "load_skill",
        {"skill_name": "web_research"},
        authorized_skills=[],
        expired_skills=["web_research"],
        ttl_hours=12.0,
    )
    data = json.loads(res)
    assert "error" in data
    assert "expired after 12.0 hours" in data["error"]
    assert "/grant_skill_for_all web_research" in data["error"]


def test_stop_does_not_cancel_thread_skills():
    mgr = ThreadSkillsManager()

    channel_id = "C_TEST"
    thread_ts = "1726000000.123456"

    mgr.grant_skills(channel_id, thread_ts, ["web_research"])

    from orchestration.thread_state import thread_state_manager
    thread_state_manager.request_stop(channel_id, thread_ts)

    # Thread skills still intact!
    assert mgr.get_active_skills(channel_id, thread_ts) == ["web_research"]
    thread_state_manager.clear_stop(channel_id, thread_ts)


def test_thread_skills_sqlite_persistence():
    mgr = ThreadSkillsManager()
    channel_id = "C_SQLITE_TEST"
    thread_ts = "1726000000.999999"

    mgr.grant_skills(channel_id, thread_ts, ["web_research", "pdf_reader"])
    mgr.grant_agent_skills(channel_id, thread_ts, "secret_scanner", ["agent-10001"])

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT agent_id, skill_name FROM thread_skills WHERE channel_id = ? AND thread_ts = ? ORDER BY skill_name ASC;",
            (channel_id, thread_ts),
        ).fetchall()
        assert len(rows) == 3
        global_skills = [r["skill_name"] for r in rows if r["agent_id"] == "*"]
        assert set(global_skills) == {"pdf_reader", "web_research"}
        agent_skills = [r["skill_name"] for r in rows if r["agent_id"] == "agent-10001"]
        assert agent_skills == ["secret_scanner"]
