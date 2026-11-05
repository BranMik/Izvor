"""
Model Context Protocol (MCP) Client Manager.

Manages external MCP server connections (stdio, SSE), tool discovery caches,
and tool execution with timeout bounds and failure isolation.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import json
import logging
from typing import Any, AsyncGenerator, Callable

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client

from orchestration.system_settings import McpServerConfig, get_mcp_server_config, get_mcp_servers
from services.mcp.schema_translator import (
    format_mcp_tool_name,
    is_mcp_tool_destructive,
    parse_mcp_tool_name,
    translate_mcp_tool_to_litellm_schema,
)
from services.mcp.windows_process import (
    assign_process_to_windows_job_object,
    build_safe_stdio_env,
)

logger = logging.getLogger(__name__)


@dataclass
class McpDiscoveredTool:
    """Represents an external MCP tool discovered from a server."""

    server_name: str
    raw_name: str
    namespaced_name: str
    description: str
    input_schema: dict[str, Any]
    litellm_schema: dict[str, Any]
    is_mutating: bool = False


class McpClientManager:
    """
    Central client manager for connecting to external MCP servers,
    discovering their tools, caching tool metadata, and executing tool calls.
    """

    def __init__(self) -> None:
        self._discovered_tools: dict[str, list[McpDiscoveredTool]] = {}
        self._mutating_tools: set[str] = set()
        self._custom_transport_factory: Callable[[str, McpServerConfig], Any] | None = None
        self._lock: asyncio.Lock | None = None

    @property
    def _async_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def configure_transport_factory(
        self,
        factory_fn: Callable[[str, McpServerConfig], Any] | None,
    ) -> None:
        """Dependency injection hook for testing without real subprocesses or network."""
        self._custom_transport_factory = factory_fn

    def reset_transport_factory(self) -> None:
        """Resets the custom transport factory and clears discovery caches."""
        self._custom_transport_factory = None
        self._discovered_tools.clear()
        self._mutating_tools.clear()
        self._lock = None

    @asynccontextmanager
    async def _create_client_session(
        self,
        server_name: str,
        server_cfg: McpServerConfig,
    ) -> AsyncGenerator[ClientSession, None]:
        """Creates an initialized ClientSession connected via stdio, sse, or mock factory."""
        if self._custom_transport_factory is not None:
            async with self._custom_transport_factory(server_name, server_cfg) as session_or_streams:
                if isinstance(session_or_streams, (tuple, list)):
                    read_stream, write_stream = session_or_streams
                    async with ClientSession(read_stream, write_stream) as session:
                        yield session
                else:
                    yield session_or_streams
            return

        transport_type = (server_cfg.transport or "stdio").lower()

        if transport_type == "stdio":
            safe_env = build_safe_stdio_env(server_cfg.env)
            server_params = StdioServerParameters(
                command=server_cfg.command,
                args=list(server_cfg.args),
                env=safe_env,
                cwd=server_cfg.cwd,
            )
            async with stdio_client(server_params) as (read_stream, write_stream):
                # Attempt to assign spawned process to Windows Job Object if possible
                try:
                    # In mcp.client.stdio, write_stream._process or similar may exist
                    proc = getattr(write_stream, "_process", None) or getattr(read_stream, "_process", None)
                    if proc and hasattr(proc, "pid"):
                        assign_process_to_windows_job_object(proc.pid)
                except Exception:
                    pass

                async with ClientSession(read_stream, write_stream) as session:
                    yield session

        elif transport_type in ("sse", "streamable_http"):
            if not server_cfg.url:
                raise ValueError(f"MCP server '{server_name}' has transport '{transport_type}' but no 'url' configured")
            async with sse_client(
                url=server_cfg.url,
                headers=dict(server_cfg.headers) if server_cfg.headers else None,
                timeout=server_cfg.timeout_seconds,
            ) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    yield session

        else:
            raise ValueError(f"Unsupported MCP transport '{transport_type}' for server '{server_name}'")

    async def discover_server_tools(
        self,
        server_name: str,
        force_refresh: bool = False,
    ) -> list[McpDiscoveredTool]:
        """
        Connects to server_name, runs session.list_tools(), translates schemas,
        and caches the discovered McpDiscoveredTool entries.
        """
        async with self._async_lock:
            if not force_refresh and server_name in self._discovered_tools:
                return list(self._discovered_tools[server_name])

            server_cfg = get_mcp_server_config(server_name)
            if not server_cfg:
                logger.warning(f"[MCP_CLIENT] Cannot discover tools: server '{server_name}' is not configured")
                return []
            if not server_cfg.enabled:
                logger.info(f"[MCP_CLIENT] Server '{server_name}' is disabled; skipping discovery")
                return []

            try:
                async def _do_discovery() -> list[McpDiscoveredTool]:
                    async with self._create_client_session(server_name, server_cfg) as session:
                        await session.initialize()
                        list_result = await session.list_tools()
                        raw_tools = getattr(list_result, "tools", []) or []

                        discovered: list[McpDiscoveredTool] = []
                        for tool in raw_tools:
                            raw_name = getattr(tool, "name", None) or (tool.get("name") if isinstance(tool, dict) else "unknown")
                            description = getattr(tool, "description", None) or (tool.get("description") if isinstance(tool, dict) else "")
                            input_schema = getattr(tool, "inputSchema", None) or (tool.get("inputSchema") if isinstance(tool, dict) else {})
                            annotations = getattr(tool, "annotations", None) or (tool.get("annotations") if isinstance(tool, dict) else None)

                            litellm_schema = translate_mcp_tool_to_litellm_schema(server_name, tool)
                            namespaced_name = litellm_schema["function"]["name"]
                            is_mutating = is_mcp_tool_destructive(server_cfg, raw_name, annotations)

                            if is_mutating:
                                self._mutating_tools.add(namespaced_name)

                            schema_dict = input_schema.model_dump() if hasattr(input_schema, "model_dump") else (dict(input_schema) if isinstance(input_schema, dict) else {})

                            discovered.append(
                                McpDiscoveredTool(
                                    server_name=server_name,
                                    raw_name=raw_name,
                                    namespaced_name=namespaced_name,
                                    description=description or "",
                                    input_schema=schema_dict,
                                    litellm_schema=litellm_schema,
                                    is_mutating=is_mutating,
                                )
                            )
                        return discovered

                tools = await asyncio.wait_for(_do_discovery(), timeout=server_cfg.timeout_seconds)
                self._discovered_tools[server_name] = tools
                logger.info(f"[MCP_CLIENT] Discovered {len(tools)} tools from server '{server_name}'")
                return list(tools)

            except Exception as e:
                logger.error(f"[MCP_CLIENT] Tool discovery failed for server '{server_name}': {e}")
                return []

    async def discover_all_enabled_servers(self) -> dict[str, list[McpDiscoveredTool]]:
        """
        Discovers tools across all enabled MCP servers defined in system_settings.json.
        Isolates errors per server.
        """
        all_servers = get_mcp_servers()
        results: dict[str, list[McpDiscoveredTool]] = {}
        for server_name, server_cfg in all_servers.items():
            if not server_cfg.enabled:
                continue
            try:
                tools = await self.discover_server_tools(server_name)
                results[server_name] = tools
            except Exception as e:
                logger.error(f"[MCP_CLIENT] Error during batch discovery for '{server_name}': {e}")
                results[server_name] = []
        return results

    def get_cached_tools_for_server(
        self,
        server_name: str,
        allowed_raw_names: list[str] | None = None,
    ) -> list[McpDiscoveredTool]:
        """
        Returns cached McpDiscoveredTool entries for server_name.
        If allowed_raw_names is specified (and not empty / contains '*'),
        filters tools to only those whose raw_name is in allowed_raw_names.
        """
        cached = self._discovered_tools.get(server_name, [])
        if not allowed_raw_names or "*" in allowed_raw_names:
            return list(cached)

        allowed_set = set(allowed_raw_names)
        return [t for t in cached if t.raw_name in allowed_set]

    def is_tool_mutating(self, namespaced_tool_name: str) -> bool:
        """
        Checks if a namespaced MCP tool is mutating/destructive, requiring HITL approval.
        """
        if namespaced_tool_name in self._mutating_tools:
            return True

        parsed = parse_mcp_tool_name(namespaced_tool_name)
        if not parsed:
            return False

        server_name, raw_name = parsed
        server_cfg = get_mcp_server_config(server_name)
        if server_cfg and (raw_name in server_cfg.mutating_tools or "*" in server_cfg.mutating_tools):
            return True

        return False

    async def call_mcp_tool(
        self,
        server_name: str,
        raw_tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> str:
        """
        Invokes raw_tool_name on server_name with arguments within timeout bounds.
        Returns a formatted text string or JSON error string.
        """
        server_cfg = get_mcp_server_config(server_name)
        if not server_cfg:
            return json.dumps({
                "server": server_name,
                "tool": raw_tool_name,
                "isError": True,
                "error": f"MCP server '{server_name}' is not configured",
            })
        if not server_cfg.enabled:
            return json.dumps({
                "server": server_name,
                "tool": raw_tool_name,
                "isError": True,
                "error": f"MCP server '{server_name}' is disabled",
            })

        timeout_sec = server_cfg.timeout_seconds or 30.0

        async def _invoke() -> str:
            async with self._create_client_session(server_name, server_cfg) as session:
                await session.initialize()
                call_res = await session.call_tool(raw_tool_name, arguments or {})

                is_error = getattr(call_res, "isError", False)
                content_items = getattr(call_res, "content", []) or []

                text_blocks: list[str] = []
                for item in content_items:
                    if hasattr(item, "text"):
                        text_blocks.append(str(item.text))
                    elif isinstance(item, dict) and "text" in item:
                        text_blocks.append(str(item["text"]))
                    else:
                        text_blocks.append(str(item))

                output_text = "\n".join(text_blocks).strip()
                if is_error:
                    return json.dumps({
                        "server": server_name,
                        "tool": raw_tool_name,
                        "isError": True,
                        "error": output_text or "MCP Tool reported failure",
                    })

                return output_text if output_text else json.dumps({
                    "server": server_name,
                    "tool": raw_tool_name,
                    "status": "success",
                })

        try:
            return await asyncio.wait_for(_invoke(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            logger.error(f"[MCP_CLIENT] Call to {server_name}.{raw_tool_name} timed out after {timeout_sec}s")
            return json.dumps({
                "server": server_name,
                "tool": raw_tool_name,
                "isError": True,
                "error": f"Tool execution timed out after {timeout_sec}s",
            })
        except Exception as e:
            logger.error(f"[MCP_CLIENT] Error invoking {server_name}.{raw_tool_name}: {e}")
            return json.dumps({
                "server": server_name,
                "tool": raw_tool_name,
                "isError": True,
                "error": f"Tool execution failed: {e}",
            })

    async def shutdown(self) -> None:
        """Cleanly releases any cached state."""
        self._discovered_tools.clear()
        self._mutating_tools.clear()
        self._lock = None
        logger.info("[MCP_CLIENT] MCP client manager shut down cleanly.")


# Global singleton instance
mcp_client_manager: McpClientManager = McpClientManager()


def get_mcp_client_manager() -> McpClientManager:
    """Returns the singleton McpClientManager instance."""
    return mcp_client_manager
