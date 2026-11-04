from unittest.mock import AsyncMock
import pytest

from orchestration.nomination import get_initial_agents
from orchestration.thread_state import thread_state_manager
from orchestration.turn_pipeline import TurnContext, TurnPipeline
from pipeline.orchestrator import SandwichOrchestrator


@pytest.fixture(autouse=True)
def clean_thread_state():
    """Ensure clean thread state before each test."""
    thread_state_manager.reset_all()
    yield
    thread_state_manager.reset_all()


@pytest.mark.anyio
async def test_first_skill_execution_success():
    pipeline = TurnPipeline()
    channel_id = "C_FIRST"
    thread_ts = "1727000001.0"
    pipeline.post_system_message_fn = AsyncMock()

    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="/first [agent-10001] What is our plan for this quarter?",
    )

    res = await pipeline.validation_stage.stage_validate_and_parse(ctx)
    assert res is None
    assert ctx.agent_key == "agent-10001"
    assert ctx.was_explicit_target is True
    assert ctx.is_open_debate is False
    assert thread_state_manager.is_open_debate(channel_id, thread_ts) is False

    remaining = thread_state_manager.get_remaining_agents(channel_id, thread_ts)
    assert "agent-10001" not in remaining
    initial_all = get_initial_agents()
    assert len(remaining) == len(initial_all) - 1


@pytest.mark.anyio
async def test_first_skill_missing_agent():
    pipeline = TurnPipeline()
    channel_id = "C_FIRST"
    thread_ts = "1727000002.0"
    post_mock = AsyncMock()
    pipeline.post_system_message_fn = post_mock

    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="/first",
    )

    res = await pipeline.validation_stage.stage_validate_and_parse(ctx)
    assert res is not None
    assert res.get("ok") is True
    assert res.get("halted") is True
    assert res.get("action") == "first"
    post_mock.assert_awaited_once()
    call_args = post_mock.call_args[1]
    assert "Missing Agent" in call_args.get("text", "")


@pytest.mark.anyio
async def test_first_skill_unrecognized_agent():
    pipeline = TurnPipeline()
    channel_id = "C_FIRST"
    thread_ts = "1727000003.0"
    post_mock = AsyncMock()
    pipeline.post_system_message_fn = post_mock

    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="/first [NonExistentAgent] What do you think?",
    )

    res = await pipeline.validation_stage.stage_validate_and_parse(ctx)
    assert res is not None
    assert res.get("halted") is True
    assert res.get("reason") == "unrecognized_agent"
    post_mock.assert_awaited_once()


@pytest.mark.anyio
async def test_open_debate_skill_execution_success():
    pipeline = TurnPipeline()
    channel_id = "C_OPEN"
    thread_ts = "1727000004.0"
    pipeline.post_system_message_fn = AsyncMock()

    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="/open_debate [agent-10002] Let's discuss freely without constraints",
    )

    res = await pipeline.validation_stage.stage_validate_and_parse(ctx)
    assert res is None
    assert ctx.agent_key == "agent-10002"
    assert ctx.was_explicit_target is True
    assert ctx.is_open_debate is True
    assert thread_state_manager.is_open_debate(channel_id, thread_ts) is True

    remaining = thread_state_manager.get_remaining_agents(channel_id, thread_ts)
    assert "agent-10002" not in remaining
    initial_all = get_initial_agents()
    assert len(remaining) == len(initial_all) - 1


@pytest.mark.anyio
async def test_open_debate_missing_agent():
    pipeline = TurnPipeline()
    channel_id = "C_OPEN"
    thread_ts = "1727000005.0"
    post_mock = AsyncMock()
    pipeline.post_system_message_fn = post_mock

    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="/open_debate",
    )

    res = await pipeline.validation_stage.stage_validate_and_parse(ctx)
    assert res is not None
    assert res.get("ok") is True
    assert res.get("halted") is True
    assert res.get("action") == "open_debate"
    post_mock.assert_awaited_once()
    call_args = post_mock.call_args[1]
    assert "Missing Agent" in call_args.get("text", "")


def test_open_debate_sandwich_phase_info():
    orchestrator = SandwichOrchestrator(
        start_chain=["agent-10001"],
        end_chain=["agent-10008"],
    )

    # In standard sandwich mode with start_chain, first is designated
    pool = ["agent-10001", "agent-10002", "agent-10008"]
    desig_std, elig_std = orchestrator.get_phase_info(pool, is_open_debate=False)
    assert desig_std == "agent-10001"

    # In open debate mode with > 1 agent, designation is None (dynamic nomination)
    desig_open, elig_open = orchestrator.get_phase_info(pool, is_open_debate=True)
    assert desig_open is None
    assert elig_open == pool

    # In open debate mode with exactly 1 remaining agent, designated is the final agent
    last_pool = ["agent-10008"]
    desig_last, elig_last = orchestrator.get_phase_info(last_pool, is_open_debate=True)
    assert desig_last == "agent-10008"
    assert elig_last == ["agent-10008"]


@pytest.mark.anyio
async def test_direct_tag_single_agent_does_not_trigger_full_nomination_without_skill():
    pipeline = TurnPipeline()
    channel_id = "C_DIRECT"
    thread_ts = "1727000006.0"
    pipeline.post_system_message_fn = AsyncMock()

    # Old regex would have matched "lead the debate" and started the whole panel
    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="/[agent-10008] please lead the debate and nominate who speaks next",
    )

    res = await pipeline.validation_stage.stage_validate_and_parse(ctx)
    assert res is None
    assert ctx.agent_key == "agent-10008"
    assert ctx.was_explicit_target is True
    # In direct single-agent tagging, remaining agents must be empty (single-agent response)
    remaining = thread_state_manager.get_remaining_agents(channel_id, thread_ts)
    assert remaining == []
    assert thread_state_manager.is_custom_flow(channel_id, thread_ts) is True
    assert thread_state_manager.is_open_debate(channel_id, thread_ts) is False
