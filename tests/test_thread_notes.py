import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents import thread_notes_agent
from agents.thread_notes_agent import (
    THREAD_CONCLUSION_MSG_MAP,
    THREAD_NOTE_MSG_MAP,
    generate_conclusion_notes,
    generate_thread_notes,
    get_thread_conclusion_content,
    get_thread_conclusion_note,
    get_thread_note,
    get_thread_note_content,
    reset_thread_notes,
    set_thread_conclusion_note,
    set_thread_note,
    trigger_thread_notes_update,
    update_notes_for_thread,
)
from core.config import get_thread_notes_model
from services.slack_client import post_thread_note, unpin_thread_note, update_thread_note


@pytest.fixture(autouse=True)
def clean_notes_state(monkeypatch):
    from orchestration import agent_router
    monkeypatch.setattr(agent_router.asyncio, "sleep", AsyncMock(return_value=None))
    reset_thread_notes()
    yield
    reset_thread_notes()


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
        {"role": "assistant", "content": "[agent-10008]: Use PostgreSQL with connection pooling."},
    ]

    with patch("pipeline.discussion_scribe.scribe.litellm.completion", return_value=mock_resp) as mock_litellm:
        content, token_stats = await generate_thread_notes(history, is_completed=False)

        assert mock_litellm.called
        call_kwargs = mock_litellm.call_args.kwargs
        assert call_kwargs["model"] == get_thread_notes_model()
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
        {"role": "assistant", "content": "[agent-10001]: Here is the vision for our product."},
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
        {"role": "assistant", "content": "[agent-10001]: Vision defined."},
        {"role": "assistant", "content": "[agent-10003]: Security and privacy must be evaluated."},
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
                choices=[MagicMock(message=MagicMock(content="Hello world [agent-10003]", tool_calls=None))],
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
        raw_text="/[agent-10001] only you answer",
        bot_user_id="U_BOT",
    )

    mock_trigger.assert_called_once_with(
        channel_id="C_ROUTER_TEST_2",
        thread_ts="1700000000.888",
        is_completed=True,
        bot_user_id="U_BOT",
    )


@pytest.mark.anyio
async def test_fetch_thread_history_excludes_scribe_notes(monkeypatch):
    from services import slack_client

    monkeypatch.setattr(slack_client, "SLACK_BOT_TOKEN", "xoxb-valid-test-token")

    fake_messages = [
        {"user": "U1", "text": "Let's debate the new API", "subtype": None},
        {
            "user": "UBOT",
            "text": "📋 *Live Discussion Notes*\n\nKey takeaways so far...",
            "username": "Live Discussion Scribe",
            "subtype": "bot_message",
        },
        {
            "user": "UBOT",
            "text": "🎯 *Discussion Conclusion*\n\nFinal consensus and arguments...",
            "username": "Discussion Conclusion",
            "subtype": "bot_message",
        },
        {"user": "UBOT", "text": "We need microservices", "username": "agent-10011", "subtype": "bot_message"},
        {"user": "UBOT", "text": "_⚡ *LegalAdvisor* is loading skill: web_research..._", "username": "agent-10011", "subtype": "bot_message"},
        {"user": "U1", "text": "👤 *Follow-up Prompt:*\n> What about SQLite?", "subtype": None},
    ]

    mock_replies = AsyncMock(return_value={"ok": True, "messages": fake_messages})
    monkeypatch.setattr(slack_client.slack_async_client, "conversations_replies", mock_replies)

    history = await slack_client.fetch_thread_history("C123", "1700000000.001")
    contents = [m["content"] for m in history]

    # Status indicator messages, live scribe, and conclusion notes should be excluded
    assert not any("_⚡" in c for c in contents)
    assert not any("Live Discussion Scribe" in c for c in contents)
    assert not any("Discussion Conclusion" in c for c in contents)

    # Runner follow-up prefix should be cleaned via re.sub
    assert len(history) == 3
    assert contents[0] == "Let's debate the new API"
    assert contents[1] == "[agent-10011]: We need microservices"
    assert contents[2] == "What about SQLite?"


@pytest.mark.anyio
async def test_scribe_disabled_skips_trigger_and_update(monkeypatch):
    from orchestration.agent_settings import set_scribe_enabled

    try:
        set_scribe_enabled(False)
        # 1. trigger_thread_notes_update returns None when disabled
        task = trigger_thread_notes_update(
            channel_id="C_DISABLED_TEST",
            thread_ts="1700000000.050",
            is_completed=False,
        )
        assert task is None

        # 2. update_notes_for_thread returns ok=False when disabled
        res = await update_notes_for_thread(
            channel_id="C_DISABLED_TEST",
            thread_ts="1700000000.050",
        )
        assert res["ok"] is False
        assert res["reason"] == "Scribe is disabled"
    finally:
        set_scribe_enabled(True)


@pytest.mark.anyio
async def test_update_notes_for_thread_completed_unpins_live_and_pins_conclusion(monkeypatch):
    """
    On is_completed=True:
    1. Unpins Live Scribe Note.
    2. Generates Conclusion Note with 2 parts (Problem Statement + Conclusion & Argumentation).
    3. Posts and pins Conclusion Note as 'Discussion Conclusion'.
    """
    channel_id = "C_COMPLETE_TEST"
    thread_ts = "1700000000.777"

    history = [
        {"role": "user", "content": "Choose between SQL and NoSQL"},
        {"role": "assistant", "content": "[agent-10001]: SQL provides strict ACID compliance."},
        {"role": "assistant", "content": "[agent-10003]: NoSQL provides flexible schema and easy scaling."},
    ]

    mock_live_notes = ("📋 *Live Strategic Synthesis*\n🎯 *Problem Statement*\nChoose DB.", {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20})
    mock_conc_notes = (
        "Part 1: *Problem Statement*\nChoose optimal database architecture.\n\n"
        "Part 2: *Conclusion and Argumentation*\n"
        "• *Consensus Decisions*: Need high availability.\n"
        "• *Opposing Conclusions & Divergent Advice*:\n"
        "  - PostgreSQL (Supporting: [agent-10001]): Strong ACID guarantees.\n"
        "  - MongoDB (Supporting: [agent-10003]): Schema agility and horizontal scaling.",
        {"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50},
    )

    unpinned_ts_list = []
    posted_notes = []

    async def mock_post_thread_note(channel_id, thread_ts, text, pin=True, username="Live Discussion Scribe", icon_emoji=":memo:", blocks=None):
        ts = f"mock_ts_{len(posted_notes) + 1}.000"
        posted_notes.append({"channel_id": channel_id, "thread_ts": thread_ts, "text": text, "pin": pin, "username": username, "ts": ts})
        return {"ok": True, "ts": ts}

    async def mock_unpin_thread_note(channel_id, message_ts):
        unpinned_ts_list.append(message_ts)
        return {"ok": True}

    monkeypatch.setattr("agents.thread_notes_agent.fetch_thread_history", AsyncMock(return_value=history))
    monkeypatch.setattr("agents.thread_notes_agent.generate_thread_notes", AsyncMock(return_value=mock_live_notes))
    monkeypatch.setattr("agents.thread_notes_agent.generate_conclusion_notes", AsyncMock(return_value=mock_conc_notes))
    monkeypatch.setattr("agents.thread_notes_agent.post_thread_note", mock_post_thread_note)
    monkeypatch.setattr("agents.thread_notes_agent.unpin_thread_note", mock_unpin_thread_note)

    # 1. Active debate turn (is_completed=False)
    res_active = await update_notes_for_thread(channel_id, thread_ts, is_completed=False)
    assert res_active["ok"] is True
    live_ts = res_active["note_ts"]
    assert live_ts == "mock_ts_1.000"
    assert len(posted_notes) == 1
    assert posted_notes[0]["username"] == "Live Discussion Scribe"
    assert posted_notes[0]["pin"] is True
    assert unpinned_ts_list == []

    # 2. Final round completed (is_completed=True)
    res_completed = await update_notes_for_thread(channel_id, thread_ts, is_completed=True)
    assert res_completed["ok"] is True
    assert res_completed["conclusion_ts"] == "mock_ts_2.000"

    # Verify live note was unpinned
    assert live_ts in unpinned_ts_list

    # Verify conclusion note was posted and pinned as 'Discussion Conclusion'
    assert len(posted_notes) == 2
    conc_post = posted_notes[1]
    assert conc_post["username"] == "Discussion Conclusion"
    assert conc_post["pin"] is True
    assert "Part 1: *Problem Statement*" in conc_post["text"]
    assert "Part 2: *Conclusion and Argumentation*" in conc_post["text"]
    assert "PostgreSQL" in conc_post["text"]
    assert "MongoDB" in conc_post["text"]


@pytest.mark.anyio
async def test_update_notes_for_thread_reengagement_updates_existing_notes(monkeypatch):
    """
    On user follow-up queries after completion, subsequent completion calls
    must update existing live note and existing conclusion note in-place.
    """
    channel_id = "C_REENGAGE_TEST"
    thread_ts = "1700000000.999"

    mock_update_note = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("agents.thread_notes_agent.update_thread_note", mock_update_note)
    monkeypatch.setattr("agents.thread_notes_agent.unpin_thread_note", AsyncMock(return_value={"ok": True}))
    monkeypatch.setattr("agents.thread_notes_agent.fetch_thread_history", AsyncMock(return_value=[{"role": "user", "content": "What about costs?"}]))
    monkeypatch.setattr("agents.thread_notes_agent.generate_thread_notes", AsyncMock(return_value=("Updated live notes", {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20})))
    monkeypatch.setattr("agents.thread_notes_agent.generate_conclusion_notes", AsyncMock(return_value=("Updated conclusion notes", {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20})))

    # Pre-populate state as already created
    set_thread_note(channel_id, thread_ts, "live_ts_123")
    set_thread_conclusion_note(channel_id, thread_ts, "conclusion_ts_456")

    res = await update_notes_for_thread(channel_id, thread_ts, is_completed=True)
    assert res["ok"] is True

    # Both notes should be updated in-place via update_thread_note
    calls = mock_update_note.call_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["message_ts"] == "live_ts_123"
    assert calls[1].kwargs["message_ts"] == "conclusion_ts_456"


