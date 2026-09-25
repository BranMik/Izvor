import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from orchestration.turn_pipeline import TurnContext, TurnPipeline
from orchestration.thread_state import thread_state_manager
from pipeline.discussion_scribe.scribe import discussion_scribe


@pytest.mark.anyio
async def test_stage_execute_llm_mechanical_limit_with_modifier():
    pipeline = TurnPipeline()
    ctx = TurnContext(
        channel_id="C123",
        thread_ts="1234.5678",
        raw_text="Explain architecture /max_words 60",
    )
    ctx.thread_key = ("C123", "1234.5678")
    ctx.agent_key = "Architect"
    ctx.agent_cfg = {"model": "gpt-4o", "skills": []}
    ctx.messages = [{"role": "user", "content": "Explain architecture"}]
    ctx.modifiers = {"max_words": 60}

    mock_resp = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "Short architecture summary."
    mock_choice.message.tool_calls = None
    mock_resp.choices = [mock_choice]
    mock_resp.usage = None

    captured_kwargs = {}

    async def mock_exec_retry(fn, completion_kwargs, **kwargs):
        captured_kwargs.update(completion_kwargs)
        return mock_resp

    pipeline._hooks["execute_llm_with_retry"] = mock_exec_retry
    await pipeline.stage_execute_llm(ctx)

    assert "max_tokens" in captured_kwargs
    # (60 + 50) * 1.5 = 165
    assert captured_kwargs["max_tokens"] == 165


@pytest.mark.anyio
async def test_stage_execute_llm_mechanical_limit_with_thread_state():
    channel = "C_STATE"
    thread_ts = "9999.0001"
    thread_state_manager.set_max_words(channel, thread_ts, 40)

    try:
        pipeline = TurnPipeline()
        ctx = TurnContext(
            channel_id=channel,
            thread_ts=thread_ts,
            raw_text="Continue discussion",
        )
        ctx.thread_key = (channel, thread_ts)
        ctx.agent_key = "TechLead"
        ctx.agent_cfg = {"model": "gpt-4o", "skills": []}
        ctx.messages = [{"role": "user", "content": "Continue discussion"}]

        mock_resp = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = "Tech perspective."
        mock_choice.message.tool_calls = None
        mock_resp.choices = [mock_choice]
        mock_resp.usage = None

        captured_kwargs = {}

        async def mock_exec_retry(fn, completion_kwargs, **kwargs):
            captured_kwargs.update(completion_kwargs)
            return mock_resp

        pipeline._hooks["execute_llm_with_retry"] = mock_exec_retry
        await pipeline.stage_execute_llm(ctx)

        assert "max_tokens" in captured_kwargs
        # (40 + 50) * 1.5 = 135
        assert captured_kwargs["max_tokens"] == 135
    finally:
        thread_state_manager.clear_max_words(channel, thread_ts)


@pytest.mark.anyio
async def test_stage_execute_llm_without_max_words():
    channel = "C_NONE"
    thread_ts = "9999.0002"
    thread_state_manager.clear_max_words(channel, thread_ts)

    pipeline = TurnPipeline()
    ctx = TurnContext(
        channel_id=channel,
        thread_ts=thread_ts,
        raw_text="No limits",
    )
    ctx.thread_key = (channel, thread_ts)
    ctx.agent_key = "Visionary"
    ctx.agent_cfg = {"model": "gpt-4o", "skills": []}
    ctx.messages = [{"role": "user", "content": "No limits"}]

    mock_resp = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "Vision."
    mock_choice.message.tool_calls = None
    mock_resp.choices = [mock_choice]
    mock_resp.usage = None

    captured_kwargs = {}

    async def mock_exec_retry(fn, completion_kwargs, **kwargs):
        captured_kwargs.update(completion_kwargs)
        return mock_resp

    pipeline._hooks["execute_llm_with_retry"] = mock_exec_retry
    await pipeline.stage_execute_llm(ctx)

    assert "max_tokens" not in captured_kwargs


@pytest.mark.anyio
async def test_stage_execute_llm_with_max_words_off():
    channel = "C_OFF"
    thread_ts = "9999.0003"

    pipeline = TurnPipeline()
    ctx = TurnContext(
        channel_id=channel,
        thread_ts=thread_ts,
        raw_text="Turn off limit /max_words off",
    )
    ctx.thread_key = (channel, thread_ts)
    ctx.agent_key = "Visionary"
    ctx.agent_cfg = {"model": "gpt-4o", "skills": []}
    ctx.messages = [{"role": "user", "content": "Turn off limit"}]
    ctx.max_words_limit = "off"
    ctx.modifiers = {"max_words": "off"}

    mock_resp = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "Full answer without limit."
    mock_choice.message.tool_calls = None
    mock_resp.choices = [mock_choice]
    mock_resp.usage = None

    captured_kwargs = {}

    async def mock_exec_retry(fn, completion_kwargs, **kwargs):
        captured_kwargs.update(completion_kwargs)
        return mock_resp

    pipeline._hooks["execute_llm_with_retry"] = mock_exec_retry
    await pipeline.stage_execute_llm(ctx)

    assert "max_tokens" not in captured_kwargs


@pytest.mark.anyio
async def test_discussion_scribe_mechanical_limit():
    history = [
        {"role": "user", "content": "Let's summarize our progress"},
        {"role": "assistant", "content": "Here is what we did."},
    ]

    mock_resp = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "Strategic synthesis note"
    mock_resp.choices = [mock_choice]
    mock_resp.usage = MagicMock(prompt_tokens=100, completion_tokens=50)

    with patch("pipeline.discussion_scribe.scribe.litellm.completion", return_value=mock_resp) as mock_litellm:
        brief, tokens = await discussion_scribe.generate_brief(
            history=history,
            is_completed=True,
            scribe_max_words=100,
        )

        assert mock_litellm.called
        call_kwargs = mock_litellm.call_args.kwargs
        # (100 + 50) * 1.5 = 225
        assert call_kwargs["max_tokens"] == 225
