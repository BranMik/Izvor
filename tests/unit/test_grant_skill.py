import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from orchestration.thread_skills_manager import ThreadSkillsManager, thread_skills_manager
from orchestration.turn_pipeline import TurnContext, TurnPipeline
from pipeline.directive_parser import directive_parser
from core.registry import get_skill_registry
skill_registry = get_skill_registry()


# ---------------------------------------------------------------------------
# 1. Directive Parser Tests for /grant_skill and /grant_for_all
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_directive_parser_grant_skill_basic():
    raw = "/grant_skill web_research agent-10008"
    parsed = await directive_parser.parse(raw)
    act = next((a for a in parsed.actions if a.name == "grant_skill"), None)
    assert act is not None
    assert act.params.get("skill") == "web_research"
    assert act.params.get("agents") == ["agent-10008"]


@pytest.mark.anyio
async def test_directive_parser_grant_skill_bracketed():
    raw = "/grant_skill [web_research] [agent-10008]"
    parsed = await directive_parser.parse(raw)
    act = next((a for a in parsed.actions if a.name == "grant_skill"), None)
    assert act is not None
    assert act.params.get("skill") == "web_research"
    assert act.params.get("agents") == ["agent-10008"]


@pytest.mark.anyio
async def test_directive_parser_grant_skill_multiple_agents():
    raw = "/grant_skill [web_research] [agent-10008] [agent-10001]"
    parsed = await directive_parser.parse(raw)
    act = next((a for a in parsed.actions if a.name == "grant_skill"), None)
    assert act is not None
    assert act.params.get("skill") == "web_research"
    assert "agent-10008" in act.params.get("agents", [])
    assert "agent-10001" in act.params.get("agents", [])


@pytest.mark.anyio
async def test_directive_parser_grant_skill_alias():
    raw = "/grant-skill web_research agent-10008"
    parsed = await directive_parser.parse(raw)
    act = next((a for a in parsed.actions if a.name == "grant_skill"), None)
    assert act is not None
    assert act.params.get("skill") == "web_research"
    assert act.params.get("agents") == ["agent-10008"]


@pytest.mark.anyio
async def test_directive_parser_grant_skill_trailing_text_ignored():
    raw = "/grant_skill web_research agent-10008 and please tell me about python"
    parsed = await directive_parser.parse(raw)
    act = next((a for a in parsed.actions if a.name == "grant_skill"), None)
    assert act is not None
    assert act.params.get("skill") == "web_research"
    assert act.params.get("agents") == ["agent-10008"]


# ---------------------------------------------------------------------------
# 2. Exclusivity Tests: /grant_for_all and /grant_skill Unconditionally Halt
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def configure_admin_env():
    from core.config import set_admin_slack_user_ids_override
    set_admin_slack_user_ids_override("U_ADMIN")
    yield
    set_admin_slack_user_ids_override(None)


@pytest.mark.anyio
async def test_grant_skill_for_all_standalone_halts():
    pipeline = TurnPipeline()
    channel_id = "C_TEST"
    thread_ts = "1726000000.1"

    post_mock = AsyncMock()
    pipeline.post_system_message_fn = post_mock

    # Pure directive without prompt halts cleanly
    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        user_id="U_ADMIN",
        raw_text="/grant_skill_for_all web_research",
    )
    directives = await directive_parser.parse(ctx.input.raw_text)
    ctx.actions = directives.actions
    ctx.raw_text_cleaned = directives.cleaned_prompt

    res = await pipeline.validation_stage.stage_validate_and_parse(ctx)
    assert res is not None
    assert res.get("ok") is True
    assert res.get("halted") is True
    assert res.get("action") == "grant_skill_for_all"
    post_mock.assert_awaited_once()
    assert "Thread-Global Skills Granted" in post_mock.call_args.kwargs["text"]


@pytest.mark.anyio
async def test_grant_skill_for_all_with_prompt_continues():
    pipeline = TurnPipeline()
    channel_id = "C_TEST"
    thread_ts = "1726000000.15"

    post_mock = AsyncMock()
    pipeline.post_system_message_fn = post_mock

    # Combined directive with substantive prompt must not halt
    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        user_id="U_ADMIN",
        raw_text="/grant_skill_for_all web_research Now discuss everything",
    )
    directives = await directive_parser.parse(ctx.input.raw_text)
    ctx.actions = directives.actions
    ctx.raw_text_cleaned = directives.cleaned_prompt

    res = await pipeline.validation_stage.stage_validate_and_parse(ctx)
    assert res is None
    post_mock.assert_awaited_once()
    assert "Thread-Global Skills Granted" in post_mock.call_args.kwargs["text"]
    assert "web_research" in thread_skills_manager.get_active_skills(channel_id, thread_ts)
    assert ctx.raw_text_cleaned == "Now discuss everything"


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
        user_id="U_ADMIN",
        raw_text="<@U0812345> /grant_skill_for_all web_research",
    )
    directives = await directive_parser.parse(ctx.input.raw_text)
    ctx.actions = directives.actions
    ctx.raw_text_cleaned = directives.cleaned_prompt

    res = await pipeline.validation_stage.stage_validate_and_parse(ctx)
    assert res is not None
    assert res.get("ok") is True
    assert res.get("halted") is True
    assert res.get("action") == "grant_skill_for_all"


@pytest.mark.anyio
async def test_grant_skill_standalone_halts():
    pipeline = TurnPipeline()
    channel_id = "C_TEST"
    thread_ts = "1726000000.3"

    post_mock = AsyncMock()
    pipeline.post_system_message_fn = post_mock

    # Pure directive without prompt halts cleanly
    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        user_id="U_ADMIN",
        raw_text="/grant_skill web_research SecurityArchitect",
    )
    directives = await directive_parser.parse(ctx.input.raw_text)
    ctx.actions = directives.actions
    ctx.raw_text_cleaned = directives.cleaned_prompt

    res = await pipeline.validation_stage.stage_validate_and_parse(ctx)
    assert res is not None
    assert res.get("ok") is True
    assert res.get("halted") is True
    assert res.get("action") == "grant_skill"
    post_mock.assert_awaited_once()
    assert "Agent Skill Granted" in post_mock.call_args.kwargs["text"]


@pytest.mark.anyio
async def test_grant_skill_with_prompt_continues():
    pipeline = TurnPipeline()
    channel_id = "C_TEST"
    thread_ts = "1726000000.35"

    post_mock = AsyncMock()
    pipeline.post_system_message_fn = post_mock

    # Combined directive with prompt must not halt
    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        user_id="U_ADMIN",
        raw_text="/grant_skill web_research SecurityArchitect And continue debate",
    )
    directives = await directive_parser.parse(ctx.input.raw_text)
    ctx.actions = directives.actions
    ctx.raw_text_cleaned = directives.cleaned_prompt

    res = await pipeline.validation_stage.stage_validate_and_parse(ctx)
    assert res is None
    post_mock.assert_awaited_once()
    assert "Agent Skill Granted" in post_mock.call_args.kwargs["text"]
    assert "web_research" in thread_skills_manager.get_active_agent_skills(channel_id, thread_ts, "agent-10008")
    assert ctx.raw_text_cleaned == "And continue debate"


@pytest.mark.anyio
async def test_revoke_skill_for_all_standalone_halts():
    pipeline = TurnPipeline()
    channel_id = "C_TEST"
    thread_ts = "1726000000.36"

    post_mock = AsyncMock()
    pipeline.post_system_message_fn = post_mock

    thread_skills_manager.grant_skills(channel_id, thread_ts, ["web_research"])
    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        user_id="U_ADMIN",
        raw_text="/revoke_skill_for_all web_research",
    )
    directives = await directive_parser.parse(ctx.input.raw_text)
    ctx.actions = directives.actions
    ctx.raw_text_cleaned = directives.cleaned_prompt

    res = await pipeline.validation_stage.stage_validate_and_parse(ctx)
    assert res is not None
    assert res.get("ok") is True
    assert res.get("halted") is True
    assert res.get("action") == "revoke_skill_for_all"
    post_mock.assert_awaited_once()
    assert "Revoked" in post_mock.call_args.kwargs["text"]
    assert "web_research" not in thread_skills_manager.get_active_skills(channel_id, thread_ts)


@pytest.mark.anyio
async def test_revoke_skill_for_all_with_prompt_continues():
    pipeline = TurnPipeline()
    channel_id = "C_TEST"
    thread_ts = "1726000000.37"

    post_mock = AsyncMock()
    pipeline.post_system_message_fn = post_mock

    thread_skills_manager.grant_skills(channel_id, thread_ts, ["web_research"])
    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        user_id="U_ADMIN",
        raw_text="/revoke_skill_for_all web_research Now discuss from memory",
    )
    directives = await directive_parser.parse(ctx.input.raw_text)
    ctx.actions = directives.actions
    ctx.raw_text_cleaned = directives.cleaned_prompt

    res = await pipeline.validation_stage.stage_validate_and_parse(ctx)
    assert res is None
    post_mock.assert_awaited_once()
    assert "Revoked" in post_mock.call_args.kwargs["text"]
    assert "web_research" not in thread_skills_manager.get_active_skills(channel_id, thread_ts)
    assert ctx.raw_text_cleaned == "Now discuss from memory"


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
        user_id="U_ADMIN",
        raw_text="/grant_skill",
    )
    directives = await directive_parser.parse(ctx.input.raw_text)
    ctx.actions = directives.actions

    res = await pipeline.validation_stage.stage_validate_and_parse(ctx)
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
        user_id="U_ADMIN",
        raw_text="/grant_skill web_research",
    )
    directives = await directive_parser.parse(ctx.input.raw_text)
    ctx.actions = directives.actions

    res = await pipeline.validation_stage.stage_validate_and_parse(ctx)
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
        user_id="U_ADMIN",
        raw_text="/grant_skill nonexistent_skill SecurityArchitect",
    )
    directives = await directive_parser.parse(ctx.input.raw_text)
    ctx.actions = directives.actions

    res = await pipeline.validation_stage.stage_validate_and_parse(ctx)
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
        user_id="U_ADMIN",
        raw_text="/grant_skill web_research FakeAgentName",
    )
    directives = await directive_parser.parse(ctx.input.raw_text)
    ctx.actions = directives.actions

    res = await pipeline.validation_stage.stage_validate_and_parse(ctx)
    assert res.get("halted") is True
    assert "Unknown agent(s)" in post_mock.call_args.kwargs["text"]


@pytest.mark.anyio
async def test_grant_skill_for_all_non_admin_denied():
    pipeline = TurnPipeline()
    post_mock = AsyncMock()
    pipeline.post_system_message_fn = post_mock

    ctx = TurnContext(
        channel_id="C_TEST",
        thread_ts="1726000000.100",
        user_id="U_REGULAR",
        raw_text="/grant_skill_for_all web_research",
    )
    directives = await directive_parser.parse(ctx.input.raw_text)
    ctx.actions = directives.actions

    with patch("services.audit_logger.log_audit_event") as mock_audit:
        res = await pipeline.validation_stage.stage_validate_and_parse(ctx)
        assert res is not None
        assert res.get("ok") is False
        assert res.get("halted") is True
        assert res.get("reason") == "unauthorized"
        post_mock.assert_awaited_once()
        assert "Permission Denied" in post_mock.call_args.kwargs["text"]

        audit_calls = [c for c in mock_audit.call_args_list if c.kwargs.get("event_type") == "UNAUTHORIZED_DIRECTIVE_ATTEMPT"]
        assert len(audit_calls) >= 1
        assert audit_calls[0].kwargs["action"] == "grant_skill_for_all"
        assert audit_calls[0].kwargs["severity"] == "CRITICAL"


@pytest.mark.anyio
async def test_grant_skill_non_admin_denied():
    pipeline = TurnPipeline()
    post_mock = AsyncMock()
    pipeline.post_system_message_fn = post_mock

    ctx = TurnContext(
        channel_id="C_TEST",
        thread_ts="1726000000.101",
        user_id="U_REGULAR",
        raw_text="/grant_skill web_research SecurityArchitect",
    )
    directives = await directive_parser.parse(ctx.input.raw_text)
    ctx.actions = directives.actions

    with patch("services.audit_logger.log_audit_event") as mock_audit:
        res = await pipeline.validation_stage.stage_validate_and_parse(ctx)
        assert res is not None
        assert res.get("ok") is False
        assert res.get("halted") is True
        assert res.get("reason") == "unauthorized"
        post_mock.assert_awaited_once()
        assert "Permission Denied" in post_mock.call_args.kwargs["text"]
        audit_calls = [c for c in mock_audit.call_args_list if c.kwargs.get("event_type") == "UNAUTHORIZED_DIRECTIVE_ATTEMPT"]
        assert len(audit_calls) >= 1


@pytest.mark.anyio
async def test_revoke_skill_for_all_non_admin_denied():
    pipeline = TurnPipeline()
    post_mock = AsyncMock()
    pipeline.post_system_message_fn = post_mock

    ctx = TurnContext(
        channel_id="C_TEST",
        thread_ts="1726000000.102",
        user_id="U_REGULAR",
        raw_text="/revoke_skill_for_all web_research",
    )
    directives = await directive_parser.parse(ctx.input.raw_text)
    ctx.actions = directives.actions

    with patch("services.audit_logger.log_audit_event") as mock_audit:
        res = await pipeline.validation_stage.stage_validate_and_parse(ctx)
        assert res is not None
        assert res.get("ok") is False
        assert res.get("halted") is True
        assert res.get("reason") == "unauthorized"
        post_mock.assert_awaited_once()
        assert "Permission Denied" in post_mock.call_args.kwargs["text"]
        audit_calls = [c for c in mock_audit.call_args_list if c.kwargs.get("event_type") == "UNAUTHORIZED_DIRECTIVE_ATTEMPT"]
        assert len(audit_calls) >= 1


@pytest.mark.anyio
async def test_revoke_constraint_non_admin_denied():
    pipeline = TurnPipeline()
    post_mock = AsyncMock()
    pipeline.post_system_message_fn = post_mock

    ctx = TurnContext(
        channel_id="C_TEST",
        thread_ts="1726000000.103",
        user_id="U_REGULAR",
        raw_text="/revoke_constraint max_words",
    )
    directives = await directive_parser.parse(ctx.input.raw_text)
    ctx.actions = directives.actions
    ctx.revoke_target = directives.revoke_target

    with patch("services.audit_logger.log_audit_event") as mock_audit:
        res = await pipeline.validation_stage.stage_validate_and_parse(ctx)
        assert res is not None
        assert res.get("ok") is False
        assert res.get("halted") is True
        assert res.get("reason") == "unauthorized"
        post_mock.assert_awaited_once()
        assert "Permission Denied" in post_mock.call_args.kwargs["text"]
        audit_calls = [c for c in mock_audit.call_args_list if c.kwargs.get("event_type") == "UNAUTHORIZED_DIRECTIVE_ATTEMPT"]
        assert len(audit_calls) >= 1


@pytest.mark.anyio
async def test_custom_skill_requires_admin_blocked_for_non_admin():
    from skills.loader import SkillDefinition
    pipeline = TurnPipeline()
    post_mock = AsyncMock()
    pipeline.post_system_message_fn = post_mock

    custom_admin_skill = SkillDefinition(
        name="custom_admin_op",
        display_name="Custom Admin Op",
        description="Dangerous admin operation",
        scope="orchestrator",
        requires_admin=True,
        triggers={"slash": ["/custom_admin_op"]},
    )
    skill_registry._skills["custom_admin_op"] = custom_admin_skill
    try:
        ctx = TurnContext(
            channel_id="C_TEST",
            thread_ts="1726000000.999",
            user_id="U_REGULAR",
            raw_text="/custom_admin_op",
        )

        with (
            patch("services.audit_logger.log_audit_event") as mock_audit,
            patch("orchestration.stages.validation.fetch_thread_files", AsyncMock(return_value=[])),
        ):
            res = await pipeline.validation_stage.stage_validate_and_parse(ctx)
            assert res is not None
            assert res.get("ok") is False
            assert res.get("halted") is True
            assert res.get("reason") == "unauthorized"
            post_mock.assert_awaited_once()
            assert "Permission Denied" in post_mock.call_args.kwargs["text"]
            assert any(
                c.kwargs.get("event_type") == "UNAUTHORIZED_DIRECTIVE_ATTEMPT"
                and c.kwargs.get("action") == "custom_admin_op"
                for c in mock_audit.call_args_list
            )
    finally:
        skill_registry._skills.pop("custom_admin_op", None)


# ---------------------------------------------------------------------------
# 4. ThreadSkillsManager Per-Agent Tests
# ---------------------------------------------------------------------------


def test_thread_skills_manager_agent_grants():
    mgr = thread_skills_manager
    channel_id = "C_TEST"
    thread_ts = "1726000000.8"

    res, exp_ts = mgr.grant_agent_skills(channel_id, thread_ts, "web_research", ["agent-10008"])
    assert "agent-10008" in res
    assert res["agent-10008"] == ["web_research"]
    assert exp_ts > time.time()

    # SecurityArchitect has skill
    assert mgr.get_active_agent_skills(channel_id, thread_ts, "agent-10008") == ["web_research"]
    # Visionary does not
    assert mgr.get_active_agent_skills(channel_id, thread_ts, "agent-10001") == []

    # Check all active
    all_active = mgr.get_all_active_agent_skills(channel_id, thread_ts)
    assert all_active.get("agent-10008") == ["web_research"]
    assert "agent-10001" not in all_active


def test_thread_skills_manager_agent_expiry():
    from core.database import get_db_transaction

    mgr = thread_skills_manager
    channel_id = "C_TEST"
    thread_ts = "1726000000.9"

    mgr.grant_agent_skills(channel_id, thread_ts, "web_research", ["agent-10008"])

    # Simulate expiry by backdating SQLite record
    with get_db_transaction(immediate=True) as conn:
        conn.execute(
            "UPDATE thread_skills SET expires_at = ? WHERE channel_id = ? AND thread_ts = ?",
            (time.time() - 3600.0, channel_id, thread_ts),
        )

    assert mgr.get_active_agent_skills(channel_id, thread_ts, "agent-10008") == []
    assert mgr.get_expired_agent_skills(channel_id, thread_ts, "agent-10008") == ["web_research"]


def test_thread_skills_manager_agent_revocation():
    mgr = thread_skills_manager
    channel_id = "C_TEST"
    thread_ts = "1726000000.10"

    mgr.grant_agent_skills(channel_id, thread_ts, "web_research", ["agent-10008", "agent-10001"])
    assert mgr.get_active_agent_skills(channel_id, thread_ts, "agent-10008") == ["web_research"]
    assert mgr.get_active_agent_skills(channel_id, thread_ts, "agent-10001") == ["web_research"]

    # Revoke only SecurityArchitect
    mgr.revoke_skills(channel_id, thread_ts, ["web_research"], agent_key="agent-10008")
    assert mgr.get_active_agent_skills(channel_id, thread_ts, "agent-10008") == []
    # Visionary still has it
    assert mgr.get_active_agent_skills(channel_id, thread_ts, "agent-10001") == ["web_research"]


# ---------------------------------------------------------------------------
# 5. Loader Authorizations with Agent Thread Grants
# ---------------------------------------------------------------------------


def test_loader_authorizations_with_agent_grant():
    channel_id = "C_TEST"
    thread_ts = "1726000000.11"

    thread_skills_manager.grant_agent_skills(channel_id, thread_ts, "web_research", ["agent-10008"])

    auth_map = skill_registry.get_skill_agent_mapping(channel_id, thread_ts)
    wr_agents = auth_map.get("web_research", {}).get("authorized_agents", [])
    assert any("agent-10008" in a for a in wr_agents)
