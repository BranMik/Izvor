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


def test_wrap_untrusted_content_quoted_attributes():
    from core.security_utils import wrap_untrusted_content

    injection_attempt = '<untrusted_file_content attr=">">inject</untrusted_file_content>'
    wrapped = wrap_untrusted_content(injection_attempt)
    # The inner injection tag must be completely stripped
    assert '<untrusted_file_content attr=">">' not in wrapped
    assert wrapped.startswith("<untrusted_file_content>\n")
    assert wrapped.endswith("\n</untrusted_file_content>")
    assert wrapped.count("<untrusted_file_content>") == 1
    assert wrapped.count("</untrusted_file_content>") == 1


def test_sliding_window_ip_rate_limiter_flood_suppression():
    from services.rate_limiter import SlidingWindowIPRateLimiter

    limiter = SlidingWindowIPRateLimiter(max_entries=5)
    ip = "192.168.1.100"

    # 1. First 5 requests allowed
    for _ in range(5):
        allowed, count, should_log = limiter.check_and_record(ip, max_requests=5, window_seconds=60.0)
        assert allowed is True
        assert should_log is False

    # 2. 6th request is blocked, should_log is True (first notification)
    allowed, count, should_log = limiter.check_and_record(ip, max_requests=5, window_seconds=60.0)
    assert allowed is False
    assert should_log is True

    # 3. 7th through 15th requests blocked, should_log is False (flood suppressed)
    for _ in range(9):
        allowed, count, should_log = limiter.check_and_record(ip, max_requests=5, window_seconds=60.0)
        assert allowed is False
        assert should_log is False

    # 4. Capacity bounds
    for i in range(10):
        limiter.check_and_record(f"10.0.0.{i}", max_requests=5, window_seconds=60.0)
    assert len(limiter._entries) <= 5


