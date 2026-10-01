import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from orchestration.thread_skills_manager import ThreadSkillsManager
from orchestration.turn_pipeline import TurnContext, TurnPipeline
from pipeline.directive_parser import directive_parser
from core.registry import get_skill_registry
skill_registry = get_skill_registry()


@pytest.fixture
def temp_skills_mgr():
    with tempfile.TemporaryDirectory() as tmp_dir:
        storage_file = Path(tmp_dir) / "thread_skills.json"
        mgr = ThreadSkillsManager(storage_file=storage_file, auto_load=True)
        yield mgr


# ---------------------------------------------------------------------------
# 1. Directive Parser Tests for /grant_skill and /grant_for_all
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_directive_parser_grant_skill_basic():
    raw = "/grant_skill web_research SecurityArchitect"
    parsed = await directive_parser.parse(raw)
    act = next((a for a in parsed.actions if a.name == "grant_skill"), None)
    assert act is not None
    assert act.params.get("skill") == "web_research"
    assert act.params.get("agents") == ["SecurityArchitect"]


@pytest.mark.anyio
async def test_directive_parser_grant_skill_bracketed():
    raw = "/grant_skill [web_research] [SecurityArchitect]"
    parsed = await directive_parser.parse(raw)
    act = next((a for a in parsed.actions if a.name == "grant_skill"), None)
    assert act is not None
    assert act.params.get("skill") == "web_research"
    assert act.params.get("agents") == ["SecurityArchitect"]


@pytest.mark.anyio
async def test_directive_parser_grant_skill_multiple_agents():
    raw = "/grant_skill [web_research] [SecurityArchitect] [Visionary]"
    parsed = await directive_parser.parse(raw)
    act = next((a for a in parsed.actions if a.name == "grant_skill"), None)
    assert act is not None
    assert act.params.get("skill") == "web_research"
    assert "SecurityArchitect" in act.params.get("agents", [])
    assert "Visionary" in act.params.get("agents", [])


@pytest.mark.anyio
async def test_directive_parser_grant_skill_alias():
    raw = "/grant-skill web_research SecurityArchitect"
    parsed = await directive_parser.parse(raw)
    act = next((a for a in parsed.actions if a.name == "grant_skill"), None)
    assert act is not None
    assert act.params.get("skill") == "web_research"
    assert act.params.get("agents") == ["SecurityArchitect"]


@pytest.mark.anyio
async def test_directive_parser_grant_skill_trailing_text_ignored():
    raw = "/grant_skill web_research SecurityArchitect and please tell me about python"
    parsed = await directive_parser.parse(raw)
    act = next((a for a in parsed.actions if a.name == "grant_skill"), None)
    assert act is not None
    assert act.params.get("skill") == "web_research"
    assert act.params.get("agents") == ["SecurityArchitect"]


# ---------------------------------------------------------------------------
# 2. Exclusivity Tests: /grant_for_all and /grant_skill Unconditionally Halt
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_grant_skill_for_all_unconditionally_halts():
    pipeline = TurnPipeline()
    channel_id = "C_TEST"
    thread_ts = "1726000000.1"

    post_mock = AsyncMock()
    pipeline.post_system_message_fn = post_mock

    # Test with extra trailing prompt text
    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="/grant_skill_for_all web_research Now discuss everything",
    )
    directives = await directive_parser.parse(ctx.raw_text)
    ctx.actions = directives.actions
    ctx.raw_text_cleaned = directives.cleaned_prompt

    res = await pipeline.stage_validate_and_parse(ctx)
    assert res is not None
    assert res.get("ok") is True
    assert res.get("halted") is True
    assert res.get("action") == "grant_skill_for_all"
    post_mock.assert_awaited_once()


@pytest.mark.anyio
async def test_grant_skill_for_all_halts_on_bot_mention():
    pipeline = TurnPipeline()
    channel_id = "C_TEST"
    thread_ts = "1726000000.2"

    post_mock = AsyncMock()
    pipeline.post_system_message_fn = post_mock

    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="<@U0812345> /grant_skill_for_all web_research",
    )
    directives = await directive_parser.parse(ctx.raw_text)
    ctx.actions = directives.actions
    ctx.raw_text_cleaned = directives.cleaned_prompt

    res = await pipeline.stage_validate_and_parse(ctx)
    assert res is not None
    assert res.get("ok") is True
    assert res.get("halted") is True
    assert res.get("action") == "grant_skill_for_all"


@pytest.mark.anyio
async def test_grant_skill_unconditionally_halts():
    pipeline = TurnPipeline()
    channel_id = "C_TEST"
    thread_ts = "1726000000.3"

    post_mock = AsyncMock()
    pipeline.post_system_message_fn = post_mock

    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="/grant_skill web_research SecurityArchitect And continue debate",
    )
    directives = await directive_parser.parse(ctx.raw_text)
    ctx.actions = directives.actions
    ctx.raw_text_cleaned = directives.cleaned_prompt

    res = await pipeline.stage_validate_and_parse(ctx)
    assert res is not None
    assert res.get("ok") is True
    assert res.get("halted") is True
    assert res.get("action") == "grant_skill"
    post_mock.assert_awaited_once()
    assert "Agent Skill Granted" in post_mock.call_args.kwargs["text"]


# ---------------------------------------------------------------------------
# 3. Validation Error Handling for /grant_skill
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_grant_skill_missing_params():
    pipeline = TurnPipeline()
    post_mock = AsyncMock()
    pipeline.post_system_message_fn = post_mock

    ctx = TurnContext(
        channel_id="C_TEST",
        thread_ts="1726000000.4",
        raw_text="/grant_skill",
    )
    directives = await directive_parser.parse(ctx.raw_text)
    ctx.actions = directives.actions

    res = await pipeline.stage_validate_and_parse(ctx)
    assert res.get("halted") is True
    assert res.get("action") == "grant_skill"
    assert "Missing Parameters" in post_mock.call_args.kwargs["text"]


@pytest.mark.anyio
async def test_grant_skill_missing_agent():
    pipeline = TurnPipeline()
    post_mock = AsyncMock()
    pipeline.post_system_message_fn = post_mock

    ctx = TurnContext(
        channel_id="C_TEST",
        thread_ts="1726000000.5",
        raw_text="/grant_skill web_research",
    )
    directives = await directive_parser.parse(ctx.raw_text)
    ctx.actions = directives.actions

    res = await pipeline.stage_validate_and_parse(ctx)
    assert res.get("halted") is True
    assert res.get("action") == "grant_skill"
    assert "Missing Agent" in post_mock.call_args.kwargs["text"]


@pytest.mark.anyio
async def test_grant_skill_unknown_skill():
    pipeline = TurnPipeline()
    post_mock = AsyncMock()
    pipeline.post_system_message_fn = post_mock

    ctx = TurnContext(
        channel_id="C_TEST",
        thread_ts="1726000000.6",
        raw_text="/grant_skill nonexistent_skill SecurityArchitect",
    )
    directives = await directive_parser.parse(ctx.raw_text)
    ctx.actions = directives.actions

    res = await pipeline.stage_validate_and_parse(ctx)
    assert res.get("halted") is True
    assert "Unknown or non-agent skill" in post_mock.call_args.kwargs["text"]


@pytest.mark.anyio
async def test_grant_skill_unknown_agent():
    pipeline = TurnPipeline()
    post_mock = AsyncMock()
    pipeline.post_system_message_fn = post_mock

    ctx = TurnContext(
        channel_id="C_TEST",
        thread_ts="1726000000.7",
        raw_text="/grant_skill web_research FakeAgentName",
    )
    directives = await directive_parser.parse(ctx.raw_text)
    ctx.actions = directives.actions

    res = await pipeline.stage_validate_and_parse(ctx)
    assert res.get("halted") is True
    assert "Unknown agent(s)" in post_mock.call_args.kwargs["text"]


# ---------------------------------------------------------------------------
# 4. ThreadSkillsManager Per-Agent Tests
# ---------------------------------------------------------------------------


def test_thread_skills_manager_agent_grants(temp_skills_mgr):
    mgr = temp_skills_mgr
    channel_id = "C_TEST"
    thread_ts = "1726000000.8"

    res, exp_ts = mgr.grant_agent_skills(channel_id, thread_ts, "web_research", ["SecurityArchitect"])
    assert "SecurityArchitect" in res
    assert res["SecurityArchitect"] == ["web_research"]
    assert exp_ts > time.time()

    # SecurityArchitect has skill
    assert mgr.get_active_agent_skills(channel_id, thread_ts, "SecurityArchitect") == ["web_research"]
    # Visionary does not
    assert mgr.get_active_agent_skills(channel_id, thread_ts, "Visionary") == []

    # Check all active
    all_active = mgr.get_all_active_agent_skills(channel_id, thread_ts)
    assert all_active.get("SecurityArchitect") == ["web_research"]
    assert "Visionary" not in all_active


def test_thread_skills_manager_agent_expiry(temp_skills_mgr):
    mgr = temp_skills_mgr
    channel_id = "C_TEST"
    thread_ts = "1726000000.9"

    mgr.grant_agent_skills(channel_id, thread_ts, "web_research", ["SecurityArchitect"])

    # Simulate 13 hours later (TTL is default 12h)
    future_time = time.time() + (13 * 3600)
    with patch("time.time", return_value=future_time):
        assert mgr.get_active_agent_skills(channel_id, thread_ts, "SecurityArchitect") == []
        assert mgr.get_expired_agent_skills(channel_id, thread_ts, "SecurityArchitect") == ["web_research"]


def test_thread_skills_manager_agent_revocation(temp_skills_mgr):
    mgr = temp_skills_mgr
    channel_id = "C_TEST"
    thread_ts = "1726000000.10"

    mgr.grant_agent_skills(channel_id, thread_ts, "web_research", ["SecurityArchitect", "Visionary"])
    assert mgr.get_active_agent_skills(channel_id, thread_ts, "SecurityArchitect") == ["web_research"]
    assert mgr.get_active_agent_skills(channel_id, thread_ts, "Visionary") == ["web_research"]

    # Revoke only SecurityArchitect
    mgr.revoke_skills(channel_id, thread_ts, ["web_research"], agent_key="SecurityArchitect")
    assert mgr.get_active_agent_skills(channel_id, thread_ts, "SecurityArchitect") == []
    # Visionary still has it
    assert mgr.get_active_agent_skills(channel_id, thread_ts, "Visionary") == ["web_research"]


# ---------------------------------------------------------------------------
# 5. Loader Authorizations with Agent Thread Grants
# ---------------------------------------------------------------------------


def test_loader_authorizations_with_agent_grant(temp_skills_mgr):
    channel_id = "C_TEST"
    thread_ts = "1726000000.11"

    temp_skills_mgr.grant_agent_skills(channel_id, thread_ts, "web_research", ["SecurityArchitect"])

    with patch("orchestration.thread_skills_manager.thread_skills_manager", temp_skills_mgr):
        auth_map = skill_registry.get_skill_agent_mapping(channel_id, thread_ts)
        wr_agents = auth_map.get("web_research", {}).get("authorized_agents", [])
        assert any("SecurityArchitect" in a for a in wr_agents)
