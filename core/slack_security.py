import hashlib
import hmac
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# Sliding window rate-limiting for AUTH_FAILURE audit logs to prevent disk/log flooding
_AUTH_FAILURE_WINDOW_SECONDS = 60.0
_AUTH_FAILURE_BURST_LIMIT = 5
_auth_failure_lock = threading.Lock()
# Mapping of client_key -> {"window_start": float, "logged_count": int, "suppressed_count": int}
_auth_failure_tracker: dict[str, dict[str, Any]] = {}


def _record_auth_failure_audit(client_id: str) -> None:
    """Logs an AUTH_FAILURE audit event with sliding-window burst suppression."""
    now = time.time()
    should_log = False
    summary_to_log: int | None = None

    with _auth_failure_lock:
        state = _auth_failure_tracker.get(client_id)
        if not state or (now - state["window_start"]) >= _AUTH_FAILURE_WINDOW_SECONDS:
            if state and state.get("suppressed_count", 0) > 0:
                summary_to_log = state["suppressed_count"]
            _auth_failure_tracker[client_id] = {
                "window_start": now,
                "logged_count": 1,
                "suppressed_count": 0,
            }
            should_log = True
        else:
            if state["logged_count"] < _AUTH_FAILURE_BURST_LIMIT:
                state["logged_count"] += 1
                should_log = True
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
                    "window_seconds": int(_AUTH_FAILURE_WINDOW_SECONDS),
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
        client_ip = (
            headers.get("x-forwarded-for", "")
            or headers.get("x-real-ip", "")
            or "unverified_client"
        )
        if "," in client_ip:
            client_ip = client_ip.split(",")[0].strip()
        _record_auth_failure_audit(client_ip or "unverified_client")

    return is_valid


