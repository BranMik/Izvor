import hashlib
import hmac
import logging
import os
import time

logger = logging.getLogger(__name__)


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
        try:
            from services.audit_logger import log_audit_event

            log_audit_event(
                event_type="AUTH_FAILURE",
                actor={"type": "slack_webhook", "id": "unverified_client"},
                action="verify_slack_signature",
                target="/slack/*",
                details={"reason": "HMAC signature mismatch"},
                severity="CRITICAL",
                status="FAILED",
            )
        except Exception:
            pass
    return is_valid

