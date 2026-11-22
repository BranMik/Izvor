import concurrent.futures
from pathlib import Path
import pytest

from core.database import DatabaseManager, MIGRATIONS


def test_pragma_user_version_advancement(tmp_path: Path):
    db_file = tmp_path / "test_migrations.db"
    mgr = DatabaseManager(db_path=db_file)
    mgr.init_db()

    with mgr.get_connection() as conn:
        ver = conn.execute("PRAGMA user_version;").fetchone()[0]
        assert ver == MIGRATIONS[-1][0]
        assert ver >= 2


def test_nested_transaction_rejection(tmp_path: Path):
    db_file = tmp_path / "test_nested.db"
    mgr = DatabaseManager(db_path=db_file)
    mgr.init_db()

    with pytest.raises(RuntimeError, match="Nested or interleaved SQLite transaction detected"):
        with mgr.transaction(immediate=True):
            with mgr.transaction(immediate=True):
                pass


def test_dead_thread_connection_cleanup(tmp_path: Path):
    db_file = tmp_path / "test_threads.db"
    mgr = DatabaseManager(db_path=db_file)
    mgr.init_db()

    def thread_worker():
        with mgr.get_connection() as conn:
            conn.execute("SELECT 1;").fetchone()

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(thread_worker) for _ in range(3)]
        for f in futures:
            f.result()

    # Trigger eviction pass
    mgr._evict_dead_thread_connections()
    with mgr._conns_lock:
        # After worker threads die, no dead thread connections remain
        active_ids = {t.ident for t in __import__("threading").enumerate()}
        assert all(tid in active_ids for tid in mgr._thread_conns.keys())


def test_migration_v4_legacy_not_null_remaining_agents_to_v5(tmp_path: Path):
    """Verifies Migration 5 rebuilds legacy thread_sessions with NOT NULL remaining_agents to nullable DEFAULT NULL."""
    import sqlite3
    from core.database import db_manager
    from orchestration.state.discussion_store import DiscussionStateStore

    db_file = tmp_path / "legacy_v4.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        """
        CREATE TABLE thread_sessions (
            channel_id TEXT NOT NULL,
            thread_ts TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('active', 'paused', 'completed')),
            remaining_agents TEXT NOT NULL DEFAULT '[]',
            current_turn INTEGER NOT NULL DEFAULT 0,
            custom_flow INTEGER NOT NULL DEFAULT 0,
            stop_requested INTEGER NOT NULL DEFAULT 0,
            rounds INTEGER NOT NULL DEFAULT 0,
            round_sequence TEXT NOT NULL DEFAULT '[]',
            initial_prompt TEXT,
            created_at REAL NOT NULL,
            last_activity REAL NOT NULL,
            workflow_snapshot TEXT DEFAULT NULL,
            open_debate BOOLEAN NOT NULL DEFAULT 0,
            consensus_reached BOOLEAN NOT NULL DEFAULT 0,
            convergence_speakers TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (channel_id, thread_ts)
        );
        """
    )
    conn.execute(
        """
        INSERT INTO thread_sessions (
            channel_id, thread_ts, status, remaining_agents, current_turn,
            round_sequence, initial_prompt, created_at, last_activity, workflow_snapshot
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "C_LEGACY",
            "1794841510.427189",
            "completed",
            "[]",
            0,
            '["agent-10001", "agent-10002"]',
            "Initial prompt",
            1000.0,
            1005.0,
            '{"mode": "fixed"}',
        ),
    )
    conn.execute("PRAGMA user_version = 4;")
    conn.commit()
    conn.close()

    orig_db_path = db_manager.db_path
    try:
        db_manager.configure_paths(db_file)

        with db_manager.get_connection() as migrated_conn:
            ver = migrated_conn.execute("PRAGMA user_version;").fetchone()[0]
            assert ver == MIGRATIONS[-1][0]
            assert ver >= 6

            cols = {
                row["name"]: row
                for row in migrated_conn.execute("PRAGMA table_info(thread_sessions)").fetchall()
            }
            assert cols["remaining_agents"]["notnull"] == 0
            assert "current_turn" not in cols
            assert "active_prompt" in cols
            assert "is_pure_skill_task" in cols

            row = migrated_conn.execute(
                "SELECT status, remaining_agents, round_sequence, initial_prompt, workflow_snapshot "
                "FROM thread_sessions WHERE channel_id = ? AND thread_ts = ?",
                ("C_LEGACY", "1794841510.427189"),
            ).fetchone()
            assert row["status"] == "completed"
            assert row["remaining_agents"] is None
            assert row["round_sequence"] == '["agent-10001", "agent-10002"]'
            assert row["initial_prompt"] == "Initial prompt"
            assert row["workflow_snapshot"] == '{"mode": "fixed"}'

        store = DiscussionStateStore()
        assert store.has_remaining_agents_key("C_LEGACY", "1794841510.427189") is False

        # Inserting a brand-new thread with remaining_agents=None and clearing an empty queue must succeed
        store.set_workflow_snapshot("C_NEW", "1794999999.0001", {"mode": "fixed"})
        store.set_initial_prompt("C_NEW", "1794999999.0001", "Fresh prompt")
        assert store.get_initial_prompt("C_NEW", "1794999999.0001") == "Fresh prompt"

        store.set_remaining_agents("C_NEW", "1794999999.0001", [])
        assert store.has_remaining_agents_key("C_NEW", "1794999999.0001") is True
        store.clear_remaining_agents("C_NEW", "1794999999.0001")
        assert store.has_remaining_agents_key("C_NEW", "1794999999.0001") is False

        store.set_active_prompt("C_NEW", "1794999999.0001", "Round 2 prompt")
        assert store.get_active_prompt("C_NEW", "1794999999.0001") == "Round 2 prompt"
        assert store.get_initial_prompt("C_NEW", "1794999999.0001") == "Fresh prompt"

        store.set_is_pure_skill_task("C_NEW", "1794999999.0001", True)
        assert store.get_is_pure_skill_task("C_NEW", "1794999999.0001") is True
        store.clear_is_pure_skill_task("C_NEW", "1794999999.0001")
        assert store.get_is_pure_skill_task("C_NEW", "1794999999.0001") is False
    finally:
        db_manager.configure_paths(orig_db_path)


def test_migration_v6_to_v7_adds_turn_checkpoints_prompt(tmp_path):
    """TKT-1210 (C7): Migration 7 adds prompt TEXT NOT NULL DEFAULT '' to pre-existing v6 turn_checkpoints tables."""
    import sqlite3
    from core.database import db_manager

    db_file = tmp_path / "v6_checkpoints.db"
    conn = sqlite3.connect(db_file)
    conn.execute("PRAGMA user_version = 6;")
    conn.execute(
        """
        CREATE TABLE turn_checkpoints (
            checkpoint_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            thread_ts TEXT NOT NULL,
            agent_key TEXT NOT NULL,
            pause_reason TEXT NOT NULL,
            hitl_request_id TEXT,
            messages_json TEXT NOT NULL,
            pending_tool_call_json TEXT,
            active_tools_json TEXT,
            skills_used_json TEXT,
            token_mapping_json TEXT,
            accumulated_history_json TEXT,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            status TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL
        );
        """
    )
    conn.execute(
        """
        INSERT INTO turn_checkpoints (
            checkpoint_id, session_id, channel_id, thread_ts, agent_key,
            pause_reason, messages_json, status, created_at, expires_at
        ) VALUES ('chk_v6', 'C1:T1', 'C1', 'T1', 'agent-10001', 'circuit_breaker', '[]', 'ACTIVE', 100.0, 9999999999.0);
        """
    )
    conn.commit()
    conn.close()

    orig_db_path = db_manager.db_path
    try:
        db_manager.configure_paths(db_file)
        with db_manager.get_connection() as migrated_conn:
            ver = migrated_conn.execute("PRAGMA user_version;").fetchone()[0]
            assert ver == MIGRATIONS[-1][0]
            cols = {
                row["name"]: row
                for row in migrated_conn.execute("PRAGMA table_info(turn_checkpoints)").fetchall()
            }
            assert "prompt" in cols
            assert cols["prompt"]["dflt_value"] == "''"
            row = migrated_conn.execute(
                "SELECT prompt FROM turn_checkpoints WHERE checkpoint_id = 'chk_v6'"
            ).fetchone()
            assert row["prompt"] == ""
    finally:
        db_manager.configure_paths(orig_db_path)

