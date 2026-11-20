import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestration.stages.tool_status import _build_tool_status_text
from orchestration.turn_pipeline import (
    TurnContext,
    TurnPipeline,
)


def test_build_tool_status_text():
    mock_tool = MagicMock()
    mock_tool.function.name = "web_search"
    mock_tool.function.arguments = json.dumps({"query": "AI breakthroughs 2026"})

    text = _build_tool_status_text("agent-10001", [mock_tool])
    assert "agent-10001" in text
    assert "is searching the web" in text
    assert "AI breakthroughs 2026" in text

    mock_tool.function.name = "fetch_web_page"
    mock_tool.function.arguments = json.dumps({"url": "https://example.com/data"})
    text_fetch = _build_tool_status_text("agent-10011", [mock_tool])
    assert "agent-10011" in text_fetch
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

    async def slow_local_tool(name, args):
        await asyncio.sleep(0.5)
        return "slow result"

    mock_retry_fn = AsyncMock(return_value=mock_resp)

    pipeline = TurnPipeline(
        timeout_secs=0.05,
        intermediate_delay_secs=10.0,
        llm_executor=mock_retry_fn,
        tool_executor=slow_local_tool,
    )
    ctx = TurnContext(
        channel_id="C_TEST",
        thread_ts="12345.6789",
        raw_text="Test prompt",
    )
    ctx.agent_key = "agent-10001"
    ctx.agent_cfg = {
        "model": "openrouter/auto",
        "username": "agent-10001",
        "skills": ["web_tools"],
    }

    res = await pipeline.execution_stage.stage_execute_llm(ctx)
    assert res is None  # continues gracefully
    assert ctx.output.final_text == "Initial analysis before tool"


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
    mock_initial_resp.model = "gemini/gemini-3.8-flash"
    mock_choice_1 = MagicMock()
    mock_choice_1.message.content = ""
    mock_choice_1.message.tool_calls = [mock_tool_call]
    mock_choice_1.message.model_dump.return_value = {"role": "assistant"}
    mock_initial_resp.choices = [mock_choice_1]

    mock_followup_resp = MagicMock()
    mock_followup_resp.model = "gemini/gemini-3.8-flash"
    mock_choice_2 = MagicMock()
    mock_choice_2.message.content = "Here is the final answer about AI 2026."
    mock_choice_2.message.tool_calls = None
    mock_followup_resp.choices = [mock_choice_2]

    async def delayed_tool(name, args):
        await asyncio.sleep(0.12)  # longer than intermediate delay 0.05s
        return "Tool search result"

    mock_retry_fn = AsyncMock(side_effect=[mock_initial_resp, mock_followup_resp])

    pipeline = TurnPipeline(
        timeout_secs=5.0,
        intermediate_delay_secs=0.05,
        llm_executor=mock_retry_fn,
        tool_executor=delayed_tool,
        slack_poster=mock_post_agent,
        update_message_fn=mock_update_msg,
        debate_turn_pacing_seconds=0.0,
        slack_post_pause_seconds=0.0,
    )
    ctx = TurnContext(
        channel_id="C_TEST",
        thread_ts="12345.6789",
        raw_text="Tell me about tech",
    )
    ctx.agent_key = "agent-10001"
    ctx.agent_cfg = {
        "model": "openrouter/auto",
        "username": "agent-10001",
        "skills": ["web_research"],
    }

    res = await pipeline.execution_stage.stage_execute_llm(ctx)
    assert res is None
    assert ctx.output.final_text == "Here is the final answer about AI 2026."
    assert ctx.intermediate_msg_ts == "11111.0001"
    mock_post_agent.assert_called_once()
    assert "is searching the web" in mock_post_agent.call_args[1]["text"]

    # Now run stage_post_and_record to verify in-place update
    with patch("orchestration.turn_pipeline.record_thread_tokens"), \
         patch("orchestration.turn_pipeline.trigger_thread_notes_update"), \
         patch("orchestration.turn_pipeline.trigger_usage_update"):
        post_res = await pipeline.post_record_stage.stage_post_and_record(ctx)
        assert post_res.get("ok") is True
        mock_update_msg.assert_called_once()
        call_kwargs = mock_update_msg.call_args[1]
        assert call_kwargs["channel_id"] == "C_TEST"
        assert call_kwargs["ts"] == "11111.0001"
        assert "Here is the final answer about AI 2026." in call_kwargs["text"]


@pytest.mark.anyio
async def test_delayed_status_on_long_initial_thinking_without_tools():
    """Verifies that >5s initial LLM thinking posts '_⏳ is analyzing data..._' and updates in-place on completion."""
    mock_post_agent = AsyncMock(return_value={"ok": True, "ts": "22222.0001"})
    mock_update_msg = AsyncMock(return_value={"ok": True, "ts": "22222.0001"})

    mock_resp = MagicMock()
    mock_resp.model = "mistral/mistral-large-latest"
    mock_choice = MagicMock()
    mock_choice.message.content = "Deep reasoning conclusion without tools."
    mock_choice.message.tool_calls = None
    mock_resp.choices = [mock_choice]

    async def slow_thinking_llm(*args, **kwargs):
        await asyncio.sleep(0.12)  # exceeds intermediate_delay_secs=0.04
        return mock_resp

    pipeline = TurnPipeline(
        timeout_secs=5.0,
        intermediate_delay_secs=0.04,
        llm_executor=slow_thinking_llm,
        slack_poster=mock_post_agent,
        update_message_fn=mock_update_msg,
        debate_turn_pacing_seconds=0.0,
        slack_post_pause_seconds=0.0,
    )
    ctx = TurnContext(
        channel_id="C_TEST",
        thread_ts="12345.6789",
        raw_text="Analyze architectural trade-offs",
    )
    ctx.agent_key = "agent-10008"
    ctx.agent_cfg = {
        "model": "mistral/mistral-large-latest",
        "username": "SecurityArchitect",
        "skills": [],
    }

    res = await pipeline.execution_stage.stage_execute_llm(ctx)
    assert res is None
    assert ctx.intermediate_msg_ts == "22222.0001"
    mock_post_agent.assert_called_once()
    assert "SecurityArchitect* is analyzing data" in mock_post_agent.call_args[1]["text"]

    with patch("orchestration.turn_pipeline.record_thread_tokens"), \
         patch("orchestration.turn_pipeline.trigger_thread_notes_update"), \
         patch("orchestration.turn_pipeline.trigger_usage_update"):
        post_res = await pipeline.post_record_stage.stage_post_and_record(ctx)
        assert post_res.get("ok") is True
        mock_update_msg.assert_called_once()
        assert "Deep reasoning conclusion without tools." in mock_update_msg.call_args[1]["text"]


@pytest.mark.anyio
async def test_multi_tool_iteration_updates_intermediate_status_in_place():
    """Verifies that once intermediate status is active, subsequent tool iterations update the status in-place."""
    mock_post_agent = AsyncMock(return_value={"ok": True, "ts": "33333.0001"})
    mock_update_msg = AsyncMock(return_value={"ok": True, "ts": "33333.0001"})

    tc_search = MagicMock()
    tc_search.id = "call_1"
    tc_search.function.name = "web_search"
    tc_search.function.arguments = json.dumps({"query": "Qdrant pricing"})

    tc_fetch = MagicMock()
    tc_fetch.id = "call_2"
    tc_fetch.function.name = "fetch_web_page"
    tc_fetch.function.arguments = json.dumps({"url": "https://qdrant.tech/pricing"})

    resp_1 = MagicMock()
    choice_1 = MagicMock()
    choice_1.message.content = ""
    choice_1.message.tool_calls = [tc_search]
    choice_1.message.model_dump.return_value = {"role": "assistant", "tool_calls": [{"id": "call_1", "function": {"name": "web_search"}}]}
    resp_1.choices = [choice_1]

    resp_2 = MagicMock()
    choice_2 = MagicMock()
    choice_2.message.content = ""
    choice_2.message.tool_calls = [tc_fetch]
    choice_2.message.model_dump.return_value = {"role": "assistant", "tool_calls": [{"id": "call_2", "function": {"name": "fetch_web_page"}}]}
    resp_2.choices = [choice_2]

    resp_3 = MagicMock()
    choice_3 = MagicMock()
    choice_3.message.content = "Qdrant Cloud starts with a free 1GB cluster."
    choice_3.message.tool_calls = None
    resp_3.choices = [choice_3]

    async def tool_exec(name, args):
        if name == "web_search":
            await asyncio.sleep(0.10)  # triggers initial 0.03s status post
            return json.dumps({"results": [{"url": "https://qdrant.tech/pricing"}]})
        return "Qdrant pricing page content"

    mock_retry_fn = AsyncMock(side_effect=[resp_1, resp_2, resp_3])

    pipeline = TurnPipeline(
        timeout_secs=5.0,
        intermediate_delay_secs=0.03,
        llm_executor=mock_retry_fn,
        tool_executor=tool_exec,
        slack_poster=mock_post_agent,
        update_message_fn=mock_update_msg,
        debate_turn_pacing_seconds=0.0,
        slack_post_pause_seconds=0.0,
    )
    ctx = TurnContext(channel_id="C_TEST", thread_ts="12345.6789", raw_text="Compare Qdrant pricing")
    ctx.agent_key = "agent-10001"
    ctx.agent_cfg = {"model": "gemini/gemini-3.8-flash", "username": "TechLead", "skills": ["web_research"]}

    res = await pipeline.execution_stage.stage_execute_llm(ctx)
    assert res is None
    assert ctx.intermediate_msg_ts == "33333.0001"
    mock_post_agent.assert_called_once()
    assert "is searching the web" in mock_post_agent.call_args[1]["text"]
    assert mock_update_msg.call_count >= 1
    assert any("is fetching webpage" in c.kwargs.get("text", "") for c in mock_update_msg.call_args_list)


@pytest.mark.anyio
async def test_tool_loop_timeout_after_gathering_evidence_runs_final_synthesis():
    """Verifies that if a later tool iteration times out after Iteration 1 gathered evidence, final synthesis still runs."""
    tc_1 = MagicMock()
    tc_1.id = "call_1"
    tc_1.function.name = "web_search"
    tc_1.function.arguments = json.dumps({"query": "Pinecone pricing"})

    tc_2 = MagicMock()
    tc_2.id = "call_2"
    tc_2.function.name = "fetch_web_page"
    tc_2.function.arguments = json.dumps({"url": "https://pinecone.io/slow"})

    resp_1 = MagicMock()
    choice_1 = MagicMock()
    choice_1.message.content = ""
    choice_1.message.tool_calls = [tc_1]
    choice_1.message.model_dump.return_value = {
        "role": "assistant",
        "tool_calls": [{"id": "call_1", "function": {"name": "web_search", "arguments": '{"query": "Pinecone pricing"}'}}],
    }
    resp_1.choices = [choice_1]

    resp_2 = MagicMock()
    choice_2 = MagicMock()
    choice_2.message.content = ""
    choice_2.message.tool_calls = [tc_2]
    choice_2.message.model_dump.return_value = {
        "role": "assistant",
        "tool_calls": [{"id": "call_2", "function": {"name": "fetch_web_page", "arguments": '{"url": "https://pinecone.io/slow"}'}}],
    }
    resp_2.choices = [choice_2]

    resp_synthesis = MagicMock()
    choice_syn = MagicMock()
    choice_syn.message.content = "Synthesized answer from Iteration 1 Pinecone pricing evidence."
    choice_syn.message.tool_calls = None
    resp_synthesis.choices = [choice_syn]

    async def tool_exec(name, args):
        if name == "web_search":
            return json.dumps({"results": [{"title": "Pinecone", "snippet": "$50/mo standard"}]})
        await asyncio.sleep(0.5)  # exceeds timeout_secs=0.08 on Iteration 2
        return "unreachable"

    mock_retry_fn = AsyncMock(side_effect=[resp_1, resp_2, resp_synthesis])

    pipeline = TurnPipeline(
        timeout_secs=0.08,
        intermediate_delay_secs=10.0,
        llm_executor=mock_retry_fn,
        tool_executor=tool_exec,
    )
    ctx = TurnContext(channel_id="C_TEST", thread_ts="12345.6789", raw_text="Check Pinecone pricing")
    ctx.agent_key = "agent-10001"
    ctx.agent_cfg = {"model": "mistral/mistral-large-latest", "username": "agent-10001", "skills": ["web_research"]}

    res = await pipeline.execution_stage.stage_execute_llm(ctx)
    assert res is None
    assert ctx.output.final_text == "Synthesized answer from Iteration 1 Pinecone pricing evidence."
    assert mock_retry_fn.call_count == 3
