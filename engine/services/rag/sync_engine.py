import asyncio
import logging
from pathlib import Path

from .config import RAG_KNOWLEDGE_DIR
from .document_parser import DocumentParser
from .embeddings import EmbeddingService, embedding_service
from .manifest_manager import ManifestManager
from .vector_store import VectorStore, vector_store

logger = logging.getLogger(__name__)


class SyncReconciliationEngine:
    """
    Scans rag_knowledge/ on startup or on demand, compares with file_manifest.json,
    and reconciles new, modified, and deleted documents into LanceDB.
    """

    def __init__(
        self,
        knowledge_dir: Path = RAG_KNOWLEDGE_DIR,
        manifest_manager: ManifestManager | None = None,
        store: VectorStore | None = None,
        embedder: EmbeddingService | None = None,
        parser: DocumentParser | None = None,
    ) -> None:
        self.knowledge_dir = knowledge_dir
        self.manifest_manager = manifest_manager or ManifestManager()
        self.vector_store = store or vector_store
        self.embedder = embedder or embedding_service
        self.parser = parser or DocumentParser()
        self._lock: asyncio.Lock | None = None

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def reconcile(self) -> dict[str, int]:
        """
        Executes non-blocking reconciliation pass.
        Returns counts of processed files: {'new': N, 'modified': M, 'deleted': D, 'unchanged': U}.
        """
        async with self.lock:
            logger.info(f"[RAG Sync] Starting knowledge reconciliation for {self.knowledge_dir}...")
            stats = {"new": 0, "modified": 0, "deleted": 0, "unchanged": 0}

            diff = await asyncio.to_thread(self.manifest_manager.diff_filesystem, self.knowledge_dir)
            stats["unchanged"] = len(diff["unchanged"])

            # 1. Process deletions
            for rel_doc_id, old_chunk_ids in diff["deleted"]:
                try:
                    if old_chunk_ids:
                        await asyncio.to_thread(self.vector_store.delete_by_chunk_ids, old_chunk_ids)
                    else:
                        await asyncio.to_thread(self.vector_store.delete_by_doc_id, rel_doc_id)
                    self.manifest_manager.remove_document(rel_doc_id)
                    stats["deleted"] += 1
                    logger.info(f"[RAG Sync] Deleted {rel_doc_id} from vector store and manifest")
                except Exception as e:  # noqa: BLE001
                    logger.error(f"[RAG Sync] Error deleting {rel_doc_id}: {e}")

            # 2. Process modified files (atomic swap: parse & embed before purging old chunks)
            for rel_doc_id, path, agent_id, sha256, mtime, old_chunk_ids in diff["modified"]:
                try:
                    chunks = await asyncio.to_thread(self.parser.parse_file, path, rel_doc_id, agent_id)
                    if chunks:
                        texts = [c.text for c in chunks]
                        vectors = await self.embedder.get_embeddings(texts)
                        if not vectors or len(vectors) != len(chunks):
                            raise RuntimeError(
                                f"Embedding generation failed for '{rel_doc_id}': "
                                f"expected {len(chunks)} vector(s), got {len(vectors) if vectors else 0}"
                            )
                        chunk_ids = [c.chunk_id for c in chunks]
                    else:
                        vectors = []
                        chunk_ids = []

                    if old_chunk_ids:
                        await asyncio.to_thread(self.vector_store.delete_by_chunk_ids, old_chunk_ids)
                    else:
                        await asyncio.to_thread(self.vector_store.delete_by_doc_id, rel_doc_id)

                    if chunks:
                        await asyncio.to_thread(
                            self.vector_store.insert_chunks, chunks, vectors, rebuild_index=False
                        )

                    self.manifest_manager.record_document(
                        rel_doc_id=rel_doc_id,
                        agent_id=agent_id,
                        sha256=sha256,
                        mtime=mtime,
                        chunk_ids=chunk_ids,
                    )
                    stats["modified"] += 1
                    logger.info(f"[RAG Sync] Updated {rel_doc_id} ({len(chunk_ids)} chunks)")
                except Exception as e:  # noqa: BLE001
                    logger.error(f"[RAG Sync] Error updating {rel_doc_id}: {e}")

            # 3. Process new files
            for rel_doc_id, path, agent_id, sha256, mtime in diff["new"]:
                try:
                    chunks = await asyncio.to_thread(self.parser.parse_file, path, rel_doc_id, agent_id)
                    if chunks:
                        texts = [c.text for c in chunks]
                        vectors = await self.embedder.get_embeddings(texts)
                        if not vectors or len(vectors) != len(chunks):
                            raise RuntimeError(
                                f"Embedding generation failed for '{rel_doc_id}': "
                                f"expected {len(chunks)} vector(s), got {len(vectors) if vectors else 0}"
                            )
                        await asyncio.to_thread(
                            self.vector_store.insert_chunks, chunks, vectors, rebuild_index=False
                        )
                        chunk_ids = [c.chunk_id for c in chunks]
                    else:
                        chunk_ids = []

                    self.manifest_manager.record_document(
                        rel_doc_id=rel_doc_id,
                        agent_id=agent_id,
                        sha256=sha256,
                        mtime=mtime,
                        chunk_ids=chunk_ids,
                    )
                    stats["new"] += 1
                    logger.info(f"[RAG Sync] Indexed new file {rel_doc_id} ({len(chunk_ids)} chunks)")
                except Exception as e:  # noqa: BLE001
                    logger.error(f"[RAG Sync] Error indexing new file {rel_doc_id}: {e}")

            # Rebuild FTS index once per sync batch and persist updated manifest to disk
            if stats["new"] > 0 or stats["modified"] > 0 or stats["deleted"] > 0:
                await asyncio.to_thread(self.vector_store.rebuild_fts_index)
                await asyncio.to_thread(self.manifest_manager.save)

            logger.info(
                f"[RAG Sync] Reconciliation completed: "
                f"{stats['new']} new, {stats['modified']} modified, "
                f"{stats['deleted']} deleted, {stats['unchanged']} unchanged."
            )
            return stats


# Global singleton sync engine
rag_sync_engine = SyncReconciliationEngine()
