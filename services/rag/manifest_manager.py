import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import CACHE_DIR, MANIFEST_PATH, RAG_KNOWLEDGE_DIR, SUPPORTED_EXTENSIONS

logger = logging.getLogger(__name__)


def compute_sha256(file_path: Path) -> str:
    """Calculates SHA-256 hash of a file's binary content."""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(65536), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


class ManifestManager:
    """
    Manages the file_manifest.json index file to track indexed document states,
    SHA-256 hashes, modification timestamps, and associated chunk IDs.
    """

    def __init__(self, manifest_path: Path = MANIFEST_PATH) -> None:
        self.manifest_path = manifest_path
        self._manifest: dict[str, dict[str, Any]] = {}
        self.load()

    def load(self) -> dict[str, dict[str, Any]]:
        """Loads manifest from disk. Returns empty dict if not found or corrupted."""
        if not self.manifest_path.exists():
            self._manifest = {}
            return self._manifest

        try:
            with open(self.manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    self._manifest = data
                else:
                    self._manifest = {}
        except Exception as e:
            logger.warning(f"[RAG Manifest] Failed to read {self.manifest_path}: {e}. Initializing fresh manifest.")
            self._manifest = {}
        return self._manifest

    def save(self) -> None:
        """Atomically saves the in-memory manifest to disk."""
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = self.manifest_path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._manifest, f, indent=2, ensure_ascii=False)
            tmp_path.replace(self.manifest_path)
            logger.debug(f"[RAG Manifest] Saved {len(self._manifest)} document record(s) to {self.manifest_path}")
        except Exception as e:
            logger.error(f"[RAG Manifest] Failed to persist manifest: {e}")
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    def get_record(self, rel_doc_id: str) -> dict[str, Any] | None:
        """Gets metadata record for a document relative path."""
        return self._manifest.get(rel_doc_id)

    def record_document(
        self,
        rel_doc_id: str,
        agent_id: str,
        sha256: str,
        mtime: float,
        chunk_ids: list[str],
    ) -> None:
        """Updates or adds an indexed document entry."""
        self._manifest[rel_doc_id] = {
            "agent_id": agent_id,
            "sha256": sha256,
            "mtime": mtime,
            "chunk_ids": chunk_ids,
            "indexed_at": datetime.now(timezone.utc).isoformat(),
        }

    def remove_document(self, rel_doc_id: str) -> list[str]:
        """Removes an entry from the manifest and returns its chunk IDs for cleanup."""
        record = self._manifest.pop(rel_doc_id, None)
        if record:
            return record.get("chunk_ids", [])
        return []

    def get_all_records(self) -> dict[str, dict[str, Any]]:
        return dict(self._manifest)

    def diff_filesystem(
        self, root_dir: Path = RAG_KNOWLEDGE_DIR
    ) -> dict[str, Any]:
        """
        Scans root_dir and compares with current manifest.
        Returns:
            {
                "new": [(rel_doc_id, full_path, agent_id, sha256, mtime), ...],
                "modified": [(rel_doc_id, full_path, agent_id, sha256, mtime, old_chunk_ids), ...],
                "deleted": [(rel_doc_id, old_chunk_ids), ...],
                "unchanged": [rel_doc_id, ...]
            }
        """
        diff: dict[str, list[Any]] = {
            "new": [],
            "modified": [],
            "deleted": [],
            "unchanged": [],
        }

        if not root_dir.exists():
            # If root_dir doesn't exist, all manifest entries are considered deleted
            for rel_doc_id, rec in self._manifest.items():
                diff["deleted"].append((rel_doc_id, rec.get("chunk_ids", [])))
            return diff

        seen_doc_ids: set[str] = set()

        for path in root_dir.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue

            rel_path = path.relative_to(root_dir)
            rel_doc_id = str(rel_path).replace("\\", "/")
            parts = rel_path.parts

            # Determine agent_id from the top-level folder inside rag_knowledge
            agent_id = parts[0] if len(parts) > 1 else "shared"
            seen_doc_ids.add(rel_doc_id)

            try:
                stat = path.stat()
                mtime = stat.st_mtime
                current_sha = compute_sha256(path)
            except OSError as e:
                logger.error(f"[RAG Manifest] Could not read file {path}: {e}")
                continue

            if rel_doc_id not in self._manifest:
                diff["new"].append((rel_doc_id, path, agent_id, current_sha, mtime))
            else:
                existing_record = self._manifest[rel_doc_id]
                if existing_record.get("sha256") != current_sha:
                    diff["modified"].append(
                        (
                            rel_doc_id,
                            path,
                            agent_id,
                            current_sha,
                            mtime,
                            existing_record.get("chunk_ids", []),
                        )
                    )
                else:
                    diff["unchanged"].append(rel_doc_id)

        # Detect deleted files
        for rel_doc_id, rec in self._manifest.items():
            if rel_doc_id not in seen_doc_ids:
                diff["deleted"].append((rel_doc_id, rec.get("chunk_ids", [])))

        return diff
