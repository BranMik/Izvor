"""Unit tests for TKT-1235: Slack events dedup/subtypes, interactivity audits, client_core rotation/rate-limit, and views hardening."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import Response
from fastapi.testclient import TestClient
import pytest

from core.config import set_admin_slack_user_ids_override
from core.slack_security import set_secret_scanner_hook
from main import app
from services.slack import (
    CHANNEL_NAME_CACHE,
    configure_slack_async_client,
    configure_token,
    get_channel_name,
    get_slack_async_client,
    open_dm_channel,
    publish_app_home,
    reset_app_home_publish_cache,
    reset_slack_async_client,
    reset_token,
)


def test_event_dedup_cache_db_error_uses_bounded_memory_fallback():
    """claim_event falls back to bounded in-memory LRU/TTL dedup when SQLite errors."""
    from adapters.slack.events import EventDeduplicationCache

    cache = EventDeduplicationCache(ttl_seconds=60.0, maxsize=2)
    with (
        patch("adapters.slack.events.get_db_transaction", side_effect=RuntimeError("db down")),
        patch("adapters.slack.events.get_connection", side_effect=RuntimeError("db down")),
    ):
        assert cache.claim_event(("C1", "100.1")) is True
        assert cache.claim_event(("C1", "100.1")) is False
        assert ("C1", "100.1") in cache

        # Fill capacity past maxsize=2 to verify LRU eviction bounds memory
        assert cache.claim_event(("C1", "100.2")) is True
        assert cache.claim_event(("C1", "100.3")) is True
        assert ("C1", "100.1") not in cache
        assert cache.claim_event(("C1", "100.1")) is True

        cache.clear()
        assert ("C1", "100.1") not in cache


def test_handle_slack_events_file_only_dm_and_subtype_filtering():
    """File-only DM uploads are accepted while non-user subtypes like channel_topic/message_changed are ignored."""
    from adapters.slack.events import PROCESSED_EVENTS

    PROCESSED_EVENTS.clear()
    client = TestClient(app)

    def _close_coro(channel_id: str, thread_ts: str, coro):
        coro.close()

    with (
        patch("adapters.slack.events.verify_slack_signature", return_value=True),
        patch("adapters.slack.events.thread_state_manager.dispatch_discussion_task", side_effect=_close_coro) as mock_dispatch,
    ):
        # 1. Disallowed subtypes must be dropped
        for bad_subtype in ("channel_topic", "message_changed"):
            resp_bad = client.post(
                "/slack/events",
                json={
                    "type": "event_callback",
                    "event": {
                        "type": "message",
                        "subtype": bad_subtype,
                        "channel": "D_DM_SUB",
                        "user": "U_USER",
                        "ts": f"1700000001.{bad_subtype}",
                        "text": "<@U_BOT> updated topic",
                    },
                },
            )
            assert resp_bad.status_code == 200
        assert mock_dispatch.call_count == 0

        # 2. File-only DM upload with empty text must be accepted once, then deduplicated on retry
        file_payload = {
            "type": "event_callback",
            "event": {
                "type": "message",
                "subtype": "file_share",
                "channel": "D_DM_FILE",
                "user": "U_USER",
                "ts": "1700000200.001",
                "text": "",
                "files": [{"id": "F123", "name": "architecture.pdf"}],
            },
        }
        resp_ok = client.post("/slack/events", json=file_payload)
        assert resp_ok.status_code == 200
        assert mock_dispatch.call_count == 1

        resp_retry = client.post("/slack/events", json=file_payload)
        assert resp_retry.status_code == 200
        assert mock_dispatch.call_count == 1

    PROCESSED_EVENTS.clear()


def test_interactivity_single_async_audit_on_success_and_failure_status_on_error():
    """Dispatcher emits one accurate async audit event (no duplicates on self-audited actions, FAILURE on errors)."""
    client = TestClient(app)
    set_admin_slack_user_ids_override("U_ADM_TEST")
    try:
        with (
            patch("adapters.slack.interactivity.verify_slack_signature", return_value=True),
            patch("services.slack.views.publish_app_home", AsyncMock(return_value={"ok": True})),
        ):
            # 1. Non-self-audited action emits exactly ONE SUCCESS async audit from dispatcher
            with (
                patch("adapters.slack.interactivity.log_audit_event_async", new_callable=AsyncMock) as mock_audit,
                patch("services.slack_home.build_edit_workspace_context_modal", return_value={"type": "modal"}),
                patch("services.slack.slack_async_client.views_open", new_callable=AsyncMock),
            ):
                resp1 = client.post(
                    "/slack/actions",
                    json={
                        "type": "block_actions",
                        "user": {"id": "U_ADM_TEST"},
                        "trigger_id": "trig_modal_1",
                        "actions": [{"action_id": "open_edit_workspace_context_modal"}],
                    },
                )
            assert resp1.status_code == 200
            assert mock_audit.await_count == 1
            assert mock_audit.call_args.kwargs["status"] == "SUCCESS"

            # 2. Self-audited action does NOT get duplicated by dispatcher
            with (
                patch("adapters.slack.interactivity.log_audit_event_async", new_callable=AsyncMock) as mock_disp_audit,
                patch("adapters.slack.actions.agent_actions.log_audit_event_async", new_callable=AsyncMock) as mock_hdl_audit,
            ):
                resp2 = client.post(
                    "/slack/actions",
                    json={
                        "type": "block_actions",
                        "user": {"id": "U_ADM_TEST"},
                        "actions": [
                            {
                                "action_id": "toggle_agent_radio:agent-10011",
                                "selected_option": {"value": "agent-10011:off"},
                            }
                        ],
                    },
                )
            assert resp2.status_code == 200
            assert mock_hdl_audit.await_count == 1
            assert mock_disp_audit.await_count == 0

            # 3. Failing handler (non-200 Response or exception) logs FAILURE, never SUCCESS
            from adapters.slack.interactivity import ACTION_REGISTRY, ActionSpec

            async def _fail_handler(*args, **kwargs):
                return Response(status_code=500)

            orig_spec = ACTION_REGISTRY["open_edit_workspace_context_modal"]
            ACTION_REGISTRY["open_edit_workspace_context_modal"] = ActionSpec(
                handler=_fail_handler,
                admin_required=True,
                audit_action="ADMIN_CONFIG_CHANGE",
                publish_home=False,
                self_audited=False,
            )
            try:
                with patch("adapters.slack.interactivity.log_audit_event_async", new_callable=AsyncMock) as mock_fail_audit:
                    resp_fail = client.post(
                        "/slack/actions",
                        json={
                            "type": "block_actions",
                            "user": {"id": "U_ADM_TEST"},
                            "actions": [{"action_id": "open_edit_workspace_context_modal"}],
                        },
                    )
                assert resp_fail.status_code == 500
                assert mock_fail_audit.await_count == 1
                assert mock_fail_audit.call_args.kwargs["status"] == "FAILURE"
            finally:
                ACTION_REGISTRY["open_edit_workspace_context_modal"] = orig_spec
    finally:
        set_admin_slack_user_ids_override(None)


@pytest.mark.anyio
async def test_slack_client_core_token_rotation_and_rate_limiter():
    """get_slack_async_client rebuilds when token changes; metadata calls acquire slack_rate_limiter."""
    reset_slack_async_client()
    configure_token("xoxb-token-alpha-111")
    try:
        client_a = get_slack_async_client()
        assert client_a.token == "xoxb-token-alpha-111"

        configure_token("xoxb-token-beta-222")
        client_b = get_slack_async_client()
        assert client_b is not client_a
        assert client_b.token == "xoxb-token-beta-222"

        CHANNEL_NAME_CACHE.pop("C_RATELIMIT", None)
        mock_client = MagicMock()
        mock_client.conversations_info = AsyncMock(return_value={"channel": {"name": "eng-alerts"}})
        mock_client.conversations_open = AsyncMock(return_value={"channel": {"id": "D_OPENED_1"}})
        configure_slack_async_client(mock_client)

        with patch("services.slack.client_core.slack_rate_limiter.acquire", new_callable=AsyncMock) as mock_acquire:
            ch_name = await get_channel_name("C_RATELIMIT")
            dm_id = await open_dm_channel("U_USER_99")

        assert ch_name == "#eng-alerts"
        assert dm_id == "D_OPENED_1"
        assert mock_acquire.await_count == 2
    finally:
        CHANNEL_NAME_CACHE.pop("C_RATELIMIT", None)
        reset_slack_async_client()
        reset_token()


@pytest.mark.anyio
async def test_publish_app_home_sanitizes_blocks_and_deduplicates_hashes():
    """publish_app_home neutralizes broadcast mentions, blocks leaked secrets, and deduplicates identical view hashes."""
    reset_app_home_publish_cache()
    configure_token("xoxb-live-app-home-token")
    mock_client = MagicMock()
    mock_client.views_publish = AsyncMock(return_value=MagicMock(data={"ok": True}))
    configure_slack_async_client(mock_client)

    try:
        base_view = {
            "type": "home",
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "Hello <!channel> team"}}],
        }
        with (
            patch("services.slack_home.build_app_home_view", return_value=base_view),
            patch("services.slack.views.slack_rate_limiter.acquire", new_callable=AsyncMock),
        ):
            res1 = await publish_app_home("U_HOME_1")
            res2 = await publish_app_home("U_HOME_1")

        assert res1 == {"ok": True}
        assert res2 == {"ok": True, "deduplicated": True}
        assert mock_client.views_publish.await_count == 1
        published_text = mock_client.views_publish.call_args.kwargs["view"]["blocks"][0]["text"]["text"]
        assert "<!channel>" not in published_text
        assert "@channel" in published_text

        # Secret match must block original blocks and trigger a fresh publish (hash changed)
        set_secret_scanner_hook(lambda text, user_id="system": "TEST_SECRET_RULE" if "SECRET_XYZ" in text else None)
        secret_view = {
            "type": "home",
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "Leak SECRET_XYZ"}}],
        }
        with (
            patch("services.slack_home.build_app_home_view", return_value=secret_view),
            patch("services.slack.views.slack_rate_limiter.acquire", new_callable=AsyncMock),
        ):
            res3 = await publish_app_home("U_HOME_1")
        assert res3 == {"ok": True}
        assert mock_client.views_publish.await_count == 2
        blocked_dump = json.dumps(mock_client.views_publish.call_args.kwargs["view"])
        assert "SECRET_XYZ" not in blocked_dump
        assert "TEST_SECRET_RULE" in blocked_dump
    finally:
        set_secret_scanner_hook(None)
        reset_app_home_publish_cache()
        reset_slack_async_client()
        reset_token()

