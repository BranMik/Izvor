import hashlib
import hmac
import time

from core.slack_security import verify_slack_signature


def test_verify_slack_signature_mock_fallback(monkeypatch):
    # If SLACK_SIGNING_SECRET is default/empty, verify returns True
    monkeypatch.setattr("core.slack_security.SLACK_SIGNING_SECRET", "")
    res = verify_slack_signature(b"{}", {})
    assert res is True


def test_verify_slack_signature_valid(monkeypatch):
    test_secret = "8f74a053265c0a1d752d47864070275e"
    monkeypatch.setattr("core.slack_security.SLACK_SIGNING_SECRET", test_secret)

    body = b'{"type": "url_verification", "challenge": "test1234"}'
    timestamp = str(int(time.time()))
    sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
    valid_sig = (
        "v0="
        + hmac.new(
            test_secret.encode("utf-8"),
            sig_basestring.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
    )

    headers = {
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": valid_sig,
    }

    assert verify_slack_signature(body, headers) is True


def test_verify_slack_signature_invalid(monkeypatch):
    test_secret = "8f74a053265c0a1d752d47864070275e"
    monkeypatch.setattr("core.slack_security.SLACK_SIGNING_SECRET", test_secret)

    body = b'{"type": "url_verification"}'
    timestamp = str(int(time.time()))
    headers = {
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": "v0=invalid_signature_hash",
    }

    assert verify_slack_signature(body, headers) is False

