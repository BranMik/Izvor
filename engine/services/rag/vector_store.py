import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

import pyarrow as pa

from .config import DEFAULT_TOP_K, EMBEDDING_DIM, LANCEDB_DIR, TABLE_NAME
from .document_parser import DocumentChunk

logger = logging.getLogger(__name__)

# Schema defining the LanceDB agent_knowledge table
RAG_SCHEMA = pa.schema([
    pa.field("chunk_id", pa.string()),
    pa.field("agent_id", pa.string()),          # e.g. "agent-10011", "Security Architect", "shared"
    pa.field("doc_id", pa.string()),            # e.g. "LegalAdvisor/runbook.md"
    pa.field("filename", pa.string()),          # e.g. "runbook.md"
    pa.field("section_header", pa.string()),    # e.g. "## Rollback Procedures"
    pa.field("text", pa.string()),              # Raw chunk text
    pa.field("vector", pa.list_(pa.float32(), EMBEDDING_DIM)),  # Embedding vector
    pa.field("updated_at", pa.float64()),
])

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._:/ '-]{1,256}$")


def _sql_literal(val: str) -> str:
    """
    Validates and sanitizes a string literal for safe interpolation into LanceDB/DataFusion SQL expressions.
    Rejects control characters / NUL bytes and values that do not match _SAFE_ID_RE.
    Escapes single quotes by doubling them.
    Returns the enclosed string with single quotes, e.g. 'clean_value'.
    """
    if not isinstance(val, str):
        raise ValueError(f"Expected string literal, got {type(val).__name__}")
    if "\x00" in val or any(ord(c) < 32 for c in val):
        raise ValueError("Control characters and NUL bytes are not permitted in SQL literals.")
    clean = val.strip()
    if not _SAFE_ID_RE.match(clean):
        raise ValueError(f"Identifier '{val}' contains unsafe characters.")
    escaped = clean.replace("'", "''")
    return f"'{escaped}'"


class VectorStore:
    """
    Manages LanceDB embedded database, schema validation, full-text FTS index,
    and hybrid / vector search with agent domain scoping.
    """

    def __init__(self, db_dir: Path = LANCEDB_DIR) -> None:
        self.db_dir = db_dir
        self._db: Any = None
        self._table: Any = None
        self._lock = threading.RLock()

    def _get_db(self) -> Any:
        if self._db is None:
            import lancedb

            self.db_dir.mkdir(parents=True, exist_ok=True)
            self._db = lancedb.connect(str(self.db_dir))
        return self._db

    def get_or_create_table(self) -> Any:
        """Returns the LanceDB table, creating it if it doesn't already exist."""
        with self._lock:
            if self._table is not None:
                return self._table

            db = self._get_db()
            try:
                self._table = db.open_table(TABLE_NAME)
            except Exception:  # noqa: BLE001
                self._table = db.create_table(TABLE_NAME, schema=RAG_SCHEMA)
                self._ensure_fts_index(self._table)

            return self._table

    def _ensure_fts_index(self, table: Any) -> None:
        """Creates or updates BM25 full-text index on the text column."""
        try:
            import lancedb.index
            table.create_index("text", config=lancedb.index.FTS(), replace=True)
            logger.info(f"[RAG VectorStore] Created BM25 FTS index on table '{TABLE_NAME}'")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[RAG VectorStore] FTS index creation skipped (e.g. empty table): {e}")

    def rebuild_fts_index(self) -> None:
        """Explicitly recreates or updates the BM25 full-text index on the table."""
        with self._lock:
            table = self.get_or_create_table()
            self._ensure_fts_index(table)

    def insert_chunks(
        self,
        chunks: list[DocumentChunk],
        vectors: list[list[float]],
        rebuild_index: bool = True,
    ) -> None:
        """
        Inserts new chunks and their corresponding embedding vectors into LanceDB.
        If rebuild_index is False, index rebuilding is deferred to caller (e.g. batch ingestion).
        """
        with self._lock:
            if not chunks or not vectors or len(chunks) != len(vectors):
                return

            table = self.get_or_create_table()
            now = time.time()

            rows = []
            for chunk, vec in zip(chunks, vectors):
                rows.append(
                    {
                        "chunk_id": chunk.chunk_id,
                        "agent_id": chunk.agent_id,
                        "doc_id": chunk.doc_id,
                        "filename": chunk.filename,
                        "section_header": chunk.section_header,
                        "text": chunk.text,
                        "vector": vec,
                        "updated_at": now,
                    }
                )

            table.add(rows)
            # Update FTS index after additions if requested
            if rebuild_index:
                self._ensure_fts_index(table)
            logger.info(f"[RAG VectorStore] Successfully inserted {len(rows)} chunk(s) into '{TABLE_NAME}'")

    def delete_by_doc_id(self, doc_id: str) -> None:
        """Deletes all chunks associated with a specific doc_id."""
        with self._lock:
            table = self.get_or_create_table()
            try:
                safe_doc_id = _sql_literal(doc_id)
                table.delete(f"doc_id = {safe_doc_id}")
                logger.info(f"[RAG VectorStore] Deleted chunks for doc_id='{doc_id}'")
            except Exception as e:  # noqa: BLE001
                logger.error(f"[RAG VectorStore] Failed to delete chunks for doc_id='{doc_id}': {e}")

    def delete_by_chunk_ids(self, chunk_ids: list[str]) -> None:
        """Deletes chunks by their unique chunk_id list."""
        with self._lock:
            if not chunk_ids:
                return
            table = self.get_or_create_table()
            try:
                id_list_str = ", ".join(_sql_literal(cid) for cid in chunk_ids)
                table.delete(f"chunk_id IN ({id_list_str})")
                logger.info(f"[RAG VectorStore] Deleted {len(chunk_ids)} chunk(s) by ID")
            except Exception as e:  # noqa: BLE001
                logger.error(f"[RAG VectorStore] Failed to delete chunks by IDs: {e}")

    def search(
        self,
        query_text: str,
        query_vector: list[float] | None = None,
        agent_name: str = "",
        scope: str = "all",
        top_k: int = DEFAULT_TOP_K,
    ) -> list[dict[str, Any]]:
        """
        Executes hybrid search combining vector cosine similarity with BM25 full-text rank.
        Applies agent scoping:
        - scope == "self": agent_id == agent_name
        - scope == "all": agent_id IN (agent_name, 'shared')
        """
        with self._lock:
            table = self.get_or_create_table()

            # Build agent scoping filter
            if scope == "self" and agent_name:
                safe_agent = _sql_literal(agent_name)
                where_filter = f"agent_id = {safe_agent}"
            elif agent_name:
                safe_agent = _sql_literal(agent_name)
                where_filter = f"agent_id IN ({safe_agent}, 'shared')"
            else:
                where_filter = "agent_id = 'shared'"

            def _execute_query(q: Any) -> list[dict[str, Any]]:
                if hasattr(q, "to_list"):
                    return q.to_list()
                if hasattr(q, "to_arrow"):
                    return q.to_arrow().to_pylist()
                if hasattr(q, "to_pandas"):
                    return q.to_pandas().to_dict(orient="records")
                return []

            raw_results: list[dict[str, Any]] = []

            # Attempt hybrid search (dense vector + Tantivy BM25) if vector is available
            if query_vector is not None:
                try:
                    search_query = (
                        table.search(query_type="hybrid", vector_column_name="vector", fts_columns="text")
                        .vector(query_vector)
                        .text(query_text)
                        .where(where_filter)
                        .limit(top_k)
                    )
                    raw_results = _execute_query(search_query)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        f"[RAG VectorStore] Hybrid search vector+text failed: {e}. Falling back to vector cosine search."
                    )
                    try:
                        search_query = (
                            table.search(query_vector, vector_column_name="vector")
                            .metric("cosine")
                            .where(where_filter)
                            .limit(top_k)
                        )
                        raw_results = _execute_query(search_query)
                    except Exception as e2:  # noqa: BLE001
                        logger.warning(f"[RAG VectorStore] Vector search failed: {e2}")

            # Fallback to full-text search if no results or no vector
            if not raw_results:
                try:
                    search_query = (
                        table.search(query_text, query_type="fts")
                        .where(where_filter)
                        .limit(top_k)
                    )
                    raw_results = _execute_query(search_query)
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"[RAG VectorStore] FTS search fallback failed: {e}")

            if not raw_results:
                return []

            records: list[dict[str, Any]] = []
            for row in raw_results:
                records.append({
                    "chunk_id": str(row.get("chunk_id", "")),
                    "agent_id": str(row.get("agent_id", "")),
                    "doc_id": str(row.get("doc_id", "")),
                    "filename": str(row.get("filename", "")),
                    "section_header": str(row.get("section_header", "")),
                    "text": str(row.get("text", "")),
                    "score": float(row.get("_relevance_score", row.get("_score", row.get("_distance", 0.0)))),
                })

            return records


# Singleton instance
vector_store = VectorStore()
