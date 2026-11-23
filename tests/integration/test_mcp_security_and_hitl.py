"""
Security and Human-In-The-Loop (HITL) integration tests for Model Context Protocol (MCP).

Verifies Roadmap R-04 risk controls:
1. Unauthorized agent principal calling external MCP tool fails closed with SECURITY_POLICY_VIOLATION.
2. Outbound secret/PII masking prevents credential exfiltration in MCP arguments.
3. Inbound secret scanning blocks leaked credentials in MCP responses.
4. Mutating MCP tools trigger mandatory HITL approval.
"""

from contextlib import asynccontextmanager
import json
from unittest.mock import AsyncMock, MagicMock
import pytest

from core.domain import ToolPrincipal
from config_store.system_settings import set_mcp_servers
from services.audit_logger import audit_logger
from services.mcp.client_manager import McpClientManager
from services.mcp.security_bridge import execute_external_mcp_tool
from skills.registry import SkillDefinition, SkillRegistry


class DummyContent:
    def __init__(self, text: str):
        self.text = text


class DummyCallToolResult:
    def __init__(self, text: str, is_error: bool = False):
        self.content = [DummyContent(text)]
        self.isError = is_error


@pytest.fixture
def secure_mcp_environment():
    """Sets up an MCP server with security and mutating tool configurations."""
    set_mcp_servers({
        "internal_db": {
            "enabled": True,
            "transport": "stdio",
            "command": "python",
            "mutating_tools": ["drop_table", "insert_record"],
            "timeout_seconds": 5.0,
            "outbound_pii_masking": True,
        }
    }, save=False)

    mock_session = MagicMock()
    mock_session.initialize = AsyncMock()

    recorded_calls = []

    async def _mock_call(name, args):
        recorded_calls.append({"name": name, "args": dict(args)})
        if name == "drop_table":
            return DummyCallToolResult("Table dropped successfully")
        elif name == "leak_test":
            # Response containing a fake AWS key
            return DummyCallToolResult("Connected with key AKIAIOSFODNN7EXAMPLE")
        return DummyCallToolResult("OK")

    mock_session.call_tool = AsyncMock(side_effect=_mock_call)

    @asynccontextmanager
    async def mock_transport_factory(server_name, server_cfg):
        yield mock_session

    mgr = McpClientManager()
    mgr.configure_transport_factory(mock_transport_factory)

    yield {
        "session": mock_session,
        "manager": mgr,
        "recorded_calls": recorded_calls,
    }

    mgr.reset_transport_factory()


@pytest.mark.anyio
async def test_r04_unauthorized_agent_principal_fails_closed(secure_mcp_environment):
    """
    R-04 Control 1: Agent principal without authorized skill cannot call MCP tool.
    Must fail closed and log SECURITY_POLICY_VIOLATION.
    """
    mgr = secure_mcp_environment["manager"]

    # Unauthorized agent (no skills granted)
    principal = ToolPrincipal(kind="agent", id="unauthorized_agent")

    res = await execute_external_mcp_tool(
        namespaced_tool_name="mcp__internal_db__drop_table",
        arguments={"table": "users"},
        agent_name=principal,
        authorized_skills=[],  # No skills authorized
        skill_registry=SkillRegistry(),
        client_manager=mgr,
    )

    data = json.loads(res)
    assert "Permission denied" in data["error"]
    assert "unauthorized_agent" in data["error"]

    # Verify mock session was NEVER called
    assert len(secure_mcp_environment["recorded_calls"]) == 0

    # Verify SECURITY_POLICY_VIOLATION was logged to audit
    from services.audit_logger import get_audit_trail

    audit_events = get_audit_trail(audit_dir=audit_logger.audit_dir)
    violations = [e for e in audit_events if e.get("event_type") == "SECURITY_POLICY_VIOLATION"]
    assert len(violations) > 0
    assert any("mcp__internal_db__drop_table" in str(v.get("target")) for v in violations)


@pytest.mark.anyio
async def test_r04_outbound_secret_masking(secure_mcp_environment):
    """
    R-04 Control 2: Outbound arguments containing secrets or PII are masked
    before being sent to the external MCP server transport.
    """
    mgr = secure_mcp_environment["manager"]

    # Authorized principal with database skill
    principal = ToolPrincipal(kind="agent", id="db_admin")
    registry = SkillRegistry()
    registry._skills["db_skill"] = SkillDefinition(
        name="db_skill",
        display_name="DB",
        description="desc",
        tools=["mcp__internal_db__insert_record"],
    )

    # Argument contains a private key and a Slack token
    sensitive_args = {
        "user_email": "john.doe@example.com",
        "raw_note": "Here is the key xoxb-123456789012-123456789012-abcdefghijklmnopqrstuvwx",
    }

    await execute_external_mcp_tool(
        namespaced_tool_name="mcp__internal_db__insert_record",
        arguments=sensitive_args,
        agent_name=principal,
        authorized_skills=["db_skill"],
        skill_registry=registry,
        client_manager=mgr,
    )

    assert len(secure_mcp_environment["recorded_calls"]) == 1
    sent_args = secure_mcp_environment["recorded_calls"][0]["args"]

    # Confirm Slack token was redacted before reaching MCP server
    assert "xoxb-123456789012" not in sent_args["raw_note"]
    assert "<SLACK_TOKEN" in sent_args["raw_note"]


@pytest.mark.anyio
async def test_r04_inbound_secret_leak_blocked(secure_mcp_environment):
    """
    R-04 Control 3: Inbound response from MCP server containing leaked secrets
    is blocked and replaced with security policy error.
    """
    mgr = secure_mcp_environment["manager"]

    principal = ToolPrincipal(kind="agent", id="db_admin")
    registry = SkillRegistry()
    registry._skills["db_skill"] = SkillDefinition(
        name="db_skill",
        display_name="DB",
        description="desc",
        tools=["mcp__internal_db__leak_test"],
    )

    res = await execute_external_mcp_tool(
        namespaced_tool_name="mcp__internal_db__leak_test",
        arguments={},
        agent_name=principal,
        authorized_skills=["db_skill"],
        skill_registry=registry,
        client_manager=mgr,
    )

    # Inbound response had an AWS key -> must be blocked
    data = json.loads(res)
    assert data.get("isError") is True
    assert "security policy" in data.get("error", "").lower()
    assert "AKIAIOSFODNN7EXAMPLE" not in res


def test_r04_mutating_mcp_tool_requires_hitl():
    """
    R-04 Control 4: Mutating MCP tool requires HITL approval.
    """
    mgr = McpClientManager()
    set_mcp_servers({
        "internal_db": {
            "enabled": True,
            "transport": "stdio",
            "command": "python",
            "mutating_tools": ["drop_table"],
        }
    }, save=False)

    # drop_table is mutating -> HITL required
    assert mgr.is_tool_mutating("mcp__internal_db__drop_table") is True

    # select_query is read-only -> HITL not required
    assert mgr.is_tool_mutating("mcp__internal_db__select_query") is False


@pytest.mark.anyio
async def test_r04_mcp_post_execution_audit_statuses():
    """TKT-1223: TOOL_EXECUTE_MCP is emitted after execution with SUCCESS, FAILURE, or BLOCKED status."""
    from services.audit_logger import get_audit_trail

    set_mcp_servers({
        "audit_srv": {
            "enabled": True,
            "transport": "stdio",
            "command": "python",
            "timeout_seconds": 5.0,
            "outbound_pii_masking": True,
        }
    }, save=False)

    mock_session = MagicMock()
    mock_session.initialize = AsyncMock()

    async def _mock_call(name, args):
        if name == "ok_tool":
            return DummyCallToolResult("healthy response")
        if name == "fail_tool":
            return DummyCallToolResult("upstream crash", is_error=True)
        if name == "leak_tool":
            return DummyCallToolResult("leaked AKIAIOSFODNN7EXAMPLE")
        return DummyCallToolResult("OK")

    mock_session.call_tool = AsyncMock(side_effect=_mock_call)

    @asynccontextmanager
    async def transport_factory(server_name, server_cfg):
        yield mock_session

    mgr = McpClientManager()
    mgr.configure_transport_factory(transport_factory)

    principal = ToolPrincipal(kind="agent", id="audit_agent")
    registry = SkillRegistry()
    registry._skills["audit_skill"] = SkillDefinition(
        name="audit_skill",
        display_name="Audit",
        description="desc",
        tools=[
            "mcp__audit_srv__ok_tool",
            "mcp__audit_srv__fail_tool",
            "mcp__audit_srv__leak_tool",
        ],
    )

    try:
        res_ok = await execute_external_mcp_tool(
            namespaced_tool_name="mcp__audit_srv__ok_tool",
            arguments={},
            agent_name=principal,
            authorized_skills=["audit_skill"],
            skill_registry=registry,
            client_manager=mgr,
        )
        assert res_ok == "healthy response"

        res_fail = await execute_external_mcp_tool(
            namespaced_tool_name="mcp__audit_srv__fail_tool",
            arguments={},
            agent_name=principal,
            authorized_skills=["audit_skill"],
            skill_registry=registry,
            client_manager=mgr,
        )
        assert json.loads(res_fail)["isError"] is True

        res_leak = await execute_external_mcp_tool(
            namespaced_tool_name="mcp__audit_srv__leak_tool",
            arguments={},
            agent_name=principal,
            authorized_skills=["audit_skill"],
            skill_registry=registry,
            client_manager=mgr,
        )
        assert "security policy" in json.loads(res_leak)["error"].lower()

        events = [
            e
            for e in get_audit_trail(audit_dir=audit_logger.audit_dir)
            if e.get("event_type") == "TOOL_EXECUTE_MCP"
        ]
        by_action = {e.get("action"): e.get("status") for e in events}
        assert by_action.get("mcp__audit_srv__ok_tool") == "SUCCESS"
        assert by_action.get("mcp__audit_srv__fail_tool") == "FAILURE"
        assert by_action.get("mcp__audit_srv__leak_tool") == "BLOCKED"
    finally:
        await mgr.shutdown()
