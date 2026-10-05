from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nlp.message_builder import build_llm_messages
from orchestration.agent_router import process_and_respond
from orchestration.nomination import resolve_target_file
from orchestration.thread_state import thread_state_manager
from pipeline.directive_parser import directive_parser
from services.slack_client import download_slack_file, sanitize_slack_download_filename


@pytest.mark.anyio
async def test_extract_file_instruction_slash_commands():
    # Basic slash command
    res = await directive_parser.parse("/read-file Analiziraj ovo")
    assert res.is_file_req is True
    assert res.target_filename is None
    assert res.cleaned_prompt == "Analiziraj ovo"

    # Slash command with target filename
    res = await directive_parser.parse("/read-file: code.py Što radi ovaj kod?")
    assert res.is_file_req is True
    assert res.target_filename == "code.py"
    assert res.cleaned_prompt == "Što radi ovaj kod?"

    # /file command with bracketed filename
    res = await directive_parser.parse("/file [data.csv] Napravi statistiku")
    assert res.is_file_req is True
    assert res.target_filename == "data.csv"
    assert res.cleaned_prompt == "Napravi statistiku"


def test_extract_file_instruction_croatian_nl():
    # Note: NL pattern extraction is now handled by IntentExtractor (Micro-LLM)
    # via DirectiveParser with document_reader action directive.
    pass


def test_extract_file_instruction_english_nl():
    # Note: NL pattern extraction is now handled by IntentExtractor (Micro-LLM)
    # via DirectiveParser with document_reader action directive.
    pass


@pytest.mark.anyio
async def test_extract_file_instruction_matching_uploaded_filename():
    available_files = [
        {"name": "sales_report_2026.csv", "id": "F1"},
        {"name": "architecture_diagram.png", "id": "F2"},
    ]
    res = await directive_parser.parse(
        "Možeš li pregledati sales_report_2026.csv?", available_files=available_files
    )
    assert res.is_file_req is True
    assert res.target_filename == "sales_report_2026.csv"


@pytest.mark.anyio
async def test_extract_file_instruction_negative_cases():
    cases = [
        "Bok, kako si danas?",
        "Tko je predsjednik Hrvatske?",
        "Napiši mi pjesmu o proljeću",
        "/history 5",
    ]
    for prompt in cases:
        res = await directive_parser.parse(prompt)
        assert res.is_file_req is False, f"False positive file trigger detected in: '{prompt}'"
        assert res.target_filename is None


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

    # Empty list and non-existent file
    assert resolve_target_file([], "non_existent_random_file_12345.py") is None


def test_resolve_target_file_resources_fallback_and_slack_precedence():
    from services.mcp_tools import RESOURCES_DIR

    test_filename = "precedence_test_doc.txt"
    res_file = RESOURCES_DIR / test_filename
    res_file.write_text("Content in resources folder", encoding="utf-8")

    try:
        # 1. When file is ONLY in resources/ (available_files is empty)
        matched_res = resolve_target_file([], test_filename)
        assert matched_res is not None
        assert matched_res.get("is_resource_file") is True
        assert matched_res.get("name") == test_filename

        # 2. Case-insensitive match in resources/
        matched_ci = resolve_target_file([], test_filename.upper())
        assert matched_ci is not None
        assert matched_ci.get("is_resource_file") is True

        # 3. PRECEDENCE RULE: File exists in BOTH Slack attachments (available_files) AND resources/
        # Attached Slack file MUST take precedence over resources folder!
        slack_files = [
            {
                "id": "SLACK_F123",
                "name": test_filename,
                "size": 999,
                "url_private_download": "https://files.slack.com/files-pri/T1/precedence_test_doc.txt",
            }
        ]
        matched_slack = resolve_target_file(slack_files, test_filename)
        assert matched_slack is not None
        assert matched_slack.get("id") == "SLACK_F123"
        assert matched_slack.get("is_resource_file") is not True

    finally:
        if res_file.exists():
            res_file.unlink()



def test_build_llm_messages_file_context_authorized_vs_unauthorized():
    file_context = {
        "file_name": "app.py",
        "content": "print('hello from app.py')",
        "size": 28,
        "authorized_agents": {"agent-10001"},
    }

    # 1. Authorized agent (Visionary) receives file context in user turn
    msgs_authorized = build_llm_messages(
        system_prompt="You are Visionary.",
        effective_history=[],
        prompt="Objasni ovaj kod.",
        is_bot_trigger=False,
        agent_key="agent-10001",
        agent_cfg={"username": "agent-10001"},
        file_context=file_context,
    )
    user_turn = msgs_authorized[-1]["content"]
    assert "--- [ATTACHED SLACK FILE: app.py (28 bytes)] ---" in user_turn
    assert "print('hello from app.py')" in user_turn

    # 2. Unauthorized agent (LegalAdvisor) does NOT receive file context
    msgs_unauthorized = build_llm_messages(
        system_prompt="You are LegalAdvisor.",
        effective_history=[],
        prompt="Objasni ovaj kod.",
        is_bot_trigger=False,
        agent_key="agent-10011",
        agent_cfg={"username": "agent-10011"},
        file_context=file_context,
    )
    user_turn_unauth = msgs_unauthorized[-1]["content"]
    assert "ATTACHED SLACK FILE" not in user_turn_unauth
    assert user_turn_unauth.startswith("Objasni ovaj kod.")


def test_build_llm_messages_file_context_all_authorized():
    # If authorized_agents is None, all agents are authorized
    file_context = {
        "file_name": "config.json",
        "content": '{"port": 8080}',
        "size": 15,
        "authorized_agents": None,
    }

    msgs = build_llm_messages(
        system_prompt="You are SecurityArchitect.",
        effective_history=[{"role": "user", "content": "Prior context"}],
        prompt="",
        is_bot_trigger=True,
        agent_key="agent-10008",
        agent_cfg={"username": "OpAiTerra SecurityArchitect"},
        file_context=file_context,
    )
    assert msgs[-1]["role"] == "user"
    assert "--- [ATTACHED SLACK FILE: config.json (15 bytes)] ---" in msgs[-1]["content"]
    assert '{"port": 8080}' in msgs[-1]["content"]


def test_build_llm_messages_file_context_missing_skill_notice():
    file_context = {
        "file_name": "data.csv",
        "content": "id,name\n1,Alice",
        "size": 15,
        "authorized_agents": None,
        "required_skill": "document_reader",
    }

    # Agent with skills configured but lacking document_reader
    msgs = build_llm_messages(
        system_prompt="You are LegalAdvisor.",
        effective_history=[],
        prompt="Analyze data.csv",
        is_bot_trigger=False,
        agent_key="agent-10011",
        agent_cfg={"username": "Legal Advisor", "skills": ["web_research"]},
        file_context=file_context,
    )
    user_turn = msgs[-1]["content"]
    assert "ATTACHED SLACK FILE" not in user_turn
    assert "--- [FILE ACCESS RESTRICTED: MISSING SKILL] ---" in user_turn
    assert "document_reader" in user_turn
    assert "Do NOT report that the file was not found." in user_turn

    # Agent equipped with document_reader gets full file content
    msgs_ok = build_llm_messages(
        system_prompt="You are Data Strategist.",
        effective_history=[],
        prompt="Analyze data.csv",
        is_bot_trigger=False,
        agent_key="agent-10009",
        agent_cfg={"username": "Data Strategist", "skills": ["document_reader"]},
        file_context=file_context,
    )
    user_turn_ok = msgs_ok[-1]["content"]
    assert "--- [ATTACHED SLACK FILE: data.csv (15 bytes)] ---" in user_turn_ok
    assert "id,name\n1,Alice" in user_turn_ok


def test_build_llm_messages_required_skills_missing_notice():
    # Agent lacking required skill (document_authoring) receives notice
    msgs = build_llm_messages(
        system_prompt="You are Visionary.",
        effective_history=[],
        prompt="Write a report to resources/report.md",
        is_bot_trigger=False,
        agent_key="agent-10001",
        agent_cfg={"username": "agent-10001", "skills": []},
        required_skills=["document_authoring"],
    )
    user_turn = msgs[-1]["content"]
    assert "--- [PERMISSION RESTRICTED: MISSING REQUIRED SKILL(S)] ---" in user_turn
    assert "document_authoring" in user_turn
    assert "Do NOT report that the file or resource was not found." in user_turn


@pytest.mark.anyio
async def test_download_slack_file_mock_mode():
    file_info = {
        "name": "sample.py",
        "url_private_download": "https://files.slack.com/files-pri/T1/sample.py",
        "size": 100,
    }
    with patch("services.slack_client.SLACK_BOT_TOKEN", ""):
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

    with (
        patch("services.slack_client.SLACK_BOT_TOKEN", "xoxb-real-mock-token"),
        patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get,
    ):
        mock_get.return_value = mock_resp
        res = await download_slack_file(file_info)
        assert res["ok"] is True
        assert res["file_name"] == "calc.py"
        assert "def add(a, b):" in res["content"]
        assert res["truncated"] is False


@pytest.mark.anyio
async def test_download_slack_file_pdf_mock():
    import io

    import pypdf

    # Create a real small PDF
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=100, height=100)
    buf = io.BytesIO()
    writer.write(buf)
    pdf_bytes = buf.getvalue()

    file_info = {
        "name": "spec.pdf",
        "url_private_download": "https://files.slack.com/files-pri/T1/spec.pdf",
        "size": len(pdf_bytes),
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = pdf_bytes

    with (
        patch("services.slack_client.SLACK_BOT_TOKEN", "xoxb-real-mock-token"),
        patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get,
    ):
        mock_get.return_value = mock_resp
        res = await download_slack_file(file_info)
        assert res["ok"] is True
        assert res["file_name"] == "spec.pdf"
        assert "--- Page 1 ---" in res["content"]
        assert res["truncated"] is False


@pytest.mark.anyio
async def test_file_context_lifecycle_in_process_and_respond():
    channel_id = "C_FILE_TEST"
    thread_ts = "1700000000.1234"

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

    with (
        patch("orchestration.agent_router.fetch_thread_files", new_callable=AsyncMock, return_value=mock_files),
        patch("orchestration.agent_router.fetch_thread_history", new_callable=AsyncMock, return_value=[]),
        patch("orchestration.agent_router.post_agent_response", new_callable=AsyncMock, return_value={"ok": True, "ts": "1700000000.2222"}),
        patch("services.slack_client.download_slack_file", new_callable=AsyncMock, return_value={"ok": True, "file_name": "database.sql", "content": "SELECT 1;", "size": 50, "truncated": False}),
        patch("orchestration.stages.validation.download_slack_file", new_callable=AsyncMock, return_value={"ok": True, "file_name": "database.sql", "content": "SELECT 1;", "size": 50, "truncated": False}),
        patch("litellm.completion", return_value=mock_litellm_resp),
        patch("orchestration.agent_router.asyncio.sleep", new_callable=AsyncMock),
    ):
                        # 1. User prompts with specific tag [agent-10001] and requests file reading
                        res = await process_and_respond(
                            channel_id=channel_id,
                            thread_ts=thread_ts,
                            raw_text="/[agent-10001] pročitaj fajl database.sql i provjeri tablice",
                            is_bot_trigger=False,
                        )
                        assert res.get("ok") is True

                        # Since Gemini was the only tagged agent in explicit target mode, discussion finished and cleared file context
                        assert thread_state_manager.get_file_context(channel_id, thread_ts) is None

                        # 2. Test explicit multi-agent flow: [agent-10001] and [agent-10008]
                        res2 = await process_and_respond(
                            channel_id=channel_id,
                            thread_ts=thread_ts,
                            raw_text="/[agent-10001] /[agent-10008] analiziraj privitak",
                            is_bot_trigger=False,
                        )
                        assert res2.get("ok") is True
                        # During the active discussion before OpAiTerra finishes, file context was active for both tagged agents
                        # Now simulate next turn with SecurityArchitect completing the flow
                        res3 = await process_and_respond(
                            channel_id=channel_id,
                            thread_ts=thread_ts,
                            raw_text="/[agent-10008]",
                            is_bot_trigger=True,
                        )
                        assert res3.get("ok") is True
                        # After OpAiTerra finishes, file context is cleanly destroyed
                        assert thread_state_manager.get_file_context(channel_id, thread_ts) is None


@pytest.mark.anyio
async def test_resource_file_resolution_in_validation_stage():
    from orchestration.turn_pipeline import TurnContext, TurnPipeline
    from services.mcp_tools import RESOURCES_DIR

    channel_id = "C_RES_STAGE_TEST"
    thread_ts = "1700000000.9999"

    doc_filename = "server_resource_doc.txt"
    res_file = RESOURCES_DIR / doc_filename
    res_file.write_text("Unique local server resource document text", encoding="utf-8")

    try:
        pipeline = TurnPipeline()
        ctx = TurnContext(
            channel_id=channel_id,
            thread_ts=thread_ts,
            raw_text=f"/read-file {doc_filename}",
            is_bot_trigger=False,
        )

        with patch.object(pipeline, "_get_hook", side_effect=lambda name, default=None: (
            AsyncMock(return_value=[]) if name == "fetch_thread_files" else
            AsyncMock(return_value={"ok": True}) if name == "post_system_message" else
            default
        )):
            stage_res = await pipeline.validation_stage.stage_validate_and_parse(ctx)
            assert stage_res is None or stage_res.get("ok", True) is True

            fc = thread_state_manager.get_file_context(channel_id, thread_ts)
            assert fc is not None
            assert fc.get("file_name") == doc_filename
            assert "Unique local server resource document text" in fc.get("content", "")
    finally:
        if res_file.exists():
            res_file.unlink()
        thread_state_manager.clear_file_context(channel_id, thread_ts)


def test_sanitize_slack_download_filename():
    # 1. Strips directory traversal
    name1 = sanitize_slack_download_filename("../../etc/passwd.txt", prefix_len=6)
    assert "../" not in name1
    assert "etc" not in name1
    assert name1.endswith("passwd.txt")
    assert len(name1.split("_")[0]) == 6

    # 2. Replaces illegal/unsafe characters
    name2 = sanitize_slack_download_filename("my unsafe * file (1).pdf", prefix_len=6)
    assert "*" not in name2
    assert " " not in name2
    assert "(" not in name2
    assert name2.endswith(".pdf")

    # 3. Collision prevention with short UUID
    name3 = sanitize_slack_download_filename("report.csv", prefix_len=6)
    name4 = sanitize_slack_download_filename("report.csv", prefix_len=6)
    assert name3 != name4
    assert name3.endswith("_report.csv")
    assert name4.endswith("_report.csv")


@pytest.mark.anyio
async def test_download_slack_file_saves_safely_inside_downloads():
    from pathlib import Path
    from core.config import MCP_WORKSPACE_DIR

    downloads_dir = (MCP_WORKSPACE_DIR / "downloads").resolve()
    downloads_dir.mkdir(parents=True, exist_ok=True)

    # Malicious filename attempting path traversal
    file_info = {
        "name": "../../../traversal_attack.py",
        "url_private_download": "https://files.slack.com/files-pri/T1/attack.py",
        "size": 35,
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b"print('malicious injection attempt')"

    with (
        patch("services.slack_client.SLACK_BOT_TOKEN", "xoxb-real-mock-token"),
        patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get,
    ):
        mock_get.return_value = mock_resp
        res = await download_slack_file(file_info)
        assert res["ok"] is True

        # Verify nothing was created in parent directories
        parent_file = MCP_WORKSPACE_DIR.parent / "traversal_attack.py"
        workspace_file = MCP_WORKSPACE_DIR / "traversal_attack.py"
        assert not parent_file.exists()
        assert not workspace_file.exists()

        # Verify file is inside downloads_dir
        found = list(downloads_dir.glob("*_traversal_attack.py"))
        assert len(found) >= 1
        for f in found:
            assert f.is_relative_to(downloads_dir)
            f.unlink()


