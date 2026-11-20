"""Unit tests for dual-mode MockSlackAsyncClient spy and multi-thread E2E verdict cards."""

from unittest.mock import AsyncMock

import pytest

from tests.e2e.protocol_client import MockSlackResponse, SlackProtocolClient


@pytest.mark.anyio
async def test_dual_mode_spy_chat_update_and_no_double_post():
    """In live_mode=True, chat_update updates posted_messages and mock_post_agent_response does not double-record."""
    raw_double = AsyncMock()
    raw_double.chat_postMessage = AsyncMock(return_value=MockSlackResponse({"ok": True, "ts": "1700.001"}))
    raw_double.chat_update = AsyncMock(return_value=MockSlackResponse({"ok": True, "ts": "1700.001"}))
    raw_double.conversations_replies = AsyncMock(
        return_value=MockSlackResponse({"ok": True, "messages": [], "has_more": False})
    )

    client = SlackProtocolClient(
        live_mode=True,
        live_channel_id="C_LIVE",
        raw_slack_client=raw_double,
        bot_token="xoxb-fake-live-test-token",
    )

    import services.slack as _sc

    _sc.configure_post_agent_response(client.mock_post_agent_response)
    _sc.configure_slack_async_client(client.slack_client_double)
    _sc.configure_token("xoxb-fake-live-test-token")
    try:
        posted = await _sc.post_agent_response(
            channel_id="C_LIVE",
            thread_ts="1700.000",
            text="_⏳ *SecurityArchitect* is executing action._",
            username="SecurityArchitect",
        )
        assert posted["ts"] == "1700.001"
        assert len(client.bus.posted_messages) == 1

        await _sc.update_slack_message(
            channel_id="C_LIVE",
            ts="1700.001",
            text="Target throughput is 5000 rps.\n\nSkills used: [document_reader]",
        )
        assert len(client.bus.updated_messages) == 1
        assert "5000 rps" in client.bus.posted_messages[0]["text"]

        replies = await client.slack_client_double.conversations_replies(channel="C_LIVE", ts="1700.000")
        assert replies["ok"] is True
        raw_double.conversations_replies.assert_awaited_once()
    finally:
        _sc.reset_token()
        _sc.reset_post_agent_response()
        _sc.reset_slack_async_client()
        await client.aclose()


@pytest.mark.anyio
async def test_synthetic_user_ids_bypass_live_slack_dm_api():
    """Synthetic E2E user IDs simulate DM/upload/ephemeral on the bus instead of calling Slack with fake IDs."""
    raw_double = AsyncMock()
    client = SlackProtocolClient(
        live_mode=True,
        live_channel_id="C_LIVE",
        raw_slack_client=raw_double,
        bot_token="xoxb-fake-live-test-token",
    )

    dm = await client.slack_client_double.conversations_open(users="U_TESTER_22")
    assert dm["ok"] is True
    assert dm["channel"]["id"].startswith("D_TEST_DM")
    raw_double.conversations_open.assert_not_called()

    up = await client.slack_client_double.files_upload_v2(
        channel=dm["channel"]["id"], content="notes", filename="scribe_notes.txt"
    )
    assert up["ok"] is True
    assert len(client.bus.uploaded_files) == 1
    raw_double.files_upload_v2.assert_not_called()

    eph = await client.slack_client_double.chat_postEphemeral(
        channel="C_LIVE", user="U_TESTER_22", text="sent to direct message"
    )
    assert eph["ok"] is True
    assert len(client.bus.ephemeral_messages) == 1
    raw_double.chat_postEphemeral.assert_not_called()
    await client.aclose()


@pytest.mark.anyio
async def test_finish_live_scenario_posts_verdict_to_all_opened_threads():
    """Multi-thread scenarios resetting current_thread_ts must receive evaluation cards on every opened thread."""
    ts_seq = iter(["1700.100", "1700.200", "1700.300"])
    posted_threads: list[str] = []

    async def _fake_post(**kwargs):
        if kwargs.get("thread_ts"):
            posted_threads.append(str(kwargs["thread_ts"]))
            return MockSlackResponse({"ok": True, "ts": f"{kwargs['thread_ts']}_reply"})
        return MockSlackResponse({"ok": True, "ts": next(ts_seq)})

    raw_double = AsyncMock()
    raw_double.chat_postMessage = AsyncMock(side_effect=_fake_post)
    client = SlackProtocolClient(
        live_mode=True,
        live_channel_id="C_LIVE",
        raw_slack_client=raw_double,
        bot_token="xoxb-fake-live-test-token",
    )

    client.set_active_scenario("RUN-[TC-26]", "Multi Thread")
    await client.start_live_scenario("Part A")
    client.current_thread_ts = None
    await client.start_live_scenario("Part B")
    client.current_thread_ts = None
    await client.start_live_scenario("Part C")

    class _DummyRes:
        passed = True
        duration_seconds = 1.25
        details = "All parts ok"

    await client.finish_live_scenario(_DummyRes())
    assert posted_threads == ["1700.100", "1700.200", "1700.300"]
    await client.aclose()
