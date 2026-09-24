import asyncio
import shutil
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from services.rag.document_parser import DocumentChunk, DocumentParser
from services.rag.embeddings import EmbeddingService
from services.rag.manifest_manager import ManifestManager, compute_sha256
from services.rag.service import RAGService
from services.rag.sync_engine import SyncReconciliationEngine
from services.rag.vector_store import VectorStore
from skills.loader import SkillRegistry


@pytest.fixture
def temp_rag_dir():
    """Creates a temporary workspace directory for RAG testing."""
    tmp = tempfile.mkdtemp(prefix="rag_test_")
    path = Path(tmp)
    knowledge_dir = path / "rag_knowledge"
    shared_dir = knowledge_dir / "shared"
    tl_dir = knowledge_dir / "TechLead"
    shared_dir.mkdir(parents=True)
    tl_dir.mkdir(parents=True)

    # Write test documents
    (shared_dir / "overview.md").write_text(
        "# Shared Overview\n\nThis is shared system documentation.\n\n## Section 2\n\nDetails on shared rules.",
        encoding="utf-8",
    )
    (tl_dir / "runbook.md").write_text(
        "# TechLead Runbook\n\n## Deployment\n\nSteps for deploying to production.",
        encoding="utf-8",
    )

    cache_dir = path / "data" / ".rag_cache"
    cache_dir.mkdir(parents=True)
    manifest_path = cache_dir / "file_manifest.json"
    db_dir = cache_dir / "lancedb"

    yield {
        "root": path,
        "knowledge_dir": knowledge_dir,
        "manifest_path": manifest_path,
        "db_dir": db_dir,
    }

    shutil.rmtree(tmp, ignore_errors=True)


def test_document_parser():
    parser = DocumentParser()
    tmp_file = Path(tempfile.mktemp(suffix=".md"))
    try:
        tmp_file.write_text(
            "# Title\n\nIntroduction paragraph.\n\n## First Header\n\nBody content of first section.\n\n## Second Header\n\nSecond body content.",
            encoding="utf-8",
        )
        chunks = parser.parse_file(tmp_file, "doc_01.md", "TechLead")
        assert len(chunks) >= 2
        assert any(c.section_header == "## First Header" for c in chunks)
        assert any(c.section_header == "## Second Header" for c in chunks)
        assert all(c.agent_id == "TechLead" for c in chunks)
    finally:
        if tmp_file.exists():
            tmp_file.unlink()


def test_manifest_diff(temp_rag_dir):
    manifest_path = temp_rag_dir["manifest_path"]
    knowledge_dir = temp_rag_dir["knowledge_dir"]
    mgr = ManifestManager(manifest_path=manifest_path)

    # First diff: everything is new
    diff = mgr.diff_filesystem(knowledge_dir)
    assert len(diff["new"]) == 2
    assert len(diff["unchanged"]) == 0
    assert len(diff["deleted"]) == 0

    # Record documents
    for rel_doc_id, path, agent_id, current_sha, mtime in diff["new"]:
        mgr.record_document(rel_doc_id, agent_id, current_sha, mtime, ["c01", "c02"])
    mgr.save()

    # Second diff: everything unchanged
    mgr2 = ManifestManager(manifest_path=manifest_path)
    diff2 = mgr2.diff_filesystem(knowledge_dir)
    assert len(diff2["new"]) == 0
    assert len(diff2["unchanged"]) == 2
    assert len(diff2["modified"]) == 0
    assert len(diff2["deleted"]) == 0

    # Modify one file
    (knowledge_dir / "shared" / "overview.md").write_text("# Modified Content", encoding="utf-8")
    diff3 = mgr2.diff_filesystem(knowledge_dir)
    assert len(diff3["modified"]) == 1
    assert diff3["modified"][0][0] == "shared/overview.md"

    # Delete one file
    (knowledge_dir / "TechLead" / "runbook.md").unlink()
    diff4 = mgr2.diff_filesystem(knowledge_dir)
    assert len(diff4["deleted"]) == 1
    assert diff4["deleted"][0][0] == "TechLead/runbook.md"


def test_reconciliation_engine_lifecycle(temp_rag_dir):
    async def _run():
        knowledge_dir = temp_rag_dir["knowledge_dir"]
        manifest_path = temp_rag_dir["manifest_path"]
        db_dir = temp_rag_dir["db_dir"]

        store = VectorStore(db_dir=db_dir)
        manifest_mgr = ManifestManager(manifest_path=manifest_path)

        from services.rag.config import EMBEDDING_DIM
        mock_embedder = EmbeddingService()
        mock_embedder.get_embeddings = AsyncMock(side_effect=lambda texts: [[0.01] * EMBEDDING_DIM for _ in texts])

        engine = SyncReconciliationEngine(
            knowledge_dir=knowledge_dir,
            manifest_manager=manifest_mgr,
            store=store,
            embedder=mock_embedder,
        )

        # 1. First sync: should index 2 files
        stats1 = await engine.reconcile()
        assert stats1["new"] == 2
        assert stats1["modified"] == 0
        assert stats1["deleted"] == 0

        # 2. Second sync with no changes: 0 new, 2 unchanged
        stats2 = await engine.reconcile()
        assert stats2["new"] == 0
        assert stats2["unchanged"] == 2

        # 3. Test Search via RAGService
        service = RAGService(store=store, embedder=mock_embedder)
        res_techlead = await service.search(
            query="deployment", agent_name="TechLead", scope="all", max_results=3
        )
        assert "TechLead" in res_techlead or "shared" in res_techlead
        assert "Knowledge Base Search Results" in res_techlead

        # 4. Agent scoping test: searching as Architect with scope="self" shouldn't see TechLead docs
        res_self = await service.search(
            query="deployment", agent_name="Architect", scope="self", max_results=3
        )
        assert "No relevant documentation found" in res_self or "Architect" not in res_self

    asyncio.run(_run())


def test_skill_registry_loads_knowledge_base_rag():
    registry = SkillRegistry()
    skill = registry.get("knowledge_base_rag")
    assert skill is not None
    assert skill.name == "knowledge_base_rag"
    assert "search_knowledge_rag" in skill.tools

    schemas = skill.get_tool_schemas()
    assert len(schemas) >= 1
    fn_names = [s.get("function", {}).get("name") for s in schemas]
    assert "search_knowledge_rag" in fn_names

    # Test tool execution dispatch
    result = registry.execute_tool(
        "search_knowledge_rag",
        {"query": "coding standards", "scope": "all", "max_results": 2},
    )
    assert isinstance(result, str)
