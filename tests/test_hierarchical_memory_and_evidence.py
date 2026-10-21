"""
Unit tests for Hierarchical 3-Tier Memory and Shared Round Evidence (PrePublish 8 Phase 3 / Epic C5).
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from orchestration.state.evidence_store import RoundEvidenceStore, EvidenceItem
from orchestration.thread_state import thread_state_manager
from nlp.message_builder import build_llm_messages


def test_round_evidence_store_basic_caching_and_hashing():
    store = RoundEvidenceStore()
    channel_id = "C123"
    thread_ts = "100.0"

    # Non-cacheable tool
    assert not store.is_cacheable_tool("post_slack_message")
    item = store.add_evidence(
        channel_id=channel_id,
        thread_ts=thread_ts,
        tool_name="post_slack_message",
        arguments={"msg": "hi"},
        result="ok",
        agent_key="agent-1",
    )
    assert item is None
    assert store.get_evidence(channel_id, thread_ts, "post_slack_message", {"msg": "hi"}) is None

    # Cacheable tool
    assert store.is_cacheable_tool("search_web")
    item1 = store.add_evidence(
        channel_id=channel_id,
        thread_ts=thread_ts,
        tool_name="search_web",
        arguments={"query": "quantum computing", "limit": 5},
        result="Quantum computing info...",
        agent_key="agent-1",
    )
    assert item1 is not None
    assert item1.tool_name == "search_web"
    assert item1.agent_key == "agent-1"

    # Argument order invariance
    cached = store.get_evidence(
        channel_id,
        thread_ts,
        "search_web",
        {"limit": 5, "query": "quantum computing"},
    )
    assert cached is not None
    assert cached.result == "Quantum computing info..."
    assert cached.args_hash == item1.args_hash


def test_round_evidence_store_format_and_clear():
    store = RoundEvidenceStore()
    channel_id = "C123"
    thread_ts = "200.0"

    store.add_evidence(
        channel_id=channel_id,
        thread_ts=thread_ts,
        tool_name="query_knowledge_base",
        arguments={"query": "company vacation policy"},
        result="Vacation policy: 25 days.",
        agent_key="agent-researcher",
    )

    block = store.format_evidence_block(channel_id, thread_ts)
    assert "Tool: query_knowledge_base" in block
    assert "agent-researcher" in block
    assert "Vacation policy: 25 days." in block

    store.clear_thread_evidence(channel_id, thread_ts)
    assert store.format_evidence_block(channel_id, thread_ts) is None


def test_message_builder_episodic_summary_and_shared_evidence():
    messages = build_llm_messages(
        system_prompt="You are a helpful assistant.",
        effective_history=[
            {"role": "user", "content": "What was decided earlier?"},
        ],
        prompt="What was decided earlier?",
        is_bot_trigger=False,
        agent_key="agent-1",
        agent_cfg={"model": "gpt-4o", "username": "Tester"},
        episodic_summary="Round 1 concluded that Python 3.12 is chosen.",
        shared_round_evidence="The following verified evidence was gathered:\n1. [Tool: search_web | Agent: agent-1]:\n   Result: Found Python 3.12 release notes.",
    )

    # First message is system prompt
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == "You are a helpful assistant."

    # Look for episodic summary and evidence block in user messages
    found_episodic = False
    found_evidence = False
    for m in messages:
        if m["role"] == "user":
            c = m["content"]
            if "<episodic_thread_summary>" in c:
                found_episodic = True
                assert "Round 1 concluded that Python 3.12 is chosen." in c
            if "<shared_round_evidence>" in c:
                found_evidence = True
                assert "Found Python 3.12 release notes." in c

    assert found_episodic
    assert found_evidence


@pytest.mark.anyio
async def test_execution_stage_evidence_caching():
    channel_id = "C999"
    thread_ts = "1234.5678"

    # Pre-populate round evidence in thread_state_manager
    thread_state_manager.add_round_evidence(
        channel_id=channel_id,
        thread_ts=thread_ts,
        tool_name="search_web",
        arguments={"query": "specs"},
        result="Specs document v2 found",
        agent_key="agent-1",
    )

    # Verify cached retrieval
    cached = thread_state_manager.evidence.get_evidence(
        channel_id,
        thread_ts,
        "search_web",
        {"query": "specs"},
    )
    assert cached is not None
    assert cached.result == "Specs document v2 found"
    assert cached.agent_key == "agent-1"
