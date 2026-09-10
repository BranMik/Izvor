import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.thread_notes_agent import generate_thread_notes
from agents.usage_agent import (
    calculate_thread_costs,
    format_usage_slack_message,
    record_thread_tokens,
)
from orchestration import agent_router
from services import pricing_service


@pytest.fixture(autouse=True)
def setup_test_environment(monkeypatch):
    monkeypatch.setattr(agent_router.asyncio, "sleep", AsyncMock(return_value=None))


def _has_cache_control(messages: list[dict]) -> bool:
    """Checks whether any message in the sequence contains a cache_control directive."""
    for msg in messages:
        if "cache_control" in msg:
            return True
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and "cache_control" in block:
                    return True
        elif isinstance(content, dict) and "cache_control" in content:
            return True
    return False


@pytest.mark.anyio
async def test_anthropic_agent_omits_cache_control(caplog):
    """Verify litellm.completion does NOT receive cache_control for Anthropic models, and logs no cache info."""
    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(message=MagicMock(content="Hello from AnthrOpus!", tool_calls=None))
    ]
    mock_resp.usage = MagicMock(prompt_tokens=200, completion_tokens=100)

    with caplog.at_level(logging.INFO), \
         patch("litellm.completion", return_value=mock_resp) as mock_completion, \
         patch("orchestration.agent_router.post_agent_response", return_value={"ok": True, "ts": "123.456"}), \
         patch("orchestration.agent_router.fetch_thread_history", return_value=[
             {"role": "user", "content": "Prior message 1"},
             {"role": "assistant", "content": "Prior reply 1"},
         ]), \
         patch("orchestration.agent_router.fetch_thread_files", return_value=[]), \
         patch("orchestration.agent_router.record_thread_tokens"), \
         patch("orchestration.agent_router.trigger_thread_notes_update"), \
         patch("orchestration.agent_router.trigger_usage_update"):

        res = await agent_router.process_and_respond(
            channel_id="C123",
            thread_ts="1234567890.123456",
            raw_text="[Innovator] Analyze this architecture",
            is_bot_trigger=False,
        )

        assert res["ok"] is True
        assert mock_completion.called
        kwargs = mock_completion.call_args.kwargs
        messages = kwargs["messages"]

        # Ensure no cache_control anywhere
        assert not _has_cache_control(messages)
        # Ensure system content is clean string, not structured list with cache_control
        assert isinstance(messages[0]["content"], str)

        # Verify NO cache logs emitted
        assert "cache_read" not in caplog.text.lower()
        assert "cache_write" not in caplog.text.lower()
        assert "cached_tokens" not in caplog.text.lower()
        assert "[ANTHROPIC]" not in caplog.text


@pytest.mark.anyio
async def test_openai_agent_emits_no_cache_logs(caplog):
    """Verify OpenAI agent execution emits no cache logs."""
    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(message=MagicMock(content="Hello from Terra!", tool_calls=None))
    ]
    mock_resp.usage = MagicMock(prompt_tokens=2048, completion_tokens=500)
    mock_resp.usage.prompt_tokens_details = MagicMock(cached_tokens=1024)

    with caplog.at_level(logging.INFO), \
         patch("litellm.completion", return_value=mock_resp) as mock_completion, \
         patch("orchestration.agent_router.post_agent_response", return_value={"ok": True, "ts": "123.456"}), \
         patch("orchestration.agent_router.fetch_thread_history", return_value=[]), \
         patch("orchestration.agent_router.fetch_thread_files", return_value=[]), \
         patch("orchestration.agent_router.record_thread_tokens"), \
         patch("orchestration.agent_router.trigger_thread_notes_update"), \
         patch("orchestration.agent_router.trigger_usage_update"):

        res = await agent_router.process_and_respond(
            channel_id="C123",
            thread_ts="1234567890.123456",
            raw_text="[Architect] Architecture review",
            is_bot_trigger=False,
        )

        assert res["ok"] is True
        assert mock_completion.called
        kwargs = mock_completion.call_args.kwargs
        messages = kwargs["messages"]
        assert not _has_cache_control(messages)

        # Verify NO cache logs emitted
        assert "[OPENAI] cached_tokens" not in caplog.text
        assert "cache_write_tokens" not in caplog.text


@pytest.mark.anyio
async def test_thread_notes_omits_cache_control_and_logs(caplog):
    """Verify thread notes agent omits cache_control and emits no cache logs."""
    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(message=MagicMock(content="## Notes\n- Point 1", tool_calls=None))
    ]
    mock_resp.usage = MagicMock(prompt_tokens=100, completion_tokens=50)

    with caplog.at_level(logging.INFO), \
         patch("litellm.completion", return_value=mock_resp) as mock_completion:

        notes, stats = await generate_thread_notes(
            history=[{"role": "user", "content": "Hello"}],
            is_completed=False,
        )

        assert "Notes" in notes
        assert "cached_tokens" not in stats
        kwargs = mock_completion.call_args.kwargs
        messages = kwargs["messages"]
        assert not _has_cache_control(messages)
        assert isinstance(messages[0]["content"], str)

        assert "cache_read" not in caplog.text.lower()
        assert "cached_tokens" not in caplog.text.lower()


@pytest.mark.anyio
async def test_pricing_agent_omits_cache_control_and_logs(caplog):
    """Verify pricing agent omits cache_control and emits no cache logs."""
    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(
            message=MagicMock(
                content='{"model_name": "test-model", "input_price_per_1m": 1.0, "output_price_per_1m": 2.0, "currency": "USD", "source": "Web", "source_url": "https://test.com"}',
                tool_calls=None,
            )
        )
    ]
    mock_resp.usage = MagicMock(prompt_tokens=80, completion_tokens=40)

    with caplog.at_level(logging.INFO), \
         patch("litellm.completion", return_value=mock_resp) as mock_completion:

        res = await pricing_service.fetch_pricing_with_agent("test-model")
        assert res["input_price_per_1m"] == 1.0
        kwargs = mock_completion.call_args.kwargs
        messages = kwargs["messages"]
        assert not _has_cache_control(messages)
        assert isinstance(messages[0]["content"], str)

        assert "cache_read" not in caplog.text.lower()
        assert "cached_tokens" not in caplog.text.lower()
        assert "cache_write" not in caplog.text.lower()


@pytest.mark.anyio
async def test_usage_agent_has_no_cache_info():
    """Verify usage agent tracking, calculations, and message formatting have no cache metrics."""
    channel_id = "C_TEST_NO_CACHE"
    thread_ts = "9999999999.000001"

    record_thread_tokens(
        channel_id=channel_id,
        thread_ts=thread_ts,
        model_name="anthropic/claude-opus-5",
        prompt_tokens=500,
        completion_tokens=250,
        channel_name="#test-channel",
    )

    costs = calculate_thread_costs(channel_id, thread_ts)
    assert "grand_cached_tokens" not in costs
    model_stats = costs["models"]["anthropic/claude-opus-5"]
    assert "cached_tokens" not in model_stats

    msg = await format_usage_slack_message(channel_id, thread_ts, is_completed=False)
    assert "Predmemorirani" not in msg
    assert "Cached" not in msg
    assert "predmemoriranih" not in msg
