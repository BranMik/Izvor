import asyncio
import hashlib
import inspect
import json
import logging
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from core.config import TOOLS_WORKSPACE_DIR
from core.domain import ToolPrincipal
from core.paths import (
    AGENTS_RESOURCES_DIR as CANONICAL_AGENTS_RESOURCES_DIR,
    PROJECT_ROOT,
)

from .web_tools import fetch_web_page, web_search

logger = logging.getLogger(__name__)


# Workspace root directory defaults to PROJECT_ROOT
WORKSPACE_DIR = (
    TOOLS_WORKSPACE_DIR.resolve()
    if (isinstance(TOOLS_WORKSPACE_DIR, Path) and TOOLS_WORKSPACE_DIR.exists())
    else PROJECT_ROOT
)

# Agents resources directory (no import-time mkdir side effects)
AGENTS_RESOURCES_DIR = (
    (WORKSPACE_DIR / "agents_resources").resolve() if WORKSPACE_DIR != PROJECT_ROOT else CANONICAL_AGENTS_RESOURCES_DIR
)

_custom_workspace_dir: Path | None = None
_custom_agents_resources_dir: Path | None = None


def configure_paths(workspace_dir: Path | None = None, agents_resources_dir: Path | None = None) -> None:
    """Configures in-memory workspace and resources paths for test isolation."""
    global _custom_workspace_dir, _custom_agents_resources_dir
    _custom_workspace_dir = workspace_dir
    _custom_agents_resources_dir = agents_resources_dir


def reset_paths() -> None:
    """Resets custom path overrides back to defaults."""
    configure_paths(None, None)


def get_workspace_dir() -> Path:
    """Returns active workspace directory, checking override first."""
    if _custom_workspace_dir is not None:
        return _custom_workspace_dir
    return WORKSPACE_DIR


def get_agents_resources_dir() -> Path:
    """Returns active agents_resources directory, checking override first."""
    if _custom_agents_resources_dir is not None:
        return _custom_agents_resources_dir
    return AGENTS_RESOURCES_DIR


MAX_READ_DOCUMENT_CHARS = 50000


def validate_resource_filename(rel_path: str) -> None:
    """
    Validates a resource relative path to prevent directory traversal, Windows device name abuse,
    NTFS alternate data streams, control characters, and forbidden characters.
    """
    clean = str(rel_path or "").strip()
    if not clean:
        raise ValueError("Resource path cannot be empty.")
    if "\x00" in clean:
        raise ValueError("NUL byte is not permitted in resource paths.")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in clean):
        raise ValueError("Control characters are not permitted in resource paths.")
    if ":" in clean:
        raise ValueError("Colon character ':' is not permitted in resource paths.")
    if clean.startswith(("/", "\\")):
        raise ValueError("Absolute or root-prefixed resource paths are not permitted.")

    norm = clean.replace("\\", "/")
    if norm.lower().startswith("agents_resources/"):
        norm = norm[len("agents_resources/") :].lstrip("/")
    elif norm.lower() == "agents_resources":
        norm = "."

    segments = re.split(r"[/\\]+", norm)
    reserved_devices = (
        {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
    )
    segment_pattern = re.compile(r"^[\w. -]{1,128}$", re.UNICODE)

    for segment in segments:
        if not segment or segment == ".":
            continue
        if segment == "..":
            raise PermissionError(f"Access denied: Directory traversal ('..') is not permitted in '{rel_path}'.")
        if segment != segment.rstrip(". "):
            raise ValueError(f"Trailing dots or spaces are not permitted in path segment '{segment}'.")
        stem = segment.split(".")[0].upper()
        if stem in reserved_devices:
            raise ValueError(f"Reserved device name '{stem}' is not permitted in resource path.")
        if not segment_pattern.match(segment):
            raise ValueError(f"Invalid characters or length in path segment '{segment}'.")


def _resolve_resource_path(rel_path: str) -> Path:
    """
    Ensures the requested document path stays strictly within the 'agents_resources/' directory
    to prevent sandbox escape.
    Automatically normalizes paths by stripping any leading 'agents_resources/' or 'agents_resources\\' prefix
    so that both 'report.txt' and 'agents_resources/report.txt' resolve safely into AGENTS_RESOURCES_DIR.
    """
    clean_raw = str(rel_path or "").strip().replace("\\", "/")
    if clean_raw.lower().startswith("agents_resources/"):
        stripped = clean_raw[len("agents_resources/") :].lstrip("/")
    elif clean_raw.lower() == "agents_resources":
        stripped = "."
    else:
        stripped = clean_raw

    if stripped and stripped != ".":
        validate_resource_filename(stripped)

    clean_path = stripped or "."
    resources_dir = get_agents_resources_dir().resolve()
    target = (resources_dir / clean_path).resolve()
    if not target.is_relative_to(resources_dir):
        raise PermissionError(
            f"Access denied: Path '{rel_path}' is outside agents_resources directory '{resources_dir}'."
        )
    return target


def list_documents(
    rel_path: str = ".",
    principal: ToolPrincipal | str | None = None,
    authorized_skills: list[str] | set[str] | str | None = None,
) -> str:
    """
    Lists the contents of a directory inside the agents_resources/ directory.
    Enforces principal authorization (fails closed if principal is missing) and skips symlinks escaping root.
    """
    if principal is None:
        return json.dumps({"error": "Permission denied: Principal identity is required to list documents."})
    if not is_agent_authorized_for_tool(principal, "list_documents", authorized_skills=authorized_skills):
        actor_id = principal.id if hasattr(principal, "id") else str(principal)
        return json.dumps({"error": f"Permission denied: Principal '{actor_id}' is not authorized to list documents."})

    try:
        resources_dir = get_agents_resources_dir().resolve()
        path = _resolve_resource_path(rel_path)
        if not path.exists():
            return json.dumps({"error": f"Resource directory '{rel_path}' does not exist."})
        if not path.is_dir():
            return json.dumps({"error": f"Resource path '{rel_path}' is not a directory."})

        contents = []
        for item in sorted(path.iterdir()):
            try:
                resolved_item = item.resolve()
            except OSError:
                continue
            if not resolved_item.is_relative_to(resources_dir):
                logger.warning(f"[LOCAL_TOOLS] Skipping entry resolving outside sandbox root: {item}")
                continue
            contents.append(
                {
                    "name": item.name,
                    "is_dir": resolved_item.is_dir(),
                    "size_bytes": resolved_item.stat().st_size if resolved_item.is_file() else None,
                }
            )
        return json.dumps({"directory": rel_path, "contents": contents})
    except PermissionError as e:
        return json.dumps({"error": str(e)})
    except FileNotFoundError:
        return json.dumps({"error": f"Resource directory '{rel_path}' does not exist."})
    except NotADirectoryError:
        return json.dumps({"error": f"Resource path '{rel_path}' is not a directory."})
    except (ValueError, TypeError) as e:
        return json.dumps({"error": f"Invalid argument: {e}"})
    except OSError as e:
        return json.dumps({"error": f"Operating system error: {e}"})


def read_document(
    rel_path: str,
    principal: ToolPrincipal | str | None = None,
    authorized_agents: set[str] | None = None,
    authorized_skills: list[str] | set[str] | str | None = None,
    max_chars: int = MAX_READ_DOCUMENT_CHARS,
) -> str:
    """
    Reads the content of a text or PDF document inside the agents_resources/ directory.
    agents_resources/ is the default working and search directory even if omitted in rel_path.
    Enforces principal authorization, per-file ACL checks, and bounded character output.
    """
    if principal is None:
        return json.dumps({"error": "Permission denied: Principal identity is required to read documents."})
    if not is_agent_authorized_for_tool(principal, "read_document", authorized_skills=authorized_skills):
        actor_id = principal.id if hasattr(principal, "id") else str(principal)
        return json.dumps({"error": f"Permission denied: Principal '{actor_id}' is not authorized to read documents."})

    if authorized_agents is not None:
        actor_id = principal.id if hasattr(principal, "id") else str(principal)
        actor_kind = getattr(principal, "kind", "agent")
        if actor_kind != "system" and actor_id != "system" and actor_id not in authorized_agents:
            return json.dumps(
                {"error": f"Permission denied: Agent '{actor_id}' is not authorized to access document '{rel_path}'."}
            )

    from config_store.system_settings import (
        get_max_file_size_bytes,
        get_max_file_size_mb,
    )

    max_size_bytes = get_max_file_size_bytes()
    max_size_mb = get_max_file_size_mb()
    effective_max_chars = max(500, min(int(max_chars), 200000))

    def _truncate_doc(raw: str) -> tuple[str, bool]:
        if len(raw) > effective_max_chars:
            return (
                raw[:effective_max_chars]
                + f"\n\n... [TRUNCATED: Document exceeded {effective_max_chars} characters] ...",
                True,
            )
        return raw, False

    try:
        path = _resolve_resource_path(rel_path)
        if not path.exists():
            return json.dumps({"error": f"Document '{rel_path}' does not exist in agents_resources."})
        if not path.is_file():
            return json.dumps({"error": f"Resource path '{rel_path}' is not a file."})

        file_size = path.stat().st_size
        if file_size > max_size_bytes:
            return json.dumps(
                {
                    "error": f"File '{rel_path}' ({file_size} bytes) exceeds maximum allowed size of {max_size_mb:.1f} MB."
                }
            )

        if path.suffix.lower() == ".pdf":
            try:
                import pypdf

                reader = pypdf.PdfReader(str(path))
                pages_text = []
                for idx, page in enumerate(reader.pages):
                    page_text = (page.extract_text() or "").strip()
                    if page_text:
                        pages_text.append(f"--- Page {idx + 1} ---\n{page_text}")
                    else:
                        pages_text.append(f"--- Page {idx + 1} ---\n[No extractable text]")
                raw_content = "\n\n".join(pages_text) if pages_text else "[Empty PDF document]"
                content, truncated = _truncate_doc(raw_content)
                return json.dumps(
                    {
                        "document": rel_path,
                        "content": content,
                        "truncated": truncated,
                        "pages": len(reader.pages),
                    }
                )
            except ImportError:
                return json.dumps({"error": "The 'pypdf' package is required to read PDF files. Please install pypdf."})
            except Exception as e:  # noqa: BLE001 - PDF parser error boundary returns failure JSON
                return json.dumps({"error": f"Failed to parse PDF '{rel_path}': {e}"})

        raw_content = path.read_text(encoding="utf-8", errors="replace")
        content, truncated = _truncate_doc(raw_content)
        return json.dumps({"document": rel_path, "content": content, "truncated": truncated})
    except PermissionError as e:
        return json.dumps({"error": str(e)})
    except FileNotFoundError:
        return json.dumps({"error": f"Document '{rel_path}' does not exist in agents_resources."})
    except IsADirectoryError:
        return json.dumps({"error": f"Resource path '{rel_path}' is a directory, not a file."})
    except UnicodeError as e:
        return json.dumps({"error": f"Encoding error reading document '{rel_path}': {e}"})
    except (ValueError, TypeError) as e:
        return json.dumps({"error": f"Invalid argument: {e}"})
    except OSError as e:
        return json.dumps({"error": f"Operating system error reading document '{rel_path}': {e}"})


def write_document(
    rel_path: str,
    content: str,
    principal: ToolPrincipal | str | None = None,
    authorized_skills: list[str] | set[str] | str | None = None,
) -> str:
    """
    Writes or overwrites content to a text document inside the agents_resources/ directory.
    Enforces principal authorization and write boundaries (fails closed if principal is missing).
    """
    if principal is None:
        return json.dumps({"error": "Permission denied: Principal identity is required to write documents."})
    if not is_agent_authorized_for_tool(principal, "write_document", authorized_skills=authorized_skills):
        actor_id = principal.id if hasattr(principal, "id") else str(principal)
        return json.dumps({"error": f"Permission denied: Principal '{actor_id}' is not authorized to write documents."})

    actor_id = principal.id if hasattr(principal, "id") else str(principal)
    actor_kind = getattr(principal, "kind", "agent")

    try:
        from config_store.system_settings import (
            get_max_write_bytes,
            is_path_write_blocked,
        )

        content_bytes = len(content.encode("utf-8"))
        max_bytes = get_max_write_bytes()
        if content_bytes > max_bytes:
            from services.audit_logger import log_audit_event

            log_audit_event(
                event_type="FILE_WRITE_BLOCKED",
                actor={"type": actor_kind, "id": actor_id},
                action="write_document",
                target=rel_path,
                details={
                    "reason": f"Content size ({content_bytes} bytes) exceeds maximum permitted limit ({max_bytes} bytes)"
                },
                severity="CRITICAL",
                status="BLOCKED",
            )
            return json.dumps(
                {
                    "error": f"File content size ({content_bytes} bytes) exceeds maximum permitted write limit of {max_bytes} bytes."
                }
            )

        path = _resolve_resource_path(rel_path)
        resources_dir = get_agents_resources_dir().resolve()
        workspace_dir = get_workspace_dir().resolve()
        if not path.is_relative_to(resources_dir):
            raise PermissionError(
                f"Access denied: Write path '{rel_path}' resolves outside agents_resources directory '{resources_dir}'."
            )

        if is_path_write_blocked(path, base_dir=workspace_dir) or is_path_write_blocked(
            rel_path, base_dir=workspace_dir
        ):
            from services.audit_logger import log_audit_event

            try:
                resolved_rel = str(path.relative_to(workspace_dir).as_posix())
            except ValueError:
                resolved_rel = str(path)

            log_audit_event(
                event_type="FILE_WRITE_BLOCKED",
                actor={"type": actor_kind, "id": actor_id},
                action="write_document",
                target=rel_path,
                details={
                    "resolved_path": resolved_rel,
                    "reason": "Path is protected in system settings",
                },
                severity="CRITICAL",
                status="BLOCKED",
            )
            return json.dumps(
                {"error": f"Permission denied: Writing to protected path '{rel_path}' is strictly prohibited."}
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as tmp_f:
                tmp_f.write(content)
                tmp_f.flush()
                os.fsync(tmp_f.fileno())
            os.replace(tmp_path, path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
        bytes_written = len(content.encode("utf-8"))
        from services.audit_logger import log_audit_event

        log_audit_event(
            event_type="TOOL_EXECUTE_FILE_WRITE",
            actor={"type": actor_kind, "id": actor_id},
            action="write_document",
            target=rel_path,
            details={"bytes_written": bytes_written},
            severity="WARNING",
            status="SUCCESS",
        )
        return json.dumps(
            {
                "success": True,
                "document": rel_path,
                "bytes_written": bytes_written,
            }
        )

    except PermissionError as e:
        return json.dumps({"error": str(e)})
    except IsADirectoryError:
        return json.dumps({"error": f"Resource path '{rel_path}' is a directory, cannot write document."})
    except UnicodeError as e:
        return json.dumps({"error": f"Encoding error writing document '{rel_path}': {e}"})
    except (ValueError, TypeError) as e:
        return json.dumps({"error": f"Invalid argument: {e}"})
    except OSError as e:
        return json.dumps({"error": f"Operating system error writing document '{rel_path}': {e}"})


# LiteLLM compatible tool schemas (OpenAI / Function calling standard)
LOCAL_TOOLS_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the live web for recent information, market trends, competitor products, news, and benchmarks. Returns a list of results with title, URL, and snippet.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query string to look up on the web.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of search results to return (1 to 10). Defaults to 5.",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_web_page",
            "description": "Fetch and extract clean readable text content from a web page URL (e.g. competitor website, pricing page, documentation). Strips HTML tags, navigation, scripts, and styling.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL of the webpage to scrape and read.",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum number of characters of text content to extract. Defaults to 4000.",
                        "default": 4000,
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_documents",
            "description": "List files and documents inside the default resources directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "rel_path": {
                        "type": "string",
                        "description": "Relative path to list within resources. Defaults to '.' for resources root.",
                        "default": ".",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_document",
            "description": "Read text or PDF content of a document or file inside the resources directory (the default search location for documents).",
            "parameters": {
                "type": "object",
                "properties": {
                    "rel_path": {
                        "type": "string",
                        "description": "Relative path or filename of the document to read within resources (the default search location).",
                    }
                },
                "required": ["rel_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_document",
            "description": "Write or overwrite text content of a document inside the default resources directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "rel_path": {
                        "type": "string",
                        "description": "Relative path or filename of the document to write within resources (the default working folder).",
                    },
                    "content": {
                        "type": "string",
                        "description": "Text content to write into the document.",
                    },
                },
                "required": ["rel_path", "content"],
            },
        },
    },
]


TOOL_DISPATCHER: dict[str, Any] = {
    "list_documents": lambda args, **kw: list_documents(
        args.get("rel_path", "."),
        principal=kw.get("principal"),
        authorized_skills=kw.get("authorized_skills"),
    ),
    "read_document": lambda args, **kw: read_document(
        args.get("rel_path", ""),
        principal=kw.get("principal"),
        authorized_agents=kw.get("authorized_agents"),
        authorized_skills=kw.get("authorized_skills"),
    ),
    "write_document": lambda args, **kw: write_document(
        args.get("rel_path", ""),
        args.get("content", ""),
        principal=kw.get("principal"),
        authorized_skills=kw.get("authorized_skills"),
    ),
    "web_search": lambda args, **kw: web_search(query=args.get("query", ""), max_results=args.get("max_results", 5)),
    "fetch_web_page": lambda args, **kw: fetch_web_page(url=args.get("url", ""), max_chars=args.get("max_chars", 4000)),
}


def is_agent_authorized_for_tool(
    agent_name: ToolPrincipal | str | None,
    tool_name: str,
    authorized_skills: list[str] | set[str] | str | None = None,
    skill_registry: Any = None,
) -> bool:
    """
    Validates whether an agent or system principal has authorization to execute the requested tool.
    Delegates directly to skills.policy.is_principal_authorized_for_tool as the single source of truth.
    """
    from skills.policy import is_principal_authorized_for_tool

    return is_principal_authorized_for_tool(
        principal=agent_name,
        tool_name=tool_name,
        authorized_skills=authorized_skills,
        skill_registry=skill_registry,
    )


def _mask_tool_args(args_dict: dict[str, Any]) -> dict[str, Any]:
    sensitive_keys = {"token", "secret", "password", "key", "auth", "credential", "api_key", "cookie"}
    masked = {}
    for k, v in args_dict.items():
        k_str = str(k).lower()
        if any(s in k_str for s in sensitive_keys):
            masked[k] = "***MASKED***"
        elif isinstance(v, str) and len(v) > 200:
            masked[k] = f"{v[:100]}... [truncated {len(v)} chars]"
        elif isinstance(v, dict):
            masked[k] = _mask_tool_args(v)
        else:
            masked[k] = v
    return masked


async def execute_local_tool(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    agent_name: ToolPrincipal | str | None = None,
    authorized_skills: list[str] | set[str] | str | None = None,
) -> str:
    """
    Executes a requested native local tool via dispatcher dictionary and returns the JSON string result.
    Enforces principal tool authorization unconditionally (defense-in-depth).
    """
    raw_args = arguments or {}
    args_hash = hashlib.sha256(json.dumps(raw_args, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    actor_id = agent_name.id if hasattr(agent_name, "id") else str(agent_name or "unknown")
    actor_kind = getattr(agent_name, "kind", "agent")
    logger.info(
        f"[Tool Call] Executing '{tool_name}' keys={list(raw_args.keys())} args_sha256={args_hash} (agent: {actor_id})"
    )
    logger.debug(f"[Tool Call Details] '{tool_name}' masked args: {_mask_tool_args(raw_args)}")

    if not is_agent_authorized_for_tool(agent_name, tool_name, authorized_skills=authorized_skills):
        logger.warning(
            f"[LOCAL_TOOLS] Access denied: Agent/Principal '{actor_id}' is not authorized to execute tool '{tool_name}'."
        )
        from services.audit_logger import log_audit_event_async

        await log_audit_event_async(
            event_type="SECURITY_POLICY_VIOLATION",
            actor={"type": actor_kind, "id": actor_id},
            action="execute_local_tool",
            target=f"tool:{tool_name}",
            details={"reason": "Defense-in-depth authorization check failed", "args_sha256": args_hash},
            severity="CRITICAL",
            status="BLOCKED",
        )
        return json.dumps(
            {"error": f"Permission denied: Agent '{actor_id}' is not authorized to execute tool '{tool_name}'."}
        )

    is_network_tool = tool_name in ("web_search", "fetch_web_page")
    handler = TOOL_DISPATCHER.get(tool_name)

    if handler:
        result_str: str
        exec_error: Exception | None = None
        try:
            sig = inspect.signature(handler)
            kwargs = {}
            if "principal" in sig.parameters or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            ):
                kwargs["principal"] = agent_name
            if "authorized_skills" in sig.parameters or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            ):
                kwargs["authorized_skills"] = authorized_skills

            if inspect.iscoroutinefunction(handler):
                result_str = await handler(raw_args, **kwargs)
            elif tool_name in ("list_documents", "read_document", "write_document"):
                result_str = await asyncio.to_thread(handler, raw_args, **kwargs)
            else:
                res = handler(raw_args, **kwargs)
                if inspect.isawaitable(res):
                    result_str = await res
                else:
                    result_str = res
        except Exception as e:  # noqa: BLE001
            exec_error = e
            logger.error(f"[LOCAL_TOOLS] Error executing tool {tool_name}: {e}")
            result_str = json.dumps({"error": f"Tool execution error: {e}"})

        if is_network_tool:
            net_status = "SUCCESS"
            net_severity = "INFO"
            if exec_error is not None:
                net_status = "FAILURE"
                net_severity = "WARNING"
            elif isinstance(result_str, str):
                lower_res = result_str.lower()
                if (
                    "blocked" in lower_res
                    or "ssrf" in lower_res
                    or "security policy" in lower_res
                    or "permission denied" in lower_res
                ):
                    net_status = "BLOCKED"
                    net_severity = "CRITICAL"
                elif result_str.lstrip().startswith("{"):
                    try:
                        parsed_net = json.loads(result_str)
                        if isinstance(parsed_net, dict) and (
                            parsed_net.get("isError") is True or "error" in parsed_net
                        ):
                            net_status = "FAILURE"
                            net_severity = "WARNING"
                    except (ValueError, TypeError):
                        pass

            from services.audit_logger import log_audit_event_async

            await log_audit_event_async(
                event_type="TOOL_EXECUTE_NETWORK",
                actor={"type": actor_kind, "id": actor_id},
                action=tool_name,
                target=str(raw_args.get("url") or raw_args.get("query") or "network"),
                details={"args_keys": list(raw_args.keys()), "args_sha256": args_hash},
                severity=net_severity,
                status=net_status,
            )

        return result_str
    return json.dumps({"error": f"Unknown tool: {tool_name}"})
