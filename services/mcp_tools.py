import hashlib
import json
import logging
from pathlib import Path
import re
from typing import Any

from core.config import MCP_WORKSPACE_DIR
from core.domain import ToolPrincipal
from core.paths import (
    AGENTS_RESOURCES_DIR as CANONICAL_AGENTS_RESOURCES_DIR,
    PROJECT_ROOT,
    SYSTEM_RESOURCES_DIR as CANONICAL_SYSTEM_RESOURCES_DIR,
)

from .web_tools import fetch_web_page, web_search

logger = logging.getLogger(__name__)


# Workspace root directory defaults to PROJECT_ROOT
WORKSPACE_DIR = (
    MCP_WORKSPACE_DIR.resolve()
    if (isinstance(MCP_WORKSPACE_DIR, Path) and MCP_WORKSPACE_DIR.exists())
    else PROJECT_ROOT
)

# Agents and system resources directories (no import-time mkdir side effects)
AGENTS_RESOURCES_DIR = (
    (WORKSPACE_DIR / "agents_resources").resolve()
    if WORKSPACE_DIR != PROJECT_ROOT
    else CANONICAL_AGENTS_RESOURCES_DIR
)
SYSTEM_RESOURCES_DIR = (
    (WORKSPACE_DIR / "system_resources").resolve()
    if WORKSPACE_DIR != PROJECT_ROOT
    else CANONICAL_SYSTEM_RESOURCES_DIR
)


def validate_resource_filename(rel_path: str) -> None:
    """
    Validates a resource relative path to prevent directory traversal, Windows device name abuse,
    NTFS alternate data streams, and forbidden characters.
    """
    clean = str(rel_path or "").strip()
    if not clean:
        raise ValueError("Resource path cannot be empty.")
    if "\x00" in clean:
        raise ValueError("NUL byte is not permitted in resource paths.")
    if ":" in clean:
        raise ValueError("Colon character ':' is not permitted in resource paths.")
    if clean.startswith(("/", "\\")):
        raise ValueError("Absolute or root-prefixed resource paths are not permitted.")

    norm = clean.replace("\\", "/")
    if norm.lower().startswith("agents_resources/"):
        norm = norm[len("agents_resources/"):].lstrip("/")
    elif norm.lower() == "agents_resources":
        norm = "."

    segments = re.split(r"[/\\]+", norm)
    reserved_devices = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
    segment_pattern = re.compile(r"^[A-Za-z0-9._ -]{1,128}$")

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
        stripped = clean_raw[len("agents_resources/"):].lstrip("/")
    elif clean_raw.lower() == "agents_resources":
        stripped = "."
    else:
        stripped = clean_raw

    if stripped and stripped != ".":
        validate_resource_filename(stripped)

    clean_path = stripped or "."
    target = (AGENTS_RESOURCES_DIR / clean_path).resolve()
    if not target.is_relative_to(AGENTS_RESOURCES_DIR):
        raise PermissionError(
            f"Access denied: Path '{rel_path}' is outside agents_resources directory '{AGENTS_RESOURCES_DIR}'."
        )
    return target



def list_documents(rel_path: str = ".") -> str:
    """
    Lists the contents of a directory inside the agents_resources/ directory.
    """
    try:
        path = _resolve_resource_path(rel_path)
        if not path.exists():
            return json.dumps({"error": f"Resource directory '{rel_path}' does not exist."})
        if not path.is_dir():
            return json.dumps({"error": f"Resource path '{rel_path}' is not a directory."})

        contents = []
        for item in sorted(path.iterdir()):
            contents.append({
                "name": item.name,
                "is_dir": item.is_dir(),
                "size_bytes": item.stat().st_size if item.is_file() else None,
            })
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


def read_document(rel_path: str) -> str:
    """
    Reads the content of a text or PDF document inside the agents_resources/ directory.
    agents_resources/ is the default working and search directory even if omitted in rel_path.
    """
    from orchestration.system_settings import (
        get_max_file_size_bytes,
        get_max_file_size_mb,
    )

    max_size_bytes = get_max_file_size_bytes()
    max_size_mb = get_max_file_size_mb()

    try:
        path = _resolve_resource_path(rel_path)
        if not path.exists():
            return json.dumps({"error": f"Document '{rel_path}' does not exist in agents_resources."})
        if not path.is_file():
            return json.dumps({"error": f"Resource path '{rel_path}' is not a file."})

        file_size = path.stat().st_size
        if file_size > max_size_bytes:
            return json.dumps({
                "error": f"File '{rel_path}' ({file_size} bytes) exceeds maximum allowed size of {max_size_mb:.1f} MB."
            })

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
                content = "\n\n".join(pages_text) if pages_text else "[Empty PDF document]"
                return json.dumps({
                    "document": rel_path,
                    "content": content,
                    "pages": len(reader.pages),
                })
            except ImportError:
                return json.dumps({
                    "error": "The 'pypdf' package is required to read PDF files. Please install pypdf."
                })
            except Exception as e:  # noqa: BLE001 - PDF parser error boundary returns failure JSON
                return json.dumps({"error": f"Failed to parse PDF '{rel_path}': {e}"})

        content = path.read_text(encoding="utf-8", errors="replace")
        return json.dumps({"document": rel_path, "content": content})
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


def write_document(rel_path: str, content: str) -> str:
    """
    Writes or overwrites content to a text document inside the agents_resources/ directory.
    """
    try:
        from orchestration.system_settings import (
            get_max_write_bytes,
            is_path_write_blocked,
        )

        content_bytes = len(content.encode("utf-8"))
        max_bytes = get_max_write_bytes()
        if content_bytes > max_bytes:
            from services.audit_logger import log_audit_event

            log_audit_event(
                event_type="FILE_WRITE_BLOCKED",
                actor={"type": "agent_tool", "id": "write_document"},
                action="write_document",
                target=rel_path,
                details={"reason": f"Content size ({content_bytes} bytes) exceeds maximum permitted limit ({max_bytes} bytes)"},
                severity="CRITICAL",
                status="BLOCKED",
            )
            return json.dumps({
                "error": f"File content size ({content_bytes} bytes) exceeds maximum permitted write limit of {max_bytes} bytes."
            })

        if is_path_write_blocked(rel_path, base_dir=WORKSPACE_DIR):
            from services.audit_logger import log_audit_event

            log_audit_event(
                event_type="FILE_WRITE_BLOCKED",
                actor={"type": "agent_tool", "id": "write_document"},
                action="write_document",
                target=rel_path,
                details={"reason": "Target path matches protected path policy"},
                severity="CRITICAL",
                status="BLOCKED",
            )
            return json.dumps({
                "error": f"Permission denied: Writing to protected path '{rel_path}' is strictly prohibited."
            })

        path = _resolve_resource_path(rel_path)
        if is_path_write_blocked(path, base_dir=WORKSPACE_DIR):
            from services.audit_logger import log_audit_event

            log_audit_event(
                event_type="FILE_WRITE_BLOCKED",
                actor={"type": "agent_tool", "id": "write_document"},
                action="write_document",
                target=rel_path,
                details={"resolved_path": str(path.relative_to(WORKSPACE_DIR).as_posix()), "reason": "Path is protected in system settings"},
                severity="CRITICAL",
                status="BLOCKED",
            )
            return json.dumps({
                "error": f"Permission denied: Writing to protected path '{rel_path}' is strictly prohibited."
            })
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        bytes_written = len(content.encode("utf-8"))
        from services.audit_logger import log_audit_event

        log_audit_event(
            event_type="TOOL_EXECUTE_FILE_WRITE",
            actor={"type": "agent_tool", "id": "write_document"},
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
MCP_TOOLS_SCHEMAS = [

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

    "list_documents": lambda args: list_documents(args.get("rel_path", ".")),
    "read_document": lambda args: read_document(args.get("rel_path", "")),
    "write_document": lambda args: write_document(args.get("rel_path", ""), args.get("content", "")),
    "web_search": lambda args: web_search(query=args.get("query", ""), max_results=args.get("max_results", 5)),
    "fetch_web_page": lambda args: fetch_web_page(url=args.get("url", ""), max_chars=args.get("max_chars", 4000)),
    "web_scrape": lambda args: fetch_web_page(url=args.get("url", ""), max_chars=args.get("max_chars", 4000)),
}


def is_agent_authorized_for_tool(
    agent_name: ToolPrincipal | str | None,
    tool_name: str,
    authorized_skills: list[str] | set[str] | str | None = None,
    skill_registry: Any = None,
) -> bool:
    """
    Validates whether an agent or system principal has authorization to execute the requested tool.
    System principal (ToolPrincipal(kind="system")) or ALL_INTERNAL_SKILLS bypass this check.
    String agents ('agent-10001') are checked against assigned/dynamic skills.
    Crucial: String "system" or empty strings do NOT bypass authorization.
    """
    from core.domain import ToolPrincipal
    from skills.loader import ALL_INTERNAL_SKILLS

    if isinstance(agent_name, ToolPrincipal):
        if agent_name.kind == "system":
            return True
        agent_id = (agent_name.id or "").strip()
    elif isinstance(agent_name, str):
        agent_id = agent_name.strip()
    else:
        agent_id = ""

    is_internal = (
        authorized_skills == ALL_INTERNAL_SKILLS
        or (isinstance(authorized_skills, (list, tuple, set)) and ALL_INTERNAL_SKILLS in authorized_skills)
    )
    if is_internal:
        return True

    # If no valid agent ID provided and not system principal/internal, fail closed
    if not agent_id:
        return False

    try:
        if skill_registry is None:
            from core.registry import get_skill_registry
            skill_registry = get_skill_registry()

        # 1. Check if tool is global across all skills
        for s_def in skill_registry.list_skills():
            if getattr(s_def, "is_global", False) and tool_name in getattr(s_def, "tools", []):
                return True

        # 2. Check agent's assigned skills (combining static config with authorized thread skills)
        from orchestration.agent_settings import get_agent_skills

        static_skills = get_agent_skills(agent_id) or []
        combined_skills = set(static_skills) | set(authorized_skills or [])
        if not combined_skills:
            return False

        permitted_tools: set[str] = set()
        for s_name in combined_skills:
            s_def = skill_registry.get(s_name)
            if s_def and hasattr(s_def, "tools"):
                permitted_tools.update(s_def.tools)
            if s_def and hasattr(s_def, "custom_tool_schemas"):
                for schema in s_def.custom_tool_schemas:
                    fn = schema.get("function", {}).get("name")
                    if fn:
                        permitted_tools.add(fn)

        return tool_name in permitted_tools
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[MCP] Error verifying tool authorization for agent '{agent_id}': {e}")
        return False


def execute_mcp_tool(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    agent_name: ToolPrincipal | str | None = None,
    authorized_skills: list[str] | set[str] | str | None = None,
) -> str:
    """
    Executes a requested native MCP tool via dispatcher dictionary and returns the JSON string result.
    Enforces principal tool authorization unconditionally (defense-in-depth).
    """
    raw_args = arguments or {}
    args_hash = hashlib.sha256(json.dumps(raw_args, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    actor_id = agent_name.id if hasattr(agent_name, "id") else str(agent_name or "unknown")
    actor_kind = getattr(agent_name, "kind", "agent")
    logger.info(f"[Tool Call] Executing '{tool_name}' keys={list(raw_args.keys())} args_sha256={args_hash} (agent: {actor_id})")
    logger.debug(f"[Tool Call Details] '{tool_name}' full args: {raw_args}")

    if not is_agent_authorized_for_tool(agent_name, tool_name, authorized_skills=authorized_skills):
        logger.warning(
            f"[MCP] Access denied: Agent/Principal '{actor_id}' is not authorized to execute tool '{tool_name}'."
        )
        from services.audit_logger import log_audit_event

        log_audit_event(
            event_type="SECURITY_POLICY_VIOLATION",
            actor={"type": actor_kind, "id": actor_id},
            action="execute_mcp_tool",
            target=f"tool:{tool_name}",
            details={"reason": "Defense-in-depth authorization check failed", "args_sha256": args_hash},
            severity="CRITICAL",
            status="BLOCKED",
        )
        return json.dumps({
            "error": f"Permission denied: Agent '{actor_id}' is not authorized to execute tool '{tool_name}'."
        })

    if tool_name in ("web_search", "fetch_web_page"):
        from services.audit_logger import log_audit_event

        log_audit_event(
            event_type="TOOL_EXECUTE_NETWORK",
            actor={"type": actor_kind, "id": actor_id},
            action=tool_name,
            target=str(raw_args.get("url") or raw_args.get("query") or "network"),
            details={"args_keys": list(raw_args.keys()), "args_sha256": args_hash},
            severity="INFO",
            status="SUCCESS",
        )

    handler = TOOL_DISPATCHER.get(tool_name)

    if handler:
        try:
            return handler(raw_args)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[MCP] Error executing tool {tool_name}: {e}")
            return json.dumps({"error": f"Tool execution error: {e}"})
    return json.dumps({"error": f"Unknown tool: {tool_name}"})

