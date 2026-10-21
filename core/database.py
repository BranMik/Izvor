"""
Embedded SQLite State Engine for Izvor.
Provides zero-infrastructure ACID state storage using standard library sqlite3 in WAL mode.
"""

from collections.abc import Generator
from contextlib import contextmanager
import logging
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any

from core.paths import DB_PATH

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = DB_PATH

SCHEMA_STATEMENTS = [
    # 1. Thread Sessions (Discussion state, scheduling, remaining agents, turns)
    """
    CREATE TABLE IF NOT EXISTS thread_sessions (
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
        PRIMARY KEY (channel_id, thread_ts)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_thread_sessions_activity ON thread_sessions(last_activity);",

    # 2. Thread Context (Constraints, word limits, language locks, attached files)
    """
    CREATE TABLE IF NOT EXISTS thread_context (
        channel_id TEXT NOT NULL,
        thread_ts TEXT NOT NULL,
        history_limit_config TEXT,
        max_words INTEGER,
        min_words INTEGER,
        scribe_max_words INTEGER,
        scribe_min_words INTEGER,
        language TEXT,
        excluded_agents TEXT NOT NULL DEFAULT '[]',
        included_agents TEXT NOT NULL DEFAULT '[]',
        format_modifiers TEXT NOT NULL DEFAULT '{}',
        file_context TEXT,
        PRIMARY KEY (channel_id, thread_ts)
    );
    """,

    # 3. Spend Ledger (Transactional append-only ledger for token and USD accounting)
    """
    CREATE TABLE IF NOT EXISTS spend_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id TEXT NOT NULL,
        thread_ts TEXT NOT NULL,
        model_name TEXT NOT NULL,
        prompt_tokens INTEGER NOT NULL,
        completion_tokens INTEGER NOT NULL,
        cost_usd REAL NOT NULL,
        timestamp REAL NOT NULL
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_spend_thread ON spend_ledger(channel_id, thread_ts);",
    "CREATE INDEX IF NOT EXISTS idx_spend_timestamp ON spend_ledger(timestamp);",

    # 4. Human-In-The-Loop Approvals
    """
    CREATE TABLE IF NOT EXISTS hitl_approvals (
        request_id TEXT PRIMARY KEY,
        channel_id TEXT NOT NULL,
        thread_ts TEXT NOT NULL,
        agent_name TEXT NOT NULL,
        skill_name TEXT NOT NULL,
        fn_name TEXT NOT NULL,
        fn_args TEXT NOT NULL,
        args_hash TEXT NOT NULL,
        initiator_user_id TEXT,
        status TEXT NOT NULL CHECK(status IN ('PENDING', 'APPROVED', 'DENIED', 'CONSUMED')),
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_hitl_lookup ON hitl_approvals(channel_id, thread_ts, fn_name, args_hash);",
    "CREATE INDEX IF NOT EXISTS idx_hitl_expiry ON hitl_approvals(expires_at);",

    # 5. Thread-Scoped Skill Grants
    """
    CREATE TABLE IF NOT EXISTS thread_skills (
        channel_id TEXT NOT NULL,
        thread_ts TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        skill_name TEXT NOT NULL,
        granted_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        PRIMARY KEY (channel_id, thread_ts, agent_id, skill_name)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_skills_expiry ON thread_skills(expires_at);",

    # 6. Event Deduplication
    """
    CREATE TABLE IF NOT EXISTS event_dedup (
        event_id TEXT PRIMARY KEY,
        received_at REAL NOT NULL
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_event_dedup_time ON event_dedup(received_at);",

    # 7. Thread Usage Metadata (Replaces per-turn thread_usage_data.json fsync writes)
    """
    CREATE TABLE IF NOT EXISTS thread_usage_meta (
        channel_id TEXT NOT NULL,
        thread_ts TEXT NOT NULL,
        channel_name TEXT NOT NULL,
        created_at_str TEXT NOT NULL,
        usage_msg_ts TEXT,
        is_completed INTEGER NOT NULL DEFAULT 0,
        last_updated REAL NOT NULL,
        PRIMARY KEY (channel_id, thread_ts)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_usage_meta_updated ON thread_usage_meta(last_updated);",

    # 8. Thread Notes & Conclusion State (Persists Scribe note pointers across server restarts)
    """
    CREATE TABLE IF NOT EXISTS thread_notes_state (
        channel_id TEXT NOT NULL,
        thread_ts TEXT NOT NULL,
        live_note_ts TEXT,
        live_note_content TEXT,
        conclusion_note_ts TEXT,
        conclusion_note_content TEXT,
        is_live_pinned INTEGER NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL,
        PRIMARY KEY (channel_id, thread_ts)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_notes_state_updated ON thread_notes_state(updated_at);",
]



def _get_db_busy_timeout() -> float:
    try:
        from orchestration.system_settings import get_db_busy_timeout_seconds
        return get_db_busy_timeout_seconds()
    except Exception:
        return 5.0


class DatabaseManager:
    """
    Manages connections and schema for Izvor's embedded SQLite database.
    Operates in WAL mode with busy timeouts to support concurrent reads and serialized writes.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._custom_db_path = db_path
        self._init_lock = threading.RLock()
        self._initialized_paths: set[Path] = set()
        self._local = threading.local()
        self._all_conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()

    @property
    def db_path(self) -> Path:
        if self._custom_db_path is not None:
            return self._custom_db_path
        env_path = os.getenv("IZVOR_DB_PATH")
        if env_path:
            return Path(env_path)
        return DEFAULT_DB_PATH

    def _ensure_schema_initialized(self, conn: sqlite3.Connection, target_path: Path) -> None:
        """Idempotently executes SCHEMA_STATEMENTS once per database file path."""
        resolved = target_path.resolve()
        if resolved in self._initialized_paths:
            return

        with self._init_lock:
            if resolved in self._initialized_paths:
                return
            for stmt in SCHEMA_STATEMENTS:
                stmt_clean = stmt.strip()
                if stmt_clean:
                    conn.execute(stmt_clean)
            self._initialized_paths.add(resolved)
            logger.info(f"[DATABASE] Idempotently auto-initialized SQLite schema at {resolved} (WAL mode).")

    def create_connection(self) -> sqlite3.Connection:
        """Creates a new sqlite3 connection configured with WAL and performance pragmas."""
        target_path = self.db_path
        target_path.parent.mkdir(parents=True, exist_ok=True)

        busy_timeout = _get_db_busy_timeout()
        conn = sqlite3.connect(
            str(target_path),
            timeout=busy_timeout,
            check_same_thread=False,
            isolation_level=None,  # Autocommit mode by default; manual transactions with BEGIN
        )
        conn.row_factory = sqlite3.Row

        # Concurrency & Durability Pragmas
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout * 1000)};")
        conn.execute("PRAGMA foreign_keys = ON;")
        self._ensure_schema_initialized(conn, target_path)
        return conn

    def _get_cached_connection(self) -> sqlite3.Connection:
        target_path = self.db_path.resolve()
        conns = getattr(self._local, "conns", None)
        if conns is None:
            conns = {}
            self._local.conns = conns

        conn = conns.get(target_path)
        if conn is not None:
            try:
                conn.execute("SELECT 1;")
                return conn
            except Exception:
                conns.pop(target_path, None)

        conn = self.create_connection()
        conns[target_path] = conn
        with self._conns_lock:
            self._all_conns.append(conn)
        return conn

    def close_all_connections(self) -> None:
        """Closes all cached SQLite connections across all threads and clears connection state."""
        with self._conns_lock:
            for conn in self._all_conns:
                try:
                    conn.close()
                except Exception:
                    pass
            self._all_conns.clear()
        if hasattr(self._local, "conns"):
            self._local.conns.clear()

    @contextmanager
    def get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager yielding a thread-safe cached connection."""
        conn = self._get_cached_connection()
        yield conn

    @contextmanager
    def transaction(self, immediate: bool = True) -> Generator[sqlite3.Connection, None, None]:
        """
        Context manager for atomic write transactions on the cached connection.
        Uses BEGIN IMMEDIATE by default to prevent writer lock escalation deadlocks.
        """
        conn = self._get_cached_connection()
        try:
            if immediate:
                conn.execute("BEGIN IMMEDIATE;")
            else:
                conn.execute("BEGIN;")
            yield conn
            conn.execute("COMMIT;")
        except Exception:
            try:
                conn.execute("ROLLBACK;")
            except Exception as rollback_err:
                logger.debug(f"[DATABASE] Rollback failed or unneeded: {rollback_err}")
            raise

    def init_db(self, db_path: Path | None = None) -> None:
        """Initializes all database tables and indices idempotently."""
        if db_path is not None:
            self._custom_db_path = db_path

        with self.transaction(immediate=True) as conn:
            for stmt in SCHEMA_STATEMENTS:
                stmt_clean = stmt.strip()
                if stmt_clean:
                    conn.execute(stmt_clean)
        self._initialized_paths.add(self.db_path.resolve())
        logger.info(f"[DATABASE] SQLite database initialized at {self.db_path} (WAL mode).")


# Global singleton database manager
db_manager = DatabaseManager()


def get_connection() -> Generator[sqlite3.Connection, None, None]:
    return db_manager.get_connection()


def get_db_transaction(immediate: bool = True) -> Generator[sqlite3.Connection, None, None]:
    return db_manager.transaction(immediate=immediate)


def init_db(db_path: Path | None = None) -> None:
    db_manager.init_db(db_path=db_path)


def close_all_connections() -> None:
    db_manager.close_all_connections()
