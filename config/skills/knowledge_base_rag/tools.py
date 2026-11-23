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
                    "description": f"Maximum number of excerpts to return (1-10, default: {DEFAULT_TOP_K}).",
                    "minimum": 1,
                    "maximum": 10,
                    "default": DEFAULT_TOP_K,
                },
            },
            "required": ["query"],
        },
    },
}

TOOL_SCHEMAS = [SEARCH_KNOWLEDGE_RAG_SCHEMA]


async def async_execute_search_knowledge_rag(
    arguments: dict[str, Any],
    agent_name: str = "",
    token_mapping: dict[str, str] | None = None,
) -> str:
    """Asynchronously executes search_knowledge_rag directly on active event loop."""
    query = arguments.get("query", "")
    scope = arguments.get("scope", "all")
    raw_max_results = arguments.get("max_results", DEFAULT_TOP_K)
    try:
        max_results = int(raw_max_results)
    except (ValueError, TypeError):
        max_results = DEFAULT_TOP_K
    max_results = max(1, min(10, max_results))

    try:
        result = await rag_service.search(
            query=query,
            agent_name=agent_name,
            scope=scope,
            max_results=max_results,
            token_mapping=token_mapping,
        )


        return result
    except Exception as e:  # noqa: BLE001
        logger.error(f"[search_knowledge_rag] Async tool execution failed: {e}")
        return f"Error executing knowledge search: {e}"


# Tool execution dispatch mapping (native async tool handler)
TOOL_HANDLERS = {
    "search_knowledge_rag": async_execute_search_knowledge_rag,
}

