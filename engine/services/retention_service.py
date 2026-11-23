"""
Data Retention Service for Izvor.
Provides lifecycle pruning of expired records and GDPR-compliant thread data erasure (forget_thread).
"""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import logging
import shutil
import threading
import time

from config_store.system_settings import (
    get_retention_checkpoints_days,
    get_retention_event_dedup_days,
    get_retention_traces_days,
)
from core.database import get_db_transaction

logger = logging.getLogger(__name__)

_purge_hooks_lock = threading.RLock()
_thread_purge_hooks: dict[str, Callable[[str, str], None]] = {}


def register_thread_purge_hook(name: str, hook: Callable[[str, str], None]) -> None:
    """Registers an in-memory purge callback invoked by forget_thread(channel_id, thread_ts)."""
    with _purge_hooks_lock:
        _thread_purge_hooks[name] = hook


def unregister_thread_purge_hook(name: str) -> None:
    """Unregisters a previously registered thread purge hook."""
    with _purge_hooks_lock:
        _thread_purge_hooks.pop(name, None)


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
        retention_checkpoints_days if retention_checkpoints_days is not None else get_retention_checkpoints_days()
    )
    trace_days = retention_traces_days if retention_traces_days is not None else get_retention_traces_days()
    event_days = (
        retention_event_dedup_days if retention_event_dedup_days is not None else get_retention_event_dedup_days()
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
    except Exception as e:  # noqa: BLE001
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

    # Remove flat history file(s) (strictly channel-scoped)
    flat_history_deleted = 0
    try:
        from services.flat_history_recorder import get_flat_histories_dir

        flat_dir = get_flat_histories_dir()
        if flat_dir.exists():
            expected_cid_hdr = f"**Channel ID:** `{cid}`"
            expected_ts_hdr = f"**Thread TS:** `{tts}`"
            for p in list(flat_dir.glob("*.md")):
                try:
                    content = p.read_text(encoding="utf-8", errors="ignore")
                    if expected_cid_hdr in content and expected_ts_hdr in content:
                        p.unlink(missing_ok=True)
                        flat_history_deleted += 1
                except OSError:
                    pass
    except OSError as fe:
        logger.debug(f"[RETENTION] Error removing flat history file for {thread_identifier}: {fe}")
    purged_counts["flat_histories"] = flat_history_deleted

    # Remove thread notes file(s) (strictly channel-scoped)
    thread_notes_deleted = 0
    try:
        from services.thread_notes_writer import get_thread_notes_dir

        notes_dir = get_thread_notes_dir()
        if notes_dir.exists():
            cid_hdrs = (f"**Channel ID**: `{cid}`", f"**Channel ID:** `{cid}`")
            ts_hdrs = (f"**Thread Timestamp**: `{tts}`", f"**Thread Timestamp:** `{tts}`")
            for p in list(notes_dir.glob("*.md")):
                try:
                    content = p.read_text(encoding="utf-8", errors="ignore")
                    if any(h in content for h in cid_hdrs) and any(h in content for h in ts_hdrs):
                        p.unlink(missing_ok=True)
                        thread_notes_deleted += 1
                except OSError:
                    pass
    except OSError as ne:
        logger.debug(f"[RETENTION] Error removing thread notes files for {thread_identifier}: {ne}")
    purged_counts["thread_notes"] = thread_notes_deleted

    # Remove thread-scoped downloaded attachments subdirectory
    downloads_deleted = 0
    try:
        from core.paths import get_downloads_dir
        from services.slack.files import get_thread_downloads_subdir

        downloads_dir = get_downloads_dir().resolve()
        subdir = get_thread_downloads_subdir(cid, tts, downloads_dir=downloads_dir)
        if subdir.exists() and subdir.is_dir():
            downloads_deleted = sum(1 for f in subdir.rglob("*") if f.is_file())
            shutil.rmtree(subdir)
    except (OSError, PermissionError, ValueError) as de:
        logger.debug(f"[RETENTION] Error removing downloads subdir for {thread_identifier}: {de}")
    purged_counts["downloads"] = downloads_deleted

    # Clear guardrail LRU embedding cache
    try:
        from services.security.guardrail import guardrail_service

        guardrail_service.clear_cache()
    except Exception as ge:  # noqa: BLE001
        logger.debug(f"[RETENTION] Error clearing guardrail cache for {thread_identifier}: {ge}")

    # Clear in-memory thread state and tracking via registered purge hooks (decouples L2 from L3)
    with _purge_hooks_lock:
        hooks_snapshot = list(_thread_purge_hooks.items())
    for hook_name, hook_fn in hooks_snapshot:
        try:
            hook_fn(cid, tts)
        except Exception as he:  # noqa: BLE001
            logger.debug(f"[RETENTION] Error executing thread purge hook '{hook_name}' for {thread_identifier}: {he}")

    try:
        from pipeline.spend_governance import spend_governance

        spend_governance.clear_session(f"{cid}:{tts}")
    except Exception as sge:  # noqa: BLE001
        logger.debug(f"[RETENTION] Error clearing spend_governance for {thread_identifier}: {sge}")

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
    except Exception as ae:  # noqa: BLE001
        logger.warning(f"[RETENTION] Failed logging audit event for forget_thread {thread_identifier}: {ae}")

    logger.info(
        f"[RETENTION] Successfully forgot thread {cid}:{tts} "
        f"(hash: {thread_hash[:12]}..., {total_records} records removed)"
    )
    return purged_counts
