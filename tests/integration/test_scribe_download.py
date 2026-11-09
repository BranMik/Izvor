from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from agents.thread_notes_agent import (
    build_conclusion_note_blocks,
    build_scribe_note_blocks,
    clear_thread_note,
    get_thread_note_content,
    reset_thread_notes,
    set_thread_conclusion_content,
    set_thread_note_content,
    update_notes_for_thread,
)
from main import app
from services.slack import upload_text_file_to_slack


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


def test_build_scribe_note_blocks_splits_long_text_across_sections():
    # Text between 2,901 and 3,800 chars splits across section blocks without truncation
    para1 = "A" * 2000
    para2 = "B" * 1500
    long_text = f"{para1}\n\n{para2}"
    blocks = build_scribe_note_blocks(long_text, "C1", "123.456")
    assert len(blocks) == 3
    assert blocks[0]["type"] == "section"
    assert blocks[1]["type"] == "section"
    assert blocks[2]["type"] == "actions"
    assert len(blocks[0]["text"]["text"]) <= 2900
    assert len(blocks[1]["text"]["text"]) <= 2900
    assert "...[truncated]" not in blocks[0]["text"]["text"]
    assert "...[truncated]" not in blocks[1]["text"]["text"]


def test_build_scribe_note_blocks_truncates_text_exceeding_total_limit():
    very_long_text = "A" * 4500
    blocks = build_scribe_note_blocks(very_long_text, "C1", "123.456")
    assert len(blocks) >= 2
    assert blocks[-1]["type"] == "actions"
    for sec in blocks[:-1]:
        assert sec["type"] == "section"
        assert len(sec["text"]["text"]) <= 2900
    combined_text = "".join(sec["text"]["text"] for sec in blocks[:-1])
    assert len(combined_text) <= 3800
    assert "...[truncated]" in combined_text


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
        channel_id="mock_dm",
        content="Hello file content",
        filename="test_notes.txt",
        title="Test Notes Title",
    )
    assert res["ok"] is True
    assert res["mock"] is True
    assert res["file"]["name"] == "test_notes.txt"
    assert res["file"]["content"] == "Hello file content"


@pytest.mark.anyio
async def test_update_notes_for_thread_passes_blocks():
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


@pytest.mark.anyio
async def test_update_notes_for_thread_converts_markdown_to_mrkdwn_for_slack_only():
    """Raw markdown from the scribe must be posted to Slack as mrkdwn, while the cached content stays raw markdown."""
    channel_id = "C_SCRIBE_MRKDWN"
    thread_ts = "1710000000.000600"
    clear_thread_note(channel_id, thread_ts)

    mock_history = [{"role": "user", "content": "Discuss DB choice"}]
    mock_notes = (
        "### **Live Strategic Synthesis**\n- **Key point**: PostgreSQL.\n---",
        {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    )

    with patch("agents.thread_notes_agent.fetch_thread_history", new_callable=AsyncMock, return_value=mock_history), \
         patch("agents.thread_notes_agent.generate_thread_notes", new_callable=AsyncMock, return_value=mock_notes), \
         patch("agents.thread_notes_agent.post_thread_note", new_callable=AsyncMock, return_value={"ok": True, "ts": "1710000002.000000"}) as mock_post:

        res = await update_notes_for_thread(channel_id, thread_ts)
        assert res["ok"] is True

        called_kwargs = mock_post.call_args.kwargs
        posted_text = called_kwargs["text"]
        assert "*Live Strategic Synthesis*" in posted_text
        assert "- *Key point*: PostgreSQL." in posted_text
        assert "──────────" in posted_text
        assert "###" not in posted_text
        assert "**" not in posted_text

        block_text = called_kwargs["blocks"][0]["text"]["text"]
        assert "*Live Strategic Synthesis*" in block_text
        assert "###" not in block_text

        # Cached content keeps raw markdown for .txt download
        cached = get_thread_note_content(channel_id, thread_ts)
        assert cached is not None
        assert "### **Live Strategic Synthesis**" in cached

    clear_thread_note(channel_id, thread_ts)


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
         patch("adapters.slack.actions.workflow_actions.post_ephemeral_to_slack", new_callable=AsyncMock) as mock_ephemeral, \
         patch("adapters.slack.actions.workflow_actions.open_dm_channel", new_callable=AsyncMock, return_value="D_USER_DM") as mock_open_dm, \
         patch("adapters.slack.actions.workflow_actions.upload_text_file_to_slack", new_callable=AsyncMock, return_value={"ok": True}) as mock_upload:

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
         patch("adapters.slack.actions.workflow_actions.post_ephemeral_to_slack", new_callable=AsyncMock) as mock_ephemeral, \
         patch("adapters.slack.actions.workflow_actions.open_dm_channel", new_callable=AsyncMock, return_value="D_USER_88"), \
         patch("adapters.slack.actions.workflow_actions.upload_text_file_to_slack", new_callable=AsyncMock, return_value={"ok": True}) as mock_upload:

        resp = client.post("/slack/interactivity", json=payload)
        assert resp.status_code == 200

        assert mock_ephemeral.called
        assert mock_upload.called
        assert mock_upload.call_args.kwargs["channel_id"] == "D_USER_88"
        assert mock_upload.call_args.kwargs["content"] == "Fallback synthesis notes from message"


def test_build_conclusion_note_blocks():
    text = "*Problem Statement*\nDefine DB.\n\n*Consensus Decisions*\nPostgreSQL wins."
    channel_id = "C_TEST_CONC"
    thread_ts = "1710000000.000999"

    blocks = build_conclusion_note_blocks(text, channel_id, thread_ts)

    assert len(blocks) == 2
    assert blocks[0]["type"] == "section"
    assert blocks[0]["text"]["text"] == text

    assert blocks[1]["type"] == "actions"
    assert blocks[1]["block_id"] == "conclusion_actions_block"
    btn = blocks[1]["elements"][0]
    assert btn["action_id"] == "download_conclusion_notes_txt"
    assert btn["value"] == f"{channel_id}:{thread_ts}"
    assert "Download Conclusion" in btn["text"]["text"]


def test_slack_interactivity_download_conclusion_action():
    client = TestClient(app)
    channel_id = "C_INTERACTIVE_CONC"
    thread_ts = "1710000000.000600"
    user_id = "U_CLICKER_77"
    conclusion_content = "*Problem Statement*\nProblem statement.\n\n*Consensus Decisions*\nDialectical consensus."

    set_thread_conclusion_content(channel_id, thread_ts, conclusion_content)

    payload = {
        "type": "block_actions",
        "user": {"id": user_id},
        "actions": [
            {
                "action_id": "download_conclusion_notes_txt",
                "value": f"{channel_id}:{thread_ts}",
            }
        ],
    }

    with patch("adapters.slack.interactivity.verify_slack_signature", return_value=True), \
         patch("adapters.slack.actions.workflow_actions.post_ephemeral_to_slack", new_callable=AsyncMock) as mock_ephemeral, \
         patch("adapters.slack.actions.workflow_actions.open_dm_channel", new_callable=AsyncMock, return_value="D_USER_CONC_DM") as mock_open_dm, \
         patch("adapters.slack.actions.workflow_actions.upload_text_file_to_slack", new_callable=AsyncMock, return_value={"ok": True}) as mock_upload:

        resp = client.post("/slack/interactivity", json=payload)
        assert resp.status_code == 200

        assert mock_ephemeral.called
        assert mock_open_dm.called
        assert mock_upload.called
        assert mock_upload.call_args.kwargs["channel_id"] == "D_USER_CONC_DM"
        assert mock_upload.call_args.kwargs["content"] == conclusion_content
        assert mock_upload.call_args.kwargs["filename"] == "conclusion_notes_1710000000_000600.txt"


def test_slack_interactivity_download_scribe_dm_open_failure():
    """When opening the DM channel fails (e.g. missing im:write scope), no upload occurs and an error is sent."""
    client = TestClient(app)
    channel_id = "C_FAIL_DM"
    thread_ts = "1710000000.000700"
    user_id = "U_FAIL_USER"
    set_thread_note_content(channel_id, thread_ts, "Sample notes for failure test")

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
         patch("adapters.slack.actions.workflow_actions.post_ephemeral_to_slack", new_callable=AsyncMock) as mock_ephemeral, \
         patch("adapters.slack.actions.workflow_actions.open_dm_channel", new_callable=AsyncMock, return_value=None) as mock_open_dm, \
         patch("adapters.slack.actions.workflow_actions.upload_text_file_to_slack", new_callable=AsyncMock) as mock_upload:

        resp = client.post("/slack/interactivity", json=payload)
        assert resp.status_code == 200

        assert mock_open_dm.called
        assert not mock_upload.called
        assert mock_ephemeral.called
        ephemeral_text = mock_ephemeral.call_args.kwargs["text"]
        assert "Could not open a direct message channel" in ephemeral_text
        assert "im:write" in ephemeral_text


def test_slack_interactivity_download_scribe_upload_failure():
    """When file upload fails, an ephemeral error is delivered to the user with the failure reason."""
    client = TestClient(app)
    channel_id = "C_FAIL_UPLOAD"
    thread_ts = "1710000000.000800"
    user_id = "U_FAIL_UPLOAD_USER"
    set_thread_note_content(channel_id, thread_ts, "Sample notes for upload error test")

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
         patch("adapters.slack.actions.workflow_actions.post_ephemeral_to_slack", new_callable=AsyncMock) as mock_ephemeral, \
         patch("adapters.slack.actions.workflow_actions.open_dm_channel", new_callable=AsyncMock, return_value="D_VALID_DM") as mock_open_dm, \
         patch("adapters.slack.actions.workflow_actions.upload_text_file_to_slack", new_callable=AsyncMock, return_value={"ok": False, "error": "file_upload_error"}) as mock_upload:

        resp = client.post("/slack/interactivity", json=payload)
        assert resp.status_code == 200

        assert mock_open_dm.called
        assert mock_upload.called
        assert mock_ephemeral.called
        ephemeral_text = mock_ephemeral.call_args.kwargs["text"]
        assert "Failed to deliver notes file to your direct message" in ephemeral_text
        assert "file_upload_error" in ephemeral_text

