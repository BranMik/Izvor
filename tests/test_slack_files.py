import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from agent_router import (
    extract_file_instruction,
    resolve_target_file,
    get_thread_file_context,
    set_thread_file_context,
    clear_thread_file_context,
    build_llm_messages,
    THREAD_ACTIVE_FILE_CONTEXT,
    process_and_respond,
)
from slack_client import fetch_thread_files, download_slack_file


def test_extract_file_instruction_slash_commands():
    # Basic slash command
    req, target, cleaned = extract_file_instruction("/read-file Analiziraj ovo")
    assert req is True
    assert target is None
    assert cleaned == "Analiziraj ovo"

    # Slash command with target filename
    req, target, cleaned = extract_file_instruction("/read-file: code.py Što radi ovaj kod?")
    assert req is True
    assert target == "code.py"
    assert cleaned == "Što radi ovaj kod?"

    # /file command with bracketed filename
    req, target, cleaned = extract_file_instruction("/file [data.csv] Napravi statistiku")
    assert req is True
    assert target == "data.csv"
    assert cleaned == "Napravi statistiku"


def test_extract_file_instruction_croatian_nl():
    # Various Croatian natural language phrases
    cases = [
        "Molim te pročitaj fajl koji sam poslao",
        "Pogledaj datoteku i reci mi što misliš",
        "Analiziraj privitak i pronađi bugove",
        "Provjeri kod u privitku",
        "U privitku se nalazi specifikacija projekta",
        "Uploadao sam fajl s podacima",
        "Poslao sam fajl na pregled",
    ]
    for prompt in cases:
        req, target, cleaned = extract_file_instruction(prompt)
        assert req is True, f"Failed to detect Croatian trigger in: '{prompt}'"


def test_extract_file_instruction_english_nl():
    cases = [
        "Please read the attached file and summarize it",
        "Check the file for any syntax errors",
        "Analyze attachment and provide feedback",
        "Inspect the uploaded file",
        "Look at the file and explain the logic",
        "The requirements are in the attachment",
    ]
    for prompt in cases:
        req, target, cleaned = extract_file_instruction(prompt)
        assert req is True, f"Failed to detect English trigger in: '{prompt}'"


def test_extract_file_instruction_matching_uploaded_filename():
    available_files = [
        {"name": "sales_report_2026.csv", "id": "F1"},
        {"name": "architecture_diagram.png", "id": "F2"},
    ]
    req, target, cleaned = extract_file_instruction(
        "Možeš li pregledati sales_report_2026.csv?", available_files=available_files
    )
    assert req is True
    assert target == "sales_report_2026.csv"


def test_extract_file_instruction_negative_cases():
    cases = [
        "Bok, kako si danas?",
        "Tko je predsjednik Hrvatske?",
        "Napiši mi pjesmu o proljeću",
        "/history 5",
    ]
    for prompt in cases:
        req, target, cleaned = extract_file_instruction(prompt)
        assert req is False, f"False positive file trigger detected in: '{prompt}'"
        assert target is None


def test_resolve_target_file():
    files = [
        {"name": "old_data.txt", "id": "F1"},
        {"name": "script.py", "id": "F2"},
        {"name": "latest_report.pdf", "id": "F3"},
    ]
    # Match exact name
    res = resolve_target_file(files, "script.py")
    assert res["id"] == "F2"

    # Match case-insensitive
    res = resolve_target_file(files, "SCRIPT.PY")
    assert res["id"] == "F2"

    # Match partial
    res = resolve_target_file(files, "latest")
    assert res["id"] == "F3"

    # Default to latest if no filename provided
    res = resolve_target_file(files, None)
    assert res["id"] == "F3"

    # Empty list
    assert resolve_target_file([], "test.py") is None


def test_build_llm_messages_file_context_authorized_vs_unauthorized():
    file_context = {
        "file_name": "app.py",
        "content": "print('hello from app.py')",
        "size": 28,
        "authorized_agents": {"Gemini_Visionary"},
    }

    # 1. Authorized agent (Gemini_Visionary) receives file context in user turn
    msgs_authorized = build_llm_messages(
        system_prompt="You are Visionary.",
        effective_history=[],
        prompt="Objasni ovaj kod.",
        is_bot_trigger=False,
        agent_key="Gemini_Visionary",
        agent_cfg={"username": "Gemini Visionary"},
        file_context=file_context,
    )
    user_turn = msgs_authorized[-1]["content"]
    assert "--- [ATTACHED SLACK FILE: app.py (28 bytes)] ---" in user_turn
    assert "print('hello from app.py')" in user_turn

    # 2. Unauthorized agent (MistLar_TechLead) does NOT receive file context
    msgs_unauthorized = build_llm_messages(
        system_prompt="You are TechLead.",
        effective_history=[],
        prompt="Objasni ovaj kod.",
        is_bot_trigger=False,
        agent_key="MistLar_TechLead",
        agent_cfg={"username": "MistLar TechLead"},
        file_context=file_context,
    )
    user_turn_unauth = msgs_unauthorized[-1]["content"]
    assert "ATTACHED SLACK FILE" not in user_turn_unauth
    assert user_turn_unauth == "Objasni ovaj kod."


def test_build_llm_messages_file_context_all_authorized():
    # If authorized_agents is None, all agents are authorized
    file_context = {
        "file_name": "config.json",
        "content": '{"port": 8080}',
        "size": 15,
        "authorized_agents": None,
    }

    msgs = build_llm_messages(
        system_prompt="You are Architect.",
        effective_history=[{"role": "user", "content": "Prior context"}],
        prompt="",
        is_bot_trigger=True,
        agent_key="OpAiTerra_Architect",
        agent_cfg={"username": "OpAiTerra Architect"},
        file_context=file_context,
    )
    assert msgs[-1]["role"] == "user"
    assert "--- [ATTACHED SLACK FILE: config.json (15 bytes)] ---" in msgs[-1]["content"]
    assert '{"port": 8080}' in msgs[-1]["content"]


@pytest.mark.anyio
async def test_download_slack_file_mock_mode():
    file_info = {
        "name": "sample.py",
        "url_private_download": "https://files.slack.com/files-pri/T1/sample.py",
        "size": 100,
    }
    with patch("slack_client.SLACK_BOT_TOKEN", ""):
        res = await download_slack_file(file_info)
        assert res["ok"] is True
        assert res["file_name"] == "sample.py"
        assert res["size"] == 100
        assert "Mock file content" in res["content"]


@pytest.mark.anyio
async def test_download_slack_file_http_mock():
    file_info = {
        "name": "calc.py",
        "url_private_download": "https://files.slack.com/files-pri/T1/calc.py",
        "size": 45,
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b"def add(a, b):\n    return a + b\n"

    with patch("slack_client.SLACK_BOT_TOKEN", "xoxb-real-mock-token"):
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp
            res = await download_slack_file(file_info)
            assert res["ok"] is True
            assert res["file_name"] == "calc.py"
            assert "def add(a, b):" in res["content"]
            assert res["truncated"] is False


@pytest.mark.anyio
async def test_file_context_lifecycle_in_process_and_respond():
    channel_id = "C_FILE_TEST"
    thread_ts = "1700000000.1234"
    thread_key = (channel_id, thread_ts)

    # Mock thread files in Slack
    mock_files = [
        {
            "id": "F100",
            "name": "database.sql",
            "size": 50,
            "url_private_download": "https://files.slack.com/files-pri/T1/database.sql",
        }
    ]

    mock_litellm_resp = MagicMock()
    mock_litellm_resp.choices = [
        MagicMock(message=MagicMock(content="Database analysis done.", tool_calls=None))
    ]
    mock_litellm_resp.usage = {"prompt_tokens": 15, "completion_tokens": 10}

    with patch("agent_router.fetch_thread_files", new_callable=AsyncMock, return_value=mock_files):
        with patch("agent_router.fetch_thread_history", new_callable=AsyncMock, return_value=[]):
            with patch("agent_router.post_agent_response", new_callable=AsyncMock, return_value={"ok": True, "ts": "1700000000.2222"}):
                with patch("litellm.completion", return_value=mock_litellm_resp):
                    with patch("agent_router.asyncio.sleep", new_callable=AsyncMock):
                        # 1. User prompts with specific tag [Gemini_Visionary] and requests file reading
                        res = await process_and_respond(
                            channel_id=channel_id,
                            thread_ts=thread_ts,
                            raw_text="[Gemini_Visionary] pročitaj fajl database.sql i provjeri tablice",
                            is_bot_trigger=False,
                        )
                        assert res.get("ok") is True

                        # Since Gemini was the only tagged agent in explicit target mode, discussion finished and cleared file context
                        assert get_thread_file_context(channel_id, thread_ts) is None

                        # 2. Test explicit multi-agent flow: [Gemini_Visionary] and [OpAiTerra_Architect]
                        res2 = await process_and_respond(
                            channel_id=channel_id,
                            thread_ts=thread_ts,
                            raw_text="[Gemini_Visionary] [OpAiTerra_Architect] analiziraj privitak",
                            is_bot_trigger=False,
                        )
                        assert res2.get("ok") is True
                        # During the active discussion before OpAiTerra finishes, file context was active for both tagged agents
                        # Now simulate next turn with OpAiTerra_Architect completing the flow
                        res3 = await process_and_respond(
                            channel_id=channel_id,
                            thread_ts=thread_ts,
                            raw_text="[OpAiTerra_Architect]",
                            is_bot_trigger=True,
                        )
                        assert res3.get("ok") is True
                        # After OpAiTerra finishes, file context is cleanly destroyed
                        assert get_thread_file_context(channel_id, thread_ts) is None
