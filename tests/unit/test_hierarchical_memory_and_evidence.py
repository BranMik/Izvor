"""
Unit tests for Hierarchical 3-Tier Memory and Shared Round Evidence (PrePublish 8 Phase 3 / Epic C5).
Updated for Pre-Publish 9 Phase P0 (TKT-903): tool alignment, bounded LRU, agent scoping, error rejection.
"""

from __future__ import annotations

import json

import pytest

from nlp.message_builder import build_llm_messages
from orchestration.state.evidence_store import RoundEvidenceStore
from orchestration.thread_state import thread_state_manager


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

    # Cacheable tool (web_search)
    assert store.is_cacheable_tool("web_search")
    item1 = store.add_evidence(
        channel_id=channel_id,
        thread_ts=thread_ts,
        tool_name="web_search",
        arguments={"query": "quantum computing", "limit": 5},
        result="Quantum computing info...",
        agent_key="agent-1",
    )
    assert item1 is not None
    assert item1.tool_name == "web_search"
    assert item1.agent_key == "agent-1"

    # Argument order invariance
    cached = store.get_evidence(
        channel_id,
        thread_ts,
        "web_search",
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
        tool_name="read_document",
        arguments={"path": "company_policy.md"},
        result="Vacation policy: 25 days.",
        agent_key="agent-researcher",
    )

    block = store.format_evidence_block(channel_id, thread_ts)
    assert block is not None
    assert "Tool: read_document" in block
    assert "agent-researcher" in block
    assert "Vacation policy: 25 days." in block
    assert "untrusted external data" in block

    store.clear_thread_evidence(channel_id, thread_ts)
    assert store.format_evidence_block(channel_id, thread_ts) is None


def test_round_evidence_store_rejects_errors_and_empty():
    store = RoundEvidenceStore()
    channel_id = "C123"
    thread_ts = "250.0"

    # Empty result
    assert store.add_evidence(channel_id, thread_ts, "web_search", {"q": "test"}, "", "agent-1") is None

    # Error JSON
    assert store.add_evidence(
        channel_id, thread_ts, "web_search", {"q": "test"},
        json.dumps({"error": "Rate limit exceeded"}), "agent-1"
    ) is None

    # Permission denied string
    assert store.add_evidence(
        channel_id, thread_ts, "read_document", {"path": "secret.txt"},
        "Permission denied: unauthorized access", "agent-1"
    ) is None


def test_agent_scoped_cache_isolation():
    store = RoundEvidenceStore()
    channel_id = "C123"
    thread_ts = "300.0"

    # search_knowledge_rag is an agent-scoped tool
    item = store.add_evidence(
        channel_id=channel_id,
        thread_ts=thread_ts,
        tool_name="search_knowledge_rag",
        arguments={"query": "agent 1 private notes"},
        result="Private chunk #42",
        agent_key="agent-1",
    )
    assert item is not None
    assert item.scope_key == "agent-1"

    # Same query by agent-1 hits cache
    hit_agent1 = store.get_evidence(
        channel_id, thread_ts, "search_knowledge_rag",
        {"query": "agent 1 private notes"}, agent_key="agent-1"
    )
    assert hit_agent1 is not None
    assert hit_agent1.result == "Private chunk #42"

    # Same query by agent-2 misses cache (no leak)
    hit_agent2 = store.get_evidence(
        channel_id, thread_ts, "search_knowledge_rag",
        {"query": "agent 1 private notes"}, agent_key="agent-2"
    )
    assert hit_agent2 is None

    # Shared round evidence block excludes agent-scoped items
    block = store.format_evidence_block(channel_id, thread_ts)
    assert block is None


def test_lru_and_per_thread_caps():
    store = RoundEvidenceStore(max_entries=3, max_items_per_thread=2)
    channel_id = "C123"
    thread_ts = "400.0"

    store.add_evidence(channel_id, thread_ts, "web_search", {"q": "1"}, "res1", "a1")
    store.add_evidence(channel_id, thread_ts, "web_search", {"q": "2"}, "res2", "a1")
    store.add_evidence(channel_id, thread_ts, "web_search", {"q": "3"}, "res3", "a1")

    # Thread items capped to max_items_per_thread=2
    items = store.get_round_evidence(channel_id, thread_ts)
    assert len(items) == 2
    assert [i.result for i in items] == ["res2", "res3"]

    # Global max_entries=3
    store.add_evidence("C456", "500.0", "web_search", {"q": "4"}, "res4", "a1")
    # Oldest entry (res1 was popped from thread, but was it popped from _cache?)
    # Now len(_cache) is 3 (res2, res3, res4)
    assert store.get_evidence(channel_id, thread_ts, "web_search", {"q": "1"}) is None
    assert store.get_evidence("C456", "500.0", "web_search", {"q": "4"}) is not None


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
        shared_round_evidence="The following verified evidence was gathered:\n1. [Tool: web_search | Agent: agent-1]:\n   Result: Found Python 3.12 release notes.",
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
        tool_name="web_search",
        arguments={"query": "specs"},
        result="Specs document v2 found",
        agent_key="agent-1",
    )

    # Verify cached retrieval
    cached = thread_state_manager.evidence.get_evidence(
        channel_id,
        thread_ts,
        "web_search",
        {"query": "specs"},
    )
    assert cached is not None
    assert cached.result == "Specs document v2 found"
    assert cached.agent_key == "agent-1"


def test_round_evidence_unwrapping_and_fallback():
    store = RoundEvidenceStore()
    channel_id = "C_UNWRAP"
    thread_ts = "999.0"

    # 1. JSON payload with 'content' unwrapped
    doc_payload = json.dumps({
        "status": "success",
        "document": "blueprint.pdf",
        "pages": 5,
        "content": "Foundation 1: Real AI\nFoundation 2: Context",
    })
    store.add_evidence(
        channel_id=channel_id,
        thread_ts=thread_ts,
        tool_name="read_document",
        arguments={"document_name": "blueprint.pdf"},
        result=doc_payload,
        agent_key="Visionary",
    )
    block = store.format_evidence_block(channel_id, thread_ts)
    assert block is not None
    assert "Document: blueprint.pdf (5 pages)" in block
    assert "Foundation 1: Real AI" in block
    assert "Foundation 2: Context" in block
    assert '"status": "success"' not in block

    # 2. Raw fallback for non-content or invalid JSON
    store.clear_thread_evidence(channel_id, thread_ts)
    raw_payload = "Direct raw string without any json formatting"
    store.add_evidence(
        channel_id=channel_id,
        thread_ts=thread_ts,
        tool_name="web_search",
        arguments={"query": "test"},
        result=raw_payload,
        agent_key="Tactician",
    )
    block2 = store.format_evidence_block(channel_id, thread_ts)
    assert block2 is not None
    assert "Direct raw string without any json formatting" in block2

    # 3. Dynamic ceiling truncation
    store.clear_thread_evidence(channel_id, thread_ts)
    long_text = "Z" * 500
    store.add_evidence(
        channel_id=channel_id,
        thread_ts=thread_ts,
        tool_name="web_search",
        arguments={"query": "test"},
        result=long_text,
        agent_key="Tactician",
    )
    block3 = store.format_evidence_block(channel_id, thread_ts, max_chars=100)
    assert "... [truncated]" in block3


def test_round_evidence_store_format_excludes_named_files():
    """format_evidence_block(exclude_file_names=...) skips only the matching file items (by
    'file', 'rel_path', or 'document_name' argument) while keeping all other evidence intact."""
    store = RoundEvidenceStore()
    channel_id = "C_EXCLUDE"
    thread_ts = "400.0"

    spec_payload = json.dumps({
        "status": "success",
        "document": "spec.txt",
        "size": 100,
        "content": "Target throughput: 5000 rps",
    })
    store.add_evidence(
        channel_id=channel_id,
        thread_ts=thread_ts,
        tool_name="document_reader",
        arguments={"file": "spec.txt"},
        result=spec_payload,
        agent_key="agent-1",
    )
    store.add_evidence(
        channel_id=channel_id,
        thread_ts=thread_ts,
        tool_name="web_search",
        arguments={"q": "k8s"},
        result="Cluster sizing benchmark results",
        agent_key="agent-2",
    )
    store.add_evidence(
        channel_id=channel_id,
        thread_ts=thread_ts,
        tool_name="read_document",
        arguments={"document_name": "policy.md"},
        result=json.dumps({"status": "success", "document": "policy.md", "content": "Vacation policy: 25 days."}),
        agent_key="agent-3",
    )
    store.add_evidence(
        channel_id=channel_id,
        thread_ts=thread_ts,
        tool_name="fetch_web_page",
        arguments={"rel_path": "spec.txt"},
        result="mirror of spec.txt",
        agent_key="agent-4",
    )

    # Without exclusions, everything is present
    block_all = store.format_evidence_block(channel_id, thread_ts)
    assert block_all is not None
    assert "5000 rps" in block_all
    assert "Cluster sizing benchmark results" in block_all
    assert "Vacation policy: 25 days." in block_all

    # Excluding spec.txt removes both the 'file' and 'rel_path' variants, keeps the rest
    block = store.format_evidence_block(channel_id, thread_ts, exclude_file_names={"spec.txt"})
    assert block is not None
    assert "5000 rps" not in block
    assert "mirror of spec.txt" not in block
    assert "Cluster sizing benchmark results" in block
    assert "Vacation policy: 25 days." in block

    # Excluding by document_name works too
    block2 = store.format_evidence_block(channel_id, thread_ts, exclude_file_names={"policy.md"})
    assert block2 is not None
    assert "Vacation policy: 25 days." not in block2
    assert "5000 rps" in block2

    # Excluding all file-backed items leaves only the non-file evidence (web_search has no file arg)
    block3 = store.format_evidence_block(channel_id, thread_ts, exclude_file_names={"spec.txt", "policy.md"})
    assert block3 is not None
    assert "Cluster sizing benchmark results" in block3
    assert "5000 rps" not in block3
    assert "Vacation policy" not in block3

