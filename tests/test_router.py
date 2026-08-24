import pytest
from unittest.mock import AsyncMock, MagicMock
from agent_router import (
    parse_agent_and_prompt,
    process_and_respond,
    enforce_valid_nomination,
    get_initial_agents,
    REMAINING_AGENTS_MAP,
    RATE_LIMIT_RETRY_QUEUE,
    RATE_LIMIT_ALREADY_RETRIED,
    CUSTOM_FLOW_THREADS,
    STOP_REQUESTED_THREADS,
    LOCKED_AGENTS,
    ENABLED_DEBATE_AGENTS,
    reset_enabled_agents,
    load_enabled_agents,
    save_enabled_agents,
    set_agent_enabled,
    toggle_agent,
    get_enabled_agents,
    is_stop_command,
    request_thread_stop,
    is_thread_stopped,
    clear_thread_stop,
    classify_agent_error,
    is_discussion_active,
    interrupt_thread_discussion,
    ACTIVE_THREAD_TASKS,
    THREAD_GENERATIONS,
    SETTINGS_FILE,
)
from slack_client import build_app_home_view


import agent_router

@pytest.fixture(autouse=True)
def setup_test_environment(monkeypatch):
    reset_enabled_agents()
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


def test_is_stop_command():
    # Valid stop commands (/stop syntax)
    assert is_stop_command("<@U12345> /stop") is True
    assert is_stop_command("<@U12345> /STOP!") is True
    assert is_stop_command("<@U12345> /Stop.") is True
    assert is_stop_command("@CaiLogics Hub /stop") is True
    assert is_stop_command("@CaiLogics Test /stop") is True
    assert is_stop_command("@CaiLogics /stop") is True
    assert is_stop_command("@app_mention /stop") is True
    assert is_stop_command("<@U12345> @Gemini Visionary /stop") is True
    assert is_stop_command("[Gemini_Visionary] /stop") is True
    assert is_stop_command("<@U12345> /stop discussion") is True
    assert is_stop_command("<@U12345> /stop flow") is True
    assert is_stop_command("<@U12345> /stop please") is True
    assert is_stop_command("<@U12345> please /stop") is True
    assert is_stop_command("<@U12345> /stani") is True
    assert is_stop_command("<@U12345> /zaustavi") is True
    assert is_stop_command("<@U12345> /prekini") is True
    assert is_stop_command("/stop", is_mention_or_dm=True) is True

    # Negative cases (should NOT be treated as stop commands)
    assert is_stop_command("<@U12345> How do I stop a docker container?") is False
    assert is_stop_command("<@U12345> Where is the nearest bus stop?") is False
    assert is_stop_command("<@U12345> Can you stop by our office tomorrow?") is False
    assert is_stop_command("[Gemini_Visionary] Design a stop-motion animation pipeline") is False
    assert is_stop_command("<@U12345> stop") is False
    assert is_stop_command("") is False


@pytest.mark.anyio
async def test_thread_stop_during_llm_execution(monkeypatch):
    """
    Scenario: While an agent is in the middle of executing its LLM call,
    the user sends a stop command in that thread.
    Expected:
    1. The in-flight LLM call completes its execution.
    2. Any generated nomination tag is stripped (force_termination).
    3. State is cleaned up and no subsequent agent is tagged.
    """
    REMAINING_AGENTS_MAP.clear()
    CUSTOM_FLOW_THREADS.clear()
    STOP_REQUESTED_THREADS.clear()

    posted_messages = []
    async def mock_post_agent_response(channel_id, thread_ts, text, username, icon_url=None):
        posted_messages.append(text)
        return {"ok": True, "ts": "12345.0004"}

    monkeypatch.setattr("agent_router.post_agent_response", mock_post_agent_response)
    monkeypatch.setattr("agent_router.fetch_thread_history", AsyncMock(return_value=[]))

    thread_key = ("C123", "12345.0004")

    mock_choice = MagicMock()
    # Model generates response and tries to nominate next agent
    mock_choice.message.content = "Here is my architecture analysis. [AnthrOpus_Innovator]"
    mock_choice.message.tool_calls = None
    mock_response = MagicMock(choices=[mock_choice], usage=None)

    async def mock_run_with_stop_injected(func, *args, **kwargs):
        # Simulate user posting stop command while LLM call is running in background
        request_thread_stop("C123", "12345.0004")
        return mock_response

    monkeypatch.setattr("agent_router.run_in_threadpool", mock_run_with_stop_injected)

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0004",
        raw_text="<@U12345> Give me system designs",
        is_bot_trigger=False,
    )

    # 1. The in-flight message was posted
    assert len(posted_messages) == 1
    assert "Here is my architecture analysis." in posted_messages[0]

    # 2. Nomination tag [AnthrOpus_Innovator] was stripped
    assert "[AnthrOpus_Innovator]" not in posted_messages[0]
    assert "AnthrOpus_Innovator" in posted_messages[0]

    # 3. State cleaned up - no remaining agents, not in stop requested set
    assert thread_key not in REMAINING_AGENTS_MAP
    assert thread_key not in STOP_REQUESTED_THREADS
    assert thread_key not in CUSTOM_FLOW_THREADS


def test_locked_agents_cannot_be_disabled():
    reset_enabled_agents()
    # Gemini_Visionary, OpAiTerra_Architect, and AnthrOpus_Sceptic are locked
    assert set_agent_enabled("Gemini_Visionary", False) is False
    assert "Gemini_Visionary" in ENABLED_DEBATE_AGENTS

    assert set_agent_enabled("OpAiTerra_Architect", False) is False
    assert "OpAiTerra_Architect" in ENABLED_DEBATE_AGENTS

    assert set_agent_enabled("AnthrOpus_Sceptic", False) is False
    assert "AnthrOpus_Sceptic" in ENABLED_DEBATE_AGENTS


def test_toggleable_agent_enable_and_disable():
    reset_enabled_agents()
    assert "MistLar_TechLead" in ENABLED_DEBATE_AGENTS

    # Disable MistLar_TechLead
    assert set_agent_enabled("MistLar_TechLead", False) is True
    assert "MistLar_TechLead" not in ENABLED_DEBATE_AGENTS
    assert "MistLar_TechLead" not in get_initial_agents()
    # Sceptic must always be present
    assert "AnthrOpus_Sceptic" in get_initial_agents()

    # Re-enable via toggle_agent
    assert toggle_agent("MistLar_TechLead") is True
    assert "MistLar_TechLead" in ENABLED_DEBATE_AGENTS
    assert "MistLar_TechLead" in get_initial_agents()


def test_disabled_agent_can_still_be_explicitly_tagged():
    reset_enabled_agents()
    # Disable MistLar_TechLead for automatic debate
    set_agent_enabled("MistLar_TechLead", False)
    assert "MistLar_TechLead" not in get_initial_agents()

    # User explicitly tags the disabled agent
    agent_key, prompt, was_explicit = parse_agent_and_prompt(
        "[MistLar_TechLead] Direct question to disabled agent",
        "C_TEST",
        "123.001",
    )
    assert agent_key == "MistLar_TechLead"
    assert prompt == "Direct question to disabled agent"
    assert was_explicit is True

    # User explicitly mentions persona name
    agent_key_2, prompt_2, was_explicit_2 = parse_agent_and_prompt(
        "<@U123> @MistLar TechLead Direct question to disabled agent",
        "C_TEST",
        "123.002",
    )
    assert agent_key_2 == "MistLar_TechLead"
    assert was_explicit_2 is True


def test_build_app_home_view():
    reset_enabled_agents()
    view = build_app_home_view()
    assert view["type"] == "home"
    blocks = view["blocks"]
    assert len(blocks) > 10

    # Header block
    assert blocks[0]["type"] == "header"

    # Action IDs present
    action_ids = []
    for b in blocks:
        if "accessory" in b:
            action_ids.append(b["accessory"].get("action_id", ""))
        elif "elements" in b:
            for el in b["elements"]:
                action_ids.append(el.get("action_id", ""))

    assert "bulk_enable_all" in action_ids
    assert "bulk_disable_optional" in action_ids
    assert "locked_agent_Gemini_Visionary" in action_ids
    assert "locked_agent_AnthrOpus_Sceptic" in action_ids
    assert "locked_agent_OpAiTerra_Architect" in action_ids
    assert "toggle_agent_radio:MistLar_TechLead" in action_ids


def test_settings_persistence(tmp_path, monkeypatch):
    test_settings_path = tmp_path / "test_agent_settings.json"
    monkeypatch.setattr("agent_router.SETTINGS_FILE", test_settings_path)

    # 1. Reset and disable specific agents
    reset_enabled_agents()
    set_agent_enabled("MistLar_TechLead", False)
    set_agent_enabled("AnthrOpus_Innovator", False)

    assert test_settings_path.exists()
    assert "MistLar_TechLead" not in ENABLED_DEBATE_AGENTS
    assert "AnthrOpus_Innovator" not in ENABLED_DEBATE_AGENTS

    # 2. Simulate server restart by clearing memory and calling load_enabled_agents()
    ENABLED_DEBATE_AGENTS.clear()
    assert len(ENABLED_DEBATE_AGENTS) == 0

    loaded = load_enabled_agents()
    assert "MistLar_TechLead" not in loaded
    assert "AnthrOpus_Innovator" not in loaded
    # Verify locked agents are always present even on reload
    assert "Gemini_Visionary" in loaded
    assert "AnthrOpus_Sceptic" in loaded
    assert "OpAiTerra_Architect" in loaded
    assert len(loaded) == 14


def test_classify_agent_error():
    # Rate limit errors
    assert classify_agent_error(Exception("Rate limit reached for requests per minute (RPM)")) == "rate_limit"
    assert classify_agent_error(Exception("429 Too Many Requests")) == "rate_limit"
    assert classify_agent_error(Exception("ResourceExhausted quota exceeded")) == "rate_limit"

    # API key errors
    assert classify_agent_error(Exception("Authentication error: api_key not found")) == "api_key_not_found"
    assert classify_agent_error(Exception("No API key provided. Please set GEMINI_API_KEY")) == "api_key_not_found"
    assert classify_agent_error(Exception("Unauthorized: 401 Invalid Token")) == "api_key_not_found"

    # Model not found errors
    assert classify_agent_error(Exception("Model 'mistral/mistral-nonexistent' not found (404)")) == "model_not_found"
    assert classify_agent_error(Exception("The model `xyz` does not exist")) == "model_not_found"

    # General error
    assert classify_agent_error(Exception("Connection reset by peer")) == "general_error"


@pytest.mark.anyio
async def test_process_and_respond_api_key_not_found(monkeypatch):
    REMAINING_AGENTS_MAP.clear()
    RATE_LIMIT_RETRY_QUEUE.clear()
    RATE_LIMIT_ALREADY_RETRIED.clear()

    async def mock_run_in_threadpool(func, *args, **kwargs):
        raise Exception("AuthenticationError: Missing API key for Gemini")

    posted = []
    async def mock_post_agent_response(channel_id, thread_ts, text, username, icon_url=None):
        posted.append({"text": text, "username": username, "icon_url": icon_url})
        return {"ok": True, "ts": "12345.0001"}

    monkeypatch.setattr("agent_router.run_in_threadpool", mock_run_in_threadpool)
    monkeypatch.setattr("agent_router.post_agent_response", mock_post_agent_response)
    monkeypatch.setattr("agent_router.fetch_thread_history", AsyncMock(return_value=[]))

    res = await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0001",
        raw_text="Hello, help me design a backend architecture",
    )

    assert len(posted) == 1
    # Verify posted as responsible agent persona
    assert posted[0]["username"] == "Gemini Visionary"
    # Verify single sentence statement
    assert "API KEY NOT FOUND" in posted[0]["text"]
    # Verify next nomination exists so discussion flows
    assert "[" in posted[0]["text"]


@pytest.mark.anyio
async def test_process_and_respond_model_not_found(monkeypatch):
    REMAINING_AGENTS_MAP.clear()
    RATE_LIMIT_RETRY_QUEUE.clear()
    RATE_LIMIT_ALREADY_RETRIED.clear()

    thread_key = ("C123", "12345.0002")
    REMAINING_AGENTS_MAP[thread_key] = ["AnthrOpus_Innovator"]

    async def mock_run_in_threadpool(func, *args, **kwargs):
        raise Exception("NotFoundError: Model gemini/gemini-3.7-flash does not exist")

    posted = []
    async def mock_post_agent_response(channel_id, thread_ts, text, username, icon_url=None):
        posted.append({"text": text, "username": username, "icon_url": icon_url})
        return {"ok": True, "ts": "12345.0002"}

    monkeypatch.setattr("agent_router.run_in_threadpool", mock_run_in_threadpool)
    monkeypatch.setattr("agent_router.post_agent_response", mock_post_agent_response)
    monkeypatch.setattr("agent_router.fetch_thread_history", AsyncMock(return_value=[]))

    res = await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0002",
        raw_text="[MistLar_TechLead] Analyze this code",
        is_bot_trigger=True,
    )

    assert len(posted) == 1
    assert posted[0]["username"] == "MistLar TechLead"
    assert "MODEL NOT FOUND" in posted[0]["text"]
    assert "[AnthrOpus_Innovator]" in posted[0]["text"]


@pytest.mark.anyio
async def test_process_and_respond_rate_limits_and_retry_flow(monkeypatch):
    REMAINING_AGENTS_MAP.clear()
    RATE_LIMIT_RETRY_QUEUE.clear()
    RATE_LIMIT_ALREADY_RETRIED.clear()

    thread_key = ("C123", "12345.0003")

    # Set up specific short flow: MistLar_TechLead -> AnthrOpus_Innovator -> (retry MistLar_TechLead) -> OpAiTerra_Architect
    REMAINING_AGENTS_MAP[thread_key] = ["AnthrOpus_Innovator"]

    posted = []
    async def mock_post_agent_response(channel_id, thread_ts, text, username, icon_url=None):
        posted.append({"text": text, "username": username, "icon_url": icon_url})
        return {"ok": True, "ts": "12345.0003"}

    # 1. First run of MistLar_TechLead encounters 429 RateLimitError
    async def mock_fail_rate_limit(func, *args, **kwargs):
        raise Exception("RateLimitError: 429 Too Many Requests, quota exceeded")

    monkeypatch.setattr("agent_router.run_in_threadpool", mock_fail_rate_limit)
    monkeypatch.setattr("agent_router.post_agent_response", mock_post_agent_response)
    monkeypatch.setattr("agent_router.fetch_thread_history", AsyncMock(return_value=[]))

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0003",
        raw_text="[MistLar_TechLead] Database architecture review",
        is_bot_trigger=True,
    )

    assert len(posted) == 1
    assert posted[0]["username"] == "MistLar TechLead"
    assert "RATE LIMITS CROSSED, will retry at the end of discussion" in posted[0]["text"]
    assert "[AnthrOpus_Innovator]" in posted[0]["text"]
    assert thread_key in RATE_LIMIT_RETRY_QUEUE
    assert "MistLar_TechLead" in RATE_LIMIT_RETRY_QUEUE[thread_key]

    # 2. AnthrOpus_Innovator runs normally, remaining is empty -> nominates retry for MistLar_TechLead!
    mock_choice = MagicMock()
    mock_choice.message.content = "My innovator perspective."
    mock_choice.message.tool_calls = None
    mock_resp = MagicMock(choices=[mock_choice], usage=None)

    async def mock_success_innovator(func, *args, **kwargs):
        return mock_resp

    monkeypatch.setattr("agent_router.run_in_threadpool", mock_success_innovator)

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0003",
        raw_text="[AnthrOpus_Innovator] Your turn",
        is_bot_trigger=True,
    )

    assert len(posted) == 2
    assert posted[1]["username"] == "AnthrOpus Innovator"
    assert "[MistLar_TechLead]" in posted[1]["text"]
    # Verify MistLar_TechLead is now marked as already retried once
    assert "MistLar_TechLead" in RATE_LIMIT_ALREADY_RETRIED[thread_key]

    # 3. MistLar_TechLead retries at the end of discussion, fails again with RateLimit:
    # Must post 'RATE LIMITS CROSSED' and NOT be retried again.
    monkeypatch.setattr("agent_router.run_in_threadpool", mock_fail_rate_limit)

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0003",
        raw_text="[MistLar_TechLead] Retry turn",
        is_bot_trigger=True,
    )

    assert len(posted) == 3
    assert posted[2]["username"] == "MistLar TechLead"
    # Should say RATE LIMITS CROSSED without 'will retry'
    assert "RATE LIMITS CROSSED" in posted[2]["text"]
    assert "will retry" not in posted[2]["text"]
    # Should NOT be added back to RATE_LIMIT_RETRY_QUEUE
    assert "MistLar_TechLead" not in RATE_LIMIT_RETRY_QUEUE.get(thread_key, [])
    # Should nominate OpAiTerra_Architect for the final summary
    assert "[OpAiTerra_Architect]" in posted[2]["text"]


def test_is_discussion_active_behavior():
    thread_key = ("C_ACTIVE_TEST", "12345.1001")
    REMAINING_AGENTS_MAP.pop(thread_key, None)
    RATE_LIMIT_RETRY_QUEUE.pop(thread_key, None)
    ACTIVE_THREAD_TASKS.pop(thread_key, None)

    # 1. Fresh / idle thread is not active
    assert is_discussion_active("C_ACTIVE_TEST", "12345.1001") is False

    # 2. Active when remaining agents exist
    REMAINING_AGENTS_MAP[thread_key] = ["MistLar_TechLead"]
    assert is_discussion_active("C_ACTIVE_TEST", "12345.1001") is True

    # 3. Active when retry queue has entries
    REMAINING_AGENTS_MAP.pop(thread_key, None)
    RATE_LIMIT_RETRY_QUEUE[thread_key] = ["AnthrOpus_Innovator"]
    assert is_discussion_active("C_ACTIVE_TEST", "12345.1001") is True

    # Clean up
    RATE_LIMIT_RETRY_QUEUE.pop(thread_key, None)
    assert is_discussion_active("C_ACTIVE_TEST", "12345.1001") is False


@pytest.mark.anyio
async def test_interrupt_thread_discussion_clears_state_and_cancels_task():
    import asyncio
    thread_key = ("C_INTERRUPT_TEST", "12345.1002")

    REMAINING_AGENTS_MAP[thread_key] = ["MistLar_TechLead", "AnthrOpus_Innovator"]
    RATE_LIMIT_RETRY_QUEUE[thread_key] = ["Gemini_Visionary"]
    RATE_LIMIT_ALREADY_RETRIED[thread_key] = {"MistLar_TechLead"}
    CUSTOM_FLOW_THREADS.add(thread_key)
    STOP_REQUESTED_THREADS.add(thread_key)

    # Create dummy running background task
    async def dummy_coro():
        await asyncio.sleep(100)

    task = asyncio.create_task(dummy_coro())
    ACTIVE_THREAD_TASKS[thread_key] = task

    initial_gen = THREAD_GENERATIONS.get(thread_key, 0)

    # Trigger interrupt
    interrupt_thread_discussion("C_INTERRUPT_TEST", "12345.1002")

    # Verify task cancelled, generation incremented, state maps wiped
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert task.cancelled() is True
    assert THREAD_GENERATIONS[thread_key] == initial_gen + 1
    assert thread_key not in REMAINING_AGENTS_MAP
    assert thread_key not in RATE_LIMIT_RETRY_QUEUE
    assert thread_key not in RATE_LIMIT_ALREADY_RETRIED
    assert thread_key not in CUSTOM_FLOW_THREADS
    assert thread_key not in STOP_REQUESTED_THREADS
    assert is_discussion_active("C_INTERRUPT_TEST", "12345.1002") is False


@pytest.mark.anyio
async def test_interruption_during_llm_execution_aborts_superseded_turn(monkeypatch):
    """
    Scenario: Agent 1 of Turn 1 is in the middle of litellm call.
    User submits a new prompt in that thread.
    interrupt_thread_discussion is called.
    Expected:
    - Turn 1 LLM response is discarded and Turn 1 DOES NOT post to Slack.
    - Turn 2 (new query) processes and posts to Slack.
    """
    thread_key = ("C_INTERRUPT_LLM", "12345.1003")
    REMAINING_AGENTS_MAP.pop(thread_key, None)
    ACTIVE_THREAD_TASKS.pop(thread_key, None)

    posted_messages = []
    async def mock_post_agent_response(channel_id, thread_ts, text, username, icon_url=None):
        posted_messages.append({"text": text, "username": username})
        return {"ok": True, "ts": "12345.1003"}

    monkeypatch.setattr("agent_router.post_agent_response", mock_post_agent_response)
    monkeypatch.setattr("agent_router.fetch_thread_history", AsyncMock(return_value=[]))

    mock_choice_1 = MagicMock()
    mock_choice_1.message.content = "Turn 1 answer to old query. [MistLar_TechLead]"
    mock_choice_1.message.tool_calls = None
    mock_resp_1 = MagicMock(choices=[mock_choice_1], usage=None)

    async def mock_run_turn_1(func, *args, **kwargs):
        # Simulate user interruption arriving while Turn 1 is waiting on LLM
        interrupt_thread_discussion("C_INTERRUPT_LLM", "12345.1003")
        return mock_resp_1

    monkeypatch.setattr("agent_router.run_in_threadpool", mock_run_turn_1)

    # Run Turn 1
    res1 = await process_and_respond(
        channel_id="C_INTERRUPT_LLM",
        thread_ts="12345.1003",
        raw_text="<@U12345> Old query topic 1",
        is_bot_trigger=False,
    )

    # Turn 1 should be superseded and post NO message to Slack
    assert res1.get("cancelled") is True
    assert len(posted_messages) == 0

    # Now run Turn 2 (the new user query)
    mock_choice_2 = MagicMock()
    mock_choice_2.message.content = "Turn 2 answer to new query. [MistLar_TechLead]"
    mock_choice_2.message.tool_calls = None
    mock_resp_2 = MagicMock(choices=[mock_choice_2], usage=None)

    async def mock_run_turn_2(func, *args, **kwargs):
        return mock_resp_2

    monkeypatch.setattr("agent_router.run_in_threadpool", mock_run_turn_2)

    res2 = await process_and_respond(
        channel_id="C_INTERRUPT_LLM",
        thread_ts="12345.1003",
        raw_text="<@U12345> New query topic 2",
        is_bot_trigger=False,
    )

    # Turn 2 successfully posts
    assert len(posted_messages) == 1
    assert "Turn 2 answer to new query." in posted_messages[0]["text"]
    assert "[MistLar_TechLead]" in posted_messages[0]["text"]


@pytest.mark.anyio
async def test_interruption_during_pacing_delay_aborts_superseded_turn(monkeypatch):
    """
    Scenario: Agent 1 completes LLM and is during the 3-second pacing delay.
    User submits a new prompt in that thread.
    Expected:
    - Turn 1 does not post to Slack.
    """
    thread_key = ("C_INTERRUPT_SLEEP", "12345.1004")
    REMAINING_AGENTS_MAP.pop(thread_key, None)
    ACTIVE_THREAD_TASKS.pop(thread_key, None)

    posted_messages = []
    async def mock_post_agent_response(channel_id, thread_ts, text, username, icon_url=None):
        posted_messages.append({"text": text, "username": username})
        return {"ok": True, "ts": "12345.1004"}

    monkeypatch.setattr("agent_router.post_agent_response", mock_post_agent_response)
    monkeypatch.setattr("agent_router.fetch_thread_history", AsyncMock(return_value=[]))

    mock_choice = MagicMock()
    mock_choice.message.content = "Turn 1 answer before sleep. [MistLar_TechLead]"
    mock_choice.message.tool_calls = None
    mock_resp = MagicMock(choices=[mock_choice], usage=None)

    async def mock_run(func, *args, **kwargs):
        return mock_resp

    monkeypatch.setattr("agent_router.run_in_threadpool", mock_run)

    # Interrupt injected during asyncio.sleep in router
    async def mock_sleep_with_interrupt(duration):
        interrupt_thread_discussion("C_INTERRUPT_SLEEP", "12345.1004")

    monkeypatch.setattr("agent_router.asyncio.sleep", mock_sleep_with_interrupt)

    res = await process_and_respond(
        channel_id="C_INTERRUPT_SLEEP",
        thread_ts="12345.1004",
        raw_text="<@U12345> Query that gets interrupted during delay",
        is_bot_trigger=False,
    )

    # Turn 1 should be cancelled/superseded and not post
    assert res.get("cancelled") is True
    assert len(posted_messages) == 0





