import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from config import AGENTS_CONFIG, is_anthropic_model
import agent_router
from agents.thread_notes_agent import generate_thread_notes
import pricing_service


def test_is_anthropic_model_detection():
    """Verify that is_anthropic_model accurately identifies Claude/Anthropic models."""
    assert is_anthropic_model("anthropic/claude-opus-5") is True
    assert is_anthropic_model("anthropic/claude-3-5-sonnet-20241022") is True
    assert is_anthropic_model("claude-opus-5") is True
    assert is_anthropic_model("claude-3-5-haiku-20241022") is True
    assert is_anthropic_model("ANTHROPIC/CLAUDE-3-5-SONNET") is True

    assert is_anthropic_model("gemini/gemini-3.7-flash") is False
    assert is_anthropic_model("openai/gpt-5.6-terra") is False
    assert is_anthropic_model("mistral/mistral-large-latest") is False
    assert is_anthropic_model("groq/llama-3.3-70b") is False
    assert is_anthropic_model("") is False
    assert is_anthropic_model(None) is False


@pytest.fixture(autouse=True)
def setup_test_environment(monkeypatch):
    monkeypatch.setattr(agent_router.asyncio, "sleep", AsyncMock(return_value=None))


@pytest.mark.anyio
async def test_anthropic_agent_passes_cache_control():
    """Verify that litellm.completion receives cache_control={'type': 'ephemeral'} for Anthropic agents."""
    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(message=MagicMock(content="Hello from AnthrOpus!", tool_calls=None))
    ]
    mock_resp.usage = MagicMock(prompt_tokens=20, completion_tokens=10)

    with patch("litellm.completion", return_value=mock_resp) as mock_completion, \
         patch("agent_router.post_agent_response", return_value={"ok": True, "ts": "123.456"}), \
         patch("agent_router.fetch_thread_history", return_value=[]), \
         patch("agent_router.fetch_thread_files", return_value=[]), \
         patch("agent_router.record_thread_tokens"), \
         patch("agent_router.trigger_thread_notes_update"), \
         patch("agent_router.trigger_usage_update"):

        res = await agent_router.process_and_respond(
            channel_id="C123",
            thread_ts="1234567890.123456",
            raw_text="[AnthrOpus_Innovator] Analyze this architecture",
            is_bot_trigger=False,
        )

        assert res["ok"] is True
        assert mock_completion.call_count == 1
        kwargs = mock_completion.call_args.kwargs
        assert kwargs["model"] == "anthropic/claude-opus-5"
        assert kwargs.get("cache_control") == {"type": "ephemeral"}


@pytest.mark.anyio
async def test_non_anthropic_agent_omits_cache_control():
    """Verify that non-Anthropic agents (e.g. Gemini, Mistral, OpenAI) do not receive cache_control."""
    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(message=MagicMock(content="Hello from Gemini!", tool_calls=None))
    ]
    mock_resp.usage = MagicMock(prompt_tokens=20, completion_tokens=10)

    with patch("litellm.completion", return_value=mock_resp) as mock_completion, \
         patch("agent_router.post_agent_response", return_value={"ok": True, "ts": "123.456"}), \
         patch("agent_router.fetch_thread_history", return_value=[]), \
         patch("agent_router.fetch_thread_files", return_value=[]), \
         patch("agent_router.record_thread_tokens"), \
         patch("agent_router.trigger_thread_notes_update"), \
         patch("agent_router.trigger_usage_update"):

        res = await agent_router.process_and_respond(
            channel_id="C123",
            thread_ts="1234567890.123456",
            raw_text="[Gemini_Visionary] Plan system roadmap",
            is_bot_trigger=False,
        )

        assert res["ok"] is True
        assert mock_completion.call_count == 1
        kwargs = mock_completion.call_args.kwargs
        assert kwargs["model"] == "gemini/gemini-3.7-flash"
        assert "cache_control" not in kwargs


@pytest.mark.anyio
async def test_anthropic_tool_call_follow_up_passes_cache_control():
    """Verify that follow-up completion after MCP tool call also passes cache_control for Anthropic models."""
    tool_call_mock = MagicMock()
    tool_call_mock.id = "call_abc123"
    tool_call_mock.function.name = "read_file"
    tool_call_mock.function.arguments = '{"path": "test.txt"}'

    first_resp = MagicMock()
    first_resp.choices = [
        MagicMock(message=MagicMock(content=None, tool_calls=[tool_call_mock], model_dump=lambda: {"role": "assistant", "content": None}))
    ]
    first_resp.usage = MagicMock(prompt_tokens=30, completion_tokens=10)

    second_resp = MagicMock()
    second_resp.choices = [
        MagicMock(message=MagicMock(content="Here is the file analysis.", tool_calls=None))
    ]
    second_resp.usage = MagicMock(prompt_tokens=40, completion_tokens=15)

    with patch("litellm.completion", side_effect=[first_resp, second_resp]) as mock_completion, \
         patch("agent_router.execute_mcp_tool", return_value="Sample file content"), \
         patch("agent_router.post_agent_response", return_value={"ok": True, "ts": "123.456"}), \
         patch("agent_router.fetch_thread_history", return_value=[]), \
         patch("agent_router.fetch_thread_files", return_value=[]), \
         patch("agent_router.record_thread_tokens"), \
         patch("agent_router.trigger_thread_notes_update"), \
         patch("agent_router.trigger_usage_update"):

        res = await agent_router.process_and_respond(
            channel_id="C123",
            thread_ts="1234567890.123456",
            raw_text="[AnthrOpus_Sceptic] Check the file contents",
            is_bot_trigger=False,
        )

        assert res["ok"] is True
        assert mock_completion.call_count == 2

        # First call (with tools)
        first_call_kwargs = mock_completion.call_args_list[0].kwargs
        assert first_call_kwargs["model"] == "anthropic/claude-opus-5"
        assert first_call_kwargs.get("cache_control") == {"type": "ephemeral"}

        # Follow-up call (after tool execution)
        second_call_kwargs = mock_completion.call_args_list[1].kwargs
        assert second_call_kwargs["model"] == "anthropic/claude-opus-5"
        assert second_call_kwargs.get("cache_control") == {"type": "ephemeral"}


@pytest.mark.anyio
async def test_thread_notes_agent_caching_support(monkeypatch):
    """Verify thread notes agent passes cache_control when configured with Anthropic model."""
    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(message=MagicMock(content="## Notes\nDiscussion notes summary."))
    ]
    mock_resp.usage = MagicMock(prompt_tokens=50, completion_tokens=25)

    # Test with Anthropic model override
    monkeypatch.setattr("agents.thread_notes_agent.THREAD_NOTES_MODEL", "anthropic/claude-opus-5")

    with patch("agents.thread_notes_agent.litellm.completion", return_value=mock_resp) as mock_completion:
        content, usage = await generate_thread_notes(
            history=[{"role": "user", "content": "Thread context"}],
            is_completed=False,
        )

        assert "Discussion notes summary" in content
        assert mock_completion.call_count == 1
        kwargs = mock_completion.call_args.kwargs
        assert kwargs["model"] == "anthropic/claude-opus-5"
        assert kwargs.get("cache_control") == {"type": "ephemeral"}


@pytest.mark.anyio
async def test_pricing_agent_caching_support(monkeypatch):
    """Verify pricing agent passes cache_control when configured with Anthropic model."""
    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(message=MagicMock(content='{"input_price_per_1m": 15.0, "output_price_per_1m": 75.0, "source": "Anthropic"}', tool_calls=None))
    ]

    # Test with Anthropic model override
    monkeypatch.setattr("pricing_service.PRICING_AGENT_MODEL", "anthropic/claude-opus-5")

    with patch("pricing_service.litellm.completion", return_value=mock_resp) as mock_completion:
        pricing = await pricing_service.fetch_pricing_with_agent("claude-opus-5")

        assert pricing["input_price_per_1m"] == 15.0
        assert mock_completion.call_count == 1
        kwargs = mock_completion.call_args.kwargs
        assert kwargs["model"] == "anthropic/claude-opus-5"
        assert kwargs.get("cache_control") == {"type": "ephemeral"}
