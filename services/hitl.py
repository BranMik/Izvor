import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from services.slack_client import slack_async_client, slack_rate_limiter

logger = logging.getLogger(__name__)

HITL_DEFAULT_TTL_SECONDS = 600.0  # 10 minutes


def compute_args_hash(fn_args: dict[str, Any] | None) -> str:
    """Computes a deterministic SHA-256 hash of tool call arguments."""
    try:
        normalized = json.dumps(fn_args or {}, sort_keys=True, default=str)
    except Exception:
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


# In-memory stores
PENDING_REQUESTS: dict[str, PendingHITLRequest] = {}
# Key: (channel_id, thread_ts, fn_name, args_hash) -> expires_at
APPROVED_TOKENS: dict[tuple[str, str, str, str], float] = {}


def _prune_expired() -> None:
    now = time.time()
    expired_reqs = [r_id for r_id, req in PENDING_REQUESTS.items() if req.is_expired()]
    for r_id in expired_reqs:
        PENDING_REQUESTS.pop(r_id, None)

    expired_tokens = [k for k, exp in APPROVED_TOKENS.items() if exp < now]
    for k in expired_tokens:
        APPROVED_TOKENS.pop(k, None)


def reset_pending_requests() -> None:
    """Clears all pending requests and approved tokens (used for testing)."""
    PENDING_REQUESTS.clear()
    APPROVED_TOKENS.clear()


def get_pending_request(request_id: str) -> PendingHITLRequest | None:
    _prune_expired()
    return PENDING_REQUESTS.get(request_id)


def approve_hitl_request(request_id: str) -> PendingHITLRequest | None:
    """Marks a pending HITL request as approved, storing a single-use token."""
    _prune_expired()
    req = PENDING_REQUESTS.pop(request_id, None)
    if not req:
        return None
    if req.is_expired():
        return None

    key = (req.channel_id, req.thread_ts, req.fn_name, req.args_hash)
    APPROVED_TOKENS[key] = req.expires_at
    req.status = "APPROVED"
    return req


def deny_hitl_request(request_id: str) -> PendingHITLRequest | None:
    """Denies and purges a pending HITL request."""
    _prune_expired()
    req = PENDING_REQUESTS.pop(request_id, None)
    if req:
        req.status = "DENIED"
    return req


def consume_tool_approval(
    channel_id: str,
    thread_ts: str,
    fn_name: str,
    fn_args: dict[str, Any] | None = None,
) -> bool:
    """
    Checks and consumes a single-use approval for the given tool and arguments.
    Returns True if an unexpired approval existed and was consumed; False otherwise.
    """
    _prune_expired()
    args_hash = compute_args_hash(fn_args)
    key = (channel_id, thread_ts, fn_name, args_hash)
    if key in APPROVED_TOKENS:
        exp = APPROVED_TOKENS.pop(key)
        if time.time() <= exp:
            return True

    # Check wildcard/legacy empty arguments hash if fn_args was provided but empty
    empty_key = (channel_id, thread_ts, fn_name, compute_args_hash({}))
    if empty_key in APPROVED_TOKENS:
        exp = APPROVED_TOKENS.pop(empty_key)
        if time.time() <= exp:
            return True

    return False


def mark_tool_approved(
    channel_id: str,
    thread_ts: str,
    fn_name: str,
    fn_args: dict[str, Any] | None = None,
    ttl_seconds: float = HITL_DEFAULT_TTL_SECONDS,
) -> None:
    """Manually marks a tool as approved (used by tests and programmatic callers)."""
    args_hash = compute_args_hash(fn_args)
    key = (channel_id, thread_ts, fn_name, args_hash)
    APPROVED_TOKENS[key] = time.time() + ttl_seconds


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
    PENDING_REQUESTS[request_id] = req
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

    args_str = str(fn_args)[:1000]
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
    await slack_rate_limiter.acquire()
    await slack_async_client.chat_postMessage(
        channel=channel_id,
        thread_ts=thread_ts,
        text=f"Approval required for {fn_name}",
        blocks=blocks,
    )
    return req.request_id
