import json
from pathlib import Path
import tempfile
import pytest
from unittest.mock import AsyncMock, MagicMock

from core.config import AGENTS_CONFIG
from nlp.message_builder import build_llm_messages
from orchestration.agent_settings import AgentSettingsManager, settings_manager
from orchestration.debate_orchestrator import DebateOrchestrator, enforce_valid_nomination
from orchestration.agent_router import (
    process_and_respond,
    REMAINING_AGENTS_MAP,
    RATE_LIMIT_RETRY_QUEUE,
    RATE_LIMIT_ALREADY_RETRIED,
    CUSTOM_FLOW_THREADS,
    STOP_REQUESTED_THREADS,
    get_orchestration_mode,
    set_orchestration_mode,
    get_sandwich_config,
    set_sandwich_config,
    get_fixed_config,
    set_fixed_config,
    get_initial_agents,
    reset_enabled_agents,
    set_agent_enabled,
)
from services.slack_client import build_app_home_view
import orchestration.agent_router as agent_router


@pytest.fixture(autouse=True)
def setup_test_chains(monkeypatch):
    reset_enabled_agents()
    set_orchestration_mode("sandwich")
    set_sandwich_config([], [])
    set_fixed_config([])
    REMAINING_AGENTS_MAP.clear()
    RATE_LIMIT_RETRY_QUEUE.clear()
    RATE_LIMIT_ALREADY_RETRIED.clear()
    CUSTOM_FLOW_THREADS.clear()
    STOP_REQUESTED_THREADS.clear()
    monkeypatch.setattr(agent_router.asyncio, "sleep", AsyncMock(return_value=None))
    yield
    reset_enabled_agents()
    set_orchestration_mode("sandwich")
    set_sandwich_config([], [])
    set_fixed_config([])
    REMAINING_AGENTS_MAP.clear()
    RATE_LIMIT_RETRY_QUEUE.clear()
    RATE_LIMIT_ALREADY_RETRIED.clear()
    CUSTOM_FLOW_THREADS.clear()
    STOP_REQUESTED_THREADS.clear()


def test_sandwich_mode_ordering_and_pool():
    set_orchestration_mode("sandwich")
    set_sandwich_config(
        start_chain=["Gemini_Visionary", "MistLar_TechLead"],
        end_chain=["OpAiTerra_Architect"],
    )

    orch = DebateOrchestrator()
    agents_flow = orch.get_initial_agents()

    # Start chain is at the beginning
    assert agents_flow[0] == "Gemini_Visionary"
    assert agents_flow[1] == "MistLar_TechLead"

    # End chain is at the end
    assert agents_flow[-1] == "OpAiTerra_Architect"

    # Total count includes start chain, middle pool, end chain
    assert len(agents_flow) == len(AGENTS_CONFIG)
    assert set(agents_flow) == set(AGENTS_CONFIG.keys())


def test_sandwich_mode_empty_chains_is_arbitrary_pool():
    set_orchestration_mode("sandwich")
    set_sandwich_config(start_chain=[], end_chain=[])

    orch = DebateOrchestrator()
    agents_flow = orch.get_initial_agents()

    assert len(agents_flow) == len(AGENTS_CONFIG)
    assert set(agents_flow) == set(AGENTS_CONFIG.keys())


def test_sandwich_mode_reentrance():
    # OpAiTerra_Architect placed in both start_chain and end_chain
    set_orchestration_mode("sandwich")
    set_sandwich_config(
        start_chain=["OpAiTerra_Architect", "Gemini_Visionary"],
        end_chain=["OpAiTerra_Architect"],
    )

    orch = DebateOrchestrator()
    agents_flow = orch.get_initial_agents()

    assert agents_flow[0] == "OpAiTerra_Architect"
    assert agents_flow[1] == "Gemini_Visionary"
    assert agents_flow[-1] == "OpAiTerra_Architect"
    assert agents_flow.count("OpAiTerra_Architect") == 2


def test_fixed_mode_strict_sequence():
    set_orchestration_mode("fixed")
    set_fixed_config(["MistLar_TechLead", "AnthrOpus_Innovator", "OpAiTerra_Architect"])

    orch = DebateOrchestrator()
    agents_flow = orch.get_initial_agents()

    # Exactly 3 agents in exact sequence
    assert agents_flow == ["MistLar_TechLead", "AnthrOpus_Innovator", "OpAiTerra_Architect"]


def test_fixed_mode_empty_chain_falls_back_to_all_enabled():
    set_orchestration_mode("fixed")
    set_fixed_config([])

    orch = DebateOrchestrator()
    agents_flow = orch.get_initial_agents()

    assert len(agents_flow) == len(AGENTS_CONFIG)


def test_deterministic_directive_in_message_builder():
    agent_cfg = AGENTS_CONFIG["Gemini_Visionary"]

    # 1. When next_designated_agent is specified
    msgs = build_llm_messages(
        system_prompt=agent_cfg["system_prompt"],
        effective_history=[],
        prompt="Discuss feature scope",
        is_bot_trigger=False,
        agent_key="Gemini_Visionary",
        agent_cfg=agent_cfg,
        next_designated_agent="AnthrOpus_Innovator",
    )
    last_content = msgs[-1]["content"]
    assert "[AnthrOpus_Innovator]" in last_content
    assert "nominate [AnthrOpus_Innovator] as the next agent to speak" in last_content

    # 2. When force_termination is True
    msgs_term = build_llm_messages(
        system_prompt=agent_cfg["system_prompt"],
        effective_history=[],
        prompt="Discuss feature scope",
        is_bot_trigger=False,
        agent_key="OpAiTerra_Architect",
        agent_cfg=agent_cfg,
        force_termination=True,
    )
    last_content_term = msgs_term[-1]["content"]
    assert "CRITICAL CONSTRAINT: You are the last agent requested to speak" in last_content_term
    assert "Do NOT nominate any other agent" in last_content_term


def test_enforce_valid_nomination_with_designated_next_agent():
    # LLM nominated Gemini_Visionary instead of designated next agent
    corrected = enforce_valid_nomination(
        text="Here is my technical assessment. [Gemini_Visionary]",
        current_agent_key="MistLar_TechLead",
        remaining=["AnthrOpus_Innovator"],
        force_termination=False,
        designated_next_agent="AnthrOpus_Innovator",
    )
    assert "[AnthrOpus_Innovator]" in corrected
    assert "[Gemini_Visionary]" not in corrected


@pytest.mark.anyio
async def test_fixed_mode_end_to_end_discussion(monkeypatch):
    set_orchestration_mode("fixed")
    set_fixed_config(["MistLar_TechLead", "AnthrOpus_Innovator", "OpAiTerra_Architect"])

    posted_messages = []
    async def mock_post_agent_response(channel_id, thread_ts, text, username, icon_url=None):
        posted_messages.append({"username": username, "text": text})
        return {"ok": True, "ts": f"99999.{len(posted_messages)}"}

    monkeypatch.setattr("orchestration.agent_router.post_agent_response", mock_post_agent_response)
    monkeypatch.setattr("orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[]))
    monkeypatch.setattr("orchestration.agent_router.record_thread_tokens", MagicMock())

    thread_key = ("C_CHAIN", "99999.0001")

    # Turn 1: MistLar_TechLead starts
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
    assert posted_messages[0]["username"] == "MistLar TechLead"
    # Deterministic nomination enforced to next agent in chain
    assert "[AnthrOpus_Innovator]" in posted_messages[0]["text"]
    assert REMAINING_AGENTS_MAP[thread_key] == ["AnthrOpus_Innovator", "OpAiTerra_Architect"]

    # Turn 2: AnthrOpus_Innovator
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
        raw_text="[AnthrOpus_Innovator] Your turn",
        is_bot_trigger=True,
    )

    assert res_2.get("ok") is True
    assert len(posted_messages) == 2
    assert posted_messages[1]["username"] == "AnthrOpus Innovator"
    assert "[OpAiTerra_Architect]" in posted_messages[1]["text"]
    assert REMAINING_AGENTS_MAP[thread_key] == ["OpAiTerra_Architect"]

    # Turn 3: OpAiTerra_Architect (final in chain)
    mock_choice_3 = MagicMock()
    mock_choice_3.message.content = "Architect final verdict."
    mock_choice_3.message.tool_calls = None
    mock_resp_3 = MagicMock(choices=[mock_choice_3], usage=None)

    async def mock_run_3(func, *args, **kwargs):
        return mock_resp_3
    monkeypatch.setattr("orchestration.agent_router.run_in_threadpool", mock_run_3)

    res_3 = await process_and_respond(
        channel_id="C_CHAIN",
        thread_ts="99999.0001",
        raw_text="[OpAiTerra_Architect] Wrap up",
        is_bot_trigger=True,
    )

    assert res_3.get("ok") is True
    assert len(posted_messages) == 3
    assert posted_messages[2]["username"] == "OpAiTerra Architect"
    # Thread finished -> state cleaned up, tags stripped
    assert thread_key not in REMAINING_AGENTS_MAP
    assert "[" not in posted_messages[2]["text"].split("\n\n[")[0]


def test_app_home_view_displays_orchestration_modes():
    set_orchestration_mode("sandwich")
    set_sandwich_config(start_chain=["Gemini_Visionary"], end_chain=["OpAiTerra_Architect"])

    view = build_app_home_view()
    blocks = view["blocks"]

    # Verify mode radio buttons
    mode_block = next((b for b in blocks if b.get("accessory", {}).get("action_id") == "select_orchestration_mode"), None)
    assert mode_block is not None
    assert mode_block["accessory"]["initial_option"]["value"] == "mode:sandwich"

    # Switch to fixed mode and verify fixed view
    set_orchestration_mode("fixed")
    set_fixed_config(["MistLar_TechLead", "OpAiTerra_Architect"])

    view_fixed = build_app_home_view()
    blocks_fixed = view_fixed["blocks"]
    mode_block_fixed = next((b for b in blocks_fixed if b.get("accessory", {}).get("action_id") == "select_orchestration_mode"), None)
    assert mode_block_fixed is not None
    assert mode_block_fixed["accessory"]["initial_option"]["value"] == "mode:fixed"

    fixed_input = next((b for b in blocks_fixed if b.get("block_id") == "fixed_chain_block"), None)
    assert fixed_input is not None
    assert "MistLar_TechLead, OpAiTerra_Architect" in fixed_input["element"]["initial_value"]
