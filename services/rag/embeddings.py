import asyncio
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

    def __init__(self, model: str = EMBEDDING_MODEL) -> None:
        self.model = model

    async def get_embeddings(self, texts: list[str]) -> list[list[float]]:
        """
        Embeds a list of texts in batches and returns list of embedding float vectors.
        """
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
        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                # liteLLM accepts text-embedding-004 or gemini/text-embedding-004
                response: Any = await litellm.aembedding(
                    model=self.model,
                    input=batch,
                )
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
            except Exception as e:  # noqa: BLE001
                last_error = e
                logger.warning(
                    f"[RAG Embeddings] Batch embedding attempt {attempt}/{max_retries} failed: {e}"
                )
                if attempt < max_retries:
                    await asyncio.sleep(2**attempt)

        logger.error(f"[RAG Embeddings] All {max_retries} embedding attempts failed: {last_error}")
        raise RuntimeError(f"Embedding generation failed: {last_error}") from last_error


# Singleton instance
embedding_service = EmbeddingService()
