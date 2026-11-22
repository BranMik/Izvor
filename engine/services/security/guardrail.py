import asyncio
from collections import OrderedDict
import hashlib
import logging
from typing import Any

import lancedb
from services.rag.config import LANCEDB_DIR, GUARDRAIL_THRESHOLD
from services.rag.embeddings import embedding_service

logger = logging.getLogger(__name__)

_EMBEDDING_LRU_CACHE: OrderedDict[str, list[float]] = OrderedDict()


def _text_audit_metadata(text: str) -> dict[str, Any]:
    """Returns non-sensitive length and truncated SHA-256 digest instead of raw prompt text."""
    return {
        "text_length": len(text),
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
    }


def _emit_guardrail_audit(
    *,
    event_type: str,
    action: str,
    severity: str,
    status: str,
    details: dict[str, Any],
) -> None:
    """Best-effort audit emission for guardrail security and degradation events."""
    try:
        from services.audit_logger import log_audit_event

        log_audit_event(
            event_type=event_type,
            actor={"type": "system", "id": "guardrail_service"},
            action=action,
            target="guardrail_table",
            severity=severity,
            status=status,
            details=details,
        )
    except Exception as ae:  # noqa: BLE001
        logger.error(f"[GUARDRAIL] Failed to emit {event_type} audit event: {ae}")


def _get_max_embedding_cache_size() -> int:
    try:
        from orchestration.system_settings import get_guardrail_embedding_cache_size
        return get_guardrail_embedding_cache_size()
    except Exception:  # noqa: BLE001
        return 500


def _search_lance_table(table, query_vector: list[float]):
    """Synchronous LanceDB table search helper executed in background thread."""
    return table.search(query_vector).metric("cosine").limit(1).to_pandas()


# When the guardrail table is missing, re-check at most every N accesses instead of
# reconnecting on every prompt, while still picking up a table seeded after startup.
MISSING_TABLE_RETRY_INTERVAL = 20


class GuardrailService:
    def __init__(
        self,
        db_dir=LANCEDB_DIR,
        threshold: float = GUARDRAIL_THRESHOLD,
        connect_fn=None,
        table=None,
    ):
        self.default_db_dir = db_dir
        self.default_table = table
        self.db_dir = db_dir
        self.threshold = threshold
        self.table_name = "guardrail_signatures"
        self._table = table
        self._connect_fn = connect_fn or lancedb.connect
        self._table_missing = False
        self._missing_checks = 0

    def configure_paths(self, db_dir=None, table=None) -> None:
        """Configures isolated LanceDB path or in-memory table override for tests."""
        if db_dir is not None:
            self.db_dir = db_dir
        if table is not None:
            self._table = table
        else:
            self._table = None
        self._table_missing = False
        self._missing_checks = 0

    def reset_paths(self) -> None:
        """Restores default LanceDB path and clears table cache/overrides."""
        self.db_dir = self.default_db_dir
        self._table = self.default_table
        self._table_missing = False
        self._missing_checks = 0
        _EMBEDDING_LRU_CACHE.clear()

    @staticmethod
    def _list_table_names(db) -> list[str]:
        """Normalizes the lancedb table listing across versions.

        lancedb >= 0.39 `list_tables()` returns a ListTablesResponse object exposing a
        `.tables` list, while `table_names()` (deprecated) and older `list_tables()`
        return a plain list of strings.
        """
        raw = db.list_tables() if hasattr(db, "list_tables") else db.table_names()
        tables_attr = getattr(raw, "tables", None)
        return list(tables_attr) if tables_attr is not None else list(raw)

    @property
    def table(self):
        if self._table is False:
            return None
        if self._table is not None:
            return self._table
        if self._table_missing and self._missing_checks < MISSING_TABLE_RETRY_INTERVAL:
            self._missing_checks += 1
            return None
        self._missing_checks = 0
        try:
            db = self._connect_fn(str(self.db_dir))
            tables = self._list_table_names(db)
            if self.table_name in tables:
                self._table = db.open_table(self.table_name)
                self._table_missing = False
                try:
                    rows = self._table.count_rows()
                except Exception:  # noqa: BLE001
                    rows = "?"
                logger.info(
                    f"[GUARDRAIL] Prompt-injection guardrail active: table '{self.table_name}' "
                    f"opened with {rows} signatures (threshold={self.threshold})."
                )
            else:
                self._table_missing = True
                logger.warning(f"Guardrail table '{self.table_name}' not found. Seeding required.")
        except Exception as e:  # noqa: BLE001
            self._table_missing = True
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

        fail_mode = get_guardrail_fail_mode()
        meta = _text_audit_metadata(text)
        if fail_mode == "closed":
            logger.critical(
                f"[GUARDRAIL] Fail-closed mode active: blocking prompt due to guardrail error ({reason})"
            )
            _emit_guardrail_audit(
                event_type="GUARDRAIL_FAIL_CLOSED_BLOCKED",
                action="evaluate_prompt_injection",
                severity="CRITICAL",
                status="BLOCKED",
                details={"reason": reason, "fail_mode": "closed", **meta},
            )
            return True

        logger.warning(
            f"[GUARDRAIL] Fail-open mode active: allowing prompt despite guardrail error ({reason})"
        )
        _emit_guardrail_audit(
            event_type="GUARDRAIL_DEGRADED_OPEN",
            action="evaluate_prompt_injection",
            severity="WARNING",
            status="DEGRADED",
            details={"reason": reason, "fail_mode": "open", **meta},
        )
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
                    _emit_guardrail_audit(
                        event_type="PROMPT_INJECTION_BLOCKED",
                        action="block_prompt_injection",
                        severity="CRITICAL",
                        status="BLOCKED",
                        details={
                            "similarity": round(float(similarity), 4),
                            "threshold": self.threshold,
                            **_text_audit_metadata(text),
                        },
                    )
                    return True

        except Exception as e:  # noqa: BLE001
            logger.error(f"Error during guardrail check: {e}")
            return self._handle_failure(str(e), text)

        return False


# Singleton instance
guardrail_service = GuardrailService()
