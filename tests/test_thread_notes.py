import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents import thread_notes_agent
from agents.thread_notes_agent import (
    THREAD_NOTE_MSG_MAP,
    generate_thread_notes,
    get_thread_note,
    set_thread_note,
    trigger_thread_notes_update,
    update_notes_for_thread,
)
from core.config import THREAD_NOTES_MODEL
from services.slack_client import post_thread_note, update_thread_note


@pytest.fixture(autouse=True)
def clean_notes_state(monkeypatch):
    from orchestration import agent_router
    monkeypatch.setattr(agent_router.asyncio, "sleep", AsyncMock(return_value=None))
    THREAD_NOTE_MSG_MAP.clear()
    thread_notes_agent.THREAD_LOCKS.clear()
    thread_notes_agent._RUNNING_TASKS.clear()
    thread_notes_agent._PENDING_UPDATES.clear()
    yield
    THREAD_NOTE_MSG_MAP.clear()
    thread_notes_agent.THREAD_LOCKS.clear()
    thread_notes_agent._RUNNING_TASKS.clear()
    thread_notes_agent._PENDING_UPDATES.clear()


@pytest.mark.anyio
async def test_generate_thread_notes_calls_mistral_model():
    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(
            message=MagicMock(
                content=(
                    "📋 *Strategic Synthesis & Action Plan* _(Updating live...)_\n"
                    "🎯 *Core Objective & Problem Statement*\nBuild scalable database architecture.\n"
                    "💡 *Key Perspectives & Insights*\n• PostgreSQL with connection pooling.\n"
                    "🤝 *Consensus & Strategic Decisions*\n• Agreed on modular microservices.\n"
                    "⚠️ *Risks, Trade-offs & Open Questions*\n• Data replication latency.\n"
                    "🚀 *Action Items & Next Steps*\n• Set up staging test cluster."
                )
            )
        )
    ]
    mock_resp.usage = MagicMock(prompt_tokens=150, completion_tokens=80, total_tokens=230)

    history = [
        {"role": "user", "content": "How should we design the database?"},
        {"role": "assistant", "content": "[Architect]: Use PostgreSQL with connection pooling."},
    ]

    with patch("pipeline.discussion_scribe.scribe.litellm.completion", return_value=mock_resp) as mock_litellm:
        content, token_stats = await generate_thread_notes(history, is_completed=False)

        assert mock_litellm.called
        call_kwargs = mock_litellm.call_args.kwargs
        assert call_kwargs["model"] == THREAD_NOTES_MODEL
        assert call_kwargs["model"] == "mistral/mistral-small-latest"
        assert "Strategic Synthesis" in content
        assert "Core Objective" in content
        assert token_stats["prompt_tokens"] == 150
        assert token_stats["completion_tokens"] == 80
        assert token_stats["total_tokens"] == 230


@pytest.mark.anyio
async def test_update_notes_for_thread_creates_new_note_on_first_reply(caplog):
    import logging

    caplog.set_level(logging.INFO)

    history = [
        {"role": "user", "content": "Start project kickoff"},
        {"role": "assistant", "content": "[Visionary]: Here is the vision for our product."},
    ]

    with patch("agents.thread_notes_agent.fetch_thread_history", new_callable=AsyncMock) as mock_fetch, \
         patch("agents.thread_notes_agent.generate_thread_notes", new_callable=AsyncMock) as mock_gen, \
         patch("agents.thread_notes_agent.post_thread_note", new_callable=AsyncMock) as mock_post:

        mock_fetch.return_value = history
        mock_gen.return_value = (
            "📋 *Strateška sinteza i akcijski plan*\n🎯 *Glavni cilj*\nIzrada nove platforme.",
            {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        )
        mock_post.return_value = {"ok": True, "ts": "1700000000.999"}

        res = await update_notes_for_thread(channel_id="C111", thread_ts="1700000000.001")

        assert res["ok"] is True
        assert res["note_ts"] == "1700000000.999"
        assert get_thread_note("C111", "1700000000.001") == "1700000000.999"
        mock_post.assert_awaited_once()

        # Verify token usage log output
        assert any("Live Note Update Completed" in record.message for record in caplog.records)
        assert any("Tokens: Prompt=100, Completion=50, Total=150" in record.message for record in caplog.records)


@pytest.mark.anyio
async def test_update_notes_for_thread_updates_existing_note_on_subsequent_replies(caplog):
    import logging

    caplog.set_level(logging.INFO)

    # Pre-set existing note message timestamp
    set_thread_note("C111", "1700000000.001", "1700000000.999")

    history = [
        {"role": "user", "content": "Start kickoff"},
        {"role": "assistant", "content": "[Visionary]: Vision defined."},
        {"role": "assistant", "content": "[Sceptic]: Security and privacy must be evaluated."},
    ]

    with patch("agents.thread_notes_agent.fetch_thread_history", new_callable=AsyncMock) as mock_fetch, \
         patch("agents.thread_notes_agent.generate_thread_notes", new_callable=AsyncMock) as mock_gen, \
         patch("agents.thread_notes_agent.update_thread_note", new_callable=AsyncMock) as mock_update, \
         patch("agents.thread_notes_agent.post_thread_note", new_callable=AsyncMock) as mock_post:

        mock_fetch.return_value = history
        mock_gen.return_value = (
            "📋 *Strateška sinteza i akcijski plan*\n⚠️ *Rizici*\n• Sigurnost i privatnost.",
            {"prompt_tokens": 200, "completion_tokens": 90, "total_tokens": 290},
        )
        mock_update.return_value = {"ok": True}

        res = await update_notes_for_thread(channel_id="C111", thread_ts="1700000000.001")

        assert res["ok"] is True
        assert res["note_ts"] == "1700000000.999"
        mock_update.assert_awaited_once()
        mock_post.assert_not_called()

        # Verify token logging
        assert any("Tokens: Prompt=200, Completion=90, Total=290" in record.message for record in caplog.records)


@pytest.mark.anyio
async def test_trigger_thread_notes_update_spawns_and_coalesces_task():
    with patch("agents.thread_notes_agent.update_notes_for_thread", new_callable=AsyncMock) as mock_update:
        mock_update.return_value = {"ok": True}

        task = trigger_thread_notes_update(channel_id="C222", thread_ts="1700000000.002")

        assert isinstance(task, asyncio.Task)
        # Calling trigger again while task is active should coalesce
        task2 = trigger_thread_notes_update(channel_id="C222", thread_ts="1700000000.002")
        assert task2 == task

        await task
        assert mock_update.called


@pytest.mark.anyio
async def test_slack_client_post_and_update_thread_note_mock_mode(monkeypatch):
    from services import slack_client
    monkeypatch.setattr(slack_client, "SLACK_BOT_TOKEN", "xoxb-your-bot-user-oauth-token")

    res_post = await post_thread_note(
        channel_id="C_MOCK",
        thread_ts="1700000000.001",
        text="*Live Notes Mock*",
        pin=True,
    )
    assert res_post["ok"] is True
    assert "ts" in res_post

    note_ts = res_post["ts"]
    res_update = await update_thread_note(
        channel_id="C_MOCK",
        message_ts=note_ts,
        text="*Updated Notes Mock*",
    )
    assert res_update["ok"] is True


@pytest.mark.anyio
async def test_slack_client_note_api_calls(monkeypatch):
    from services import slack_client
    monkeypatch.setattr(slack_client, "SLACK_BOT_TOKEN", "xoxb-valid-test-token")

    mock_post = AsyncMock(return_value={"ok": True, "ts": "1700000000.888"})
    mock_pin = AsyncMock(return_value={"ok": True})
    mock_update = AsyncMock(return_value={"ok": True, "ts": "1700000000.888"})

    monkeypatch.setattr(slack_client.slack_async_client, "chat_postMessage", mock_post)
    monkeypatch.setattr(slack_client.slack_async_client, "pins_add", mock_pin)
    monkeypatch.setattr(slack_client.slack_async_client, "chat_update", mock_update)

    res_post = await post_thread_note(
        channel_id="C12345",
        thread_ts="1700000000.001",
        text="📋 *Live Notes*",
        pin=True,
        username="Live Discussion Scribe",
        icon_emoji=":memo:",
    )
    assert res_post["ok"] is True
    assert res_post["ts"] == "1700000000.888"
    mock_post.assert_awaited_once_with(
        channel="C12345",
        thread_ts="1700000000.001",
        text="📋 *Live Notes*",
        username="Live Discussion Scribe",
        icon_emoji=":memo:",
    )
    mock_pin.assert_awaited_once_with(channel="C12345", timestamp="1700000000.888")

    res_update = await update_thread_note(
        channel_id="C12345",
        message_ts="1700000000.888",
        text="📋 *Updated Live Notes*",
    )
    assert res_update["ok"] is True
    mock_update.assert_awaited_once_with(
        channel="C12345",
        ts="1700000000.888",
        text="📋 *Updated Live Notes*",
    )


@pytest.mark.anyio
async def test_router_invokes_trigger_thread_notes_update(monkeypatch):
    from orchestration import agent_router

    mock_trigger = MagicMock()
    monkeypatch.setattr("orchestration.agent_router.trigger_thread_notes_update", mock_trigger)
    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool",
        AsyncMock(
            return_value=MagicMock(
                choices=[MagicMock(message=MagicMock(content="Hello world [Sceptic]", tool_calls=None))],
                usage=MagicMock(prompt_tokens=10, completion_tokens=10, total_tokens=20),
            )
        ),
    )
    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response",
        AsyncMock(return_value={"ok": True, "ts": "1700000000.999"}),
    )
    monkeypatch.setattr("orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[]))

    # 1. Multi-agent debate trigger (is_completed=False)
    await agent_router.process_and_respond(
        channel_id="C_ROUTER_TEST",
        thread_ts="1700000000.999",
        raw_text="<@U_BOT> Let's discuss product vision",
        bot_user_id="U_BOT",
    )

    mock_trigger.assert_called_once_with(
        channel_id="C_ROUTER_TEST",
        thread_ts="1700000000.999",
        is_completed=False,
        bot_user_id="U_BOT",
    )

    # 2. Single-agent explicit trigger (is_completed=True)
    mock_trigger.reset_mock()
    await agent_router.process_and_respond(
        channel_id="C_ROUTER_TEST_2",
        thread_ts="1700000000.888",
        raw_text="[Visionary] only you answer",
        bot_user_id="U_BOT",
    )

    mock_trigger.assert_called_once_with(
        channel_id="C_ROUTER_TEST_2",
        thread_ts="1700000000.888",
        is_completed=True,
        bot_user_id="U_BOT",
    )
