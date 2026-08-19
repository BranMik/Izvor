import pytest
from agent_router import parse_agent_and_prompt


def test_parse_explicit_bracket_tags():
    agent_key, prompt, was_explicit = parse_agent_and_prompt(
        "<@U12345> [Gemini_Visionary] Create a new UI flow", "C12345", "123456789.0001"
    )
    assert agent_key == "Gemini_Visionary"
    assert prompt == "Create a new UI flow"
    assert was_explicit is True


def test_parse_mistral_techlead_tag():
    agent_key, prompt, was_explicit = parse_agent_and_prompt(
        "[Mistral_TechLead] Check this database query", "C12345", "123456789.0001"
    )
    assert agent_key == "Mistral_TechLead"
    assert prompt == "Check this database query"
    assert was_explicit is True


def test_parse_multiple_tags_first_invalid():
    agent_key, prompt, was_explicit = parse_agent_and_prompt(
        "[InvalidTag] [Mistral_TechLead] Check this database query", "C12345", "123456789.0001"
    )
    assert agent_key == "Mistral_TechLead"
    assert prompt == "[InvalidTag]  Check this database query"
    assert was_explicit is True


def test_parse_at_mention_persona():
    agent_key, prompt, was_explicit = parse_agent_and_prompt(
        "<@U12345> @CaiLogics Innovator generate an alternative algorithm",
        "C12345",
        "123456789.0001",
    )
    assert agent_key == "Mistral_Innovator"
    assert prompt == "generate an alternative algorithm"
    assert was_explicit is True


def test_parse_fallback_default_agent():
    agent_key, prompt, was_explicit = parse_agent_and_prompt(
        "<@U12345> Tell me how to structure this application",
        "C12345",
        "123456789.0001",
    )
    assert agent_key == "Gemini_Visionary"
    assert prompt == "Tell me how to structure this application"
    assert was_explicit is False


@pytest.mark.anyio
async def test_process_and_respond_token_appending(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    from agent_router import process_and_respond

    # Mock litellm.completion response
    mock_usage = MagicMock()
    mock_usage.prompt_tokens = 150
    mock_usage.completion_tokens = 50

    mock_choice = MagicMock()
    mock_choice.message.content = "This is a response message."
    mock_choice.message.tool_calls = None

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.usage = mock_usage

    async def mock_run_in_threadpool(func, *args, **kwargs):
        return mock_response

    posted_messages = []
    async def mock_post_agent_response(channel_id, thread_ts, text, username, icon_url=None):
        posted_messages.append(text)
        return {"ok": True, "ts": "12345.6789"}

    monkeypatch.setattr("agent_router.run_in_threadpool", mock_run_in_threadpool)
    monkeypatch.setattr("agent_router.post_agent_response", mock_post_agent_response)
    monkeypatch.setattr("agent_router.fetch_thread_history", AsyncMock(return_value=[]))

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.6789",
        raw_text="Hello [Gemini_Visionary]",
    )

    assert len(posted_messages) == 1
    assert posted_messages[0].endswith("\n\n[150/50]")
