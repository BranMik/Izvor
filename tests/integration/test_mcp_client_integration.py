"""
Integration tests for MCP Client Manager, in-memory transport testing,
declarative SKILL.md MCP bindings, and external tool execution.
"""

import asyncio
from contextlib import asynccontextmanager
import json
from unittest.mock import AsyncMock, MagicMock
import pytest

from core.domain import ToolPrincipal
from orchestration.system_settings import set_mcp_servers
from services.mcp.client_manager import (
    McpClientManager,
    get_mcp_client_manager,
)
from services.mcp.security_bridge import execute_external_mcp_tool
from skills.loader import SkillDefinition, SkillRegistry


class DummyContent:
    def __init__(self, text: str):
        self.text = text


class DummyCallToolResult:
    def __init__(self, text: str, is_error: bool = False):
        self.content = [DummyContent(text)]
        self.isError = is_error


class DummyMcpTool:
    def __init__(self, name: str, description: str, input_schema: dict, annotations: dict | None = None):
        self.name = name
        self.description = description
        self.inputSchema = input_schema
        self.annotations = annotations


@pytest.fixture
def mock_mcp_server():
    """Sets up an in-memory mock MCP server for 'github'."""
    set_mcp_servers({
        "github": {
            "enabled": True,
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "mutating_tools": ["create_issue"],
            "timeout_seconds": 5.0,
            "outbound_pii_masking": True,
        }
    }, save=False)

    mock_session = MagicMock()
    mock_session.initialize = AsyncMock()

    mock_tools = [
        DummyMcpTool(
            name="search_repos",
            description="Searches GitHub repositories",
            input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        ),
        DummyMcpTool(
            name="create_issue",
            description="Creates a new GitHub issue",
            input_schema={"type": "object", "properties": {"title": {"type": "string"}}},
            annotations={"destructiveHint": True},
        ),
    ]
    mock_session.list_tools = AsyncMock(return_value=MagicMock(tools=mock_tools))

    async def _mock_call_tool(name, arguments):
        if name == "search_repos":
            return DummyCallToolResult(json.dumps([{"full_name": "owner/izvor", "stars": 100}]))
        elif name == "create_issue":
            return DummyCallToolResult(json.dumps({"issue_id": 42, "status": "opened"}))
        return DummyCallToolResult("Unknown tool", is_error=True)

    mock_session.call_tool = AsyncMock(side_effect=_mock_call_tool)

    @asynccontextmanager
    async def mock_transport_factory(server_name, server_cfg):
        yield mock_session

    mgr = get_mcp_client_manager()
    mgr.configure_transport_factory(mock_transport_factory)

    yield {
        "session": mock_session,
        "manager": mgr,
    }

    mgr.reset_transport_factory()


@pytest.mark.anyio
async def test_mcp_client_manager_discover_tools(mock_mcp_server):
    """Validates tool discovery and mutating flag assignment via mock transport."""
    mgr = mock_mcp_server["manager"]

    tools = await mgr.discover_server_tools("github", force_refresh=True)
    assert len(tools) == 2

    by_name = {t.raw_name: t for t in tools}
    assert "search_repos" in by_name
    assert "create_issue" in by_name

    search_tool = by_name["search_repos"]
    assert search_tool.namespaced_name == "mcp__github__search_repos"
    assert search_tool.is_mutating is False
    assert mgr.is_tool_mutating("mcp__github__search_repos") is False

    create_tool = by_name["create_issue"]
    assert create_tool.namespaced_name == "mcp__github__create_issue"
    assert create_tool.is_mutating is True
    assert mgr.is_tool_mutating("mcp__github__create_issue") is True


@pytest.mark.anyio
async def test_mcp_skill_declarative_binding_and_schemas(mock_mcp_server, tmp_path):
    """Validates parsing of SKILL.md with mcp_server and dynamic schema unlocking."""
    mgr = mock_mcp_server["manager"]
    await mgr.discover_server_tools("github", force_refresh=True)

    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text(
        "---\n"
        "name: github_management\n"
        "display_name: GitHub Management\n"
        "description: Manage GitHub issues and repositories\n"
        "requires_admin: false\n"
        "mcp_server: github\n"
        "mcp_tools:\n"
        "  - search_repos\n"
        "  - create_issue\n"
        "---\n"
        "Always search for duplicate issues before opening a new one.\n",
        encoding="utf-8",
    )

    registry = SkillRegistry()
    skill_def = registry._parse_skill_file(skill_md)

    assert skill_def.name == "github_management"
    assert skill_def.mcp_server == "github"
    assert "mcp__github__search_repos" in skill_def.tools
    assert "mcp__github__create_issue" in skill_def.tools

    schemas = skill_def.get_tool_schemas()
    schema_names = [s["function"]["name"] for s in schemas]
    assert "mcp__github__search_repos" in schema_names
    assert "mcp__github__create_issue" in schema_names


@pytest.mark.anyio
async def test_mcp_execute_external_tool_authorized(mock_mcp_server):
    """Validates successful execution of external MCP tool with ToolPrincipal."""
    mgr = mock_mcp_server["manager"]
    await mgr.discover_server_tools("github", force_refresh=True)

    principal = ToolPrincipal(kind="agent", id="dev_agent")
    registry = SkillRegistry()
    registry._skills["github_management"] = SkillDefinition(
        name="github_management",
        display_name="GitHub",
        description="desc",
        tools=["mcp__github__search_repos"],
    )

    result = await execute_external_mcp_tool(
        namespaced_tool_name="mcp__github__search_repos",
        arguments={"query": "izvor"},
        agent_name=principal,
        authorized_skills=["github_management"],
        skill_registry=registry,
        client_manager=mgr,
    )

    assert "owner/izvor" in result
    mock_mcp_server["session"].call_tool.assert_awaited_once_with(
        "search_repos", {"query": "izvor"}
    )


@pytest.mark.anyio
async def test_mcp_call_tool_timeout_and_error_isolation():
    """Validates that call_mcp_tool catches timeouts and failures without crashing."""
    set_mcp_servers({
        "slow_server": {
            "enabled": True,
            "transport": "stdio",
            "command": "dummy",
            "timeout_seconds": 0.05,
        }
    }, save=False)

    mock_session = MagicMock()
    mock_session.initialize = AsyncMock()

    async def _slow_call(name, args):
        await asyncio.sleep(0.5)
        return DummyCallToolResult("Never returned")

    mock_session.call_tool = AsyncMock(side_effect=_slow_call)

    @asynccontextmanager
    async def slow_transport_factory(server_name, server_cfg):
        yield mock_session

    mgr = McpClientManager()
    mgr.configure_transport_factory(slow_transport_factory)

    res = await mgr.call_mcp_tool("slow_server", "hang_forever", {})
    data = json.loads(res)
    assert data["isError"] is True
    assert "timed out" in data["error"].lower()


@pytest.mark.anyio
async def test_mcp_persistent_session_reuse_auto_reconnect_and_shutdown():
    """TKT-1224: Persistent sessions initialize once across calls, reconnect once on ConnectionError, and shutdown cleanly."""
    set_mcp_servers({
        "github": {
            "enabled": True,
            "transport": "stdio",
            "command": "npx",
            "timeout_seconds": 5.0,
        }
    }, save=False)

    tools = [
        DummyMcpTool(
            name="search_repos",
            description="Search repos",
            input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        )
    ]

    session1 = MagicMock()
    session1.initialize = AsyncMock()
    session1.list_tools = AsyncMock(return_value=MagicMock(tools=tools))
    session1.call_tool = AsyncMock(
        side_effect=[
            DummyCallToolResult("result_1"),
            DummyCallToolResult("result_2"),
            ConnectionError("broken pipe"),
        ]
    )

    session2 = MagicMock()
    session2.initialize = AsyncMock()
    session2.list_tools = AsyncMock(return_value=MagicMock(tools=tools))
    session2.call_tool = AsyncMock(return_value=DummyCallToolResult("reconnected_result"))

    created_sessions = []

    @asynccontextmanager
    async def pool_transport_factory(server_name, server_cfg):
        idx = len(created_sessions)
        sess = session1 if idx == 0 else session2
        created_sessions.append(sess)
        yield sess

    mgr = McpClientManager()
    mgr.configure_transport_factory(pool_transport_factory)

    try:
        discovered = await mgr.discover_server_tools("github", force_refresh=True)
        assert len(discovered) == 1
        res1 = await mgr.call_mcp_tool("github", "search_repos", {"query": "a"})
        res2 = await mgr.call_mcp_tool("github", "search_repos", {"query": "b"})

        assert res1 == "result_1"
        assert res2 == "result_2"
        session1.initialize.assert_awaited_once()
        assert len(created_sessions) == 1

        # Next call raises ConnectionError -> auto-reconnects once onto session2
        res3 = await mgr.call_mcp_tool("github", "search_repos", {"query": "c"})
        assert res3 == "reconnected_result"
        assert len(created_sessions) == 2
        session2.initialize.assert_awaited_once()
    finally:
        await mgr.shutdown()
        assert len(mgr._sessions) == 0
