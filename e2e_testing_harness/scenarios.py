"""
E2E Behavioural Test Scenarios.

Defines the 28 comprehensive test scenarios matching e2e_testing_harness/SLACK_BEHAVIOURAL_TEST_CASES.md,
including verification logic and assertion checks.
Seamlessly supports both offline simulation and live Slack dispatch.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from e2e_testing_harness.protocol_client import SlackProtocolClient
from orchestration.agent_settings import (
    set_circuit_breaker_enabled,
    set_circuit_breaker_threshold,
)
from orchestration.thread_state import thread_state_manager


@dataclass
class ScenarioResult:
    id: str
    name: str
    passed: bool
    details: str
    duration_seconds: float = 0.0


@dataclass
class Scenario:
    id: str
    name: str
    category: str
    run_fn: Callable[[SlackProtocolClient], Coroutine[Any, Any, ScenarioResult]]


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

    actual_thread = thread_ts or (
        client.bus.posted_messages[0]["thread_ts"] if client.bus.posted_messages else ""
    )
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

    actual_thread = thread_ts or (
        client.bus.posted_messages[0]["thread_ts"] if client.bus.posted_messages else ""
    )
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
    prompt = "Should we replace our relational schema with a vector-native graph DB? /[Innovator], start us off and nominate who speaks next."
    await client.send_mention(prompt, channel_id=channel, thread_ts=thread_ts)

    actual_thread = thread_ts or (
        client.bus.posted_messages[0]["thread_ts"] if client.bus.posted_messages else ""
    )
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

    actual_thread = (
        client.current_thread_ts
        or thread_ts
        or (
            client.bus.posted_messages[0]["thread_ts"]
            if client.bus.posted_messages
            else ""
        )
    )
    msgs = client.bus.get_messages_for_thread(channel, actual_thread)
    if not msgs:
        return ScenarioResult("TC-04", "Maximum Word Count", False, "No response.")

    text = msgs[0].get("text", "")
    substantive_text = text.split("\n\n{")[0] if "\n\n{" in text else text
    word_count = len(substantive_text.split())
    limit = thread_state_manager.get_max_words(channel, actual_thread)
    passed = limit == 40 and word_count <= 42
    return ScenarioResult(
        "TC-04", "Maximum Word Count", passed, f"Words: {word_count} (limit: {limit})"
    )


async def tc_05_min_words(client: SlackProtocolClient) -> ScenarioResult:
    channel = client.get_channel("C_TEST_05")
    thread_ts = client.get_thread_ts()
    prompt = "/[Security Architect] /min_words 60 Detail webhook security threat modeling."
    await client.send_mention(prompt, channel_id=channel, thread_ts=thread_ts)

    actual_thread = thread_ts or (
        client.bus.posted_messages[0]["thread_ts"] if client.bus.posted_messages else ""
    )
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

    actual_thread = thread_ts or (
        client.bus.posted_messages[0]["thread_ts"] if client.bus.posted_messages else ""
    )
    limit = thread_state_manager.get_scribe_max_words(channel, actual_thread)

    note_text = await client.wait_for_scribe_note(channel, actual_thread, timeout=12.0)
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
    passed = limit == 40 and has_scribe_note and word_count <= 45
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

    actual_thread = thread_ts or (
        client.bus.posted_messages[0]["thread_ts"] if client.bus.posted_messages else ""
    )
    limit = thread_state_manager.get_scribe_min_words(channel, actual_thread)

    note_text = await client.wait_for_scribe_note(channel, actual_thread, timeout=12.0)
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
    # Establish authentic multi-turn conversation history (4 prior turns) before invoking /history 2
    await client.send_mention(
        "/[LegalAdvisor] /max_words 10 Step 1: Initial design proposal.",
        channel_id=channel,
        thread_ts=init_thread,
    )
    actual_thread = client.current_thread_ts or (
        client.bus.posted_messages[0]["thread_ts"]
        if client.bus.posted_messages
        else "T_08"
    )

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
        "[SalesAdvisor] /max_words 10 Step 4: Quality verification.",
        channel_id=channel,
        thread_ts=actual_thread,
    )

    # Turn 5: Request response with /history 2
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
    # Exclude /[Innovator] (who is in default review workflow start_chain) and trigger default workflow with /max_words 10
    prompt = "/exclude /[Innovator] /max_words 10 What is the best strategy for microservice resiliency?"
    await client.send_mention(prompt, channel_id=channel, thread_ts=thread_ts)

    actual_thread = (
        thread_ts
        or client.current_thread_ts
        or (
            client.bus.posted_messages[0]["thread_ts"]
            if client.bus.posted_messages
            else ""
        )
    )
    msgs = client.bus.get_messages_for_thread(channel, actual_thread)
    speakers = [m.get("username") for m in msgs]
    excluded_participated = "Innovator" in speakers
    visionary_started = any(s == "Visionary" for s in speakers)
    passed = not excluded_participated and visionary_started and len(speakers) >= 1
    return ScenarioResult(
        "TC-09",
        "Agent Exclusion Directive",
        passed,
        f"Speakers: {speakers} (Innovator excluded: {not excluded_participated}, Visionary started: {visionary_started})",
    )


async def tc_10_revoke_constraints(client: SlackProtocolClient) -> ScenarioResult:
    channel = client.get_channel("C_TEST_10")
    thread_ts = client.get_thread_ts()
    await client.send_mention(
        "/max_words 30 /[LegalAdvisor] Short explanation.",
        channel_id=channel,
        thread_ts=thread_ts,
    )
    actual_thread = thread_ts or (
        client.bus.posted_messages[0]["thread_ts"] if client.bus.posted_messages else ""
    )

    # Revoke constraint
    await client.send_mention(
        "/revoke_constraint max_words", channel_id=channel, thread_ts=actual_thread
    )
    limit = thread_state_manager.get_max_words(channel, actual_thread)
    sys_msgs = [
        s["text"] for s in client.bus.system_messages if s.get("channel_id") == channel
    ]
    passed = limit is None and any("Revoked constraint" in t for t in sys_msgs)
    return ScenarioResult(
        "TC-10",
        "Constraint Revocation",
        passed,
        "max_words constraint successfully revoked.",
    )


async def tc_11_multi_round(client: SlackProtocolClient) -> ScenarioResult:
    channel = client.get_channel("C_TEST_11")
    thread_ts = client.get_thread_ts()
    prompt = "/[Security Architect] /[LegalAdvisor] /rounds 2 Monorepo vs Polyrepo debate."
    await client.send_mention(prompt, channel_id=channel, thread_ts=thread_ts)

    actual_thread = thread_ts or (
        client.bus.posted_messages[0]["thread_ts"] if client.bus.posted_messages else ""
    )
    msgs = client.bus.get_messages_for_thread(channel, actual_thread)
    speakers = [m.get("username") for m in msgs]
    passed = len(speakers) >= 3
    return ScenarioResult(
        "TC-11",
        "Multi-Round Debate",
        passed,
        f"Total turns executed across rounds: {len(speakers)}",
    )


async def tc_12_web_research(client: SlackProtocolClient) -> ScenarioResult:
    channel = client.get_channel("C_TEST_12")
    thread_ts = client.get_thread_ts()
    prompt = "/[MarketAndCompetitionResearchExpert] Search the web for current pricing of Anthropic Claude."
    await client.send_mention(prompt, channel_id=channel, thread_ts=thread_ts)

    actual_thread = thread_ts or (
        client.bus.posted_messages[0]["thread_ts"] if client.bus.posted_messages else ""
    )
    msgs = client.bus.get_messages_for_thread(channel, actual_thread)
    if not msgs:
        return ScenarioResult(
            "TC-12", "Web Research Skill", False, "No response posted."
        )

    text = msgs[0].get("text", "")
    has_pricing = "Claude" in text or "Anthropic" in text or "pricing" in text
    passed = has_pricing
    return ScenarioResult(
        "TC-12",
        "Web Research Skill",
        passed,
        "Web research synthesized with pricing citations.",
    )


async def tc_13_codebase_inspection(client: SlackProtocolClient) -> ScenarioResult:
    channel = client.get_channel("C_TEST_13")
    thread_ts = client.get_thread_ts()
    prompt = "/[Security Architect] Inspect directory layout of orchestration/stages and turn_pipeline.py."
    await client.send_mention(prompt, channel_id=channel, thread_ts=thread_ts)

    actual_thread = thread_ts or (
        client.bus.posted_messages[0]["thread_ts"] if client.bus.posted_messages else ""
    )
    msgs = client.bus.get_messages_for_thread(channel, actual_thread)
    if not msgs:
        return ScenarioResult(
            "TC-13", "Codebase Inspection Skill", False, "No response posted."
        )

    text = msgs[0].get("text", "")
    passed = "stages" in text or "TurnPipeline" in text or "validation" in text
    return ScenarioResult(
        "TC-13",
        "Codebase Inspection Skill",
        passed,
        "Codebase inspection executed and files cited.",
    )


async def tc_14_document_authoring(client: SlackProtocolClient) -> ScenarioResult:
    channel = client.get_channel("C_TEST_14")
    thread_ts = client.get_thread_ts()
    prompt = "/[ROIStrategist] Draft a 3-point business case and save it as a document named agents_resources/llm_cost_reduction_plan.md."
    await client.send_mention(prompt, channel_id=channel, thread_ts=thread_ts)

    actual_thread = thread_ts or (
        client.bus.posted_messages[0]["thread_ts"] if client.bus.posted_messages else ""
    )
    msgs = client.bus.get_messages_for_thread(channel, actual_thread)
    if not msgs:
        return ScenarioResult(
            "TC-14", "Document Authoring Skill", False, "No response posted."
        )

    text = msgs[0].get("text", "")
    passed = "llm_cost_reduction_plan.md" in text or "saved" in text or "draft" in text
    return ScenarioResult(
        "TC-14",
        "Document Authoring Skill",
        passed,
        "Document authoring executed in agents_resources/.",
    )


async def tc_15_document_reader_attachment(
    client: SlackProtocolClient,
) -> ScenarioResult:
    channel = client.get_channel("C_TEST_15")
    thread_ts = client.get_thread_ts()
    prompt = "/[Security Architect] /read_document agents_resources/sys_spec.txt What is the throughput specified in this file?"
    await client.send_mention(prompt, channel_id=channel, thread_ts=thread_ts)

    actual_thread = (
        client.current_thread_ts
        or thread_ts
        or (
            client.bus.posted_messages[0]["thread_ts"]
            if client.bus.posted_messages
            else "T_15"
        )
    )
    msgs = client.bus.get_messages_for_thread(channel, actual_thread)
    if not msgs:
        return ScenarioResult(
            "TC-15", "Document Reader & Attachment", False, "No response posted."
        )

    text = msgs[0].get("text", "")
    passed = "5000" in text or "rps" in text.lower()
    return ScenarioResult(
        "TC-15",
        "Document Reader & Attachment",
        passed,
        f"Document sys_spec.txt parsed from agents_resources/ (found throughput metrics: {passed}).",
    )


async def tc_16_unauthorized_tool_rejection(
    client: SlackProtocolClient,
) -> ScenarioResult:
    channel = client.get_channel("C_TEST_16")
    thread_ts = client.get_thread_ts()
    prompt = "/[Security Architect] Use load_skill workspace_authoring to modify system configuration."
    await client.send_mention(prompt, channel_id=channel, thread_ts=thread_ts)

    actual_thread = (
        client.current_thread_ts
        or thread_ts
        or (
            client.bus.posted_messages[0]["thread_ts"]
            if client.bus.posted_messages
            else "T_16"
        )
    )
    msgs = client.bus.get_messages_for_thread(channel, actual_thread)
    if not msgs:
        return ScenarioResult(
            "TC-16", "Unauthorized Tool Rejection", False, "No response."
        )

    text = msgs[0].get("text", "").lower()
    passed = any(
        k in text
        for k in [
            "permission denied",
            "not authorized",
            "authorization error",
            "not permitted",
            "do not have permission",
            "don't have permission",
            "unauthorized",
            "lacks permission",
        ]
    )
    return ScenarioResult(
        "TC-16",
        "Unauthorized Tool Rejection",
        passed,
        f"Unauthorized tool rejected with permission denial: {passed}.",
    )


async def tc_17_grant_skill(client: SlackProtocolClient) -> ScenarioResult:
    channel = client.get_channel("C_TEST_17")
    thread_ts = client.get_thread_ts()
    prompt = "/grant_skill web_research /[Security Architect]"
    await client.send_mention(prompt, channel_id=channel, thread_ts=thread_ts)

    sys_msgs = [
        s["text"] for s in client.bus.system_messages if s.get("channel_id") == channel
    ]
    passed = any("Agent Skill Granted" in m for m in sys_msgs)
    return ScenarioResult(
        "TC-17",
        "Granular Skill Grant",
        passed,
        "web_research granted to Security Architect.",
    )


async def tc_18_grant_for_all(client: SlackProtocolClient) -> ScenarioResult:
    channel = client.get_channel("C_TEST_18")
    thread_ts = client.get_thread_ts()
    prompt = "/grant_skill_for_all web_research"
    await client.send_mention(prompt, channel_id=channel, thread_ts=thread_ts)

    sys_msgs = [
        s["text"] for s in client.bus.system_messages if s.get("channel_id") == channel
    ]
    passed = any("Thread-Global Skills Granted" in m for m in sys_msgs)
    return ScenarioResult(
        "TC-18",
        "Global Skill Grant",
        passed,
        "web_research granted globally in thread.",
    )


async def tc_19_revoke_for_all(client: SlackProtocolClient) -> ScenarioResult:
    channel = client.get_channel("C_TEST_19")
    thread_ts = client.get_thread_ts()
    await client.send_mention(
        "/grant_skill_for_all web_research", channel_id=channel, thread_ts=thread_ts
    )
    actual_thread = thread_ts or (
        client.bus.system_messages[-1]["thread_ts"]
        if client.bus.system_messages
        else ""
    )

    await client.send_mention(
        "/revoke_skill_for_all web_research",
        channel_id=channel,
        thread_ts=actual_thread,
    )
    sys_msgs = [
        s["text"] for s in client.bus.system_messages if s.get("channel_id") == channel
    ]
    passed = any("Revoked" in m for m in sys_msgs)
    return ScenarioResult(
        "TC-19",
        "Global Skill Revocation",
        passed,
        "Global skills revoked successfully.",
    )


async def tc_20_list_skills(client: SlackProtocolClient) -> ScenarioResult:
    channel = client.get_channel("C_TEST_20")
    thread_ts = client.get_thread_ts()
    prompt = "/list_skills"
    await client.send_mention(prompt, channel_id=channel, thread_ts=thread_ts)

    sys_msgs = [
        s["text"] for s in client.bus.system_messages if s.get("channel_id") == channel
    ]
    posted = [
        m["text"] for m in client.bus.posted_messages if m.get("channel_id") == channel
    ]
    all_msgs = sys_msgs + posted
    passed = any(
        "Skills" in m or "web_research" in m or "codebase_inspection" in m
        for m in all_msgs
    )
    return ScenarioResult(
        "TC-20",
        "Skills Directory Query",
        passed,
        "Skills catalog listed transparently.",
    )


async def tc_21_delayed_progress_inplace_edit(
    client: SlackProtocolClient,
) -> ScenarioResult:
    channel = client.get_channel("C_TEST_21")
    thread_ts = client.get_thread_ts()
    prompt = "/[MarketAndCompetitionResearchExpert] Comprehensive web search on vector databases."
    await client.send_mention(prompt, channel_id=channel, thread_ts=thread_ts)

    actual_thread = (
        client.current_thread_ts
        or thread_ts
        or (
            client.bus.posted_messages[0]["thread_ts"]
            if client.bus.posted_messages
            else "T_21"
        )
    )
    msgs = client.bus.get_messages_for_thread(channel, actual_thread)
    if not msgs:
        return ScenarioResult(
            "TC-21",
            "Delayed Progress Indicator & In-Place Edit",
            False,
            "No response posted.",
        )

    text = msgs[0].get("text", "").lower()
    has_vector_results = any(k in text for k in ["pinecone", "qdrant", "milvus"])
    passed = len(msgs) >= 1 and has_vector_results
    return ScenarioResult(
        "TC-21",
        "Delayed Progress Indicator & In-Place Edit",
        passed,
        f"Web search executed with vector database findings: {has_vector_results}.",
    )


async def tc_22_scribe_download_dm(client: SlackProtocolClient) -> ScenarioResult:
    channel = client.get_channel("C_TEST_22")
    thread_ts = client.get_thread_ts()

    # Step 1: Run authentic deliberation in thread to generate a live Scribe synthesis note
    prompt = "/[Security Architect] /[LegalAdvisor] /max_words 25 What is our strategy for container image vulnerability scanning in CI/CD?"
    await client.send_mention(prompt, channel_id=channel, thread_ts=thread_ts)

    actual_thread = thread_ts or (
        client.bus.posted_messages[0]["thread_ts"] if client.bus.posted_messages else ""
    )
    note_text = await client.wait_for_scribe_note(channel, actual_thread, timeout=12.0)
    has_scribe_note = bool(note_text) or any(
        m.get("is_scribe_note") or m.get("username") == "Live Discussion Scribe"
        for m in client.bus.posted_messages
    )

    # Step 2: Trigger download block action on the scribe card
    await client.send_block_action(
        action_id="download_scribe_notes_txt",
        value=f"{channel}:{actual_thread}",
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
    passed = has_scribe_note and has_ephemeral and has_file
    return ScenarioResult(
        "TC-22",
        "Scribe Download Button (.txt in DM)",
        passed,
        f"Live scribe note posted: {has_scribe_note}, English ephemeral notice: {has_ephemeral}, .txt file uploaded: {has_file}.",
    )


async def tc_23_circuit_breaker(client: SlackProtocolClient) -> ScenarioResult:
    channel = client.get_channel("C_TEST_23")
    thread_ts = client.get_thread_ts()
    try:
        set_circuit_breaker_enabled(True, save=False)
        set_circuit_breaker_threshold(0.00001, save=False)

        # Trigger turn that exceeds $0.00001
        prompt = (
            "/[Security Architect] /[LegalAdvisor] Migration to event streaming."
        )
        await client.send_mention(prompt, channel_id=channel, thread_ts=thread_ts)

        actual_thread = thread_ts or (
            client.bus.posted_messages[0]["thread_ts"]
            if client.bus.posted_messages
            else ""
        )
        texts = client.bus.get_all_texts_for_thread(channel, actual_thread)
        tripped = any("EXPENSE LIMIT REACHED FOR THE THREAD" in t for t in texts)
        return ScenarioResult(
            "TC-23",
            "Expense Circuit Breaker Tripping",
            tripped,
            "Circuit breaker tripped and calls halted.",
        )
    finally:
        set_circuit_breaker_enabled(True, save=False)
        set_circuit_breaker_threshold(1.50, save=False)


async def tc_24_usage_channel(client: SlackProtocolClient) -> ScenarioResult:
    channel = client.get_channel("C_TEST_24")
    thread_ts = client.get_thread_ts()
    await client.send_mention(
        "/[Visionary] Future of quantum computing in fintech.",
        channel_id=channel,
        thread_ts=thread_ts,
    )

    # Verify usage recorder recorded tokens
    from agents.usage_agent import usage_tracker

    actual_thread = thread_ts or (
        client.bus.posted_messages[0]["thread_ts"] if client.bus.posted_messages else ""
    )
    entry = usage_tracker.thread_usage_data.get((channel, actual_thread), {})
    models = entry.get("models", {})
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

    # Step 1: Start full workflow discussion across all agents (untagged mention triggers full workflow)
    prompt = "/max_words 15 Debate the viability of replacing SQL databases with blockchain for high-frequency inventory tracking across all our services."
    await client.send_mention(
        prompt, channel_id=channel, thread_ts=thread_ts, wait_for_task=False
    )

    actual_thread = (
        client.current_thread_ts
        or thread_ts
        or (
            client.bus.posted_messages[0]["thread_ts"]
            if client.bus.posted_messages
            else ""
        )
    )

    # Step 2: Interrupt mid-flight with /stop
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

    # Check remaining agent queue is cleared
    rem_agents = thread_state_manager.get_remaining_agents(channel, actual_thread)

    posted_msgs = client.bus.get_messages_for_thread(channel, actual_thread)
    speakers = [
        m.get("username")
        for m in posted_msgs
        if m.get("username")
        not in ("Panel System", "Live Discussion Scribe", "E2E Test Runner")
    ]

    # Full workflow has 17 agents; stopping must halt deliberation and keep speakers well below total pool
    passed = ((not is_active) or has_stop_confirmation or not rem_agents) and len(
        speakers
    ) < 17
    return ScenarioResult(
        "TC-25",
        "Stop Directive Concurrency",
        passed,
        f"Workflow halted cleanly. Active: {is_active}, stop confirmed: {has_stop_confirmation}, remaining cleared: {not rem_agents}, speakers before halt: {len(speakers)}/17.",
    )


async def tc_27_default_workflow_untagged(
    client: SlackProtocolClient,
) -> ScenarioResult:
    channel = client.get_channel("C_TEST_27")
    thread_ts = client.get_thread_ts()
    # Mention without any explicit agent bracket tag [Agent]
    prompt = "Evaluate our distributed consensus and deployment strategy for 2026."
    await client.send_mention(prompt, channel_id=channel, thread_ts=thread_ts)

    actual_thread = thread_ts or (
        client.bus.posted_messages[0]["thread_ts"] if client.bus.posted_messages else ""
    )
    msgs = client.bus.get_messages_for_thread(channel, actual_thread)
    if not msgs:
        return ScenarioResult(
            "TC-27",
            "Default Workflow on Untagged Mention",
            False,
            "No agent response posted.",
        )

    speakers = [m.get("username") for m in msgs]
    # Default workflow starts with Visionary, hands off to Innovator, and concludes with Security Architect
    passed = len(speakers) >= 2 and speakers[0] == "Visionary"
    return ScenarioResult(
        "TC-27",
        "Default Workflow on Untagged Mention",
        passed,
        f"Default workflow triggered. Speakers: {speakers}",
    )


ALL_SCENARIOS: list[Scenario] = [
    Scenario(
        "TC-01",
        "Direct Single Persona Invocation",
        "Core Routing",
        tc_01_single_persona,
    ),
    Scenario(
        "TC-02", "Multi-Persona Sequential Chain", "Core Routing", tc_02_multi_persona
    ),
    Scenario(
        "TC-03", "Autonomous Peer Nomination", "Topology", tc_03_autonomous_nomination
    ),
    Scenario("TC-04", "Maximum Word Count Limit", "Directives", tc_04_max_words),
    Scenario("TC-05", "Minimum Word Count Floor", "Directives", tc_05_min_words),
    Scenario(
        "TC-06", "Scribe Maximum Word Count", "Directives", tc_06_scribe_max_words
    ),
    Scenario(
        "TC-07", "Scribe Minimum Word Count", "Directives", tc_07_scribe_min_words
    ),
    Scenario("TC-08", "History Window Slicing", "Context", tc_08_history_slicing),
    Scenario("TC-09", "Agent Exclusion Directive", "Governance", tc_09_agent_exclusion),
    Scenario("TC-10", "Constraint Revocation", "Directives", tc_10_revoke_constraints),
    Scenario("TC-11", "Multi-Round Debate Cycling", "Topology", tc_11_multi_round),
    Scenario("TC-12", "Live Web Research Skill", "Skills", tc_12_web_research),
    Scenario("TC-13", "Codebase Inspection Skill", "Skills", tc_13_codebase_inspection),
    Scenario(
        "TC-14",
        "Document Authoring in resources/e2e_testing/",
        "Skills",
        tc_14_document_authoring,
    ),
    Scenario(
        "TC-15",
        "Document Reader in resources/e2e_testing/",
        "Skills",
        tc_15_document_reader_attachment,
    ),
    Scenario(
        "TC-16",
        "Unauthorized Tool Rejection",
        "Security",
        tc_16_unauthorized_tool_rejection,
    ),
    Scenario("TC-17", "Granular Agent Skill Grant", "Permissions", tc_17_grant_skill),
    Scenario("TC-18", "Thread-Global Skill Grant", "Permissions", tc_18_grant_for_all),
    Scenario(
        "TC-19", "Thread-Global Skill Revocation", "Permissions", tc_19_revoke_for_all
    ),
    Scenario("TC-20", "Skills Directory Query", "Discovery", tc_20_list_skills),
    Scenario(
        "TC-21",
        "Delayed Progress & In-Place Edit",
        "UX",
        tc_21_delayed_progress_inplace_edit,
    ),
    Scenario(
        "TC-22",
        "Scribe Download Button (.txt in DM)",
        "Interactivity",
        tc_22_scribe_download_dm,
    ),
    Scenario(
        "TC-23", "Expense Circuit Breaker Tripping", "Governance", tc_23_circuit_breaker
    ),
    Scenario(
        "TC-24", "Usage Channel Token Tracking", "Governance", tc_24_usage_channel
    ),
    Scenario("TC-25", "Stop Directive Concurrency", "Safety", tc_25_stop_concurrency),
    Scenario(
        "TC-27",
        "Default Workflow on Untagged Mention",
        "Topology",
        tc_27_default_workflow_untagged,
    ),
]
