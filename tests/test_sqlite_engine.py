import concurrent.futures
import time


from adapters.slack.events import EventDeduplicationCache
from core.database import DatabaseManager, get_connection
from core.models import FileContext
from orchestration.state.context_store import ThreadContextStore
from orchestration.state.discussion_store import DiscussionStateStore
from orchestration.thread_skills_manager import thread_skills_manager
from pipeline.spend_governance.circuit_breaker import SpendGovernance


def test_sqlite_wal_mode_and_pragmas(tmp_path):
    db_file = tmp_path / "test_wal.db"
    manager = DatabaseManager(db_path=db_file)
    manager.init_db()

    with manager.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode;")
        journal_mode = cursor.fetchone()[0]
        assert journal_mode.lower() == "wal"

        cursor.execute("PRAGMA busy_timeout;")
        busy_timeout = cursor.fetchone()[0]
        assert busy_timeout >= 5000

        cursor.execute("PRAGMA foreign_keys;")
        foreign_keys = cursor.fetchone()[0]
        assert foreign_keys == 1


def test_sqlite_concurrent_writers(tmp_path):
    db_file = tmp_path / "test_concurrent.db"
    manager = DatabaseManager(db_path=db_file)
    manager.init_db()

    num_threads = 10
    writes_per_thread = 20

    def write_worker(thread_idx: int):
        for i in range(writes_per_thread):
            with manager.transaction(immediate=True) as conn:
                conn.execute(
                    """
                    INSERT INTO spend_ledger (
                        channel_id, thread_ts, model_name, prompt_tokens, completion_tokens, cost_usd, timestamp
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (f"C_{thread_idx}", f"T_{i}", "gemini-2.5-pro", 100, 50, 0.001, time.time()),
                )

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(write_worker, t) for t in range(num_threads)]
        for f in concurrent.futures.as_completed(futures):
            f.result()  # Should not raise OperationalError: database is locked

    with manager.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM spend_ledger")
        total_rows = cursor.fetchone()[0]
        assert total_rows == num_threads * writes_per_thread


def test_spend_ledger_persistence_and_daily_aggregation(tmp_path):
    db_file = tmp_path / "test_spend.db"
    from core.database import db_manager
    db_manager.configure_paths(db_path=db_file)
    try:
        cb1 = SpendGovernance()
        session_id = "C_CHANNEL:T_THREAD"

        # Record usage
        cb1.record_usage(session_id, "gemini-2.5-flash", 1000, 200)
        cb1.record_usage(session_id, "gemini-2.5-pro", 500, 100)

        spend_metrics = cb1.calculate_spend(session_id)
        assert spend_metrics.total_cost_usd > 0.0
        assert spend_metrics.total_prompt_tokens == 1500
        assert spend_metrics.total_completion_tokens == 300
        total_spend_1 = cb1.get_daily_spend()
        assert total_spend_1 > 0.0

        # Simulate process restart by instantiating new SpendGovernance
        cb2 = SpendGovernance()
        spend_metrics_2 = cb2.calculate_spend(session_id)
        assert spend_metrics_2.total_cost_usd == spend_metrics.total_cost_usd
        assert spend_metrics_2.total_prompt_tokens == 1500
        assert cb2.get_daily_spend() == total_spend_1

        # Clear session (clears in-memory session cache while preserving append-only spend ledger)
        cb2.clear_session(session_id)
        assert session_id not in cb2._session_tokens
        assert cb2.get_daily_spend() == total_spend_1
    finally:
        db_manager.reset_paths()


def test_hitl_atomic_approval_consumption(tmp_path):
    from services.hitl import (
        approve_hitl_request,
        consume_tool_approval,
        register_hitl_request,
    )

    req = register_hitl_request(
        channel_id="C_HITL",
        thread_ts="1700000001.0001",
        agent_name="Analyst",
        skill_name="code_exec",
        fn_name="execute_python",
        fn_args={"code": "import os; print(os.name)"},
    )

    # Approve request
    approved = approve_hitl_request(req.request_id)
    assert approved is not None
    assert approved.status == "APPROVED"

    # Concurrent consumption test: only 1 consumer should succeed
    results = []

    def try_consume():
        res = consume_tool_approval(
            channel_id="C_HITL",
            thread_ts="1700000001.0001",
            fn_name="execute_python",
            fn_args={"code": "import os; print(os.name)"},
        )
        results.append(res)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(try_consume) for _ in range(5)]
        for f in concurrent.futures.as_completed(futures):
            f.result()

    # Exactly one True, others False
    assert results.count(True) == 1
    assert results.count(False) == 4


def test_thread_skills_persistence_and_expiry(tmp_path, monkeypatch):
    channel_id = "C_SKILLS"
    thread_ts = "1700000002.0002"

    thread_skills_manager.revoke_skills(channel_id, thread_ts)

    # Grant skills
    thread_skills_manager.grant_skills(channel_id, thread_ts, ["web_search", "python_interpreter"])
    active = thread_skills_manager.get_active_skills(channel_id, thread_ts)
    assert "web_search" in active
    assert "python_interpreter" in active

    # Directly check SQLite database
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT skill_name FROM thread_skills WHERE channel_id = ? AND thread_ts = ?",
            (channel_id, thread_ts),
        )
        skills_in_db = {row[0] for row in cursor.fetchall()}
        assert skills_in_db == {"web_search", "python_interpreter"}

    # Revoke skills
    thread_skills_manager.revoke_skills(channel_id, thread_ts)
    assert thread_skills_manager.get_active_skills(channel_id, thread_ts) == []

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM thread_skills WHERE channel_id = ? AND thread_ts = ?",
            (channel_id, thread_ts),
        )
        assert cursor.fetchone()[0] == 0


def test_discussion_and_context_state_stores_persistence():
    channel_id = "C_STORE_TEST"
    thread_ts = "1700000003.0003"

    # Discussion Store write
    d_store_1 = DiscussionStateStore()
    d_store_1.set_remaining_agents(channel_id, thread_ts, ["agent-10001", "agent-10008"])
    d_store_1.set_custom_flow(channel_id, thread_ts, True)
    d_store_1.set_rounds(channel_id, thread_ts, 3)
    d_store_1.set_initial_prompt(channel_id, thread_ts, "Analyze this architecture.")

    # Fresh store instance simulates fresh load from DB
    d_store_2 = DiscussionStateStore()
    assert d_store_2.get_remaining_agents(channel_id, thread_ts) == ["agent-10001", "agent-10008"]
    assert d_store_2.is_custom_flow(channel_id, thread_ts) is True
    assert d_store_2.get_rounds(channel_id, thread_ts) == 3
    assert d_store_2.get_initial_prompt(channel_id, thread_ts) == "Analyze this architecture."

    # Context Store write
    c_store_1 = ThreadContextStore()
    c_store_1.set_history_limit(channel_id, thread_ts, limit=5, prior_cutoff_index=12)
    c_store_1.set_max_words(channel_id, thread_ts, 150)
    c_store_1.set_language(channel_id, thread_ts, "fr")
    fc = FileContext(file_name="data.json", content='{"key": "val"}', size=15)
    c_store_1.set_file_context(channel_id, thread_ts, fc)

    # Fresh context store instance verifies DB write-through
    c_store_2 = ThreadContextStore()
    assert c_store_2.get_history_limit(channel_id, thread_ts) == {"limit": 5, "prior_cutoff_index": 12}
    assert c_store_2.get_max_words(channel_id, thread_ts) == 150
    assert c_store_2.get_language(channel_id, thread_ts) == "fr"
    fc_loaded = c_store_2.get_file_context(channel_id, thread_ts)
    assert fc_loaded["file_name"] == "data.json"

    # Cleanup verification
    d_store_2.clear_thread(channel_id, thread_ts)
    c_store_2.clear_thread(channel_id, thread_ts)

    d_store_3 = DiscussionStateStore()
    c_store_3 = ThreadContextStore()
    assert d_store_3.get_remaining_agents(channel_id, thread_ts) == []
    assert c_store_3.get_max_words(channel_id, thread_ts) is None


def test_event_deduplication_cache_sqlite():
    cache1 = EventDeduplicationCache(ttl_seconds=10.0)
    key = ("C_DEDUP", "1700000004.0004")

    assert key not in cache1
    now = time.time()
    cache1[key] = now

    assert key in cache1

    # Simulate fresh cache instance
    cache2 = EventDeduplicationCache(ttl_seconds=10.0)
    assert key in cache2

    # Verify clear cleans DB
    cache2.clear()
    cache3 = EventDeduplicationCache(ttl_seconds=10.0)
    assert key not in cache3


def test_empty_remaining_agents_persisted_and_reloaded_from_sqlite():
    """Verifies that an exhausted/empty remaining_agents list persists in SQLite and reloads as [] rather than falling back."""
    channel_id = "C_EMPTY_TEST"
    thread_ts = "1700000099.9999"

    store1 = DiscussionStateStore()
    store1.set_remaining_agents(channel_id, thread_ts, [])

    # Verify that in a fresh store instance, has_remaining_agents_key is True and returns []
    store2 = DiscussionStateStore()
    assert store2.has_remaining_agents_key(channel_id, thread_ts) is True
    assert store2.get_remaining_agents(channel_id, thread_ts, default=["fallback"]) == []

