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


@pytest.mark.anyio
async def test_process_and_respond_custom_sequence(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    from agent_router import process_and_respond, REMAINING_AGENTS_MAP, CUSTOM_FLOW_THREADS

    # Clear maps
    REMAINING_AGENTS_MAP.clear()
    CUSTOM_FLOW_THREADS.clear()

    # Mock litellm.completion response
    mock_choice = MagicMock()
    mock_choice.message.content = "This is a custom response."
    mock_choice.message.tool_calls = None

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.usage = None

    async def mock_run_in_threadpool(func, *args, **kwargs):
        # Capture the system prompt passed to completion
        captured_messages = kwargs.get("messages") or args[2]
        mock_run_in_threadpool.captured_sys_prompt = captured_messages[0]["content"]
        return mock_response

    posted_messages = []
    async def mock_post_agent_response(channel_id, thread_ts, text, username, icon_url=None):
        posted_messages.append(text)
        return {"ok": True, "ts": "12345.6789"}

    monkeypatch.setattr("agent_router.run_in_threadpool", mock_run_in_threadpool)
    monkeypatch.setattr("agent_router.post_agent_response", mock_post_agent_response)
    monkeypatch.setattr("agent_router.fetch_thread_history", AsyncMock(return_value=[]))

    thread_key = ("C123", "12345.6789")

    # Step 1: User message tags two agents (Mistral_TechLead and OpenAi_Sceptic)
    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.6789",
        raw_text="[Mistral_TechLead] [OpenAi_Sceptic] check this",
        is_bot_trigger=False,
    )

    # Verify that:
    # 1. The first agent (Mistral_TechLead) spoke.
    # 2. OpenAi_Sceptic is left in REMAINING_AGENTS_MAP.
    # 3. Thread is marked in CUSTOM_FLOW_THREADS.
    # 4. Mistral_TechLead did NOT get force_termination block (system prompt does NOT contain CRITICAL CONSTRAINT).
    assert thread_key in CUSTOM_FLOW_THREADS
    assert REMAINING_AGENTS_MAP[thread_key] == ["OpenAi_Sceptic"]
    assert "CRITICAL CONSTRAINT" not in mock_run_in_threadpool.captured_sys_prompt

    # Step 2: Bot trigger for OpenAi_Sceptic (the last agent in the custom sequence)
    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.6789",
        raw_text="[OpenAi_Sceptic] next",
        is_bot_trigger=True,
    )

    # Verify that:
    # 1. OpenAi_Sceptic was the last one (remaining is empty).
    # 2. OpenAi_Sceptic got forced termination (system prompt contains CRITICAL CONSTRAINT).
    # 3. State has been cleaned up.
    assert thread_key not in CUSTOM_FLOW_THREADS
    assert thread_key not in REMAINING_AGENTS_MAP
    assert "CRITICAL CONSTRAINT" in mock_run_in_threadpool.captured_sys_prompt
