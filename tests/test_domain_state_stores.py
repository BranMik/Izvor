

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
    assert mgr.remaining_agents_map[(channel_id, thread_ts)] == ["agent-10001", "agent-10008"]

    mgr.add_to_retry_queue(channel_id, thread_ts, "agent-10002")
    assert mgr.retry.get_retry_queue(channel_id, thread_ts) == ["agent-10002"]
    assert mgr.rate_limit_retry_queue[(channel_id, thread_ts)] == ["agent-10002"]

    mgr.set_language(channel_id, thread_ts, "de")
    assert mgr.context.get_language(channel_id, thread_ts) == "de"
    assert mgr.thread_languages[(channel_id, thread_ts)] == "de"

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

    popped_last = mgr.pop_random_remaining_agent(channel_id, thread_ts)
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
    assert session.to_llm_history() == [
        {"role": "user", "content": "Hello world"},
        {"role": "assistant", "content": "Hi there"},
    ]

    ctx = TurnContext(
        channel_id="C_SESS",
        thread_ts="123.456",
        raw_text="Test prompt",
        in_memory_history=raw_history,
    )
    assert ctx.session_id == "C_SESS:123.456"
    assert isinstance(ctx.session, SessionContext)
    assert len(ctx.session.history) == 2

