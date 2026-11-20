import pytest
from unittest.mock import AsyncMock, MagicMock

from orchestration.thread_state import thread_state_manager
from orchestration.turn_pipeline import TurnContext, TurnPipeline
from orchestration.stages.post_record import (
    PostRecordStage,
    _extract_resolved_nomination_keys,
)


def test_extract_resolved_nomination_keys():
    text = "Great insight! Now over to /[The Visionary] and /[The Sceptic]."
    keys = _extract_resolved_nomination_keys(text)
    # The Visionary resolves to agent-10001, The Sceptic resolves to agent-10003
    assert "agent-10001" in keys
    assert "agent-10003" in keys


@pytest.mark.anyio
async def test_display_name_nomination_preserves_round_and_pops_retry():
    channel_id = "C_NOM_TEST"
    thread_ts = "1700000088.8888"

    # Setup retry queue with agent-10003 (Sceptic)
    thread_state_manager.cleanup_thread(channel_id, thread_ts)
    thread_state_manager.add_to_retry_queue(channel_id, thread_ts, "agent-10003")
    assert thread_state_manager.has_pending_retries(channel_id, thread_ts) is True

    # Setup TurnContext where remaining is empty, but agent nominates by display name
    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="Test prompt",
        user_id="U123",
    )
    ctx.agent_key = "agent-10001"
    ctx.agent_cfg = {"username": "Visionary", "model": "test-model"}
    ctx.remaining = []
    ctx.eligible_remaining_agents = []
    ctx.output.final_text = "I nominate /[The Sceptic] to share critical thoughts."

    # Pipeline and post_agent_response
    mock_post = AsyncMock(return_value={"ts": "1700000088.9999"})
    mock_pipeline = TurnPipeline(
        sleep_fn=AsyncMock(),
        slack_poster=mock_post,
        usage_updater=MagicMock(),
        notes_updater=MagicMock(),
    )
    stage = PostRecordStage(mock_pipeline.deps)

    await stage.stage_post_and_record(ctx)

    # 1. Retry queue for agent-10003 should have been popped
    assert thread_state_manager.has_pending_retries(channel_id, thread_ts) is False

    # 2. generated_tags should contain canonical key 'agent-10003'
    assert "agent-10003" in ctx.output.generated_tags

    # 3. Discussion should NOT be marked completed because has_nominated_next is True
    assert ctx.output.is_completed is False


def test_enforce_valid_nomination_blocks_self_alias():
    """Plan 201: enforce_valid_nomination never nominates an alias resolving to current_agent_key."""
    from orchestration.nomination import enforce_valid_nomination

    result = enforce_valid_nomination(
        "Threat model complete. /[SecurityArchitect]",
        current_agent_key="agent-10008",
        remaining=["security_architect"],
        force_termination=False,
    )
    assert "/[security_architect]" not in result
    assert "/[agent-10008]" not in result
    assert "/[SecurityArchitect]" not in result
    assert "Threat model complete." in result


def test_parse_agent_and_prompt_removes_alias_from_remaining():
    """Plan 201: parse_agent_and_prompt removes lingering aliases matching the resolved speaker."""
    from orchestration.nomination import parse_agent_and_prompt

    channel_id = "C_NOM_ALIAS"
    thread_ts = "1700000099.0001"
    thread_state_manager.cleanup_thread(channel_id, thread_ts)
    thread_state_manager.set_remaining_agents(channel_id, thread_ts, ["security_architect", "agent-10011"])

    agent_key, prompt, was_explicit = parse_agent_and_prompt(
        "/[Security Architect] Analyze replay guarantees.",
        channel_id,
        thread_ts,
    )
    assert agent_key == "agent-10008"
    assert was_explicit is True
    assert prompt == "Analyze replay guarantees."
    assert thread_state_manager.get_remaining_agents(channel_id, thread_ts) == ["agent-10011"]


@pytest.mark.anyio
async def test_post_record_completes_when_only_self_alias_remains():
    """Plan 201: PostRecordStage clears lingering self-aliases and completes cleanly."""
    channel_id = "C_NOM_SELF_ALIAS"
    thread_ts = "1700000099.0002"
    thread_state_manager.cleanup_thread(channel_id, thread_ts)
    thread_state_manager.set_remaining_agents(channel_id, thread_ts, ["security_architect"])

    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="Analyze replay guarantees.",
        user_id="U123",
    )
    ctx.agent_key = "agent-10008"
    ctx.agent_cfg = {"username": "SecurityArchitect", "model": "test-model"}
    ctx.remaining = ["security_architect"]
    ctx.eligible_remaining_agents = ["security_architect"]
    ctx.output.final_text = "Replay guarantees verified."

    mock_post = AsyncMock(return_value={"ok": True, "ts": "1700000099.9999"})
    mock_pipeline = TurnPipeline(
        sleep_fn=AsyncMock(),
        slack_poster=mock_post,
        usage_updater=MagicMock(),
        notes_updater=MagicMock(),
    )
    stage = PostRecordStage(mock_pipeline.deps)

    await stage.stage_post_and_record(ctx)

    assert ctx.output.is_completed is True
    assert ctx.output.generated_tags == []
    assert thread_state_manager.get_remaining_agents(channel_id, thread_ts) == []
