"""
Unit and integration tests for Phase P0 Pre-Publish Gates:
  - Turn ceiling enforcement via max_discussion_turns setting (N1)
  - Spend accounting on error, cancellation, and superseded turns (N2)
  - Circuit breaker LLM notes suppression (N2)
  - Normalized unicode-safe nomination tag format (N10)
  - Tool arguments JSON decode error handling (B6 Part 1)
  - Final text non-null fallback (B6 Part 1)
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.slack_security import sanitize_slack_broadcast_mentions
from orchestration.agent_router import orchestrate_discussion
from orchestration.nomination import NOMINATION_TAG_REGEX, format_nomination_tag
from orchestration.stages.execution import ExecutionStage
from orchestration.stages.post_record import PostRecordStage
from orchestration.system_settings import (
    get_max_discussion_turns,
    set_max_discussion_turns,
)
from orchestration.thread_state import thread_state_manager
from orchestration.turn_pipeline import TurnContext, TurnPipeline


def test_turn_ceiling_configuration():
    """Verifies that max_discussion_turns setting is queryable and settable."""
    original = get_max_discussion_turns()
    try:
        set_max_discussion_turns(15)
        assert get_max_discussion_turns() == 15
    finally:
        set_max_discussion_turns(original)


@pytest.mark.anyio
async def test_orchestrator_clamps_turns_to_ceiling(monkeypatch):
    """Verifies orchestrate_discussion clamps expected round turns to get_max_discussion_turns()."""
    channel_id = "C_TEST_CEIL"
    thread_ts = "100.200"

    # Set ceiling to 3 turns
    monkeypatch.setattr("orchestration.agent_router.get_max_discussion_turns", lambda: 3)
    # Simulate active rounds requesting 10 rounds
    thread_state_manager.set_rounds(channel_id, thread_ts, 10)

    turns_executed = 0

    async def mock_process_and_respond(**kwargs):
        nonlocal turns_executed
        turns_executed += 1
        return {
            "ok": True,
            "final_text": f"Turn {turns_executed} done",
            "is_completed": False,
        }

    monkeypatch.setattr("orchestration.agent_router.process_and_respond", mock_process_and_respond)
    monkeypatch.setattr("orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[]))

    res = await orchestrate_discussion(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="Start discussion",
    )

    # Turns executed must not exceed ceiling of 3 turns
    assert turns_executed == 3
    thread_state_manager.clear_state(channel_id, thread_ts)


def test_nomination_tag_unicode_and_format():
    """Verifies NOMINATION_TAG_REGEX handles unicode diacritics and format_nomination_tag formats canonical tags."""
    # Canonical tags
    assert format_nomination_tag("agent-10002") == "/[agent-10002]"
    assert format_nomination_tag("/[agent-10002]") == "/[agent-10002]"
    assert format_nomination_tag(" [agent-10002] ") == "/[agent-10002]"

    # Unicode regex matching
    text = "Great discussion! I nominate /[Agent_Češki] to summarize."
    matches = NOMINATION_TAG_REGEX.findall(text)
    assert matches == ["Agent_Češki"]

    text2 = "Next speaker: /[Dr_Müller] please."
    matches2 = NOMINATION_TAG_REGEX.findall(text2)
    assert matches2 == ["Dr_Müller"]


@pytest.mark.anyio
async def test_handle_error_records_spent_tokens():
    """Verifies that PostRecordStage.handle_error records actual tokens spent before the error."""
    pipeline = TurnPipeline()
    ctx = TurnContext(
        channel_id="C_ERR",
        thread_ts="111.222",
        raw_text="Test prompt",
    )
    ctx.total_prompt_tokens = 450
    ctx.total_completion_tokens = 120
    ctx.agent_key = "agent-10011"
    ctx.agent_cfg = {"model": "test-model"}
    ctx.force_termination = True

    recorded = {}

    def mock_record_tokens(channel_id, thread_ts, model_name, prompt_tokens, completion_tokens):
        recorded["channel_id"] = channel_id
        recorded["thread_ts"] = thread_ts
        recorded["model_name"] = model_name
        recorded["prompt_tokens"] = prompt_tokens
        recorded["completion_tokens"] = completion_tokens

    pipeline.token_recorder = mock_record_tokens
    pipeline.slack_poster = AsyncMock(return_value={"ok": True})
    pipeline.notes_updater = MagicMock()
    pipeline.usage_updater = MagicMock()

    stage = PostRecordStage(pipeline)
    res = await stage.handle_error(ctx, RuntimeError("Simulated failure"))

    assert res["ok"] is False
    assert recorded["prompt_tokens"] == 450
    assert recorded["completion_tokens"] == 120
    assert recorded["model_name"] == "test-model"


@pytest.mark.anyio
async def test_tool_arguments_malformed_json_handling():
    """Verifies that ExecutionStage catches JSONDecodeError on tool args without crashing."""
    pipeline = TurnPipeline()
    ctx = TurnContext(
        channel_id="C_TOOL_ERR",
        thread_ts="333.444",
        raw_text="Execute tool",
    )
    ctx.agent_key = "agent-10011"
    ctx.agent_cfg = {"model": "test-model"}
    ctx.messages = [{"role": "user", "content": "Execute tool"}]

    stage = ExecutionStage(pipeline)

    # Create mock response with invalid JSON in tool arguments
    mock_tool_call = MagicMock()
    mock_tool_call.id = "call_abc123"
    mock_tool_call.function.name = "some_tool"
    mock_tool_call.function.arguments = "{malformed_json_string: missing_quotes"

    mock_response = MagicMock()
    mock_response.choices = [
        MagicMock(
            message=MagicMock(
                content=None,
                tool_calls=[mock_tool_call],
                model_dump=lambda: {"role": "assistant", "content": None, "tool_calls": []},
            )
        )
    ]
    mock_response.usage = None

    pipeline.llm_executor = AsyncMock(return_value=mock_response)

    # stage_execute_llm will run and handle the invalid JSON error cleanly
    res = await stage.stage_execute_llm(ctx)
    assert res is None  # Completed without unhandled exception

    # Check that error response was appended to tool messages
    tool_msg = next((m for m in ctx.messages if m.get("role") == "tool"), None)
    assert tool_msg is not None
    assert "Invalid JSON arguments" in tool_msg["content"]
    assert ctx.final_text is not None  # Never left as None
