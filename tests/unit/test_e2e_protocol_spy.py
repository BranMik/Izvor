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
async def test_finish_live_scenario_updates_root_first_row_and_posts_verdict_to_all_opened_threads():
    """Root messages must be updated with first-row PASS/FAIL and receive verdict replies on every opened thread."""
    ts_seq = iter(["1700.100", "1700.200", "1700.300"])
    posted_threads: list[str] = []
    updated_roots: list[tuple[str, str]] = []

    async def _fake_post(**kwargs):
        if kwargs.get("thread_ts"):
            posted_threads.append(str(kwargs["thread_ts"]))
            return MockSlackResponse({"ok": True, "ts": f"{kwargs['thread_ts']}_reply"})
        return MockSlackResponse({"ok": True, "ts": next(ts_seq)})

    async def _fake_update(**kwargs):
        updated_roots.append((str(kwargs["ts"]), str(kwargs["text"])))
        return MockSlackResponse({"ok": True, "ts": kwargs["ts"]})

    raw_double = AsyncMock()
    raw_double.chat_postMessage = AsyncMock(side_effect=_fake_post)
    raw_double.chat_update = AsyncMock(side_effect=_fake_update)
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
    assert [ts for ts, _ in updated_roots] == ["1700.100", "1700.200", "1700.300"]
    for (_, updated_text), part in zip(updated_roots, ("Part A", "Part B", "Part C")):
        lines = updated_text.splitlines()
        assert len(lines) == 2
        assert lines[0].startswith("🧪 *RUN-[TC-26]: Multi Thread*")
        assert "PASS" in lines[0]
        assert "(1.25s)" in lines[0]
        assert part in lines[1]
    await client.aclose()


@pytest.mark.anyio
async def test_finish_live_scenario_marks_fail_on_root_first_row():
    """Failing scenarios must update the initial root message's first row with FAIL."""
    updated_roots: list[str] = []

    async def _fake_post(**kwargs):
        if kwargs.get("thread_ts"):
            return MockSlackResponse({"ok": True, "ts": "1800.100_reply"})
        return MockSlackResponse({"ok": True, "ts": "1800.100"})

    async def _fake_update(**kwargs):
        updated_roots.append(str(kwargs["text"]))
        return MockSlackResponse({"ok": True, "ts": kwargs["ts"]})

    raw_double = AsyncMock()
    raw_double.chat_postMessage = AsyncMock(side_effect=_fake_post)
    raw_double.chat_update = AsyncMock(side_effect=_fake_update)
    client = SlackProtocolClient(
        live_mode=True,
        live_channel_id="C_LIVE",
        raw_slack_client=raw_double,
        bot_token="xoxb-fake-live-test-token",
    )

    client.set_active_scenario("RUN-[TC-09]", "Agent Exclusion")
    await client.start_live_scenario("/exclude /[Innovator] test")

    class _FailRes:
        passed = False
        duration_seconds = 2.50
        details = "Innovator spoke unexpectedly"

    await client.finish_live_scenario(_FailRes())
    assert len(updated_roots) == 1
    first_row, body_row = updated_roots[0].splitlines()
    assert first_row == "🧪 *RUN-[TC-09]: Agent Exclusion* — ❌ *FAIL* (2.50s)"
    assert "/exclude /[Innovator] test" in body_row
    await client.aclose()


@pytest.mark.anyio
async def test_post_live_run_summary_creates_separate_thread_and_records_bus():
    """Final run summary must post a separate root message + chunked thread replies with tested capabilities."""
    posted_calls: list[dict] = []
    call_counter = 0

    async def _fake_post(**kwargs):
        nonlocal call_counter
        call_counter += 1
        posted_calls.append(dict(kwargs))
        return MockSlackResponse({"ok": True, "ts": f"1900.{call_counter:03d}"})

    raw_double = AsyncMock()
    raw_double.chat_postMessage = AsyncMock(side_effect=_fake_post)
    client = SlackProtocolClient(
        live_mode=True,
        live_channel_id="C_LIVE",
        raw_slack_client=raw_double,
        bot_token="xoxb-fake-live-test-token",
    )

    from tests.e2e.scenarios import ALL_SCENARIOS, ScenarioResult

    results = [
        ScenarioResult(
            id=sc.id,
            name=sc.name,
            passed=(idx != 1),
            details="Mismatch" if idx == 1 else "ok",
            duration_seconds=0.5,
            capability=sc.capability,
        )
        for idx, sc in enumerate(ALL_SCENARIOS)
    ]

    summary_ts = await client.post_live_run_summary(
        run_prefix="TEST_RUN_99",
        mode_desc="LIVE SLACK",
        llm_mode="LIVE ALL LLM",
        results=results,
        total_duration=19.0,
    )

    assert summary_ts == "1900.001"
    assert len(posted_calls) >= 3  # 1 root summary + multiple chunked thread replies for 38 scenarios
    assert "thread_ts" not in posted_calls[0]
    assert "TEST_RUN_99: E2E Test Suite Summary" in posted_calls[0]["text"]
    assert "37/38 PASSED" in posted_calls[0]["text"]
    assert "1 FAILED" in posted_calls[0]["text"]

    combined_thread_text = "\n".join(c["text"] for c in posted_calls[1:])
    assert all(c.get("thread_ts") == "1900.001" for c in posted_calls[1:])
    for sc in ALL_SCENARIOS:
        assert sc.capability, f"{sc.id} missing capability description"
        assert sc.id in combined_thread_text
        assert sc.capability in combined_thread_text

    assert len(client.bus.run_summaries) == 1
    assert client.bus.run_summaries[0]["thread_ts"] == "1900.001"
    await client.aclose()


@pytest.mark.anyio
async def test_post_live_run_summary_offline_records_on_bus_without_slack_calls():
    """In offline mode, post_live_run_summary records summary and capabilities on bus without calling Slack."""
    raw_double = AsyncMock()
    client = SlackProtocolClient(live_mode=False, raw_slack_client=raw_double)

    from tests.e2e.scenarios import ScenarioResult

    summary_ts = await client.post_live_run_summary(
        run_prefix="TEST_RUN_OFFLINE",
        mode_desc="OFFLINE SIMULATION",
        llm_mode="MOCK ONLY",
        results=[
            ScenarioResult(
                id="TC-01",
                name="Direct Single Persona Invocation",
                passed=True,
                details="ok",
                duration_seconds=0.1,
                capability="Direct /[Persona] routing",
            )
        ],
        total_duration=0.1,
    )
    assert summary_ts is None
    raw_double.chat_postMessage.assert_not_called()
    assert len(client.bus.run_summaries) == 1
    assert "Direct /[Persona] routing" in client.bus.run_summaries[0]["thread_replies"][0]
    await client.aclose()
