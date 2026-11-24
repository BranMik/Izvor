import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import MANIFEST_PATH, RAG_KNOWLEDGE_DIR, SUPPORTED_EXTENSIONS

logger = logging.getLogger(__name__)

META_KEY = "__meta__"


def compute_rag_config_hash(
    embedding_model: str,
    pii_masking: str,
    max_chunk_chars: int,
    chunk_overlap_chars: int,
    min_chunk_chars: int,
) -> str:
    """Computes deterministic SHA-256 fingerprint of active RAG indexing configuration."""
    payload = json.dumps(
        {
            "embedding_model": str(embedding_model),
            "pii_masking": str(pii_masking).upper(),
            "max_chunk_chars": int(max_chunk_chars),
            "chunk_overlap_chars": int(chunk_overlap_chars),
            "min_chunk_chars": int(min_chunk_chars),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
        except (OSError, json.JSONDecodeError, ValueError) as e:
            logger.warning(f"[RAG Manifest] Failed to read {self.manifest_path}: {e}. Initializing fresh manifest.")
            self._manifest = {}
        return self._manifest

    def save(self) -> None:
        """Atomically saves the in-memory manifest to disk."""
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.manifest_path.parent / f"{self.manifest_path.name}.{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._manifest, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.manifest_path)
            logger.debug(f"[RAG Manifest] Saved {len(self._manifest)} entry/entries to {self.manifest_path}")
        except (OSError, json.JSONDecodeError, ValueError) as e:
            logger.error(f"[RAG Manifest] Failed to persist manifest: {e}")
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise

    def get_config_hash(self) -> str | None:
        """Returns persisted RAG configuration hash stored under META_KEY, or None."""
        meta = self._manifest.get(META_KEY)
        if isinstance(meta, dict):
            val = meta.get("config_hash")
            return str(val) if val else None
        return None

    def set_config_hash(self, config_hash: str) -> None:
        """Records the current RAG configuration hash under META_KEY."""
        self._manifest[META_KEY] = {
            "config_hash": str(config_hash),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def get_record(self, rel_doc_id: str) -> dict[str, Any] | None:
        """Gets metadata record for a document relative path."""
        if rel_doc_id == META_KEY:
            return None
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
        if rel_doc_id == META_KEY:
            return []
        record = self._manifest.pop(rel_doc_id, None)
        if record:
            return record.get("chunk_ids", [])
        return []

    def get_all_records(self) -> dict[str, dict[str, Any]]:
        return {k: v for k, v in self._manifest.items() if k != META_KEY}

    def diff_filesystem(
        self,
        root_dir: Path = RAG_KNOWLEDGE_DIR,
        use_mtime_fastpath: bool = True,
        current_config_hash: str | None = None,
    ) -> dict[str, Any]:
        """
        Scans root_dir and compares with current manifest.
        If current_config_hash differs from persisted config_hash on a non-empty manifest,
        forces full re-indexing of all present documents as 'modified'.
        """
        has_doc_records = any(k != META_KEY for k in self._manifest)
        config_drifted = bool(
            current_config_hash is not None
            and has_doc_records
            and self.get_config_hash() != current_config_hash
        )
        diff: dict[str, Any] = {
            "new": [],
            "modified": [],
            "deleted": [],
            "unchanged": [],
            "config_drifted": config_drifted,
        }

        if not root_dir.exists():
            # If root_dir doesn't exist, all document manifest entries are considered deleted
            for rel_doc_id, rec in self._manifest.items():
                if rel_doc_id == META_KEY:
                    continue
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
            except OSError as e:
                logger.error(f"[RAG Manifest] Could not stat file {path}: {e}")
                continue

            existing_record = self._manifest.get(rel_doc_id)
            if existing_record and use_mtime_fastpath and not config_drifted:
                recorded_mtime = float(existing_record.get("mtime", 0.0))
                if abs(recorded_mtime - mtime) < 1e-3:
                    diff["unchanged"].append(rel_doc_id)
                    continue

            try:
                current_sha = compute_sha256(path)
            except OSError as e:
                logger.error(f"[RAG Manifest] Could not read file {path}: {e}")
                continue

            if rel_doc_id not in self._manifest or rel_doc_id == META_KEY:
                diff["new"].append((rel_doc_id, path, agent_id, current_sha, mtime))
            else:
                if config_drifted or existing_record.get("sha256") != current_sha:
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

        # Detect deleted files (skipping metadata key)
        for rel_doc_id, rec in self._manifest.items():
            if rel_doc_id == META_KEY:
                continue
            if rel_doc_id not in seen_doc_ids:
                diff["deleted"].append((rel_doc_id, rec.get("chunk_ids", [])))

        return diff
