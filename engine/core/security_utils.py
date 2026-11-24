import json
import re
from typing import Any

_PLAIN_TOOL_ERROR_PREFIXES: tuple[str, ...] = (
    "error:",
    "error executing ",
    "tool execution error:",
    "permission denied",
    "permissionerror:",
)


def is_tool_error_payload(result: Any) -> bool:
    """Returns True when a tool result represents an exception, permission denial, or structured error."""
    if isinstance(result, BaseException):
        return True
    if isinstance(result, dict):
        return bool(
            result.get("isError")
            or result.get("is_error")
            or result.get("error")
            or result.get("error_code")
        )
    if isinstance(result, str):
        stripped = result.strip()
        if not stripped:
            return False
        lowered = stripped.lower()
        if lowered.startswith(_PLAIN_TOOL_ERROR_PREFIXES):
            return True
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                return False
            if isinstance(parsed, dict):
                return bool(
                    parsed.get("isError")
                    or parsed.get("is_error")
                    or parsed.get("error")
                    or parsed.get("error_code")
                )
    return False


def wrap_untrusted_content(text: str) -> str:
    """Wraps untrusted external data to prevent indirect prompt injection."""
    if not text:
        return ""
    # Strip any existing tags to prevent injection of closing tags or boundary breakouts
    clean_text = str(text)
    pattern = re.compile(
        r"<\s*/?\s*untrusted_file_content(?:\s+[^>\"']*(?:\"[^\"]*\"|'[^']*')*)*\s*/?>|"
        r"<\s*/?\s*untrusted_file_content[^>]*>",
        re.IGNORECASE,
    )
    prev = None
    while prev != clean_text:
        prev = clean_text
        clean_text = pattern.sub("", clean_text)
    return f"<untrusted_file_content>\n{clean_text}\n</untrusted_file_content>"
