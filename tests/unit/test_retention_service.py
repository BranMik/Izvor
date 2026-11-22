"""
Tests for Data Retention Service (prune_expired_records & forget_thread) and Checkpoint Redaction.
"""

from __future__ import annotations

import time
import pytest
from core.database import get_connection, get_db_transaction
from core.domain import TurnCheckpoint
from core.paths import FLAT_HISTORIES_DIR
from orchestration.thread_state import thread_state_manager
from services.retention_service import forget_thread, prune_expired_records
from services.turn_checkpoint_store import TurnCheckpointStore


@pytest.fixture(autouse=True)
def clean_db():
    from core.database import init_db

    init_db()
    yield


def test_checkpoint_redaction():
    store = TurnCheckpointStore()
    chk = TurnCheckpoint(
        checkpoint_id="chk_redact_01",
        session_id="C_TEST:100.1",
        channel_id="C_TEST",
        thread_ts="100.1",
        agent_key="agent-10001",
        pause_reason="HITL",
        messages=[{"role": "user", "content": "secret data"}],
        accumulated_history=[{"role": "assistant", "content": "prior turn"}],
        status="PENDING",
        created_at=time.time(),
        expires_at=time.time() + 3600,
    )
    store.save_checkpoint(chk)

    # Initial state verification
    loaded = store.get_checkpoint("chk_redact_01")
    assert loaded is not None
    assert len(loaded.messages) == 1
    assert loaded.messages[0]["content"] == "secret data"

    # 1. Test mark_resumed redaction
    assert store.mark_resumed("chk_redact_01") is True
    resumed = store.get_checkpoint("chk_redact_01")
    assert resumed.status == "RESUMED"
    assert resumed.messages == []
    assert resumed.accumulated_history == []

    # 2. Test mark_resolved redaction
    chk2 = TurnCheckpoint(
        checkpoint_id="chk_redact_02",
        session_id="C_TEST:100.2",
        channel_id="C_TEST",
        thread_ts="100.2",
        agent_key="agent-10001",
        pause_reason="HITL",
        messages=[{"role": "user", "content": "confidential"}],
        accumulated_history=[{"role": "assistant", "content": "prior"}],
        status="PENDING",
        created_at=time.time(),
        expires_at=time.time() + 3600,
    )
    store.save_checkpoint(chk2)
    assert store.mark_resolved("chk_redact_02") is True
    resolved = store.get_checkpoint("chk_redact_02")
    assert resolved.status == "RESOLVED"
    assert resolved.messages == []
    assert resolved.accumulated_history == []

    # 3. Test mark_abandoned redaction
    chk3 = TurnCheckpoint(
        checkpoint_id="chk_redact_03",
        session_id="C_TEST:100.3",
        channel_id="C_TEST",
        thread_ts="100.3",
        agent_key="agent-10001",
        pause_reason="HITL",
        messages=[{"role": "user", "content": "confidential"}],
        accumulated_history=[{"role": "assistant", "content": "prior"}],
        status="PENDING",
        created_at=time.time(),
        expires_at=time.time() + 3600,
    )
    store.save_checkpoint(chk3)
    assert store.mark_abandoned("chk_redact_03") is True
    abandoned = store.get_checkpoint("chk_redact_03")
    assert abandoned.status == "ABANDONED"
    assert abandoned.messages == []
    assert abandoned.accumulated_history == []


def test_prune_expired_records():
    now = time.time()
    day = 86400.0

    with get_db_transaction(immediate=True) as conn:
        # Checkpoints: 1 expired by expires_at, 1 expired by age (>7 days), 1 active
        conn.execute(
            """
            INSERT OR REPLACE INTO turn_checkpoints (
                checkpoint_id, session_id, channel_id, thread_ts, agent_key,
                pause_reason, messages_json, status, created_at, expires_at
            ) VALUES
                ('chk_exp_time', 'S:1', 'C_1', 'T_1', 'A1', 'HITL', '[]', 'EXPIRED', ?, ?),
                ('chk_old_age', 'S:2', 'C_2', 'T_2', 'A1', 'HITL', '[]', 'RESOLVED', ?, ?),
                ('chk_keep', 'S:3', 'C_3', 'T_3', 'A1', 'HITL', '[]', 'ACTIVE', ?, ?);
            """,
            (now - 100, now - 10, now - (8 * day), now + 1000, now, now + 1000),
        )

        # Traces: 1 old (>30 days), 1 recent
        conn.execute(
            """
            INSERT OR REPLACE INTO turn_traces (
                trace_id, session_id, channel_id, thread_ts, agent_key,
                stage_name, duration_ms, status, created_at
            ) VALUES
                ('tr_old', 'S:1', 'C_1', 'T_1', 'A1', 'exec', 10.0, 'OK', ?),
                ('tr_recent', 'S:2', 'C_2', 'T_2', 'A1', 'exec', 10.0, 'OK', ?);
            """,
            (now - (35 * day), now - (2 * day)),
        )

        # Event dedup: 1 old (>1 day), 1 recent
        conn.execute(
            """
            INSERT OR REPLACE INTO event_dedup (event_id, received_at) VALUES
                ('ev_old', ?),
                ('ev_recent', ?);
            """,
            (now - (2 * day), now - 60),
        )

        # HITL: 1 old past expiry (>7 days past expiry), 1 recent
        conn.execute(
            """
            INSERT OR REPLACE INTO hitl_approvals (
                request_id, channel_id, thread_ts, agent_name, skill_name,
                fn_name, fn_args, args_hash, status, created_at, expires_at
            ) VALUES
                ('req_old', 'C_1', 'T_1', 'A1', 's', 'fn', '{}', 'h1', 'DENIED', ?, ?),
                ('req_recent', 'C_2', 'T_2', 'A1', 's', 'fn', '{}', 'h2', 'PENDING', ?, ?);
            """,
            (now - (10 * day), now - (8 * day), now - 100, now + 1000),
        )

        # Thread skills: 1 old past expiry (>7 days past expiry), 1 active
        conn.execute(
            """
            INSERT OR REPLACE INTO thread_skills (
                channel_id, thread_ts, agent_id, skill_name, granted_at, expires_at
            ) VALUES
                ('C_1', 'T_1', 'A1', 'sk_old', ?, ?),
                ('C_2', 'T_2', 'A1', 'sk_active', ?, ?);
            """,
            (now - (10 * day), now - (8 * day), now, now + 1000),
        )

    res = prune_expired_records(
        retention_checkpoints_days=7,
        retention_traces_days=30,
        retention_event_dedup_days=1,
    )

    assert res["turn_checkpoints"] == 2
    assert res["turn_traces"] == 1
    assert res["event_dedup"] == 1
    assert res["hitl_approvals"] == 1
    assert res["thread_skills"] == 1

    with get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM turn_checkpoints WHERE checkpoint_id = 'chk_keep'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM turn_traces WHERE trace_id = 'tr_recent'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM event_dedup WHERE event_id = 'ev_recent'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM hitl_approvals WHERE request_id = 'req_recent'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM thread_skills WHERE skill_name = 'sk_active'").fetchone()[0] == 1


def test_forget_thread():
    cid = "C_FORGET"
    tts = "1799999999.123456"
    now = time.time()

    with get_db_transaction(immediate=True) as conn:
        conn.execute(
            "INSERT INTO thread_sessions (channel_id, thread_ts, status, created_at, last_activity) VALUES (?, ?, 'completed', ?, ?)",
            (cid, tts, now, now),
        )
        conn.execute(
            "INSERT INTO thread_context (channel_id, thread_ts, max_words) VALUES (?, ?, 200)",
            (cid, tts),
        )
        conn.execute(
            "INSERT INTO spend_ledger (channel_id, thread_ts, model_name, prompt_tokens, completion_tokens, cost_usd, timestamp) VALUES (?, ?, 'm', 10, 20, 0.01, ?)",
            (cid, tts, now),
        )
        conn.execute(
            "INSERT INTO hitl_approvals (request_id, channel_id, thread_ts, agent_name, skill_name, fn_name, fn_args, args_hash, status, created_at, expires_at) VALUES ('req_f', ?, ?, 'a', 's', 'f', '{}', 'h', 'APPROVED', ?, ?)",
            (cid, tts, now, now + 100),
        )
        conn.execute(
            "INSERT INTO thread_skills (channel_id, thread_ts, agent_id, skill_name, granted_at, expires_at) VALUES (?, ?, 'a', 's', ?, ?)",
            (cid, tts, now, now + 100),
        )
        conn.execute(
            "INSERT INTO thread_usage_meta (channel_id, thread_ts, channel_name, created_at_str, last_updated) VALUES (?, ?, 'general', '2026', ?)",
            (cid, tts, now),
        )
        conn.execute(
            "INSERT INTO thread_notes_state (channel_id, thread_ts, live_note_content, updated_at) VALUES (?, ?, 'Notes content', ?)",
            (cid, tts, now),
        )
        conn.execute(
            "INSERT INTO turn_checkpoints (checkpoint_id, session_id, channel_id, thread_ts, agent_key, pause_reason, messages_json, status, created_at, expires_at) VALUES ('chk_f', 's', ?, ?, 'a', 'p', '[]', 'ACTIVE', ?, ?)",
            (cid, tts, now, now + 100),
        )
        conn.execute(
            "INSERT INTO turn_traces (trace_id, session_id, channel_id, thread_ts, agent_key, stage_name, duration_ms, status, created_at) VALUES ('tr_f', 's', ?, ?, 'a', 'stg', 5.0, 'OK', ?)",
            (cid, tts, now),
        )

    # In-memory thread state setup
    thread_state_manager.set_remaining_agents(cid, tts, ["agent-10001"])
    thread_state_manager.touch(cid, tts)
    assert len(thread_state_manager.get_remaining_agents(cid, tts)) == 1

    # Flat history file setup
    FLAT_HISTORIES_DIR.mkdir(parents=True, exist_ok=True)
    flat_file = FLAT_HISTORIES_DIR / f"test_forget_{tts}.md"
    flat_file.write_text(f"# History\n- **Thread TS:** `{tts}`\n- **Channel ID:** `{cid}`\n", encoding="utf-8")
    assert flat_file.exists()

    # Call forget_thread
    counts = forget_thread(cid, tts)

    # Assert all tables purged
    assert counts["thread_sessions"] == 1
    assert counts["thread_context"] == 1
    assert counts["spend_ledger"] == 1
    assert counts["hitl_approvals"] == 1
    assert counts["thread_skills"] == 1
    assert counts["thread_usage_meta"] == 1
    assert counts["thread_notes_state"] == 1
    assert counts["turn_checkpoints"] == 1
    assert counts["turn_traces"] == 1
    assert counts["flat_histories"] == 1

    # Verify database state
    with get_connection() as conn:
        for tbl in [
            "thread_sessions",
            "thread_context",
            "spend_ledger",
            "hitl_approvals",
            "thread_skills",
            "thread_usage_meta",
            "thread_notes_state",
            "turn_checkpoints",
            "turn_traces",
        ]:
            c = conn.execute(f"SELECT COUNT(*) FROM {tbl} WHERE channel_id = ? AND thread_ts = ?", (cid, tts)).fetchone()[0]
            assert c == 0, f"Table {tbl} still has rows for purged thread"

    # Verify flat file deleted
    assert not flat_file.exists()

    # Verify in-memory thread state purged
    assert thread_state_manager.get_remaining_agents(cid, tts) == []
    assert (cid, tts) not in thread_state_manager.thread_last_activity


def test_forget_thread_purges_thread_notes_via_configured_dir(tmp_path):
    """TKT-1209 (H2): retention_service.forget_thread uses get_thread_notes_dir()."""
    from services.retention_service import forget_thread
    from services.thread_notes_writer import configure_paths, reset_paths

    notes_dir = tmp_path / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    target_ts = "1799888777.00123"
    note_file = notes_dir / "topic_00123_live_notes.md"
    note_file.write_text(f"- **Thread Timestamp**: `{target_ts}`\n", encoding="utf-8")

    configure_paths(thread_notes_dir=notes_dir)
    try:
        counts = forget_thread("C_NOTES_RET", target_ts)
        assert counts["thread_notes"] == 1
        assert not note_file.exists()
    finally:
        reset_paths()
