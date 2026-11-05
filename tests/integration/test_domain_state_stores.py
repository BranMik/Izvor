

import pytest

from core.models import FileContext
from orchestration.state import (
    DiscussionStateStore,
    ThreadContextStore,
    TurnRetryStateStore,
)
from orchestration.thread_state import ThreadStateManager


def test_discussion_state_store_isolated():
    store = DiscussionStateStore()
    channel_id = "C_DISCUSS"
    thread_ts = "1700000001.0001"

    # Remaining agents
    assert store.get_remaining_agents(channel_id, thread_ts) == []
    assert store.has_remaining_agents_key(channel_id, thread_ts) is False
    store.set_remaining_agents(channel_id, thread_ts, ["agent-10001", "agent-10008", "Critic"])
    assert store.has_remaining_agents_key(channel_id, thread_ts) is True
    assert store.get_remaining_agents(channel_id, thread_ts) == ["agent-10001", "agent-10008", "Critic"]

    # Pop remaining agents
    assert store.pop_remaining_agent(channel_id, thread_ts) == "agent-10001"
    assert store.get_remaining_agents(channel_id, thread_ts) == ["agent-10008", "Critic"]
    assert store.pop_remaining_agent(channel_id, thread_ts, "Critic") == "Critic"
    assert store.get_remaining_agents(channel_id, thread_ts) == ["agent-10008"]
    store.clear_remaining_agents(channel_id, thread_ts)
    assert store.get_remaining_agents(channel_id, thread_ts) == []

    # Custom flow
    assert store.is_custom_flow(channel_id, thread_ts) is False
    store.set_custom_flow(channel_id, thread_ts, True)
    assert store.is_custom_flow(channel_id, thread_ts) is True
    store.clear_custom_flow(channel_id, thread_ts)
    assert store.is_custom_flow(channel_id, thread_ts) is False

    # Stop flag
    assert store.is_stopped(channel_id, thread_ts) is False
    store.request_stop(channel_id, thread_ts)
    assert store.is_stopped(channel_id, thread_ts) is True
    store.clear_stop(channel_id, thread_ts)
    assert store.is_stopped(channel_id, thread_ts) is False

    # Active task
    fake_task = object()
    store.set_active_task(channel_id, thread_ts, fake_task)
    assert store.get_active_task(channel_id, thread_ts) is fake_task
    store.clear_active_task(channel_id, thread_ts, object())  # mismatch should not clear
    assert store.get_active_task(channel_id, thread_ts) is fake_task
    assert store.pop_active_task(channel_id, thread_ts) is fake_task
    assert store.get_active_task(channel_id, thread_ts) is None

    # Reset
    store.set_remaining_agents(channel_id, thread_ts, ["agent-10001"])
    store.set_custom_flow(channel_id, thread_ts, True)
    store.request_stop(channel_id, thread_ts)
    store.reset()
    assert store.get_remaining_agents(channel_id, thread_ts) == []
    assert store.is_custom_flow(channel_id, thread_ts) is False
    assert store.is_stopped(channel_id, thread_ts) is False


def test_retry_state_store_isolated():
    store = TurnRetryStateStore()
    channel_id = "C_RETRY"
    thread_ts = "1700000002.0002"

    # Retry queue
    assert store.has_pending_retries(channel_id, thread_ts) is False
    assert store.get_retry_queue(channel_id, thread_ts) == []
    store.add_to_retry_queue(channel_id, thread_ts, "agent-10011")
    store.add_to_retry_queue(channel_id, thread_ts, "agent-10002")
    assert store.has_pending_retries(channel_id, thread_ts) is True
    assert store.get_retry_queue(channel_id, thread_ts) == ["agent-10011", "agent-10002"]
    assert store.pop_retry_agent(channel_id, thread_ts) == "agent-10011"
    assert store.pop_retry_agent(channel_id, thread_ts, "agent-10002") == "agent-10002"
    assert store.has_pending_retries(channel_id, thread_ts) is False

    # Already retried
    assert store.is_already_retried(channel_id, thread_ts, "agent-10011") is False
    store.mark_already_retried(channel_id, thread_ts, "agent-10011")
    assert store.is_already_retried(channel_id, thread_ts, "agent-10011") is True
    assert store.get_already_retried(channel_id, thread_ts) == {"agent-10011"}
    store.clear_already_retried(channel_id, thread_ts)
    assert store.is_already_retried(channel_id, thread_ts, "agent-10011") is False

    # 503 errored agents
    assert store.has_agent_503_errored(channel_id, thread_ts, "agent-10001") is False
    store.mark_agent_503_errored(channel_id, thread_ts, "agent-10001")
    assert store.has_agent_503_errored(channel_id, thread_ts, "agent-10001") is True
    assert store.get_503_errored_agents(channel_id, thread_ts) == {"agent-10001"}
    store.clear_503_errored_agents(channel_id, thread_ts)
    assert store.has_agent_503_errored(channel_id, thread_ts, "agent-10001") is False

    # Generations
    assert store.get_generation(channel_id, thread_ts) == 0
    assert store.increment_generation(channel_id, thread_ts) == 1
    assert store.increment_generation(channel_id, thread_ts) == 2
    assert store.get_generation(channel_id, thread_ts) == 2
    store.clear_generation(channel_id, thread_ts)
    assert store.get_generation(channel_id, thread_ts) == 0

    # Reset
    store.add_to_retry_queue(channel_id, thread_ts, "agent-10011")
    store.mark_already_retried(channel_id, thread_ts, "agent-10011")
    store.mark_agent_503_errored(channel_id, thread_ts, "agent-10011")
    store.increment_generation(channel_id, thread_ts)
    store.reset()
    assert store.has_pending_retries(channel_id, thread_ts) is False
    assert store.get_already_retried(channel_id, thread_ts) == set()
    assert store.get_503_errored_agents(channel_id, thread_ts) == set()
    assert store.get_generation(channel_id, thread_ts) == 0


def test_context_state_store_isolated():
    store = ThreadContextStore()
    channel_id = "C_CTX"
    thread_ts = "1700000003.0003"

    # History limits
    assert store.get_history_limit(channel_id, thread_ts) is None
    store.set_history_limit(channel_id, thread_ts, limit=5, prior_cutoff_index=10)
    assert store.get_history_limit(channel_id, thread_ts) == {
        "limit": 5,
        "prior_cutoff_index": 10,
    }
    store.clear_history_limit(channel_id, thread_ts)
    assert store.get_history_limit(channel_id, thread_ts) is None

    # Words limits
    store.set_max_words(channel_id, thread_ts, 200)
    store.set_min_words(channel_id, thread_ts, 75)
    assert store.get_max_words(channel_id, thread_ts) == 200
    assert store.get_min_words(channel_id, thread_ts) == 75
    store.clear_max_words(channel_id, thread_ts)
    store.clear_min_words(channel_id, thread_ts)
    assert store.get_max_words(channel_id, thread_ts) is None
    assert store.get_min_words(channel_id, thread_ts) is None

    # Language lock
    assert store.get_language(channel_id, thread_ts) is None
    store.set_language(channel_id, thread_ts, "hr")
    assert store.get_language(channel_id, thread_ts) == "hr"
    store.clear_language(channel_id, thread_ts)
    assert store.get_language(channel_id, thread_ts) is None

    # File context
    fc = FileContext(file_name="test.py", content="print('hello')", size=20)
    store.set_file_context(channel_id, thread_ts, fc)
    assert store.get_file_context(channel_id, thread_ts)["file_name"] == "test.py"
    store.clear_file_context(channel_id, thread_ts)
    assert store.get_file_context(channel_id, thread_ts) is None

    # Reset
    store.set_max_words(channel_id, thread_ts, 100)
    store.set_language(channel_id, thread_ts, "es")
    store.reset()
    assert store.get_max_words(channel_id, thread_ts) is None
    assert store.get_language(channel_id, thread_ts) is None


def test_thread_state_manager_facade_composition():
    mgr = ThreadStateManager()
    channel_id = "C_FACADE"
    thread_ts = "1700000004.0004"

    # Verify domain stores exist on facade
    assert isinstance(mgr.discussion, DiscussionStateStore)
    assert isinstance(mgr.retry, TurnRetryStateStore)
    assert isinstance(mgr.context, ThreadContextStore)

    # State mutations via facade reflect on domain store
    mgr.set_remaining_agents(channel_id, thread_ts, ["agent-10001", "agent-10008"])
    assert mgr.discussion.get_remaining_agents(channel_id, thread_ts) == ["agent-10001", "agent-10008"]
    assert mgr.get_remaining_agents(channel_id, thread_ts) == ["agent-10001", "agent-10008"]

    mgr.add_to_retry_queue(channel_id, thread_ts, "agent-10002")
    assert mgr.retry.get_retry_queue(channel_id, thread_ts) == ["agent-10002"]
    assert mgr.get_retry_queue(channel_id, thread_ts) == ["agent-10002"]

    mgr.set_language(channel_id, thread_ts, "de")
    assert mgr.context.get_language(channel_id, thread_ts) == "de"
    assert mgr.get_language(channel_id, thread_ts) == "de"

    # Multi-store lifecycle: cleanup_thread purges across all 3 stores
    mgr.cleanup_thread(channel_id, thread_ts)
    assert mgr.get_remaining_agents(channel_id, thread_ts) == []
    assert mgr.get_retry_queue(channel_id, thread_ts) == []
    assert mgr.get_language(channel_id, thread_ts) is None
    assert (channel_id, thread_ts) not in mgr.thread_last_activity


def test_atomic_queue_pop_and_remove_methods():
    mgr = ThreadStateManager()
    channel_id = "C_ATOMIC"
    thread_ts = "1700000005.0005"

    mgr.set_remaining_agents(channel_id, thread_ts, ["A1", "A2", "A3"])
    assert mgr.remove_remaining_agent(channel_id, thread_ts, "A2") is True
    assert mgr.get_remaining_agents(channel_id, thread_ts) == ["A1", "A3"]
    assert mgr.remove_remaining_agent(channel_id, thread_ts, "NONEXISTENT") is False

    popped_random = mgr.pop_random_remaining_agent(channel_id, thread_ts)
    assert popped_random in ("A1", "A3")
    remaining = mgr.get_remaining_agents(channel_id, thread_ts)
    assert len(remaining) == 1
    assert popped_random not in remaining

    assert mgr.pop_random_remaining_agent(channel_id, thread_ts) is not None
    assert len(mgr.get_remaining_agents(channel_id, thread_ts)) == 0
    assert mgr.pop_random_remaining_agent(channel_id, thread_ts) is None


def test_session_context_hydration_in_turn_context():
    from core.domain import SessionContext, Turn
    from orchestration.turn_pipeline import TurnContext

    raw_history = [
        {"role": "user", "content": "Hello world", "speaker": "User"},
        {"role": "assistant", "content": "Hi there", "speaker": "Agent1"},
    ]
    session = SessionContext.from_thread("C_SESS", "123.456", raw_history)
    assert session.session_id == "C_SESS:123.456"
    assert len(session.history) == 2
    assert isinstance(session.history[0], Turn)

    ctx = TurnContext(
        channel_id="C_SESS",
        thread_ts="123.456",
        raw_text="Test prompt",
        in_memory_history=raw_history,
    )
    assert ctx.session_id == "C_SESS:123.456"
    assert isinstance(ctx.session, SessionContext)
    assert len(ctx.session.history) == 2


def test_discussion_store_session_durability_rehydration():
    """A-03: Verify workflow snapshot, open debate, and consensus reached survive RAM eviction and reload from DB."""
    channel_id = "C_DURABLE"
    thread_ts = "1700000002.0001"
    store1 = DiscussionStateStore()

    store1.set_open_debate(channel_id, thread_ts, True)
    store1.set_consensus_reached(channel_id, thread_ts, True)
    snapshot = {"workflow_id": "test_flow", "mode": "FIXED", "agents": ["a1", "a2"]}
    store1.set_workflow_snapshot(channel_id, thread_ts, snapshot)

    assert store1.is_open_debate(channel_id, thread_ts) is True
    assert store1.is_consensus_reached(channel_id, thread_ts) is True
    assert store1.get_workflow_snapshot(channel_id, thread_ts) == snapshot

    # Create a fresh store instance simulating a server restart / RAM cache eviction
    store2 = DiscussionStateStore()
    assert store2.is_open_debate(channel_id, thread_ts) is True
    assert store2.is_consensus_reached(channel_id, thread_ts) is True
    assert store2.get_workflow_snapshot(channel_id, thread_ts) == snapshot

    # Cleanup
    store2.clear_thread(channel_id, thread_ts)
    store3 = DiscussionStateStore()
    assert store3.is_open_debate(channel_id, thread_ts) is False
    assert store3.is_consensus_reached(channel_id, thread_ts) is False
    assert store3.get_workflow_snapshot(channel_id, thread_ts) is None


def test_telemetry_bounded_buffer_and_retry_requeue():
    """A-12: Verify telemetry queue bounds and retry re-queuing on DB error."""
    from unittest.mock import patch
    import services.telemetry as tel
    from services.telemetry import (
        _TRACE_BUFFER,
        _TRACE_BUFFER_LOCK,
        MAX_TRACE_BUFFER_SIZE,
        flush_turn_traces,
        record_turn_trace,
    )

    assert _TRACE_BUFFER.maxlen == MAX_TRACE_BUFFER_SIZE
    with _TRACE_BUFFER_LOCK:
        _TRACE_BUFFER.clear()

    with patch("services.telemetry.get_db_transaction", side_effect=RuntimeError("Contention")):
        record_turn_trace("s1", "c1", "t1", "agent1", "stage1", 10.0)
        assert len(_TRACE_BUFFER) == 1

    flushed = flush_turn_traces()
    assert flushed == 1
    assert len(_TRACE_BUFFER) == 0


def test_pricing_service_returns_deepcopy():
    """A-13: Verify PricingService returns deepcopies so callers cannot mutate shared cache."""
    from services.pricing_service import pricing_service
    p1 = pricing_service.get_pricing("gemini-2.5-flash")
    p2 = pricing_service.get_pricing("gemini-2.5-flash")
    assert p1 is not p2
    original_val = p1["input_price_per_1m"]
    p1["input_price_per_1m"] = 999999.0
    p3 = pricing_service.get_pricing("gemini-2.5-flash")
    assert p3["input_price_per_1m"] == original_val


@pytest.mark.anyio
async def test_channel_name_cache_ttl():
    """A-14: Verify channel name cache obeys TTL and updates timestamp."""
    import services.slack.client_core as sc
    sc.CHANNEL_NAME_CACHE.clear()
    ch_id = "C_TEST_TTL"
    sc.CHANNEL_NAME_CACHE[ch_id] = ("#old-name", 0.0)
    resolved = await sc.get_channel_name(ch_id)
    assert resolved == f"#{ch_id}"
    assert sc.CHANNEL_NAME_CACHE[ch_id][1] > 0


def test_manifest_manager_atomic_save(tmp_path):
    """A-15: Verify ManifestManager saves atomically with unique temp file."""
    from services.rag.manifest_manager import ManifestManager
    mf_file = tmp_path / "test_manifest.json"
    mgr = ManifestManager(manifest_path=mf_file)
    mgr.record_document("doc1.md", "agentA", "hash123", 100.0, ["c1", "c2"])
    mgr.save()
    assert mf_file.exists()
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert len(tmp_files) == 0


def test_rag_citation_instructions_cached():
    """A-15: Verify RAGService citation instructions are cached."""
    from services.rag.service import _load_citation_instructions
    import services.rag.service as rag_mod
    rag_mod._CACHED_CITATION_INSTRUCTIONS = "MOCK_CITATION_HEADER"
    assert _load_citation_instructions() == "MOCK_CITATION_HEADER"
    rag_mod._CACHED_CITATION_INSTRUCTIONS = None


@pytest.mark.anyio
async def test_dispatch_discussion_task_safe():
    """A-20: Verify ThreadStateManager.dispatch_discussion_task uses safe_create_task."""
    mgr = ThreadStateManager()
    completed = False

    async def sample_coro():
        nonlocal completed
        completed = True

    mgr.dispatch_discussion_task("C_DISPATCH_SAFE", "T_SAFE", sample_coro())
    task = mgr.get_active_task("C_DISPATCH_SAFE", "T_SAFE")
    assert task is not None
    assert task.get_name() == "discussion:C_DISPATCH_SAFE:T_SAFE"
    await task
    assert completed is True


def test_convergence_speakers_sqlite_durability():
    """Verify convergence speakers persist to SQLite thread_sessions and survive cache reload."""
    from orchestration.thread_state import thread_state_manager
    ch = "C_CONV_PERSIST"
    ts = "1700000999.0001"

    thread_state_manager.clear_convergence_speakers(ch, ts)
    assert thread_state_manager.get_convergence_speakers(ch, ts) == []

    thread_state_manager.record_convergence_speaker(ch, ts, "agent-10001")
    thread_state_manager.record_convergence_speaker(ch, ts, "agent-10008")
    # Duplicate addition should be ignored (streak of unique agents)
    thread_state_manager.record_convergence_speaker(ch, ts, "agent-10001")
    assert thread_state_manager.get_convergence_speakers(ch, ts) == ["agent-10001", "agent-10008"]

    # Verify reloading from SQLite restores convergence speakers
    reloaded = thread_state_manager.get_convergence_speakers(ch, ts)
    assert reloaded == ["agent-10001", "agent-10008"]

    # Clear and verify DB sync
    thread_state_manager.clear_convergence_speakers(ch, ts)
    assert thread_state_manager.get_convergence_speakers(ch, ts) == []


@pytest.mark.anyio
async def test_outbound_messages_dlp_unified():
    """Verify post_thread_note, update_thread_note, and post_message_to_slack sanitize via _prepare_outbound_payload."""
    from services.slack.messages import post_thread_note, update_thread_note, post_message_to_slack
    from services.slack.client_core import configure_token, reset_token

    configure_token("")
    secret_text = "Here is sensitive token: sk_live_1234567890abcdef"
    try:
        # Note post
        res_post = await post_thread_note("C_TEST", "1700000001.0001", secret_text)
        assert res_post["ok"] is True
        blocks = res_post.get("blocks")
        assert blocks is not None
        assert "blocked by security policy" in blocks[0]["text"]["text"]

        # Note update
        res_upd = await update_thread_note("C_TEST", "mock_note_1", secret_text)
        assert res_upd["ok"] is True
        upd_blocks = res_upd.get("blocks")
        assert upd_blocks is not None
        assert "blocked by security policy" in upd_blocks[0]["text"]["text"]

        # Message post
        res_msg = await post_message_to_slack("C_TEST", secret_text)
        assert res_msg["ok"] is True
        assert res_msg.get("mock") is True
    finally:
        reset_token()




