import asyncio
import logging

from .config import DEFAULT_TOP_K
from .embeddings import EmbeddingService, embedding_service
from .vector_store import VectorStore, vector_store

logger = logging.getLogger(__name__)

_CACHED_CITATION_INSTRUCTIONS: str | None = None


def _load_citation_instructions() -> str:
    """Loads citation and fidelity instructions from external markdown file with caching."""
    global _CACHED_CITATION_INSTRUCTIONS
    if _CACHED_CITATION_INSTRUCTIONS is not None:
        return _CACHED_CITATION_INSTRUCTIONS
    try:
        from .config import CITATION_INSTRUCTIONS_PATH

        if CITATION_INSTRUCTIONS_PATH.exists():
            _CACHED_CITATION_INSTRUCTIONS = CITATION_INSTRUCTIONS_PATH.read_text(encoding="utf-8").strip()
            return _CACHED_CITATION_INSTRUCTIONS
    except (OSError, UnicodeDecodeError) as e:
        logger.debug(f"[RAG Service] Could not load citation instructions: {e}")
    return ""


class RAGService:
    """
    High-level facade for querying the agent-oriented RAG system.
    """

    def __init__(
        self,
        store: VectorStore | None = None,
        embedder: EmbeddingService | None = None,
    ) -> None:
        self.vector_store = store or vector_store
        self.embedder = embedder or embedding_service

    async def search(
        self,
        query: str,
        agent_name: str = "",
        scope: str = "all",
        max_results: int = DEFAULT_TOP_K,
    ) -> str:
        """
        Executes hybrid search and returns a structured markdown string
        with citations (doc_id, section_header) and text excerpts.
        """
        cleaned_query = (query or "").strip()
        if not cleaned_query:
            return "Error: Search query cannot be empty."

        clean_agent_name = (
            getattr(agent_name, "id", None)
            if hasattr(agent_name, "id")
            else (str(agent_name) if agent_name is not None else "")
        )

        # Generate query vector
        query_vector = None
        try:
            vectors = await self.embedder.get_embeddings([cleaned_query])
            if vectors:
                query_vector = vectors[0]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[RAG Service] Failed to compute query vector: {e}. Falling back to BM25 text search.")

        # Execute search in vector store
        try:
            results = await asyncio.to_thread(
                self.vector_store.search,
                query_text=cleaned_query,
                query_vector=query_vector,
                agent_name=clean_agent_name,
                scope=scope,
                top_k=max_results,
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"[RAG Service] Search execution failed: {e}")
            return f"Error executing knowledge base search: {e}"

        if not results:
            scope_desc = (
                f"agent '{agent_name}' and shared knowledge" if scope == "all" else f"agent '{agent_name}' only"
            )
            return f"No relevant documentation found for '{cleaned_query}' in {scope_desc}."

        # Format results as readable markdown citations
        output_blocks = [f'### Knowledge Base Search Results for: *"{cleaned_query}"*\n']
        from services.security.pii_masker import pii_masker

        for idx, item in enumerate(results, start=1):
            doc_id = item.get("doc_id", "Unknown")
            section = item.get("section_header", "Overview")
            text = item.get("text", "").strip()
            if pii_masker.should_mask_at_retrieval():
                text, _ = await pii_masker.async_mask(text)

            output_blocks.append(f"**[{idx}] `{doc_id}` — {section}**\n> {text.replace(chr(10), chr(10) + '> ')}\n")

        instructions = _load_citation_instructions()
        if instructions:
            output_blocks.append(f"\n{instructions}")

        return "\n".join(output_blocks)


# Global singleton facade
rag_service = RAGService()
