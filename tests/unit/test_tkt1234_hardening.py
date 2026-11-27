"""Unit tests for TKT-1234 hardening: web_tools, local_tools, and core/logging_filters."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
from pathlib import Path
import socket
from unittest.mock import patch

import pytest

from core.domain import SYSTEM_PRINCIPAL
from core.logging_filters import CredentialRedactionFilter, redact_text, setup_credential_redaction_filters
from services import local_tools
from services.web_tools import fetch_web_page, reset_turn_web_budget, validate_url_security


def test_validate_url_security_enforces_implicit_default_ports():
    """Default port 80 (http) / 443 (https) must be evaluated against allowed_ports even when omitted in URL."""
    with (
        patch("services.web_tools.get_web_security_limits", return_value={"allowed_ports": [443]}),
        patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]),
    ):
        ok_http, _, reason_http, _ = validate_url_security("http://example.com/insecure")
        assert ok_http is False
        assert "Port 80 is not allowed" in reason_http

        ok_https, _, reason_https, _ = validate_url_security("https://example.com/secure")
        assert ok_https is True
        assert reason_https is None


@pytest.mark.anyio
async def test_fetch_web_page_fails_closed_on_missing_or_empty_content_type():
    """Missing or blank Content-Type must be rejected fail-closed."""

    class _NoCTResponse:
        def __init__(self, headers):
            self.status_code = 200
            self.headers = headers
            self.encoding = "utf-8"

        async def aiter_bytes(self, chunk_size=65536):
            yield b"<html><body>hello</body></html>"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    reset_turn_web_budget()
    try:
        for hdrs in ({}, {"content-type": ""}, {"content-type": "   ; charset=utf-8"}):
            reset_turn_web_budget()
            with (
                patch("httpx.AsyncClient.stream", return_value=_NoCTResponse(hdrs)),
                patch(
                    "services.web_tools.validate_url_security",
                    return_value=(True, "https://example.com/no-ct", None, "93.184.216.34"),
                ),
            ):
                res = await fetch_web_page("https://example.com/no-ct")
            data = json.loads(res.replace("<untrusted_file_content>\n", "").replace("\n</untrusted_file_content>", ""))
            assert "Unsupported or dangerous Content-Type" in data["error"]
    finally:
        reset_turn_web_budget()


@pytest.mark.anyio
async def test_fetch_web_page_enforces_wall_clock_deadline_against_slowloris_stream():
    """Drip-streaming responses exceeding total fetch_timeout wall clock must abort with timeout."""

    class _SlowlorisResponse:
        def __init__(self):
            self.status_code = 200
            self.headers = {"content-type": "text/plain"}
            self.encoding = "utf-8"

        async def aiter_bytes(self, chunk_size=65536):
            for _ in range(10):
                await asyncio.sleep(0.06)
                yield b"drip"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    reset_turn_web_budget()
    try:
        with (
            patch("services.web_tools.get_web_fetch_timeout", return_value=0.12),
            patch("httpx.AsyncClient.stream", return_value=_SlowlorisResponse()),
            patch(
                "services.web_tools.validate_url_security",
                return_value=(True, "https://example.com/slow", None, "93.184.216.34"),
            ),
        ):
            res = await fetch_web_page("https://example.com/slow")
        data = json.loads(res.replace("<untrusted_file_content>\n", "").replace("\n</untrusted_file_content>", ""))
        assert "timed out" in data["error"].lower()
    finally:
        reset_turn_web_budget()


@pytest.mark.anyio
async def test_web_search_concurrent_ddgs_engine_configuration_is_threadsafe():
    """Concurrent web_search calls safely synchronize DDGS engine setup via _DDGS_ENGINE_LOCK."""
    from ddgs.engines import ENGINES

    yahoo_cls = ENGINES["text"]["yahoo"]
    orig_xpath = yahoo_cls.items_xpath
    orig_elements = dict(yahoo_cls.elements_xpath)
    orig_patched = getattr(yahoo_cls, "_izvor_dual_xpath", False)

    class _FastDDGS:
        def __init__(self, timeout: int = 10):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def text(self, query: str, max_results: int = 5, backend: str = "auto"):
            return [{"title": "Hit", "href": "https://example.com/a", "body": "Body"}]

    reset_turn_web_budget()
    try:
        with (
            patch("ddgs.DDGS", _FastDDGS),
            patch(
                "services.web_tools.validate_url_security",
                return_value=(True, "https://example.com/a", None, "93.184.216.34"),
            ),
        ):
            from services.web_tools import _web_search_inner

            def _run_sync():
                return asyncio.run(_web_search_inner("concurrent test", max_results=2))

            with ThreadPoolExecutor(max_workers=4) as pool:
                outs = list(pool.map(lambda _: _run_sync(), range(4)))
            assert all(json.loads(o).get("results_count") == 1 for o in outs)
    finally:
        yahoo_cls.items_xpath = orig_xpath
        yahoo_cls.elements_xpath = orig_elements
        yahoo_cls._izvor_dual_xpath = orig_patched
        reset_turn_web_budget()


def test_local_tools_unicode_filenames_control_chars_truncation_and_atomic_write(tmp_path: Path):
    """local_tools allows valid Unicode filenames, rejects control chars, truncates reads, and writes safely."""
    sandbox = tmp_path / "agents_resources"
    outside_workspace = tmp_path / "other_workspace"
    sandbox.mkdir(parents=True, exist_ok=True)
    outside_workspace.mkdir(parents=True, exist_ok=True)

    local_tools.configure_paths(workspace_dir=outside_workspace, agents_resources_dir=sandbox)
    try:
        # Unicode valid filenames pass; control chars fail
        local_tools.validate_resource_filename("izvještaj_čćžšđ_2026.md")
        with pytest.raises(ValueError, match="Control characters"):
            local_tools.validate_resource_filename("bad\x07name.txt")

        # Write works cleanly via atomic temp file + os.replace with zero leftover .tmp files
        res_write = json.loads(local_tools.write_document("izvještaj_čćžšđ_2026.md", "A" * 1200, principal=SYSTEM_PRINCIPAL))
        assert res_write["success"] is True
        assert list(sandbox.glob("*.tmp")) == []

        # read_document truncates large files at bounded limit and reports truncated=True
        res_read = json.loads(
            local_tools.read_document("izvještaj_čćžšđ_2026.md", principal=SYSTEM_PRINCIPAL, max_chars=500)
        )
        assert res_read["truncated"] is True
        assert "[TRUNCATED: Document exceeded 500 characters]" in res_read["content"]
        assert len(res_read["content"]) < 700

        # Blocked write outside workspace_dir does not crash on relative_to
        res_blocked = json.loads(local_tools.write_document(".env", "SECRET=1", principal=SYSTEM_PRINCIPAL))
        assert "protected path" in res_blocked["error"]
    finally:
        local_tools.reset_paths()


def test_local_tools_list_documents_skips_symlink_escapes(tmp_path: Path):
    """list_documents resolves targets and excludes symlinks/junctions pointing outside agents_resources."""
    sandbox = tmp_path / "agents_resources"
    sandbox.mkdir(parents=True, exist_ok=True)
    outside_dir = tmp_path / "secret_outside_dir"
    outside_dir.mkdir(parents=True, exist_ok=True)
    (outside_dir / "secret.txt").write_text("TOP SECRET", encoding="utf-8")
    (sandbox / "safe.txt").write_text("SAFE", encoding="utf-8")

    escape_link = sandbox / "escape_dir"
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(outside_dir), str(escape_link))
    else:
        os.symlink(outside_dir, escape_link, target_is_directory=True)

    local_tools.configure_paths(workspace_dir=tmp_path, agents_resources_dir=sandbox)
    try:
        listed = json.loads(local_tools.list_documents(".", principal=SYSTEM_PRINCIPAL))
        names = [entry["name"] for entry in listed["contents"]]
        assert "safe.txt" in names
        assert "escape_dir" not in names
    finally:
        local_tools.reset_paths()


def test_logging_filters_basic_auth_percent_args_and_third_party_loggers():
    """CredentialRedactionFilter scrubs Basic auth, formats %s args before scrubbing, and attaches to uvicorn/httpx."""
    assert "Basic [REDACTED_TOKEN]" in redact_text("Authorization: Basic dXNlcjpzZWNyZXRwYXNz")

    flt = CredentialRedactionFilter()
    rec = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Upstream call %s header Authorization: Basic %s",
        args=("https://api.example.com", "dXNlcjpzZWNyZXRwYXNz"),
        exc_info=None,
    )
    assert flt.filter(rec) is True
    assert rec.args == ()
    assert "Basic [REDACTED_TOKEN]" in rec.msg
    assert "dXNlcjpzZWNyZXRwYXNz" not in rec.msg
    assert "https://api.example.com" in rec.msg

    setup_credential_redaction_filters()
    for target_name in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx"):
        target_logger = logging.getLogger(target_name)
        assert any(isinstance(f, CredentialRedactionFilter) for f in target_logger.filters)


def test_uvicorn_access_record_preserves_5_tuple_and_redacts_query_secrets():
    """CredentialRedactionFilter preserves uvicorn.access 5-tuple args while redacting query secrets."""
    from uvicorn.logging import AccessFormatter

    flt = CredentialRedactionFilter()
    rec = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=(
            "127.0.0.1:12345",
            "POST",
            "/slack/events?token=xoxb-123456789012-123456789012-abcdefghijklmnopqrstuvwx&key=sk-proj-1234567890123456789012345678",
            "1.1",
            200,
        ),
        exc_info=None,
    )
    assert flt.filter(rec) is True
    assert isinstance(rec.args, tuple)
    assert len(rec.args) == 5
    assert isinstance(rec.args[4], int) and rec.args[4] == 200
    assert "xoxb-" not in rec.args[2]
    assert "sk-proj-" not in rec.args[2]
    assert "[REDACTED_SLACK_TOKEN]" in rec.args[2]
    assert "[REDACTED_API_KEY]" in rec.args[2]

    formatted = AccessFormatter(
        fmt='%(client_addr)s - "%(request_line)s" %(status_code)s',
        use_colors=False,
    ).format(rec)
    assert '127.0.0.1:12345 - "POST /slack/events?token=[REDACTED_SLACK_TOKEN]&key=[REDACTED_API_KEY] HTTP/1.1" 200 OK' == formatted
    assert "xoxb-" not in formatted
    assert "sk-proj-" not in formatted


