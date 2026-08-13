import json
import pytest
from mcp_tools import list_directory, read_file, write_file, execute_mcp_tool

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
