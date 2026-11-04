import asyncio
import inspect
import logging
from typing import Any

import litellm

from .config import EMBEDDING_BATCH_SIZE, EMBEDDING_MODEL

logger = logging.getLogger(__name__)


class EmbeddingService:
    """
    Asynchronous embedding generator using LiteLLM to interface
    with Gemini (or OpenAI / local models) with batching.
    """

    _get_embeddings_override: Any = None

    @classmethod
    def configure_embeddings_fn(cls, fn: Any) -> None:
        cls._get_embeddings_override = fn

    @classmethod
    def reset_embeddings_fn(cls) -> None:
        cls._get_embeddings_override = None

    def __init__(self, model: str = EMBEDDING_MODEL) -> None:
        self.model = model

    async def get_embeddings(self, texts: list[str]) -> list[list[float]]:
        """
        Embeds a list of texts in batches and returns list of embedding float vectors.
        """
        if self._get_embeddings_override is not None:
            res = self._get_embeddings_override(texts)
            if inspect.isawaitable(res):
                return await res
            return res

        if not texts:
            return []

        # Clean empty texts
        cleaned_texts = [t if t.strip() else " " for t in texts]
        results: list[list[float]] = []

        for i in range(0, len(cleaned_texts), EMBEDDING_BATCH_SIZE):
            batch = cleaned_texts[i : i + EMBEDDING_BATCH_SIZE]
            batch_vectors = await self._embed_batch(batch)
            results.extend(batch_vectors)

        return results

    async def _embed_batch(self, batch: list[str], max_retries: int = 3) -> list[list[float]]:
        from core.llm_utils import execute_llm_with_retry

        try:
            response: Any = await execute_llm_with_retry(
                func=litellm.aembedding,
                kwargs={
                    "model": self.model,
                    "input": batch,
                },
                agent_key="rag_embeddings",
                thread_key=("system", "embeddings"),
                max_attempts=max_retries,
            )
        except Exception as e:
            logger.error(f"[RAG Embeddings] All {max_retries} embedding attempts failed: {e}")
            raise RuntimeError(f"Embedding generation failed: {e}") from e

        try:
            from pipeline.spend_governance import spend_governance

            usage = getattr(response, "usage", None)
            p_toks = getattr(usage, "prompt_tokens", 0) or getattr(usage, "total_tokens", 0) or 0
            if p_toks > 0:
                spend_governance.record_usage(
                    session_id="system:embeddings",
                    model_name=self.model,
                    prompt_tokens=p_toks,
                    completion_tokens=0,
                )
        except Exception as rec_err:  # noqa: BLE001
            logger.debug(f"[RAG Embeddings] Failed to record spend ledger: {rec_err}")

        return [item["embedding"] for item in response.data]


# Singleton instance
embedding_service = EmbeddingService()
