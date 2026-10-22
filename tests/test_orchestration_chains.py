from unittest.mock import AsyncMock, MagicMock

import pytest

from core.config import AGENTS_CONFIG
from nlp.message_builder import build_llm_messages
from orchestration import agent_router
from orchestration.agent_router import process_and_respond
from orchestration.agent_settings import (
    reset_enabled_agents,
    set_agent_enabled,
    set_fixed_config,
    set_orchestration_mode,
    set_sandwich_config,
)
from orchestration.nomination import enforce_valid_nomination, get_initial_agents
from orchestration.thread_state import thread_state_manager


@pytest.fixture(autouse=True)
def setup_test_chains(monkeypatch):
    reset_enabled_agents()
    set_orchestration_mode("sandwich")
    set_sandwich_config([], [])
    set_fixed_config([])
    thread_state_manager.reset_all()
    monkeypatch.setattr(agent_router.asyncio, "sleep", AsyncMock(return_value=None))
    yield
    reset_enabled_agents()
    set_orchestration_mode("sandwich")
    set_sandwich_config([], [])
    set_fixed_config([])
    thread_state_manager.reset_all()


def test_sandwich_mode_ordering_and_pool():
    set_orchestration_mode("sandwich")
    set_sandwich_config(
        start_chain=["agent-10001", "agent-10011"],
        end_chain=["agent-10008"],
    )

    agents_flow = get_initial_agents()

    # Start chain is at the beginning
    assert agents_flow[0] == "agent-10001"
    assert agents_flow[1] == "agent-10011"

    # End chain is at the end
    assert agents_flow[-1] == "agent-10008"

    # Total count includes start chain, middle pool, end chain
    assert len(agents_flow) == len(AGENTS_CONFIG)
    assert set(agents_flow) == set(AGENTS_CONFIG.keys())


def test_sandwich_mode_empty_chains_is_arbitrary_pool():
    set_orchestration_mode("sandwich")
    set_sandwich_config(start_chain=[], end_chain=[])

    agents_flow = get_initial_agents()

    assert len(agents_flow) == len(AGENTS_CONFIG)
    assert set(agents_flow) == set(AGENTS_CONFIG.keys())


def test_sandwich_mode_reentrance():
    # SecurityArchitect placed in both start_chain and end_chain
    set_orchestration_mode("sandwich")
    set_sandwich_config(
        start_chain=["agent-10008", "agent-10001"],
        end_chain=["agent-10008"],
    )

    agents_flow = get_initial_agents()

    assert agents_flow[0] == "agent-10008"
    assert agents_flow[1] == "agent-10001"
    assert agents_flow[-1] == "agent-10008"
    assert agents_flow.count("agent-10008") == 2


def test_fixed_mode_strict_sequence():
    set_orchestration_mode("fixed")
    set_fixed_config(["agent-10011", "agent-10002", "agent-10008"])

    agents_flow = get_initial_agents()

    # Exactly 3 agents in exact sequence
    assert agents_flow == ["agent-10011", "agent-10002", "agent-10008"]


def test_fixed_mode_empty_chain_falls_back_to_all_enabled():
    set_orchestration_mode("fixed")
    set_fixed_config([])

    agents_flow = get_initial_agents()

    assert len(agents_flow) == len(AGENTS_CONFIG)


def test_deterministic_directive_in_message_builder():
    agent_cfg = AGENTS_CONFIG["agent-10001"]

    # 1. When next_designated_agent is specified
    msgs = build_llm_messages(
        system_prompt=agent_cfg["system_prompt"],
        effective_history=[],
        prompt="Discuss feature scope",
        is_bot_trigger=False,
        agent_key="agent-10001",
        agent_cfg=agent_cfg,
        next_designated_agent="agent-10002",
    )
    last_content = msgs[-1]["content"]
    assert "[agent-10002]" in last_content
    assert "nominate /[agent-10002] as the next agent to speak" in last_content

    # 2. When force_termination is True
    msgs_term = build_llm_messages(
        system_prompt=agent_cfg["system_prompt"],
        effective_history=[],
        prompt="Discuss feature scope",
        is_bot_trigger=False,
        agent_key="agent-10008",
        agent_cfg=agent_cfg,
        force_termination=True,
    )
    last_content_term = msgs_term[-1]["content"]
    assert "CRITICAL CONSTRAINT: You are the last agent requested to speak" in last_content_term
    assert "Do NOT nominate any other agent" in last_content_term


def test_enforce_valid_nomination_with_designated_next_agent():
    # LLM nominated Visionary instead of designated next agent
    corrected = enforce_valid_nomination(
        text="Here is my technical assessment. /[agent-10001]",
        current_agent_key="agent-10011",
        remaining=["agent-10002"],
        force_termination=False,
        designated_next_agent="agent-10002",
    )
    assert "/[agent-10002]" in corrected
    assert "/[agent-10001]" not in corrected


@pytest.mark.anyio
async def test_fixed_mode_end_to_end_discussion(monkeypatch):
    set_orchestration_mode("fixed")
    set_fixed_config(["agent-10011", "agent-10002", "agent-10008"])

    posted_messages = []
    async def mock_post_agent_response(channel_id, thread_ts, text, username, icon_url=None):
        posted_messages.append({"username": username, "text": text})
        return {"ok": True, "ts": f"99999.{len(posted_messages)}"}

    monkeypatch.setattr("orchestration.agent_router.post_agent_response", mock_post_agent_response)
    monkeypatch.setattr("orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[]))
    monkeypatch.setattr("orchestration.agent_router.record_thread_tokens", MagicMock())

    thread_key = ("C_CHAIN", "99999.0001")

    # Turn 1: LegalAdvisor starts
    mock_choice_1 = MagicMock()
    mock_choice_1.message.content = "Tech lead evaluation."
    mock_choice_1.message.tool_calls = None
    mock_resp_1 = MagicMock(choices=[mock_choice_1], usage=None)

    async def mock_run_1(func, *args, **kwargs):
        return mock_resp_1
    monkeypatch.setattr("orchestration.agent_router.run_in_threadpool", mock_run_1)

    res_1 = await process_and_respond(
        channel_id="C_CHAIN",
        thread_ts="99999.0001",
        raw_text="Let's build a new search engine",
        is_bot_trigger=False,
    )

    assert res_1.get("ok") is True
    assert len(posted_messages) == 1
    assert posted_messages[0]["username"] == "LegalAdvisor"
    # Deterministic nomination enforced to next agent in chain
    assert "/[agent-10002]" in posted_messages[0]["text"]
    assert thread_state_manager.remaining_agents_map[thread_key] == ["agent-10002", "agent-10008"]

    # Turn 2: Innovator
    mock_choice_2 = MagicMock()
    mock_choice_2.message.content = "Innovator vision."
    mock_choice_2.message.tool_calls = None
    mock_resp_2 = MagicMock(choices=[mock_choice_2], usage=None)

    async def mock_run_2(func, *args, **kwargs):
        return mock_resp_2
    monkeypatch.setattr("orchestration.agent_router.run_in_threadpool", mock_run_2)

    res_2 = await process_and_respond(
        channel_id="C_CHAIN",
        thread_ts="99999.0001",
        raw_text="/[agent-10002] Your turn",
        is_bot_trigger=True,
    )

    assert res_2.get("ok") is True
    assert len(posted_messages) == 2
    assert posted_messages[1]["username"] == "Innovator"
    assert "/[agent-10008]" in posted_messages[1]["text"]
    assert thread_state_manager.remaining_agents_map[thread_key] == ["agent-10008"]

    # Turn 3: SecurityArchitect (final in chain)
    mock_choice_3 = MagicMock()
    mock_choice_3.message.content = "SecurityArchitect final verdict."
    mock_choice_3.message.tool_calls = None
    mock_resp_3 = MagicMock(choices=[mock_choice_3], usage=None)

    async def mock_run_3(func, *args, **kwargs):
        return mock_resp_3
    monkeypatch.setattr("orchestration.agent_router.run_in_threadpool", mock_run_3)

    res_3 = await process_and_respond(
        channel_id="C_CHAIN",
        thread_ts="99999.0001",
        raw_text="/[agent-10008] Wrap up",
        is_bot_trigger=True,
    )

    assert res_3.get("ok") is True
    assert len(posted_messages) == 3
    assert posted_messages[2]["username"] == "SecurityArchitect"
    # Thread finished -> state cleaned up, tags stripped
    assert thread_key not in thread_state_manager.remaining_agents_map
    assert "[" not in posted_messages[2]["text"].split("\n\n[")[0]


@pytest.mark.anyio
async def test_sandwich_mode_end_to_end_discussion(monkeypatch):
    """
    Tests full Sandwich Mode execution with re-entrance:
    start_chain: [SecurityArchitect, Visionary]
    middle_pool: [agent-10011]
    end_chain: [agent-10008]
    Verifies that:
    1. SecurityArchitect opens the discussion and nominates Visionary.
    2. Visionary attempts to nominate SecurityArchitect (from end_chain), but the router corrects
       the nomination to LegalAdvisor (the middle pool agent).
    3. LegalAdvisor (last middle pool agent) hands off to SecurityArchitect (end_chain).
    4. SecurityArchitect closes the discussion with force_termination (does not nominate anyone).
    """
    set_orchestration_mode("sandwich")
    set_sandwich_config(
        start_chain=["agent-10008", "agent-10001"],
        end_chain=["agent-10008"],
    )
    # Enable only these 3 agents so middle_pool has exactly LegalAdvisor
    for a in AGENTS_CONFIG:
        set_agent_enabled(a, a in ("agent-10008", "agent-10001", "agent-10011"))

    posted_messages = []
    async def mock_post_agent_response(channel_id, thread_ts, text, username, icon_url=None):
        posted_messages.append({"username": username, "text": text})
        return {"ok": True, "ts": f"88888.{len(posted_messages)}"}

    monkeypatch.setattr("orchestration.agent_router.post_agent_response", mock_post_agent_response)
    monkeypatch.setattr("orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[]))
    monkeypatch.setattr("orchestration.agent_router.record_thread_tokens", MagicMock())

    thread_key = ("C_SANDWICH", "88888.0001")

    # Turn 1: SecurityArchitect opens discussion
    mock_choice_1 = MagicMock()
    mock_choice_1.message.content = "SecurityArchitecture proposal. /[agent-10001]"
    mock_choice_1.message.tool_calls = None
    mock_resp_1 = MagicMock(choices=[mock_choice_1], usage=None)

    async def mock_run_1(func, *args, **kwargs):
        return mock_resp_1
    monkeypatch.setattr("orchestration.agent_router.run_in_threadpool", mock_run_1)

    res_1 = await process_and_respond(
        channel_id="C_SANDWICH",
        thread_ts="88888.0001",
        raw_text="Let's build a new distributed system",
        is_bot_trigger=False,
    )

    assert res_1.get("ok") is True
    assert len(posted_messages) == 1
    assert posted_messages[0]["username"] == "SecurityArchitect"
    assert "/[agent-10001]" in posted_messages[0]["text"]
    assert thread_state_manager.remaining_agents_map[thread_key] == ["agent-10001", "agent-10011", "agent-10008"]

    # Turn 2: Visionary speaks (last of start_chain).
    # Visionary cites [agent-10008] in text and tries to nominate /[agent-10008],
    # but LegalAdvisor is in middle_pool so SecurityArchitect (end_chain) must NOT be nominated!
    mock_choice_2 = MagicMock()
    mock_choice_2.message.content = "I agree with [agent-10008]. Let's wrap up with /[agent-10008]"
    mock_choice_2.message.tool_calls = None
    mock_resp_2 = MagicMock(choices=[mock_choice_2], usage=None)

    async def mock_run_2(func, *args, **kwargs):
        return mock_resp_2
    monkeypatch.setattr("orchestration.agent_router.run_in_threadpool", mock_run_2)

    res_2 = await process_and_respond(
        channel_id="C_SANDWICH",
        thread_ts="88888.0001",
        raw_text="/[agent-10001] Your turn",
        is_bot_trigger=True,
    )

    assert res_2.get("ok") is True
    assert len(posted_messages) == 2
    assert posted_messages[1]["username"] == "Visionary"
    # Must have corrected nomination to LegalAdvisor (middle pool agent)
    assert "/[agent-10011]" in posted_messages[1]["text"]
    assert thread_state_manager.remaining_agents_map[thread_key] == ["agent-10011", "agent-10008"]

    # Turn 3: LegalAdvisor speaks (middle pool).
    # LegalAdvisor is the last of middle pool, so designated next agent is SecurityArchitect (end_chain).
    mock_choice_3 = MagicMock()
    mock_choice_3.message.content = "Technical implementation details."
    mock_choice_3.message.tool_calls = None
    mock_resp_3 = MagicMock(choices=[mock_choice_3], usage=None)

    async def mock_run_3(func, *args, **kwargs):
        return mock_resp_3
    monkeypatch.setattr("orchestration.agent_router.run_in_threadpool", mock_run_3)

    res_3 = await process_and_respond(
        channel_id="C_SANDWICH",
        thread_ts="88888.0001",
        raw_text="/[agent-10011] Your assessment",
        is_bot_trigger=True,
    )

    assert res_3.get("ok") is True
    assert len(posted_messages) == 3
    assert posted_messages[2]["username"] == "LegalAdvisor"
    assert "/[agent-10008]" in posted_messages[2]["text"]
    assert thread_state_manager.remaining_agents_map[thread_key] == ["agent-10008"]

    # Turn 4: SecurityArchitect speaks (end_chain).
    # This is the final agent; must force termination and NOT nominate anyone.
    mock_choice_4 = MagicMock()
    mock_choice_4.message.content = "Final architecture synthesis."
    mock_choice_4.message.tool_calls = None
    mock_resp_4 = MagicMock(choices=[mock_choice_4], usage=None)

    async def mock_run_4(func, *args, **kwargs):
        return mock_resp_4
    monkeypatch.setattr("orchestration.agent_router.run_in_threadpool", mock_run_4)

    res_4 = await process_and_respond(
        channel_id="C_SANDWICH",
        thread_ts="88888.0001",
        raw_text="/[agent-10008] Please conclude",
        is_bot_trigger=True,
    )

    assert res_4.get("ok") is True
    assert len(posted_messages) == 4
    assert posted_messages[3]["username"] == "SecurityArchitect"
    # Thread finished -> state cleaned up, tags stripped
    assert thread_key not in thread_state_manager.remaining_agents_map
    assert "[" not in posted_messages[3]["text"].split("\n\n[")[0]


