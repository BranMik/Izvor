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
async def test_display_name_nomination_preserves_round_and_pops_retry(monkeypatch):
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
    ctx.final_text = "I nominate /[The Sceptic] to share critical thoughts."

    # Pipeline and post_agent_response
    mock_post = AsyncMock(return_value={"ts": "1700000088.9999"})
    mock_pipeline = TurnPipeline(sleep_fn=AsyncMock(), slack_poster=mock_post)
    stage = PostRecordStage(mock_pipeline.deps)
    monkeypatch.setattr(
        "orchestration.stages.post_record.trigger_usage_update",
        MagicMock(),
    )
    monkeypatch.setattr(
        "orchestration.stages.post_record.trigger_thread_notes_update",
        MagicMock(),
    )

    await stage.stage_post_and_record(ctx)

    # 1. Retry queue for agent-10003 should have been popped
    assert thread_state_manager.has_pending_retries(channel_id, thread_ts) is False

    # 2. generated_tags should contain canonical key 'agent-10003'
    assert "agent-10003" in ctx.generated_tags

    # 3. Discussion should NOT be marked completed because has_nominated_next is True
    assert ctx.is_completed is False
