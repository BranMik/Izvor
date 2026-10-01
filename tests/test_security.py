import hashlib
import hmac
import time

from core.slack_security import verify_slack_signature


def test_verify_slack_signature_unconfigured_secret(monkeypatch):
    # If SLACK_SIGNING_SECRET is empty, verify returns False
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "")
    res = verify_slack_signature(b"{}", {})
    assert res is False

    # If SLACK_SIGNING_SECRET is default placeholder, verify returns False
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "your-slack-signing-secret")
    res_default = verify_slack_signature(b"{}", {})
    assert res_default is False


def test_verify_slack_signature_valid(monkeypatch):
    test_secret = "8f74a053265c0a1d752d47864070275e"
    monkeypatch.setenv("SLACK_SIGNING_SECRET", test_secret)

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
    monkeypatch.setenv("SLACK_SIGNING_SECRET", test_secret)

    body = b'{"type": "url_verification"}'
    timestamp = str(int(time.time()))
    headers = {
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": "v0=invalid_signature_hash",
    }

    assert verify_slack_signature(body, headers) is False


def test_verify_slack_signature_non_utf8_bytes(monkeypatch):
    test_secret = "8f74a053265c0a1d752d47864070275e"
    monkeypatch.setenv("SLACK_SIGNING_SECRET", test_secret)

    # Malformed non-UTF8 byte sequence (e.g. \xff\xfe\xfa)
    body = b"\xff\xfe\xfa\x80\x81invalid-utf8-bytes"
    timestamp = str(int(time.time()))
    sig_basestring = b"v0:" + timestamp.encode("utf-8") + b":" + body
    valid_sig = (
        "v0="
        + hmac.new(
            test_secret.encode("utf-8"),
            sig_basestring,
            hashlib.sha256,
        ).hexdigest()
    )

    headers = {
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": valid_sig,
    }

    # Must verify successfully without raising UnicodeDecodeError
    assert verify_slack_signature(body, headers) is True

