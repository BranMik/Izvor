from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestration import agent_router
from orchestration.agent_router import process_and_respond
from orchestration.agent_settings import (
    reset_enabled_agents,
    set_fixed_config,
    set_orchestration_mode,
    set_sandwich_config,
)
from orchestration.thread_state import thread_state_manager
from pipeline.directive_parser import directive_parser
from core.registry import get_skill_registry
skill_registry = get_skill_registry()


@pytest.fixture(autouse=True)
def setup_test_environment(monkeypatch):
    reset_enabled_agents()
    set_orchestration_mode("sandwich")
    set_sandwich_config([], [])
    set_fixed_config([])
    thread_state_manager.reset_all()
    monkeypatch.setattr(agent_router.asyncio, "sleep", AsyncMock(return_value=None))


def test_rounds_skill_discovered_in_registry():
    """Verify that the rounds skill is parsed and loaded into the SkillRegistry."""
    skill = skill_registry.get("rounds")
    assert skill is not None
    assert skill.name == "rounds"
    assert skill.scope == "orchestrator"
    triggers = skill.triggers.get("slash", [])
    assert "/rounds" in triggers
    assert "/round" in triggers
    assert "/next_round" in triggers
    assert "/next-round" in triggers


@pytest.mark.anyio
async def test_directive_parser_parses_rounds_int():
    """Verify directive_parser extracts /rounds [x] properly."""
    res = await directive_parser.parse("Compare microservices vs monolith /rounds 3")
    act = next((a for a in res.actions if a.name == "rounds"), None)
    assert act is not None
    assert act.params.get("rounds") == 3
    assert "Compare microservices vs monolith" in res.cleaned_prompt

    # Test /rounds: 2
    res2 = await directive_parser.parse("Debate topic /rounds: 2")
    act2 = next((a for a in res2.actions if a.name == "rounds"), None)
    assert act2 is not None
    assert act2.params.get("rounds") == 2

    # Test /round 4
    res3 = await directive_parser.parse("Discuss /round 4")
    act3 = next((a for a in res3.actions if a.name == "rounds"), None)
    assert act3 is not None
    assert act3.params.get("rounds") == 4


@pytest.mark.anyio
async def test_directive_parser_parses_next_round():
    """Verify directive_parser extracts /next_round defaulting to 1 round."""
    res = await directive_parser.parse("/next_round Let's evaluate costs")
    act = next((a for a in res.actions if a.name == "rounds"), None)
    assert act is not None
    assert act.params.get("rounds") == 1
    assert act.params.get("is_next_round") is True
    assert "Let's evaluate costs" in res.cleaned_prompt

    # Standalone /next_round
    res2 = await directive_parser.parse("/next_round")
    act2 = next((a for a in res2.actions if a.name == "rounds"), None)
    assert act2 is not None
    assert act2.params.get("rounds") == 1
    assert act2.params.get("is_next_round") is True


def test_thread_state_manager_rounds_lifecycle():
    """Test DiscussionStateStore and ThreadStateManager round tracking."""
    channel_id = "C_TEST"
    thread_ts = "1234.5678"

    assert thread_state_manager.get_rounds(channel_id, thread_ts) == 0

    thread_state_manager.set_rounds(channel_id, thread_ts, 3)
    assert thread_state_manager.get_rounds(channel_id, thread_ts) == 3

    dec = thread_state_manager.decrement_rounds(channel_id, thread_ts)
    assert dec == 2
    assert thread_state_manager.get_rounds(channel_id, thread_ts) == 2

    thread_state_manager.set_round_sequence(channel_id, thread_ts, ["agent-10002", "agent-10011"])
    assert thread_state_manager.get_round_sequence(channel_id, thread_ts) == ["agent-10002", "agent-10011"]

    thread_state_manager.clear_rounds(channel_id, thread_ts)
    assert thread_state_manager.get_rounds(channel_id, thread_ts) == 0
    # Sequence is retained for subsequent /next_round calls
    assert thread_state_manager.get_round_sequence(channel_id, thread_ts) == ["agent-10002", "agent-10011"]


@pytest.mark.anyio
async def test_multi_round_execution_end_to_end(monkeypatch):
    """
    Test that /rounds 2 loops through participating agents twice.
    Round 1: Innovator -> LegalAdvisor
    LegalAdvisor finishes Round 1, nominates Innovator for Round 2.
    Round 2: Innovator -> LegalAdvisor
    LegalAdvisor finishes Round 2, terminates discussion cleanly.
    """
    posted_messages = []

    async def mock_post_agent_response(channel_id, thread_ts, text, username, icon_url=None):
        posted_messages.append(text)
        return {"ok": True, "ts": f"12345.{len(posted_messages)}"}

    monkeypatch.setattr("orchestration.agent_router.post_agent_response", mock_post_agent_response)
    monkeypatch.setattr("orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[]))

    # Turn 1: Innovator in Round 1
    mock_choice_1 = MagicMock()
    mock_choice_1.message.content = "Innovator initial idea."
    mock_choice_1.message.tool_calls = None
    mock_resp_1 = MagicMock(choices=[mock_choice_1], usage=None)

    async def mock_run_turn(func, *args, **kwargs):
        return mock_run_turn.current_response

    monkeypatch.setattr("orchestration.agent_router.execute_llm_with_retry", mock_run_turn)

    mock_run_turn.current_response = mock_resp_1
    res_1 = await process_and_respond(
        channel_id="C_ROUNDS",
        thread_ts="100.001",
        raw_text="/[agent-10002] /[agent-10011] Compare frameworks /rounds 2",
        is_bot_trigger=False,
    )
    assert not res_1.get("is_completed")
    assert "/[agent-10011]" in posted_messages[0]

    # Turn 2: LegalAdvisor at end of Round 1
    mock_choice_2 = MagicMock()
    mock_choice_2.message.content = "LegalAdvisor evaluation for round 1."
    mock_choice_2.message.tool_calls = None
    mock_resp_2 = MagicMock(choices=[mock_choice_2], usage=None)
    mock_run_turn.current_response = mock_resp_2

    res_2 = await process_and_respond(
        channel_id="C_ROUNDS",
        thread_ts="100.001",
        raw_text="/[agent-10011] LegalAdvisor evaluation",
        is_bot_trigger=True,
    )
    # Since rounds=2, LegalAdvisor at the end of Round 1 must NOT terminate;
    # it must nominate the first agent Innovator to start Round 2!
    assert not res_2.get("is_completed")
    assert "/[agent-10002]" in posted_messages[1]
    assert thread_state_manager.get_rounds("C_ROUNDS", "100.001") == 1

    # Turn 3: Innovator in Round 2
    mock_choice_3 = MagicMock()
    mock_choice_3.message.content = "Innovator response in round 2."
    mock_choice_3.message.tool_calls = None
    mock_resp_3 = MagicMock(choices=[mock_choice_3], usage=None)
    mock_run_turn.current_response = mock_resp_3

    res_3 = await process_and_respond(
        channel_id="C_ROUNDS",
        thread_ts="100.001",
        raw_text="/[agent-10002] Innovator response",
        is_bot_trigger=True,
    )
    assert not res_3.get("is_completed")
    assert "/[agent-10011]" in posted_messages[2]

    # Turn 4: LegalAdvisor in Round 2 (Final round!)
    mock_choice_4 = MagicMock()
    mock_choice_4.message.content = "LegalAdvisor final conclusion."
    mock_choice_4.message.tool_calls = None
    mock_resp_4 = MagicMock(choices=[mock_choice_4], usage=None)
    mock_run_turn.current_response = mock_resp_4

    res_4 = await process_and_respond(
        channel_id="C_ROUNDS",
        thread_ts="100.001",
        raw_text="/[agent-10011] LegalAdvisor final conclusion",
        is_bot_trigger=True,
    )
    # Final round completes!
    assert res_4.get("is_completed")
    assert "/[agent-10002]" not in posted_messages[3]
    assert "/[agent-10011]" not in posted_messages[3]


@pytest.mark.anyio
async def test_next_round_command_after_discussion_finishes(monkeypatch):
    """
    Test that /next_round executed on an already finished discussion
    starts another round from the first agent to the last agent.
    """
    posted_messages = []

    async def mock_post_agent_response(channel_id, thread_ts, text, username, icon_url=None):
        posted_messages.append(text)
        return {"ok": True, "ts": f"12345.{len(posted_messages)}"}

    monkeypatch.setattr("orchestration.agent_router.post_agent_response", mock_post_agent_response)
    monkeypatch.setattr("orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[]))

    async def mock_run_turn(func, *args, **kwargs):
        return mock_run_turn.current_response

    monkeypatch.setattr("orchestration.agent_router.execute_llm_with_retry", mock_run_turn)

    # Initial 1-round discussion with 2 agents
    mock_choice_1 = MagicMock()
    mock_choice_1.message.content = "Idea 1"
    mock_choice_1.message.tool_calls = None
    mock_run_turn.current_response = MagicMock(choices=[mock_choice_1], usage=None)

    await process_and_respond(
        channel_id="C_NEXT",
        thread_ts="200.001",
        raw_text="/[agent-10002] /[agent-10011] Let's discuss",
        is_bot_trigger=False,
    )

    # Second agent finishes round 1
    mock_choice_2 = MagicMock()
    mock_choice_2.message.content = "Conclusion 1"
    mock_choice_2.message.tool_calls = None
    mock_run_turn.current_response = MagicMock(choices=[mock_choice_2], usage=None)

    res_finish = await process_and_respond(
        channel_id="C_NEXT",
        thread_ts="200.001",
        raw_text="/[agent-10011] Conclusion 1",
        is_bot_trigger=True,
    )
    assert res_finish.get("is_completed")

    # Now user sends /next_round into the completed thread!
    mock_choice_3 = MagicMock()
    mock_choice_3.message.content = "Idea 2 for next round"
    mock_choice_3.message.tool_calls = None
    mock_run_turn.current_response = MagicMock(choices=[mock_choice_3], usage=None)

    res_next_1 = await process_and_respond(
        channel_id="C_NEXT",
        thread_ts="200.001",
        raw_text="/next_round Please also consider scalability",
        is_bot_trigger=False,
    )
    # Innovator (first agent) must have answered and nominated LegalAdvisor!
    assert res_next_1.get("agent_key") == "agent-10002"
    assert not res_next_1.get("is_completed")
    assert "/[agent-10011]" in posted_messages[2]

    # LegalAdvisor answers and completes the next round!
    mock_choice_4 = MagicMock()
    mock_choice_4.message.content = "LegalAdvisor scalability conclusion"
    mock_choice_4.message.tool_calls = None
    mock_run_turn.current_response = MagicMock(choices=[mock_choice_4], usage=None)

    res_next_2 = await process_and_respond(
        channel_id="C_NEXT",
        thread_ts="200.001",
        raw_text="[agent-10011] LegalAdvisor scalability",
        is_bot_trigger=True,
    )
    assert res_next_2.get("agent_key") == "agent-10011"
    assert res_next_2.get("is_completed")


@pytest.mark.anyio
async def test_stop_command_cancels_multi_round_debate(monkeypatch):
    """Verify that /stop request terminates an active multi-round debate."""
    channel_id = "C_STOP"
    thread_ts = "300.001"

    thread_state_manager.set_rounds(channel_id, thread_ts, 3)
    thread_state_manager.set_round_sequence(channel_id, thread_ts, ["agent-10002", "agent-10011"])

    thread_state_manager.request_stop(channel_id, thread_ts)

    assert thread_state_manager.is_stopped(channel_id, thread_ts)
    assert thread_state_manager.get_rounds(channel_id, thread_ts) == 0
