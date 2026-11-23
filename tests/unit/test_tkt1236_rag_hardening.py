"""Unit tests for TKT-1236: RAG Unicode SQL IDs, collision-free sanitize_id, code-fence parsing, shared doc PII map, and config_hash re-sync."""

from __future__ import annotations

from pathlib import Path

import pytest

from config_store.system_settings import system_settings_manager
from services.rag.config import EMBEDDING_DIM
from services.rag.document_parser import DocumentParser, sanitize_id
from services.rag.manifest_manager import ManifestManager
from services.rag.sync_engine import SyncReconciliationEngine
from services.rag.vector_store import VectorStore, _sql_literal


def test_vector_store_sql_literal_accepts_unicode_identifiers_and_blocks_injection():
    """_SAFE_ID_RE permits valid Unicode letters/digits while still blocking SQL injection."""
    assert _sql_literal("izvještaj_čćžšđ/sec.md") == "'izvještaj_čćžšđ/sec.md'"
    assert _sql_literal("doc_東京_01") == "'doc_東京_01'"

    with pytest.raises(ValueError):
        _sql_literal("doc'; DROP TABLE x; --")
    with pytest.raises(ValueError):
        _sql_literal("doc\x00bad")


def test_document_parser_sanitize_id_collision_free_and_code_fence_preservation(tmp_path: Path):
    """sanitize_id distinguishes 'a-b.md' vs 'a_b.md'; _parse_markdown ignores '#' inside fenced code blocks."""
    assert sanitize_id("shared/a-b.md") != sanitize_id("shared/a_b.md")
    assert len(sanitize_id("shared/a-b.md").rsplit("_", 1)[1]) == 8

    md_file = tmp_path / "fences.md"
    md_file.write_text(
        "# Real Top Heading\n\n"
        "Intro text.\n\n"
        "```bash\n"
        "# This shell comment is NOT a heading\n"
        "echo hello\n"
        "```\n\n"
        "~~~\n"
        "## Tilde fence comment is also NOT a heading\n"
        "~~~\n\n"
        "## Second Real Heading\n\n"
        "Closing section text.\n",
        encoding="utf-8",
    )
    chunks = DocumentParser().parse_file(md_file, "shared/fences.md", "shared")
    headers = [c.section_header for c in chunks]
    assert "# Real Top Heading" in headers
    assert "## Second Real Heading" in headers
    assert "# This shell comment is NOT a heading" not in headers
    assert "## Tilde fence comment is also NOT a heading" not in headers
    assert any("# This shell comment is NOT a heading" in c.text for c in chunks)
    assert any("## Tilde fence comment is also NOT a heading" in c.text for c in chunks)


def test_document_parser_shares_pii_token_mapping_across_chunks(tmp_path: Path):
    """Ingestion-time PII masking shares one document token_mapping so repeated entities keep identical aliases."""
    orig_mode = system_settings_manager.get_rag_setting("pii_masking", "AT_RETRIEVAL")
    system_settings_manager.set_rag_setting("pii_masking", "AT_INGESTION", save=False)
    try:
        md_file = tmp_path / "pii_doc.md"
        md_file.write_text(
            "# Section One\n\nPrimary contact is alice@example.com for approvals.\n\n"
            "# Section Two\n\nSecondary is bob@example.com, and cc alice@example.com again.\n",
            encoding="utf-8",
        )
        chunks = DocumentParser(max_chunk_chars=250, chunk_overlap_chars=0, min_chunk_chars=20).parse_file(
            md_file, "shared/pii_doc.md", "shared"
        )
        assert len(chunks) == 2
        assert "<EMAIL_0>" in chunks[0].text
        assert "<EMAIL_1>" in chunks[1].text
        assert "<EMAIL_0>" in chunks[1].text
        assert "alice@example.com" not in chunks[0].text
        assert "alice@example.com" not in chunks[1].text
        assert "bob@example.com" not in chunks[1].text
    finally:
        system_settings_manager.set_rag_setting("pii_masking", orig_mode, save=False)


@pytest.mark.anyio
async def test_sync_engine_triggers_clean_reindex_when_rag_config_drifts(tmp_path: Path):
    """Changing embedding_model, pii_masking, or chunk sizes forces re-indexing via manifest config_hash."""
    knowledge_dir = tmp_path / "rag_knowledge"
    shared_dir = knowledge_dir / "shared"
    shared_dir.mkdir(parents=True, exist_ok=True)
    (shared_dir / "policy.md").write_text(
        "# Security Policy\n\nContact secops@example.com for escalations.\n",
        encoding="utf-8",
    )

    manifest_path = tmp_path / "custom_cache" / "file_manifest.json"
    db_dir = tmp_path / "custom_cache" / "lancedb"

    class _ModelFakeEmbedder:
        def __init__(self, model: str) -> None:
            self.model = model
            self.calls = 0

        async def get_embeddings(self, texts: list[str]) -> list[list[float]]:
            self.calls += 1
            return [[0.05] * EMBEDDING_DIM for _ in texts]

    manifest_mgr = ManifestManager(manifest_path=manifest_path)
    store = VectorStore(db_dir=db_dir)
    embedder = _ModelFakeEmbedder("model-v1")
    engine = SyncReconciliationEngine(
        knowledge_dir=knowledge_dir,
        manifest_manager=manifest_mgr,
        store=store,
        embedder=embedder,
        parser=DocumentParser(max_chunk_chars=500, chunk_overlap_chars=50, min_chunk_chars=20),
    )

    # 1. Initial sync indexes 1 new document and persists config_hash
    stats1 = await engine.reconcile()
    assert stats1 == {"new": 1, "modified": 0, "deleted": 0, "unchanged": 0}
    initial_hash = manifest_mgr.get_config_hash()
    assert initial_hash is not None
    assert "__meta__" not in manifest_mgr.get_all_records()

    # 2. Repeat sync with unchanged files & config -> fast-path unchanged (0 re-embeds)
    stats2 = await engine.reconcile()
    assert stats2 == {"new": 0, "modified": 0, "deleted": 0, "unchanged": 1}
    assert embedder.calls == 1

    # 3. Change embedding_model -> config_hash drifts -> forces full re-index even without file edit
    embedder.model = "model-v2"
    stats3 = await engine.reconcile()
    assert stats3 == {"new": 0, "modified": 1, "deleted": 0, "unchanged": 0}
    assert embedder.calls == 2
    assert manifest_mgr.get_config_hash() != initial_hash

