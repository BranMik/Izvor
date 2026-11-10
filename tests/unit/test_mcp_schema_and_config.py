"""
Unit tests for MCP Server configuration, environment interpolation,
tool schema translation, and subprocess environment isolation.
"""

import os
import pytest

from orchestration.system_settings import (
    McpServerConfig,
    _expand_env_vars_in_dict,
    _expand_env_vars_in_str,
    system_settings_manager,
    get_mcp_servers,
    get_mcp_server_config,
    set_mcp_servers,
)
from services.mcp.schema_translator import (
    format_mcp_tool_name,
    is_mcp_namespaced_tool,
    is_mcp_tool_destructive,
    parse_mcp_tool_name,
    translate_mcp_tool_to_litellm_schema,
)
from services.mcp.windows_process import (
    BLOCKED_ENV_VARS,
    build_safe_stdio_env,
)


def test_mcp_server_config_defaults():
    """Validates McpServerConfig default values."""
    cfg = McpServerConfig()
    assert cfg.enabled is True
    assert cfg.transport == "stdio"
    assert cfg.command == ""
    assert cfg.args == []
    assert cfg.env == {}
    assert cfg.cwd is None
    assert cfg.url == ""
    assert cfg.headers == {}
    assert cfg.timeout_seconds == 30.0
    assert cfg.mutating_tools == []
    assert cfg.outbound_pii_masking is True


def test_env_var_expansion():
    """Validates ${VAR} expansion in strings and nested dictionaries."""
    test_env = {"TEST_TOKEN": "secret_abc123", "SERVER_PORT": "8080"}
    expanded_str = _expand_env_vars_in_str("Bearer ${TEST_TOKEN}", env=test_env)
    assert expanded_str == "Bearer secret_abc123"

    d = {
        "auth": "Bearer ${TEST_TOKEN}",
        "url": "http://localhost:${SERVER_PORT}/sse",
        "nested": {"key": "${TEST_TOKEN}"},
        "list": ["val", "${SERVER_PORT}"],
    }
    res = _expand_env_vars_in_dict(d, env=test_env)
    assert res["auth"] == "Bearer secret_abc123"
    assert res["url"] == "http://localhost:8080/sse"
    assert res["nested"]["key"] == "secret_abc123"
    assert res["list"] == ["val", "8080"]


def test_system_settings_mcp_servers():
    """Validates getting and setting MCP servers in SystemSettingsManager."""
    test_env = {"GITHUB_TOKEN": "ghp_mock_12345"}
    servers_payload = {
        "github": {
            "enabled": True,
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"},
            "mutating_tools": ["create_issue", "push_files"],
        }
    }
    set_mcp_servers(servers_payload, save=False)

    servers = get_mcp_servers(env=test_env)
    assert "github" in servers
    gh_cfg = servers["github"]
    assert gh_cfg.command == "npx"
    assert gh_cfg.env["GITHUB_PERSONAL_ACCESS_TOKEN"] == "ghp_mock_12345"
    assert "create_issue" in gh_cfg.mutating_tools

    cfg = get_mcp_server_config("github", env=test_env)
    assert cfg is not None
    assert cfg.enabled is True

    assert get_mcp_server_config("nonexistent", env=test_env) is None


def test_format_and_parse_mcp_tool_name():
    """Validates namespacing convention mcp__{server}__{tool}."""
    formatted = format_mcp_tool_name("github", "create_issue")
    assert formatted == "mcp__github__create_issue"
    assert is_mcp_namespaced_tool(formatted) is True

    parsed = parse_mcp_tool_name(formatted)
    assert parsed == ("github", "create_issue")

    # Non-MCP tools
    assert parse_mcp_tool_name("read_document") is None
    assert is_mcp_namespaced_tool("read_document") is False
    assert parse_mcp_tool_name("mcp_single_underscore") is None

    # Sanitization of invalid characters
    sanitized = format_mcp_tool_name("my server!", "do-action.now")
    assert sanitized == "mcp__my_server___do-action_now"
    assert is_mcp_namespaced_tool(sanitized) is True


def test_translate_mcp_tool_to_litellm_schema():
    """Validates translation of raw MCP tool dict/object to LiteLLM schema."""
    mcp_tool = {
        "name": "search_repos",
        "description": "Searches GitHub repositories",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"}
            },
            "required": ["query"],
        },
    }

    schema = translate_mcp_tool_to_litellm_schema("github", mcp_tool)
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "mcp__github__search_repos"
    assert "Searches GitHub repositories" in schema["function"]["description"]
    assert schema["function"]["parameters"]["type"] == "object"
    assert "query" in schema["function"]["parameters"]["properties"]


def test_is_mcp_tool_destructive():
    """Validates detection of mutating/destructive tools."""
    cfg = McpServerConfig(mutating_tools=["create_issue", "delete_branch"])

    # In mutating_tools list
    assert is_mcp_tool_destructive(cfg, "create_issue") is True
    assert is_mcp_tool_destructive(cfg, "delete_branch") is True
    assert is_mcp_tool_destructive(cfg, "get_issue") is False

    # Annotation destructiveHint
    assert is_mcp_tool_destructive(cfg, "wipe_data", {"destructiveHint": True}) is True
    assert is_mcp_tool_destructive(cfg, "read_data", {"destructiveHint": False}) is False

    # Annotation readOnlyHint=False
    assert is_mcp_tool_destructive(cfg, "update_status", {"readOnlyHint": False}) is True
    assert is_mcp_tool_destructive(cfg, "read_status", {"readOnlyHint": True}) is False


def test_build_safe_stdio_env_strips_internal_secrets():
    """Validates that build_safe_stdio_env strips sensitive Izvor secrets."""
    base_env = {
        "PATH": "C:\\Windows\\System32",
        "SLACK_BOT_TOKEN": "xoxb-leaked-token",
        "SLACK_SIGNING_SECRET": "leaked-secret",
        "SLACK_APP_TOKEN": "xapp-leaked",
        "AUDIT_HMAC_SECRET": "super-hmac-secret",
        "ADMIN_PASSWORD": "admin-password",
    }
    child_env = build_safe_stdio_env({"CUSTOM_API_KEY": "val123"}, base_env=base_env)

    # Preserved system vars
    assert "PATH" in child_env
    assert child_env["PYTHONIOENCODING"] == "utf-8"
    assert child_env["PYTHONUTF8"] == "1"
    assert child_env["CUSTOM_API_KEY"] == "val123"

    # Blocked secrets must NEVER be present
    for blocked in BLOCKED_ENV_VARS:
        assert blocked not in child_env

