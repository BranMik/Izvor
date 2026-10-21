import concurrent.futures
from pathlib import Path
import sqlite3
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
