"""
Schema translation and tool namespacing for Model Context Protocol (MCP).

Provides bidirectional mapping between external MCP tool definitions and
LiteLLM/OpenAI-compatible function calling schemas, ensuring strict namespacing:
mcp__{server_name}__{tool_name}
"""

import logging
import re
from typing import Any

from orchestration.system_settings import McpServerConfig

logger = logging.getLogger(__name__)

# Valid function name pattern for OpenAI/Anthropic/Gemini: ^[a-zA-Z0-9_-]{1,64}$
_MCP_NAME_PATTERN = re.compile(r"^mcp__([a-zA-Z0-9_-]+)__([a-zA-Z0-9_-]+)$")
_SANITIZE_IDENTIFIER = re.compile(r"[^a-zA-Z0-9_-]")


def format_mcp_tool_name(server_name: str, raw_tool_name: str) -> str:
    """
    Constructs a canonical namespaced tool name: mcp__{server_name}__{raw_tool_name}.

    Replaces invalid characters with underscores to conform to LLM function name limits.
    """
    clean_server = _SANITIZE_IDENTIFIER.sub("_", server_name.strip())
    clean_tool = _SANITIZE_IDENTIFIER.sub("_", raw_tool_name.strip())
    full_name = f"mcp__{clean_server}__{clean_tool}"
    if len(full_name) > 64:
        # Truncate while preserving prefix and suffix
        max_tool_len = 64 - len(f"mcp__{clean_server}__")
        if max_tool_len > 8:
            clean_tool = clean_tool[:max_tool_len]
            full_name = f"mcp__{clean_server}__{clean_tool}"
        else:
            full_name = full_name[:64]
    return full_name


def parse_mcp_tool_name(full_tool_name: str) -> tuple[str, str] | None:
    """
    Parses a namespaced tool name into (server_name, raw_tool_name).
    Returns None if the tool name does not match the MCP namespace convention.
    """
    if not isinstance(full_tool_name, str):
        return None
    match = _MCP_NAME_PATTERN.match(full_tool_name)
    if not match:
        return None
    return match.group(1), match.group(2)


def is_mcp_namespaced_tool(tool_name: str) -> bool:
    """Returns True if the tool name belongs to the MCP namespace."""
    return parse_mcp_tool_name(tool_name) is not None


def translate_mcp_tool_to_litellm_schema(server_name: str, mcp_tool: Any) -> dict[str, Any]:
    """
    Converts an MCP Tool object (or dict) into LiteLLM/OpenAI-compatible function schema:
    {
        "type": "function",
        "function": {
            "name": "mcp__{server}__{tool}",
            "description": "...",
            "parameters": { ... JSON schema ... }
        }
    }
    """
    raw_name = getattr(mcp_tool, "name", None) or (mcp_tool.get("name") if isinstance(mcp_tool, dict) else "unknown_tool")
    description = getattr(mcp_tool, "description", None) or (mcp_tool.get("description") if isinstance(mcp_tool, dict) else "")
    input_schema = getattr(mcp_tool, "inputSchema", None) or (mcp_tool.get("inputSchema") if isinstance(mcp_tool, dict) else {})

    if hasattr(input_schema, "model_dump"):
        parameters = input_schema.model_dump()
    elif isinstance(input_schema, dict):
        parameters = dict(input_schema)
    else:
        parameters = {"type": "object", "properties": {}}

    # Ensure parameters has valid JSON Schema structure
    if "type" not in parameters:
        parameters["type"] = "object"
    if "properties" not in parameters and parameters.get("type") == "object":
        parameters["properties"] = {}

    namespaced_name = format_mcp_tool_name(server_name, raw_name)
    desc_str = str(description or f"External MCP tool '{raw_name}' from server '{server_name}'").strip()

    return {
        "type": "function",
        "function": {
            "name": namespaced_name,
            "description": desc_str,
            "parameters": parameters,
        },
    }


def is_mcp_tool_destructive(
    server_cfg: McpServerConfig,
    raw_tool_name: str,
    tool_annotations: Any = None,
) -> bool:
    """
    Determines if an MCP tool should be treated as mutating/destructive,
    requiring mandatory Human-In-The-Loop (HITL) approval before execution.

    Checks:
    1. Explicit mutating_tools list in server configuration.
    2. MCP ToolAnnotations hints (destructiveHint, readOnlyHint=False).
    """
    if raw_tool_name in server_cfg.mutating_tools or "*" in server_cfg.mutating_tools:
        return True

    if tool_annotations is not None:
        if isinstance(tool_annotations, dict):
            if tool_annotations.get("destructiveHint") is True:
                return True
            if tool_annotations.get("readOnlyHint") is False:
                return True
        else:
            if getattr(tool_annotations, "destructiveHint", None) is True:
                return True
            if getattr(tool_annotations, "readOnlyHint", None) is False:
                return True

    return False

