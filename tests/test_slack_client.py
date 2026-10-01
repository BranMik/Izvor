from unittest.mock import AsyncMock

import pytest
from slack_sdk.errors import SlackApiError


@pytest.mark.anyio
async def test_fetch_thread_history_filters_and_formats(monkeypatch):
    """
    Verifies that fetch_thread_history runs real parsing, regex stripping,
    role mapping, and filtering without crashing.
    """
    from services import slack_client

    monkeypatch.setattr(slack_client, "SLACK_BOT_TOKEN", "xoxb-valid-test-token")

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

    mock_replies = AsyncMock(return_value={"ok": True, "messages": raw_messages})
    monkeypatch.setattr(slack_client.slack_async_client, "conversations_replies", mock_replies)

    history = await slack_client.fetch_thread_history(
        channel_id="C_TEST",
        thread_ts="1234567890.123456",
        bot_user_id="U_BOT_APP_ID",
    )

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
    assert history[2] == {"role": "assistant", "content": "[agent-10008]: SQLite is suitable for low-write embedded environments."}

    # Message 4: matched by bot_user_id
    assert history[3] == {"role": "assistant", "content": "[Assistant]: I agree with SecurityArchitect."}


@pytest.mark.anyio
async def test_fetch_thread_history_handles_slack_api_error(monkeypatch):
    """Verifies that SlackApiError is caught and returns empty list gracefully."""
    from services import slack_client

    monkeypatch.setattr(slack_client, "SLACK_BOT_TOKEN", "xoxb-valid-test-token")

    mock_error_replies = AsyncMock(
        side_effect=SlackApiError("channel_not_found", response={"error": "channel_not_found"})
    )
    monkeypatch.setattr(slack_client.slack_async_client, "conversations_replies", mock_error_replies)

    result = await slack_client.fetch_thread_history("C_NOT_FOUND", "123.456")
    assert result == []


@pytest.mark.anyio
async def test_fetch_thread_history_handles_generic_exception(monkeypatch):
    """Verifies that any unexpected exception does not crash the calling background task."""
    from services import slack_client

    monkeypatch.setattr(slack_client, "SLACK_BOT_TOKEN", "xoxb-valid-test-token")

    mock_crashed_replies = AsyncMock(side_effect=RuntimeError("Connection socket reset by peer"))
    monkeypatch.setattr(slack_client.slack_async_client, "conversations_replies", mock_crashed_replies)

    result = await slack_client.fetch_thread_history("C_ERR", "123.456")
    assert result == []


@pytest.mark.anyio
async def test_fetch_thread_history_empty_token(monkeypatch):
    """Verifies empty or dummy token immediately returns empty list without calling API."""
    from services import slack_client

    monkeypatch.setattr(slack_client, "SLACK_BOT_TOKEN", "")
    result = await slack_client.fetch_thread_history("C1", "T1")
    assert result == []


@pytest.mark.anyio
async def test_fetch_thread_history_pagination(monkeypatch):
    """Verifies fetch_thread_history fetches multiple pages when has_more is True."""
    from services import slack_client

    monkeypatch.setattr(slack_client, "SLACK_BOT_TOKEN", "xoxb-valid-test-token")

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

    monkeypatch.setattr(slack_client.slack_async_client, "conversations_replies", mock_replies)

    result = await slack_client.fetch_thread_history("C1", "T1", max_messages=500)
    assert call_count == 2
    assert len(result) == 130
    assert result[0]["content"] == "Msg 0"
    assert result[-1]["content"] == "Msg 129"


@pytest.mark.anyio
async def test_fetch_thread_files_pagination(monkeypatch):
    """Verifies fetch_thread_files paginates to discover file attachments across long threads."""
    from services import slack_client

    monkeypatch.setattr(slack_client, "SLACK_BOT_TOKEN", "xoxb-valid-test-token")

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

    monkeypatch.setattr(slack_client.slack_async_client, "conversations_replies", mock_replies)

    files = await slack_client.fetch_thread_files("C1", "T1", max_messages=500)
    assert len(files) == 2
    file_names = [f["name"] for f in files]
    assert "doc1.txt" in file_names
    assert "doc2.txt" in file_names

