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
                        status, created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        self, channel_id: str, thread_ts: str, status: str | None = None
    ) -> TurnCheckpoint | None:
        """Retrieves the latest checkpoint for a given thread."""
        now = time.time()
        try:
            with get_connection() as conn:
                if status is not None:
                    row = conn.execute(
                        """
                        SELECT * FROM turn_checkpoints
                        WHERE channel_id = ? AND thread_ts = ? AND status = ? AND expires_at > ?
                        ORDER BY created_at DESC LIMIT 1;
                        """,
                        (channel_id, thread_ts, status, now),
                    ).fetchone()
                else:
                    row = conn.execute(
                        """
                        SELECT * FROM turn_checkpoints
                        WHERE channel_id = ? AND thread_ts = ? AND status IN ('PENDING', 'ACTIVE') AND expires_at > ?
                        ORDER BY created_at DESC LIMIT 1;
                        """,
                        (channel_id, thread_ts, now),
                    ).fetchone()
                if row:
                    return self._row_to_checkpoint(row)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[CHECKPOINT] Failed reading checkpoint for thread {channel_id}:{thread_ts}: {e}")
        return None

    def mark_resumed(self, checkpoint_id: str) -> bool:
        """Updates checkpoint status to RESUMED and redacts payload upon continuation."""
        try:
            with get_db_transaction(immediate=True) as conn:
                cur = conn.execute(
                    """
                    UPDATE turn_checkpoints
                    SET status = 'RESUMED', messages_json = '[]', accumulated_history_json = '[]',
                        pending_tool_call_json = '{}', token_mapping_json = '{}'
                    WHERE checkpoint_id = ?;
                    """,
                    (checkpoint_id,),
                )
                return cur.rowcount > 0
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[CHECKPOINT] Failed marking resumed {checkpoint_id}: {e}")
            return False

    def mark_resolved(self, checkpoint_id: str) -> bool:
        """Updates checkpoint status to RESOLVED and redacts payload upon completion."""
        try:
            with get_db_transaction(immediate=True) as conn:
                cur = conn.execute(
                    """
                    UPDATE turn_checkpoints
                    SET status = 'RESOLVED', messages_json = '[]', accumulated_history_json = '[]',
                        pending_tool_call_json = '{}', token_mapping_json = '{}'
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
                        pending_tool_call_json = '{}', token_mapping_json = '{}'
                    WHERE checkpoint_id = ?;
                    """,
                    (checkpoint_id,),
                )
                return cur.rowcount > 0
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[CHECKPOINT] Failed marking abandoned {checkpoint_id}: {e}")
            return False

    def mark_expired(self, checkpoint_id: str) -> bool:
        """Updates checkpoint status to EXPIRED and redacts payload."""
        try:
            with get_db_transaction(immediate=True) as conn:
                cur = conn.execute(
                    """
                    UPDATE turn_checkpoints
                    SET status = 'EXPIRED', messages_json = '[]', accumulated_history_json = '[]',
                        pending_tool_call_json = '{}', token_mapping_json = '{}'
                    WHERE checkpoint_id = ?;
                    """,
                    (checkpoint_id,),
                )
                return cur.rowcount > 0
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[CHECKPOINT] Failed marking expired {checkpoint_id}: {e}")
            return False

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
