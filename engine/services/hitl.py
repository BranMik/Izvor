import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from core.database import get_connection, get_db_transaction
from services.slack import post_message_to_slack

logger = logging.getLogger(__name__)

HITL_DEFAULT_TTL_SECONDS = 600.0  # 10 minutes


def compute_args_hash(fn_args: dict[str, Any] | None) -> str:
    """Computes a deterministic SHA-256 hash of tool call arguments."""
    try:
        normalized = json.dumps(fn_args or {}, sort_keys=True, default=str)
    except (json.JSONDecodeError, ValueError):
        normalized = str(fn_args or {})
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass
class PendingHITLRequest:
    request_id: str
    channel_id: str
    thread_ts: str
    agent_name: str
    skill_name: str
    fn_name: str
    fn_args: dict[str, Any]
    args_hash: str
    initiator_user_id: str | None
    created_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + HITL_DEFAULT_TTL_SECONDS)
    status: str = "PENDING"

    def is_expired(self) -> bool:
        return time.time() > self.expires_at


def _row_to_request(row: Any) -> PendingHITLRequest:
    try:
        fn_args = json.loads(row["fn_args"]) if row["fn_args"] else {}
    except (json.JSONDecodeError, ValueError):
        fn_args = {}
    return PendingHITLRequest(
        request_id=row["request_id"],
        channel_id=row["channel_id"],
        thread_ts=row["thread_ts"],
        agent_name=row["agent_name"],
        skill_name=row["skill_name"],
        fn_name=row["fn_name"],
        fn_args=fn_args,
        args_hash=row["args_hash"],
        initiator_user_id=row["initiator_user_id"],
        created_at=float(row["created_at"]),
        expires_at=float(row["expires_at"]),
        status=row["status"],
    )


def _prune_expired() -> None:
    now = time.time()
    try:
        with get_db_transaction(immediate=True) as conn:
            conn.execute("DELETE FROM hitl_approvals WHERE status = 'PENDING' AND expires_at < ?;", (now,))
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[HITL] Prune expired SQLite failed: {e}")


def has_pending_request_for_thread(channel_id: str, thread_ts: str) -> bool:
    """Returns True if any unexpired PENDING HITL approval exists for the thread."""
    _prune_expired()
    now = time.time()
    try:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM hitl_approvals
                WHERE channel_id = ? AND thread_ts = ? AND status = 'PENDING' AND expires_at > ?
                LIMIT 1;
                """,
                (channel_id, thread_ts, now),
            ).fetchone()
            return row is not None
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[HITL] SQLite has_pending_request_for_thread failed: {e}")
        return False


def expire_thread_requests(channel_id: str, thread_ts: str) -> int:
    """Immediately invalidates all PENDING HITL approval requests for a thread."""
    now = time.time()
    try:
        with get_db_transaction(immediate=True) as conn:
            cur = conn.execute(
                """
                UPDATE hitl_approvals
                SET status = 'DENIED', expires_at = ?
                WHERE channel_id = ? AND thread_ts = ? AND status = 'PENDING';
                """,
                (now - 1.0, channel_id, thread_ts),
            )
            return cur.rowcount
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[HITL] SQLite expire_thread_requests failed: {e}")
        return 0


def get_pending_request(request_id: str) -> PendingHITLRequest | None:
    _prune_expired()
    now = time.time()
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM hitl_approvals WHERE request_id = ? AND expires_at > ? AND status = 'PENDING';",
                (request_id, now),
            ).fetchone()
            if row:
                return _row_to_request(row)
    except Exception as e:  # noqa: BLE001
        logger.error(f"[HITL] SQLite get_pending_request failed: {e}")
    return None


def approve_hitl_request(request_id: str) -> PendingHITLRequest | None:
    """Marks a pending HITL request as approved atomically in SQLite."""
    _prune_expired()
    now = time.time()
    try:
        with get_db_transaction(immediate=True) as conn:
            cursor = conn.execute(
                """
                UPDATE hitl_approvals
                SET status = 'APPROVED'
                WHERE request_id = ? AND status = 'PENDING' AND expires_at > ?;
                """,
                (request_id, now),
            )
            if cursor.rowcount > 0:
                row = conn.execute(
                    "SELECT * FROM hitl_approvals WHERE request_id = ?;",
                    (request_id,),
                ).fetchone()
                if row:
                    return _row_to_request(row)
    except Exception as e:  # noqa: BLE001
        logger.error(f"[HITL] SQLite approve_hitl_request failed: {e}")
    return None


def deny_hitl_request(request_id: str) -> PendingHITLRequest | None:
    """Denies a pending HITL request atomically in SQLite."""
    _prune_expired()
    now = time.time()
    try:
        with get_db_transaction(immediate=True) as conn:
            cursor = conn.execute(
                """
                UPDATE hitl_approvals
                SET status = 'DENIED'
                WHERE request_id = ? AND status = 'PENDING' AND expires_at > ?;
                """,
                (request_id, now),
            )
            if cursor.rowcount > 0:
                row = conn.execute(
                    "SELECT * FROM hitl_approvals WHERE request_id = ?;",
                    (request_id,),
                ).fetchone()
                if row:
                    return _row_to_request(row)
    except Exception as e:  # noqa: BLE001
        logger.error(f"[HITL] SQLite deny_hitl_request failed: {e}")
    return None


def consume_tool_approval(
    channel_id: str,
    thread_ts: str,
    fn_name: str,
    fn_args: dict[str, Any] | None = None,
    agent_name: str | None = None,
) -> bool:
    """
    Checks and consumes a single-use approval for the given tool, arguments, and agent atomically.
    Returns True if an unexpired approval existed and was consumed; False otherwise.
    Wildcard function-level approvals without argument hash matching are rejected.
    """
    _prune_expired()
    now = time.time()
    args_hash = compute_args_hash(fn_args)

    try:
        with get_db_transaction(immediate=True) as conn:
            sql = """
                SELECT request_id FROM hitl_approvals
                WHERE channel_id = ? AND thread_ts = ? AND fn_name = ?
                  AND args_hash = ?
                  AND status = 'APPROVED' AND expires_at > ?
            """
            params: list[Any] = [channel_id, thread_ts, fn_name, args_hash, now]
            if agent_name:
                sql += " AND agent_name = ?"
                params.append(agent_name)
            sql += " LIMIT 1;"

            row = conn.execute(sql, tuple(params)).fetchone()

            if row:
                req_id = row["request_id"]
                cur = conn.execute(
                    "UPDATE hitl_approvals SET status = 'CONSUMED' WHERE request_id = ? AND status = 'APPROVED';",
                    (req_id,),
                )
                if cur.rowcount > 0:
                    return True
    except Exception as e:  # noqa: BLE001
        logger.error(f"[HITL] SQLite consume_tool_approval failed: {e}")

    return False


def register_hitl_request(
    channel_id: str,
    thread_ts: str,
    fn_name: str,
    fn_args: dict[str, Any] | None = None,
    agent_name: str = "Agent",
    skill_name: str = "Skill",
    initiator_user_id: str | None = None,
    ttl_seconds: float = HITL_DEFAULT_TTL_SECONDS,
) -> PendingHITLRequest:
    """Registers an in-memory pending request and returns the created record."""
    _prune_expired()
    request_id = str(uuid.uuid4())
    args = fn_args or {}
    args_hash = compute_args_hash(args)
    now = time.time()

    req = PendingHITLRequest(
        request_id=request_id,
        channel_id=channel_id,
        thread_ts=thread_ts,
        agent_name=agent_name,
        skill_name=skill_name,
        fn_name=fn_name,
        fn_args=args,
        args_hash=args_hash,
        initiator_user_id=initiator_user_id,
        created_at=now,
        expires_at=now + ttl_seconds,
        status="PENDING",
    )

    try:
        with get_db_transaction(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO hitl_approvals (
                    request_id, channel_id, thread_ts, agent_name, skill_name, fn_name, fn_args, args_hash, initiator_user_id, status, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?);
                """,
                (
                    request_id,
                    channel_id,
                    thread_ts,
                    agent_name,
                    skill_name,
                    fn_name,
                    json.dumps(args, sort_keys=True, default=str),
                    args_hash,
                    initiator_user_id,
                    now,
                    now + ttl_seconds,
                ),
            )
    except Exception as e:
        logger.error(f"[HITL] SQLite register_hitl_request failed: {e}")
        raise

    return req


async def request_human_approval(
    channel_id: str,
    thread_ts: str,
    agent_name: str,
    skill_name: str,
    fn_name: str,
    fn_args: dict[str, Any],
    initiator_user_id: str | None = None,
) -> str:
    """
    Creates a pending HITL request and posts an interactive approval card to Slack.
    Returns the generated request_id.
    """
    req = register_hitl_request(
        channel_id=channel_id,
        thread_ts=thread_ts,
        fn_name=fn_name,
        fn_args=fn_args,
        agent_name=agent_name,
        skill_name=skill_name,
        initiator_user_id=initiator_user_id,
    )

    args_str = str(fn_args).replace("```", "'''")[:1000]
    initiator_note = f"\n*Initiator:* <@{initiator_user_id}>" if initiator_user_id else ""
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"⚠️ *Action Required: Human-in-the-Loop Approval*\n"
                    f"Agent *{agent_name}* wants to execute a destructive tool.\n"
                    f"*Skill:* `{skill_name}`\n"
                    f"*Tool:* `{fn_name}`{initiator_note}\n"
                    f"*Arguments:* ```{args_str}```"
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve", "emoji": True},
                    "style": "primary",
                    "value": req.request_id,
                    "action_id": "hitl_approve_tool",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Deny", "emoji": True},
                    "style": "danger",
                    "value": req.request_id,
                    "action_id": "hitl_deny_tool",
                },
            ],
        },
    ]
    await post_message_to_slack(
        channel_id=channel_id,
        thread_ts=thread_ts,
        text=f"Approval required for {fn_name}",
        blocks=blocks,
    )
    return req.request_id
