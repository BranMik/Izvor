"""
Schema translation and tool namespacing for Model Context Protocol (MCP).

Provides bidirectional mapping between external MCP tool definitions and
LiteLLM/OpenAI-compatible function calling schemas, ensuring strict namespacing:
mcp__{server_name}__{tool_name}
"""

import hashlib
import logging
import re
import threading
from typing import Any

from config_store.system_settings import McpServerConfig

logger = logging.getLogger(__name__)

# Valid function name pattern for OpenAI/Anthropic/Gemini: ^[a-zA-Z0-9_-]{1,64}$
_MCP_NAME_PATTERN = re.compile(r"^mcp__([a-zA-Z0-9_-]+?)__([a-zA-Z0-9_-]+)$")
_SANITIZE_IDENTIFIER = re.compile(r"[^a-zA-Z0-9_-]")
_REGISTRY_LOCK = threading.RLock()
_NAMESPACED_TO_RAW: dict[str, tuple[str, str]] = {}


def register_mcp_tool_name(namespaced_name: str, server_name: str, raw_tool_name: str) -> None:
    """Records bidirectional mapping from canonical LLM tool name back to (server_name, raw_tool_name)."""
    with _REGISTRY_LOCK:
        _NAMESPACED_TO_RAW[namespaced_name] = (server_name, raw_tool_name)


def clear_mcp_tool_name_registry() -> None:
    """Clears the registered MCP tool name lookup table."""
    with _REGISTRY_LOCK:
        _NAMESPACED_TO_RAW.clear()


def format_mcp_tool_name(server_name: str, raw_tool_name: str) -> str:
    """
    Constructs a canonical namespaced tool name: mcp__{server_name}__{raw_tool_name}.

    Replaces invalid characters with underscores to conform to LLM function name limits,
    appends a 6-char SHA-256 hash suffix when >64 chars, and registers exact reverse mapping.
    """
    raw_server = server_name.strip()
    raw_tool = raw_tool_name.strip()
    clean_server = _SANITIZE_IDENTIFIER.sub("_", raw_server)
    clean_tool = _SANITIZE_IDENTIFIER.sub("_", raw_tool)
    full_name = f"mcp__{clean_server}__{clean_tool}"
    if len(full_name) > 64:
        digest = hashlib.sha256(f"{raw_server}:{raw_tool}".encode("utf-8")).hexdigest()[:6]
        full_name = f"{full_name[:57]}_{digest}"
    register_mcp_tool_name(full_name, raw_server, raw_tool)
    return full_name


def parse_mcp_tool_name(full_tool_name: str) -> tuple[str, str] | None:
    """
    Parses a namespaced tool name into (server_name, raw_tool_name).
    Returns None if the tool name does not match the MCP namespace convention.
    """
    if not isinstance(full_tool_name, str):
        return None
    with _REGISTRY_LOCK:
        registered = _NAMESPACED_TO_RAW.get(full_tool_name)
        if registered is not None:
            return registered

    try:
        from config_store.system_settings import get_mcp_servers

        configured_servers = list(get_mcp_servers().keys())
    except Exception:  # noqa: BLE001
        configured_servers = []

    for srv in sorted(configured_servers, key=len, reverse=True):
        clean_srv = _SANITIZE_IDENTIFIER.sub("_", str(srv).strip())
        prefix = f"mcp__{clean_srv}__"
        if full_tool_name.startswith(prefix):
            raw_part = full_tool_name[len(prefix) :]
            if raw_part and re.fullmatch(r"[a-zA-Z0-9_-]+", raw_part):
                return str(srv).strip(), raw_part

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

