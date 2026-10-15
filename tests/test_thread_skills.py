"""
Tests for Thread-Scoped Global Skills, ThreadSkillsManager, /grant_for_all, /revoke_for_all, and TTL expiration.
"""

import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestration.thread_skills_manager import ThreadSkillsManager
from pipeline.directive_parser import directive_parser
from core.registry import get_skill_registry
skill_registry = get_skill_registry()


@pytest.fixture(autouse=True)
def clean_background_worker_tasks():
    from agents.thread_notes_agent import _RUNNING_TASKS
    from agents.usage_agent import thread_usage_tracker

    yield
    for task in list(_RUNNING_TASKS.values()):
        if not task.done():
            task.cancel()
    _RUNNING_TASKS.clear()
    for task in list(thread_usage_tracker.running_usage_tasks.values()):
        if not task.done():
            task.cancel()
    thread_usage_tracker.running_usage_tasks.clear()


def test_thread_skills_manager_grant_and_active():
    with tempfile.TemporaryDirectory() as tmp_dir:
        storage_file = Path(tmp_dir) / "thread_skills.json"
        mgr = ThreadSkillsManager(storage_file=storage_file, auto_load=True)

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

        # Check file persisted
        assert storage_file.exists()
        mgr2 = ThreadSkillsManager(storage_file=storage_file, auto_load=True)
        assert mgr2.get_active_skills(channel_id, thread_ts) == ["web_research", "document_reader"]


def test_thread_skills_manager_revocation():
    with tempfile.TemporaryDirectory() as tmp_dir:
        storage_file = Path(tmp_dir) / "thread_skills.json"
        mgr = ThreadSkillsManager(storage_file=storage_file, auto_load=True)

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
    with tempfile.TemporaryDirectory() as tmp_dir:
        storage_file = Path(tmp_dir) / "thread_skills.json"
        mgr = ThreadSkillsManager(storage_file=storage_file, auto_load=True)

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


def test_execute_tool_expired_skill_warning():
    # If a skill was expired, execute_tool should return the explicit expired message
    res = skill_registry.execute_tool(
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
    with tempfile.TemporaryDirectory() as tmp_dir:
        storage_file = Path(tmp_dir) / "thread_skills.json"
        mgr = ThreadSkillsManager(storage_file=storage_file, auto_load=True)

        channel_id = "C_TEST"
        thread_ts = "1726000000.123456"

        mgr.grant_skills(channel_id, thread_ts, ["web_research"])

        from orchestration.thread_state import thread_state_manager
        thread_state_manager.request_stop(channel_id, thread_ts)

        # Thread skills still intact!
        assert mgr.get_active_skills(channel_id, thread_ts) == ["web_research"]
        thread_state_manager.clear_stop(channel_id, thread_ts)
