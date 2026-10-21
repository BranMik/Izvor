import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from orchestration.turn_pipeline import TurnContext, TurnPipeline
from orchestration.thread_state import thread_state_manager
from orchestration.stages.post_record import apply_mechanical_word_limit
from pipeline.discussion_scribe.scribe import discussion_scribe


def test_apply_mechanical_word_limit_preserves_trailing_tag():
    # 120 words followed by nomination tag
    words = [f"word{i}" for i in range(1, 121)]
    long_text = " ".join(words) + " /[agent-10007]"
    
    # max_words = 50 -> cutoff is 50 + 50 = 100 words
    result = apply_mechanical_word_limit(long_text, max_words=50)
    
    assert "/[agent-10007]" in result
    assert result.endswith("/[agent-10007]")
    # Extract body before tag
    body = result.replace("/[agent-10007]", "").strip()
    assert len(body.split()) == 100
    assert body.split()[0] == "word1"
    assert body.split()[-1] == "word100"


def test_apply_mechanical_word_limit_no_tag():
    words = [f"item{i}" for i in range(1, 115)]
    text = " ".join(words)
    result = apply_mechanical_word_limit(text, max_words=50)
    assert len(result.split()) == 100
    assert result.split()[-1] == "item100"


def test_apply_mechanical_word_limit_within_threshold():
    words = [f"word{i}" for i in range(1, 75)]
    text = " ".join(words) + " /[agent-10003]"
    result = apply_mechanical_word_limit(text, max_words=50)
    assert result == text


@pytest.mark.anyio
async def test_stage_execute_llm_does_not_artificially_choke_max_tokens():
    """Verify that stage_execute_llm does NOT set a tiny max_tokens on litellm,

    leaving reasoning models (Gemini) and multilingual texts unhindered.
    """
    pipeline = TurnPipeline()
    ctx = TurnContext(
        channel_id="C123",
        thread_ts="1234.5678",
        raw_text="Explain architecture /max_words 50",
    )
    ctx.thread_key = ("C123", "1234.5678")
    ctx.agent_key = "agent-10008"
    ctx.agent_cfg = {"model": "gpt-4o", "skills": []}
    ctx.messages = [{"role": "user", "content": "Explain architecture"}]
    ctx.modifiers = {"max_words": 50}

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
    await pipeline.execution_stage.stage_execute_llm(ctx)

    # max_tokens should NOT be present in captured_kwargs for normal debate
    assert "max_tokens" not in captured_kwargs


@pytest.mark.anyio
async def test_stage_post_and_record_mechanical_truncation():
    """Verify that stage_post_and_record mechanically truncates responses

    exceeding (max_words + 50) words and preserves the nomination tag.
    """
    channel = "C_POST"
    thread_ts = "9999.0010"
    thread_state_manager.set_max_words(channel, thread_ts, 50)

    try:
        pipeline = TurnPipeline()
        ctx = TurnContext(
            channel_id=channel,
            thread_ts=thread_ts,
            raw_text="Test prompt",
        )
        ctx.thread_key = (channel, thread_ts)
        ctx.agent_key = "agent-10008"
        ctx.agent_cfg = {"username": "agent-10008", "model": "openai/gpt-4o"}
        ctx.eligible_remaining_agents = ["agent-10011"]
        ctx.remaining = ["agent-10011"]

        # 120 words followed by /[agent-10011] tag
        words = [f"word{i}" for i in range(1, 121)]
        ctx.final_text = " ".join(words) + "\n\n/[agent-10011]"

        # Mock Slack post and sleep
        posted_payload = {}

        async def mock_post(channel_id, thread_ts, text, username, icon_url):
            posted_payload["text"] = text
            return {"ok": True, "ts": "1234.9999"}

        pipeline._hooks["post_agent_response"] = mock_post
        pipeline._hooks["DEBATE_TURN_PACING_SECONDS"] = 0
        pipeline._hooks["SLACK_POST_PAUSE_SECONDS"] = 0

        res = await pipeline.post_record_stage.stage_post_and_record(ctx)
        assert res is None or res.get("ok", True)

        posted_text = posted_payload.get("text", "")
        # Must retain /[agent-10011] nomination tag
        assert "/[agent-10011]" in posted_text
        # Extract body (excluding token footer {model : p/c} and nomination tag)
        clean_body = posted_text.split("/[agent-10011]")[0].strip()
        assert len(clean_body.split()) == 100
        assert clean_body.split()[-1] == "word100"
    finally:
        thread_state_manager.clear_max_words(channel, thread_ts)


@pytest.mark.anyio
async def test_stage_post_and_record_no_truncation_when_within_limit():
    channel = "C_POST_OK"
    thread_ts = "9999.0011"
    thread_state_manager.set_max_words(channel, thread_ts, 50)

    try:
        pipeline = TurnPipeline()
        ctx = TurnContext(
            channel_id=channel,
            thread_ts=thread_ts,
            raw_text="Test prompt",
        )
        ctx.thread_key = (channel, thread_ts)
        ctx.agent_key = "agent-10008"
        ctx.agent_cfg = {"username": "agent-10008", "model": "openai/gpt-4o"}
        ctx.eligible_remaining_agents = ["agent-10011"]
        ctx.remaining = ["agent-10011"]

        words = [f"word{i}" for i in range(1, 60)]
        original_content = " ".join(words)
        ctx.final_text = original_content + "\n\n/[agent-10011]"

        posted_payload = {}

        async def mock_post(channel_id, thread_ts, text, username, icon_url):
            posted_payload["text"] = text
            return {"ok": True, "ts": "1234.9999"}

        pipeline._hooks["post_agent_response"] = mock_post
        pipeline._hooks["DEBATE_TURN_PACING_SECONDS"] = 0
        pipeline._hooks["SLACK_POST_PAUSE_SECONDS"] = 0

        await pipeline.post_record_stage.stage_post_and_record(ctx)

        posted_text = posted_payload.get("text", "")
        clean_body = posted_text.split("/[agent-10011]")[0].strip()
        assert len(clean_body.split()) == 59
    finally:
        thread_state_manager.clear_max_words(channel, thread_ts)


@pytest.mark.anyio
async def test_discussion_scribe_mechanical_word_limit():
    """Verify that DiscussionScribe mechanically truncates brief in Python

    to (scribe_max_words + 50) words if the model exceeds it.
    """
    history = [
        {"role": "user", "content": "Let's summarize our progress"},
        {"role": "assistant", "content": "Here is what we did."},
    ]

    # Generate a brief with 120 words
    words = [f"briefword{i}" for i in range(1, 121)]
    overlong_brief = " ".join(words)

    mock_resp = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = overlong_brief
    mock_resp.choices = [mock_choice]
    mock_resp.usage = MagicMock(prompt_tokens=100, completion_tokens=150)

    with patch("pipeline.discussion_scribe.scribe.litellm.acompletion", return_value=mock_resp):
        # scribe_max_words = 40 -> cutoff is 40 + 50 = 90 words
        brief, tokens = await discussion_scribe.generate_brief(
            history=history,
            is_completed=True,
            scribe_max_words=40,
        )

        assert brief is not None
        brief_words = brief.raw_markdown.split()
        assert len(brief_words) == 90
        assert brief_words[-1] == "briefword90"
