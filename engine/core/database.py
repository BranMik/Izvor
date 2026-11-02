"""
Embedded SQLite State Engine for Izvor.
Provides zero-infrastructure ACID state storage using standard library sqlite3 in WAL mode.
"""

from collections.abc import Callable, Generator
from contextlib import contextmanager
import logging
from pathlib import Path
import sqlite3
import threading

from core.paths import DB_PATH

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = DB_PATH

BASELINE_SCHEMA_STATEMENTS = [
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

    # 9. Turn Checkpoints (Durable Checkpointing for HITL & Circuit Breaker)
    """
    CREATE TABLE IF NOT EXISTS turn_checkpoints (
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
    """,
    "CREATE INDEX IF NOT EXISTS idx_turn_checkpoints_session ON turn_checkpoints(session_id);",
    "CREATE INDEX IF NOT EXISTS idx_turn_checkpoints_hitl ON turn_checkpoints(hitl_request_id);",
    "CREATE INDEX IF NOT EXISTS idx_turn_checkpoints_thread ON turn_checkpoints(channel_id, thread_ts);",
    "CREATE INDEX IF NOT EXISTS idx_turn_checkpoints_expiry ON turn_checkpoints(expires_at);",

    # 10. Turn Traces (Execution observability spans across stages)
    """
    CREATE TABLE IF NOT EXISTS turn_traces (
        trace_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        thread_ts TEXT NOT NULL,
        agent_key TEXT NOT NULL,
        stage_name TEXT NOT NULL,
        duration_ms REAL NOT NULL,
        prompt_tokens INTEGER DEFAULT 0,
        completion_tokens INTEGER DEFAULT 0,
        tools_called TEXT DEFAULT '[]',
        status TEXT NOT NULL,
        error_message TEXT,
        created_at REAL NOT NULL
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_turn_traces_session ON turn_traces(session_id);",
    "CREATE INDEX IF NOT EXISTS idx_turn_traces_created ON turn_traces(created_at);",
]

AUDIT_ANCHORS_STATEMENTS = [
    # 11. Audit Anchors (Head-hash anchors for detecting JSONL log truncation)
    """
    CREATE TABLE IF NOT EXISTS audit_anchors (
        month TEXT NOT NULL,
        seq INTEGER NOT NULL,
        entry_hash TEXT NOT NULL,
        anchored_at TEXT NOT NULL,
        PRIMARY KEY (month, seq)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_audit_anchors_month_seq ON audit_anchors(month, seq);",
]

SESSION_DURABILITY_STATEMENTS = [
    # 12. Thread Session Durability (Workflow snapshot, open debate, consensus reached)
    "ALTER TABLE thread_sessions ADD COLUMN workflow_snapshot TEXT DEFAULT NULL;",
    "ALTER TABLE thread_sessions ADD COLUMN open_debate BOOLEAN NOT NULL DEFAULT 0;",
    "ALTER TABLE thread_sessions ADD COLUMN consensus_reached BOOLEAN NOT NULL DEFAULT 0;",
]

SCHEMA_STATEMENTS = BASELINE_SCHEMA_STATEMENTS + AUDIT_ANCHORS_STATEMENTS + SESSION_DURABILITY_STATEMENTS

MIGRATIONS: list[tuple[int, str, list[str]]] = [
    (1, "baseline_schema", BASELINE_SCHEMA_STATEMENTS),
    (2, "audit_anchors_and_indices", AUDIT_ANCHORS_STATEMENTS),
    (3, "thread_session_durability", SESSION_DURABILITY_STATEMENTS),
]


_db_busy_timeout_provider: Callable[[], float] | None = None


def set_db_busy_timeout_provider(provider: Callable[[], float] | None) -> None:
    """Injectable provider for dynamic busy timeout, decoupling core/database from orchestration."""
    global _db_busy_timeout_provider
    _db_busy_timeout_provider = provider


def _get_db_busy_timeout() -> float:
    if _db_busy_timeout_provider is not None:
        try:
            return float(_db_busy_timeout_provider())
        except Exception:
            pass
    try:
        import os
        return float(os.getenv("IZVOR_DB_BUSY_TIMEOUT", "5.0"))
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
        self._conns_lock = threading.Lock()
        self._thread_conns: dict[int, dict[Path, sqlite3.Connection]] = {}

    @property
    def db_path(self) -> Path:
        if self._custom_db_path is not None:
            return self._custom_db_path
        return DB_PATH

    @property
    def _all_conns(self) -> list[sqlite3.Connection]:
        with self._conns_lock:
            conns: list[sqlite3.Connection] = []
            for t_conns in self._thread_conns.values():
                conns.extend(t_conns.values())
            return conns

    def _evict_dead_thread_connections(self) -> None:
        """Evicts and closes SQLite connections belonging to terminated threads."""
        active_thread_ids = {t.ident for t in threading.enumerate()}
        dead_ids = [tid for tid in self._thread_conns if tid not in active_thread_ids]
        for tid in dead_ids:
            dead_conns = self._thread_conns.pop(tid, {})
            for c in dead_conns.values():
                try:
                    c.close()
                except Exception as e:  # noqa: BLE001
                    logger.debug("Error closing dead thread connection: %s", e)

    def _ensure_schema_initialized(self, conn: sqlite3.Connection, target_path: Path) -> None:
        """Idempotently executes MIGRATIONS once per database file path using PRAGMA user_version."""
        resolved = target_path.resolve()
        if resolved in self._initialized_paths:
            return

        with self._init_lock:
            if resolved in self._initialized_paths:
                return
            cursor = conn.execute("PRAGMA user_version;")
            row = cursor.fetchone()
            current_version = int(row[0]) if row else 0
            for version, description, statements in MIGRATIONS:
                if version > current_version:
                    conn.execute("BEGIN IMMEDIATE;")
                    try:
                        for stmt in statements:
                            stmt_clean = stmt.strip()
                            if stmt_clean:
                                try:
                                    conn.execute(stmt_clean)
                                except sqlite3.OperationalError as op_err:
                                    if "duplicate column name" in str(op_err).lower():
                                        pass
                                    else:
                                        raise
                        conn.execute(f"PRAGMA user_version = {version};")
                        conn.execute("COMMIT;")
                        logger.info(
                            f"[DATABASE] Applied migration {version} ({description}) to {resolved}."
                        )
                    except Exception:
                        try:
                            conn.execute("ROLLBACK;")
                        except Exception:
                            pass
                        raise
            self._initialized_paths.add(resolved)
            logger.info(f"[DATABASE] SQLite schema at {resolved} initialized up to version {MIGRATIONS[-1][0]} (WAL mode).")

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
        tid = threading.get_ident()
        with self._conns_lock:
            self._evict_dead_thread_connections()
            if tid not in self._thread_conns:
                self._thread_conns[tid] = {}
            thread_conns = self._thread_conns[tid]

        conn = thread_conns.get(target_path)
        if conn is not None:
            try:
                conn.execute("SELECT 1;")
                return conn
            except Exception:  # noqa: BLE001
                with self._conns_lock:
                    thread_conns.pop(target_path, None)

        conn = self.create_connection()
        with self._conns_lock:
            thread_conns[target_path] = conn
        return conn

    def close_all_connections(self) -> None:
        """Closes all cached SQLite connections across all threads and clears connection state."""
        with self._conns_lock:
            for thread_conns in self._thread_conns.values():
                for conn in thread_conns.values():
                    try:
                        conn.close()
                    except Exception as e:  # noqa: BLE001
                        logger.debug("Error closing connection during close_all: %s", e)
            self._thread_conns.clear()
        with self._init_lock:
            self._initialized_paths.clear()

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
        Rejects nested or interleaved transactions on the same thread.
        """
        if getattr(self._local, "in_transaction", False):
            raise RuntimeError("Nested or interleaved SQLite transaction detected on the same thread.")
        self._local.in_transaction = True
        conn = None
        try:
            conn = self._get_cached_connection()
            if immediate:
                conn.execute("BEGIN IMMEDIATE;")
            else:
                conn.execute("BEGIN;")
            yield conn
            conn.execute("COMMIT;")
        except Exception:
            try:
                if conn is not None:
                    conn.execute("ROLLBACK;")
            except Exception as rollback_err:  # noqa: BLE001
                logger.debug(f"[DATABASE] Rollback failed or unneeded: {rollback_err}")
            raise
        finally:
            self._local.in_transaction = False

    def init_db(self, db_path: Path | None = None) -> None:
        """Initializes all database tables and indices idempotently."""
        if db_path is not None:
            self._custom_db_path = db_path

        with self.transaction(immediate=True) as conn:
            cursor = conn.execute("PRAGMA user_version;")
            row = cursor.fetchone()
            current_version = int(row[0]) if row else 0
            for version, description, statements in MIGRATIONS:
                if version > current_version:
                    for stmt in statements:
                        stmt_clean = stmt.strip()
                        if stmt_clean:
                            try:
                                conn.execute(stmt_clean)
                            except sqlite3.OperationalError as op_err:
                                if "duplicate column name" in str(op_err).lower():
                                    pass
                                else:
                                    raise
                    conn.execute(f"PRAGMA user_version = {version};")
                    logger.info(
                        f"[DATABASE] Applied migration {version} ({description}) to {self.db_path}."
                    )
        with self._init_lock:
            self._initialized_paths.add(self.db_path.resolve())
        logger.info(f"[DATABASE] SQLite database initialized at {self.db_path} up to version {MIGRATIONS[-1][0]} (WAL mode).")


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
