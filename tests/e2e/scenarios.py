"""
E2E Behavioural Test Scenarios.

Defines 38 comprehensive test scenarios (`TC-01`..`TC-38`) matching
`tests/e2e/SLACK_BEHAVIOURAL_TEST_CASES.md`, supporting both live-LLM execution
(default) and offline deterministic simulation (`--mock`).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from core.registry import get_skill_registry
from orchestration.agent_router import (
    configure_router_deps,
    reset_router_deps,
)
from config_store.agent_settings import settings_manager
from config_store.app_settings import (
    is_convergence_fast_forward_enabled,
    is_filter_unauthorized_skill_agents_enabled,
    set_circuit_breaker_enabled,
    set_circuit_breaker_threshold,
    set_convergence_fast_forward_enabled,
    set_filter_unauthorized_skill_agents_enabled,
)
from orchestration.protocols import PipelineDeps
from orchestration.revisor_service import run_internal_revisor
from config_store.system_settings import system_settings_manager
from orchestration.thread_state import thread_state_manager
from orchestration.turn_pipeline import TurnContext
from prompts.builder import build_agent_prompt
from services.audit_logger import audit_logger, get_audit_trail
from services.local_tools import get_agents_resources_dir
from services.mcp.client_manager import get_mcp_client_manager
from services.slack import post_agent_response
from services.web_tools import configure_web_search_fn, reset_web_search_fn
from skills.registry import SkillDefinition
from tests.e2e.protocol_client import (
    TEST_ADMIN_USER_ID,
    TEST_USER_ID,
    SlackProtocolClient,
)


@dataclass
class ScenarioResult:
    id: str
    name: str
    passed: bool
    details: str
    duration_seconds: float = 0.0
    capability: str = ""


@dataclass
class Scenario:
    id: str
    name: str
    category: str
    run_fn: Callable[[SlackProtocolClient], Coroutine[Any, Any, ScenarioResult]]
    capability: str = ""


def _resolve_thread_ts(client: SlackProtocolClient, thread_ts: str | None) -> str:
    if client.current_thread_ts:
        return client.current_thread_ts
    if thread_ts:
        return thread_ts
    if client.bus.posted_messages:
        return str(client.bus.posted_messages[0].get("thread_ts", ""))
    if client.bus.system_messages:
        return str(client.bus.system_messages[-1].get("thread_ts", ""))
    return ""


# --- Scenario Implementation Functions ---


async def tc_01_single_persona(client: SlackProtocolClient) -> ScenarioResult:
    channel = client.get_channel("C_TEST_01")
    thread_ts = client.get_thread_ts()
    res = await client.send_mention(
        "/[Visionary] What is the outlook for AI-native agent operating systems?",
        channel_id=channel,
        thread_ts=thread_ts,
    )
    assert res.status_code == 200

    actual_thread = _resolve_thread_ts(client, thread_ts)
    msgs = client.bus.get_messages_for_thread(channel, actual_thread)
    if not msgs:
        return ScenarioResult(
            "TC-01", "Single Persona Invocation", False, "No agent response posted."
        )

    first = msgs[0]
    has_visionary = first.get("username") == "Visionary"
    has_footer = "{" in first.get("text", "") and "}" in first.get("text", "")
    passed = has_visionary and has_footer and len(msgs) == 1
    return ScenarioResult(
        "TC-01",
        "Single Persona Invocation",
        passed,
        f"Posted by {first.get('username')}, msgs={len(msgs)}",
    )


async def tc_02_multi_persona(client: SlackProtocolClient) -> ScenarioResult:
    channel = client.get_channel("C_TEST_02")
    thread_ts = client.get_thread_ts()
    prompt = "/[Security Architect] /[LegalAdvisor] How should we migrate to event-driven architecture?"
    await client.send_mention(prompt, channel_id=channel, thread_ts=thread_ts)

    actual_thread = _resolve_thread_ts(client, thread_ts)
    msgs = client.bus.get_messages_for_thread(channel, actual_thread)
    speakers = [m.get("username") for m in msgs]
    passed = (
        any(s in ("SecurityArchitect", "Security Architect") for s in speakers)
        and any(s in ("LegalAdvisor", "Legal Advisor") for s in speakers)
        and len(speakers) >= 2
    )
    return ScenarioResult(
        "TC-02", "Multi-Persona Handoff", passed, f"Speakers: {speakers}"
    )


async def tc_03_autonomous_nomination(client: SlackProtocolClient) -> ScenarioResult:
    channel = client.get_channel("C_TEST_03")
    thread_ts = client.get_thread_ts()
    prompt = "/first [Innovator] Should we replace our relational schema with a vector-native graph DB?"
    await client.send_mention(prompt, channel_id=channel, thread_ts=thread_ts)

    actual_thread = _resolve_thread_ts(client, thread_ts)
    msgs = client.bus.get_messages_for_thread(channel, actual_thread)
    speakers = [m.get("username") for m in msgs]
    passed = len(speakers) >= 2 and speakers[0] == "Innovator"
    return ScenarioResult(
        "TC-03",
        "Autonomous Peer Nomination",
        passed,
        f"Debate chained across: {speakers}",
    )


async def tc_04_max_words(client: SlackProtocolClient) -> ScenarioResult:
    channel = client.get_channel("C_TEST_04")
    thread_ts = client.get_thread_ts()
    prompt = "/[LegalAdvisor] /max_words 40 Explain Docker multi-stage builds."
    await client.send_mention(prompt, channel_id=channel, thread_ts=thread_ts)

    actual_thread = _resolve_thread_ts(client, thread_ts)
    msgs = client.bus.get_messages_for_thread(channel, actual_thread)
    if not msgs:
        return ScenarioResult("TC-04", "Maximum Word Count", False, "No response.")

    text = msgs[0].get("text", "")
    substantive_text = text.split("\n\n{")[0] if "\n\n{" in text else text
    word_count = len(substantive_text.split())
    limit = thread_state_manager.get_max_words(channel, actual_thread)
    grace = system_settings_manager.get_word_limit_grace_buffer()
    passed = limit == 40 and word_count <= (40 + grace)
    return ScenarioResult(
        "TC-04", "Maximum Word Count", passed, f"Words: {word_count} (limit: {limit})"
    )


async def tc_05_min_words(client: SlackProtocolClient) -> ScenarioResult:
    channel = client.get_channel("C_TEST_05")
    thread_ts = client.get_thread_ts()
    prompt = "/[Security Architect] /min_words 60 Detail webhook security threat modeling."
    await client.send_mention(prompt, channel_id=channel, thread_ts=thread_ts)

    actual_thread = _resolve_thread_ts(client, thread_ts)
    msgs = client.bus.get_messages_for_thread(channel, actual_thread)
    if not msgs:
        return ScenarioResult(
            "TC-05", "Minimum Word Count Floor", False, "No response."
        )

    text = msgs[0].get("text", "")
    substantive_text = text.split("\n\n{")[0] if "\n\n{" in text else text
    word_count = len(substantive_text.split())
    limit = thread_state_manager.get_min_words(channel, actual_thread)
    passed = limit == 60 and word_count >= 50
    return ScenarioResult(
        "TC-05",
        "Minimum Word Count Floor",
        passed,
        f"Words: {word_count} (floor: {limit})",
    )


async def tc_06_scribe_max_words(client: SlackProtocolClient) -> ScenarioResult:
    channel = client.get_channel("C_TEST_06")
    thread_ts = client.get_thread_ts()
    prompt = "/scribe_max_words 40 /[Security Architect] Evaluate distributed transaction consistency."
    await client.send_mention(prompt, channel_id=channel, thread_ts=thread_ts)

    actual_thread = _resolve_thread_ts(client, thread_ts)
    limit = thread_state_manager.get_scribe_max_words(channel, actual_thread)

    note_text = await client.wait_for_scribe_note(channel, actual_thread, timeout=15.0)
    substantive_text = ""
    if note_text:
        substantive_text = (
            note_text.split("\n\n_Last updated:")[0]
            if "\n\n_Last updated:" in note_text
            else note_text
        )
    word_count = len(substantive_text.split()) if substantive_text else 0

    has_scribe_note = bool(note_text) or any(
        m.get("is_scribe_note") or m.get("username") == "Live Discussion Scribe"
        for m in client.bus.posted_messages
    )
    passed = limit == 40 and has_scribe_note and word_count <= 50
    return ScenarioResult(
        "TC-06",
        "Scribe Maximum Word Count",
        passed,
        f"Scribe note generated ({word_count} words, limit: {limit}). Note in thread: {has_scribe_note}",
    )


async def tc_07_scribe_min_words(client: SlackProtocolClient) -> ScenarioResult:
    channel = client.get_channel("C_TEST_07")
    thread_ts = client.get_thread_ts()
    prompt = "/scribe_min_words 30 /[Security Architect] Analyze event sourcing replay guarantees."
    await client.send_mention(prompt, channel_id=channel, thread_ts=thread_ts)

    actual_thread = _resolve_thread_ts(client, thread_ts)
    limit = thread_state_manager.get_scribe_min_words(channel, actual_thread)

    note_text = await client.wait_for_scribe_note(channel, actual_thread, timeout=15.0)
    substantive_text = ""
    if note_text:
        substantive_text = (
            note_text.split("\n\n_Last updated:")[0]
            if "\n\n_Last updated:" in note_text
            else note_text
        )
    word_count = len(substantive_text.split()) if substantive_text else 0

    has_scribe_note = bool(note_text) or any(
        m.get("is_scribe_note") or m.get("username") == "Live Discussion Scribe"
        for m in client.bus.posted_messages
    )
    passed = limit == 30 and has_scribe_note and word_count >= 25
    return ScenarioResult(
        "TC-07",
        "Scribe Minimum Word Count",
        passed,
        f"Scribe note generated ({word_count} words, floor: {limit}). Note in thread: {has_scribe_note}",
    )


async def tc_08_history_slicing(client: SlackProtocolClient) -> ScenarioResult:
    channel = client.get_channel("C_TEST_08")
    init_thread = client.get_thread_ts()
    await client.send_mention(
        "/[LegalAdvisor] /max_words 10 Step 1: Initial design proposal.",
        channel_id=channel,
        thread_ts=init_thread,
    )
    actual_thread = _resolve_thread_ts(client, init_thread) or "T_08"

    await client.send_mention(
        "/[Security Architect] /max_words 10 Step 2: Security Architecture review.",
        channel_id=channel,
        thread_ts=actual_thread,
    )
    await client.send_mention(
        "/[Security Architect] /max_words 10 Step 3: Security review.",
        channel_id=channel,
        thread_ts=actual_thread,
    )
    await client.send_mention(
        "/[SalesAdvisor] /max_words 10 Step 4: Quality verification.",
        channel_id=channel,
        thread_ts=actual_thread,
    )

    prompt = "/history 2 /[Visionary] /max_words 10 Summarize recent context."
    await client.send_mention(prompt, channel_id=channel, thread_ts=actual_thread)

    msgs = client.bus.get_messages_for_thread(channel, actual_thread)
    hist_info = thread_state_manager.get_history_limit(channel, actual_thread)
    passed = (
        len(msgs) >= 5
        and hist_info is not None
        and hist_info["limit"] == 2
        and msgs[-1].get("username") == "Visionary"
    )
    return ScenarioResult(
        "TC-08",
        "History Window Slicing",
        passed,
        f"5 turns executed, history window sliced to {hist_info.get('limit') if hist_info else 'None'}.",
    )


async def tc_09_agent_exclusion(client: SlackProtocolClient) -> ScenarioResult:
    channel = client.get_channel("C_TEST_09")
    thread_ts = client.get_thread_ts()
    prompt = "/exclude /[Innovator] /max_words 10 What is the best strategy for microservice resiliency?"
    await client.send_mention(prompt, channel_id=channel, thread_ts=thread_ts)

    actual_thread = _resolve_thread_ts(client, thread_ts)
    msgs = client.bus.get_messages_for_thread(channel, actual_thread)
    speakers = [m.get("username") for m in msgs]
    excluded_participated = "Innovator" in speakers
    passed = not excluded_participated and len(speakers) >= 1
    return ScenarioResult(
        "TC-09",
        "Agent Exclusion Directive",
        passed,
        f"Speakers: {speakers} (Innovator excluded: {not excluded_participated})",
    )


async def tc_10_revoke_constraints(client: SlackProtocolClient) -> ScenarioResult:
    channel = client.get_channel("C_TEST_10")
    thread_ts = client.get_thread_ts()
    await client.send_mention(
        "/max_words 20 /[LegalAdvisor] Short explanation of container isolation.",
        channel_id=channel,
        thread_ts=thread_ts,
        user_id=TEST_USER_ID,
    )
    actual_thread = _resolve_thread_ts(client, thread_ts)

    await client.send_mention(
        "/revoke_constraint max_words",
        channel_id=channel,
        thread_ts=actual_thread,
        user_id=TEST_ADMIN_USER_ID,
    )
    limit = thread_state_manager.get_max_words(channel, actual_thread)
    sys_msgs = [
        s["text"] for s in client.bus.system_messages if s.get("channel_id") == channel
    ]

    # Follow-up turn to confirm constraint revocation allows normal turn execution
    await client.send_mention(
        "/[LegalAdvisor] Explain Kubernetes container lifecycle hooks in detail.",
        channel_id=channel,
        thread_ts=actual_thread,
        user_id=TEST_USER_ID,
    )
    msgs = client.bus.get_messages_for_thread(channel, actual_thread)

    passed = (
        limit is None
        and any("Revoked constraint" in t for t in sys_msgs)
        and len(msgs) >= 2
    )
    return ScenarioResult(
        "TC-10",
        "Constraint Revocation",
        passed,
        f"max_words limit={limit}, msgs={len(msgs)}, sys_msgs={len(sys_msgs)}",
    )


async def tc_11_multi_round(client: SlackProtocolClient) -> ScenarioResult:
    channel = client.get_channel("C_TEST_11")
    thread_ts = client.get_thread_ts()
    prompt = "/[Security Architect] /[LegalAdvisor] /rounds 2 /max_words 25 Monorepo vs Polyrepo debate."
    await client.send_mention(prompt, channel_id=channel, thread_ts=thread_ts)

    actual_thread = _resolve_thread_ts(client, thread_ts)
    msgs = client.bus.get_messages_for_thread(channel, actual_thread)
    speakers = [m.get("username") for m in msgs]
    passed = len(speakers) >= 3
    return ScenarioResult(
        "TC-11",
        "Multi-Round Debate",
        passed,
        f"Total turns executed across rounds: {len(speakers)} ({speakers})",
    )


async def tc_12_web_research(client: SlackProtocolClient) -> ScenarioResult:
    channel = client.get_channel("C_TEST_12")
    thread_ts = client.get_thread_ts()
    prompt = "/[MarketAndCompetitionResearchExpert] Search the web for current vector database pricing."
    await client.send_mention(prompt, channel_id=channel, thread_ts=thread_ts)

    actual_thread = _resolve_thread_ts(client, thread_ts)
    msgs = client.bus.get_messages_for_thread(channel, actual_thread)
    if not msgs:
        return ScenarioResult(
            "TC-12", "Web Research Skill", False, "No response posted."
        )

    text = msgs[0].get("text", "")
    has_pricing = any(
        k in text.lower() for k in ("pinecone", "qdrant", "milvus", "vector", "pricing")
    )
    has_skill_footer = "web_research" in text
    passed = has_pricing and has_skill_footer
    return ScenarioResult(
        "TC-12",
        "Web Research Skill",
        passed,
        f"has_pricing={has_pricing}, skill_footer={has_skill_footer}",
    )


async def tc_13_document_reader_compliance(client: SlackProtocolClient) -> ScenarioResult:
    resources_dir = get_agents_resources_dir()
    resources_dir.mkdir(parents=True, exist_ok=True)
    comp_file = resources_dir / "compliance_guidelines.txt"
    comp_file.write_text(
        "Retention Policy: Audit logs must be retained for 7 years with AES-256 encrypted archival storage.\n",
        encoding="utf-8",
    )

    channel = client.get_channel("C_TEST_13")
    thread_ts = client.get_thread_ts()
    prompt = "/[Security Architect] Read 'agents_resources/compliance_guidelines.txt' and summarize the key retention obligations."
    await client.send_mention(prompt, channel_id=channel, thread_ts=thread_ts)

    actual_thread = _resolve_thread_ts(client, thread_ts)
    msgs = client.bus.get_messages_for_thread(channel, actual_thread)
    if not msgs:
        return ScenarioResult(
            "TC-13", "Document Reading Skill", False, "No response posted."
        )

    text = msgs[0].get("text", "").lower()
    passed = ("7" in text or "seven" in text or "retention" in text) and "document_reader" in text
    return ScenarioResult(
        "TC-13",
        "Document Reading Skill",
        passed,
        f"Compliance document read via document_reader: {passed}",
    )


async def tc_14_document_authoring(client: SlackProtocolClient) -> ScenarioResult:
    target_path = get_agents_resources_dir() / "llm_cost_reduction_plan.md"
    if target_path.exists():
        target_path.unlink(missing_ok=True)

    channel = client.get_channel("C_TEST_14")
    thread_ts = client.get_thread_ts()
    prompt = "/[ROIStrategist] Draft a 3-point business case and save it as a document named agents_resources/llm_cost_reduction_plan.md."
    await client.send_mention(
        prompt,
        channel_id=channel,
        thread_ts=thread_ts,
        user_id=TEST_ADMIN_USER_ID,
    )

    actual_thread = _resolve_thread_ts(client, thread_ts)

    # document_authoring requires HITL approval; approve the card if posted
    if client.bus.hitl_cards:
        hitl_card = client.bus.hitl_cards[-1]
        req_id = ""
        for blk in hitl_card.get("blocks") or []:
            for elem in blk.get("elements") or []:
                if elem.get("action_id") == "hitl_approve_tool":
                    req_id = str(elem.get("value", ""))
        if req_id:
            await client.send_block_action(
                action_id="hitl_approve_tool",
                value=req_id,
                channel_id=channel,
                thread_ts=actual_thread,
                user_id=TEST_ADMIN_USER_ID,
                message_ts=hitl_card.get("ts"),
                wait_for_task=True,
            )

    msgs = client.bus.get_messages_for_thread(channel, actual_thread)
    if not msgs:
        return ScenarioResult(
            "TC-14", "Document Authoring Skill", False, "No response posted."
        )

    text = msgs[-1].get("text", "")
    file_written = (
        target_path.exists()
        and len(target_path.read_text(encoding="utf-8").strip()) > 10
    )
    passed = file_written and (
        "llm_cost_reduction_plan.md" in text or "document_authoring" in text
    )
    return ScenarioResult(
        "TC-14",
        "Document Authoring Skill",
        passed,
        f"File written on disk ({target_path}): {file_written}",
    )


async def tc_15_document_reader_attachment(
    client: SlackProtocolClient,
) -> ScenarioResult:
    resources_dir = get_agents_resources_dir()
    resources_dir.mkdir(parents=True, exist_ok=True)
    spec_file = resources_dir / "sys_spec.txt"
    if not spec_file.exists():
        spec_file.write_text(
            "Target throughput: 5000 rps\nLatency budget p99: <50ms\n",
            encoding="utf-8",
        )

    channel = client.get_channel("C_TEST_15")
    thread_ts = client.get_thread_ts()
    prompt = "/[Security Architect] Read agents_resources/sys_spec.txt and tell me the throughput specified in this file."
    await client.send_mention(prompt, channel_id=channel, thread_ts=thread_ts)

    actual_thread = _resolve_thread_ts(client, thread_ts) or "T_15"
    msgs = client.bus.get_messages_for_thread(channel, actual_thread)
    if not msgs:
        return ScenarioResult(
            "TC-15", "Document Reader & Attachment", False, "No response posted."
        )

    text = msgs[0].get("text", "")
    passed = ("5000" in text or "5,000" in text or "rps" in text.lower()) and "document_reader" in text
    return ScenarioResult(
        "TC-15",
        "Document Reader & Attachment",
        passed,
        f"Document sys_spec.txt parsed via document_reader: {passed}",
    )


async def tc_16_unauthorized_tool_rejection(
    client: SlackProtocolClient,
) -> ScenarioResult:
    channel = client.get_channel("C_TEST_16")
    thread_ts = client.get_thread_ts()
    blocked_target = get_agents_resources_dir() / "system_spec.md"
    if blocked_target.exists():
        blocked_target.unlink(missing_ok=True)

    prompt = "/[Security Architect] Save this configuration to agents_resources/system_spec.md using document_authoring."
    await client.send_mention(prompt, channel_id=channel, thread_ts=thread_ts)

    actual_thread = _resolve_thread_ts(client, thread_ts) or "T_16"
    msgs = client.bus.get_messages_for_thread(channel, actual_thread)
    if not msgs:
        return ScenarioResult(
            "TC-16", "Unauthorized Tool Rejection", False, "No response."
        )

    text = msgs[0].get("text", "").lower()
    has_denial = any(
        k in text
        for k in (
            "permission denied",
            "not authorized",
            "authorization error",
            "not permitted",
            "do not have permission",
            "don't have permission",
            "unauthorized",
            "lacks permission",
            "lack permission",
            "cannot",
            "unable to",
            "missing",
            "document_authoring",
        )
    )
    not_created = not blocked_target.exists()
    passed = has_denial and not_created
    return ScenarioResult(
        "TC-16",
        "Unauthorized Tool Rejection",
        passed,
        f"Denial reported={has_denial}, unauthorized file blocked={not_created}.",
    )


async def tc_17_grant_skill(client: SlackProtocolClient) -> ScenarioResult:
    channel = client.get_channel("C_TEST_17")
    thread_ts = client.get_thread_ts()
    await client.send_mention(
        "/grant_skill web_research /[Security Architect]",
        channel_id=channel,
        thread_ts=thread_ts,
        user_id=TEST_ADMIN_USER_ID,
    )
    actual_thread = _resolve_thread_ts(client, thread_ts)

    sys_msgs = [
        s["text"] for s in client.bus.system_messages if s.get("channel_id") == channel
    ]
    granted = any("Agent Skill Granted" in m for m in sys_msgs)

    # Follow-up turn verifying SecurityArchitect can now execute web_research
    await client.send_mention(
        "/[Security Architect] Search the web for current vector database pricing.",
        channel_id=channel,
        thread_ts=actual_thread,
        user_id=TEST_USER_ID,
    )
    msgs = client.bus.get_messages_for_thread(channel, actual_thread)
    used_web = bool(msgs and "web_research" in msgs[-1].get("text", ""))
    passed = granted and used_web
    return ScenarioResult(
        "TC-17",
        "Granular Skill Grant",
        passed,
        f"granted={granted}, follow_up_used_web_research={used_web}",
    )


async def tc_18_grant_for_all(client: SlackProtocolClient) -> ScenarioResult:
    channel = client.get_channel("C_TEST_18")
    thread_ts = client.get_thread_ts()
    await client.send_mention(
        "/grant_skill_for_all web_research",
        channel_id=channel,
        thread_ts=thread_ts,
        user_id=TEST_ADMIN_USER_ID,
    )

    sys_msgs = [
        s["text"] for s in client.bus.system_messages if s.get("channel_id") == channel
    ]
    passed = any("Thread-Global Skills Granted" in m for m in sys_msgs)
    return ScenarioResult(
        "TC-18",
        "Global Skill Grant",
        passed,
        f"Thread-global grant notices: {len(sys_msgs)}",
    )


async def tc_19_revoke_for_all(client: SlackProtocolClient) -> ScenarioResult:
    channel = client.get_channel("C_TEST_19")
    thread_ts = client.get_thread_ts()
    await client.send_mention(
        "/grant_skill_for_all web_research",
        channel_id=channel,
        thread_ts=thread_ts,
        user_id=TEST_ADMIN_USER_ID,
    )
    actual_thread = _resolve_thread_ts(client, thread_ts)

    await client.send_mention(
        "/revoke_skill_for_all web_research",
        channel_id=channel,
        thread_ts=actual_thread,
        user_id=TEST_ADMIN_USER_ID,
    )
    sys_msgs = [
        s["text"] for s in client.bus.system_messages if s.get("channel_id") == channel
    ]
    passed = any("Revoked" in m for m in sys_msgs)
    return ScenarioResult(
        "TC-19",
        "Global Skill Revocation",
        passed,
        f"Revocation confirmed in system messages: {passed}",
    )


async def tc_20_list_skills(client: SlackProtocolClient) -> ScenarioResult:
    channel = client.get_channel("C_TEST_20")
    thread_ts = client.get_thread_ts()
    await client.send_mention("/list_skills", channel_id=channel, thread_ts=thread_ts)

    actual_thread = _resolve_thread_ts(client, thread_ts)
    sys_msgs = [
        s["text"] for s in client.bus.system_messages if s.get("channel_id") == channel
    ]
    agent_msgs = client.bus.get_messages_for_thread(channel, actual_thread)
    has_catalog = any(
        "web_research" in m or "document_reader" in m or "Skills" in m for m in sys_msgs
    )
    passed = has_catalog and len(agent_msgs) == 0
    return ScenarioResult(
        "TC-20",
        "Skills Directory Query",
        passed,
        f"Catalog posted={has_catalog}, panel_halted={len(agent_msgs) == 0}",
    )


async def tc_21_delayed_progress_inplace_edit(
    client: SlackProtocolClient,
) -> ScenarioResult:
    channel = client.get_channel("C_TEST_21")
    thread_ts = client.get_thread_ts()

    # Configure a short intermediate delay so tool status + in-place edit trigger reliably
    configure_router_deps(PipelineDeps(intermediate_delay_secs=0.05))
    is_mock = bool(getattr(client, "mock", False))
    if is_mock:
        async def _slow_vector_search(query: str, max_results: int = 5) -> str:
            await asyncio.sleep(0.15)
            return json.dumps({
                "results": [
                    {"title": "Pinecone", "url": "https://pinecone.io", "snippet": "Managed cloud vector database."},
                    {"title": "Qdrant", "url": "https://qdrant.tech", "snippet": "Open-source Rust vector search."},
                    {"title": "Milvus", "url": "https://milvus.io", "snippet": "Distributed vector database."},
                ]
            })

        configure_web_search_fn(_slow_vector_search)

    try:
        prompt = "/[MarketAndCompetitionResearchExpert] Comprehensive web search on vector databases."
        await client.send_mention(prompt, channel_id=channel, thread_ts=thread_ts)
    finally:
        reset_router_deps()
        if is_mock:
            from tests.e2e.runner import _mock_web_search

            configure_web_search_fn(_mock_web_search)
        else:
            reset_web_search_fn()

    actual_thread = _resolve_thread_ts(client, thread_ts) or "T_21"
    msgs = client.bus.get_messages_for_thread(channel, actual_thread)
    if not msgs:
        return ScenarioResult(
            "TC-21",
            "Delayed Progress Indicator & In-Place Edit",
            False,
            "No response posted.",
        )

    text = msgs[0].get("text", "").lower()
    has_vector_results = any(k in text for k in ("pinecone", "qdrant", "milvus", "vector"))
    has_inplace_edit = len(client.bus.updated_messages) >= 1
    passed = len(msgs) == 1 and has_vector_results and has_inplace_edit
    return ScenarioResult(
        "TC-21",
        "Delayed Progress Indicator & In-Place Edit",
        passed,
        f"vector_results={has_vector_results}, in_place_edits={len(client.bus.updated_messages)}, msgs={len(msgs)}",
    )


async def tc_22_scribe_download_dm(client: SlackProtocolClient) -> ScenarioResult:
    channel = client.get_channel("C_TEST_22")
    thread_ts = client.get_thread_ts()

    prompt = "/[Security Architect] /[LegalAdvisor] /max_words 25 What is our strategy for container image vulnerability scanning in CI/CD?"
    await client.send_mention(prompt, channel_id=channel, thread_ts=thread_ts)

    actual_thread = _resolve_thread_ts(client, thread_ts)
    note_text = await client.wait_for_scribe_note(channel, actual_thread, timeout=15.0)
    has_scribe_note = bool(note_text) or any(
        m.get("is_scribe_note") or m.get("username") == "Live Discussion Scribe"
        for m in client.bus.posted_messages
    )

    download_value = f"{channel}:{actual_thread}"
    has_dl_button = False
    for note in reversed(client.bus.thread_notes or client.bus.posted_messages):
        if note.get("channel_id") != channel:
            continue
        for block in note.get("blocks") or []:
            for elem in block.get("elements") or []:
                if elem.get("action_id") == "download_scribe_notes_txt":
                    has_dl_button = True
                    if elem.get("value"):
                        download_value = str(elem["value"])
                    break
            if has_dl_button:
                break
        if has_dl_button:
            break

    await client.send_block_action(
        action_id="download_scribe_notes_txt",
        value=download_value,
        channel_id=channel,
        thread_ts=actual_thread,
        user_id="U_TESTER_22",
    )

    has_ephemeral = any(
        e.get("user_id") == "U_TESTER_22"
        and "direct message" in e.get("text", "").lower()
        for e in client.bus.ephemeral_messages
    )
    has_file = any("txt" in f.get("filename", "") for f in client.bus.uploaded_files)
    passed = has_scribe_note and has_dl_button and has_ephemeral and has_file
    return ScenarioResult(
        "TC-22",
        "Scribe Download Button (.txt in DM)",
        passed,
        f"note={has_scribe_note}, button={has_dl_button}, ephemeral={has_ephemeral}, txt_uploaded={has_file}",
    )


async def tc_23_circuit_breaker(client: SlackProtocolClient) -> ScenarioResult:
    channel = client.get_channel("C_TEST_23")
    thread_ts = client.get_thread_ts()
    try:
        set_circuit_breaker_enabled(True, save=False)
        set_circuit_breaker_threshold(0.00001, save=False)

        prompt = "/[Security Architect] /[LegalAdvisor] Migration to event streaming."
        await client.send_mention(prompt, channel_id=channel, thread_ts=thread_ts)

        actual_thread = _resolve_thread_ts(client, thread_ts)
        texts = client.bus.get_all_texts_for_thread(channel, actual_thread)
        tripped = any("EXPENSE LIMIT REACHED FOR THE THREAD" in t for t in texts)
        return ScenarioResult(
            "TC-23",
            "Expense Circuit Breaker Tripping",
            tripped,
            "Circuit breaker tripped and calls halted.",
        )
    finally:
        set_circuit_breaker_enabled(False, save=False)
        set_circuit_breaker_threshold(1.50, save=False)


async def tc_24_usage_channel(client: SlackProtocolClient) -> ScenarioResult:
    channel = client.get_channel("C_TEST_24")
    thread_ts = client.get_thread_ts()
    await client.send_mention(
        "/[Visionary] Future of quantum computing in fintech.",
        channel_id=channel,
        thread_ts=thread_ts,
    )

    from agents.usage_agent import usage_tracker

    actual_thread = _resolve_thread_ts(client, thread_ts)
    cost_data = usage_tracker.calculate_thread_costs(channel, actual_thread)
    models = cost_data.get("models", {})
    passed = bool(
        models and any(m.get("prompt_tokens", 0) > 0 for m in models.values())
    )
    return ScenarioResult(
        "TC-24",
        "Usage Channel Token Tracking",
        passed,
        f"Tracked models: {list(models.keys())}",
    )


async def tc_25_stop_concurrency(client: SlackProtocolClient) -> ScenarioResult:
    channel = client.get_channel("C_TEST_25")
    thread_ts = client.get_thread_ts()

    prompt = "/max_words 15 Debate the viability of replacing SQL databases with blockchain for high-frequency inventory tracking across all our services."
    await client.send_mention(
        prompt, channel_id=channel, thread_ts=thread_ts, wait_for_task=False
    )

    actual_thread = _resolve_thread_ts(client, thread_ts)

    await asyncio.sleep(0.05)
    await client.send_mention("/stop", channel_id=channel, thread_ts=actual_thread)
    await asyncio.sleep(0.1)

    is_active = thread_state_manager.is_discussion_active(channel, actual_thread)
    sys_msgs = [
        s["text"] for s in client.bus.system_messages if s.get("channel_id") == channel
    ]
    has_stop_confirmation = any(
        "Discussion stopped" in m or "Halted" in m for m in sys_msgs
    )
    rem_agents = thread_state_manager.get_remaining_agents(channel, actual_thread)

    posted_msgs = client.bus.get_messages_for_thread(channel, actual_thread)
    speakers = [m.get("username") for m in posted_msgs]
    total_panel = max(1, len(settings_manager.get_enabled_agents()))

    passed = (
        (not is_active) or has_stop_confirmation or not rem_agents
    ) and len(speakers) < total_panel
    await client.drain_background_tasks()
    return ScenarioResult(
        "TC-25",
        "Stop Directive Concurrency",
        passed,
        f"Active={is_active}, remaining_cleared={not rem_agents}, speakers={len(speakers)}/{total_panel}",
    )


async def tc_26_superseding_language_and_tag_validation(
    client: SlackProtocolClient,
) -> ScenarioResult:
    # Part A: Mid-flight superseding increments generation
    ch_a = client.get_channel("C_TEST_26A")
    th_a = client.get_thread_ts()
    await client.send_mention(
        "/[Innovator] /[Security Architect] /[LegalAdvisor] /max_words 15 Compare monolithic and microservice topologies.",
        channel_id=ch_a,
        thread_ts=th_a,
        wait_for_task=False,
    )
    actual_th_a = _resolve_thread_ts(client, th_a)
    await asyncio.sleep(0.02)
    await client.send_mention(
        "/[Visionary] /max_words 15 Pivot completely: outline our edge caching strategy.",
        channel_id=ch_a,
        thread_ts=actual_th_a,
    )
    gen = thread_state_manager.get_generation(ch_a, actual_th_a)
    msgs_a = client.bus.get_messages_for_thread(ch_a, actual_th_a)
    superseded_ok = gen >= 1 and bool(msgs_a) and msgs_a[-1].get("username") == "Visionary"
    await client.drain_background_tasks()

    # Part B: Croatian language lock
    ch_b = client.get_channel("C_TEST_26B") if not client.live_mode else ch_a
    th_b = None
    client.current_thread_ts = None
    await client.send_mention(
        "/[Security Architect] /max_words 25 Koji su ključni rizici ako migriramo sustav na mikroservise?",
        channel_id=ch_b,
        thread_ts=th_b,
    )
    actual_th_b = _resolve_thread_ts(client, th_b)
    locked_lang = thread_state_manager.get_language(ch_b, actual_th_b)
    lang_ok = locked_lang == "Croatian"
    await client.drain_background_tasks()

    # Part C: Unrecognized tag halts cleanly without invoking agents
    ch_c = client.get_channel("C_TEST_26C") if not client.live_mode else ch_a
    client.current_thread_ts = None
    await client.send_mention(
        "/[NonExistentPersonaXYZ] Explain this architecture.",
        channel_id=ch_c,
        thread_ts=None,
    )
    actual_th_c = _resolve_thread_ts(client, None)
    sys_c = [
        s["text"] for s in client.bus.system_messages if s.get("channel_id") == ch_c
    ]
    msgs_c = client.bus.get_messages_for_thread(ch_c, actual_th_c)
    tag_halt_ok = any("Unrecognized Agent Tag" in t for t in sys_c) and len(msgs_c) == 0
    await client.drain_background_tasks()

    passed = superseded_ok and lang_ok and tag_halt_ok
    return ScenarioResult(
        "TC-26",
        "Mid-Flight Superseding, Language Lock & Tag Validation",
        passed,
        f"superseded_gen={gen} ({superseded_ok}), croatian_lock={locked_lang} ({lang_ok}), tag_halt={tag_halt_ok}",
    )


async def tc_27_default_workflow_untagged(
    client: SlackProtocolClient,
) -> ScenarioResult:
    channel = client.get_channel("C_TEST_27")
    thread_ts = client.get_thread_ts()
    prompt = "Evaluate our distributed consensus and deployment strategy for 2026."
    await client.send_mention(prompt, channel_id=channel, thread_ts=thread_ts)

    actual_thread = _resolve_thread_ts(client, thread_ts)
    msgs = client.bus.get_messages_for_thread(channel, actual_thread)
    if not msgs:
        return ScenarioResult(
            "TC-27",
            "Default Workflow on Untagged Mention",
            False,
            "No agent response posted.",
        )

    speakers = [m.get("username") for m in msgs]
    passed = len(speakers) >= 2 and speakers[0] == "Visionary"
    return ScenarioResult(
        "TC-27",
        "Default Workflow on Untagged Mention",
        passed,
        f"Default workflow triggered. Speakers: {speakers}",
    )


async def tc_28_admin_rbac_gate(client: SlackProtocolClient) -> ScenarioResult:
    channel = client.get_channel("C_TEST_28")
    thread_ts = client.get_thread_ts()

    # Non-admin user must be rejected with CRITICAL audit event
    await client.send_mention(
        "/grant_skill web_research /[Security Architect]",
        channel_id=channel,
        thread_ts=thread_ts,
        user_id=TEST_USER_ID,
    )
    actual_thread = _resolve_thread_ts(client, thread_ts)

    sys_msgs = [
        s["text"] for s in client.bus.system_messages if s.get("channel_id") == channel
    ]
    blocked_non_admin = any("Permission Denied" in m for m in sys_msgs)
    audit_events = get_audit_trail(audit_dir=audit_logger.audit_dir)
    has_critical_audit = any(
        e.get("event_type") == "UNAUTHORIZED_DIRECTIVE_ATTEMPT"
        and e.get("severity") == "CRITICAL"
        for e in audit_events
    )

    # Admin user must succeed
    await client.send_mention(
        "/grant_skill web_research /[Security Architect]",
        channel_id=channel,
        thread_ts=actual_thread,
        user_id=TEST_ADMIN_USER_ID,
    )
    sys_msgs_after = [
        s["text"] for s in client.bus.system_messages if s.get("channel_id") == channel
    ]
    allowed_admin = any("Agent Skill Granted" in m for m in sys_msgs_after)

    passed = blocked_non_admin and has_critical_audit and allowed_admin
    return ScenarioResult(
        "TC-28",
        "Declarative Admin RBAC Gate & Audit",
        passed,
        f"blocked_non_admin={blocked_non_admin}, critical_audit={has_critical_audit}, allowed_admin={allowed_admin}",
    )


async def tc_29_combined_grant_inline_continuation(
    client: SlackProtocolClient,
) -> ScenarioResult:
    channel = client.get_channel("C_TEST_29")
    thread_ts = client.get_thread_ts()
    prompt = (
        "/grant_skill_for_all web_research /[Security Architect] /max_words 45 "
        "Load the web_research skill and search the web for vector database pricing."
    )
    await client.send_mention(
        prompt,
        channel_id=channel,
        thread_ts=thread_ts,
        user_id=TEST_ADMIN_USER_ID,
    )

    actual_thread = _resolve_thread_ts(client, thread_ts)
    sys_msgs = [
        s["text"] for s in client.bus.system_messages if s.get("channel_id") == channel
    ]
    msgs = client.bus.get_messages_for_thread(channel, actual_thread)
    grant_posted = any("Thread-Global Skills Granted" in m for m in sys_msgs)
    continued = bool(msgs and msgs[0].get("username") == "SecurityArchitect" and "web_research" in msgs[0].get("text", ""))
    passed = grant_posted and continued
    return ScenarioResult(
        "TC-29",
        "Combined Grant Directive + Inline Continuation",
        passed,
        f"grant_posted={grant_posted}, continued_with_web_research={continued}",
    )


async def tc_30_skill_routing_filter_and_short_circuit(
    client: SlackProtocolClient,
) -> ScenarioResult:
    channel = client.get_channel("C_TEST_30")
    thread_ts = client.get_thread_ts()

    orig_filter = is_filter_unauthorized_skill_agents_enabled()
    orig_mode = settings_manager.get_orchestration_mode()
    orig_fixed = list(settings_manager.fixed_chain)

    try:
        set_filter_unauthorized_skill_agents_enabled(True, save=False)
        settings_manager.set_orchestration_mode("fixed", save=False)
        # Visionary (agent-10001) and Sceptic (agent-10003) both lack web_research
        settings_manager.set_fixed_config(["agent-10001", "agent-10003"], save=False)

        await client.send_mention(
            "Search the web for current vector database pricing.",
            channel_id=channel,
            thread_ts=thread_ts,
        )
        actual_thread = _resolve_thread_ts(client, thread_ts)
        sys_msgs = [
            s["text"] for s in client.bus.system_messages if s.get("channel_id") == channel
        ]
        msgs = client.bus.get_messages_for_thread(channel, actual_thread)
        has_filter_notice = any("Skill Routing Filter" in m for m in sys_msgs)
        has_username_hint = any(
            name in m
            for m in sys_msgs
            for name in (
                "CreativeAdvisor",
                "LegalAdvisor",
                "MarketAndCompetitionResearchExpert",
                "Revisor",
                "SalesAdvisor",
            )
        )
        short_circuited = len(msgs) == 0

        # Next test with an authorized agent included in chain
        client.current_thread_ts = None
        ch2 = client.get_channel("C_TEST_30B") if not client.live_mode else channel
        settings_manager.set_fixed_config(["agent-10001", "agent-10009"], save=False)
        await client.send_mention(
            "Search the web for current vector database pricing.",
            channel_id=ch2,
            thread_ts=None,
        )
        th2 = _resolve_thread_ts(client, None)
        msgs2 = client.bus.get_messages_for_thread(ch2, th2)
        routed_to_authorized = bool(
            msgs2 and msgs2[0].get("username") == "MarketAndCompetitionResearchExpert"
        )

        passed = has_filter_notice and has_username_hint and short_circuited and routed_to_authorized
        return ScenarioResult(
            "TC-30",
            "Skill Routing Filter & Pure-Skill Short-Circuit",
            passed,
            f"filter_notice={has_filter_notice}, username_hint={has_username_hint}, short_circuit={short_circuited}, routed_auth={routed_to_authorized}",
        )
    finally:
        set_filter_unauthorized_skill_agents_enabled(orig_filter, save=False)
        settings_manager.set_orchestration_mode(orig_mode, save=False)
        settings_manager.set_fixed_config(orig_fixed, save=False)


async def tc_31_exhausted_queue_recovery_and_active_prompt(
    client: SlackProtocolClient,
) -> ScenarioResult:
    channel = client.get_channel("C_TEST_31")
    thread_ts = client.get_thread_ts()

    # Turn 1: Single explicit agent completes and exhausts queue
    await client.send_mention(
        "/[Visionary] /max_words 15 Explain zero-trust networking.",
        channel_id=channel,
        thread_ts=thread_ts,
    )
    actual_thread = _resolve_thread_ts(client, thread_ts)
    init_p_1 = thread_state_manager.get_initial_prompt(channel, actual_thread)
    active_p_1 = thread_state_manager.get_active_prompt(channel, actual_thread)

    # Turn 2: Follow-up untagged prompt after queue exhaustion must re-initialize cleanly
    await client.send_mention(
        "/max_words 15 Now evaluate database sharding tradeoffs.",
        channel_id=channel,
        thread_ts=actual_thread,
    )
    init_p_2 = thread_state_manager.get_initial_prompt(channel, actual_thread)
    active_p_2 = thread_state_manager.get_active_prompt(channel, actual_thread)
    msgs = client.bus.get_messages_for_thread(channel, actual_thread)

    passed = (
        len(msgs) >= 2
        and init_p_1 == init_p_2
        and "zero-trust" in (init_p_2 or "").lower()
        and active_p_2 != active_p_1
        and "sharding" in (active_p_2 or "").lower()
    )
    return ScenarioResult(
        "TC-31",
        "Exhausted Queue Recovery & active_prompt Tracking",
        passed,
        f"msgs={len(msgs)}, initial_preserved={'zero-trust' in (init_p_2 or '').lower()}, active_updated={active_p_2}",
    )


async def tc_32_internal_revisor_ground_truth_and_confirmation(
    client: SlackProtocolClient,
) -> ScenarioResult:
    channel = client.get_channel("C_TEST_32")
    thread_ts = client.get_thread_ts()
    resources_dir = get_agents_resources_dir()
    resources_dir.mkdir(parents=True, exist_ok=True)
    spec_file = resources_dir / "sys_spec.txt"
    if not spec_file.exists():
        spec_file.write_text(
            "Target throughput: 5000 rps\nLatency budget p99: <50ms\n",
            encoding="utf-8",
        )

    orig_revisor = system_settings_manager.get_internal_revisor_enabled()
    orig_sys_timeout = system_settings_manager.get_system_agent_inference_timeout_seconds()
    system_settings_manager.set_internal_revisor_enabled(True, save=False)
    system_settings_manager.set_system_agent_inference_timeout_seconds(45.0, save=False)
    try:
        # Turn 1: Grounded read of sys_spec.txt (5000 rps) -> populates evidence_store
        prompt_1 = "/[Security Architect] Read agents_resources/sys_spec.txt and report the target throughput."
        await client.send_mention(
            prompt_1,
            channel_id=channel,
            thread_ts=thread_ts,
        )
        actual_thread = _resolve_thread_ts(client, thread_ts)
        await client.drain_background_tasks()

        msgs_after_turn1 = client.bus.get_messages_for_thread(channel, actual_thread)
        turn1_revisor_alerts = [
            m
            for m in msgs_after_turn1
            if m.get("username") in ("Internal Revisor", "InternalRevisor")
            and "ATTENTION:" in m.get("text", "")
        ]
        clean_turn1 = len(turn1_revisor_alerts) == 0

        # Turn 2: Exercise InternalRevisor Stage 1 + Stage 2 against real Turn 1 evidence_store
        # without coercing a live speaker LLM to fabricate a falsehood.
        agent_cfg = dict(settings_manager.get_agent("agent-10008") or {})
        speaker_username = agent_cfg.get("username", "SecurityArchitect")
        speaker_icon = agent_cfg.get("icon_url", "avatars/image_06.png")
        contradicted_claim = (
            "According to sys_spec.txt, the verified target throughput is 99999 rps under normal conditions."
        )
        await post_agent_response(
            channel_id=channel,
            thread_ts=actual_thread,
            text=contradicted_claim,
            username=speaker_username,
            icon_url=speaker_icon,
        )

        revisor_ctx = TurnContext(
            channel_id=channel,
            thread_ts=actual_thread,
            raw_text=prompt_1,
            final_text=contradicted_claim,
            skills_used=["document_reader"],
        )
        revisor_ctx.agent_key = "agent-10008"
        revisor_ctx.agent_cfg = agent_cfg or {"username": speaker_username}
        revisor_ctx.full_history = [
            {
                "role": "assistant",
                "content": f"[{m.get('username', speaker_username)}]: {m.get('text', '')}",
            }
            for m in msgs_after_turn1
            if m.get("role") != "user"
            and m.get("username") not in ("Internal Revisor", "InternalRevisor")
            and m.get("text")
        ]
        revisor_ctx.force_termination = True
        await run_internal_revisor(revisor_ctx)

        msgs = client.bus.get_messages_for_thread(channel, actual_thread)
        revisor_msgs = [
            m for m in msgs if m.get("username") in ("Internal Revisor", "InternalRevisor")
        ]
        # Verify InternalRevisor ran cleanly on Turn 1 and confirmed ATTENTION alert on Turn 2
        has_attention = any("ATTENTION:" in m.get("text", "") for m in revisor_msgs)
        passed = clean_turn1 and len(msgs) >= 2 and has_attention
        return ScenarioResult(
            "TC-32",
            "InternalRevisor Evidence Isolation & Stage 2 Confirmation",
            passed,
            f"total_msgs={len(msgs)}, turn1_false_positives={len(turn1_revisor_alerts)}, revisor_alerts={len(revisor_msgs)}, attention={has_attention}",
        )
    finally:
        system_settings_manager.set_internal_revisor_enabled(orig_revisor, save=False)
        system_settings_manager.set_system_agent_inference_timeout_seconds(orig_sys_timeout, save=False)


async def tc_33_challenger_and_arbitrator_stress_test(
    client: SlackProtocolClient,
) -> ScenarioResult:
    channel = client.get_channel("C_TEST_33")
    thread_ts = client.get_thread_ts()

    orig_ff = is_convergence_fast_forward_enabled()
    orig_thresh = system_settings_manager.get_convergence_consecutive_threshold()
    orig_stress = dict(system_settings_manager.dialectical_stress_test)
    orig_mode = settings_manager.get_orchestration_mode()
    orig_start = list(settings_manager.sandwich_start_chain)
    orig_end = list(settings_manager.sandwich_end_chain)

    try:
        set_convergence_fast_forward_enabled(True, save=False)
        system_settings_manager.dialectical_stress_test["enabled"] = True
        system_settings_manager.set_convergence_consecutive_threshold(2, save=False)
        settings_manager.set_orchestration_mode("sandwich", save=False)
        settings_manager.set_sandwich_config(["agent-10001"], ["agent-10008"], save=False)

        prompt = (
            "/max_words 25 We already agree on phased async architecture. "
            "Load control_plane and call signal_consensus_convergence to converge on this plan."
        )
        await client.send_mention(prompt, channel_id=channel, thread_ts=thread_ts)

        actual_thread = _resolve_thread_ts(client, thread_ts)
        msgs = client.bus.get_messages_for_thread(channel, actual_thread)
        speakers = [m.get("username") for m in msgs]
        has_challenger = "Challenger" in speakers
        has_arbitrator = "Arbitrator" in speakers
        passed = has_challenger and has_arbitrator
        return ScenarioResult(
            "TC-33",
            "Challenger & Arbitrator Convergence Stress-Test",
            passed,
            f"Speakers: {speakers}",
        )
    finally:
        set_convergence_fast_forward_enabled(orig_ff, save=False)
        system_settings_manager.dialectical_stress_test = orig_stress
        system_settings_manager.set_convergence_consecutive_threshold(orig_thresh, save=False)
        settings_manager.set_orchestration_mode(orig_mode, save=False)
        settings_manager.set_sandwich_config(orig_start, orig_end, save=False)


async def tc_34_hitl_clarification_suspend_resume(
    client: SlackProtocolClient,
) -> ScenarioResult:
    channel = client.get_channel("C_TEST_34")
    thread_ts = client.get_thread_ts()

    # Turn 1: Visionary calls request_user_clarification, pausing chain before SecurityArchitect
    await client.send_mention(
        "/[Visionary] /[Security Architect] Before deciding, call request_user_clarification to ask the user for clarification on target region and SLA.",
        channel_id=channel,
        thread_ts=thread_ts,
    )
    actual_thread = _resolve_thread_ts(client, thread_ts)
    msgs_before = client.bus.get_messages_for_thread(channel, actual_thread)
    speakers_before = [m.get("username") for m in msgs_before]
    rem_during_pause = thread_state_manager.get_remaining_agents(channel, actual_thread)
    paused_cleanly = (
        len(msgs_before) == 1
        and speakers_before[0] == "Visionary"
        and "agent-10008" in rem_during_pause
    )

    # Turn 2: Human provides clarification -> discussion resumes with SecurityArchitect
    await client.send_mention(
        "Use EU-Central region with 99.99% availability SLA and 15-minute RPO.",
        channel_id=channel,
        thread_ts=actual_thread,
    )
    msgs_after = client.bus.get_messages_for_thread(channel, actual_thread)
    speakers_after = [m.get("username") for m in msgs_after]
    resumed_ok = "SecurityArchitect" in speakers_after
    passed = paused_cleanly and resumed_ok
    return ScenarioResult(
        "TC-34",
        "HITL Clarification Suspend & Resume",
        passed,
        f"paused_cleanly={paused_cleanly} (rem={rem_during_pause}), speakers_after={speakers_after}",
    )


async def tc_35_external_mcp_bridge_and_hitl(
    client: SlackProtocolClient,
) -> ScenarioResult:
    channel = client.get_channel("C_TEST_35")
    thread_ts = client.get_thread_ts()

    class _DummyContent:
        def __init__(self, text: str) -> None:
            self.text = text

    class _DummyCallToolResult:
        def __init__(self, text: str, is_error: bool = False) -> None:
            self.content = [_DummyContent(text)]
            self.isError = is_error

    class _DummyMcpTool:
        def __init__(self, name: str, description: str, input_schema: dict[str, Any], annotations: dict[str, Any] | None = None) -> None:
            self.name = name
            self.description = description
            self.inputSchema = input_schema
            self.annotations = annotations

    recorded_mcp_calls: list[tuple[str, dict[str, Any]]] = []
    mock_session = MagicMock()
    mock_session.initialize = AsyncMock()
    mock_session.list_tools = AsyncMock(
        return_value=MagicMock(
            tools=[
                _DummyMcpTool(
                    name="query_records",
                    description="Reads records from mock_db",
                    input_schema={"type": "object", "properties": {"table": {"type": "string"}, "actor": {"type": "string"}}},
                ),
                _DummyMcpTool(
                    name="drop_table",
                    description="Drops a table from mock_db (destructive)",
                    input_schema={"type": "object", "properties": {"table": {"type": "string"}}},
                    annotations={"destructiveHint": True},
                ),
            ]
        )
    )

    async def _call_tool(name: str, args: dict[str, Any]) -> _DummyCallToolResult:
        recorded_mcp_calls.append((name, dict(args)))
        if name == "query_records":
            return _DummyCallToolResult(json.dumps({"rows": 42, "table": args.get("table", "orders")}))
        if name == "drop_table":
            return _DummyCallToolResult(json.dumps({"status": "dropped", "table": args.get("table", "orders")}))
        return _DummyCallToolResult("Unknown tool", is_error=True)

    mock_session.call_tool = AsyncMock(side_effect=_call_tool)

    @asynccontextmanager
    async def _mock_transport(server_name: str, server_cfg: Any):
        yield mock_session

    reg = get_skill_registry()
    mgr = get_mcp_client_manager()
    target_agent_key = "agent-10006"
    orig_mcp_servers = dict(system_settings_manager.mcp_servers)
    orig_agent_skills = list(settings_manager.get_agent_skills(target_agent_key))
    temp_skill = SkillDefinition(
        name="mcp_mock_db",
        display_name="Mock DB MCP",
        description="External MCP mock database skill",
        instructions="Use mcp__mock_db__query_records or mcp__mock_db__drop_table.",
        scope="agent",
        mcp_server="mock_db",
        mcp_tools=["*"],
    )

    try:
        system_settings_manager.set_mcp_servers(
            {
                "mock_db": {
                    "enabled": True,
                    "transport": "stdio",
                    "command": "python",
                    "mutating_tools": ["drop_table"],
                    "outbound_pii_masking": True,
                    "timeout_seconds": 5.0,
                }
            },
            save=False,
        )
        mgr.configure_transport_factory(_mock_transport)
        reg._skills["mcp_mock_db"] = temp_skill
        settings_manager.set_agent_skills(
            target_agent_key,
            list(dict.fromkeys(orig_agent_skills + ["mcp_mock_db"])),
            save=False,
        )

        # Step 1: Non-destructive MCP read tool
        await client.send_mention(
            "/[ROIStrategist] Use skill mcp_mock_db to run query_records on table tmp_staging_orders.",
            channel_id=channel,
            thread_ts=thread_ts,
            user_id=TEST_ADMIN_USER_ID,
        )
        actual_thread = _resolve_thread_ts(client, thread_ts)
        read_called = any(name == "query_records" for name, _ in recorded_mcp_calls)

        # Step 2: Mutating MCP tool triggers HITL approval card on temporary staging table
        await client.send_mention(
            "/[ROIStrategist] Use skill mcp_mock_db to run drop_table on table tmp_staging_orders (temporary E2E test table; human approval will be requested automatically).",
            channel_id=channel,
            thread_ts=actual_thread,
            user_id=TEST_ADMIN_USER_ID,
        )
        hitl_card = client.bus.hitl_cards[-1] if client.bus.hitl_cards else None
        drop_blocked_before_approval = not any(name == "drop_table" for name, _ in recorded_mcp_calls)

        req_id = ""
        if hitl_card:
            for blk in hitl_card.get("blocks") or []:
                for elem in blk.get("elements") or []:
                    if elem.get("action_id") == "hitl_approve_tool":
                        req_id = str(elem.get("value", ""))

        # Step 3: Approve via Slack interactive button -> executes drop_table & resumes
        approved_ok = False
        if req_id:
            await client.send_block_action(
                action_id="hitl_approve_tool",
                value=req_id,
                channel_id=channel,
                thread_ts=actual_thread,
                user_id=TEST_ADMIN_USER_ID,
                message_ts=hitl_card.get("ts") if hitl_card else None,
                wait_for_task=True,
            )
            approved_ok = any(name == "drop_table" for name, _ in recorded_mcp_calls)

        passed = read_called and bool(hitl_card) and drop_blocked_before_approval and approved_ok
        return ScenarioResult(
            "TC-35",
            "External MCP Tool Bridge & Destructive HITL Gate",
            passed,
            f"read_called={read_called}, hitl_card={bool(hitl_card)}, blocked_before_approve={drop_blocked_before_approval}, approved_exec={approved_ok}",
        )
    finally:
        reg._skills.pop("mcp_mock_db", None)
        mgr.reset_transport_factory()
        system_settings_manager.set_mcp_servers(orig_mcp_servers, save=False)
        settings_manager.set_agent_skills(target_agent_key, orig_agent_skills, save=False)


async def tc_36_scatter_gather_topology(client: SlackProtocolClient) -> ScenarioResult:
    channel = client.get_channel("C_TEST_36")
    thread_ts = client.get_thread_ts()

    orig_mode = settings_manager.get_orchestration_mode()
    orig_start = list(settings_manager.sandwich_start_chain)
    orig_end = list(settings_manager.sandwich_end_chain)
    orig_enabled = set(settings_manager.enabled_agents)

    try:
        # Keep panel concise (Visionary -> [Innovator, Sceptic] -> SecurityArchitect) for deterministic speed
        settings_manager.enabled_agents.clear()
        settings_manager.enabled_agents.update(
            {"agent-10001", "agent-10002", "agent-10003", "agent-10008"}
        )
        settings_manager.set_orchestration_mode("scatter_gather", save=False)
        settings_manager.set_sandwich_config(["agent-10001"], ["agent-10008"], save=False)

        await client.send_mention(
            "/max_words 20 Evaluate multi-region active-active failover architecture.",
            channel_id=channel,
            thread_ts=thread_ts,
        )
        actual_thread = _resolve_thread_ts(client, thread_ts)
        msgs = client.bus.get_messages_for_thread(channel, actual_thread)
        speakers = [m.get("username") for m in msgs]
        passed = (
            "Visionary" in speakers
            and "Innovator" in speakers
            and "Sceptic" in speakers
            and speakers[-1] == "SecurityArchitect"
        )
        return ScenarioResult(
            "TC-36",
            "Scatter-Gather Parallel Topology",
            passed,
            f"Speakers sequence: {speakers}",
        )
    finally:
        settings_manager.enabled_agents.clear()
        settings_manager.enabled_agents.update(orig_enabled)
        settings_manager.set_orchestration_mode(orig_mode, save=False)
        settings_manager.set_sandwich_config(orig_start, orig_end, save=False)


async def tc_37_tight_word_limits_output_constraints_and_pii_org(
    client: SlackProtocolClient,
) -> ScenarioResult:
    channel = client.get_channel("C_TEST_37")
    thread_ts = client.get_thread_ts()

    orig_default = system_settings_manager.get_output_max_words(is_short=False)
    orig_short = system_settings_manager.get_output_max_words(is_short=True)
    try:
        # Verify configurable output_constraints propagate into build_agent_prompt
        system_settings_manager.set_output_constraints(
            default_max_words=120, short_max_words=55, save=False
        )
        built_prompt = build_agent_prompt("agent-10001")
        constraints_updated = "120" in built_prompt

        # Send <20 max_words prompt with personal PII + ORG name
        prompt = (
            "/[Visionary] /max_words 12 pii_echo_test "
            "Confirm onboarding for Alice Smith (alice.smith@example.com) at Acme Corp."
        )
        await client.send_mention(prompt, channel_id=channel, thread_ts=thread_ts)

        actual_thread = _resolve_thread_ts(client, thread_ts)
        msgs = client.bus.get_messages_for_thread(channel, actual_thread)
        if not msgs:
            return ScenarioResult(
                "TC-37",
                "Tight Word Limits, Output Constraints & PII ORG Exclusion",
                False,
                "No response posted.",
            )

        text = msgs[0].get("text", "")
        substantive = text.split("\n\n{")[0] if "\n\n{" in text else text
        word_count = len(substantive.split())
        # Zero grace buffer applies when max_words < 20
        strict_limit_ok = word_count <= 12
        org_preserved_out = "acme corp" in substantive.lower()

        # In mocked LLM inspection, confirm personal email was masked outbound while ORG stayed unmasked
        pii_masked_outbound = True
        if getattr(client, "mock", False) or getattr(client, "mocked_llm", False):
            interceptor = getattr(client, "interceptor", None)
            if interceptor and interceptor.call_history:
                last_msgs = interceptor.call_history[-1].get("messages", [])
                non_sys = " ".join(
                    str(m.get("content", ""))
                    for m in last_msgs
                    if m.get("role") != "system"
                )
                pii_masked_outbound = (
                    "alice.smith@example.com" not in non_sys.lower()
                    and "acme corp" in non_sys.lower()
                )

        passed = (
            constraints_updated
            and strict_limit_ok
            and org_preserved_out
            and pii_masked_outbound
        )
        return ScenarioResult(
            "TC-37",
            "Tight Word Limits, Output Constraints & PII ORG Exclusion",
            passed,
            f"constraints_ok={constraints_updated}, words={word_count}<=12, org_ok={org_preserved_out}, pii_masked={pii_masked_outbound}",
        )
    finally:
        system_settings_manager.set_output_constraints(
            default_max_words=orig_default,
            short_max_words=orig_short,
            save=False,
        )


async def tc_38_scribe_ssot_empty_guard_and_dm_failure(
    client: SlackProtocolClient,
) -> ScenarioResult:
    channel = client.get_channel("C_TEST_38")
    thread_ts = client.get_thread_ts()

    # Step 1: Verify empty scribe output does not post blank cards or overwrite note state
    from agents.thread_notes_agent import get_thread_note_content, update_notes_for_thread

    interceptor = getattr(client, "interceptor", None)
    if getattr(client, "mock", False) and interceptor is not None:
        interceptor.force_empty_scribe_output = True
        await update_notes_for_thread(channel, "T_EMPTY_SCRIBE_38", is_completed=False)
        interceptor.force_empty_scribe_output = False
        empty_guard_ok = (
            get_thread_note_content(channel, "T_EMPTY_SCRIBE_38") is None
            and len(client.bus.thread_notes) == 0
        )
    else:
        # In live LLM mode, ensure Scribe produces valid non-blank note and empty helper rejects blank content
        from agents.thread_notes_agent import _is_usable_scribe_output

        empty_guard_ok = (not _is_usable_scribe_output("")) and (
            not _is_usable_scribe_output("*(Synthesis unavailable: empty)*")
        )

    # Step 2: Generate a valid Scribe note, then simulate DM open failure on download
    await client.send_mention(
        "/[Security Architect] /max_words 25 Summarize key rotation policies.",
        channel_id=channel,
        thread_ts=thread_ts,
    )
    actual_thread = _resolve_thread_ts(client, thread_ts)
    note_text = await client.wait_for_scribe_note(channel, actual_thread, timeout=15.0)
    has_note = bool(note_text)

    if not client.live_mode:
        client.slack_client_double.fail_dm_open = "cannot_dm_bot"
        await client.send_block_action(
            action_id="download_scribe_notes_txt",
            value=f"{channel}:{actual_thread}",
            channel_id=channel,
            thread_ts=actual_thread,
            user_id="U_TESTER_38",
        )
        client.slack_client_double.fail_dm_open = None
        dm_failure_feedback = any(
            e.get("user_id") == "U_TESTER_38"
            and "could not open a direct message channel" in e.get("text", "").lower()
            for e in client.bus.ephemeral_messages
        )
        no_file_uploaded = len(client.bus.uploaded_files) == 0
    else:
        dm_failure_feedback = True
        no_file_uploaded = True

    passed = empty_guard_ok and has_note and dm_failure_feedback and no_file_uploaded
    return ScenarioResult(
        "TC-38",
        "Scribe SSOT Empty-Output Guard & DM Failure Feedback",
        passed,
        f"empty_guard={empty_guard_ok}, has_note={has_note}, dm_err_feedback={dm_failure_feedback}, no_file={no_file_uploaded}",
    )


ALL_SCENARIOS: list[Scenario] = [
    Scenario(
        "TC-01",
        "Direct Single Persona Invocation",
        "Core Routing",
        tc_01_single_persona,
        capability="Direct /[Persona] routing to a single agent and persona attribution footer",
    ),
    Scenario(
        "TC-02",
        "Multi-Persona Sequential Chain",
        "Core Routing",
        tc_02_multi_persona,
        capability="Sequential handoff across multiple explicit /[Agent] tags",
    ),
    Scenario(
        "TC-03",
        "Autonomous Peer Nomination",
        "Topology",
        tc_03_autonomous_nomination,
        capability="/first [Agent] entry routing and autonomous peer nomination",
    ),
    Scenario(
        "TC-04",
        "Maximum Word Count Limit",
        "Directives",
        tc_04_max_words,
        capability="/max_words directive enforcement within configured grace buffer",
    ),
    Scenario(
        "TC-05",
        "Minimum Word Count Floor",
        "Directives",
        tc_05_min_words,
        capability="/min_words directive floor enforcement on agent responses",
    ),
    Scenario(
        "TC-06",
        "Scribe Maximum Word Count",
        "Directives",
        tc_06_scribe_max_words,
        capability="/scribe_max_words word limit on Live Discussion Scribe synthesis",
    ),
    Scenario(
        "TC-07",
        "Scribe Minimum Word Count",
        "Directives",
        tc_07_scribe_min_words,
        capability="/scribe_min_words word floor on Live Discussion Scribe synthesis",
    ),
    Scenario(
        "TC-08",
        "History Window Slicing",
        "Context",
        tc_08_history_slicing,
        capability="/history N thread context window slicing across multi-turn threads",
    ),
    Scenario(
        "TC-09",
        "Agent Exclusion Directive",
        "Governance",
        tc_09_agent_exclusion,
        capability="/exclude /[Agent] filtering from active panel participation",
    ),
    Scenario(
        "TC-10",
        "Constraint Revocation",
        "Directives",
        tc_10_revoke_constraints,
        capability="Admin /revoke_constraint clearing active thread constraints",
    ),
    Scenario(
        "TC-11",
        "Multi-Round Debate Cycling",
        "Topology",
        tc_11_multi_round,
        capability="/rounds N multi-round sequential debate cycling",
    ),
    Scenario(
        "TC-12",
        "Live Web Research Skill",
        "Skills",
        tc_12_web_research,
        capability="web_research skill invocation and tool-backed response synthesis",
    ),
    Scenario(
        "TC-13",
        "Document Reading in agents_resources/",
        "Skills",
        tc_13_document_reader_compliance,
        capability="document_reader skill reading compliance_guidelines.txt from agents_resources/",
    ),
    Scenario(
        "TC-14",
        "Document Authoring in agents_resources/",
        "Skills",
        tc_14_document_authoring,
        capability="document_authoring skill with HITL approval writing files to agents_resources/",
    ),
    Scenario(
        "TC-15",
        "Document Reader (sys_spec.txt)",
        "Skills",
        tc_15_document_reader_attachment,
        capability="document_reader skill extracting exact specs from agents_resources/sys_spec.txt",
    ),
    Scenario(
        "TC-16",
        "Unauthorized Skill Access Rejection",
        "Security",
        tc_16_unauthorized_tool_rejection,
        capability="Skill RBAC enforcement blocking unauthorized document_authoring writes",
    ),
    Scenario(
        "TC-17",
        "Granular Agent Skill Grant",
        "Permissions",
        tc_17_grant_skill,
        capability="Admin /grant_skill granting web_research to a specific agent in-thread",
    ),
    Scenario(
        "TC-18",
        "Thread-Global Skill Grant",
        "Permissions",
        tc_18_grant_for_all,
        capability="Admin /grant_skill_for_all granting a skill across the whole thread",
    ),
    Scenario(
        "TC-19",
        "Thread-Global Skill Revocation",
        "Permissions",
        tc_19_revoke_for_all,
        capability="Admin /revoke_skill_for_all revoking thread-global skill grants",
    ),
    Scenario(
        "TC-20",
        "Skills Directory Query",
        "Discovery",
        tc_20_list_skills,
        capability="/list_skills skill catalog discovery without invoking debate agents",
    ),
    Scenario(
        "TC-21",
        "Delayed Progress & In-Place Edit",
        "UX",
        tc_21_delayed_progress_inplace_edit,
        capability="Delayed tool-status indicator and Slack chat.update in-place replacement",
    ),
    Scenario(
        "TC-22",
        "Scribe Download Button (.txt in DM)",
        "Interactivity",
        tc_22_scribe_download_dm,
        capability="Interactive download_scribe_notes_txt Block Kit button delivering .txt via DM",
    ),
    Scenario(
        "TC-23",
        "Expense Circuit Breaker Tripping",
        "Governance",
        tc_23_circuit_breaker,
        capability="Thread cost circuit breaker halting execution when spend threshold is exceeded",
    ),
    Scenario(
        "TC-24",
        "Usage Channel Token Tracking",
        "Governance",
        tc_24_usage_channel,
        capability="usage_tracker prompt/completion token accounting per thread and model",
    ),
    Scenario(
        "TC-25",
        "Stop Directive Concurrency",
        "Safety",
        tc_25_stop_concurrency,
        capability="/stop mid-flight discussion cancellation and remaining-agent queue clearing",
    ),
    Scenario(
        "TC-26",
        "Mid-Flight Superseding, Language & Tag Halts",
        "Concurrency & NLP",
        tc_26_superseding_language_and_tag_validation,
        capability="Generation-based mid-flight superseding, Croatian language lock, and unknown tag halt",
    ),
    Scenario(
        "TC-27",
        "Default Workflow on Untagged Mention",
        "Topology",
        tc_27_default_workflow_untagged,
        capability="Default panel workflow initiation on untagged user mentions",
    ),
    Scenario(
        "TC-28",
        "Declarative Admin RBAC Gate & Audit",
        "Security",
        tc_28_admin_rbac_gate,
        capability="Non-admin directive rejection with CRITICAL audit log vs admin approval",
    ),
    Scenario(
        "TC-29",
        "Combined Grant Directive + Inline Continuation",
        "Permissions",
        tc_29_combined_grant_inline_continuation,
        capability="Inline /grant_skill_for_all directive chained with prompt continuation",
    ),
    Scenario(
        "TC-30",
        "Skill Routing Filter & Pure-Skill Short-Circuit",
        "Routing",
        tc_30_skill_routing_filter_and_short_circuit,
        capability="filter_unauthorized_skill_agents routing filter and pure-skill short-circuit",
    ),
    Scenario(
        "TC-31",
        "Exhausted Queue Recovery & active_prompt Tracking",
        "State",
        tc_31_exhausted_queue_recovery_and_active_prompt,
        capability="Session recovery after queue exhaustion while preserving initial_prompt vs active_prompt",
    ),
    Scenario(
        "TC-32",
        "InternalRevisor Evidence Isolation & Stage 2",
        "Verification",
        tc_32_internal_revisor_ground_truth_and_confirmation,
        capability="InternalRevisor evidence_store ground-truth check and Stage 2 contradiction confirmation",
    ),
    Scenario(
        "TC-33",
        "Challenger & Arbitrator Convergence Stress-Test",
        "Topology",
        tc_33_challenger_and_arbitrator_stress_test,
        capability="Dialectical convergence detection triggering Challenger and Arbitrator intervention",
    ),
    Scenario(
        "TC-34",
        "HITL Clarification Suspend & Resume",
        "Interactivity",
        tc_34_hitl_clarification_suspend_resume,
        capability="request_user_clarification thread pause and seamless resume on human reply",
    ),
    Scenario(
        "TC-35",
        "External MCP Bridge & Destructive HITL Gate",
        "MCP & Security",
        tc_35_external_mcp_bridge_and_hitl,
        capability="External MCP tool bridge (read + mutating drop_table gated by hitl_approve_tool)",
    ),
    Scenario(
        "TC-36",
        "Scatter-Gather Parallel Topology",
        "Topology",
        tc_36_scatter_gather_topology,
        capability="scatter_gather parallel fan-out branches and end-chain gather synthesis",
    ),
    Scenario(
        "TC-37",
        "Tight Word Limits, Output Constraints & PII ORG",
        "Directives & Privacy",
        tc_37_tight_word_limits_output_constraints_and_pii_org,
        capability="Strict zero-grace max_words < 20, configurable output_constraints, and Presidio ORG preservation",
    ),
    Scenario(
        "TC-38",
        "Scribe SSOT Empty Guard & DM Failure Feedback",
        "Scribe & Interactivity",
        tc_38_scribe_ssot_empty_guard_and_dm_failure,
        capability="Scribe empty-output SSOT guard and ephemeral DM-open failure error feedback",
    ),
]
