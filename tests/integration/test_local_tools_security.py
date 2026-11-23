import json
from pathlib import Path

import pytest

from core.domain import SYSTEM_PRINCIPAL, ToolPrincipal
from services.local_tools import (
    is_agent_authorized_for_tool,
    validate_resource_filename,
    write_document,
)


def test_validate_resource_filename_security_boundaries():
    # Windows reserved device names
    for bad_device in ["CON.txt", "prn.pdf", "aux", "NUL", "com1.log", "LPT9"]:
        with pytest.raises(ValueError) as exc:
            validate_resource_filename(bad_device)
        assert "Reserved device name" in str(exc.value)

    # NTFS Alternate Data Streams and colons
    with pytest.raises(ValueError) as exc:
        validate_resource_filename("report.txt:hidden_stream")
    assert "Colon character" in str(exc.value)

    # Trailing dots or spaces
    with pytest.raises(ValueError) as exc:
        validate_resource_filename("bad_file.txt.")
    assert "Trailing dots or spaces" in str(exc.value)

    with pytest.raises(ValueError) as exc:
        validate_resource_filename("bad_dir /test.txt")
    assert "Trailing dots or spaces" in str(exc.value)

    # Directory traversal
    with pytest.raises(PermissionError) as exc:
        validate_resource_filename("../secret.txt")
    assert "Directory traversal" in str(exc.value)

    # Absolute or root-prefixed paths
    with pytest.raises(ValueError) as exc:
        validate_resource_filename("/etc/passwd")
    assert "Absolute or root-prefixed" in str(exc.value)

    # Empty and NUL byte
    with pytest.raises(ValueError) as exc:
        validate_resource_filename("")
    assert "cannot be empty" in str(exc.value)

    with pytest.raises(ValueError) as exc:
        validate_resource_filename("file\x00.txt")
    assert "NUL byte" in str(exc.value)

    # Valid filenames and paths
    validate_resource_filename("valid_report_123.txt")
    validate_resource_filename("subfolder/nested-doc.md")
    validate_resource_filename("agents_resources/clean_file.csv")


def test_write_document_size_cap_enforcement():
    # Set max_write_bytes to 500 bytes for test
    from config_store.system_settings import set_max_write_bytes
    set_max_write_bytes(500)

    try:
        small_content = "A" * 200
        res_ok = write_document("test_small.txt", small_content, principal=SYSTEM_PRINCIPAL)
        assert json.loads(res_ok).get("success") is True

        large_content = "A" * 600
        res_blocked = write_document("test_large.txt", large_content, principal=SYSTEM_PRINCIPAL)
        data = json.loads(res_blocked)
        assert "error" in data
        assert "exceeds maximum permitted write limit" in data["error"]
    finally:
        set_max_write_bytes(1_048_576)
        from services.local_tools import AGENTS_RESOURCES_DIR
        for p in [AGENTS_RESOURCES_DIR / "test_small.txt", AGENTS_RESOURCES_DIR / "test_large.txt"]:
            if p.exists():
                p.unlink()


def test_tool_principal_authorization_fail_closed():
    # String "system" must NOT bypass authorization
    assert is_agent_authorized_for_tool("system", "write_document", authorized_skills=[]) is False
    assert is_agent_authorized_for_tool("orchestrator", "write_document", authorized_skills=[]) is False

    # SYSTEM_PRINCIPAL must bypass
    assert is_agent_authorized_for_tool(SYSTEM_PRINCIPAL, "write_document") is True

    # None or empty principal must fail closed
    assert is_agent_authorized_for_tool(None, "write_document") is False
    assert is_agent_authorized_for_tool("", "write_document") is False

    # Agent ToolPrincipal with authorized skill
    agent_principal = ToolPrincipal(kind="agent", id="agent-10008")
    assert is_agent_authorized_for_tool(agent_principal, "list_documents") is True

    # Tool invocation fail-closed when principal is None
    from services.local_tools import list_documents, read_document
    assert "Principal identity is required" in list_documents()
    assert "Principal identity is required" in read_document("doc.txt")
    assert "Principal identity is required" in write_document("doc.txt", "content")


def test_should_unmask_tool_args_enforces_outbound_pii_policy():
    """TKT-1209 (C5): Outbound web tools and PII-masked MCP servers must never receive unmasked PII args."""
    from unittest.mock import patch
    from orchestration.stages.tool_loop_runner import _should_unmask_tool_args
    from config_store.system_settings import McpServerConfig

    assert _should_unmask_tool_args("web_search") is False
    assert _should_unmask_tool_args("fetch_web_page") is False
    assert _should_unmask_tool_args("read_document") is True

    with patch(
        "orchestration.stages.tool_loop_runner.system_settings_manager.get_mcp_server_config",
        return_value=McpServerConfig(outbound_pii_masking=True),
    ):
        assert _should_unmask_tool_args("mcp__ext__lookup") is False

    with patch(
        "orchestration.stages.tool_loop_runner.system_settings_manager.get_mcp_server_config",
        return_value=McpServerConfig(outbound_pii_masking=False),
    ):
        assert _should_unmask_tool_args("mcp__ext__lookup") is True

    with patch(
        "orchestration.stages.tool_loop_runner.system_settings_manager.get_mcp_server_config",
        return_value=None,
    ):
        assert _should_unmask_tool_args("mcp__unknown__lookup") is False


def test_local_tools_cannot_access_thread_notes_dir(tmp_path):
    """TKT-1209 (H2): Files saved in THREAD_NOTES_DIR are isolated from list_documents and read_document."""
    from core import paths
    from services.local_tools import (
        configure_paths as configure_local_paths,
        list_documents,
        read_document,
        reset_paths as reset_local_paths,
    )
    from services.thread_notes_writer import (
        configure_paths as configure_notes_paths,
        reset_paths as reset_notes_paths,
        save_thread_notes_to_disk,
    )

    assert paths.THREAD_NOTES_DIR == paths.DATA_DIR / "thread_notes"

    sandbox_resources = tmp_path / "agents_resources"
    sandbox_resources.mkdir(parents=True, exist_ok=True)
    isolated_notes_dir = tmp_path / "data" / "thread_notes"

    configure_local_paths(workspace_dir=tmp_path, agents_resources_dir=sandbox_resources)
    configure_notes_paths(thread_notes_dir=isolated_notes_dir)
    try:
        res = save_thread_notes_to_disk(
            channel_id="C_TESTNOTES",
            thread_ts="1799111222.00456",
            initial_prompt="Secret Strategy",
            live_notes_text="Confidential DM notes",
        )
        assert res.get("live_notes_path")
        saved = Path(res["live_notes_path"])
        assert saved.exists()
        assert isolated_notes_dir in saved.parents

        listed = list_documents(principal=SYSTEM_PRINCIPAL)
        assert saved.name not in listed
        read_res = read_document(saved.name, principal=SYSTEM_PRINCIPAL)
        assert "Confidential DM notes" not in read_res
        read_rel_res = read_document("../data/thread_notes/" + saved.name, principal=SYSTEM_PRINCIPAL)
        assert "Confidential DM notes" not in read_rel_res
    finally:
        reset_notes_paths()
        reset_local_paths()
