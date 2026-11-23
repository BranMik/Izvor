from unittest.mock import AsyncMock, MagicMock

import pytest
from slack_sdk.errors import SlackApiError
from services import slack as slack_client
from services.slack import configure_slack_async_client, reset_slack_async_client


@pytest.fixture(autouse=True)
def configure_slack_token_for_tests():
    slack_client.configure_token("xoxb-valid-test-token")
    yield
    slack_client.reset_token()


@pytest.mark.anyio
async def test_fetch_thread_history_filters_and_formats():
    """
    Verifies that fetch_thread_history runs real parsing, regex stripping,
    role mapping, and filtering without crashing.
    """
    raw_messages = [
        # 1. Plain user message
        {"user": "U_USER1", "text": "Can we discuss the architecture?", "subtype": None},
        # 2. Runner follow-up prompt requiring re.sub cleaning
        {"user": "U_USER1", "text": "👤 *Follow-up Prompt:*\n> What about SQLite for storage?", "subtype": None},
        # 3. Status messages to be ignored
        {"user": "U_BOT", "text": "_⚡ *SecurityArchitect* is loading skill: db_inspect..._", "subtype": "bot_message"},
        {"user": "U_BOT", "text": "_🔍 Searching online resources..._", "subtype": "bot_message"},
        # 4. System warnings / scribe / test cards to be ignored
        {"user": "U_BOT", "text": "📋 Scribe summary note", "username": "Live Discussion Scribe", "subtype": "bot_message"},
        {"user": "U_BOT", "text": "🧪 E2E test card", "username": "E2E Test Runner", "subtype": "bot_message"},
        {"user": "U_BOT", "text": "⚠️ Warning: Spend limit close", "username": "Panel System", "subtype": "bot_message"},
        # 5. Legitimate bot assistant message
        {"user": "U_BOT", "text": "SQLite is suitable for low-write embedded environments.", "username": "agent-10008", "subtype": "bot_message"},
        # 6. Bot message matched by bot_user_id
        {"user": "U_BOT_APP_ID", "text": "I agree with SecurityArchitect.", "subtype": None},
    ]

    mock_client = AsyncMock()
    mock_client.conversations_replies.return_value = {"ok": True, "messages": raw_messages}
    configure_slack_async_client(mock_client)
    try:
        history = await slack_client.fetch_thread_history(
            channel_id="C_TEST",
            thread_ts="1234567890.123456",
            bot_user_id="U_BOT_APP_ID",
        )
    finally:
        reset_slack_async_client()

    # Status indicators, scribe notes, and system cards must be filtered out
    contents = [m["content"] for m in history]
    assert not any("_⚡" in c for c in contents)
    assert not any("_🔍" in c for c in contents)
    assert not any("Live Discussion Scribe" in c for c in contents)
    assert not any("E2E Test Runner" in c for c in contents)
    assert not any("Warning: Spend limit" in c for c in contents)

    # Remaining messages count
    assert len(history) == 4

    # Message 1: plain user
    assert history[0] == {"role": "user", "content": "Can we discuss the architecture?"}

    # Message 2: runner follow-up cleaned via regex
    assert history[1] == {"role": "user", "content": "What about SQLite for storage?"}

    # Message 3: assistant persona
    assert history[2] == {
        "role": "assistant",
        "username": "agent-10008",
        "content": "[agent-10008]: SQLite is suitable for low-write embedded environments.",
    }

    # Message 4: matched by bot_user_id
    assert history[3] == {
        "role": "assistant",
        "username": "Assistant",
        "content": "[Assistant]: I agree with SecurityArchitect.",
    }


@pytest.mark.anyio
async def test_fetch_thread_history_preserves_human_warning_emoji_and_filters_orchestrator():
    """TKT-1215: human messages beginning with warning emoji are kept; Orchestrator bot banners are filtered."""
    raw_messages = [
        {"user": "U_HUMAN_1", "text": "⚠️ Why did our server alert fire?", "subtype": None},
        {
            "user": "U_BOT",
            "text": "⚠️ *Ambiguous Agent Reference*: Please specify.",
            "username": "Orchestrator",
            "subtype": "bot_message",
        },
        {
            "user": "U_BOT",
            "text": "Checking telemetry now.\n\nSkills used: [web_search]\n\n{gemini/gemini-3.8-flash : 412/98}",
            "username": "The Pragmatist",
            "subtype": "bot_message",
        },
    ]

    mock_client = AsyncMock()
    mock_client.conversations_replies.return_value = {"ok": True, "messages": raw_messages}
    configure_slack_async_client(mock_client)
    try:
        history = await slack_client.fetch_thread_history(
            channel_id="C_TEST",
            thread_ts="1700000000.0001",
            bot_user_id="U_BOT",
        )
    finally:
        reset_slack_async_client()

    assert len(history) == 2
    assert history[0] == {"role": "user", "content": "⚠️ Why did our server alert fire?"}
    assert history[1]["username"] == "The Pragmatist"


@pytest.mark.anyio
async def test_fetch_thread_history_handles_slack_api_error():
    """Verifies that SlackApiError is caught and returns empty list gracefully."""
    mock_client = AsyncMock()
    mock_client.conversations_replies.side_effect = SlackApiError(
        "channel_not_found", response={"error": "channel_not_found"}
    )
    configure_slack_async_client(mock_client)
    try:
        result = await slack_client.fetch_thread_history("C_NOT_FOUND", "123.456")
    finally:
        reset_slack_async_client()
    assert result == []


@pytest.mark.anyio
async def test_fetch_thread_history_handles_generic_exception():
    """Verifies that any unexpected exception does not crash the calling background task."""
    mock_client = AsyncMock()
    mock_client.conversations_replies.side_effect = RuntimeError(
        "Connection socket reset by peer"
    )
    configure_slack_async_client(mock_client)
    try:
        result = await slack_client.fetch_thread_history("C_ERR", "123.456")
    finally:
        reset_slack_async_client()
    assert result == []


@pytest.mark.anyio
async def test_fetch_thread_history_empty_token():
    """Verifies empty or dummy token immediately returns empty list without calling API."""
    slack_client.configure_token("")
    try:
        result = await slack_client.fetch_thread_history("C1", "T1")
        assert result == []
    finally:
        slack_client.configure_token("xoxb-valid-test-token")


@pytest.mark.anyio
async def test_fetch_thread_history_pagination():
    """Verifies fetch_thread_history fetches multiple pages when has_more is True."""
    page1_msgs = [{"user": "U1", "text": f"Msg {i}", "subtype": None} for i in range(100)]
    page2_msgs = [{"user": "U1", "text": f"Msg {i}", "subtype": None} for i in range(100, 130)]

    call_count = 0

    async def mock_replies(**kwargs):
        nonlocal call_count
        call_count += 1
        cursor = kwargs.get("cursor")
        if not cursor:
            return {
                "ok": True,
                "messages": page1_msgs,
                "has_more": True,
                "response_metadata": {"next_cursor": "cursor_page_2"},
            }
        elif cursor == "cursor_page_2":
            return {
                "ok": True,
                "messages": page2_msgs,
                "has_more": False,
                "response_metadata": {},
            }
        return {"ok": True, "messages": [], "has_more": False}

    mock_client = AsyncMock()
    mock_client.conversations_replies.side_effect = mock_replies
    configure_slack_async_client(mock_client)
    try:
        result = await slack_client.fetch_thread_history("C1", "T1", max_messages=500)
    finally:
        reset_slack_async_client()
    assert call_count == 2
    assert len(result) == 130
    assert result[0]["content"] == "Msg 0"
    assert result[-1]["content"] == "Msg 129"


@pytest.mark.anyio
async def test_fetch_thread_files_pagination():
    """Verifies fetch_thread_files paginates to discover file attachments across long threads."""
    page1_msgs = [
        {"user": "U1", "ts": "1.0", "files": [{"id": "F1", "name": "doc1.txt"}]}
    ]
    page2_msgs = [
        {"user": "U2", "ts": "2.0", "files": [{"id": "F2", "name": "doc2.txt"}]}
    ]

    async def mock_replies(**kwargs):
        if not kwargs.get("cursor"):
            return {
                "ok": True,
                "messages": page1_msgs,
                "has_more": True,
                "response_metadata": {"next_cursor": "c2"},
            }
        return {
            "ok": True,
            "messages": page2_msgs,
            "has_more": False,
            "response_metadata": {},
        }

    mock_client = AsyncMock()
    mock_client.conversations_replies.side_effect = mock_replies
    configure_slack_async_client(mock_client)
    try:
        files = await slack_client.fetch_thread_files("C1", "T1", max_messages=500)
    finally:
        reset_slack_async_client()
    assert len(files) == 2
    file_names = [f["name"] for f in files]
    assert "doc1.txt" in file_names
    assert "doc2.txt" in file_names


def test_sanitize_slack_broadcast_mentions():
    """Verifies that Slack broadcast mentions are neutralized to safe plain text."""
    from core.slack_security import sanitize_slack_broadcast_mentions

    assert sanitize_slack_broadcast_mentions("Hello <!channel>, please review.") == "Hello @channel, please review."
    assert sanitize_slack_broadcast_mentions("Alert <!here> and <!everyone>!") == "Alert @here and @everyone!"
    assert sanitize_slack_broadcast_mentions("Normal user <@U12345> stay intact.") == "Normal user <@U12345> stay intact."
    assert sanitize_slack_broadcast_mentions("") == ""
    assert sanitize_slack_broadcast_mentions(None) is None


@pytest.mark.anyio
async def test_post_agent_response_unfurl_and_sanitize():
    """Verifies post_agent_response sanitizes broadcast mentions and disables unfurling."""
    captured_kwargs = {}

    async def mock_post(**kwargs):
        captured_kwargs.update(kwargs)
        return MagicMock(data={"ok": True, "ts": "123.456"})

    mock_client = AsyncMock()
    mock_client.chat_postMessage.side_effect = mock_post
    configure_slack_async_client(mock_client)
    try:
        res = await slack_client.post_agent_response(
            channel_id="C_TEST",
            thread_ts="123.456",
            text="Attention <!channel>! Check https://example.com",
            username="TestAgent",
        )
    finally:
        reset_slack_async_client()

    assert res["ok"] is True
    assert captured_kwargs["unfurl_links"] is False
    assert captured_kwargs["unfurl_media"] is False
    assert "<!channel>" not in captured_kwargs["text"]
    assert "@channel" in captured_kwargs["text"]


