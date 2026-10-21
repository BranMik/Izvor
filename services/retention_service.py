"""
Data Retention Service for Izvor.
Provides lifecycle pruning of expired records and GDPR-compliant thread data erasure (forget_thread).
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

from core.database import get_db_transaction
from orchestration.system_settings import (
    get_retention_checkpoints_days,
    get_retention_event_dedup_days,
    get_retention_traces_days,
)

logger = logging.getLogger(__name__)


def prune_expired_records(
    retention_checkpoints_days: int | None = None,
    retention_traces_days: int | None = None,
    retention_event_dedup_days: int | None = None,
) -> dict[str, int]:
    """
    Deletes expired records across SQLite tables:
    - turn_checkpoints older than retention_checkpoints_days or where expires_at < now
    - turn_traces older than retention_traces_days
    - event_dedup older than retention_event_dedup_days (default 1 day)
    - hitl_approvals and thread_skills older than 7 days past expiration timestamp
    """
    chk_days = (
        retention_checkpoints_days
        if retention_checkpoints_days is not None
        else get_retention_checkpoints_days()
    )
    trace_days = (
        retention_traces_days
        if retention_traces_days is not None
        else get_retention_traces_days()
    )
    event_days = (
        retention_event_dedup_days
        if retention_event_dedup_days is not None
        else get_retention_event_dedup_days()
    )

    now = time.time()
    chk_cutoff = now - (chk_days * 86400.0)
    trace_cutoff = now - (trace_days * 86400.0)
    event_cutoff = now - (event_days * 86400.0)
    expired_grace_cutoff = now - (7 * 86400.0)

    pruned_counts: dict[str, int] = {
        "turn_checkpoints": 0,
        "turn_traces": 0,
        "event_dedup": 0,
        "hitl_approvals": 0,
        "thread_skills": 0,
    }

    try:
        with get_db_transaction(immediate=True) as conn:
            # 1. Turn Checkpoints
            c_chk = conn.execute(
                "DELETE FROM turn_checkpoints WHERE expires_at < ? OR created_at < ?;",
                (now, chk_cutoff),
            )
            pruned_counts["turn_checkpoints"] = c_chk.rowcount

            # 2. Turn Traces
            c_traces = conn.execute(
                "DELETE FROM turn_traces WHERE created_at < ?;",
                (trace_cutoff,),
            )
            pruned_counts["turn_traces"] = c_traces.rowcount

            # 3. Event Deduplication
            c_events = conn.execute(
                "DELETE FROM event_dedup WHERE received_at < ?;",
                (event_cutoff,),
            )
            pruned_counts["event_dedup"] = c_events.rowcount

            # 4. Expired HITL approvals (7 days past expiry)
            c_hitl = conn.execute(
                "DELETE FROM hitl_approvals WHERE expires_at < ?;",
                (expired_grace_cutoff,),
            )
            pruned_counts["hitl_approvals"] = c_hitl.rowcount

            # 5. Expired thread skills (7 days past expiry)
            c_skills = conn.execute(
                "DELETE FROM thread_skills WHERE expires_at < ?;",
                (expired_grace_cutoff,),
            )
            pruned_counts["thread_skills"] = c_skills.rowcount

        logger.info(
            f"[RETENTION] Pruned expired records: "
            f"checkpoints={pruned_counts['turn_checkpoints']}, "
            f"traces={pruned_counts['turn_traces']}, "
            f"event_dedup={pruned_counts['event_dedup']}, "
            f"hitl={pruned_counts['hitl_approvals']}, "
            f"skills={pruned_counts['thread_skills']}"
        )
    except Exception as e:
        logger.error(f"[RETENTION] Error pruning expired records: {e}")

    return pruned_counts


def forget_thread(channel_id: str, thread_ts: str) -> dict[str, int]:
    """
    Atomically purges all rows for (channel_id, thread_ts) across SQLite tables,
    deletes flat history files, cleans in-memory thread state, and emits an audit event.
    """
    cid = str(channel_id).strip()
    tts = str(thread_ts).strip()
    thread_identifier = f"{cid}:{tts}"
    thread_hash = hashlib.sha256(thread_identifier.encode("utf-8")).hexdigest()

    purged_counts: dict[str, int] = {}
    tables = [
        "thread_sessions",
        "thread_context",
        "spend_ledger",
        "hitl_approvals",
        "thread_skills",
        "thread_usage_meta",
        "thread_notes_state",
        "turn_checkpoints",
        "turn_traces",
    ]

    try:
        with get_db_transaction(immediate=True) as conn:
            for tbl in tables:
                cur = conn.execute(
                    f"DELETE FROM {tbl} WHERE channel_id = ? AND thread_ts = ?;",
                    (cid, tts),
                )
                purged_counts[tbl] = cur.rowcount
    except Exception as e:
        logger.error(f"[RETENTION] Database error during forget_thread for {thread_identifier}: {e}")
        raise

    # Remove flat history file(s)
    flat_history_deleted = 0
    try:
        from core.paths import FLAT_HISTORIES_DIR

        if FLAT_HISTORIES_DIR.exists():
            clean_ts = "".join(ch for ch in tts if ch.isalnum())
            for p in list(FLAT_HISTORIES_DIR.glob("*.md")):
                try:
                    content = p.read_text(encoding="utf-8", errors="ignore")
                    if f"**Thread TS:** `{tts}`" in content or (clean_ts and f"_{clean_ts}" in p.stem):
                        p.unlink(missing_ok=True)
                        flat_history_deleted += 1
                except OSError:
                    pass
    except Exception as fe:
        logger.debug(f"[RETENTION] Error removing flat history file for {thread_identifier}: {fe}")
    purged_counts["flat_histories"] = flat_history_deleted

    # Clear in-memory thread state
    try:
        from orchestration.thread_state import thread_state_manager

        thread_state_manager.cleanup_thread(cid, tts)
    except Exception as se:
        logger.debug(f"[RETENTION] Error clearing thread_state_manager for {thread_identifier}: {se}")

    # Emit audit event
    total_records = sum(purged_counts.values())
    try:
        from services.audit_logger import log_audit_event

        log_audit_event(
            event_type="THREAD_DATA_PURGED",
            actor={"type": "system", "id": "retention_service"},
            action="forget_thread",
            target=thread_hash,
            details={
                "channel_id": cid,
                "thread_hash": thread_hash,
                "purged_records": total_records,
                "table_counts": purged_counts,
            },
            severity="INFO",
            status="SUCCESS",
        )
    except Exception as ae:
        logger.warning(f"[RETENTION] Failed logging audit event for forget_thread {thread_identifier}: {ae}")

    logger.info(
        f"[RETENTION] Successfully forgot thread {cid}:{tts} "
        f"(hash: {thread_hash[:12]}..., {total_records} records removed)"
    )
    return purged_counts
