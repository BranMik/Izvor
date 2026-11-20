import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.thread_notes_agent import (
    generate_conclusion_notes,
    generate_thread_notes,
    get_thread_conclusion_content,
    get_thread_note,
    get_thread_note_content,
    reset_thread_notes,
    set_live_note_pinned,
    set_thread_conclusion_note,
    set_thread_note,
    set_thread_note_content,
    trigger_thread_notes_update,
    update_notes_for_thread,
)
from core.config import get_orchestrator_fast_model, get_system_agent_model
from services.slack import post_thread_note, update_thread_note


@pytest.fixture(autouse=True)
def clean_notes_state():
    from orchestration.agent_router import configure_router_sleep_fn
    configure_router_sleep_fn(AsyncMock(return_value=None))
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

    with patch("pipeline.discussion_scribe.scribe.litellm.acompletion", return_value=mock_resp) as mock_litellm:
        content, token_stats = await generate_thread_notes(history, is_completed=False)

        assert mock_litellm.called
        call_kwargs = mock_litellm.call_args.kwargs
        expected_scribe_model = get_system_agent_model("DiscussionScribe", fallback="mistral/mistral-small-latest")
        assert call_kwargs["model"] == expected_scribe_model
        assert "max_tokens" not in call_kwargs
        assert "Strategic Synthesis" in content
        assert "Core Objective" in content
        assert token_stats["prompt_tokens"] == 150
        assert token_stats["completion_tokens"] == 80
        assert token_stats["total_tokens"] == 230
        assert token_stats["model"] == expected_scribe_model


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
async def test_slack_client_post_and_update_thread_note_mock_mode():
    from services import slack as slack_client
    slack_client.configure_token("xoxb-your-bot-user-oauth-token")
    try:
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
    finally:
        slack_client.reset_token()


@pytest.mark.anyio
async def test_slack_client_note_api_calls():
    from services import slack as slack_client
    from services.slack import configure_slack_async_client, reset_slack_async_client
    slack_client.configure_token("xoxb-valid-test-token")
    try:
        mock_post = AsyncMock(return_value={"ok": True, "ts": "1700000000.888"})
        mock_pin = AsyncMock(return_value={"ok": True})
        mock_update = AsyncMock(return_value={"ok": True, "ts": "1700000000.888"})

        mock_client = AsyncMock()
        mock_client.chat_postMessage = mock_post
        mock_client.pins_add = mock_pin
        mock_client.chat_update = mock_update
        configure_slack_async_client(mock_client)
        try:
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
                unfurl_links=False,
                unfurl_media=False,
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
                unfurl_links=False,
                unfurl_media=False,
            )
        finally:
            reset_slack_async_client()
    finally:
        slack_client.reset_token()


@pytest.mark.anyio
async def test_router_invokes_trigger_thread_notes_update():
    from agents.thread_notes_agent import configure_trigger_thread_notes_update
    from core.llm_utils import configure_llm_completion_fn
    from orchestration import agent_router
    from orchestration.agent_router import (
        configure_fetch_thread_history,
        configure_post_agent_response,
    )

    mock_trigger = MagicMock()
    configure_trigger_thread_notes_update(mock_trigger)
    configure_llm_completion_fn(
        AsyncMock(
            return_value=MagicMock(
                choices=[MagicMock(message=MagicMock(content="Hello world [agent-10003]", tool_calls=None))],
                usage=MagicMock(prompt_tokens=10, completion_tokens=10, total_tokens=20),
            )
        )
    )
    configure_post_agent_response(
        AsyncMock(return_value={"ok": True, "ts": "1700000000.999"})
    )
    configure_fetch_thread_history(AsyncMock(return_value=[]))

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
async def test_fetch_thread_history_excludes_scribe_notes():
    from services import slack as slack_client

    slack_client.configure_token("xoxb-valid-test-token")
    try:
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

        mock_client = AsyncMock()
        mock_client.conversations_replies.return_value = {"ok": True, "messages": fake_messages}
        from services.slack import configure_slack_async_client, reset_slack_async_client

        configure_slack_async_client(mock_client)
        try:
            history = await slack_client.fetch_thread_history("C123", "1700000000.001")
        finally:
            reset_slack_async_client()
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
    finally:
        slack_client.reset_token()


@pytest.mark.anyio
async def test_scribe_disabled_skips_trigger_and_update():
    from orchestration.app_settings import set_scribe_enabled

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
async def test_update_notes_for_thread_completed_unpins_live_and_pins_conclusion():
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

    mock_live_notes = ("### **Live Strategic Synthesis**\n**Problem Statement**: Choose DB.", {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20})
    mock_conc_notes = (
        "---\n### **Problem Statement**\nChoose optimal database architecture.\n\n"
        "### **Consensus Decisions**\n• Need high availability.\n\n"
        "### **Divergent Positions & Trade-offs**\n"
        "• **PostgreSQL** ([agent-10001]): Strong ACID guarantees.\n"
        "• **MongoDB** ([agent-10003]): Schema agility and horizontal scaling.\n\n"
        "### **Action Items & Next Steps**\n• Benchmark connection pooling.",
        {"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50},
    )

    unpinned_ts_list = []
    posted_notes = []

    async def mock_post_thread_note(channel_id, thread_ts, text, pin=True, username="Live Discussion Scribe", icon_emoji=":memo:", blocks=None):
        ts = f"mock_ts_{len(posted_notes) + 1}.000"
        posted_notes.append({"channel_id": channel_id, "thread_ts": thread_ts, "text": text, "pin": pin, "username": username, "ts": ts, "blocks": blocks})
        return {"ok": True, "ts": ts}

    async def mock_unpin_thread_note(channel_id, message_ts):
        unpinned_ts_list.append(message_ts)
        return {"ok": True}

    with (
        patch("agents.thread_notes_agent.fetch_thread_history", AsyncMock(return_value=history)),
        patch("agents.thread_notes_agent.generate_thread_notes", AsyncMock(return_value=mock_live_notes)),
        patch("agents.thread_notes_agent.generate_conclusion_notes", AsyncMock(return_value=mock_conc_notes)),
        patch("agents.thread_notes_agent.post_thread_note", mock_post_thread_note),
        patch("agents.thread_notes_agent.unpin_thread_note", mock_unpin_thread_note),
    ):
        # 1. Active debate turn (is_completed=False)
        res_active = await update_notes_for_thread(channel_id, thread_ts, is_completed=False)
        assert res_active["ok"] is True
        live_ts = res_active["note_ts"]
        assert live_ts == "mock_ts_1.000"
        assert len(posted_notes) == 1
        assert posted_notes[0]["username"] == "Live Discussion Scribe"
        assert posted_notes[0]["pin"] is True
        assert unpinned_ts_list == []

        # Live note posted to Slack must be Slack mrkdwn, not raw markdown
        live_post = posted_notes[0]
        assert "*Live Strategic Synthesis*" in live_post["text"]
        assert "*Problem Statement*: Choose DB." in live_post["text"]
        assert "###" not in live_post["text"]
        assert "**" not in live_post["text"]
        live_block_text = live_post["blocks"][0]["text"]["text"]
        assert "*Live Strategic Synthesis*" in live_block_text
        assert "###" not in live_block_text
        # Cached/DB content keeps raw markdown for .txt downloads
        assert "### **Live Strategic Synthesis**" in (get_thread_note_content(channel_id, thread_ts) or "")

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
        # Conclusion posted to Slack must be Slack mrkdwn, not raw markdown
        assert "*Problem Statement*" in conc_post["text"]
        assert "*Consensus Decisions*" in conc_post["text"]
        assert "*Divergent Positions & Trade-offs*" in conc_post["text"]
        assert "*Action Items & Next Steps*" in conc_post["text"]
        assert "###" not in conc_post["text"]
        assert "**" not in conc_post["text"]
        assert "──────────" in conc_post["text"]  # '---' converted to Slack divider
        conc_block_text = conc_post["blocks"][0]["text"]["text"]
        assert "*Problem Statement*" in conc_block_text
        assert "###" not in conc_block_text
        assert "PostgreSQL" in conc_post["text"]
        assert "MongoDB" in conc_post["text"]
        # Cached/DB conclusion content keeps raw markdown for .txt downloads
        cached_conc = get_thread_conclusion_content(channel_id, thread_ts) or ""
        assert "### **Problem Statement**" in cached_conc


@pytest.mark.anyio
async def test_update_notes_for_thread_reengagement_updates_existing_notes():
    """
    On user follow-up queries after completion, subsequent completion calls
    must update existing live note and existing conclusion note in-place.
    """
    channel_id = "C_REENGAGE_TEST"
    thread_ts = "1700000000.999"

    mock_update_note = AsyncMock(return_value={"ok": True})

    with (
        patch("agents.thread_notes_agent.update_thread_note", mock_update_note),
        patch("agents.thread_notes_agent.unpin_thread_note", new_callable=AsyncMock, return_value={"ok": True}),
        patch("agents.thread_notes_agent.fetch_thread_history", new_callable=AsyncMock, return_value=[{"role": "user", "content": "What about costs?"}]),
        patch("agents.thread_notes_agent.generate_thread_notes", new_callable=AsyncMock, return_value=("Updated live notes", {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20})),
        patch("agents.thread_notes_agent.generate_conclusion_notes", new_callable=AsyncMock, return_value=("Updated conclusion notes", {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20})),
    ):
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


def test_thread_notes_sqlite_persistence_and_cache_reload():
    from core.database import get_connection
    from agents.thread_notes_agent import (
        get_thread_conclusion_content,
        get_thread_conclusion_note,
        get_thread_note,
        get_thread_note_content,
        set_thread_conclusion_content,
        set_thread_conclusion_note,
        set_thread_note,
        set_thread_note_content,
    )

    channel_id = "C_SQLITE_NOTES"
    thread_ts = "1700000000.777"

    set_thread_note(channel_id, thread_ts, "live_ts_999")
    set_thread_note_content(channel_id, thread_ts, "Live synthesis content")
    set_thread_conclusion_note(channel_id, thread_ts, "conc_ts_888")
    set_thread_conclusion_content(channel_id, thread_ts, "Conclusion content")

    # Verify directly in SQLite
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM thread_notes_state WHERE channel_id = ? AND thread_ts = ?;",
            (channel_id, thread_ts),
        ).fetchone()
        assert row is not None
        assert row["live_note_ts"] == "live_ts_999"
        assert row["live_note_content"] == "Live synthesis content"
        assert row["conclusion_note_ts"] == "conc_ts_888"
        assert row["conclusion_note_content"] == "Conclusion content"

    # Querying getters should read directly from SQLite SSOT
    assert get_thread_note(channel_id, thread_ts) == "live_ts_999"
    assert get_thread_note_content(channel_id, thread_ts) == "Live synthesis content"
    assert get_thread_conclusion_note(channel_id, thread_ts) == "conc_ts_888"
    assert get_thread_conclusion_content(channel_id, thread_ts) == "Conclusion content"


@pytest.mark.anyio
async def test_generate_thread_notes_reasoning_exhaustion_fallback():
    """Verify Scribe retries on orchestrator fast model when primary returns empty content due to reasoning/length."""
    empty_choice = MagicMock()
    empty_choice.finish_reason = "length"
    empty_choice.message = MagicMock(
        content="",
        reasoning_content="Extensive internal chain of thought that exhausted output budget...",
    )
    first_resp = MagicMock(choices=[empty_choice], usage=MagicMock(prompt_tokens=200, completion_tokens=150))

    recovered_choice = MagicMock()
    recovered_choice.finish_reason = "stop"
    recovered_choice.message = MagicMock(content="*Core Objective*\nRecovered synthesis note.", reasoning_content=None)
    second_resp = MagicMock(choices=[recovered_choice], usage=MagicMock(prompt_tokens=210, completion_tokens=40))

    history = [
        {"role": "user", "content": "Evaluate failover architecture"},
        {"role": "assistant", "content": "[Architect]: Recommend active-passive failover."},
    ]

    expected_primary = get_system_agent_model("DiscussionScribe", fallback="mistral/mistral-small-latest")
    expected_fallback = get_orchestrator_fast_model(fallback=expected_primary) or expected_primary

    with patch(
        "pipeline.discussion_scribe.scribe.litellm.acompletion",
        side_effect=[first_resp, second_resp],
    ) as mock_litellm:
        content, token_stats = await generate_thread_notes(history, is_completed=True)

    assert mock_litellm.call_count == 2
    first_kwargs = mock_litellm.call_args_list[0].kwargs
    second_kwargs = mock_litellm.call_args_list[1].kwargs
    assert first_kwargs["model"] == expected_primary
    assert "max_tokens" not in first_kwargs
    assert second_kwargs["model"] == expected_fallback
    assert "max_tokens" not in second_kwargs
    assert content == "*Core Objective*\nRecovered synthesis note."
    assert token_stats["prompt_tokens"] == 410
    assert token_stats["completion_tokens"] == 190
    assert token_stats["total_tokens"] == 600
    assert token_stats["model"] == expected_fallback


@pytest.mark.anyio
async def test_generate_conclusion_notes_double_empty_returns_unavailable_marker():
    """Verify double empty LLM responses yield a non-empty diagnostic marker rather than silent empty text."""
    empty_choice = MagicMock()
    empty_choice.finish_reason = "length"
    empty_choice.message = MagicMock(content="   ", reasoning_content=None)
    empty_resp = MagicMock(choices=[empty_choice], usage=MagicMock(prompt_tokens=100, completion_tokens=50))

    history = [
        {"role": "user", "content": "Should we migrate?"},
        {"role": "assistant", "content": "[Architect]: Yes, staged migration."},
    ]

    with patch(
        "pipeline.discussion_scribe.scribe.litellm.acompletion",
        side_effect=[empty_resp, empty_resp],
    ) as mock_litellm:
        content, token_stats = await generate_conclusion_notes(history)

    assert mock_litellm.call_count == 2
    assert "max_tokens" not in mock_litellm.call_args_list[0].kwargs
    assert content.startswith("*(Conclusion unavailable:")
    assert token_stats["total_tokens"] == 300


@pytest.mark.anyio
async def test_update_notes_for_thread_preserves_existing_live_note_and_skips_empty_conclusion():
    """Verify unusable Scribe/Conclusion output preserves existing live notes and avoids empty Conclusion posts."""
    channel_id = "C_PRESERVE"
    thread_ts = "1700000000.888"
    set_thread_note(channel_id, thread_ts, "live_ts_keep")
    set_thread_note_content(channel_id, thread_ts, "Existing valid live synthesis")
    set_live_note_pinned(channel_id, thread_ts, True)

    history = [
        {"role": "user", "content": "Design gateway"},
        {"role": "assistant", "content": "[Architect]: Use reverse proxy."},
    ]
    expected_scribe_model = get_system_agent_model("DiscussionScribe", fallback="mistral/mistral-small-latest")

    with patch("agents.thread_notes_agent.fetch_thread_history", new_callable=AsyncMock, return_value=history), \
         patch(
             "agents.thread_notes_agent.generate_thread_notes",
             new_callable=AsyncMock,
             return_value=("", {"prompt_tokens": 50, "completion_tokens": 100, "total_tokens": 150, "model": expected_scribe_model}),
         ), \
         patch(
             "agents.thread_notes_agent.generate_conclusion_notes",
             new_callable=AsyncMock,
             return_value=(
                 "*(Conclusion unavailable: model returned empty content)*",
                 {"prompt_tokens": 80, "completion_tokens": 120, "total_tokens": 200, "model": expected_scribe_model},
             ),
         ), \
         patch("agents.thread_notes_agent.unpin_thread_note", new_callable=AsyncMock) as mock_unpin, \
         patch("agents.thread_notes_agent.post_thread_note", new_callable=AsyncMock) as mock_post, \
         patch("agents.thread_notes_agent.update_thread_note", new_callable=AsyncMock) as mock_update, \
         patch("agents.thread_notes_agent.save_thread_notes_to_disk") as mock_save_disk, \
         patch("agents.thread_notes_agent.record_thread_tokens") as mock_record_tokens, \
         patch("agents.thread_notes_agent.trigger_usage_update"):
        res = await update_notes_for_thread(channel_id=channel_id, thread_ts=thread_ts, is_completed=True)

    assert res["ok"] is True
    assert res["conclusion_ts"] is None
    mock_unpin.assert_awaited_once()
    mock_update.assert_not_awaited()
    mock_post.assert_not_awaited()
    assert get_thread_note_content(channel_id, thread_ts) == "Existing valid live synthesis"
    assert get_thread_conclusion_content(channel_id, thread_ts) is None

    mock_save_disk.assert_called_once()
    save_kwargs = mock_save_disk.call_args.kwargs
    assert save_kwargs["live_notes_text"] == "Existing valid live synthesis"
    assert save_kwargs["conclusion_text"] is None

    mock_record_tokens.assert_called_once()
    assert mock_record_tokens.call_args.kwargs["model_name"] == expected_scribe_model


@pytest.mark.anyio
async def test_update_notes_for_thread_skips_empty_initial_live_note_post():
    """Verify empty live synthesis output does not create an empty Slack download-button message."""
    channel_id = "C_SKIP_EMPTY"
    thread_ts = "1700000000.999"
    history = [
        {"role": "user", "content": "Initial question"},
        {"role": "assistant", "content": "[Architect]: Initial response."},
    ]

    with patch("agents.thread_notes_agent.fetch_thread_history", new_callable=AsyncMock, return_value=history), \
         patch(
             "agents.thread_notes_agent.generate_thread_notes",
             new_callable=AsyncMock,
             return_value=("   \n ", {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}),
         ), \
         patch("agents.thread_notes_agent.post_thread_note", new_callable=AsyncMock) as mock_post, \
         patch("agents.thread_notes_agent.record_thread_tokens"), \
         patch("agents.thread_notes_agent.trigger_usage_update"):
        res = await update_notes_for_thread(channel_id=channel_id, thread_ts=thread_ts, is_completed=False)

    assert res["ok"] is True
    assert res["note_ts"] is None
    mock_post.assert_not_awaited()
    assert get_thread_note(channel_id, thread_ts) is None
    assert get_thread_note_content(channel_id, thread_ts) is None


@pytest.mark.anyio
async def test_generate_thread_notes_falls_back_when_scribe_key_missing():
    """Verify DiscussionScribe falls back to first available model when its configured provider key is missing."""
    from core.model_catalog import configure_env_provider

    configure_env_provider(lambda: {"GEMINI_API_KEY": "AIzaSyValidKey"})
    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(
            message=MagicMock(
                content=(
                    "📋 *Strategic Synthesis & Action Plan*\n"
                    "🎯 *Core Objective & Problem Statement*\nFallback scribe synthesis."
                )
            )
        )
    ]
    mock_resp.usage = MagicMock(prompt_tokens=60, completion_tokens=30, total_tokens=90)

    history = [
        {"role": "user", "content": "How should we design the database?"},
        {"role": "assistant", "content": "[agent-10001]: Use PostgreSQL."},
    ]

    with patch("pipeline.discussion_scribe.scribe.litellm.acompletion", return_value=mock_resp) as mock_litellm:
        content, token_stats = await generate_thread_notes(history, is_completed=False)

    assert "Fallback scribe synthesis" in content
    assert mock_litellm.call_args.kwargs["model"] == "gemini/gemini-3.8-flash"
    assert token_stats["model"] == "gemini/gemini-3.8-flash"





