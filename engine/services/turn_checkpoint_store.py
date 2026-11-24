"""
Durable Turn Checkpoint Store.
Persists in-flight agent turn state across HITL approval and circuit-breaker pauses.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from core.database import get_connection, get_db_transaction
from core.domain import TurnCheckpoint

logger = logging.getLogger(__name__)


class TurnCheckpointStore:
    """ACID SQLite store for in-flight turn execution checkpoints."""

    def save_checkpoint(self, checkpoint: TurnCheckpoint) -> str:
        """Persists a TurnCheckpoint to SQLite."""
        if not checkpoint.checkpoint_id:
            checkpoint.checkpoint_id = f"chk_{uuid.uuid4().hex[:12]}"

        messages_json = json.dumps(checkpoint.messages, default=str)
        pending_tool_call_json = (
            json.dumps(checkpoint.pending_tool_call, default=str) if checkpoint.pending_tool_call is not None else None
        )
        active_tools_json = json.dumps(checkpoint.active_tools, default=str)
        skills_used_json = json.dumps(checkpoint.skills_used, default=str)
        token_mapping_json = json.dumps(checkpoint.token_mapping, default=str)
        accumulated_history_json = json.dumps(checkpoint.accumulated_history, default=str)

        try:
            with get_db_transaction(immediate=True) as conn:
                conn.execute(
                    """
                    INSERT INTO turn_checkpoints (
                        checkpoint_id, session_id, channel_id, thread_ts, agent_key,
                        pause_reason, hitl_request_id, messages_json, pending_tool_call_json,
                        active_tools_json, skills_used_json, token_mapping_json,
                        accumulated_history_json, prompt_tokens, completion_tokens,
                        status, prompt, my_generation, created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(checkpoint_id) DO UPDATE SET
                        session_id = excluded.session_id,
                        channel_id = excluded.channel_id,
                        thread_ts = excluded.thread_ts,
                        agent_key = excluded.agent_key,
                        pause_reason = excluded.pause_reason,
                        hitl_request_id = excluded.hitl_request_id,
                        messages_json = excluded.messages_json,
                        pending_tool_call_json = excluded.pending_tool_call_json,
                        active_tools_json = excluded.active_tools_json,
                        skills_used_json = excluded.skills_used_json,
                        token_mapping_json = excluded.token_mapping_json,
                        accumulated_history_json = excluded.accumulated_history_json,
                        prompt_tokens = excluded.prompt_tokens,
                        completion_tokens = excluded.completion_tokens,
                        status = excluded.status,
                        prompt = excluded.prompt,
                        my_generation = excluded.my_generation,
                        created_at = excluded.created_at,
                        expires_at = excluded.expires_at;
                    """,
                    (
                        checkpoint.checkpoint_id,
                        checkpoint.session_id,
                        checkpoint.channel_id,
                        checkpoint.thread_ts,
                        checkpoint.agent_key,
                        checkpoint.pause_reason,
                        checkpoint.hitl_request_id,
                        messages_json,
                        pending_tool_call_json,
                        active_tools_json,
                        skills_used_json,
                        token_mapping_json,
                        accumulated_history_json,
                        checkpoint.prompt_tokens,
                        checkpoint.completion_tokens,
                        checkpoint.status,
                        checkpoint.prompt or "",
                        checkpoint.my_generation,
                        checkpoint.created_at,
                        checkpoint.expires_at,
                    ),
                )
            logger.info(
                f"[CHECKPOINT] Saved checkpoint {checkpoint.checkpoint_id} for "
                f"thread {checkpoint.thread_ts} (reason={checkpoint.pause_reason}, status={checkpoint.status})"
            )
            return checkpoint.checkpoint_id
        except Exception as e:
            logger.exception(f"[CHECKPOINT] Failed saving checkpoint {checkpoint.checkpoint_id}: {e}")
            raise

    def _row_to_checkpoint(self, row: Any) -> TurnCheckpoint:
        row_keys = row.keys() if hasattr(row, "keys") else ()
        raw_gen = row["my_generation"] if "my_generation" in row_keys else None
        return TurnCheckpoint(
            checkpoint_id=row["checkpoint_id"],
            session_id=row["session_id"],
            channel_id=row["channel_id"],
            thread_ts=row["thread_ts"],
            agent_key=row["agent_key"],
            pause_reason=row["pause_reason"],
            hitl_request_id=row["hitl_request_id"],
            messages=json.loads(row["messages_json"]) if row["messages_json"] else [],
            pending_tool_call=json.loads(row["pending_tool_call_json"]) if row["pending_tool_call_json"] else None,
            active_tools=json.loads(row["active_tools_json"]) if row["active_tools_json"] else [],
            skills_used=json.loads(row["skills_used_json"]) if row["skills_used_json"] else [],
            token_mapping=json.loads(row["token_mapping_json"]) if row["token_mapping_json"] else {},
            accumulated_history=json.loads(row["accumulated_history_json"]) if row["accumulated_history_json"] else [],
            prompt_tokens=int(row["prompt_tokens"] or 0),
            completion_tokens=int(row["completion_tokens"] or 0),
            status=row["status"],
            prompt=str(row["prompt"] or "") if "prompt" in row_keys else "",
            my_generation=int(raw_gen) if raw_gen is not None else None,
            created_at=float(row["created_at"]),
            expires_at=float(row["expires_at"]),
        )

    def get_checkpoint(self, checkpoint_id: str) -> TurnCheckpoint | None:
        """Retrieves a checkpoint by its unique ID."""
        try:
            with get_connection() as conn:
                row = conn.execute(
                    "SELECT * FROM turn_checkpoints WHERE checkpoint_id = ?;",
                    (checkpoint_id,),
                ).fetchone()
                if row:
                    return self._row_to_checkpoint(row)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[CHECKPOINT] Failed reading checkpoint {checkpoint_id}: {e}")
        return None

    def get_checkpoint_by_hitl_request(self, hitl_request_id: str) -> TurnCheckpoint | None:
        """Retrieves an active checkpoint associated with a specific HITL approval request."""
        now = time.time()
        try:
            with get_connection() as conn:
                row = conn.execute(
                    """
                    SELECT * FROM turn_checkpoints
                    WHERE hitl_request_id = ? AND status IN ('PENDING', 'ACTIVE') AND expires_at > ?
                    ORDER BY created_at DESC LIMIT 1;
                    """,
                    (hitl_request_id, now),
                ).fetchone()
                if row:
                    return self._row_to_checkpoint(row)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[CHECKPOINT] Failed reading checkpoint for hitl {hitl_request_id}: {e}")
        return None

    def get_latest_checkpoint_for_thread(
        self,
        channel_id: str,
        thread_ts: str,
        status: str | None = None,
        pause_reason: str | None = None,
    ) -> TurnCheckpoint | None:
        """Retrieves the latest checkpoint for a given thread, optionally filtered by status and pause_reason."""
        now = time.time()
        try:
            with get_connection() as conn:
                clauses = ["channel_id = ?", "thread_ts = ?"]
                params: list[Any] = [channel_id, thread_ts]
                if status is not None:
                    clauses.append("status = ?")
                    params.append(status)
                else:
                    clauses.append("status IN ('PENDING', 'ACTIVE')")
                if pause_reason is not None:
                    clauses.append("pause_reason = ?")
                    params.append(pause_reason)
                clauses.append("expires_at > ?")
                params.append(now)
                sql = f"SELECT * FROM turn_checkpoints WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT 1;"
                row = conn.execute(sql, tuple(params)).fetchone()
                if row:
                    return self._row_to_checkpoint(row)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[CHECKPOINT] Failed reading checkpoint for thread {channel_id}:{thread_ts}: {e}")
        return None

    def claim_for_resume(self, checkpoint_id: str) -> bool:
        """
        Atomically transitions an unexpired checkpoint from PENDING/ACTIVE to RESUMING
        using compare-and-swap semantics to prevent double-resumption races.
        """
        now = time.time()
        try:
            with get_db_transaction(immediate=True) as conn:
                cur = conn.execute(
                    """
                    UPDATE turn_checkpoints
                    SET status = 'RESUMING'
                    WHERE checkpoint_id = ?
                      AND status IN ('PENDING', 'ACTIVE')
                      AND expires_at > ?;
                    """,
                    (checkpoint_id, now),
                )
                return cur.rowcount > 0
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[CHECKPOINT] Failed claiming checkpoint {checkpoint_id} for resume: {e}")
            return False

    def release_claim(self, checkpoint_id: str) -> bool:
        """
        Reverts a RESUMING checkpoint back to ACTIVE without redacting payloads so
        graceful shutdown cancellation can safely resume later.
        """
        try:
            with get_db_transaction(immediate=True) as conn:
                cur = conn.execute(
                    """
                    UPDATE turn_checkpoints
                    SET status = 'ACTIVE'
                    WHERE checkpoint_id = ? AND status = 'RESUMING';
                    """,
                    (checkpoint_id,),
                )
                return cur.rowcount > 0
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[CHECKPOINT] Failed releasing claim for {checkpoint_id}: {e}")
            return False

    def update_messages(
        self,
        checkpoint_id: str,
        messages: list[dict[str, Any]],
        token_mapping: dict[str, Any] | None = None,
    ) -> bool:
        """Persists updated messages (and optional token_mapping) on an active or resuming checkpoint."""
        try:
            messages_json = json.dumps(messages, default=str)
            with get_db_transaction(immediate=True) as conn:
                if token_mapping is not None:
                    cur = conn.execute(
                        """
                        UPDATE turn_checkpoints
                        SET messages_json = ?, token_mapping_json = ?
                        WHERE checkpoint_id = ? AND status IN ('PENDING', 'ACTIVE', 'RESUMING');
                        """,
                        (messages_json, json.dumps(token_mapping, default=str), checkpoint_id),
                    )
                else:
                    cur = conn.execute(
                        """
                        UPDATE turn_checkpoints
                        SET messages_json = ?
                        WHERE checkpoint_id = ? AND status IN ('PENDING', 'ACTIVE', 'RESUMING');
                        """,
                        (messages_json, checkpoint_id),
                    )
                return cur.rowcount > 0
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[CHECKPOINT] Failed updating messages for {checkpoint_id}: {e}")
            return False

    def update_accumulated_history(
        self, checkpoint_id: str, accumulated_history: list[dict[str, Any]]
    ) -> bool:
        """Persists reduced sibling accumulated_history onto a paused checkpoint."""
        try:
            history_json = json.dumps(accumulated_history, default=str)
            with get_db_transaction(immediate=True) as conn:
                cur = conn.execute(
                    """
                    UPDATE turn_checkpoints
                    SET accumulated_history_json = ?
                    WHERE checkpoint_id = ? AND status IN ('PENDING', 'ACTIVE');
                    """,
                    (history_json, checkpoint_id),
                )
                return cur.rowcount > 0
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[CHECKPOINT] Failed updating accumulated_history for {checkpoint_id}: {e}")
            return False

    def mark_resumed(self, checkpoint_id: str) -> bool:
        """Updates checkpoint status to RESUMED and redacts payload upon continuation."""
        try:
            with get_db_transaction(immediate=True) as conn:
                cur = conn.execute(
                    """
                    UPDATE turn_checkpoints
                    SET status = 'RESUMED', messages_json = '[]', accumulated_history_json = '[]',
                        pending_tool_call_json = '{}', token_mapping_json = '{}', prompt = ''
                    WHERE checkpoint_id = ? AND status IN ('PENDING', 'ACTIVE', 'RESUMING');
                    """,
                    (checkpoint_id,),
                )
                return cur.rowcount > 0
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[CHECKPOINT] Failed marking resumed {checkpoint_id}: {e}")
            return False

    def mark_failed_resume(self, checkpoint_id: str) -> bool:
        """Transitions a claimed RESUMING checkpoint to ABANDONED on unrecoverable resume error."""
        try:
            with get_db_transaction(immediate=True) as conn:
                cur = conn.execute(
                    """
                    UPDATE turn_checkpoints
                    SET status = 'ABANDONED', messages_json = '[]', accumulated_history_json = '[]',
                        pending_tool_call_json = '{}', token_mapping_json = '{}', prompt = ''
                    WHERE checkpoint_id = ? AND status = 'RESUMING';
                    """,
                    (checkpoint_id,),
                )
                return cur.rowcount > 0
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[CHECKPOINT] Failed marking failed resume {checkpoint_id}: {e}")
            return False

    def mark_resolved(self, checkpoint_id: str) -> bool:
        """Updates checkpoint status to RESOLVED and redacts payload upon completion."""
        try:
            with get_db_transaction(immediate=True) as conn:
                cur = conn.execute(
                    """
                    UPDATE turn_checkpoints
                    SET status = 'RESOLVED', messages_json = '[]', accumulated_history_json = '[]',
                        pending_tool_call_json = '{}', token_mapping_json = '{}', prompt = ''
                    WHERE checkpoint_id = ?;
                    """,
                    (checkpoint_id,),
                )
                return cur.rowcount > 0
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[CHECKPOINT] Failed marking resolved {checkpoint_id}: {e}")
            return False

    def mark_abandoned(self, checkpoint_id: str) -> bool:
        """Updates checkpoint status to ABANDONED and redacts payload."""
        try:
            with get_db_transaction(immediate=True) as conn:
                cur = conn.execute(
                    """
                    UPDATE turn_checkpoints
                    SET status = 'ABANDONED', messages_json = '[]', accumulated_history_json = '[]',
                        pending_tool_call_json = '{}', token_mapping_json = '{}', prompt = ''
                    WHERE checkpoint_id = ?;
                    """,
                    (checkpoint_id,),
                )
                return cur.rowcount > 0
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[CHECKPOINT] Failed marking abandoned {checkpoint_id}: {e}")
            return False

    def has_active_checkpoint(self, channel_id: str, thread_ts: str) -> bool:
        """Returns True if any unexpired PENDING/ACTIVE/RESUMING checkpoint exists for the thread."""
        now = time.time()
        try:
            with get_connection() as conn:
                row = conn.execute(
                    """
                    SELECT 1 FROM turn_checkpoints
                    WHERE channel_id = ? AND thread_ts = ?
                      AND status IN ('PENDING', 'ACTIVE', 'RESUMING')
                      AND expires_at > ?
                    LIMIT 1;
                    """,
                    (channel_id, thread_ts, now),
                ).fetchone()
                return row is not None
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[CHECKPOINT] Failed checking active checkpoint for {channel_id}:{thread_ts}: {e}")
            return False

    def abandon_thread_checkpoints(self, channel_id: str, thread_ts: str) -> int:
        """Marks all PENDING/ACTIVE checkpoints for a thread as ABANDONED and redacts payloads."""
        try:
            with get_db_transaction(immediate=True) as conn:
                cur = conn.execute(
                    """
                    UPDATE turn_checkpoints
                    SET status = 'ABANDONED', messages_json = '[]', accumulated_history_json = '[]',
                        pending_tool_call_json = '{}', token_mapping_json = '{}', prompt = ''
                    WHERE channel_id = ? AND thread_ts = ? AND status IN ('PENDING', 'ACTIVE');
                    """,
                    (channel_id, thread_ts),
                )
                return cur.rowcount
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[CHECKPOINT] Failed abandoning thread checkpoints for {channel_id}:{thread_ts}: {e}")
            return 0

    def prune_expired(self) -> int:
        """Prunes checkpoints whose expiration timestamp has passed."""
        now = time.time()
        try:
            with get_db_transaction(immediate=True) as conn:
                cur = conn.execute(
                    "DELETE FROM turn_checkpoints WHERE expires_at < ?;",
                    (now,),
                )
                return cur.rowcount
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[CHECKPOINT] Failed pruning expired checkpoints: {e}")
            return 0


# Global singleton instance
turn_checkpoint_store = TurnCheckpointStore()
