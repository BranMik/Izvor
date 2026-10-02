from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agents.thread_notes_agent import (
    build_scribe_note_blocks,
    clear_thread_note,
    get_thread_note_content,
    reset_thread_notes,
    set_thread_note_content,
    update_notes_for_thread,
)
from main import app
from services.slack_client import upload_text_file_to_slack


def test_build_scribe_note_blocks():
    text = "📋 *Live Strategic Synthesis*\n- Key takeaway 1\n- Key takeaway 2"
    channel_id = "C_TEST_123"
    thread_ts = "1710000000.000100"

    blocks = build_scribe_note_blocks(text, channel_id, thread_ts)

    assert len(blocks) == 2
    assert blocks[0]["type"] == "section"
    assert blocks[0]["text"]["type"] == "mrkdwn"
    assert blocks[0]["text"]["text"] == text

    assert blocks[1]["type"] == "actions"
    assert blocks[1]["block_id"] == "scribe_actions_block"
    assert len(blocks[1]["elements"]) == 1

    btn = blocks[1]["elements"][0]
    assert btn["type"] == "button"
    assert btn["action_id"] == "download_scribe_notes_txt"
    assert btn["value"] == f"{channel_id}:{thread_ts}"
    assert "Download Notes" in btn["text"]["text"]


def test_build_scribe_note_blocks_truncates_long_text():
    very_long_text = "A" * 3500
    blocks = build_scribe_note_blocks(very_long_text, "C1", "123.456")
    sec_text = blocks[0]["text"]["text"]
    assert len(sec_text) <= 3000
    assert "...[truncated]" in sec_text


def test_thread_note_content_cache():
    reset_thread_notes()
    channel_id = "C_TEST_CACHE"
    thread_ts = "1710000000.000200"
    content = "Test synthesis notes content"

    assert get_thread_note_content(channel_id, thread_ts) is None

    set_thread_note_content(channel_id, thread_ts, content)
    assert get_thread_note_content(channel_id, thread_ts) == content

    clear_thread_note(channel_id, thread_ts)
    assert get_thread_note_content(channel_id, thread_ts) is None


@pytest.mark.anyio
async def test_upload_text_file_to_slack_mock():
    res = await upload_text_file_to_slack(
        channel_id="D_TEST_DM",
        content="Hello file content",
        filename="test_notes.txt",
        title="Test Notes Title",
    )
    assert res["ok"] is True
    assert res["mock"] is True
    assert res["file"]["name"] == "test_notes.txt"
    assert res["file"]["content"] == "Hello file content"


@pytest.mark.anyio
async def test_update_notes_for_thread_passes_blocks(monkeypatch):
    channel_id = "C_SCRIBE_BLOCKS"
    thread_ts = "1710000000.000300"
    clear_thread_note(channel_id, thread_ts)

    mock_history = [{"role": "user", "content": "Hello team"}]
    mock_notes = ("📋 *Synthesis Notes*", {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30})

    with patch("agents.thread_notes_agent.fetch_thread_history", new_callable=AsyncMock, return_value=mock_history), \
         patch("agents.thread_notes_agent.generate_thread_notes", new_callable=AsyncMock, return_value=mock_notes), \
         patch("agents.thread_notes_agent.post_thread_note", new_callable=AsyncMock, return_value={"ok": True, "ts": "1710000001.000000"}) as mock_post:

        res = await update_notes_for_thread(channel_id, thread_ts)
        assert res["ok"] is True
        assert mock_post.called

        called_kwargs = mock_post.call_args.kwargs
        assert "blocks" in called_kwargs
        blocks = called_kwargs["blocks"]
        assert len(blocks) == 2
        btn = blocks[1]["elements"][0]
        assert btn["action_id"] == "download_scribe_notes_txt"
        assert btn["value"] == f"{channel_id}:{thread_ts}"

        # Verify content was cached
        cached = get_thread_note_content(channel_id, thread_ts)
        assert cached is not None
        assert "Synthesis Notes" in cached


def test_slack_interactivity_download_scribe_action():
    client = TestClient(app)
    channel_id = "C_INTERACTIVE"
    thread_ts = "1710000000.000400"
    user_id = "U_CLICKER_99"
    note_content = "Strategic live discussion synthesis content for test"

    set_thread_note_content(channel_id, thread_ts, note_content)

    payload = {
        "type": "block_actions",
        "user": {"id": user_id},
        "actions": [
            {
                "action_id": "download_scribe_notes_txt",
                "value": f"{channel_id}:{thread_ts}",
            }
        ],
    }

    with patch("adapters.slack.interactivity.verify_slack_signature", return_value=True), \
         patch("adapters.slack.interactivity.post_ephemeral_to_slack", new_callable=AsyncMock) as mock_ephemeral, \
         patch("adapters.slack.interactivity.open_dm_channel", new_callable=AsyncMock, return_value="D_USER_DM") as mock_open_dm, \
         patch("adapters.slack.interactivity.upload_text_file_to_slack", new_callable=AsyncMock, return_value={"ok": True}) as mock_upload:

        resp = client.post("/slack/interactivity", json=payload)
        assert resp.status_code == 200

        # Background tasks run in TestClient
        assert mock_ephemeral.called
        assert mock_ephemeral.call_args.kwargs["channel_id"] == channel_id
        assert mock_ephemeral.call_args.kwargs["user_id"] == user_id
        assert mock_ephemeral.call_args.kwargs["thread_ts"] == thread_ts
        assert "direct message (DM)" in mock_ephemeral.call_args.kwargs["text"]

        assert mock_open_dm.called
        assert mock_open_dm.call_args[0][0] == user_id

        assert mock_upload.called
        assert mock_upload.call_args.kwargs["channel_id"] == "D_USER_DM"
        assert mock_upload.call_args.kwargs["content"] == note_content
        assert mock_upload.call_args.kwargs["filename"] == "scribe_notes_1710000000_000400.txt"


def test_slack_interactivity_download_scribe_fallback_to_payload():
    client = TestClient(app)
    channel_id = "C_FALLBACK"
    thread_ts = "1710000000.000500"
    user_id = "U_CLICKER_88"
    clear_thread_note(channel_id, thread_ts)

    payload = {
        "type": "block_actions",
        "user": {"id": user_id},
        "channel": {"id": channel_id},
        "message": {
            "thread_ts": thread_ts,
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "Fallback synthesis notes from message"},
                }
            ],
        },
        "actions": [
            {
                "action_id": "download_scribe_notes_txt",
                "value": "",  # Empty value triggers container/message fallback
            }
        ],
    }

    with patch("adapters.slack.interactivity.verify_slack_signature", return_value=True), \
         patch("adapters.slack.interactivity.post_ephemeral_to_slack", new_callable=AsyncMock) as mock_ephemeral, \
         patch("adapters.slack.interactivity.open_dm_channel", new_callable=AsyncMock, return_value="D_USER_88"), \
         patch("adapters.slack.interactivity.upload_text_file_to_slack", new_callable=AsyncMock, return_value={"ok": True}) as mock_upload:

        resp = client.post("/slack/interactivity", json=payload)
        assert resp.status_code == 200

        assert mock_ephemeral.called
        assert mock_upload.called
        assert mock_upload.call_args.kwargs["channel_id"] == "D_USER_88"
        assert mock_upload.call_args.kwargs["content"] == "Fallback synthesis notes from message"
