import hashlib
import hmac
import logging
import time

from core.config import SLACK_SIGNING_SECRET

logger = logging.getLogger(__name__)


def verify_slack_signature(body_bytes: bytes, headers: dict) -> bool:
    """
    Verifies that the incoming HTTP request is genuinely sent from Slack
    using HMAC SHA256 signature verification.
    """
    # If signing secret is not configured or is default placeholder, reject request
    if not SLACK_SIGNING_SECRET or SLACK_SIGNING_SECRET == "your-slack-signing-secret":
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

    sig_basestring = f"v0:{timestamp}:{body_bytes.decode('utf-8')}"
    my_signature = (
        "v0="
        + hmac.new(
            SLACK_SIGNING_SECRET.encode("utf-8"),
            sig_basestring.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
    )

    is_valid = hmac.compare_digest(my_signature, slack_signature)
    if not is_valid:
        logger.error("Slack signature verification failed!")
    return is_valid
