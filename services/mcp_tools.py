import json
import logging
from pathlib import Path
from typing import Any

from core.config import MCP_WORKSPACE_DIR
from orchestration.app_settings import is_path_write_blocked

from .web_tools import fetch_web_page, web_search

logger = logging.getLogger(__name__)


# Project root directory: one level above the 'services' folder where mcp_tools.py resides
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Workspace root directory defaults to PROJECT_ROOT
WORKSPACE_DIR = (
    MCP_WORKSPACE_DIR.resolve()
    if (isinstance(MCP_WORKSPACE_DIR, Path) and MCP_WORKSPACE_DIR.exists())
    else PROJECT_ROOT
)

# Resources directory sandboxed for non-code document reading & authoring
RESOURCES_DIR = (WORKSPACE_DIR / "resources").resolve()
RESOURCES_DIR.mkdir(parents=True, exist_ok=True)


def _resolve_resource_path(rel_path: str) -> Path:
    """
    Ensures the requested document path stays strictly within the 'resources/' directory
    to prevent sandbox escape.
    Automatically normalizes paths by stripping any leading 'resources/' or 'resources\\' prefix
    so that both 'report.txt' and 'resources/report.txt' resolve safely into RESOURCES_DIR.
    """
    clean_path = str(rel_path or "").strip().replace("\\", "/")
    if clean_path.lower().startswith("resources/"):
        clean_path = clean_path[len("resources/"):].lstrip("/")
    elif clean_path.lower() == "resources" or not clean_path:
        clean_path = "."

    target = (RESOURCES_DIR / clean_path).resolve()
    if not target.is_relative_to(RESOURCES_DIR):
        raise PermissionError(
            f"Access denied: Path '{rel_path}' is outside resources directory '{RESOURCES_DIR}'."
        )
    return target


def find_resource_file(filename_or_rel_path: str) -> Path | None:
    """
    Searches for a document within RESOURCES_DIR.
    1. Direct resolution via _resolve_resource_path
    2. Case-insensitive exact match in RESOURCES_DIR root
    3. Case-insensitive recursive match across all subdirectories of RESOURCES_DIR
    """
    if not filename_or_rel_path:
        return None
    try:
        direct = _resolve_resource_path(filename_or_rel_path)
        if direct.exists() and direct.is_file():
            return direct
    except (OSError, ValueError) as e:
        logger.debug(f"[RESOURCES] Direct path resolution skipped for '{filename_or_rel_path}': {e}")

    clean_name = Path(str(filename_or_rel_path).strip().replace("\\", "/")).name.lower()
    if not clean_name:
        return None

    try:
        if RESOURCES_DIR.exists():
            for item in RESOURCES_DIR.iterdir():
                if item.is_file() and item.name.lower() == clean_name:
                    return item
            for item in RESOURCES_DIR.rglob("*"):
                if item.is_file() and item.name.lower() == clean_name:
                    return item
    except OSError as e:
        logger.debug(f"[RESOURCES] Error searching for resource file '{filename_or_rel_path}': {e}")

    return None


def _resolve_safe_path(rel_path: str) -> Path:
    """
    Ensures the requested file/directory path stays within the workspace boundary (PROJECT_ROOT by default)
    to prevent directory traversal attacks.
    """
    base_dir = (
        MCP_WORKSPACE_DIR.resolve()
        if (isinstance(MCP_WORKSPACE_DIR, Path) and MCP_WORKSPACE_DIR.exists())
        else PROJECT_ROOT
    )
    target = (base_dir / rel_path).resolve()
    if not target.is_relative_to(base_dir):
        raise PermissionError(
            f"Access denied: Path '{rel_path}' is outside workspace boundary '{base_dir}'."
        )
    return target


def list_directory(rel_path: str = ".") -> str:
    """
    Lists the contents of a directory inside the workspace.
    """
    try:
        path = _resolve_safe_path(rel_path)
        if not path.exists():
            return json.dumps({"error": f"Directory '{rel_path}' does not exist."})
        if not path.is_dir():
            return json.dumps({"error": f"Path '{rel_path}' is not a directory."})

        items = []
        for child in path.iterdir():
            items.append(
                {
                    "name": child.name,
                    "type": "directory" if child.is_dir() else "file",
                    "size_bytes": child.stat().st_size if child.is_file() else None,
                }
            )
        return json.dumps({"directory": rel_path, "contents": items}, indent=2)
    except PermissionError as e:
        return json.dumps({"error": str(e)})
    except FileNotFoundError:
        return json.dumps({"error": f"Directory '{rel_path}' does not exist."})
    except NotADirectoryError:
        return json.dumps({"error": f"Path '{rel_path}' is not a directory."})
    except (ValueError, TypeError) as e:
        return json.dumps({"error": f"Invalid path '{rel_path}': {e}"})
    except OSError as e:
        return json.dumps({"error": f"Operating system error: {e}"})


def read_file(rel_path: str) -> str:
    """
    Reads the content of a text file inside the workspace.
    If not found in the root workspace, seamlessly checks the default resources/ directory.
    """
    try:
        path = _resolve_safe_path(rel_path)
        if not path.exists() or not path.is_file():
            # Fallback to resources/ if file is not found in root workspace
            res_path = find_resource_file(rel_path)
            if res_path and res_path.exists() and res_path.is_file():
                path = res_path
            elif not path.exists():
                return json.dumps({"error": f"File '{rel_path}' does not exist."})
            elif not path.is_file():
                return json.dumps({"error": f"Path '{rel_path}' is not a file."})

        content = path.read_text(encoding="utf-8", errors="replace")
        return json.dumps({"file": rel_path, "content": content})
    except PermissionError as e:
        return json.dumps({"error": str(e)})
    except FileNotFoundError:
        return json.dumps({"error": f"File '{rel_path}' does not exist."})
    except IsADirectoryError:
        return json.dumps({"error": f"Path '{rel_path}' is a directory, not a file."})
    except UnicodeError as e:
        return json.dumps({"error": f"Encoding error reading file '{rel_path}': {e}"})
    except (ValueError, TypeError) as e:
        return json.dumps({"error": f"Invalid argument: {e}"})
    except OSError as e:
        return json.dumps({"error": f"Operating system error reading file '{rel_path}': {e}"})


def write_file(rel_path: str, content: str) -> str:
    """
    Writes or overwrites content to a text file inside the workspace.
    """
    try:
        path = _resolve_safe_path(rel_path)
        if is_path_write_blocked(path, base_dir=WORKSPACE_DIR):
            return json.dumps({
                "error": f"Permission denied: Writing to protected path '{rel_path}' is strictly prohibited."
            })
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return json.dumps(
            {
                "success": True,
                "file": rel_path,
                "bytes_written": len(content.encode("utf-8")),
            }
        )
    except PermissionError as e:
        return json.dumps({"error": str(e)})
    except IsADirectoryError:
        return json.dumps({"error": f"Path '{rel_path}' is a directory, cannot write file."})
    except UnicodeError as e:
        return json.dumps({"error": f"Encoding error writing file '{rel_path}': {e}"})
    except (ValueError, TypeError) as e:
        return json.dumps({"error": f"Invalid argument: {e}"})
    except OSError as e:
        return json.dumps({"error": f"Operating system error writing file '{rel_path}': {e}"})


def list_documents(rel_path: str = ".") -> str:
    """
    Lists the contents of a directory inside the resources/ directory.
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
    Reads the content of a text or PDF document inside the resources/ directory.
    resources/ is the default working and search directory even if omitted in rel_path.
    """
    from orchestration.system_settings import (
        get_max_file_size_bytes,
        get_max_file_size_mb,
    )

    max_size_bytes = get_max_file_size_bytes()
    max_size_mb = get_max_file_size_mb()

    try:
        path = _resolve_resource_path(rel_path)
        if not path.exists() or not path.is_file():
            fallback = find_resource_file(rel_path)
            if fallback and fallback.exists() and fallback.is_file():
                path = fallback
            elif not path.exists():
                return json.dumps({"error": f"Document '{rel_path}' does not exist in resources."})
            elif not path.is_file():
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
        return json.dumps({"error": f"Document '{rel_path}' does not exist in resources."})
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
    Writes or overwrites content to a text document inside the resources/ directory.
    """
    try:
        path = _resolve_resource_path(rel_path)
        if is_path_write_blocked(path, base_dir=WORKSPACE_DIR):
            return json.dumps({
                "error": f"Permission denied: Writing to protected path '{rel_path}' is strictly prohibited."
            })
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return json.dumps(
            {
                "success": True,
                "document": rel_path,
                "bytes_written": len(content.encode("utf-8")),
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
            "name": "list_directory",
            "description": "List files and subdirectories in a workspace directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "rel_path": {
                        "type": "string",
                        "description": "Relative path to list within the workspace. Defaults to '.' for root.",
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
            "name": "read_file",
            "description": "Read text content of a file inside the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "rel_path": {
                        "type": "string",
                        "description": "Relative path of the file to read.",
                    }
                },
                "required": ["rel_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write text content to a file inside the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "rel_path": {
                        "type": "string",
                        "description": "Relative path of the file to write.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Text content to write into the file.",
                    },
                },
                "required": ["rel_path", "content"],
            },
        },
    },
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


def execute_mcp_tool(tool_name: str, arguments: dict[str, Any], agent_name: str = "") -> str:
    """
    Executes a requested native MCP tool and returns the JSON string result.
    """
    logger.info(f"Executing MCP tool: {tool_name} with args: {arguments} (agent: {agent_name})")
    if tool_name == "list_directory":
        rel_path = arguments.get("rel_path", ".")
        return list_directory(rel_path)
    elif tool_name == "read_file":
        rel_path = arguments.get("rel_path", "")
        return read_file(rel_path)
    elif tool_name == "write_file":
        rel_path = arguments.get("rel_path", "")
        content = arguments.get("content", "")
        return write_file(rel_path, content)
    elif tool_name == "list_documents":
        rel_path = arguments.get("rel_path", ".")
        return list_documents(rel_path)
    elif tool_name == "read_document":
        rel_path = arguments.get("rel_path", "")
        return read_document(rel_path)
    elif tool_name == "write_document":
        rel_path = arguments.get("rel_path", "")
        content = arguments.get("content", "")
        return write_document(rel_path, content)
    elif tool_name == "web_search":
        query = arguments.get("query", "")
        max_results = arguments.get("max_results", 5)
        return web_search(query=query, max_results=max_results)
    elif tool_name in ("fetch_web_page", "web_scrape"):
        url = arguments.get("url", "")
        max_chars = arguments.get("max_chars", 4000)
        return fetch_web_page(url=url, max_chars=max_chars)
    else:
        return json.dumps({"error": f"Unknown tool: {tool_name}"})
