import pytest
from unittest.mock import AsyncMock, MagicMock
from agent_router import (
    parse_agent_and_prompt,
    process_and_respond,
    enforce_valid_nomination,
    get_initial_agents,
    REMAINING_AGENTS_MAP,
    CUSTOM_FLOW_THREADS,
)


import agent_router

@pytest.fixture(autouse=True)
def patch_asyncio_sleep(monkeypatch):
    monkeypatch.setattr(agent_router.asyncio, "sleep", AsyncMock(return_value=None))


def test_parse_explicit_bracket_tags():
    agent_key, prompt, was_explicit = parse_agent_and_prompt(
        "<@U12345> [Gemini_Visionary] Create a new UI flow", "C12345", "123456789.0001"
    )
    assert agent_key == "Gemini_Visionary"
    assert prompt == "Create a new UI flow"
    assert was_explicit is True


def test_parse_mistral_techlead_tag():
    agent_key, prompt, was_explicit = parse_agent_and_prompt(
        "[MistLar_TechLead] Check this database query", "C12345", "123456789.0001"
    )
    assert agent_key == "MistLar_TechLead"
    assert prompt == "Check this database query"
    assert was_explicit is True


def test_parse_multiple_tags_first_invalid():
    agent_key, prompt, was_explicit = parse_agent_and_prompt(
        "[InvalidTag] [MistLar_TechLead] Check this database query", "C12345", "123456789.0001"
    )
    assert agent_key == "MistLar_TechLead"
    assert prompt == "[InvalidTag]  Check this database query"
    assert was_explicit is True


def test_parse_at_mention_persona():
    agent_key, prompt, was_explicit = parse_agent_and_prompt(
        "<@U12345> @Gemini Visionary generate a concept",
        "C12345",
        "123456789.0001",
    )
    assert agent_key == "Gemini_Visionary"
    assert prompt == "generate a concept"
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
async def test_scenario_1_single_agent_tagged(monkeypatch):
    """
    Scenario: User tags ONLY one agent.
    Expected: Single agent speaks, force_termination=True, any generated nomination tag stripped, flow cleanly ends.
    """
    REMAINING_AGENTS_MAP.clear()
    CUSTOM_FLOW_THREADS.clear()

    mock_choice = MagicMock()
    # Model attempts to nominate Sceptic even though user only wanted 1 agent
    mock_choice.message.content = "My single vision response. [AnthrOpus_Sceptic]"
    mock_choice.message.tool_calls = None

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.usage = None

    async def mock_run_in_threadpool(func, *args, **kwargs):
        captured_messages = kwargs.get("messages") or args[2]
        mock_run_in_threadpool.captured_sys_prompt = captured_messages[0]["content"]
        return mock_response

    posted_messages = []
    async def mock_post_agent_response(channel_id, thread_ts, text, username, icon_url=None):
        posted_messages.append(text)
        return {"ok": True, "ts": "12345.0001"}

    monkeypatch.setattr("agent_router.run_in_threadpool", mock_run_in_threadpool)
    monkeypatch.setattr("agent_router.post_agent_response", mock_post_agent_response)
    monkeypatch.setattr("agent_router.fetch_thread_history", AsyncMock(return_value=[]))

    thread_key = ("C123", "12345.0001")

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0001",
        raw_text="[Gemini_Visionary] Explain this concept only",
        is_bot_trigger=False,
    )

    # Verify:
    # 1. System prompt forced termination
    assert "CRITICAL CONSTRAINT" in mock_run_in_threadpool.captured_sys_prompt
    # 2. State cleaned up
    assert thread_key not in CUSTOM_FLOW_THREADS
    assert thread_key not in REMAINING_AGENTS_MAP
    # 3. Posted message has stripped bracket tags (so no next bot is triggered)
    assert len(posted_messages) == 1
    assert "[AnthrOpus_Sceptic]" not in posted_messages[0]
    assert "AnthrOpus_Sceptic" in posted_messages[0]


@pytest.mark.anyio
async def test_scenario_2_multiple_agents_custom_flow(monkeypatch):
    """
    Scenario: User tags multiple specific agents.
    Expected: Only those agents speak in order, intermediate choices are validated, and flow ends after the last one.
    """
    REMAINING_AGENTS_MAP.clear()
    CUSTOM_FLOW_THREADS.clear()

    posted_messages = []
    async def mock_post_agent_response(channel_id, thread_ts, text, username, icon_url=None):
        posted_messages.append(text)
        return {"ok": True, "ts": f"12345.{len(posted_messages)}"}

    monkeypatch.setattr("agent_router.post_agent_response", mock_post_agent_response)
    monkeypatch.setattr("agent_router.fetch_thread_history", AsyncMock(return_value=[]))

    thread_key = ("C123", "12345.0002")

    # Step 1: User requests 2 specific agents: [AnthrOpus_Innovator] and [MistLar_TechLead]
    mock_choice_1 = MagicMock()
    # Innovator attempts to nominate Architect prematurely (Architect was not in user's list)
    mock_choice_1.message.content = "Innovative idea. [OpAiTerra_Architect]"
    mock_choice_1.message.tool_calls = None
    mock_response_1 = MagicMock(choices=[mock_choice_1], usage=None)

    async def mock_run_1(func, *args, **kwargs):
        return mock_response_1
    monkeypatch.setattr("agent_router.run_in_threadpool", mock_run_1)

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0002",
        raw_text="[AnthrOpus_Innovator] [MistLar_TechLead] Debate this feature",
        is_bot_trigger=False,
    )

    # Innovator's premature Architect nomination should be corrected to MistLar_TechLead
    assert thread_key in CUSTOM_FLOW_THREADS
    assert REMAINING_AGENTS_MAP[thread_key] == ["MistLar_TechLead"]
    assert "[MistLar_TechLead]" in posted_messages[0]

    # Step 2: Next bot trigger for MistLar_TechLead (the last custom agent)
    mock_choice_2 = MagicMock()
    mock_choice_2.message.content = "Tech lead evaluation and conclusion. [OpAiTerra_Architect]"
    mock_choice_2.message.tool_calls = None
    mock_response_2 = MagicMock(choices=[mock_choice_2], usage=None)

    async def mock_run_2(func, *args, **kwargs):
        captured_messages = kwargs.get("messages") or args[2]
        mock_run_2.captured_sys_prompt = captured_messages[0]["content"]
        return mock_response_2
    monkeypatch.setattr("agent_router.run_in_threadpool", mock_run_2)

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0002",
        raw_text="[MistLar_TechLead] Debate this feature",
        is_bot_trigger=True,
    )

    # Last custom agent cleanly terminates
    assert "CRITICAL CONSTRAINT" in mock_run_2.captured_sys_prompt
    assert thread_key not in CUSTOM_FLOW_THREADS
    assert thread_key not in REMAINING_AGENTS_MAP
    assert "[OpAiTerra_Architect]" not in posted_messages[1]


@pytest.mark.anyio
async def test_scenario_3_no_agent_tagged_default_flow(monkeypatch):
    """
    Scenario: User tags NO agent (plain message / app_mention).
    Expected: Default flow starts with Gemini_Visionary, initializes all 14 panel agents,
    programmatically prevents premature Architect nomination, and finishes with OpAiTerra_Architect.
    """
    REMAINING_AGENTS_MAP.clear()
    CUSTOM_FLOW_THREADS.clear()

    posted_messages = []
    async def mock_post_agent_response(channel_id, thread_ts, text, username, icon_url=None):
        posted_messages.append(text)
        return {"ok": True, "ts": f"12345.{len(posted_messages)}"}

    monkeypatch.setattr("agent_router.post_agent_response", mock_post_agent_response)
    monkeypatch.setattr("agent_router.fetch_thread_history", AsyncMock(return_value=[]))

    thread_key = ("C123", "12345.0003")

    # Step 1: Plain user prompt (no tags)
    mock_choice_1 = MagicMock()
    mock_choice_1.message.content = "Vision for the product. [AnthrOpus_Sceptic]"
    mock_choice_1.message.tool_calls = None
    mock_response_1 = MagicMock(choices=[mock_choice_1], usage=None)

    async def mock_run_1(func, *args, **kwargs):
        return mock_response_1
    monkeypatch.setattr("agent_router.run_in_threadpool", mock_run_1)

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0003",
        raw_text="<@U12345> Give me commercial ideas for Benford Law",
        is_bot_trigger=False,
    )

    # Default starts with Gemini_Visionary and initializes all panel agents
    assert thread_key not in CUSTOM_FLOW_THREADS
    assert len(REMAINING_AGENTS_MAP[thread_key]) == 14
    assert "[AnthrOpus_Sceptic]" in posted_messages[0]

    # Step 2: Intermediate agent (Monitoring Specialist) tries to prematurely nominate Architect
    REMAINING_AGENTS_MAP[thread_key] = [
        "OpAiTerra_UserAdvocate",
        "MistLar_SecurityArchitect",
        "MistMed_MonitoringAndAlertingSpecialist",
    ]

    mock_choice_2 = MagicMock()
    mock_choice_2.message.content = "Monitoring review. [OpAiTerra_Architect]"
    mock_choice_2.message.tool_calls = None
    mock_response_2 = MagicMock(choices=[mock_choice_2], usage=None)

    async def mock_run_2(func, *args, **kwargs):
        return mock_response_2
    monkeypatch.setattr("agent_router.run_in_threadpool", mock_run_2)

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0003",
        raw_text="[MistMed_MonitoringAndAlertingSpecialist] next",
        is_bot_trigger=True,
    )

    # Premature Architect tag is programmatically replaced with first remaining agent (OpAiTerra_UserAdvocate)
    assert "[OpAiTerra_UserAdvocate]" in posted_messages[1]
    assert "OpAiTerra_UserAdvocate" in REMAINING_AGENTS_MAP[thread_key]

    # Step 3: Final intermediate agent speaks when remaining is empty -> nominates Architect
    REMAINING_AGENTS_MAP[thread_key] = ["MistLar_SecurityArchitect"]

    mock_choice_3 = MagicMock()
    mock_choice_3.message.content = "Security review finished."
    mock_choice_3.message.tool_calls = None
    mock_response_3 = MagicMock(choices=[mock_choice_3], usage=None)

    async def mock_run_3(func, *args, **kwargs):
        return mock_response_3
    monkeypatch.setattr("agent_router.run_in_threadpool", mock_run_3)

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0003",
        raw_text="[MistLar_SecurityArchitect] next",
        is_bot_trigger=True,
    )

    # Now remaining is empty, so [OpAiTerra_Architect] is nominated for final summary
    assert REMAINING_AGENTS_MAP[thread_key] == []
    assert "[OpAiTerra_Architect]" in posted_messages[2]

    # Step 4: OpAiTerra_Architect executes final summary
    mock_choice_4 = MagicMock()
    mock_choice_4.message.content = "Final architecture summary. [Gemini_Visionary]"
    mock_choice_4.message.tool_calls = None
    mock_response_4 = MagicMock(choices=[mock_choice_4], usage=None)

    async def mock_run_4(func, *args, **kwargs):
        return mock_response_4
    monkeypatch.setattr("agent_router.run_in_threadpool", mock_run_4)

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0003",
        raw_text="[OpAiTerra_Architect] next",
        is_bot_trigger=True,
    )

    # Architect finishes, state cleaned up, tags stripped
    assert thread_key not in REMAINING_AGENTS_MAP
    assert "[Gemini_Visionary]" not in posted_messages[3]
