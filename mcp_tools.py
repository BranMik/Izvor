import os
import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Tuple
from config import MCP_WORKSPACE_DIR

logger = logging.getLogger(__name__)

def _resolve_safe_path(rel_path: str) -> Path:
    """
    Ensures the requested file/directory path stays within MCP_WORKSPACE_DIR to prevent directory traversal attacks.
    """
    target = (MCP_WORKSPACE_DIR / rel_path).resolve()
    if not str(target).startswith(str(MCP_WORKSPACE_DIR)):
        raise PermissionError(f"Access denied: Path '{rel_path}' is outside workspace boundary '{MCP_WORKSPACE_DIR}'.")
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
            items.append({
                "name": child.name,
                "type": "directory" if child.is_dir() else "file",
                "size_bytes": child.stat().st_size if child.is_file() else None
            })
        return json.dumps({"directory": rel_path, "contents": items}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})

def read_file(rel_path: str) -> str:
    """
    Reads the content of a text file inside the workspace.
    """
    try:
        path = _resolve_safe_path(rel_path)
        if not path.exists():
            return json.dumps({"error": f"File '{rel_path}' does not exist."})
        if not path.is_file():
            return json.dumps({"error": f"Path '{rel_path}' is not a file."})

        content = path.read_text(encoding="utf-8", errors="replace")
        return json.dumps({"file": rel_path, "content": content})
    except Exception as e:
        return json.dumps({"error": str(e)})

def write_file(rel_path: str, content: str) -> str:
    """
    Writes or overwrites content to a text file inside the workspace.
    """
    try:
        path = _resolve_safe_path(rel_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return json.dumps({"success": True, "file": rel_path, "bytes_written": len(content.encode("utf-8"))})
    except Exception as e:
        return json.dumps({"error": str(e)})

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
                        "default": "."
                    }
                },
                "required": []
            }
        }
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
                        "description": "Relative path of the file to read."
                    }
                },
                "required": ["rel_path"]
            }
        }
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
                        "description": "Relative path of the file to write."
                    },
                    "content": {
                        "type": "string",
                        "description": "Text content to write into the file."
                    }
                },
                "required": ["rel_path", "content"]
            }
        }
    }
]

def execute_mcp_tool(tool_name: str, arguments: Dict[str, Any]) -> str:
    """
    Executes a requested native MCP tool and returns the JSON string result.
    """
    logger.info(f"Executing MCP tool: {tool_name} with args: {arguments}")
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
    else:
        return json.dumps({"error": f"Unknown tool: {tool_name}"})
