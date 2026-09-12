import json

from services.mcp_tools import list_directory, read_file, write_file


def test_mcp_write_and_read():
    rel_path = "tests/scratch_test.txt"
    content = "Hello from Native MCP Test!"

    write_res = write_file(rel_path, content)
    assert "success" in write_res

    read_res = read_file(rel_path)
    res_json = json.loads(read_res)
    assert res_json.get("content") == content

def test_mcp_list_directory():
    list_res = list_directory(".")
    res_json = json.loads(list_res)
    assert "contents" in res_json
    assert isinstance(res_json["contents"], list)

def test_mcp_directory_traversal_prevention():
    read_res = read_file("../../windows/system32/cmd.exe")
    res_json = json.loads(read_res)
    assert "error" in res_json
    assert "Access denied" in res_json["error"] or "does not exist" in res_json["error"]


def test_mcp_workspace_root_is_project_root():
    from pathlib import Path

    from services.mcp_tools import PROJECT_ROOT, _resolve_safe_path

    expected_root = Path(__file__).resolve().parent.parent
    assert PROJECT_ROOT == expected_root
    assert _resolve_safe_path(".") == expected_root

    # Can read services/mcp_tools.py from the project root
    read_res = read_file("services/mcp_tools.py")
    res_json = json.loads(read_res)
    assert "content" in res_json
    assert "def read_file" in res_json["content"]


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

    # Attempt to read outside resources/
    read_res = read_document("../main.py")
    res_json = json.loads(read_res)
    assert "error" in res_json
    assert "Access denied" in res_json["error"]

    # Attempt to write outside resources/
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
    from services.mcp_tools import RESOURCES_DIR
    for fname in ("scratch_doc.txt", "dispatch_test.txt"):
        fpath = RESOURCES_DIR / fname
        if fpath.exists():
            fpath.unlink()



