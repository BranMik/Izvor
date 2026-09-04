import pytest
from unittest.mock import AsyncMock, MagicMock
from core.config import AGENTS_CONFIG
from orchestration.agent_router import (
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
    request_thread_stop,
    is_thread_stopped,
    clear_thread_stop,
    classify_agent_error,
    is_discussion_active,
    interrupt_thread_discussion,
    ACTIVE_THREAD_TASKS,
    THREAD_GENERATIONS,
    SETTINGS_FILE,
    get_thread_history_limit,
    set_thread_history_limit,
    clear_thread_history_limit,
    THREAD_HISTORY_LIMITS,
    is_circuit_breaker_enabled,
    set_circuit_breaker_enabled,
    get_circuit_breaker_threshold,
    set_circuit_breaker_threshold,
    get_circuit_breaker_config,
    HIGH_DEMAND_ERRORED_AGENTS,
    is_high_demand_503_error,
    execute_llm_with_retry,
    set_orchestration_mode,
    set_sandwich_config,
    set_fixed_config,
    get_orchestration_mode,
)
from nlp.message_builder import build_llm_messages
from nlp.nl_patterns import extract_history_limit, is_stop_command
from services.slack_client import build_app_home_view


import orchestration.agent_router as agent_router


@pytest.fixture(autouse=True)
def setup_test_environment(monkeypatch):
    reset_enabled_agents()
    set_orchestration_mode("sandwich")
    set_sandwich_config([], [])
    set_fixed_config([])
    REMAINING_AGENTS_MAP.clear()
    RATE_LIMIT_RETRY_QUEUE.clear()
    RATE_LIMIT_ALREADY_RETRIED.clear()
    CUSTOM_FLOW_THREADS.clear()
    STOP_REQUESTED_THREADS.clear()
    ACTIVE_THREAD_TASKS.clear()
    HIGH_DEMAND_ERRORED_AGENTS.clear()
    monkeypatch.setattr(agent_router.asyncio, "sleep", AsyncMock(return_value=None))


def test_parse_explicit_bracket_tags():
    agent_key, prompt, was_explicit = parse_agent_and_prompt(
        "<@U12345> [Visionary] Create a new UI flow", "C12345", "123456789.0001"
    )
    assert agent_key == "Visionary"
    assert prompt == "Create a new UI flow"
    assert was_explicit is True


def test_parse_mistral_techlead_tag():
    agent_key, prompt, was_explicit = parse_agent_and_prompt(
        "[TechLead] Check this database query", "C12345", "123456789.0001"
    )
    assert agent_key == "TechLead"
    assert prompt == "Check this database query"
    assert was_explicit is True


def test_parse_multiple_tags_first_invalid():
    agent_key, prompt, was_explicit = parse_agent_and_prompt(
        "[InvalidTag] [TechLead] Check this database query", "C12345", "123456789.0001"
    )
    assert agent_key == "TechLead"
    assert prompt == "[InvalidTag]  Check this database query"
    assert was_explicit is True


def test_parse_at_mention_persona():
    # Non-bracketed name mention is no longer an explicit agent tag; falls back to dynamic enabled agent
    agent_key, prompt, was_explicit = parse_agent_and_prompt(
        "<@U12345> @Gemini Visionary generate a concept",
        "C12345",
        "123456789.0001",
    )
    assert agent_key in get_enabled_agents()
    assert prompt == "@Gemini Visionary generate a concept"
    assert was_explicit is False

    # Bracketed tag is the only recognized explicit agent tag
    agent_key_tagged, prompt_tagged, was_explicit_tagged = parse_agent_and_prompt(
        "<@U12345> [Visionary] generate a concept",
        "C12345",
        "123456789.0002",
    )
    assert agent_key_tagged == "Visionary"
    assert prompt_tagged == "generate a concept"
    assert was_explicit_tagged is True


def test_parse_fallback_default_agent():
    agent_key, prompt, was_explicit = parse_agent_and_prompt(
        "<@U12345> Tell me how to structure this application",
        "C12345",
        "123456789.0001",
    )
    assert agent_key in get_enabled_agents()
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

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted_messages.append(text)
        return {"ok": True, "ts": "12345.6789"}

    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_run_in_threadpool
    )
    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent_response
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.6789",
        raw_text="Hello [Visionary]",
    )

    assert len(posted_messages) == 1
    assert posted_messages[0].endswith("\n\n{gemini/gemini-3.8-flash : 150/50}")


@pytest.mark.anyio
async def test_process_and_respond_model_token_footer_openai(monkeypatch):
    """
    Verifies that an agent using Architect ends with {openai/gpt-5.6-terra : 721/64}.
    """
    mock_usage = MagicMock()
    mock_usage.prompt_tokens = 721
    mock_usage.completion_tokens = 64

    mock_choice = MagicMock()
    mock_choice.message.content = "Analysis completed."
    mock_choice.message.tool_calls = None

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.usage = mock_usage

    async def mock_run_in_threadpool(func, *args, **kwargs):
        return mock_response

    posted_messages = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted_messages.append(text)
        return {"ok": True, "ts": "12345.9999"}

    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_run_in_threadpool
    )
    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent_response
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.9999",
        raw_text="Hello [Architect]",
    )

    assert len(posted_messages) == 1
    assert posted_messages[0].endswith("\n\n{openai/gpt-5.6-terra : 721/64}")


@pytest.mark.anyio
async def test_model_token_footer_ignored_by_tag_detection():
    """
    Verifies that {openai/gpt-5.6-terra : 721/64} is not flagged as unrecognized or ambiguous agent tag.
    """
    from pipeline.entity_resolver import entity_resolver

    sample_text = "Here is our response.\n\n{openai/gpt-5.6-terra : 721/64}"
    unrecognized = entity_resolver.detect_unrecognized_tags(sample_text)
    assert unrecognized == []

    ambiguous = entity_resolver.detect_ambiguous_tags(sample_text)
    assert ambiguous == []


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
    mock_choice.message.content = "My single vision response. [Sceptic]"
    mock_choice.message.tool_calls = None

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.usage = None

    async def mock_run_in_threadpool(func, *args, **kwargs):
        captured_messages = kwargs.get("messages") or args[2]
        mock_run_in_threadpool.captured_user_prompt = captured_messages[-1]["content"]
        return mock_response

    posted_messages = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted_messages.append(text)
        return {"ok": True, "ts": "12345.0001"}

    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_run_in_threadpool
    )
    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent_response
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )

    thread_key = ("C123", "12345.0001")

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0001",
        raw_text="[Visionary] Explain this concept only",
        is_bot_trigger=False,
    )

    # Verify:
    # 1. User prompt forced termination
    assert "CRITICAL CONSTRAINT" in mock_run_in_threadpool.captured_user_prompt
    # 2. State cleaned up
    assert thread_key not in CUSTOM_FLOW_THREADS
    assert thread_key not in REMAINING_AGENTS_MAP
    # 3. Posted message has stripped bracket tags (so no next bot is triggered)
    assert len(posted_messages) == 1
    assert "[Sceptic]" not in posted_messages[0]
    assert "Sceptic" in posted_messages[0]


@pytest.mark.anyio
async def test_scenario_2_multiple_agents_custom_flow(monkeypatch):
    """
    Scenario: User tags multiple specific agents.
    Expected: Only those agents speak in order, intermediate choices are validated, and flow ends after the last one.
    """
    REMAINING_AGENTS_MAP.clear()
    CUSTOM_FLOW_THREADS.clear()

    posted_messages = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted_messages.append(text)
        return {"ok": True, "ts": f"12345.{len(posted_messages)}"}

    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent_response
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )

    thread_key = ("C123", "12345.0002")

    # Step 1: User requests 2 specific agents: [Innovator] and [TechLead]
    mock_choice_1 = MagicMock()
    # Innovator attempts to nominate Architect prematurely (Architect was not in user's list)
    mock_choice_1.message.content = "Innovative idea. [Architect]"
    mock_choice_1.message.tool_calls = None
    mock_response_1 = MagicMock(choices=[mock_choice_1], usage=None)

    async def mock_run_1(func, *args, **kwargs):
        return mock_response_1

    monkeypatch.setattr("orchestration.agent_router.run_in_threadpool", mock_run_1)

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0002",
        raw_text="[Innovator] [TechLead] Debate this feature",
        is_bot_trigger=False,
    )

    # Innovator's premature Architect nomination should be corrected to TechLead
    assert thread_key in CUSTOM_FLOW_THREADS
    assert REMAINING_AGENTS_MAP[thread_key] == ["TechLead"]
    assert "[TechLead]" in posted_messages[0]

    # Step 2: Next bot trigger for TechLead (the last custom agent)
    mock_choice_2 = MagicMock()
    mock_choice_2.message.content = "Tech lead evaluation and conclusion. [Architect]"
    mock_choice_2.message.tool_calls = None
    mock_response_2 = MagicMock(choices=[mock_choice_2], usage=None)

    async def mock_run_2(func, *args, **kwargs):
        captured_messages = kwargs.get("messages") or args[2]
        mock_run_2.captured_user_prompt = captured_messages[-1]["content"]
        return mock_response_2

    monkeypatch.setattr("orchestration.agent_router.run_in_threadpool", mock_run_2)

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0002",
        raw_text="[TechLead] Debate this feature",
        is_bot_trigger=True,
    )

    # Last custom agent cleanly terminates
    assert "CRITICAL CONSTRAINT" in mock_run_2.captured_user_prompt
    assert thread_key not in CUSTOM_FLOW_THREADS
    assert thread_key not in REMAINING_AGENTS_MAP
    assert "[Architect]" not in posted_messages[1]


@pytest.mark.anyio
async def test_scenario_3_no_agent_tagged_default_flow(monkeypatch):
    """
    Scenario: User tags NO agent (plain message / app_mention).
    Expected: Default flow starts with Visionary, initializes all 15 panel agents,
    programmatically prevents premature Architect nomination, and finishes with Architect.
    """
    REMAINING_AGENTS_MAP.clear()
    CUSTOM_FLOW_THREADS.clear()

    posted_messages = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted_messages.append(text)
        return {"ok": True, "ts": f"12345.{len(posted_messages)}"}

    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent_response
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )

    thread_key = ("C123", "12345.0003")

    # Step 1: Plain user prompt (no tags)
    mock_choice_1 = MagicMock()
    mock_choice_1.message.content = "Vision for the product. [Sceptic]"
    mock_choice_1.message.tool_calls = None
    mock_response_1 = MagicMock(choices=[mock_choice_1], usage=None)

    async def mock_run_1(func, *args, **kwargs):
        return mock_response_1

    monkeypatch.setattr("orchestration.agent_router.run_in_threadpool", mock_run_1)

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0003",
        raw_text="<@U12345> Give me commercial ideas for Benford Law",
        is_bot_trigger=False,
    )

    # Default starts with Visionary and initializes all panel agents
    assert thread_key not in CUSTOM_FLOW_THREADS
    assert len(REMAINING_AGENTS_MAP[thread_key]) == 16
    assert "[Sceptic]" in posted_messages[0]

    # Step 2: Intermediate agent (Monitoring Specialist) tries to prematurely nominate Architect
    REMAINING_AGENTS_MAP[thread_key] = [
        "UserAdvocate",
        "SecurityArchitect",
        "MonitoringAndAlertingSpecialist",
    ]

    mock_choice_2 = MagicMock()
    mock_choice_2.message.content = "Monitoring review. [Architect]"
    mock_choice_2.message.tool_calls = None
    mock_response_2 = MagicMock(choices=[mock_choice_2], usage=None)

    async def mock_run_2(func, *args, **kwargs):
        return mock_response_2

    monkeypatch.setattr("orchestration.agent_router.run_in_threadpool", mock_run_2)

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0003",
        raw_text="[MonitoringAndAlertingSpecialist] next",
        is_bot_trigger=True,
    )

    # Premature Architect tag is programmatically replaced with first remaining agent (UserAdvocate)
    assert "[UserAdvocate]" in posted_messages[1]
    assert "UserAdvocate" in REMAINING_AGENTS_MAP[thread_key]

    # Step 3: Intermediate agent speaks and nominates next remaining agent
    REMAINING_AGENTS_MAP[thread_key] = ["SecurityArchitect", "Architect"]

    mock_choice_3 = MagicMock()
    mock_choice_3.message.content = "Security review finished."
    mock_choice_3.message.tool_calls = None
    mock_response_3 = MagicMock(choices=[mock_choice_3], usage=None)

    async def mock_run_3(func, *args, **kwargs):
        return mock_response_3

    monkeypatch.setattr("orchestration.agent_router.run_in_threadpool", mock_run_3)

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0003",
        raw_text="[SecurityArchitect] next",
        is_bot_trigger=True,
    )

    # Next agent is Architect
    assert REMAINING_AGENTS_MAP[thread_key] == ["Architect"]
    assert "[Architect]" in posted_messages[2]

    # Step 4: Architect executes final summary
    mock_choice_4 = MagicMock()
    mock_choice_4.message.content = "Final architecture summary. [Visionary]"
    mock_choice_4.message.tool_calls = None
    mock_response_4 = MagicMock(choices=[mock_choice_4], usage=None)

    async def mock_run_4(func, *args, **kwargs):
        return mock_response_4

    monkeypatch.setattr("orchestration.agent_router.run_in_threadpool", mock_run_4)

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0003",
        raw_text="[Architect] next",
        is_bot_trigger=True,
    )

    # Architect finishes, state cleaned up, tags stripped
    assert thread_key not in REMAINING_AGENTS_MAP
    assert "[Visionary]" not in posted_messages[3]


def test_is_stop_command():
    # Valid stop commands (/stop syntax anywhere with whitespace or start/end boundaries)
    assert is_stop_command("<@U12345> /stop") is True
    assert is_stop_command("<@U12345> /STOP!") is True
    assert is_stop_command("<@U12345> /Stop.") is True
    assert is_stop_command("@CaiLogics Hub /stop") is True
    assert is_stop_command("@CaiLogics Test /stop") is True
    assert is_stop_command("@CaiLogics /stop") is True
    assert is_stop_command("@app_mention /stop") is True
    assert is_stop_command("[Visionary] /stop") is True
    assert is_stop_command("/stop", is_mention_or_dm=True) is True
    assert is_stop_command("anything /stop bla") is True
    assert is_stop_command("/stop bla bla") is True
    assert is_stop_command("bla bla /stop") is True
    assert is_stop_command("<@U12345> /stop discussion") is True
    assert is_stop_command("<@U12345> /stop flow") is True
    assert is_stop_command("<@U12345> /stop please") is True
    assert is_stop_command("<@U12345> please /stop") is True
    assert is_stop_command("<@U12345> @Gemini Visionary /stop") is True

    # Negative cases (should NOT be treated as stop commands)
    assert is_stop_command("<@U12345> /stani") is False
    assert is_stop_command("<@U12345> /zaustavi") is False
    assert is_stop_command("<@U12345> /prekini") is False
    assert is_stop_command("http://example.com/stop") is False
    assert is_stop_command("/stopwatch") is False
    assert is_stop_command("<@U12345> How do I stop a docker container?") is False
    assert is_stop_command("<@U12345> Where is the nearest bus stop?") is False
    assert is_stop_command("<@U12345> Can you stop by our office tomorrow?") is False
    assert (
        is_stop_command("[Visionary] Design a stop-motion animation pipeline") is False
    )
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

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted_messages.append(text)
        return {"ok": True, "ts": "12345.0004"}

    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent_response
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )

    thread_key = ("C123", "12345.0004")

    mock_choice = MagicMock()
    # Model generates response and tries to nominate next agent
    mock_choice.message.content = "Here is my architecture analysis. [Innovator]"
    mock_choice.message.tool_calls = None
    mock_response = MagicMock(choices=[mock_choice], usage=None)

    async def mock_run_with_stop_injected(func, *args, **kwargs):
        # Simulate user posting stop command while LLM call is running in background
        request_thread_stop("C123", "12345.0004")
        return mock_response

    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_run_with_stop_injected
    )

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0004",
        raw_text="<@U12345> Give me system designs",
        is_bot_trigger=False,
    )

    # 1. The in-flight message was posted
    assert len(posted_messages) == 1
    assert "Here is my architecture analysis." in posted_messages[0]

    # 2. Nomination tag [Innovator] was stripped
    assert "[Innovator]" not in posted_messages[0]
    assert "Innovator" in posted_messages[0]

    # 3. State cleaned up - no remaining agents, not in stop requested set
    assert thread_key not in REMAINING_AGENTS_MAP
    assert thread_key not in STOP_REQUESTED_THREADS
    assert thread_key not in CUSTOM_FLOW_THREADS


def test_locked_agents_cannot_be_disabled():
    reset_enabled_agents()
    # All agents can be freely toggled on and off (no locked agents)
    assert set_agent_enabled("Visionary", False) is True
    assert "Visionary" not in ENABLED_DEBATE_AGENTS

    assert set_agent_enabled("Architect", False) is True
    assert "Architect" not in ENABLED_DEBATE_AGENTS

    assert set_agent_enabled("Sceptic", False) is True
    assert "Sceptic" not in ENABLED_DEBATE_AGENTS


def test_toggleable_agent_enable_and_disable():
    reset_enabled_agents()
    assert "TechLead" in ENABLED_DEBATE_AGENTS

    # Disable TechLead
    assert set_agent_enabled("TechLead", False) is True
    assert "TechLead" not in ENABLED_DEBATE_AGENTS
    assert "TechLead" not in get_initial_agents()

    # Re-enable via toggle_agent
    assert toggle_agent("TechLead") is True
    assert "TechLead" in ENABLED_DEBATE_AGENTS
    assert "TechLead" in get_initial_agents()


def test_disabled_agent_can_still_be_explicitly_tagged():
    reset_enabled_agents()
    # Disable TechLead for automatic debate
    set_agent_enabled("TechLead", False)
    assert "TechLead" not in get_initial_agents()

    # User explicitly tags the disabled agent
    agent_key, prompt, was_explicit = parse_agent_and_prompt(
        "[TechLead] Direct question to disabled agent",
        "C_TEST",
        "123.001",
    )
    assert agent_key == "TechLead"
    assert prompt == "Direct question to disabled agent"
    assert was_explicit is True

    # User mentions persona name without brackets -> does NOT tag disabled agent, falls back to default agent
    agent_key_2, prompt_2, was_explicit_2 = parse_agent_and_prompt(
        "<@U123> @MistLar TechLead Direct question to disabled agent",
        "C_TEST",
        "123.002",
    )
    assert agent_key_2 in get_enabled_agents()
    assert was_explicit_2 is False


def test_build_app_home_view():
    reset_enabled_agents()
    set_circuit_breaker_enabled(True)
    set_circuit_breaker_threshold(2.50)
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
        elif "element" in b and isinstance(b["element"], dict):
            action_ids.append(b["element"].get("action_id", ""))

    assert "bulk_enable_all" in action_ids
    assert "bulk_disable_all" in action_ids
    assert "select_orchestration_mode" in action_ids
    assert "set_start_chain_input" in action_ids
    assert "set_end_chain_input" in action_ids
    assert "save_sandwich_chains_button" in action_ids
    assert "toggle_agent_radio:TechLead" in action_ids
    assert "toggle_circuit_breaker_radio" in action_ids
    assert "set_circuit_breaker_threshold_input" in action_ids
    assert "save_circuit_breaker_threshold_button" in action_ids
    assert "set_circuit_breaker_preset:0.50" in action_ids

    # Dynamic block IDs present
    block_ids = [b.get("block_id") for b in blocks if "block_id" in b]
    assert "cb_threshold_block_2.50" in block_ids
    assert "sandwich_start_chain_block" in block_ids
    assert "sandwich_end_chain_block" in block_ids
    assert "agent_toggle_block_TechLead_on" in block_ids

    # Agent ID in square brackets rendered
    assert any("[TechLead]" in b.get("text", {}).get("text", "") for b in blocks)


def test_settings_persistence(tmp_path, monkeypatch):
    test_settings_path = tmp_path / "test_agent_settings.json"
    monkeypatch.setattr("orchestration.agent_router.SETTINGS_FILE", test_settings_path)

    # 1. Reset and disable specific agents & set circuit-breaker config
    reset_enabled_agents()
    set_agent_enabled("TechLead", False)
    set_agent_enabled("Innovator", False)
    set_circuit_breaker_enabled(True)
    set_circuit_breaker_threshold(3.50)

    assert test_settings_path.exists()
    assert "TechLead" not in ENABLED_DEBATE_AGENTS
    assert "Innovator" not in ENABLED_DEBATE_AGENTS
    assert is_circuit_breaker_enabled() is True
    assert get_circuit_breaker_threshold() == 3.50

    # 2. Simulate server restart by clearing memory and calling load_enabled_agents()
    ENABLED_DEBATE_AGENTS.clear()
    assert len(ENABLED_DEBATE_AGENTS) == 0

    loaded = load_enabled_agents()
    assert "TechLead" not in loaded
    assert "Innovator" not in loaded
    # Verify locked agents are always present even on reload
    assert "Visionary" in loaded
    assert "Sceptic" in loaded
    assert "Architect" in loaded
    assert len(loaded) == 15
    assert is_circuit_breaker_enabled() is True
    assert get_circuit_breaker_threshold() == 3.50


def test_classify_agent_error():
    # Rate limit errors
    assert (
        classify_agent_error(
            Exception("Rate limit reached for requests per minute (RPM)")
        )
        == "rate_limit"
    )
    assert classify_agent_error(Exception("429 Too Many Requests")) == "rate_limit"
    assert (
        classify_agent_error(Exception("ResourceExhausted quota exceeded"))
        == "rate_limit"
    )

    # Service unavailable / 503 / High demand spike errors
    assert (
        classify_agent_error(Exception("503 Service Unavailable"))
        == "service_unavailable"
    )
    assert classify_agent_error(Exception("529 Overloaded")) == "service_unavailable"
    assert (
        classify_agent_error(Exception("The model is overloaded due to high demand"))
        == "service_unavailable"
    )
    assert (
        classify_agent_error(
            Exception("ServiceUnavailableError: upstream connect error or disconnect")
        )
        == "service_unavailable"
    )
    assert (
        classify_agent_error(
            Exception("Server temporarily overloaded (high demand spike)")
        )
        == "service_unavailable"
    )
    assert is_high_demand_503_error(Exception("503 Service Unavailable")) is True
    assert is_high_demand_503_error(Exception("429 Too Many Requests")) is False

    # API key errors
    assert (
        classify_agent_error(Exception("Authentication error: api_key not found"))
        == "api_key_not_found"
    )
    assert (
        classify_agent_error(
            Exception("No API key provided. Please set GEMINI_API_KEY")
        )
        == "api_key_not_found"
    )
    assert (
        classify_agent_error(Exception("Unauthorized: 401 Invalid Token"))
        == "api_key_not_found"
    )

    # Model not found errors
    assert (
        classify_agent_error(
            Exception("Model 'mistral/mistral-nonexistent' not found (404)")
        )
        == "model_not_found"
    )
    assert (
        classify_agent_error(Exception("The model `xyz` does not exist"))
        == "model_not_found"
    )

    # General error
    assert (
        classify_agent_error(Exception("Connection reset by peer")) == "general_error"
    )


@pytest.mark.anyio
async def test_process_and_respond_api_key_not_found(monkeypatch):
    REMAINING_AGENTS_MAP.clear()
    RATE_LIMIT_RETRY_QUEUE.clear()
    RATE_LIMIT_ALREADY_RETRIED.clear()

    async def mock_run_in_threadpool(func, *args, **kwargs):
        raise Exception("AuthenticationError: Missing API key for Gemini")

    posted = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted.append({"text": text, "username": username, "icon_url": icon_url})
        return {"ok": True, "ts": "12345.0001"}

    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_run_in_threadpool
    )
    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent_response
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )

    res = await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0001",
        raw_text="Hello, help me design a backend architecture",
    )

    assert len(posted) == 1
    # Verify posted as responsible agent persona
    assert posted[0]["username"] in [v["username"] for v in AGENTS_CONFIG.values()]
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
    REMAINING_AGENTS_MAP[thread_key] = ["Innovator"]

    async def mock_run_in_threadpool(func, *args, **kwargs):
        raise Exception("NotFoundError: Model gemini/gemini-3.7-flash does not exist")

    posted = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted.append({"text": text, "username": username, "icon_url": icon_url})
        return {"ok": True, "ts": "12345.0002"}

    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_run_in_threadpool
    )
    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent_response
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )

    res = await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0002",
        raw_text="[TechLead] Analyze this code",
        is_bot_trigger=True,
    )

    assert len(posted) == 1
    assert posted[0]["username"] == "TechLead"
    assert "MODEL NOT FOUND" in posted[0]["text"]
    assert "[Innovator]" in posted[0]["text"]


@pytest.mark.anyio
async def test_process_and_respond_retries_and_clean_error_message(monkeypatch):
    REMAINING_AGENTS_MAP.clear()
    RATE_LIMIT_RETRY_QUEUE.clear()
    RATE_LIMIT_ALREADY_RETRIED.clear()

    thread_key = ("C123", "12345.9999")
    REMAINING_AGENTS_MAP[thread_key] = ["Innovator"]

    call_count = 0

    async def mock_fail_general_error(func, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise Exception("Internal Server Error: Unexpected JSON decoding crash")

    posted = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted.append({"text": text, "username": username, "icon_url": icon_url})
        return {"ok": True, "ts": "12345.9999"}

    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_fail_general_error
    )
    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent_response
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )

    res = await process_and_respond(
        channel_id="C123",
        thread_ts="12345.9999",
        raw_text="[TechLead] Analyze this code",
        is_bot_trigger=True,
    )

    # Must have retried 3 times
    assert call_count == 3
    assert len(posted) == 1
    assert posted[0]["username"] == "TechLead"
    # Clean error message without raw traceback
    assert "Error processing request - check the logs" in posted[0]["text"]
    assert "Unexpected JSON decoding crash" not in posted[0]["text"]
    # Continues thread to next agent
    assert "[Innovator]" in posted[0]["text"]


@pytest.mark.anyio
async def test_process_and_respond_rate_limits_and_retry_flow(monkeypatch):
    REMAINING_AGENTS_MAP.clear()
    RATE_LIMIT_RETRY_QUEUE.clear()
    RATE_LIMIT_ALREADY_RETRIED.clear()

    thread_key = ("C123", "12345.0003")

    # Set up specific short flow: TechLead -> Innovator -> (retry TechLead) -> Architect
    REMAINING_AGENTS_MAP[thread_key] = ["Innovator"]

    posted = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted.append({"text": text, "username": username, "icon_url": icon_url})
        return {"ok": True, "ts": "12345.0003"}

    # 1. First run of TechLead encounters 429 RateLimitError
    async def mock_fail_rate_limit(func, *args, **kwargs):
        raise Exception("RateLimitError: 429 Too Many Requests, quota exceeded")

    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_fail_rate_limit
    )
    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent_response
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0003",
        raw_text="[TechLead] Database architecture review",
        is_bot_trigger=True,
    )

    assert len(posted) == 1
    assert posted[0]["username"] == "TechLead"
    assert (
        "RATE LIMITS CROSSED, will retry at the end of discussion" in posted[0]["text"]
    )
    assert "[Innovator]" in posted[0]["text"]
    assert thread_key in RATE_LIMIT_RETRY_QUEUE
    assert "TechLead" in RATE_LIMIT_RETRY_QUEUE[thread_key]

    # 2. Innovator runs normally, remaining is empty -> nominates retry for TechLead!
    mock_choice = MagicMock()
    mock_choice.message.content = "My innovator perspective."
    mock_choice.message.tool_calls = None
    mock_resp = MagicMock(choices=[mock_choice], usage=None)

    async def mock_success_innovator(func, *args, **kwargs):
        return mock_resp

    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_success_innovator
    )

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0003",
        raw_text="[Innovator] Your turn",
        is_bot_trigger=True,
    )

    assert len(posted) == 2
    assert posted[1]["username"] == "Innovator"
    assert "[TechLead]" in posted[1]["text"]
    # Verify TechLead is now marked as already retried once
    assert "TechLead" in RATE_LIMIT_ALREADY_RETRIED[thread_key]

    # 3. TechLead retries at the end of discussion, fails again with RateLimit:
    # Must post 'RATE LIMITS CROSSED' and NOT be retried again.
    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_fail_rate_limit
    )

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0003",
        raw_text="[TechLead] Retry turn",
        is_bot_trigger=True,
    )

    assert len(posted) == 3
    assert posted[2]["username"] == "TechLead"
    # Should say RATE LIMITS CROSSED without 'will retry'
    assert "RATE LIMITS CROSSED" in posted[2]["text"]
    assert "will retry" not in posted[2]["text"]
    # Should NOT be added back to RATE_LIMIT_RETRY_QUEUE
    assert "TechLead" not in RATE_LIMIT_RETRY_QUEUE.get(thread_key, [])
    # Clean termination without forced closing nomination tag
    assert "[Architect]" not in posted[2]["text"]


@pytest.mark.anyio
async def test_process_and_respond_503_high_demand_first_skip_and_random_handoff(
    monkeypatch,
):
    REMAINING_AGENTS_MAP.clear()
    RATE_LIMIT_RETRY_QUEUE.clear()
    RATE_LIMIT_ALREADY_RETRIED.clear()
    HIGH_DEMAND_ERRORED_AGENTS.clear()

    thread_key = ("C_503", "12345.5031")
    # Remaining agents list before TechLead speaks: Innovator, DeepSeek_Challenger
    REMAINING_AGENTS_MAP[thread_key] = ["Innovator", "DeepSeek_Challenger"]

    posted = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted.append({"text": text, "username": username, "icon_url": icon_url})
        return {"ok": True, "ts": "12345.5031"}

    # Simulate 503 High Demand error
    async def mock_fail_503(func, *args, **kwargs):
        raise Exception(
            "ServiceUnavailableError: 503 The server is currently overloaded with high demand"
        )

    monkeypatch.setattr("orchestration.agent_router.run_in_threadpool", mock_fail_503)
    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent_response
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )

    res = await process_and_respond(
        channel_id="C_503",
        thread_ts="12345.5031",
        raw_text="[TechLead] Analyze backend structure",
        is_bot_trigger=True,
    )

    assert len(posted) == 1
    assert posted[0]["username"] == "TechLead"
    assert (
        "HIGH DEMAND / SERVICE OVERLOADED (503) - Skipping for now, will retry later"
        in posted[0]["text"]
    )
    # Verify TechLead is recorded as 503-errored for this thread
    assert thread_key in HIGH_DEMAND_ERRORED_AGENTS
    assert "TechLead" in HIGH_DEMAND_ERRORED_AGENTS[thread_key]

    # Verify TechLead is kept in REMAINING_AGENTS_MAP
    assert "TechLead" in REMAINING_AGENTS_MAP[thread_key]
    assert "Innovator" in REMAINING_AGENTS_MAP[thread_key]
    assert "DeepSeek_Challenger" in REMAINING_AGENTS_MAP[thread_key]

    # Verify next nomination is one of the other candidates
    nominated_tag = None
    if "[Innovator]" in posted[0]["text"]:
        nominated_tag = "Innovator"
    elif "[DeepSeek_Challenger]" in posted[0]["text"]:
        nominated_tag = "DeepSeek_Challenger"
    assert nominated_tag in ["Innovator", "DeepSeek_Challenger"]


@pytest.mark.anyio
async def test_process_and_respond_503_high_demand_second_query_retries_and_terminates(
    monkeypatch,
):
    REMAINING_AGENTS_MAP.clear()
    RATE_LIMIT_RETRY_QUEUE.clear()
    RATE_LIMIT_ALREADY_RETRIED.clear()
    HIGH_DEMAND_ERRORED_AGENTS.clear()

    thread_key = ("C_503", "12345.5032")
    # TechLead already encountered 503 once
    HIGH_DEMAND_ERRORED_AGENTS[thread_key] = {"TechLead"}
    REMAINING_AGENTS_MAP[thread_key] = []  # No other agents left

    posted = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted.append({"text": text, "username": username, "icon_url": icon_url})
        return {"ok": True, "ts": "12345.5032"}

    attempts_count = 0

    async def mock_fail_503_retry(func, *args, **kwargs):
        nonlocal attempts_count
        attempts_count += 1
        raise Exception("503 Server Temporarily Unavailable (high demand spike)")

    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_fail_503_retry
    )
    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent_response
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )

    res = await process_and_respond(
        channel_id="C_503",
        thread_ts="12345.5032",
        raw_text="[TechLead] Second attempt query",
        is_bot_trigger=True,
    )

    assert len(posted) == 1
    assert posted[0]["username"] == "TechLead"
    # Should say HIGH DEMAND / SERVICE OVERLOADED (503) without 'will retry later'
    assert "HIGH DEMAND / SERVICE OVERLOADED (503)" in posted[0]["text"]
    assert "will retry later" not in posted[0]["text"]
    # Retried 3 times standardly because allow_503_immediate_retry=True on second query
    assert attempts_count == 3
    # Should NOT be added back to remaining
    assert "TechLead" not in REMAINING_AGENTS_MAP.get(thread_key, [])
    # Clean termination without forced closing nomination tag
    assert "[Architect]" not in posted[0]["text"]


@pytest.mark.anyio
async def test_execute_llm_with_retry_503_selective_bypass():
    call_count = 0

    def mock_503_llm():
        nonlocal call_count
        call_count += 1
        raise Exception("503 Service Unavailable: overloaded")

    # 1. When allow_503_immediate_retry=False (first encounter): raises immediately after 1 attempt
    call_count = 0
    with pytest.raises(Exception, match="503 Service Unavailable"):
        await execute_llm_with_retry(
            mock_503_llm,
            {},
            agent_key="TestAgent",
            thread_key=("C", "T"),
            allow_503_immediate_retry=False,
        )
    assert call_count == 1

    # 2. When allow_503_immediate_retry=True (second encounter): retries 3 times
    call_count = 0
    with pytest.raises(Exception, match="503 Service Unavailable"):
        await execute_llm_with_retry(
            mock_503_llm,
            {},
            agent_key="TestAgent",
            thread_key=("C", "T"),
            allow_503_immediate_retry=True,
        )
    assert call_count == 3

    # 3. For non-503 errors with allow_503_immediate_retry=False: STILL retries 3 times as standard!
    call_count = 0

    def mock_network_error():
        nonlocal call_count
        call_count += 1
        raise ConnectionResetError("Connection reset by peer")

    with pytest.raises(ConnectionResetError):
        await execute_llm_with_retry(
            mock_network_error,
            {},
            agent_key="TestAgent",
            thread_key=("C", "T"),
            allow_503_immediate_retry=False,
        )
    assert call_count == 3


def test_is_discussion_active_behavior():
    thread_key = ("C_ACTIVE_TEST", "12345.1001")
    REMAINING_AGENTS_MAP.pop(thread_key, None)
    RATE_LIMIT_RETRY_QUEUE.pop(thread_key, None)
    ACTIVE_THREAD_TASKS.pop(thread_key, None)

    # 1. Fresh / idle thread is not active
    assert is_discussion_active("C_ACTIVE_TEST", "12345.1001") is False

    # 2. Active when remaining agents exist
    REMAINING_AGENTS_MAP[thread_key] = ["TechLead"]
    assert is_discussion_active("C_ACTIVE_TEST", "12345.1001") is True

    # 3. Active when retry queue has entries
    REMAINING_AGENTS_MAP.pop(thread_key, None)
    RATE_LIMIT_RETRY_QUEUE[thread_key] = ["Innovator"]
    assert is_discussion_active("C_ACTIVE_TEST", "12345.1001") is True

    # Clean up
    RATE_LIMIT_RETRY_QUEUE.pop(thread_key, None)
    assert is_discussion_active("C_ACTIVE_TEST", "12345.1001") is False


@pytest.mark.anyio
async def test_interrupt_thread_discussion_clears_state_and_cancels_task():
    import asyncio

    thread_key = ("C_INTERRUPT_TEST", "12345.1002")

    REMAINING_AGENTS_MAP[thread_key] = ["TechLead", "Innovator"]
    RATE_LIMIT_RETRY_QUEUE[thread_key] = ["Visionary"]
    RATE_LIMIT_ALREADY_RETRIED[thread_key] = {"TechLead"}
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

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted_messages.append({"text": text, "username": username})
        return {"ok": True, "ts": "12345.1003"}

    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent_response
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )

    mock_choice_1 = MagicMock()
    mock_choice_1.message.content = "Turn 1 answer to old query. [TechLead]"
    mock_choice_1.message.tool_calls = None
    mock_resp_1 = MagicMock(choices=[mock_choice_1], usage=None)

    async def mock_run_turn_1(func, *args, **kwargs):
        # Simulate user interruption arriving while Turn 1 is waiting on LLM
        interrupt_thread_discussion("C_INTERRUPT_LLM", "12345.1003")
        return mock_resp_1

    monkeypatch.setattr("orchestration.agent_router.run_in_threadpool", mock_run_turn_1)

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
    mock_choice_2.message.content = "Turn 2 answer to new query. [TechLead]"
    mock_choice_2.message.tool_calls = None
    mock_resp_2 = MagicMock(choices=[mock_choice_2], usage=None)

    async def mock_run_turn_2(func, *args, **kwargs):
        return mock_resp_2

    monkeypatch.setattr("orchestration.agent_router.run_in_threadpool", mock_run_turn_2)

    res2 = await process_and_respond(
        channel_id="C_INTERRUPT_LLM",
        thread_ts="12345.1003",
        raw_text="<@U12345> New query topic 2",
        is_bot_trigger=False,
    )

    # Turn 2 successfully posts
    assert len(posted_messages) == 1
    assert "Turn 2 answer to new query." in posted_messages[0]["text"]
    assert "[TechLead]" in posted_messages[0]["text"]


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

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted_messages.append({"text": text, "username": username})
        return {"ok": True, "ts": "12345.1004"}

    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent_response
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )

    mock_choice = MagicMock()
    mock_choice.message.content = "Turn 1 answer before sleep. [TechLead]"
    mock_choice.message.tool_calls = None
    mock_resp = MagicMock(choices=[mock_choice], usage=None)

    async def mock_run(func, *args, **kwargs):
        return mock_resp

    monkeypatch.setattr("orchestration.agent_router.run_in_threadpool", mock_run)

    # Interrupt injected during asyncio.sleep in router
    async def mock_sleep_with_interrupt(duration):
        interrupt_thread_discussion("C_INTERRUPT_SLEEP", "12345.1004")

    monkeypatch.setattr(
        "orchestration.agent_router.asyncio.sleep", mock_sleep_with_interrupt
    )

    res = await process_and_respond(
        channel_id="C_INTERRUPT_SLEEP",
        thread_ts="12345.1004",
        raw_text="<@U12345> Query that gets interrupted during delay",
        is_bot_trigger=False,
    )

    # Turn 1 should be cancelled/superseded and not post
    assert res.get("cancelled") is True
    assert len(posted_messages) == 0


def test_extract_history_limit_formats():
    # 1. Standard format /history 5
    limit, cleaned = extract_history_limit("<@U12345> Tell me a joke /history 5")
    assert limit == 5
    assert cleaned == "<@U12345> Tell me a joke"

    # 2. Bracketed format /history [3]
    limit, cleaned = extract_history_limit(
        "@CaiLogics [TechLead] Explain rust /history [3]"
    )
    assert limit == 3
    assert cleaned == "@CaiLogics [TechLead] Explain rust"

    # 3. Explicit int format /history [2:int]
    limit, cleaned = extract_history_limit("@app_mention /history [2:int] summarize")
    assert limit == 2
    assert cleaned == "@app_mention summarize"

    # 4. Colon format /history: 10 and /history:4
    limit, cleaned = extract_history_limit("@CaiLogics /history: 10 test prompt")
    assert limit == 10
    assert cleaned == "@CaiLogics test prompt"

    limit, cleaned = extract_history_limit("@CaiLogics /history:4 test prompt")
    assert limit == 4
    assert cleaned == "@CaiLogics test prompt"

    # 5. Equals format /history=7
    limit, cleaned = extract_history_limit("@CaiLogics /history=7 test prompt")
    assert limit == 7
    assert cleaned == "@CaiLogics test prompt"

    # 6. Zero history /history 0
    limit, cleaned = extract_history_limit("Prompt with /history 0")
    assert limit == 0
    assert cleaned == "Prompt with"

    # 7. No history command
    limit, cleaned = extract_history_limit("Plain prompt without history")
    assert limit is None
    assert cleaned == "Plain prompt without history"

    # 8. Command in middle of text
    limit, cleaned = extract_history_limit(
        "@CaiLogics What is /history 3 quantum computing?"
    )
    assert limit == 3
    assert cleaned == "@CaiLogics What is quantum computing?"

    # 9. Natural language variations (Croatian and English)
    limit, cleaned = extract_history_limit("uzmi u obzir zadnjih 5 poruka i nastavi")
    assert limit == 5
    assert cleaned == "i nastavi"

    limit, cleaned = extract_history_limit("last 3 messages analyze the issue")
    assert limit == 3
    assert cleaned == "analyze the issue"

    limit, cleaned = extract_history_limit("povijest od 4 poruke i odgovori")
    assert limit == 4
    assert cleaned == "i odgovori"


def test_parse_agent_and_prompt_cleans_history_command():
    agent_key, prompt, was_explicit = parse_agent_and_prompt(
        "<@U12345> [Visionary] What is AI? /history 4",
        "C_HIST_PARSE",
        "12345.0001",
    )
    assert agent_key == "Visionary"
    assert prompt == "What is AI?"
    assert was_explicit is True

    agent_key2, prompt2, was_explicit2 = parse_agent_and_prompt(
        "<@U12345> /history [2] What is AI?",
        "C_HIST_PARSE",
        "12345.0002",
    )
    assert agent_key2 in get_enabled_agents()
    assert prompt2 == "What is AI?"
    assert was_explicit2 is False


@pytest.mark.anyio
async def test_process_and_respond_history_limit_initial_prompt(monkeypatch):
    """
    Scenario: Thread has 5 prior messages. User submits prompt with /history 2.
    Expected:
    - LiteLLM receives only the last 2 prior messages + current user prompt.
    - Active limit is stored in THREAD_HISTORY_LIMITS.
    """
    thread_key = ("C_HIST_TEST", "12345.2001")
    THREAD_HISTORY_LIMITS.pop(thread_key, None)
    REMAINING_AGENTS_MAP.pop(thread_key, None)

    mock_history = [
        {"role": "user", "content": "Message 1"},
        {"role": "assistant", "content": "Reply 1"},
        {"role": "user", "content": "Message 2"},
        {"role": "assistant", "content": "Reply 2"},
        {"role": "user", "content": "Message 3"},
        {"role": "user", "content": "<@U12345> What now? /history 2"},
    ]

    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history",
        AsyncMock(return_value=mock_history),
    )

    captured_messages = []

    mock_choice = MagicMock()
    mock_choice.message.content = "Visionary response. [TechLead]"
    mock_choice.message.tool_calls = None
    mock_resp = MagicMock(choices=[mock_choice], usage=None)

    async def mock_run(func, *args, **kwargs):
        if "messages" in kwargs:
            captured_messages.extend(kwargs["messages"])
        return mock_resp

    monkeypatch.setattr("orchestration.agent_router.run_in_threadpool", mock_run)
    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response",
        AsyncMock(return_value={"ok": True, "ts": "12345.2002"}),
    )

    await process_and_respond(
        channel_id="C_HIST_TEST",
        thread_ts="12345.2001",
        raw_text="<@U12345> What now? /history 2",
        is_bot_trigger=False,
    )

    # Verify captured_messages:
    # 0: system prompt
    # 1: context prefix (ensures conversational sequence starts with user)
    # 2: Reply 2 (4th message in history)
    # 3: Message 3 (5th message in history)
    # 4: What now? (current user prompt)
    assert len(captured_messages) == 5
    assert captured_messages[0]["role"] == "system"
    assert captured_messages[1]["role"] == "user"
    assert captured_messages[2] == {"role": "assistant", "content": "Reply 2"}
    assert captured_messages[3] == {"role": "user", "content": "Message 3"}
    assert captured_messages[4]["role"] == "user"
    assert "What now?" in captured_messages[4]["content"]

    # Verify THREAD_HISTORY_LIMITS is preserved for subsequent bot turns
    limit_info = get_thread_history_limit("C_HIST_TEST", "12345.2001")
    assert limit_info is not None
    assert limit_info["limit"] == 2
    assert limit_info["prior_cutoff_index"] == 5


@pytest.mark.anyio
async def test_process_and_respond_history_limit_subsequent_bot_rounds(monkeypatch):
    """
    Scenario: Following the previous prompt (/history 2), a bot trigger occurs (e.g. Mistral_TechLead).
    Expected:
    - Scribe/Slack history has 5 prior messages + user prompt + Agent 1 reply.
    - LiteLLM receives last 2 prior messages + user prompt + Agent 1 reply + closing user turn.
    """
    thread_key = ("C_HIST_TEST_2", "12345.3001")
    # Simulate active history limit from turn 1
    set_thread_history_limit(
        "C_HIST_TEST_2", "12345.3001", limit=2, prior_cutoff_index=5
    )

    mock_history = [
        {"role": "user", "content": "Message 1"},
        {"role": "assistant", "content": "Reply 1"},
        {"role": "user", "content": "Message 2"},
        {"role": "assistant", "content": "Reply 2"},
        {"role": "user", "content": "Message 3"},
        # Discussion started here:
        {"role": "user", "content": "What now?"},
        {
            "role": "assistant",
            "content": "[CaiLogics Visionary]: Visionary response. [TechLead]",
        },
    ]

    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history",
        AsyncMock(return_value=mock_history),
    )

    captured_messages = []

    mock_choice = MagicMock()
    mock_choice.message.content = "TechLead response. [Sceptic]"
    mock_choice.message.tool_calls = None
    mock_resp = MagicMock(choices=[mock_choice], usage=None)

    async def mock_run(func, *args, **kwargs):
        if "messages" in kwargs:
            captured_messages.extend(kwargs["messages"])
        return mock_resp

    monkeypatch.setattr("orchestration.agent_router.run_in_threadpool", mock_run)
    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response",
        AsyncMock(return_value={"ok": True, "ts": "12345.3003"}),
    )

    await process_and_respond(
        channel_id="C_HIST_TEST_2",
        thread_ts="12345.3001",
        raw_text="Visionary response. [TechLead]",
        is_bot_trigger=True,
    )

    # Verify captured_messages:
    # 0: system prompt
    # 1: context prefix (ensures start is user)
    # 2: Reply 2 (prior)
    # 3: Message 3 (prior)
    # 4: What now? (discussion prompt)
    # 5: Visionary response... (discussion reply 1)
    # 6: user turn prompting TechLead (ensures conversation ends with user)
    assert len(captured_messages) == 7
    assert captured_messages[0]["role"] == "system"
    assert captured_messages[1]["role"] == "user"
    assert captured_messages[2] == {"role": "assistant", "content": "Reply 2"}
    assert captured_messages[3] == {"role": "user", "content": "Message 3"}
    assert captured_messages[4] == {"role": "user", "content": "What now?"}
    assert captured_messages[5]["role"] == "assistant"
    assert "[CaiLogics Visionary]" in captured_messages[5]["content"]
    assert captured_messages[6]["role"] == "user"
    assert "TechLead" in captured_messages[6]["content"]


@pytest.mark.anyio
async def test_process_and_respond_subsequent_prompt_resets_full_history(monkeypatch):
    """
    Scenario: In a thread that previously had /history 2, a new user prompt is submitted WITHOUT /history.
    Expected:
    - THREAD_HISTORY_LIMITS is cleared.
    - LiteLLM receives ALL prior messages (complete thread history) + new prompt.
    """
    thread_key = ("C_HIST_TEST_RESET", "12345.4001")
    # Simulate pre-existing limit
    set_thread_history_limit(
        "C_HIST_TEST_RESET", "12345.4001", limit=2, prior_cutoff_index=3
    )

    mock_history = [
        {"role": "user", "content": "Message 1"},
        {"role": "assistant", "content": "Reply 1"},
        {"role": "user", "content": "Message 2"},
        {"role": "assistant", "content": "Reply 2"},
        {"role": "user", "content": "<@U12345> Tell me more"},
    ]

    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history",
        AsyncMock(return_value=mock_history),
    )

    captured_messages = []

    mock_choice = MagicMock()
    mock_choice.message.content = "Full history response. [TechLead]"
    mock_choice.message.tool_calls = None
    mock_resp = MagicMock(choices=[mock_choice], usage=None)

    async def mock_run(func, *args, **kwargs):
        if "messages" in kwargs:
            captured_messages.extend(kwargs["messages"])
        return mock_resp

    monkeypatch.setattr("orchestration.agent_router.run_in_threadpool", mock_run)
    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response",
        AsyncMock(return_value={"ok": True, "ts": "12345.4002"}),
    )

    await process_and_respond(
        channel_id="C_HIST_TEST_RESET",
        thread_ts="12345.4001",
        raw_text="<@U12345> Tell me more",
        is_bot_trigger=False,
    )

    # Limit should be cleared
    assert get_thread_history_limit("C_HIST_TEST_RESET", "12345.4001") is None

    # Verify all 4 prior messages + new prompt are in context:
    assert len(captured_messages) == 6
    assert captured_messages[0]["role"] == "system"
    assert captured_messages[1] == {"role": "user", "content": "Message 1"}
    assert captured_messages[2] == {"role": "assistant", "content": "Reply 1"}
    assert captured_messages[3] == {"role": "user", "content": "Message 2"}
    assert captured_messages[4] == {"role": "assistant", "content": "Reply 2"}
    assert captured_messages[5]["role"] == "user"
    assert "Tell me more" in captured_messages[5]["content"]


@pytest.mark.anyio
async def test_process_and_respond_history_zero(monkeypatch):
    """
    Scenario: User specifies /history 0.
    Expected:
    - LiteLLM receives ONLY system prompt + current user prompt.
    """
    thread_key = ("C_HIST_ZERO", "12345.5001")
    THREAD_HISTORY_LIMITS.pop(thread_key, None)

    mock_history = [
        {"role": "user", "content": "Message 1"},
        {"role": "assistant", "content": "Reply 1"},
        {"role": "user", "content": "<@U12345> Fresh start /history 0"},
    ]

    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history",
        AsyncMock(return_value=mock_history),
    )

    captured_messages = []

    mock_choice = MagicMock()
    mock_choice.message.content = "Fresh response. [TechLead]"
    mock_choice.message.tool_calls = None
    mock_resp = MagicMock(choices=[mock_choice], usage=None)

    async def mock_run(func, *args, **kwargs):
        if "messages" in kwargs:
            captured_messages.extend(kwargs["messages"])
        return mock_resp

    monkeypatch.setattr("orchestration.agent_router.run_in_threadpool", mock_run)
    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response",
        AsyncMock(return_value={"ok": True, "ts": "12345.5002"}),
    )

    await process_and_respond(
        channel_id="C_HIST_ZERO",
        thread_ts="12345.5001",
        raw_text="<@U12345> Fresh start /history 0",
        is_bot_trigger=False,
    )

    # Only system prompt + user prompt (0 prior messages)
    assert len(captured_messages) == 2
    assert captured_messages[0]["role"] == "system"
    assert captured_messages[1]["role"] == "user"
    assert "Fresh start" in captured_messages[1]["content"]


def test_build_llm_messages_user_trigger():
    """Test standard user trigger message construction."""
    sys_prompt = "You are a helpful assistant."
    history = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    agent_cfg = {"username": "Test Bot"}
    messages = build_llm_messages(
        system_prompt=sys_prompt,
        effective_history=history,
        prompt="Tell me more",
        is_bot_trigger=False,
        agent_key="Test_Bot",
        agent_cfg=agent_cfg,
    )
    assert len(messages) == 4
    assert messages[0] == {"role": "system", "content": sys_prompt}
    assert messages[1] == {"role": "user", "content": "Hello"}
    assert messages[2] == {"role": "assistant", "content": "Hi there!"}
    assert messages[3] == {"role": "user", "content": "Tell me more"}


def test_build_llm_messages_bot_trigger_strictly_ends_with_user_turn():
    """Test bot trigger appends user turn so Anthropic/Mistral/Gemini won't reject with 400 Bad Request."""
    sys_prompt = "You are The Sceptic."
    history = [
        {"role": "user", "content": "Initial question"},
        {"role": "assistant", "content": "[Innovator]: Propose X [Sceptic]"},
    ]
    agent_cfg = {"username": "The Sceptic"}
    messages = build_llm_messages(
        system_prompt=sys_prompt,
        effective_history=history,
        prompt="Propose X",
        is_bot_trigger=True,
        agent_key="Sceptic",
        agent_cfg=agent_cfg,
    )
    assert len(messages) == 4
    assert messages[0] == {"role": "system", "content": sys_prompt}
    assert messages[1] == {"role": "user", "content": "Initial question"}
    assert messages[2] == {
        "role": "assistant",
        "content": "[Innovator]: Propose X [Sceptic]",
    }
    assert messages[3]["role"] == "user"
    assert "The Sceptic" in messages[3]["content"]


def test_build_llm_messages_leading_assistant_gets_user_prefix():
    """Test that trimmed history starting with assistant message gets a user prefix so API accepts it."""
    sys_prompt = "You are Tech Lead."
    history = [
        {"role": "assistant", "content": "Assistant turn 1"},
        {"role": "assistant", "content": "Assistant turn 2"},
    ]
    agent_cfg = {"username": "Tech Lead"}
    messages = build_llm_messages(
        system_prompt=sys_prompt,
        effective_history=history,
        prompt="Test",
        is_bot_trigger=True,
        agent_key="TechLead",
        agent_cfg=agent_cfg,
    )
    # index 0: system
    # index 1: user prefix
    # index 2: assistant 1
    # index 3: assistant 2
    # index 4: user closing directive
    assert len(messages) == 5
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert messages[2] == {"role": "assistant", "content": "Assistant turn 1"}
    assert messages[3] == {"role": "assistant", "content": "Assistant turn 2"}
    assert messages[4]["role"] == "user"


def test_circuit_breaker_configuration_methods():
    reset_enabled_agents()
    set_circuit_breaker_enabled(True)
    assert is_circuit_breaker_enabled() is True

    set_circuit_breaker_threshold(2.75)
    assert get_circuit_breaker_threshold() == 2.75

    cfg = get_circuit_breaker_config()
    assert cfg["enabled"] is True
    assert cfg["threshold_usd"] == 2.75

    set_circuit_breaker_enabled(False)
    assert is_circuit_breaker_enabled() is False

    # Negative threshold should clamp to 0.0
    set_circuit_breaker_threshold(-1.0)
    assert get_circuit_breaker_threshold() == 0.0


@pytest.mark.anyio
async def test_circuit_breaker_triggers_when_threshold_exceeded(monkeypatch):
    REMAINING_AGENTS_MAP.clear()
    channel_id = "C_CB_TRIGGER"
    thread_ts = "123456789.9001"
    thread_key = (channel_id, thread_ts)
    REMAINING_AGENTS_MAP[thread_key] = ["TechLead", "Architect"]

    set_circuit_breaker_enabled(True)
    set_circuit_breaker_threshold(0.50)

    # Mock calculate_thread_costs to return $0.75 (> $0.50)
    monkeypatch.setattr(
        "orchestration.agent_router.calculate_thread_costs",
        lambda ch, ts: {"grand_total_cost_usd": 0.75},
    )

    posted_messages = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted_messages.append({"text": text, "username": username})
        return {"ok": True, "ts": "12345.9999"}

    llm_called = False

    async def mock_run_in_threadpool(func, *args, **kwargs):
        nonlocal llm_called
        llm_called = True
        raise Exception("LLM should not be called when circuit-breaker is triggered")

    notes_updated = False

    def mock_trigger_notes(channel_id, thread_ts, is_completed=False, **kwargs):
        nonlocal notes_updated
        if is_completed:
            notes_updated = True

    usage_updated = False

    def mock_trigger_usage(channel_id, thread_ts, is_completed=False, **kwargs):
        nonlocal usage_updated
        if is_completed:
            usage_updated = True

    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent_response
    )
    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_run_in_threadpool
    )
    monkeypatch.setattr(
        "orchestration.agent_router.trigger_thread_notes_update", mock_trigger_notes
    )
    monkeypatch.setattr(
        "orchestration.agent_router.trigger_usage_update", mock_trigger_usage
    )

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="Hello agents, continue analysis",
        is_bot_trigger=False,
    )

    assert res.get("circuit_breaker_triggered") is True
    assert llm_called is False
    assert len(posted_messages) == 1
    assert (
        posted_messages[0]["text"]
        == "EXPENSE LIMIT REACHED FOR THE THREAD. ENDING CALLS"
    )
    assert notes_updated is True
    assert usage_updated is True
    assert thread_key not in REMAINING_AGENTS_MAP


@pytest.mark.anyio
async def test_circuit_breaker_allows_normal_response_when_below_threshold(
    monkeypatch,
):
    REMAINING_AGENTS_MAP.clear()
    channel_id = "C_CB_BELOW"
    thread_ts = "123456789.9002"

    set_circuit_breaker_enabled(True)
    set_circuit_breaker_threshold(1.00)

    # Cost is $0.20 (< $1.00)
    monkeypatch.setattr(
        "orchestration.agent_router.calculate_thread_costs",
        lambda ch, ts: {"grand_total_cost_usd": 0.20},
    )

    mock_choice = MagicMock()
    mock_choice.message.content = "Normal agent response [Architect]"
    mock_choice.message.tool_calls = None

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=10, completion_tokens=10)

    async def mock_run_in_threadpool(func, *args, **kwargs):
        return mock_response

    posted_messages = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted_messages.append({"text": text, "username": username})
        return {"ok": True, "ts": "12345.9999"}

    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent_response
    )
    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_run_in_threadpool
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="[Visionary] Normal query",
        is_bot_trigger=False,
    )

    assert res.get("ok") is True
    assert res.get("circuit_breaker_triggered") is not True
    assert len(posted_messages) == 1
    assert "Normal agent response" in posted_messages[0]["text"]


@pytest.mark.anyio
async def test_circuit_breaker_allows_normal_response_when_disabled(monkeypatch):
    REMAINING_AGENTS_MAP.clear()
    channel_id = "C_CB_DISABLED"
    thread_ts = "123456789.9003"

    set_circuit_breaker_enabled(False)
    set_circuit_breaker_threshold(0.10)

    # Cost is $5.00 (> $0.10), but circuit breaker is disabled
    monkeypatch.setattr(
        "orchestration.agent_router.calculate_thread_costs",
        lambda ch, ts: {"grand_total_cost_usd": 5.00},
    )

    mock_choice = MagicMock()
    mock_choice.message.content = "Normal response despite high cost"
    mock_choice.message.tool_calls = None

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=10, completion_tokens=10)

    async def mock_run_in_threadpool(func, *args, **kwargs):
        return mock_response

    posted_messages = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted_messages.append({"text": text, "username": username})
        return {"ok": True, "ts": "12345.9999"}

    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent_response
    )
    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_run_in_threadpool
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="[Visionary] Normal query",
        is_bot_trigger=False,
    )

    assert res.get("ok") is True
    assert res.get("circuit_breaker_triggered") is not True
    assert len(posted_messages) == 1
    assert "Normal response despite high cost" in posted_messages[0]["text"]


def test_extract_max_words_limit():
    from nlp.nl_patterns import extract_max_words_limit

    # Standard syntax
    limit, text = extract_max_words_limit("/max_words 50 hello world")
    assert limit == 50
    assert text == "hello world"

    # Bracketed typed syntax /max_words [x:int]
    limit, text = extract_max_words_limit("/max_words [75:int] evaluate this")
    assert limit == 75
    assert text == "evaluate this"

    # Colon and equals syntax
    limit, text = extract_max_words_limit("/max_words: 30 test")
    assert limit == 30
    assert text == "test"

    limit, text = extract_max_words_limit("/max_words=100 test")
    assert limit == 100
    assert text == "test"

    # Hyphen and single-word variations
    limit, text = extract_max_words_limit("/max-words 25 test")
    assert limit == 25

    limit, text = extract_max_words_limit("/maxwords 15 test")
    assert limit == 15

    # Natural language variations (Croatian and English)
    limit, text = extract_max_words_limit(
        "odgovori u maksimalno 100 riječi na pitanje o k8s"
    )
    assert limit == 100
    assert text == "odgovori na pitanje o k8s"

    limit, text = extract_max_words_limit("odgovori u maximalno 80 rijeci")
    assert limit == 80

    limit, text = extract_max_words_limit("objasni temu u najviše 50 riječi")
    assert limit == 50
    assert text == "objasni temu"

    limit, text = extract_max_words_limit("in max 120 words please")
    assert limit == 120
    assert text == "please"

    limit, text = extract_max_words_limit("up to 60 words summarize")
    assert limit == 60
    assert text == "summarize"

    # No command present
    limit, text = extract_max_words_limit("Just a regular prompt")
    assert limit is None
    assert text == "Just a regular prompt"


def test_extract_min_words_limit():
    from nlp.nl_patterns import extract_min_words_limit

    # Standard syntax
    limit, text = extract_min_words_limit("/min_words 50 hello world")
    assert limit == 50
    assert text == "hello world"

    # Bracketed typed syntax /min_words [x:int]
    limit, text = extract_min_words_limit("/min_words [75:int] evaluate this")
    assert limit == 75
    assert text == "evaluate this"

    # Colon and equals syntax
    limit, text = extract_min_words_limit("/min_words: 30 test")
    assert limit == 30
    assert text == "test"

    limit, text = extract_min_words_limit("/min_words=100 test")
    assert limit == 100
    assert text == "test"

    # Hyphen and single-word variations
    limit, text = extract_min_words_limit("/min-words 25 test")
    assert limit == 25

    limit, text = extract_min_words_limit("/minwords 15 test")
    assert limit == 15

    # Natural language variations (Croatian and English)
    limit, text = extract_min_words_limit("odgovori u minimalno 120 riječi na pitanje")
    assert limit == 120
    assert text == "odgovori na pitanje"

    limit, text = extract_min_words_limit("barem 150 riječi opiši arhitekturu")
    assert limit == 150
    assert text == "opiši arhitekturu"

    limit, text = extract_min_words_limit("at least 100 words detailed analysis")
    assert limit == 100
    assert text == "detailed analysis"

    limit, text = extract_min_words_limit("najmanje 90 rijeci molim")
    assert limit == 90
    assert text == "molim"

    # No command present
    limit, text = extract_min_words_limit("Just a regular prompt")
    assert limit is None
    assert text == "Just a regular prompt"


@pytest.mark.anyio
async def test_process_and_respond_with_max_words(monkeypatch):
    from orchestration.agent_router import (
        REMAINING_AGENTS_MAP,
        RATE_LIMIT_RETRY_QUEUE,
        RATE_LIMIT_ALREADY_RETRIED,
        THREAD_MAX_WORDS_LIMITS,
        get_thread_max_words,
    )

    REMAINING_AGENTS_MAP.clear()
    RATE_LIMIT_RETRY_QUEUE.clear()
    RATE_LIMIT_ALREADY_RETRIED.clear()
    THREAD_MAX_WORDS_LIMITS.clear()

    channel_id = "C_MAX_WORDS"
    thread_ts = "99999.0001"
    thread_key = (channel_id, thread_ts)
    REMAINING_AGENTS_MAP[thread_key] = ["Architect"]

    captured_messages = []

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message = MagicMock(
        content="Concise response under limit [Architect]", tool_calls=None
    )
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=15, completion_tokens=10)

    async def mock_run_in_threadpool(func, *args, **kwargs):
        captured_messages.append(kwargs.get("messages", []))
        return mock_response

    posted_messages = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted_messages.append({"text": text, "username": username})
        return {"ok": True, "ts": thread_ts}

    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent_response
    )
    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_run_in_threadpool
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )
    monkeypatch.setattr("orchestration.agent_router.record_thread_tokens", MagicMock())

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="[TechLead] [Architect] /max_words [40:int] Explain Kubernetes ingress architecture",
        is_bot_trigger=False,
    )

    assert res.get("ok") is True
    # Thread level setting persisted for remaining agents in flow
    assert get_thread_max_words(channel_id, thread_ts) == 40

    # System prompt in LLM call has 40 words limit and skips standard output constraints
    assert len(captured_messages) == 1
    system_msg = captured_messages[0][0]["content"]
    assert (
        "Hard Limit: Your entire response must strictly NOT exceed 40 words."
        in system_msg
    )
    # Skipped standard output_constraints
    assert "1 sentence stating your core verdict/concern." not in system_msg
    assert "2-3 concise bullet points" not in system_msg

    # User message had the /max_words command stripped cleanly
    user_msg = captured_messages[0][-1]["content"]
    assert "/max_words" not in user_msg
    assert "Explain Kubernetes ingress architecture" in user_msg


@pytest.mark.anyio
async def test_process_and_respond_with_min_words(monkeypatch):
    from orchestration.agent_router import (
        REMAINING_AGENTS_MAP,
        RATE_LIMIT_RETRY_QUEUE,
        RATE_LIMIT_ALREADY_RETRIED,
        THREAD_MIN_WORDS_LIMITS,
        get_thread_min_words,
    )

    REMAINING_AGENTS_MAP.clear()
    RATE_LIMIT_RETRY_QUEUE.clear()
    RATE_LIMIT_ALREADY_RETRIED.clear()
    THREAD_MIN_WORDS_LIMITS.clear()

    channel_id = "C_MIN_WORDS"
    thread_ts = "88888.0001"
    thread_key = (channel_id, thread_ts)
    REMAINING_AGENTS_MAP[thread_key] = ["Architect"]

    captured_messages = []

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message = MagicMock(
        content="Detailed thorough response with at least eighty words [Architect]",
        tool_calls=None,
    )
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=15, completion_tokens=85)

    async def mock_run_in_threadpool(func, *args, **kwargs):
        captured_messages.append(kwargs.get("messages", []))
        return mock_response

    posted_messages = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted_messages.append({"text": text, "username": username})
        return {"ok": True, "ts": thread_ts}

    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent_response
    )
    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_run_in_threadpool
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )
    monkeypatch.setattr("orchestration.agent_router.record_thread_tokens", MagicMock())

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="[TechLead] [Architect] /min_words [80:int] Explain Kubernetes ingress architecture",
        is_bot_trigger=False,
    )

    assert res.get("ok") is True
    # Thread level setting persisted for remaining agents in flow
    assert get_thread_min_words(channel_id, thread_ts) == 80

    # System prompt in LLM call has min_words instruction and skips standard output constraints
    assert len(captured_messages) == 1
    system_msg = captured_messages[0][0]["content"]
    assert (
        "Minimum Length: Your entire response must have a minimum length of 80 words."
        in system_msg
    )
    # Skipped standard output_constraints
    assert "1 sentence stating your core verdict/concern." not in system_msg
    assert "2-3 concise bullet points" not in system_msg

    # User message had the /min_words command stripped cleanly
    user_msg = captured_messages[0][-1]["content"]
    assert "/min_words" not in user_msg
    assert "Explain Kubernetes ingress architecture" in user_msg


@pytest.mark.anyio
async def test_process_and_respond_with_min_and_max_words(monkeypatch):
    from orchestration.agent_router import (
        REMAINING_AGENTS_MAP,
        RATE_LIMIT_RETRY_QUEUE,
        RATE_LIMIT_ALREADY_RETRIED,
        THREAD_MAX_WORDS_LIMITS,
        THREAD_MIN_WORDS_LIMITS,
        get_thread_max_words,
        get_thread_min_words,
    )

    REMAINING_AGENTS_MAP.clear()
    RATE_LIMIT_RETRY_QUEUE.clear()
    RATE_LIMIT_ALREADY_RETRIED.clear()
    THREAD_MAX_WORDS_LIMITS.clear()
    THREAD_MIN_WORDS_LIMITS.clear()

    channel_id = "C_MIN_MAX_WORDS"
    thread_ts = "77777.0001"
    thread_key = (channel_id, thread_ts)
    REMAINING_AGENTS_MAP[thread_key] = ["Architect"]

    captured_messages = []

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message = MagicMock(
        content="Response between bounds [Architect]", tool_calls=None
    )
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=15, completion_tokens=60)

    async def mock_run_in_threadpool(func, *args, **kwargs):
        captured_messages.append(kwargs.get("messages", []))
        return mock_response

    posted_messages = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted_messages.append({"text": text, "username": username})
        return {"ok": True, "ts": thread_ts}

    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent_response
    )
    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_run_in_threadpool
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )
    monkeypatch.setattr("orchestration.agent_router.record_thread_tokens", MagicMock())

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="[TechLead] [Architect] /min_words [50:int] /max_words [150:int] Analyze database scaling",
        is_bot_trigger=False,
    )

    assert res.get("ok") is True
    assert get_thread_min_words(channel_id, thread_ts) == 50
    assert get_thread_max_words(channel_id, thread_ts) == 150

    assert len(captured_messages) == 1
    system_msg = captured_messages[0][0]["content"]
    assert (
        "Minimum Length: Your entire response must have a minimum length of 50 words."
        in system_msg
    )
    assert (
        "Hard Limit: Your entire response must strictly NOT exceed 150 words."
        in system_msg
    )

    user_msg = captured_messages[0][-1]["content"]
    assert "/min_words" not in user_msg
    assert "/max_words" not in user_msg
    assert "Analyze database scaling" in user_msg


@pytest.mark.anyio
async def test_process_and_respond_with_natural_language_limits(monkeypatch):
    from orchestration.agent_router import (
        REMAINING_AGENTS_MAP,
        RATE_LIMIT_RETRY_QUEUE,
        RATE_LIMIT_ALREADY_RETRIED,
        THREAD_MAX_WORDS_LIMITS,
        THREAD_MIN_WORDS_LIMITS,
        get_thread_max_words,
        get_thread_min_words,
    )

    REMAINING_AGENTS_MAP.clear()
    RATE_LIMIT_RETRY_QUEUE.clear()
    RATE_LIMIT_ALREADY_RETRIED.clear()
    THREAD_MAX_WORDS_LIMITS.clear()
    THREAD_MIN_WORDS_LIMITS.clear()

    channel_id = "C_NL_LIMITS"
    thread_ts = "66666.0001"
    thread_key = (channel_id, thread_ts)
    REMAINING_AGENTS_MAP[thread_key] = ["Architect"]

    captured_messages = []

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message = MagicMock(
        content="Response under 60 words [Architect]", tool_calls=None
    )
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=15, completion_tokens=30)

    async def mock_run_in_threadpool(func, *args, **kwargs):
        captured_messages.append(kwargs.get("messages", []))
        return mock_response

    posted_messages = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted_messages.append({"text": text, "username": username})
        return {"ok": True, "ts": thread_ts}

    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent_response
    )
    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_run_in_threadpool
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )
    monkeypatch.setattr("orchestration.agent_router.record_thread_tokens", MagicMock())

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="[TechLead] [Architect] odgovori u maksimalno 60 riječi o mikroservisima",
        is_bot_trigger=False,
    )

    assert res.get("ok") is True
    assert get_thread_max_words(channel_id, thread_ts) == 60

    assert len(captured_messages) == 1
    system_msg = captured_messages[0][0]["content"]
    assert (
        "Hard Limit: Your entire response must strictly NOT exceed 60 words."
        in system_msg
    )

    user_msg = captured_messages[0][-1]["content"]
    assert "maksimalno 60 riječi" not in user_msg
    assert "mikroservisima" in user_msg


@pytest.mark.anyio
async def test_process_and_respond_with_exclude_command(monkeypatch):
    from orchestration.agent_router import (
        REMAINING_AGENTS_MAP,
        RATE_LIMIT_RETRY_QUEUE,
        RATE_LIMIT_ALREADY_RETRIED,
    )

    REMAINING_AGENTS_MAP.clear()
    RATE_LIMIT_RETRY_QUEUE.clear()
    RATE_LIMIT_ALREADY_RETRIED.clear()

    channel_id = "C_EXCLUDE_CMD"
    thread_ts = "88888.0001"
    thread_key = (channel_id, thread_ts)

    captured_messages = []

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message = MagicMock(
        content="Reviewing without innovator [ScopeOptimizer]", tool_calls=None
    )
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=20, completion_tokens=40)

    async def mock_run_in_threadpool(func, *args, **kwargs):
        captured_messages.append(kwargs.get("messages", []))
        return mock_response

    posted_messages = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted_messages.append({"text": text, "username": username})
        return {"ok": True, "ts": thread_ts}

    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent_response
    )
    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_run_in_threadpool
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )
    monkeypatch.setattr("orchestration.agent_router.record_thread_tokens", MagicMock())

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="[TechLead] [Innovator] [ScopeOptimizer] /exclude [Innovator] Evaluate the refactoring",
        is_bot_trigger=False,
    )

    assert res.get("ok") is True
    # Innovator should be removed immediately from remaining agents
    assert "Innovator" not in REMAINING_AGENTS_MAP[thread_key]
    assert REMAINING_AGENTS_MAP[thread_key] == ["ScopeOptimizer"]

    # Command should be cleanly stripped from user message passed to model
    user_msg = captured_messages[0][-1]["content"]
    assert "/exclude" not in user_msg
    assert "Evaluate the refactoring" in user_msg


@pytest.mark.anyio
async def test_process_and_respond_with_multi_agent_exclude_command(monkeypatch):
    from orchestration.agent_router import (
        REMAINING_AGENTS_MAP,
        RATE_LIMIT_RETRY_QUEUE,
        RATE_LIMIT_ALREADY_RETRIED,
    )

    REMAINING_AGENTS_MAP.clear()
    RATE_LIMIT_RETRY_QUEUE.clear()
    RATE_LIMIT_ALREADY_RETRIED.clear()

    channel_id = "C_MULTI_EXCLUDE"
    thread_ts = "88888.0002"
    thread_key = (channel_id, thread_ts)

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message = MagicMock(
        content="Reviewing [ScalabilityEngineer]", tool_calls=None
    )
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=20, completion_tokens=40)

    async def mock_run_in_threadpool(func, *args, **kwargs):
        return mock_response

    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response",
        AsyncMock(return_value={"ok": True, "ts": thread_ts}),
    )
    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_run_in_threadpool
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )
    monkeypatch.setattr("orchestration.agent_router.record_thread_tokens", MagicMock())

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="[TechLead] [Innovator] [ScopeOptimizer] [ScalabilityEngineer] /exclude [Innovator] [ScopeOptimizer] Plan sprint",
        is_bot_trigger=False,
    )

    assert res.get("ok") is True
    # Both Innovator and ScopeOptimizer should be removed from remaining agents
    assert REMAINING_AGENTS_MAP[thread_key] == ["ScalabilityEngineer"]


@pytest.mark.anyio
async def test_process_and_respond_with_default_flow_and_exclude(monkeypatch):
    from orchestration.agent_router import (
        REMAINING_AGENTS_MAP,
        RATE_LIMIT_RETRY_QUEUE,
        RATE_LIMIT_ALREADY_RETRIED,
    )

    REMAINING_AGENTS_MAP.clear()
    RATE_LIMIT_RETRY_QUEUE.clear()
    RATE_LIMIT_ALREADY_RETRIED.clear()

    channel_id = "C_DEFAULT_EXCLUDE"
    thread_ts = "88888.0003"
    thread_key = (channel_id, thread_ts)

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message = MagicMock(
        content="Debating without techlead and innovator [Sceptic]", tool_calls=None
    )
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=20, completion_tokens=40)

    async def mock_run_in_threadpool(func, *args, **kwargs):
        return mock_response

    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response",
        AsyncMock(return_value={"ok": True, "ts": thread_ts}),
    )
    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_run_in_threadpool
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )
    monkeypatch.setattr("orchestration.agent_router.record_thread_tokens", MagicMock())

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="Debate this architecture /exclude [TechLead] [Innovator]",
        is_bot_trigger=False,
    )

    assert res.get("ok") is True
    assert "TechLead" not in REMAINING_AGENTS_MAP[thread_key]
    assert "Innovator" not in REMAINING_AGENTS_MAP[thread_key]


@pytest.mark.anyio
async def test_process_and_respond_with_nl_exclude(monkeypatch):
    from orchestration.agent_router import (
        REMAINING_AGENTS_MAP,
        RATE_LIMIT_RETRY_QUEUE,
        RATE_LIMIT_ALREADY_RETRIED,
    )

    REMAINING_AGENTS_MAP.clear()
    RATE_LIMIT_RETRY_QUEUE.clear()
    RATE_LIMIT_ALREADY_RETRIED.clear()

    channel_id = "C_NL_EXCLUDE"
    thread_ts = "88888.0004"
    thread_key = (channel_id, thread_ts)

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message = MagicMock(content="Debating topic [Sceptic]", tool_calls=None)
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=20, completion_tokens=40)

    async def mock_run_in_threadpool(func, *args, **kwargs):
        return mock_response

    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response",
        AsyncMock(return_value={"ok": True, "ts": thread_ts}),
    )
    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_run_in_threadpool
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )
    monkeypatch.setattr("orchestration.agent_router.record_thread_tokens", MagicMock())

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="Raspravite o arhitekturi bez agenta TechLead",
        is_bot_trigger=False,
    )

    assert res.get("ok") is True
    assert "TechLead" not in REMAINING_AGENTS_MAP[thread_key]


@pytest.mark.anyio
async def test_partial_tag_routing(monkeypatch):
    from orchestration.agent_router import REMAINING_AGENTS_MAP

    REMAINING_AGENTS_MAP.clear()

    channel_id = "C_PARTIAL_TAG"
    thread_ts = "99999.0001"

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message = MagicMock(
        content="Visionary speaking [TechLead]", tool_calls=None
    )
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=20, completion_tokens=40)

    posted_messages = []

    async def mock_post_agent(channel_id, thread_ts, text, username, icon_url=None):
        posted_messages.append({"username": username, "text": text})
        return {"ok": True, "ts": thread_ts}

    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent
    )
    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool",
        AsyncMock(return_value=mock_response),
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )
    monkeypatch.setattr("orchestration.agent_router.record_thread_tokens", MagicMock())

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="[Vis] what are your architectural thoughts?",
        is_bot_trigger=False,
    )

    assert res.get("ok") is True
    assert len(posted_messages) == 1
    assert "Visionary" in posted_messages[0]["username"]


@pytest.mark.anyio
async def test_ambiguous_tag_system_warning_halt(monkeypatch):
    from orchestration.agent_router import REMAINING_AGENTS_MAP

    REMAINING_AGENTS_MAP.clear()

    channel_id = "C_AMBIG_TAG"
    thread_ts = "99999.0002"

    system_posts = []

    async def mock_post_system(
        channel_id, thread_ts, text, username=None, icon_url=None
    ):
        system_posts.append({"username": username, "text": text})
        return {"ok": True, "ts": thread_ts}

    llm_called = False

    async def mock_run_in_threadpool(func, *args, **kwargs):
        nonlocal llm_called
        llm_called = True
        return MagicMock()

    monkeypatch.setattr(
        "orchestration.agent_router.post_system_message", mock_post_system
    )
    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_run_in_threadpool
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="[Arch] please review this design",
        is_bot_trigger=False,
    )

    assert res.get("halted") is True
    assert res.get("reason") == "ambiguous_agent"
    assert llm_called is False
    assert len(system_posts) == 1
    assert "Ambiguous Agent Reference" in system_posts[0]["text"]
    assert "Architect" in system_posts[0]["text"]
    assert "SecurityArchitect" in system_posts[0]["text"]
    assert "[" not in system_posts[0]["text"]
    assert "]" not in system_posts[0]["text"]


@pytest.mark.anyio
async def test_unrecognized_tag_system_warning_halt(monkeypatch):
    from orchestration.agent_router import REMAINING_AGENTS_MAP

    REMAINING_AGENTS_MAP.clear()

    channel_id = "C_UNREC_TAG"
    thread_ts = "99999.0003"

    system_posts = []

    async def mock_post_system(
        channel_id, thread_ts, text, username=None, icon_url=None
    ):
        system_posts.append({"username": username, "text": text})
        return {"ok": True, "ts": thread_ts}

    llm_called = False

    async def mock_run_in_threadpool(func, *args, **kwargs):
        nonlocal llm_called
        llm_called = True
        return MagicMock()

    monkeypatch.setattr(
        "orchestration.agent_router.post_system_message", mock_post_system
    )
    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_run_in_threadpool
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="[SomeUnknownAgent] please review this design",
        is_bot_trigger=False,
    )

    assert res.get("halted") is True
    assert res.get("reason") == "unrecognized_agent"
    assert llm_called is False
    assert len(system_posts) == 1
    assert "Unrecognized Agent Tag" in system_posts[0]["text"]
    assert "SomeUnknownAgent" in system_posts[0]["text"]
    assert "[" not in system_posts[0]["text"]
    assert "]" not in system_posts[0]["text"]
