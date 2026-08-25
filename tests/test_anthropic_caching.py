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
        assert "cache_control" not in kwargs
        assert kwargs["messages"][0]["role"] == "system"
        assert kwargs["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}


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
        assert isinstance(kwargs["messages"][0]["content"], str)


@pytest.mark.anyio
async def test_anthropic_tool_call_follow_up_passes_cache_control():
    """Verify that follow-up completion after MCP tool call maintains cache_control in messages for Anthropic models."""
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
        assert "cache_control" not in first_call_kwargs
        assert first_call_kwargs["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}

        # Follow-up call (after tool execution)
        second_call_kwargs = mock_completion.call_args_list[1].kwargs
        assert second_call_kwargs["model"] == "anthropic/claude-opus-5"
        assert "cache_control" not in second_call_kwargs
        assert second_call_kwargs["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}


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
        assert "cache_control" not in kwargs
        assert kwargs["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}


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
        assert "cache_control" not in kwargs
        assert kwargs["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}


def _count_cache_control_breakpoints(messages: list[dict]) -> int:
    """Helper to count total cache_control breakpoints across all messages."""
    count = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and "cache_control" in block:
                    count += 1
    return count


def test_build_llm_messages_anthropic_caching_with_file():
    """Verify Anthropic model message building caches both system prompt and uploaded file block."""
    file_context = {
        "file_name": "data_dump.sql",
        "content": "CREATE TABLE users (id INT);",
        "size": 31,
        "authorized_agents": None,
    }
    messages = agent_router.build_llm_messages(
        system_prompt="You are Innovator.",
        effective_history=[],
        prompt="Review database schema",
        is_bot_trigger=False,
        agent_key="AnthrOpus_Innovator",
        agent_cfg={"model": "anthropic/claude-opus-5", "username": "AnthrOpus Innovator"},
        file_context=file_context,
    )
    # Breakpoint 1: System prompt
    assert messages[0]["role"] == "system"
    assert messages[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    # Breakpoint 2: Attached file block in user turn
    assert messages[1]["role"] == "user"
    assert isinstance(messages[1]["content"], list)
    assert messages[1]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert "data_dump.sql" in messages[1]["content"][0]["text"]
    assert "Review database schema" in messages[1]["content"][1]["text"]
    assert _count_cache_control_breakpoints(messages) == 2


def test_build_llm_messages_anthropic_caching_with_long_history_and_file():
    """Verify Anthropic model message building caps total cache breakpoints at exactly 4 with history and file."""
    history = [
        {"role": "user", "content": "Question 1"},
        {"role": "assistant", "content": "Answer 1"},
        {"role": "user", "content": "Question 2"},
        {"role": "assistant", "content": "Answer 2"},
        {"role": "user", "content": "Question 3"},
    ]
    file_context = {
        "file_name": "architecture.md",
        "content": "# Arch Doc",
        "size": 10,
        "authorized_agents": None,
    }
    messages = agent_router.build_llm_messages(
        system_prompt="You are Sceptic.",
        effective_history=history,
        prompt="Check this",
        is_bot_trigger=True,
        agent_key="AnthrOpus_Sceptic",
        agent_cfg={"model": "anthropic/claude-opus-5", "username": "AnthrOpus Sceptic"},
        file_context=file_context,
    )
    # Breakpoints:
    # 1. System prompt
    # 2. History[0]
    # 3. History[1]
    # 4. File block in final active turn
    assert _count_cache_control_breakpoints(messages) == 4
    assert messages[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert messages[1]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert messages[2]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert messages[-1]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert "architecture.md" in messages[-1]["content"][0]["text"]


def test_build_llm_messages_anthropic_caching_without_file_uses_3_history_slots():
    """Verify Anthropic model message building uses 3 history cache slots when no file is present."""
    history = [
        {"role": "user", "content": "Turn 1"},
        {"role": "assistant", "content": "Turn 2"},
        {"role": "user", "content": "Turn 3"},
        {"role": "assistant", "content": "Turn 4"},
    ]
    messages = agent_router.build_llm_messages(
        system_prompt="You are Sceptic.",
        effective_history=history,
        prompt="Turn 5",
        is_bot_trigger=True,
        agent_key="AnthrOpus_Sceptic",
        agent_cfg={"model": "anthropic/claude-opus-5", "username": "AnthrOpus Sceptic"},
        file_context=None,
    )
    # 1 on system + 3 on early history = 4 total breakpoints
    assert _count_cache_control_breakpoints(messages) == 4
    assert messages[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert messages[1]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert messages[2]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert messages[3]["content"][0]["cache_control"] == {"type": "ephemeral"}
