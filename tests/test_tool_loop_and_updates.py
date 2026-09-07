import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from orchestration.turn_pipeline import (
    TurnContext,
    TurnPipeline,
    _build_tool_status_text,
)


def test_build_tool_status_text():
    mock_tool = MagicMock()
    mock_tool.function.name = "web_search"
    mock_tool.function.arguments = json.dumps({"query": "AI breakthroughs 2026"})

    text = _build_tool_status_text("Visionary", [mock_tool])
    assert "Visionary" in text
    assert "is searching the web" in text
    assert "AI breakthroughs 2026" in text

    mock_tool.function.name = "fetch_web_page"
    mock_tool.function.arguments = json.dumps({"url": "https://example.com/data"})
    text_fetch = _build_tool_status_text("TechLead", [mock_tool])
    assert "TechLead" in text_fetch
    assert "is fetching webpage" in text_fetch
    assert "https://example.com/data" in text_fetch

    mock_tool.function.name = "load_skill"
    mock_tool.function.arguments = json.dumps({"skill_name": "market_research"})
    text_skill = _build_tool_status_text("Strategist", [mock_tool])
    assert "Strategist" in text_skill
    assert "is loading skill" in text_skill
    assert "market_research" in text_skill


@pytest.mark.anyio
async def test_tool_call_loop_timeout_fallback():
    """Verifies that stage_execute_llm times out and falls back gracefully when tool loop hangs."""
    # Simulate tool call in first response
    mock_tool_call = MagicMock()
    mock_tool_call.id = "call_1"
    mock_tool_call.function.name = "web_search"
    mock_tool_call.function.arguments = json.dumps({"query": "hang forever"})

    mock_resp = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "Initial analysis before tool"
    mock_choice.message.tool_calls = [mock_tool_call]
    mock_choice.message.model_dump.return_value = {"role": "assistant", "content": "Initial"}
    mock_resp.choices = [mock_choice]

    async def slow_mcp_tool(name, args):
        await asyncio.sleep(0.5)
        return "slow result"

    mock_retry_fn = AsyncMock(return_value=mock_resp)

    pipeline = TurnPipeline(
        hooks={
            "TOOL_CALL_LOOP_TIMEOUT_SECONDS": 0.05,
            "TOOL_INTERMEDIATE_UPDATE_DELAY_SECONDS": 10.0,  # no intermediate post during test
            "execute_llm_with_retry": mock_retry_fn,
            "execute_mcp_tool": slow_mcp_tool,
        }
    )
    ctx = TurnContext(
        channel_id="C_TEST",
        thread_ts="12345.6789",
        raw_text="Test prompt",
    )
    ctx.agent_key = "Visionary"
    ctx.agent_cfg = {
        "model": "openrouter/auto",
        "username": "Visionary",
        "skills": ["web_tools"],
    }

    res = await pipeline.stage_execute_llm(ctx)
    assert res is None  # continues gracefully
    assert ctx.final_text == "Initial analysis before tool"


@pytest.mark.anyio
async def test_delayed_intermediate_status_and_inplace_update():
    """Verifies that an intermediate message is sent after delay and updated in-place on completion."""
    mock_post_agent = AsyncMock(return_value={"ok": True, "ts": "11111.0001"})
    mock_update_msg = AsyncMock(return_value={"ok": True, "ts": "11111.0001"})

    mock_tool_call = MagicMock()
    mock_tool_call.id = "call_1"
    mock_tool_call.function.name = "web_search"
    mock_tool_call.function.arguments = json.dumps({"query": "AI 2026"})

    mock_initial_resp = MagicMock()
    mock_choice_1 = MagicMock()
    mock_choice_1.message.content = ""
    mock_choice_1.message.tool_calls = [mock_tool_call]
    mock_choice_1.message.model_dump.return_value = {"role": "assistant"}
    mock_initial_resp.choices = [mock_choice_1]

    mock_followup_resp = MagicMock()
    mock_choice_2 = MagicMock()
    mock_choice_2.message.content = "Here is the final answer about AI 2026."
    mock_choice_2.message.tool_calls = None
    mock_followup_resp.choices = [mock_choice_2]

    async def delayed_tool(name, args):
        await asyncio.sleep(0.12)  # longer than intermediate delay 0.05s
        return "Tool search result"

    mock_retry_fn = AsyncMock(side_effect=[mock_initial_resp, mock_followup_resp])

    pipeline = TurnPipeline(
        hooks={
            "TOOL_CALL_LOOP_TIMEOUT_SECONDS": 5.0,
            "TOOL_INTERMEDIATE_UPDATE_DELAY_SECONDS": 0.05,  # quick trigger
            "execute_llm_with_retry": mock_retry_fn,
            "execute_mcp_tool": delayed_tool,
            "post_agent_response": mock_post_agent,
            "update_slack_message": mock_update_msg,
            "DEBATE_TURN_PACING_SECONDS": 0.0,
            "SLACK_POST_PAUSE_SECONDS": 0.0,
        }
    )
    ctx = TurnContext(
        channel_id="C_TEST",
        thread_ts="12345.6789",
        raw_text="Tell me about tech",
    )
    ctx.agent_key = "Visionary"
    ctx.agent_cfg = {
        "model": "openrouter/auto",
        "username": "Visionary",
        "skills": ["web_tools"],
    }

    res = await pipeline.stage_execute_llm(ctx)
    assert res is None
    assert ctx.final_text == "Here is the final answer about AI 2026."
    assert ctx.intermediate_msg_ts == "11111.0001"
    mock_post_agent.assert_called_once()
    assert "is searching the web" in mock_post_agent.call_args[1]["text"]

    # Now run stage_post_and_record to verify in-place update
    with patch("orchestration.turn_pipeline.record_thread_tokens"), \
         patch("orchestration.turn_pipeline.trigger_thread_notes_update"), \
         patch("orchestration.turn_pipeline.trigger_usage_update"):
        post_res = await pipeline.stage_post_and_record(ctx)
        assert post_res.get("ok") is True
        mock_update_msg.assert_called_once()
        call_kwargs = mock_update_msg.call_args[1]
        assert call_kwargs["channel_id"] == "C_TEST"
        assert call_kwargs["ts"] == "11111.0001"
        assert "Here is the final answer about AI 2026." in call_kwargs["text"]
