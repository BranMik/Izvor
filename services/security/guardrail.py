import asyncio
from collections import OrderedDict
import logging
import lancedb
from services.rag.config import LANCEDB_DIR, GUARDRAIL_THRESHOLD
from services.rag.embeddings import embedding_service

logger = logging.getLogger(__name__)

_EMBEDDING_LRU_CACHE: OrderedDict[str, list[float]] = OrderedDict()


def _get_max_embedding_cache_size() -> int:
    try:
        from orchestration.system_settings import get_guardrail_embedding_cache_size
        return get_guardrail_embedding_cache_size()
    except Exception:
        return 500


_MAX_EMBEDDING_CACHE_SIZE = _get_max_embedding_cache_size()


def _search_lance_table(table, query_vector: list[float]):
    """Synchronous LanceDB table search helper executed in background thread."""
    return table.search(query_vector).metric("cosine").limit(1).to_pandas()


class GuardrailService:
    def __init__(self, db_dir=LANCEDB_DIR, threshold: float = GUARDRAIL_THRESHOLD):
        self.db_dir = db_dir
        self.threshold = threshold
        self.table_name = "guardrail_signatures"
        self._table = None

    @property
    def table(self):
        if self._table is None:
            try:
                db = lancedb.connect(str(self.db_dir))
                tables = db.list_tables() if hasattr(db, "list_tables") else db.table_names()
                if self.table_name in tables:
                    self._table = db.open_table(self.table_name)
                else:
                    logger.warning(f"Guardrail table '{self.table_name}' not found. Seeding required.")
            except Exception as e:
                logger.error(f"Failed to connect to LanceDB guardrail table: {e}")
        return self._table

    async def _get_cached_embedding(self, text: str) -> list[float] | None:
        """Retrieves embedding vector from in-memory LRU cache or calls embedding_service."""
        if text in _EMBEDDING_LRU_CACHE:
            _EMBEDDING_LRU_CACHE.move_to_end(text)
            return _EMBEDDING_LRU_CACHE[text]

        vectors = await embedding_service.get_embeddings([text])
        if not vectors or len(vectors) == 0:
            return None

        vector = vectors[0]
        _EMBEDDING_LRU_CACHE[text] = vector
        if len(_EMBEDDING_LRU_CACHE) > _get_max_embedding_cache_size():
            _EMBEDDING_LRU_CACHE.popitem(last=False)
        return vector

    def _handle_failure(self, reason: str, text: str) -> bool:
        from orchestration.system_settings import get_guardrail_fail_mode
        from services.audit_logger import log_audit_event

        fail_mode = get_guardrail_fail_mode()
        if fail_mode == "closed":
            logger.critical(
                f"[GUARDRAIL] Fail-closed mode active: blocking prompt due to guardrail error ({reason})"
            )
            try:
                log_audit_event(
                    event_type="GUARDRAIL_FAIL_CLOSED_BLOCKED",
                    actor={"type": "system", "id": "guardrail_service"},
                    action="evaluate_prompt_injection",
                    target="guardrail_table",
                    severity="CRITICAL",
                    status="BLOCKED",
                    details={
                        "reason": reason,
                        "text_snippet": text[:100],
                        "fail_mode": "closed",
                    },
                )
            except Exception as ae:
                logger.error(f"[GUARDRAIL] Failed to emit fail-closed audit event: {ae}")
            return True
        else:
            logger.warning(
                f"[GUARDRAIL] Fail-open mode active: allowing prompt despite guardrail error ({reason})"
            )
            try:
                log_audit_event(
                    event_type="GUARDRAIL_DEGRADED_OPEN",
                    actor={"type": "system", "id": "guardrail_service"},
                    action="evaluate_prompt_injection",
                    target="guardrail_table",
                    severity="WARNING",
                    status="DEGRADED",
                    details={
                        "reason": reason,
                        "text_snippet": text[:100],
                        "fail_mode": "open",
                    },
                )
            except Exception as ae:
                logger.error(f"[GUARDRAIL] Failed to emit fail-open audit event: {ae}")
            return False

    async def is_prompt_injection(self, text: str) -> bool:
        """
        Returns True if the text closely matches a known prompt injection signature.
        """
        if not text or not text.strip():
            return False

        table = self.table
        if table is None:
            return self._handle_failure("LanceDB table connection unavailable", text)

        try:
            query_vector = await self._get_cached_embedding(text)
            if not query_vector:
                return self._handle_failure("Embedding service returned empty vector", text)

            # Search LanceDB using Cosine distance in background thread
            results = await asyncio.to_thread(_search_lance_table, table, query_vector)

            if len(results) > 0:
                # Cosine distance: 0 is identical, 1 is orthogonal
                # Similarity = 1 - distance
                distance = results.iloc[0]["_distance"]
                similarity = 1.0 - distance

                if similarity >= self.threshold:
                    matched_text = results.iloc[0]["text"]
                    logger.warning(
                        f"Guardrail triggered! Similarity: {similarity:.3f} | Matched: '{matched_text[:50]}...'"
                    )
                    return True

        except Exception as e:
            logger.error(f"Error during guardrail check: {e}")
            return self._handle_failure(str(e), text)

        return False


# Singleton instance
guardrail_service = GuardrailService()
