import json

from services.mcp_tools import list_documents, read_document, write_document


def test_mcp_write_and_read():
    rel_path = "tests/scratch_test.txt"
    content = "Hello from Native MCP Test!"

    write_res = write_document(rel_path, content)
    assert "success" in write_res

    read_res = read_document(rel_path)
    res_json = json.loads(read_res)
    assert res_json.get("content") == content

def test_mcp_list_directory():
    list_res = list_documents(".")
    res_json = json.loads(list_res)
    assert "contents" in res_json
    assert isinstance(res_json["contents"], list)

def test_mcp_directory_traversal_prevention():
    read_res = read_document("../../windows/system32/cmd.exe")
    res_json = json.loads(read_res)
    assert "error" in res_json
    assert "Access denied" in res_json["error"] or "does not exist" in res_json["error"]


def test_mcp_document_write_and_read():
    from services.mcp_tools import read_document, write_document

    rel_path = "scratch_doc.txt"
    content = "Hello from Sandboxed Resource Document!"

    write_res = write_document(rel_path, content)
    write_json = json.loads(write_res)
    assert write_json.get("success") is True
    assert write_json.get("document") == rel_path

    read_res = read_document(rel_path)
    res_json = json.loads(read_res)
    assert res_json.get("content") == content
    assert res_json.get("document") == rel_path


def test_mcp_list_documents():
    from services.mcp_tools import list_documents

    list_res = list_documents(".")
    res_json = json.loads(list_res)
    assert "contents" in res_json
    assert isinstance(res_json["contents"], list)
    names = [item["name"] for item in res_json["contents"]]
    assert ".gitkeep" in names or "scratch_doc.txt" in names


def test_mcp_document_traversal_prevention():
    from services.mcp_tools import read_document, write_document

    # Attempt to read outside agents_resources/
    read_res = read_document("../main.py")
    res_json = json.loads(read_res)
    assert "error" in res_json
    assert "Access denied" in res_json["error"]

    # Attempt to write outside agents_resources/
    write_res = write_document("../leak.txt", "danger")
    write_json = json.loads(write_res)
    assert "error" in write_json
    assert "Access denied" in write_json["error"]


def test_mcp_execute_tool_document_dispatch():
    from services.mcp_tools import execute_mcp_tool

    # Write document via execute_mcp_tool
    write_res = execute_mcp_tool(
        "write_document",
        {"rel_path": "dispatch_test.txt", "content": "Dispatched document content"}
    )
    assert "success" in json.loads(write_res)

    # Read document via execute_mcp_tool
    read_res = execute_mcp_tool("read_document", {"rel_path": "dispatch_test.txt"})
    assert json.loads(read_res).get("content") == "Dispatched document content"

    # List documents via execute_mcp_tool
    list_res = execute_mcp_tool("list_documents", {"rel_path": "."})
    assert "contents" in json.loads(list_res)

    # Clean up test artifacts
    from services.mcp_tools import AGENTS_RESOURCES_DIR
    for fname in ("scratch_doc.txt", "dispatch_test.txt"):
        fpath = AGENTS_RESOURCES_DIR / fname
        if fpath.exists():
            fpath.unlink()


def test_mcp_read_pdf_document():
    from unittest.mock import MagicMock, patch

    import pypdf

    from services.mcp_tools import AGENTS_RESOURCES_DIR, read_document

    pdf_filename = "test_sample.pdf"
    pdf_path = AGENTS_RESOURCES_DIR / pdf_filename

    # Create a real 2-page PDF
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_blank_page(width=200, height=200)
    with open(pdf_path, "wb") as f:
        writer.write(f)

    try:
        # Test real PDF reading with blank pages
        res = read_document(pdf_filename)
        data = json.loads(res)
        assert data.get("document") == pdf_filename
        assert data.get("pages") == 2
        assert "--- Page 1 ---" in data.get("content", "")
        assert "--- Page 2 ---" in data.get("content", "")

        # Test with mocked extracted text
        mock_page1 = MagicMock()
        mock_page1.extract_text.return_value = "Quarterly Financial Analysis"
        mock_page2 = MagicMock()
        mock_page2.extract_text.return_value = "Summary and Recommendations"
        mock_reader = MagicMock()
        mock_reader.pages = [mock_page1, mock_page2]

        with patch("pypdf.PdfReader", return_value=mock_reader):
            res_mocked = read_document(pdf_filename)
            data_mocked = json.loads(res_mocked)
            assert data_mocked.get("pages") == 2
            assert "Quarterly Financial Analysis" in data_mocked["content"]
            assert "Summary and Recommendations" in data_mocked["content"]
    finally:
        if pdf_path.exists():
            pdf_path.unlink()


def test_mcp_read_pdf_corrupted():
    from services.mcp_tools import AGENTS_RESOURCES_DIR, read_document

    bad_pdf = AGENTS_RESOURCES_DIR / "corrupt.pdf"
    bad_pdf.write_bytes(b"not a valid pdf data stream")

    try:
        res = read_document("corrupt.pdf")
        data = json.loads(res)
        assert "error" in data
        assert "Failed to parse PDF" in data["error"]
    finally:
        if bad_pdf.exists():
            bad_pdf.unlink()


def test_mcp_resource_path_normalization_and_prefix_stripping():
    from services.mcp_tools import AGENTS_RESOURCES_DIR, _resolve_resource_path

    # Naked filename
    p1 = _resolve_resource_path("doc.txt")
    assert p1 == (AGENTS_RESOURCES_DIR / "doc.txt").resolve()

    # With agents_resources/ prefix
    p2 = _resolve_resource_path("agents_resources/doc.txt")
    assert p2 == (AGENTS_RESOURCES_DIR / "doc.txt").resolve()

    # With agents_resources\ prefix
    p3 = _resolve_resource_path("agents_resources\\doc.txt")
    assert p3 == (AGENTS_RESOURCES_DIR / "doc.txt").resolve()

    # Bare "agents_resources"
    p4 = _resolve_resource_path("agents_resources")
    assert p4 == AGENTS_RESOURCES_DIR.resolve()


def test_mcp_read_document_search_in_resources_subfolder_and_case_insensitive():
    from services.mcp_tools import AGENTS_RESOURCES_DIR, read_document

    sub_dir = AGENTS_RESOURCES_DIR / "subfolder"
    sub_dir.mkdir(parents=True, exist_ok=True)
    nested_file = sub_dir / "Nested_Report.txt"
    nested_file.write_text("Found nested document content", encoding="utf-8")

    try:
        # 1. Search with exact name without folder
        res1 = json.loads(read_document("Nested_Report.txt"))
        assert "Found nested document content" in res1.get("content", "")

        # 2. Search case-insensitively
        res2 = json.loads(read_document("nested_report.txt"))
        assert "Found nested document content" in res2.get("content", "")

        # 3. Read with 'agents_resources/' prefix
        res3 = json.loads(read_document("agents_resources/subfolder/Nested_Report.txt"))
        assert "Found nested document content" in res3.get("content", "")
    finally:
        if nested_file.exists():
            nested_file.unlink()
        if sub_dir.exists():
            sub_dir.rmdir()


def test_mcp_read_file_fallback_to_resources():
    from services.mcp_tools import AGENTS_RESOURCES_DIR, read_document

    res_only_file = AGENTS_RESOURCES_DIR / "resource_only.txt"
    res_only_file.write_text("Hello from resources folder fallback", encoding="utf-8")

    try:
        # read_document should find resource_only.txt via fallback
        res = json.loads(read_document("resource_only.txt"))
        assert "content" in res
        assert "Hello from resources folder fallback" in res["content"]
    finally:
        if res_only_file.exists():
            res_only_file.unlink()


def test_mcp_write_document_normalizes_resources_prefix():
    from services.mcp_tools import AGENTS_RESOURCES_DIR, write_document

    test_file = AGENTS_RESOURCES_DIR / "normalized_write.txt"
    try:
        # Write specifying "agents_resources/normalized_write.txt"
        res = json.loads(write_document("agents_resources/normalized_write.txt", "Content via resources prefix"))
        assert res.get("success") is True
        assert test_file.exists()
        assert test_file.read_text(encoding="utf-8") == "Content via resources prefix"
    finally:
        if test_file.exists():
            test_file.unlink()


def test_mcp_write_file_blocks_protected_default_files():
    from services.mcp_tools import write_document

    targets = [
        ".env",
        ".env.local",
        ".git/hooks/pre-commit",
        "system_resources",
        "system_resources/agy/plan.md",
        "data/settings/agent_settings.json",
        "data/settings/app_settings.json",
    ]
    for target in targets:
        res = json.loads(write_document(target, "malicious_overwrite_attempt"))
        assert "error" in res, f"Expected error when writing to {target}"
        assert "Permission denied: Writing to protected path" in res["error"]


def test_mcp_write_file_blocks_custom_blacklist(monkeypatch, tmp_path):
    from orchestration.system_settings import system_settings_manager
    from services.mcp_tools import write_document

    monkeypatch.setattr(
        system_settings_manager,
        "get_workspace_write_blacklist_additional",
        lambda: ["custom_blocked_dir/*", "blacklisted.txt"],
    )

    # 1. Blocked custom paths
    res1 = json.loads(write_document("custom_blocked_dir/sample.txt", "content"))
    assert "error" in res1
    assert "Permission denied: Writing to protected path" in res1["error"]

    res2 = json.loads(write_document("blacklisted.txt", "content"))
    assert "error" in res2
    assert "Permission denied: Writing to protected path" in res2["error"]

    # 2. Allowed normal path
    allowed_file = "tests/test_write_allowed.tmp"
    res3 = json.loads(write_document(allowed_file, "valid content"))
    assert res3.get("success") is True
    from services.mcp_tools import WORKSPACE_DIR
    p = WORKSPACE_DIR / allowed_file
    if p.exists():
        p.unlink()


def test_mcp_write_document_blocks_blacklisted_path(monkeypatch):
    from orchestration.system_settings import system_settings_manager
    from services.mcp_tools import write_document

    monkeypatch.setattr(
        system_settings_manager,
        "get_workspace_write_blacklist_additional",
        lambda: ["agents_resources/secret_doc.txt"],
    )

    res = json.loads(write_document("secret_doc.txt", "confidential"))
    assert "error" in res
    assert "Permission denied: Writing to protected path" in res["error"]


def test_mcp_tool_dispatcher_registry():
    from services.mcp_tools import TOOL_DISPATCHER

    expected_tools = {
        "read_document",
        "write_document",
        "list_documents",
        "read_document",
        "write_document",
        "list_documents",
        "web_search",
        "fetch_web_page",
    }
    assert expected_tools.issubset(set(TOOL_DISPATCHER.keys()))
    for name, func in TOOL_DISPATCHER.items():
        assert callable(func), f"Dispatcher entry '{name}' must be callable"


def test_mcp_execute_tool_unknown_and_invalid_args(monkeypatch):
    from services.mcp_tools import TOOL_DISPATCHER, execute_mcp_tool

    # Unknown tool
    unknown_res = execute_mcp_tool("non_existent_tool_xyz", {"foo": "bar"})
    unknown_json = json.loads(unknown_res)
    assert "error" in unknown_json
    assert "Unknown tool: non_existent_tool_xyz" in unknown_json["error"]

    # Tool execution error when handler raises an exception
    def _failing_handler(args):
        raise RuntimeError("Boom")

    monkeypatch.setitem(TOOL_DISPATCHER, "faulty_tool", _failing_handler)
    error_res = execute_mcp_tool("faulty_tool", {})
    error_json = json.loads(error_res)
    assert "error" in error_json
    assert "Tool execution error: Boom" in error_json["error"]


def test_mcp_execute_tool_agent_authorization():
    from services.mcp_tools import execute_mcp_tool

    # Sceptic has no skills configured -> write_document must be denied
    denied_res = execute_mcp_tool(
        "write_document",
        {"rel_path": "agents_resources/unauthorized_test.txt", "content": "test"},
        agent_name="agent-10003",
    )
    denied_json = json.loads(denied_res)
    assert "error" in denied_json
    assert "Permission denied" in denied_json["error"]
    assert "agent-10003" in denied_json["error"]

    # System or empty agent_name must be allowed
    sys_res = execute_mcp_tool(
        "list_documents",
        {"rel_path": "."},
        agent_name="system",
    )
    sys_json = json.loads(sys_res)
    assert "contents" in sys_json

    # SecurityArchitect has document_reader skill -> list_documents must be allowed
    tech_res = execute_mcp_tool(
        "list_documents",
        {"rel_path": "."},
        agent_name="agent-10008",
    )
    tech_json = json.loads(tech_res)
    assert "contents" in tech_json


def test_mcp_system_resources_boundary_enforcement():
    from services.mcp_tools import read_document, write_document

    # Attempt to read inside system_resources via relative path traversal
    res_read = json.loads(read_document("../system_resources/agy/README.md"))
    assert "error" in res_read
    assert "Access denied" in res_read["error"]

    # Attempt to write inside system_resources via relative path traversal
    res_write = json.loads(write_document("../system_resources/tamper.txt", "evil"))
    assert "error" in res_write
    assert "Permission denied" in res_write["error"] or "Access denied" in res_write["error"]
