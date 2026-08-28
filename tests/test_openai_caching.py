import logging
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from llm_utils import (
    extract_cached_tokens,
    extract_openai_cache_usage,
    is_openai_model,
    log_openai_cached_tokens,
)
import agent_router
from agents.thread_notes_agent import generate_thread_notes
import pricing_service


def test_is_openai_model_detection():
    """Verify that is_openai_model accurately identifies OpenAI models."""
    assert is_openai_model("openai/gpt-5.6-terra") is True
    assert is_openai_model("openai/gpt-4o") is True
    assert is_openai_model("openai/gpt-4o-mini") is True
    assert is_openai_model("gpt-4o") is True
    assert is_openai_model("gpt-3.5-turbo") is True
    assert is_openai_model("openai/o1") is True
    assert is_openai_model("openai/o3-mini") is True
    assert is_openai_model("o1-preview") is True
    assert is_openai_model("o3-mini") is True
    assert is_openai_model("chatgpt-4o-latest") is True
    assert is_openai_model("OPENAI/GPT-4O") is True

    assert is_openai_model("anthropic/claude-opus-5") is False
    assert is_openai_model("gemini/gemini-3.7-flash") is False
    assert is_openai_model("mistral/mistral-large-latest") is False
    assert is_openai_model("groq/llama-3.3-70b") is False
    assert is_openai_model("") is False
    assert is_openai_model(None) is False
    assert is_openai_model(123) is False


def test_extract_cached_tokens():
    """Verify extract_cached_tokens handles various response/usage formats."""
    # 1. Mock with prompt_tokens_details.cached_tokens
    mock_resp = MagicMock()
    mock_resp.usage.prompt_tokens_details.cached_tokens = 128
    assert extract_cached_tokens(mock_resp) == 128

    # 2. Mock with cached_tokens directly on usage
    mock_resp2 = MagicMock()
    mock_resp2.usage = MagicMock(spec=["cached_tokens"])
    mock_resp2.usage.cached_tokens = 64
    assert extract_cached_tokens(mock_resp2) == 64

    # 3. Dict usage with prompt_tokens_details
    dict_resp = {"usage": {"prompt_tokens_details": {"cached_tokens": 256}}}
    assert extract_cached_tokens(dict_resp) == 256

    # 4. Direct dict usage
    dict_usage = {"prompt_tokens": 100, "cached_tokens": 32}
    assert extract_cached_tokens(dict_usage) == 32

    # 5. Empty / default mock (without cached_tokens set)
    mock_default = MagicMock()
    assert extract_cached_tokens(mock_default) == 0

    # 6. None or empty
    assert extract_cached_tokens(None) == 0
    assert extract_cached_tokens({}) == 0


@pytest.fixture(autouse=True)
def setup_test_environment(monkeypatch):
    monkeypatch.setattr(agent_router.asyncio, "sleep", AsyncMock(return_value=None))


@pytest.mark.anyio
async def test_openai_agent_logs_cached_tokens(caplog):
    """Verify that calling an OpenAI agent logs cached_tokens."""
    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(message=MagicMock(content="Hello from Terra!", tool_calls=None))
    ]
    mock_resp.usage = MagicMock(prompt_tokens=100, completion_tokens=50)
    mock_resp.usage.prompt_tokens_details = MagicMock(cached_tokens=42)

    with caplog.at_level(logging.INFO), \
         patch("litellm.completion", return_value=mock_resp) as mock_completion, \
         patch("agent_router.post_agent_response", return_value={"ok": True, "ts": "123.456"}), \
         patch("agent_router.fetch_thread_history", return_value=[]), \
         patch("agent_router.fetch_thread_files", return_value=[]), \
         patch("agent_router.record_thread_tokens"), \
         patch("agent_router.trigger_thread_notes_update"), \
         patch("agent_router.trigger_usage_update"):

        res = await agent_router.process_and_respond(
            channel_id="C123",
            thread_ts="1234567890.123456",
            raw_text="[OpAiTerra_Architect] Architecture review",
            is_bot_trigger=False,
        )

        assert res["ok"] is True
        assert mock_completion.call_count == 1
        assert "[OPENAI] cached_tokens: 42" in caplog.text
        assert "openai/gpt-5.6-terra" in caplog.text


@pytest.mark.anyio
async def test_non_openai_agent_does_not_log_openai_cached_tokens(caplog):
    """Verify that calling a non-OpenAI agent (e.g. Gemini, Mistral, Anthropic) does NOT log [OPENAI] cached_tokens."""
    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(message=MagicMock(content="Hello from Gemini!", tool_calls=None))
    ]
    mock_resp.usage = MagicMock(prompt_tokens=20, completion_tokens=10)

    with caplog.at_level(logging.INFO), \
         patch("litellm.completion", return_value=mock_resp), \
         patch("agent_router.post_agent_response", return_value={"ok": True, "ts": "123.456"}), \
         patch("agent_router.fetch_thread_history", return_value=[]), \
         patch("agent_router.fetch_thread_files", return_value=[]), \
         patch("agent_router.record_thread_tokens"), \
         patch("agent_router.trigger_thread_notes_update"), \
         patch("agent_router.trigger_usage_update"):

        res = await agent_router.process_and_respond(
            channel_id="C123",
            thread_ts="1234567890.123456",
            raw_text="[Gemini_Visionary] Plan roadmap",
            is_bot_trigger=False,
        )

        assert res["ok"] is True
        assert "[OPENAI] cached_tokens:" not in caplog.text


@pytest.mark.anyio
async def test_openai_tool_call_follow_up_logs_cached_tokens(caplog):
    """Verify follow-up completion after tool call also logs cached_tokens for OpenAI models."""
    tool_call_mock = MagicMock()
    tool_call_mock.id = "call_xyz123"
    tool_call_mock.function.name = "read_file"
    tool_call_mock.function.arguments = '{"path": "test.txt"}'

    first_resp = MagicMock()
    first_resp.choices = [
        MagicMock(message=MagicMock(content=None, tool_calls=[tool_call_mock], model_dump=lambda: {"role": "assistant", "content": None}))
    ]
    first_resp.usage = MagicMock(prompt_tokens=30, completion_tokens=10)
    first_resp.usage.prompt_tokens_details = MagicMock(cached_tokens=10)

    second_resp = MagicMock()
    second_resp.choices = [
        MagicMock(message=MagicMock(content="Analysis complete.", tool_calls=None))
    ]
    second_resp.usage = MagicMock(prompt_tokens=40, completion_tokens=15)
    second_resp.usage.prompt_tokens_details = MagicMock(cached_tokens=25)

    with caplog.at_level(logging.INFO), \
         patch("litellm.completion", side_effect=[first_resp, second_resp]) as mock_completion, \
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
            raw_text="[OpAiTerra_Architect] Check file",
            is_bot_trigger=False,
        )

        assert res["ok"] is True
        assert mock_completion.call_count == 2
        assert "[OPENAI] cached_tokens: 10" in caplog.text
        assert "[OPENAI] cached_tokens: 25" in caplog.text
        assert "follow-up" in caplog.text


@pytest.mark.anyio
async def test_thread_notes_agent_logs_cached_tokens_when_openai(caplog, monkeypatch):
    """Verify thread notes agent logs cached_tokens when configured with an OpenAI model."""
    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(message=MagicMock(content="## Notes\nSummary notes."))
    ]
    mock_resp.usage = MagicMock(prompt_tokens=50, completion_tokens=25)
    mock_resp.usage.prompt_tokens_details = MagicMock(cached_tokens=30)

    monkeypatch.setattr("agents.thread_notes_agent.THREAD_NOTES_MODEL", "openai/gpt-4o")

    with caplog.at_level(logging.INFO), \
         patch("agents.thread_notes_agent.litellm.completion", return_value=mock_resp):
        content, usage = await generate_thread_notes(
            history=[{"role": "user", "content": "Thread context"}],
            is_completed=False,
        )

        assert "Summary notes" in content
        assert "[OPENAI] cached_tokens: 30" in caplog.text
        assert "openai/gpt-4o" in caplog.text


@pytest.mark.anyio
async def test_pricing_agent_logs_cached_tokens_when_openai(caplog, monkeypatch):
    """Verify pricing agent logs cached_tokens when configured with an OpenAI model."""
    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(message=MagicMock(content='{"input_price_per_1m": 2.5, "output_price_per_1m": 10.0, "source": "OpenAI"}', tool_calls=None))
    ]
    mock_resp.usage = MagicMock(prompt_tokens=60, completion_tokens=20)
    mock_resp.usage.prompt_tokens_details = MagicMock(cached_tokens=55)

    monkeypatch.setattr("pricing_service.PRICING_AGENT_MODEL", "openai/gpt-4o")

    with caplog.at_level(logging.INFO), \
         patch("pricing_service.litellm.completion", return_value=mock_resp):
        pricing = await pricing_service.fetch_pricing_with_agent("gpt-4o")

        assert pricing["input_price_per_1m"] == 2.5
        assert "[OPENAI] cached_tokens: 55" in caplog.text
        assert "openai/gpt-4o" in caplog.text


def test_extract_openai_cache_usage():
    """Verify extract_openai_cache_usage handles explicit and implicit cache write metrics."""
    # 1. Explicit cache_write_tokens in prompt_tokens_details
    m1 = MagicMock()
    m1.usage.prompt_tokens = 800
    m1.usage.prompt_tokens_details.cached_tokens = 200
    m1.usage.prompt_tokens_details.cache_write_tokens = 600
    assert extract_openai_cache_usage(m1) == {
        "cached_tokens": 200,
        "cache_write_tokens": 600,
    }

    # 2. Implicit calculation when prompt_tokens >= 1024 (automatic OpenAI caching threshold)
    m2 = MagicMock()
    m2.usage.prompt_tokens = 2048
    m2.usage.prompt_tokens_details = MagicMock(spec=["cached_tokens"])
    m2.usage.prompt_tokens_details.cached_tokens = 1024
    assert extract_openai_cache_usage(m2) == {
        "cached_tokens": 1024,
        "cache_write_tokens": 1024,
    }

    # 3. Prompt tokens < 1024 with no explicit cache_write
    m3 = MagicMock()
    m3.usage.prompt_tokens = 500
    m3.usage.prompt_tokens_details = MagicMock(spec=["cached_tokens"])
    m3.usage.prompt_tokens_details.cached_tokens = 0
    assert extract_openai_cache_usage(m3) == {
        "cached_tokens": 0,
        "cache_write_tokens": 0,
    }

    # 4. Dict usage with explicit cache_write_tokens
    d1 = {
        "usage": {
            "prompt_tokens": 1500,
            "prompt_tokens_details": {
                "cached_tokens": 1024,
                "cache_write_tokens": 476,
            },
        }
    }
    assert extract_openai_cache_usage(d1) == {
        "cached_tokens": 1024,
        "cache_write_tokens": 476,
    }

    # 5. Empty / None
    assert extract_openai_cache_usage(None) == {
        "cached_tokens": 0,
        "cache_write_tokens": 0,
    }
    assert extract_openai_cache_usage({}) == {
        "cached_tokens": 0,
        "cache_write_tokens": 0,
    }


@pytest.mark.anyio
async def test_openai_agent_logs_cache_write_tokens(caplog):
    """Verify OpenAI agent execution logs both cached_tokens (read) and cache_write_tokens."""
    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(message=MagicMock(content="Architecture proposal", tool_calls=None))
    ]
    mock_resp.usage = MagicMock(prompt_tokens=2048, completion_tokens=100)
    mock_resp.usage.prompt_tokens_details = MagicMock(spec=["cached_tokens"])
    mock_resp.usage.prompt_tokens_details.cached_tokens = 1024

    with caplog.at_level(logging.INFO), \
         patch("litellm.completion", return_value=mock_resp) as mock_completion, \
         patch("agent_router.post_agent_response", return_value={"ok": True, "ts": "123.456"}), \
         patch("agent_router.fetch_thread_history", return_value=[]), \
         patch("agent_router.fetch_thread_files", return_value=[]), \
         patch("agent_router.record_thread_tokens"), \
         patch("agent_router.trigger_thread_notes_update"), \
         patch("agent_router.trigger_usage_update"):

        res = await agent_router.process_and_respond(
            channel_id="C123",
            thread_ts="1234567890.123456",
            raw_text="[OpAiTerra_Architect] Review the design",
            is_bot_trigger=False,
        )

        assert res["ok"] is True
        assert mock_completion.call_count == 1
        assert "[OPENAI] cached_tokens: 1024 | cache_write_tokens: 1024" in caplog.text
