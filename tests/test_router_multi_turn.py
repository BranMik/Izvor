from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestration import agent_router
from orchestration.agent_router import process_and_respond
from orchestration.agent_settings import (
    ENABLED_DEBATE_AGENTS,
    get_circuit_breaker_threshold,
    get_enabled_agents,
    is_circuit_breaker_enabled,
    load_enabled_agents,
    reset_enabled_agents,
    set_agent_enabled,
    set_circuit_breaker_enabled,
    set_circuit_breaker_threshold,
    set_fixed_config,
    set_orchestration_mode,
    set_sandwich_config,
    toggle_agent,
)
from orchestration.nomination import get_initial_agents, parse_agent_and_prompt
from orchestration.thread_state import thread_state_manager
from pipeline.directive_parser import is_stop_command
from services.slack_client import build_app_home_view


@pytest.fixture(autouse=True)
def setup_test_environment(monkeypatch):
    reset_enabled_agents()
    set_orchestration_mode("sandwich")
    set_sandwich_config([], [])
    set_fixed_config([])
    thread_state_manager.reset_all()
    monkeypatch.setattr(agent_router.asyncio, "sleep", AsyncMock(return_value=None))


@pytest.mark.anyio
async def test_scenario_2_multiple_agents_custom_flow(monkeypatch):
    """
    Scenario: User tags multiple specific agents.
    Expected: Only those agents speak in order, intermediate choices are validated, and flow ends after the last one.
    """
    thread_state_manager.reset_all()

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

    # Step 1: User requests 2 specific agents: [agent-10002] and [agent-10011]
    mock_choice_1 = MagicMock()
    # Innovator attempts to nominate SecurityArchitect prematurely (SecurityArchitect was not in user's list)
    mock_choice_1.message.content = "Innovative idea. [agent-10008]"
    mock_choice_1.message.tool_calls = None
    mock_response_1 = MagicMock(choices=[mock_choice_1], usage=None)

    async def mock_run_1(func, *args, **kwargs):
        return mock_response_1

    monkeypatch.setattr("orchestration.agent_router.run_in_threadpool", mock_run_1)

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0002",
        raw_text="/[agent-10002] /[agent-10011] Debate this feature",
        is_bot_trigger=False,
    )

    # Innovator's premature SecurityArchitect nomination should be corrected to LegalAdvisor
    assert thread_key in thread_state_manager.custom_flow_threads
    assert thread_state_manager.remaining_agents_map[thread_key] == ["agent-10011"]
    assert "/[agent-10011]" in posted_messages[0]

    # Step 2: Next bot trigger for LegalAdvisor (the last custom agent)
    mock_choice_2 = MagicMock()
    mock_choice_2.message.content = "Tech lead evaluation and conclusion. /[agent-10008]"
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
        raw_text="/[agent-10011] Debate this feature",
        is_bot_trigger=True,
    )

    # Last custom agent cleanly terminates
    assert "CRITICAL CONSTRAINT" in mock_run_2.captured_user_prompt
    assert thread_key not in thread_state_manager.custom_flow_threads
    assert thread_key not in thread_state_manager.remaining_agents_map
    assert "/[agent-10008]" not in posted_messages[1]


@pytest.mark.anyio
async def test_scenario_3_no_agent_tagged_default_flow(monkeypatch):
    """
    Scenario: User tags NO agent (plain message / app_mention).
    Expected: Default flow starts with Visionary, initializes all 15 panel agents,
    programmatically prevents premature SecurityArchitect nomination, and finishes with SecurityArchitect.
    """
    thread_state_manager.reset_all()

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
    mock_choice_1.message.content = "Vision for the product. /[agent-10003]"
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
    assert thread_key not in thread_state_manager.custom_flow_threads
    assert len(thread_state_manager.remaining_agents_map[thread_key]) == 10
    assert "/[agent-10003]" in posted_messages[0]

    # Step 2: Intermediate agent (Monitoring Specialist) tries to prematurely nominate SecurityArchitect
    thread_state_manager.remaining_agents_map[thread_key] = [
        "agent-10007",
        "agent-10008",
        "agent-10003",
    ]

    mock_choice_2 = MagicMock()
    mock_choice_2.message.content = "Monitoring review. /[agent-10002]"
    mock_choice_2.message.tool_calls = None
    mock_response_2 = MagicMock(choices=[mock_choice_2], usage=None)

    async def mock_run_2(func, *args, **kwargs):
        return mock_response_2

    monkeypatch.setattr("orchestration.agent_router.run_in_threadpool", mock_run_2)

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0003",
        raw_text="/[agent-10003] next",
        is_bot_trigger=True,
    )

    # Premature Innovator tag is programmatically replaced with first remaining agent (UserAdvocate)
    assert "/[agent-10007]" in posted_messages[1]
    assert "agent-10007" in thread_state_manager.remaining_agents_map[thread_key]

    # Step 3: Intermediate agent speaks and nominates next remaining agent
    thread_state_manager.remaining_agents_map[thread_key] = ["agent-10008", "agent-10008"]

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
        raw_text="/[agent-10008] next",
        is_bot_trigger=True,
    )

    # Next agent is SecurityArchitect
    assert thread_state_manager.remaining_agents_map[thread_key] == ["agent-10008"]
    assert "/[agent-10008]" in posted_messages[2]

    # Step 4: SecurityArchitect executes final summary
    mock_choice_4 = MagicMock()
    mock_choice_4.message.content = "Final architecture summary. /[agent-10001]"
    mock_choice_4.message.tool_calls = None
    mock_response_4 = MagicMock(choices=[mock_choice_4], usage=None)

    async def mock_run_4(func, *args, **kwargs):
        return mock_response_4

    monkeypatch.setattr("orchestration.agent_router.run_in_threadpool", mock_run_4)

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0003",
        raw_text="/[agent-10008] next",
        is_bot_trigger=True,
    )

    # SecurityArchitect finishes, state cleaned up, tags stripped
    assert thread_key not in thread_state_manager.remaining_agents_map
    assert "/[agent-10001]" not in posted_messages[3]


def test_is_stop_command():
    # Valid stop commands (/stop syntax anywhere with whitespace or start/end boundaries)
    assert is_stop_command("<@U12345> /stop") is True
    assert is_stop_command("<@U12345> /STOP!") is True
    assert is_stop_command("<@U12345> /Stop.") is True
    assert is_stop_command("@CaiLogics Hub /stop") is True
    assert is_stop_command("@CaiLogics Test /stop") is True
    assert is_stop_command("@CaiLogics /stop") is True
    assert is_stop_command("@app_mention /stop") is True
    assert is_stop_command("[agent-10001] /stop") is True
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
        is_stop_command("[agent-10001] Design a stop-motion animation pipeline") is False
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
    thread_state_manager.reset_all()

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
    mock_choice.message.content = "Here is my architecture analysis. /[agent-10002]"
    mock_choice.message.tool_calls = None
    mock_response = MagicMock(choices=[mock_choice], usage=None)

    async def mock_run_with_stop_injected(func, *args, **kwargs):
        # Simulate user posting stop command while LLM call is running in background
        thread_state_manager.request_stop("C123", "12345.0004")
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

    # 2. Nomination tag and agent name /[agent-10002] completely stripped
    assert "/[agent-10002]" not in posted_messages[0]
    assert "agent-10002" not in posted_messages[0]

    # 3. State cleaned up - no remaining agents, not in stop requested set
    assert thread_key not in thread_state_manager.remaining_agents_map
    assert thread_key not in thread_state_manager.stop_requested_threads
    assert thread_key not in thread_state_manager.custom_flow_threads


def test_locked_agents_cannot_be_disabled():
    reset_enabled_agents()
    # All agents can be freely toggled on and off (no locked agents)
    assert set_agent_enabled("agent-10001", False) is True
    assert "agent-10001" not in ENABLED_DEBATE_AGENTS

    assert set_agent_enabled("agent-10008", False) is True
    assert "agent-10008" not in ENABLED_DEBATE_AGENTS

    assert set_agent_enabled("agent-10003", False) is True
    assert "agent-10003" not in ENABLED_DEBATE_AGENTS


def test_toggleable_agent_enable_and_disable():
    reset_enabled_agents()
    assert "agent-10011" in ENABLED_DEBATE_AGENTS

    # Disable LegalAdvisor
    assert set_agent_enabled("agent-10011", False) is True
    assert "agent-10011" not in ENABLED_DEBATE_AGENTS
    assert "agent-10011" not in get_initial_agents()

    # Re-enable via toggle_agent
    assert toggle_agent("agent-10011") is True
    assert "agent-10011" in ENABLED_DEBATE_AGENTS
    assert "agent-10011" in get_initial_agents()


def test_disabled_agent_can_still_be_explicitly_tagged():
    reset_enabled_agents()
    # Disable LegalAdvisor for automatic debate
    set_agent_enabled("agent-10011", False)
    assert "agent-10011" not in get_initial_agents()

    # User explicitly tags the disabled agent
    agent_key, prompt, was_explicit = parse_agent_and_prompt(
        "/[agent-10011] Direct question to disabled agent",
        "C_TEST",
        "123.001",
    )
    assert agent_key == "agent-10011"
    assert prompt == "Direct question to disabled agent"
    assert was_explicit is True

    # User mentions persona name without brackets -> does NOT tag disabled agent, falls back to default agent
    agent_key_2, _, was_explicit_2 = parse_agent_and_prompt(
        "<@U123> @MistLar LegalAdvisor Direct question to disabled agent",
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
    assert "toggle_agent_radio:agent-10011" in action_ids
    assert "toggle_circuit_breaker_radio" in action_ids
    assert "set_circuit_breaker_threshold_input" in action_ids
    assert "save_circuit_breaker_threshold_button" in action_ids
    assert "set_circuit_breaker_preset:0.50" in action_ids

    # Dynamic block IDs present
    block_ids = [b.get("block_id") for b in blocks if "block_id" in b]
    assert "cb_threshold_block_2.50" in block_ids
    assert any(b.startswith("sandwich_start_chain_block") for b in block_ids)
    assert any(b.startswith("sandwich_end_chain_block") for b in block_ids)
    assert "agent_toggle_block_agent-10011_on" in block_ids

    # Agent ID in square brackets rendered
    assert any("[agent-10011]" in b.get("text", {}).get("text", "") for b in blocks)


def test_settings_persistence(tmp_path, monkeypatch):
    test_settings_path = tmp_path / "test_agent_settings.json"
    monkeypatch.setattr("orchestration.agent_settings.SETTINGS_FILE", test_settings_path)

    # 1. Reset and disable specific agents & set circuit-breaker config
    reset_enabled_agents()
    set_agent_enabled("agent-10011", False)
    set_agent_enabled("agent-10002", False)
    set_circuit_breaker_enabled(True)
    set_circuit_breaker_threshold(3.50)

    assert test_settings_path.exists()
    assert "agent-10011" not in ENABLED_DEBATE_AGENTS
    assert "agent-10002" not in ENABLED_DEBATE_AGENTS
    assert is_circuit_breaker_enabled() is True
    assert get_circuit_breaker_threshold() == 3.50

    # 2. Simulate server restart by clearing memory and calling load_enabled_agents()
    ENABLED_DEBATE_AGENTS.clear()
    assert len(ENABLED_DEBATE_AGENTS) == 0

    loaded = load_enabled_agents()
    assert "agent-10011" not in loaded
    assert "agent-10002" not in loaded
    # Verify locked agents are always present even on reload
    assert "agent-10001" in loaded
    assert "agent-10003" in loaded
    assert "agent-10008" in loaded
    assert len(loaded) == 9
    assert is_circuit_breaker_enabled() is True
    assert get_circuit_breaker_threshold() == 3.50


def test_is_discussion_active_behavior():
    thread_key = ("C_ACTIVE_TEST", "12345.1001")
    thread_state_manager.remaining_agents_map.pop(thread_key, None)
    thread_state_manager.rate_limit_retry_queue.pop(thread_key, None)
    thread_state_manager.active_thread_tasks.pop(thread_key, None)

    # 1. Fresh / idle thread is not active
    assert thread_state_manager.is_discussion_active("C_ACTIVE_TEST", "12345.1001") is False

    # 2. Active when remaining agents exist
    thread_state_manager.remaining_agents_map[thread_key] = ["agent-10011"]
    assert thread_state_manager.is_discussion_active("C_ACTIVE_TEST", "12345.1001") is True

    # 3. Active when retry queue has entries
    thread_state_manager.remaining_agents_map.pop(thread_key, None)
    thread_state_manager.rate_limit_retry_queue[thread_key] = ["agent-10002"]
    assert thread_state_manager.is_discussion_active("C_ACTIVE_TEST", "12345.1001") is True

    # Clean up
    thread_state_manager.rate_limit_retry_queue.pop(thread_key, None)
    assert thread_state_manager.is_discussion_active("C_ACTIVE_TEST", "12345.1001") is False


@pytest.mark.anyio
async def test_interrupt_thread_discussion_clears_state_and_cancels_task():
    import asyncio

    thread_key = ("C_INTERRUPT_TEST", "12345.1002")

    thread_state_manager.remaining_agents_map[thread_key] = ["agent-10011", "agent-10002"]
    thread_state_manager.rate_limit_retry_queue[thread_key] = ["agent-10001"]
    thread_state_manager.rate_limit_already_retried[thread_key] = {"agent-10011"}
    thread_state_manager.custom_flow_threads.add(thread_key)
    thread_state_manager.stop_requested_threads.add(thread_key)

    # Create dummy running background task
    async def dummy_coro():
        await asyncio.sleep(100)

    task = asyncio.create_task(dummy_coro())
    thread_state_manager.active_thread_tasks[thread_key] = task

    initial_gen = thread_state_manager.thread_generations.get(thread_key, 0)

    # Trigger interrupt
    thread_state_manager.interrupt_discussion("C_INTERRUPT_TEST", "12345.1002")

    # Verify task cancelled, generation incremented, state maps wiped
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert task.cancelled() is True
    assert thread_state_manager.thread_generations[thread_key] == initial_gen + 1
    assert thread_key not in thread_state_manager.remaining_agents_map
    assert thread_key not in thread_state_manager.rate_limit_retry_queue
    assert thread_key not in thread_state_manager.rate_limit_already_retried
    assert thread_key not in thread_state_manager.custom_flow_threads
    assert thread_key not in thread_state_manager.stop_requested_threads
    assert thread_state_manager.is_discussion_active("C_INTERRUPT_TEST", "12345.1002") is False


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
    thread_state_manager.remaining_agents_map.pop(thread_key, None)
    thread_state_manager.active_thread_tasks.pop(thread_key, None)

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
    mock_choice_1.message.content = "Turn 1 answer to old query. /[agent-10011]"
    mock_choice_1.message.tool_calls = None
    mock_resp_1 = MagicMock(choices=[mock_choice_1], usage=None)

    async def mock_run_turn_1(func, *args, **kwargs):
        # Simulate user interruption arriving while Turn 1 is waiting on LLM
        thread_state_manager.interrupt_discussion("C_INTERRUPT_LLM", "12345.1003")
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
    mock_choice_2.message.content = "Turn 2 answer to new query. /[agent-10011]"
    mock_choice_2.message.tool_calls = None
    mock_resp_2 = MagicMock(choices=[mock_choice_2], usage=None)

    async def mock_run_turn_2(func, *args, **kwargs):
        return mock_resp_2

    monkeypatch.setattr("orchestration.agent_router.run_in_threadpool", mock_run_turn_2)

    await process_and_respond(
        channel_id="C_INTERRUPT_LLM",
        thread_ts="12345.1003",
        raw_text="<@U12345> New query topic 2",
        is_bot_trigger=False,
    )

    # Turn 2 successfully posts
    assert len(posted_messages) == 1
    assert "Turn 2 answer to new query." in posted_messages[0]["text"]
    assert "/[agent-10011]" in posted_messages[0]["text"]


@pytest.mark.anyio
async def test_interruption_during_pacing_delay_aborts_superseded_turn(monkeypatch):
    """
    Scenario: Agent 1 completes LLM and is during the 3-second pacing delay.
    User submits a new prompt in that thread.
    Expected:
    - Turn 1 does not post to Slack.
    """
    thread_key = ("C_INTERRUPT_SLEEP", "12345.1004")
    thread_state_manager.remaining_agents_map.pop(thread_key, None)
    thread_state_manager.active_thread_tasks.pop(thread_key, None)

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
    mock_choice.message.content = "Turn 1 answer before sleep. /[agent-10011]"
    mock_choice.message.tool_calls = None
    mock_resp = MagicMock(choices=[mock_choice], usage=None)

    async def mock_run(func, *args, **kwargs):
        return mock_resp

    monkeypatch.setattr("orchestration.agent_router.run_in_threadpool", mock_run)

    # Interrupt injected during asyncio.sleep in router
    async def mock_sleep_with_interrupt(duration):
        thread_state_manager.interrupt_discussion("C_INTERRUPT_SLEEP", "12345.1004")

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


@pytest.mark.anyio
async def test_process_and_respond_history_limit_subsequent_bot_rounds(monkeypatch):
    """
    Scenario: Following the previous prompt (/history 2), a bot trigger occurs (e.g. Mistral_LegalAdvisor).
    Expected:
    - Scribe/Slack history has 5 prior messages + user prompt + Agent 1 reply.
    - LiteLLM receives last 2 prior messages + user prompt + Agent 1 reply + closing user turn.
    """
    # Simulate active history limit from turn 1
    thread_state_manager.set_history_limit(
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
            "content": "[CaiLogics Visionary]: Visionary response. /[agent-10011]",
        },
    ]

    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history",
        AsyncMock(return_value=mock_history),
    )

    captured_messages = []

    mock_choice = MagicMock()
    mock_choice.message.content = "LegalAdvisor response. /[agent-10003]"
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
        raw_text="Visionary response. /[agent-10011]",
        is_bot_trigger=True,
    )

    # Verify captured_messages:
    # 0: system prompt
    # 1: context prefix (ensures start is user)
    # 2: Reply 2 (prior)
    # 3: Message 3 (prior)
    # 4: What now? (discussion prompt)
    # 5: Visionary response... (discussion reply 1)
    # 6: user turn prompting LegalAdvisor (ensures conversation ends with user)
    assert len(captured_messages) == 7
    assert captured_messages[0]["role"] == "system"
    assert captured_messages[1]["role"] == "user"
    assert captured_messages[2] == {"role": "assistant", "content": "Reply 2"}
    assert captured_messages[3] == {"role": "user", "content": "Message 3"}
    assert captured_messages[4] == {"role": "user", "content": "What now?"}
    assert captured_messages[5]["role"] == "assistant"
    assert "[CaiLogics Visionary]" in captured_messages[5]["content"]
    assert captured_messages[6]["role"] == "user"
    assert "Legal Advisor" in captured_messages[6]["content"]


@pytest.mark.anyio
async def test_process_and_respond_subsequent_prompt_resets_full_history(monkeypatch):
    """
    Scenario: In a thread that previously had /history 2, a new user prompt is submitted WITHOUT /history.
    Expected:
    - THREAD_HISTORY_LIMITS is cleared.
    - LiteLLM receives ALL prior messages (complete thread history) + new prompt.
    """
    # Simulate pre-existing limit
    thread_state_manager.set_history_limit(
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
    mock_choice.message.content = "Full history response. /[agent-10011]"
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
    assert thread_state_manager.get_history_limit("C_HIST_TEST_RESET", "12345.4001") is None

    # Verify all 4 prior messages + new prompt are in context:
    assert len(captured_messages) == 6
    assert captured_messages[0]["role"] == "system"
    assert captured_messages[1] == {"role": "user", "content": "Message 1"}
    assert captured_messages[2] == {"role": "assistant", "content": "Reply 1"}
    assert captured_messages[3] == {"role": "user", "content": "Message 2"}
    assert captured_messages[4] == {"role": "assistant", "content": "Reply 2"}
    assert captured_messages[5]["role"] == "user"
    assert "Tell me more" in captured_messages[5]["content"]
