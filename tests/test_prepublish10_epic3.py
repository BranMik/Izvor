import asyncio
import json
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.domain import SYSTEM_PRINCIPAL, ToolPrincipal
from core.slack_security import sanitize_slack_broadcast_mentions
from services.audit_logger import (
    AuditLogger,
    _dispatch_critical_alert,
    set_main_event_loop,
    validate_audit_configuration,
)
from services.mcp_tools import (
    _mask_tool_args,
    list_documents,
    read_document,
    write_document,
)
from services.slack.messages import post_message_to_slack


def test_slack_broadcast_mentions_neutralized():
    """TKT-1009 / S-06: Verifies labelled and subteam broadcast mentions are neutralized."""
    raw_texts = [
        ("Hello <!here|here>, please review.", "Hello @here, please review."),
        ("Attention <!channel|all-engineers>!", "Attention @channel!"),
        ("Ping <!everyone>", "Ping @everyone"),
        ("Calling <!subteam^S12345|@backend-team>", "Calling @subteam^S12345"),
        ("Team <!subteam^S99999>", "Team @subteam^S99999"),
        ("Normal text @john and #general", "Normal text @john and #general"),
    ]
    for raw, expected in raw_texts:
        assert sanitize_slack_broadcast_mentions(raw) == expected


@pytest.mark.anyio
async def test_post_message_to_slack_dlp_and_broadcast_mentions(monkeypatch):
    """TKT-1009 / S-01: Verifies post_message_to_slack neutralizes broadcasts and blocks secret leaks."""
    monkeypatch.setattr("services.slack.messages.get_slack_bot_token", lambda: "xoxb-real-token")
    mock_post = AsyncMock(return_value=MagicMock(data={"ok": True, "ts": "123.456"}))
    mock_client = MagicMock(chat_postMessage=mock_post)
    monkeypatch.setattr("services.slack.messages.get_slack_async_client", lambda: mock_client)

    # 1. Mention neutralization in text and blocks
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "<!here|here> alert"}}]
    await post_message_to_slack(
        channel_id="C123",
        text="<!channel|team> attention",
        blocks=blocks,
    )
    call_kwargs = mock_post.call_args.kwargs
    assert call_kwargs["text"] == "@channel attention"
    assert call_kwargs["blocks"][0]["text"]["text"] == "@here alert"

    # 2. Secret leak blocking
    mock_post.reset_mock()
    secret_text = "Here is the key: xoxb-1234567890-1234567890123-abcdefghijklmnopqrstuvwx"
    await post_message_to_slack(
        channel_id="C123",
        text=secret_text,
    )
    call_kwargs2 = mock_post.call_args.kwargs
    assert "Message blocked by security policy" in call_kwargs2["text"]


def test_audit_logger_production_mode_requires_hmac_secret(monkeypatch):
    """TKT-1010 / S-04: Verifies startup fails closed if AUDIT_HMAC_SECRET is missing in production."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setattr("services.audit_logger.get_audit_hmac_secret", lambda: b"")
    monkeypatch.setattr("core.config.is_test_environment", lambda: False)

    with pytest.raises(RuntimeError, match="AUDIT_HMAC_SECRET is required in production"):
        validate_audit_configuration()


@pytest.mark.anyio
async def test_audit_logger_cross_thread_alerting(monkeypatch):
    """TKT-1010 / S-05: Verifies critical alerts can be dispatched from worker threads via run_coroutine_threadsafe."""
    main_loop = asyncio.get_running_loop()
    set_main_event_loop(main_loop)

    alert_dispatched = asyncio.Event()

    async def mock_send_alert(**kwargs):
        alert_dispatched.set()
        return {"ok": True}

    monkeypatch.setattr("services.audit_logger.send_admin_audit_alert", mock_send_alert)

    # Invoke from a worker thread where get_running_loop() normally raises RuntimeError
    def worker_thread_action():
        _dispatch_critical_alert({
            "event_type": "SECURITY_POLICY_VIOLATION",
            "action": "execute_tool",
            "target": "write_document",
            "status": "BLOCKED",
            "details": {"test": "cross_thread"},
        })

    await asyncio.to_thread(worker_thread_action)
    await asyncio.wait_for(alert_dispatched.wait(), timeout=2.0)
    assert alert_dispatched.is_set()


def test_audit_logger_corrupted_latest_file_raises(tmp_path):
    """TKT-1010 / C-13: Verifies get_last_state raises on corrupted newest audit file instead of forking."""
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)

    # Old partition: valid
    old_file = audit_dir / "audit_2026_09.jsonl"
    old_record = {
        "seq": 10,
        "entry_hash": "a" * 64,
        "prev_hash": "0" * 64,
        "event_type": "TEST",
    }
    old_file.write_text(json.dumps(old_record) + "\n", encoding="utf-8")

    # Newest partition: corrupted last line
    new_file = audit_dir / "audit_2026_10.jsonl"
    new_file.write_text("{\"seq\": 11, \"entry_hash\": corrupted json ...\n", encoding="utf-8")

    logger = AuditLogger(audit_dir=audit_dir)
    logger._cached_last_state = None

    with pytest.raises(RuntimeError, match="Audit log corruption detected in latest file"):
        logger.get_last_state()


def test_mcp_tools_document_access_control(tmp_path, monkeypatch):
    """TKT-1011 / S-03: Verifies read_document and list_documents enforce principal authorization and ACLs."""
    test_res_dir = tmp_path / "agents_resources"
    test_res_dir.mkdir(parents=True)
    (test_res_dir / "public.txt").write_text("public document content", encoding="utf-8")
    (test_res_dir / "confidential.txt").write_text("top secret plan", encoding="utf-8")

    monkeypatch.setattr("services.mcp_tools.AGENTS_RESOURCES_DIR", test_res_dir)
    monkeypatch.setattr("services.mcp_tools.WORKSPACE_DIR", tmp_path)

    # 1. Principal not authorized for tool
    unauth_principal = ToolPrincipal(kind="agent", id="agent-unauthorized")
    res_unauth = json.loads(read_document("public.txt", principal=unauth_principal))
    assert "Permission denied" in res_unauth["error"]

    res_list_unauth = json.loads(list_documents(".", principal=unauth_principal))
    assert "Permission denied" in res_list_unauth["error"]

    # 2. System principal is always authorized
    res_sys = json.loads(read_document("public.txt", principal=SYSTEM_PRINCIPAL))
    assert res_sys.get("content") == "public document content"

    # 3. FileContext ACL enforcement: restricted document
    reader_principal = ToolPrincipal(kind="agent", id="agent-10008")
    monkeypatch.setattr(
        "services.mcp_tools.is_agent_authorized_for_tool",
        lambda principal, tool_name, **kw: True,
    )

    # Agent is authorized by ACL
    res_acl_ok = json.loads(
        read_document(
            "confidential.txt",
            principal=reader_principal,
            authorized_agents={"agent-10008", "agent-10011"},
        )
    )
    assert res_acl_ok.get("content") == "top secret plan"

    # Agent is NOT in authorized_agents ACL
    unauth_reader = ToolPrincipal(kind="agent", id="agent-10002")
    res_acl_denied = json.loads(
        read_document(
            "confidential.txt",
            principal=unauth_reader,
            authorized_agents={"agent-10008", "agent-10011"},
        )
    )
    assert "Permission denied: Agent 'agent-10002' is not authorized" in res_acl_denied["error"]


def test_mcp_tools_mask_sensitive_arguments():
    """TKT-1012 / S-09: Verifies sensitive keywords (tokens, passwords, keys) are masked in logs."""
    raw = {
        "api_key": "sk-proj-secret-12345",
        "auth_token": "bearer-token-xyz",
        "normal_field": "hello world",
        "nested": {"db_password": "supersecretpassword", "counter": 42},
    }
    masked = _mask_tool_args(raw)
    assert masked["api_key"] == "***MASKED***"
    assert masked["auth_token"] == "***MASKED***"
    assert masked["normal_field"] == "hello world"
    assert masked["nested"]["db_password"] == "***MASKED***"
    assert masked["nested"]["counter"] == 42


def test_basic_auth_throttling_on_failures():
    """TKT-1012 / S-13: Verifies sliding window rate limiting throttles repeated basic auth failures."""
    from fastapi import HTTPException
    from fastapi.security import HTTPBasicCredentials
    from starlette.requests import Request
    from services.rate_limiter import ip_rate_limiter
    from main import verify_admin_basic_auth

    ip_rate_limiter.reset()
    bad_creds = HTTPBasicCredentials(username="malicious_actor", password="wrong_password")
    mock_request = Request(scope={"type": "http", "client": ("198.51.100.1", 12345), "headers": []})

    # 5 attempts fail with 401
    for _ in range(5):
        with pytest.raises(HTTPException) as exc_info:
            verify_admin_basic_auth(mock_request, bad_creds)
        assert exc_info.value.status_code == 401

    # 6th attempt exceeds burst limit, fails with 429 Too Many Requests
    with pytest.raises(HTTPException) as exc_info:
        verify_admin_basic_auth(mock_request, bad_creds)
    assert exc_info.value.status_code == 429
    assert "Too many failed authentication attempts" in exc_info.value.detail
