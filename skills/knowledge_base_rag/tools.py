import asyncio
import concurrent.futures
import json
import logging
from typing import Any

from services.rag.config import DEFAULT_TOP_K
from services.rag.service import rag_service

logger = logging.getLogger(__name__)

# LiteLLM compatible tool schema for search_knowledge_rag
SEARCH_KNOWLEDGE_RAG_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_knowledge_rag",
        "description": (
            "Search verified documentation, architecture decisions, and agent-specific "
            "runbooks using hybrid dense vector + BM25 full-text retrieval."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language query or technical keyword to look up.",
                },
                "scope": {
                    "type": "string",
                    "enum": ["all", "self"],
                    "description": (
                        "'all' searches both agent-specific and shared organization documents. "
                        "'self' restricts search strictly to the current agent's domain."
                    ),
                    "default": "all",
                },
                "max_results": {
                    "type": "integer",
                    "description": f"Maximum number of excerpts to return (default: {DEFAULT_TOP_K}).",
                    "default": DEFAULT_TOP_K,
                },
            },
            "required": ["query"],
        },
    },
}

TOOL_SCHEMAS = [SEARCH_KNOWLEDGE_RAG_SCHEMA]


def _run_coroutine_sync(coro: Any) -> Any:
    """Helper to run an async coroutine synchronously from sync tool execution."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # Running inside an active loop (e.g. from async caller)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    else:
        return asyncio.run(coro)


def execute_search_knowledge_rag(arguments: dict[str, Any], agent_name: str = "") -> str:
    """Executes search_knowledge_rag with given arguments."""
    query = arguments.get("query", "")
    scope = arguments.get("scope", "all")
    max_results = arguments.get("max_results", DEFAULT_TOP_K)

    try:
        result = _run_coroutine_sync(
            rag_service.search(
                query=query,
                agent_name=agent_name,
                scope=scope,
                max_results=max_results,
            )
        )
        from core.security_utils import wrap_untrusted_content
        return wrap_untrusted_content(result)
    except Exception as e:
        logger.error(f"[search_knowledge_rag] Tool execution failed: {e}")
        return f"Error executing knowledge search: {e}"


# Tool execution dispatch mapping
TOOL_HANDLERS = {
    "search_knowledge_rag": execute_search_knowledge_rag,
}
