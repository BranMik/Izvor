import json

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
    from orchestration.system_settings import set_max_write_bytes
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
