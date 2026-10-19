import hashlib
import hmac
import logging
import os
import re
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# Sliding window rate-limiting for AUTH_FAILURE audit logs to prevent disk/log flooding
_AUTH_FAILURE_WINDOW_SECONDS = 60.0
_AUTH_FAILURE_BURST_LIMIT = 5
_GLOBAL_AUTH_FAILURE_BURST_LIMIT = 25
_MAX_TRACKER_ENTRIES = 1000


def _get_security_burst_limits() -> dict[str, Any]:
    try:
        from orchestration.system_settings import system_settings_manager
        return system_settings_manager.get_security_burst_limits()
    except Exception:
        return {
            "auth_failure_window_seconds": _AUTH_FAILURE_WINDOW_SECONDS,
            "auth_failure_burst_limit": _AUTH_FAILURE_BURST_LIMIT,
            "global_auth_failure_burst_limit": _GLOBAL_AUTH_FAILURE_BURST_LIMIT,
            "max_tracker_entries": _MAX_TRACKER_ENTRIES,
        }


_auth_failure_lock = threading.Lock()
# Mapping of client_key -> {"window_start": float, "logged_count": int, "suppressed_count": int}
_auth_failure_tracker: dict[str, dict[str, Any]] = {}
_global_burst_window_start: float = 0.0
_global_burst_count: int = 0


def _extract_client_ip(headers: dict) -> str:
    """
    Extracts client IP from headers safely.
    Only trusts proxy headers (X-Forwarded-For, X-Real-IP) if TRUST_PROXY_HEADERS is explicitly enabled.
    """
    trust_proxy = os.getenv("TRUST_PROXY_HEADERS", "false").lower() in ("1", "true", "yes")
    if not trust_proxy:
        return "direct_client"

    client_ip = (
        headers.get("x-forwarded-for")
        or headers.get("X-Forwarded-For")
        or headers.get("x-real-ip")
        or headers.get("X-Real-IP")
        or ""
    )
    if client_ip and "," in client_ip:
        client_ip = client_ip.split(",")[0].strip()
    return client_ip.strip() if client_ip else "direct_client"


def _record_auth_failure_audit(client_id: str) -> None:
    """Logs an AUTH_FAILURE audit event with sliding-window burst suppression and bounded tracker."""
    global _global_burst_window_start, _global_burst_count
    limits = _get_security_burst_limits()
    window_secs = float(limits.get("auth_failure_window_seconds", _AUTH_FAILURE_WINDOW_SECONDS))
    burst_limit = int(limits.get("auth_failure_burst_limit", _AUTH_FAILURE_BURST_LIMIT))
    global_burst_limit = int(limits.get("global_auth_failure_burst_limit", _GLOBAL_AUTH_FAILURE_BURST_LIMIT))
    max_entries = int(limits.get("max_tracker_entries", _MAX_TRACKER_ENTRIES))

    now = time.time()
    should_log = False
    summary_to_log: int | None = None

    with _auth_failure_lock:
        if now - _global_burst_window_start >= window_secs:
            _global_burst_window_start = now
            _global_burst_count = 0

        # Periodic cleanup of expired entries
        expired_keys = [
            k
            for k, v in _auth_failure_tracker.items()
            if now - v.get("window_start", 0.0) >= window_secs
        ]
        for k in expired_keys:
            if k != client_id:
                _auth_failure_tracker.pop(k, None)

        # Cap tracker capacity
        while len(_auth_failure_tracker) >= max_entries and client_id not in _auth_failure_tracker:
            oldest_key = min(
                _auth_failure_tracker.keys(),
                key=lambda k: _auth_failure_tracker[k].get("window_start", 0.0),
            )
            _auth_failure_tracker.pop(oldest_key, None)

        state = _auth_failure_tracker.get(client_id)
        if not state or (now - state["window_start"]) >= window_secs:
            if state and state.get("suppressed_count", 0) > 0:
                summary_to_log = state["suppressed_count"]
            _auth_failure_tracker[client_id] = {
                "window_start": now,
                "logged_count": 1,
                "suppressed_count": 0,
            }
            if _global_burst_count < global_burst_limit:
                _global_burst_count += 1
                should_log = True
        else:
            if state["logged_count"] < burst_limit:
                state["logged_count"] += 1
                if _global_burst_count < global_burst_limit:
                    _global_burst_count += 1
                    should_log = True
                else:
                    state["suppressed_count"] += 1
            else:
                state["suppressed_count"] += 1

    try:
        from services.audit_logger import log_audit_event

        if summary_to_log:
            log_audit_event(
                event_type="AUTH_FAILURE",
                actor={"type": "slack_webhook", "id": client_id},
                action="verify_slack_signature",
                target="/slack/*",
                details={
                    "reason": "HMAC signature mismatch (burst aggregation)",
                    "suppressed_attempts_count": summary_to_log,
                    "window_seconds": int(window_secs),
                },
                severity="CRITICAL",
                status="FAILED",
            )

        if should_log:
            log_audit_event(
                event_type="AUTH_FAILURE",
                actor={"type": "slack_webhook", "id": client_id},
                action="verify_slack_signature",
                target="/slack/*",
                details={"reason": "HMAC signature mismatch"},
                severity="CRITICAL",
                status="FAILED",
            )
    except Exception as e:
        logger.debug(f"[SECURITY] Failed to record auth failure audit: {e}")


def verify_slack_signature(body_bytes: bytes, headers: dict) -> bool:
    """
    Verifies that the incoming HTTP request is genuinely sent from Slack
    using HMAC SHA256 signature verification.
    """
    signing_secret = os.getenv("SLACK_SIGNING_SECRET", "")

    # If signing secret is not configured or is default placeholder, reject request
    if not signing_secret or signing_secret == "your-slack-signing-secret":
        logger.error(
            "SLACK_SIGNING_SECRET is not configured or has default placeholder. Rejecting request."
        )
        return False

    timestamp = headers.get(
        "x-slack-request-timestamp", headers.get("X-Slack-Request-Timestamp")
    )
    slack_signature = headers.get("x-slack-signature", headers.get("X-Slack-Signature"))

    if not timestamp or not slack_signature:
        logger.error("Missing Slack signature or timestamp in request headers.")
        return False

    # Protect against replay attacks (ignore requests older than 5 minutes)
    try:
        req_timestamp = int(timestamp)
        if abs(time.time() - req_timestamp) > 60 * 5:
            logger.error("Slack request timestamp is out of date (> 5 mins).")
            return False
    except ValueError:
        logger.error("Invalid Slack request timestamp header.")
        return False

    sig_basestring = (
        b"v0:"
        + str(timestamp).encode("utf-8")
        + b":"
        + (body_bytes if isinstance(body_bytes, bytes) else str(body_bytes).encode("utf-8"))
    )
    my_signature = (
        "v0="
        + hmac.new(
            signing_secret.encode("utf-8"),
            sig_basestring,
            hashlib.sha256,
        ).hexdigest()
    )

    is_valid = hmac.compare_digest(my_signature, slack_signature)
    if not is_valid:
        logger.error("Slack signature verification failed!")
        client_ip = _extract_client_ip(headers)
        _record_auth_failure_audit(client_ip)

    return is_valid


_BROADCAST_MENTION_PATTERN = re.compile(r"<!(channel|here|everyone)>", re.IGNORECASE)


def sanitize_slack_broadcast_mentions(text: str) -> str:
    """
    Neutralizes broadcast mentions (<!channel>, <!here>, <!everyone>) in outgoing
    text by converting them to safe plain text (@channel, @here, @everyone).
    Prevents LLM outputs or automated messages from pinging entire workspaces or channels.
    """
    if not text:
        return text
    return _BROADCAST_MENTION_PATTERN.sub(r"@\1", text)


def sanitize_slack_blocks(
    blocks: list[dict[str, Any]] | None,
    user_id: str = "system",
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """
    Recursively scans and sanitizes Block Kit blocks for sensitive data / leaked secrets
    and neutralizes broadcast mentions (<!channel>, etc.).
    Returns: (sanitized_blocks, matched_secret_rule_name_or_None).
    """
    if not blocks:
        return blocks, None

    import copy
    from services.security import scan_output_for_secrets

    blocks_copy = copy.deepcopy(blocks)
    matched_rule: str | None = None

    def _walk_and_sanitize(obj: Any) -> Any:
        nonlocal matched_rule
        if isinstance(obj, str):
            if matched_rule is None:
                rule = scan_output_for_secrets(obj, user_id=user_id)
                if rule:
                    matched_rule = rule
            return sanitize_slack_broadcast_mentions(obj)
        elif isinstance(obj, dict):
            return {k: _walk_and_sanitize(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [_walk_and_sanitize(item) for item in obj]
        return obj

    sanitized = _walk_and_sanitize(blocks_copy)
    return sanitized, matched_rule



