"""
Tool status message formatting for in-flight tool operations.
"""

from __future__ import annotations

import json
from typing import Any


def _build_tool_status_text(username: str, tool_calls: list[Any]) -> str:
    """Builds a user-friendly progress text for in-flight tool operations."""
    if not tool_calls:
        return f"_⏳ *{username}* analizira podatke..._"
    first_tool = tool_calls[0]
    fn_name = (
        getattr(first_tool.function, "name", "")
        if hasattr(first_tool, "function")
        else ""
    )
    raw_args = (
        getattr(first_tool.function, "arguments", {})
        if hasattr(first_tool, "function")
        else {}
    )
    if isinstance(raw_args, str):
        try:
            fn_args = json.loads(raw_args)
        except Exception:
            fn_args = {}
    elif isinstance(raw_args, dict):
        fn_args = raw_args
    else:
        fn_args = {}

    if fn_name == "web_search":
        q = fn_args.get("query", "")
        query_str = f': "{q}"' if q else ""
        return f"_🔍 *{username}* pretražuje web{query_str}..._"
    elif fn_name in ("fetch_web_page", "web_scrape"):
        url = fn_args.get("url", "")
        url_str = f": {url}" if url else ""
        return f"_🌐 *{username}* dohvaća web stranicu{url_str}..._"
    elif fn_name == "load_skill":
        skill = fn_args.get("skill_name", "")
        skill_str = f": {skill}" if skill else ""
        return f"_⚡ *{username}* učitava vještinu{skill_str}..._"
    elif fn_name:
        return f"_⏳ *{username}* izvršava radnju ({fn_name})..._"
    return f"_⏳ *{username}* analizira podatke..._"
