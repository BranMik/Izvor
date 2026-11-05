import ipaddress
import json
import socket
from unittest.mock import patch

import pytest

from services.web_tools import (
    check_and_increment_web_budget,
    is_ip_blocked,
    reset_turn_web_budget,
    sanitize_outbound_url,
    validate_url_security,
    _web_search_inner,
)


def test_is_ip_blocked_encapsulated_and_nat64():
    # NAT64 prefixes
    assert is_ip_blocked(ipaddress.ip_address("64:ff9b::1")) is True
    assert is_ip_blocked(ipaddress.ip_address("64:ff9b:1::1")) is True

    # 6to4 encapsulation (prefix 2002::/16 with 127.0.0.1 encapsulated -> 2002:7f00:0001::1)
    assert is_ip_blocked(ipaddress.ip_address("2002:7f00:0001::1")) is True

    # IPv4 mapped IPv6 (::ffff:127.0.0.1)
    assert is_ip_blocked(ipaddress.ip_address("::ffff:127.0.0.1")) is True

    # CGNAT
    assert is_ip_blocked(ipaddress.ip_address("100.64.0.1")) is True

    # Global public addresses
    assert is_ip_blocked(ipaddress.ip_address("8.8.8.8")) is False
    assert is_ip_blocked(ipaddress.ip_address("1.1.1.1")) is False
    assert is_ip_blocked(ipaddress.ip_address("2606:4700:4700::1111")) is False


def test_validate_url_security_port_allowlist():

    # Mock limits with default allowed ports [80, 443]
    with patch("services.web_tools.get_web_security_limits", return_value={"allowed_ports": [80, 443]}):
        # Disallowed ports
        ok, _, reason, _ = validate_url_security("http://example.com:8080/api")
        assert ok is False
        assert "Port 8080 is not allowed" in reason

        ok, _, reason, _ = validate_url_security("https://example.com:22/ssh")
        assert ok is False
        assert "Port 22 is not allowed" in reason

        # Allowed ports
        with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]):
            ok, _, reason, _ = validate_url_security("https://example.com:443/page")
            assert ok is True
            assert reason is None


def test_validate_url_security_domain_allowlist():
    custom_limits = {
        "allowed_ports": [80, 443],
        "allowed_domains": ["trusted.com", "api.github.com"],
    }
    with patch("services.web_tools.get_web_security_limits", return_value=custom_limits):
        # Untrusted domain blocked
        ok, _, reason, _ = validate_url_security("https://evil.org/exfil")
        assert ok is False
        assert "not in the configured web egress allowlist" in reason

        # Trusted domain allowed
        with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]):
            ok, _, reason, _ = validate_url_security("https://trusted.com/data")
            assert ok is True

            # Subdomain of trusted domain allowed
            ok, _, reason, _ = validate_url_security("https://sub.trusted.com/data")
            assert ok is True


def test_sanitize_outbound_url_query_stripping():
    custom_limits = {
        "allowed_domains": ["internal.corp"],
        "strip_query_on_untrusted_domains": True,
    }
    with patch("services.web_tools.get_web_security_limits", return_value=custom_limits):
        # Untrusted external URL should have query string and fragment stripped
        sanitized = sanitize_outbound_url("https://external.com/search?q=secret_token&ref=admin#frag")
        assert sanitized == "https://external.com/search"

        # Trusted domain retains query params
        sanitized_trusted = sanitize_outbound_url("https://internal.corp/search?q=safe_query")
        assert sanitized_trusted == "https://internal.corp/search?q=safe_query"


def test_per_turn_web_fetch_budget():
    reset_turn_web_budget()
    custom_limits = {"max_fetches_per_turn": 3}

    with patch("services.web_tools.get_web_security_limits", return_value=custom_limits):
        # First 3 calls succeed
        check_and_increment_web_budget()
        check_and_increment_web_budget()
        check_and_increment_web_budget()

        # 4th call raises PermissionError
        with pytest.raises(PermissionError) as exc_info:
            check_and_increment_web_budget()
        assert "Web fetch budget exceeded" in str(exc_info.value)

    # Calling reset permits fetches again
    reset_turn_web_budget()
    with patch("services.web_tools.get_web_security_limits", return_value=custom_limits):
        check_and_increment_web_budget()


@pytest.mark.anyio
async def test_web_search_ssrf_blocked_on_endpoint():
    with patch("services.web_tools.get_web_search_endpoint", return_value="http://127.0.0.1:8000/search"):
        res = await _web_search_inner("test query")
        data = json.loads(res)
        assert "error" in data
        assert "Security Block on search endpoint" in data["error"]
