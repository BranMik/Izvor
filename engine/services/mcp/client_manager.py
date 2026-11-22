"""
Model Context Protocol (MCP) Client Manager.

Manages external MCP server connections (stdio, SSE), tool discovery caches,
and tool execution with timeout bounds and failure isolation.
"""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
import json
import logging
from typing import Any, AsyncGenerator, Callable

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client

from orchestration.system_settings import McpServerConfig, get_mcp_server_config, get_mcp_servers
from services.mcp.schema_translator import (
    clear_mcp_tool_name_registry,
    is_mcp_tool_destructive,
    parse_mcp_tool_name,
    translate_mcp_tool_to_litellm_schema,
)
from services.mcp.windows_process import (
    assign_process_to_windows_job_object,
    build_safe_stdio_env,
    close_all_windows_job_handles,
    close_windows_job_handle,
)

logger = logging.getLogger(__name__)

_TRANSIENT_MCP_ERRORS = (
    OSError,
    ConnectionError,
    RuntimeError,
    anyio.BrokenResourceError,
    anyio.ClosedResourceError,
)


@dataclass
class _ManagedServerSession:
    """Tracks an active persistent MCP ClientSession and its teardown stack."""

    server_name: str
    config_signature: str
    exit_stack: AsyncExitStack
    session: Any
    call_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    job_handle: Any = None
    loop: asyncio.AbstractEventLoop | None = None


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
        self._lock_loop: asyncio.AbstractEventLoop | None = None
        self._sessions: dict[str, _ManagedServerSession] = {}
        self._server_connect_locks: dict[str, tuple[asyncio.AbstractEventLoop | None, asyncio.Lock]] = {}

    @property
    def _async_lock(self) -> asyncio.Lock:
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if self._lock is None or (running_loop is not None and self._lock_loop is not running_loop):
            self._lock = asyncio.Lock()
            self._lock_loop = running_loop
            self._server_connect_locks.clear()
        return self._lock

    def _get_server_connect_lock(self, server_name: str) -> asyncio.Lock:
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        lock_entry = self._server_connect_locks.get(server_name)
        if lock_entry is None or (running_loop is not None and lock_entry[0] is not running_loop):
            new_lock = asyncio.Lock()
            self._server_connect_locks[server_name] = (running_loop, new_lock)
            return new_lock
        return lock_entry[1]

    def configure_transport_factory(
        self,
        factory_fn: Callable[[str, McpServerConfig], Any] | None,
    ) -> None:
        """Dependency injection hook for testing without real subprocesses or network."""
        self._custom_transport_factory = factory_fn

    def reset_transport_factory(self) -> None:
        """Resets the custom transport factory and clears sessions, handles, and discovery caches."""
        self._custom_transport_factory = None
        self._sessions.clear()
        self._server_connect_locks.clear()
        self._discovered_tools.clear()
        self._mutating_tools.clear()
        close_all_windows_job_handles()
        clear_mcp_tool_name_registry()
        self._lock = None
        self._lock_loop = None

    @staticmethod
    def _compute_config_signature(server_cfg: McpServerConfig) -> str:
        payload = server_cfg.model_dump() if hasattr(server_cfg, "model_dump") else str(server_cfg)
        return json.dumps(payload, sort_keys=True, default=str)

    @asynccontextmanager
    async def _create_client_session(
        self,
        server_name: str,
        server_cfg: McpServerConfig,
        *,
        _job_handles_out: list[Any] | None = None,
    ) -> AsyncGenerator[ClientSession, None]:
        """Creates an initialized ClientSession connected via stdio, sse, streamable_http, or mock factory."""
        if self._custom_transport_factory is not None:
            async with self._custom_transport_factory(server_name, server_cfg) as session_or_streams:
                if isinstance(session_or_streams, (tuple, list)):
                    read_stream, write_stream = session_or_streams[0], session_or_streams[1]
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
                try:
                    proc = getattr(write_stream, "_process", None) or getattr(read_stream, "_process", None)
                    if proc and hasattr(proc, "pid"):
                        h_job = assign_process_to_windows_job_object(proc.pid)
                        if h_job and _job_handles_out is not None:
                            _job_handles_out.append(h_job)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[MCP_CLIENT] Skipping Windows Job Object attachment for '{server_name}': {exc}")

                async with ClientSession(read_stream, write_stream) as session:
                    yield session

        elif transport_type == "sse":
            if not server_cfg.url:
                raise ValueError(f"MCP server '{server_name}' has transport 'sse' but no 'url' configured")
            async with sse_client(
                url=server_cfg.url,
                headers=dict(server_cfg.headers) if server_cfg.headers else None,
                timeout=server_cfg.timeout_seconds,
            ) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    yield session

        elif transport_type == "streamable_http":
            if not server_cfg.url:
                raise ValueError(
                    f"MCP server '{server_name}' has transport 'streamable_http' but no 'url' configured"
                )
            import httpx
            from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

            http_headers = dict(server_cfg.headers) if server_cfg.headers else None
            http_timeout = httpx.Timeout(server_cfg.timeout_seconds or 30.0)
            async with create_mcp_http_client(headers=http_headers, timeout=http_timeout) as http_client:
                async with streamable_http_client(server_cfg.url, http_client=http_client) as (
                    read_stream,
                    write_stream,
                    _get_session_id,
                ):
                    async with ClientSession(read_stream, write_stream) as session:
                        yield session

        else:
            raise ValueError(f"Unsupported MCP transport '{transport_type}' for server '{server_name}'")

    async def _close_server_session(self, server_name: str) -> None:
        """Closes and removes any cached persistent session for server_name."""
        entry = self._sessions.pop(server_name, None)
        if entry is None:
            return
        try:
            await entry.exit_stack.aclose()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[MCP_CLIENT] Ignoring error closing session for '{server_name}': {exc}")
        finally:
            if entry.job_handle:
                close_windows_job_handle(entry.job_handle)

    async def _get_or_connect_session(
        self,
        server_name: str,
        server_cfg: McpServerConfig,
    ) -> _ManagedServerSession:
        """Returns a live cached initialized session for server_name, or opens & initializes one."""
        sig = self._compute_config_signature(server_cfg)
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        connect_lock = self._get_server_connect_lock(server_name)
        async with connect_lock:
            existing = self._sessions.get(server_name)
            if existing is not None and existing.config_signature == sig and existing.loop is running_loop:
                return existing

            if existing is not None:
                if existing.loop is running_loop or existing.loop is None:
                    await self._close_server_session(server_name)
                else:
                    self._sessions.pop(server_name, None)
                    if existing.job_handle:
                        close_windows_job_handle(existing.job_handle)

            stack = AsyncExitStack()
            job_handles: list[Any] = []
            try:
                session = await stack.enter_async_context(
                    self._create_client_session(server_name, server_cfg, _job_handles_out=job_handles)
                )
                timeout_sec = server_cfg.timeout_seconds or 30.0
                await asyncio.wait_for(session.initialize(), timeout=timeout_sec)
                entry = _ManagedServerSession(
                    server_name=server_name,
                    config_signature=sig,
                    exit_stack=stack,
                    session=session,
                    job_handle=job_handles[0] if job_handles else None,
                    loop=running_loop,
                )
                self._sessions[server_name] = entry
                return entry
            except BaseException:
                try:
                    await stack.aclose()
                except Exception as close_exc:  # noqa: BLE001
                    logger.debug(f"[MCP_CLIENT] Ignoring stack close error during cleanup for '{server_name}': {close_exc}")
                for h_job in job_handles:
                    close_windows_job_handle(h_job)
                raise

    async def _run_on_managed_session(
        self,
        server_name: str,
        server_cfg: McpServerConfig,
        op_fn: Callable[[Any], Any],
        timeout_sec: float,
    ) -> Any:
        """Executes op_fn on a managed session, reconnecting once on transient transport errors."""
        for attempt in range(2):
            entry = await self._get_or_connect_session(server_name, server_cfg)
            try:
                async with entry.call_lock:
                    return await asyncio.wait_for(op_fn(entry.session), timeout=timeout_sec)
            except asyncio.TimeoutError:
                await self._close_server_session(server_name)
                raise
            except _TRANSIENT_MCP_ERRORS as exc:
                logger.warning(
                    f"[MCP_CLIENT] Transient connection error on '{server_name}' (attempt {attempt + 1}/2): {exc}"
                )
                await self._close_server_session(server_name)
                if attempt == 1:
                    raise

    async def discover_server_tools(
        self,
        server_name: str,
        force_refresh: bool = False,
    ) -> list[McpDiscoveredTool]:
        """
        Connects to server_name (or reuses its persistent session), runs session.list_tools(),
        translates schemas, and caches the discovered McpDiscoveredTool entries.
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

            timeout_sec = server_cfg.timeout_seconds or 30.0
            try:
                async def _do_discovery(session: Any) -> list[McpDiscoveredTool]:
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

                        schema_dict = (
                            input_schema.model_dump()
                            if hasattr(input_schema, "model_dump")
                            else (dict(input_schema) if isinstance(input_schema, dict) else {})
                        )

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

                tools = await self._run_on_managed_session(server_name, server_cfg, _do_discovery, timeout_sec)
                for tool in tools:
                    if tool.is_mutating:
                        self._mutating_tools.add(tool.namespaced_name)
                self._discovered_tools[server_name] = tools
                logger.info(f"[MCP_CLIENT] Discovered {len(tools)} tools from server '{server_name}'")
                return list(tools)

            except Exception as e:  # noqa: BLE001
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
            except Exception as e:  # noqa: BLE001
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

        for tools in self._discovered_tools.values():
            for tool in tools:
                if tool.namespaced_name == namespaced_tool_name:
                    return bool(tool.is_mutating)

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
        Invokes raw_tool_name on server_name using a persistent initialized session with
        automatic single reconnect on transient transport failures.
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

        async def _invoke(session: Any) -> str:
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
            return await self._run_on_managed_session(server_name, server_cfg, _invoke, timeout_sec)
        except asyncio.TimeoutError:
            logger.error(f"[MCP_CLIENT] Call to {server_name}.{raw_tool_name} timed out after {timeout_sec}s")
            return json.dumps({
                "server": server_name,
                "tool": raw_tool_name,
                "isError": True,
                "error": f"Tool execution timed out after {timeout_sec}s",
            })
        except Exception as e:  # noqa: BLE001
            logger.error(f"[MCP_CLIENT] Error invoking {server_name}.{raw_tool_name}: {e}")
            return json.dumps({
                "server": server_name,
                "tool": raw_tool_name,
                "isError": True,
                "error": f"Tool execution failed: {e}",
            })

    async def shutdown(self) -> None:
        """Cleanly closes all managed sessions, Windows job handles, and cached state."""
        for server_name in list(self._sessions.keys()):
            await self._close_server_session(server_name)
        close_all_windows_job_handles()
        self._server_connect_locks.clear()
        self._discovered_tools.clear()
        self._mutating_tools.clear()
        clear_mcp_tool_name_registry()
        self._lock = None
        self._lock_loop = None
        logger.info("[MCP_CLIENT] MCP client manager shut down cleanly.")


# Global singleton instance
mcp_client_manager: McpClientManager = McpClientManager()


def get_mcp_client_manager() -> McpClientManager:
    """Returns the singleton McpClientManager instance."""
    return mcp_client_manager
