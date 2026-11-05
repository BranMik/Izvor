import asyncio
import shutil
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.rag.document_parser import DocumentParser
from services.rag.embeddings import EmbeddingService
from services.rag.manifest_manager import ManifestManager
from services.rag.pii_masker import PIIMasker, pii_masker
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
    tl_dir = knowledge_dir / "agent-10011"
    shared_dir.mkdir(parents=True)
    tl_dir.mkdir(parents=True)

    # Write test documents
    (shared_dir / "overview.md").write_text(
        "# Shared Overview\n\nThis is shared system documentation.\n\n## Section 2\n\nDetails on shared rules.",
        encoding="utf-8",
    )
    (tl_dir / "runbook.md").write_text(
        "# LegalAdvisor Runbook\n\n## Deployment\n\nSteps for deploying to production.",
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
        chunks = parser.parse_file(tmp_file, "doc_01.md", "agent-10011")
        assert len(chunks) >= 2
        assert any(c.section_header == "## First Header" for c in chunks)
        assert any(c.section_header == "## Second Header" for c in chunks)
        assert all(c.agent_id == "agent-10011" for c in chunks)
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
    (knowledge_dir / "agent-10011" / "runbook.md").unlink()
    diff4 = mgr2.diff_filesystem(knowledge_dir)
    assert len(diff4["deleted"]) == 1
    assert diff4["deleted"][0][0] == "agent-10011/runbook.md"


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
            query="deployment", agent_name="agent-10011", scope="all", max_results=3
        )
        assert "agent-10011" in res_techlead or "shared" in res_techlead
        assert "Knowledge Base Search Results" in res_techlead

        # 4. Agent scoping test: searching as SecurityArchitect with scope="self" shouldn't see LegalAdvisor docs
        res_self = await service.search(
            query="deployment", agent_name="agent-10008", scope="self", max_results=3
        )
        assert "No relevant documentation found" in res_self or "agent-10008" not in res_self

    asyncio.run(_run())


@pytest.mark.anyio
async def test_skill_registry_loads_knowledge_base_rag():
    registry = SkillRegistry()
    skill = registry.get("knowledge_base_rag")
    assert skill is not None
    assert skill.name == "knowledge_base_rag"
    assert "search_knowledge_rag" in skill.tools

    schemas = skill.get_tool_schemas()
    assert len(schemas) == 1
    fn_names = [s.get("function", {}).get("name") for s in schemas]
    assert fn_names == ["search_knowledge_rag"]
    assert len(fn_names) == len(set(fn_names))

    # Test tool execution dispatch
    result = await registry.execute_tool_async(
        "search_knowledge_rag",
        {"query": "coding standards", "scope": "all", "max_results": 2},
        authorized_skills=["knowledge_base_rag"],
    )
    assert isinstance(result, str)

    # Test tool execution dispatch with agent_name
    result_agent = await registry.execute_tool_async(
        "search_knowledge_rag",
        {"query": "AI in delivery", "scope": "all", "max_results": 2},
        authorized_skills=["knowledge_base_rag"],
        agent_name="agent-10011",
    )
    assert isinstance(result_agent, str)


def test_pii_masker_patterns():
    masker = PIIMasker()

    sample_text = (
        "Contact me at alice.smith@example.com or 555-123-4567. "
        "My name is Branko, living in New York. "
        "SSN: 000-12-3456. Card: 4111-2222-3333-4444. "
        "Server IP is 192.168.1.100. "
        "Use Slack token xoxb-123456789012-123456789012-abcdefghijklmnopqrstuvwx and OpenAI sk-abc1234567890abcdef123456. "
        "Config: password: MySecretPassword123. "
        "MISTRAL_API_KEY=VLWNOVBWOEBOGBLNVLXKKNOdoibhsoibhoiweho "
        "GROQ_API_KEY=gsk_b4vpoinewqobgoqwbgeowvioeuwqbiegfUWEOFIGSE "
        "Hash: a1b2c3d4e5f60718293a4b5c6d7e8f90"
    )

    masked, mapping = masker.mask(sample_text)

    assert "alice.smith@example.com" not in masked
    assert "<EMAIL_0>" in masked
    if masker._presidio_analyzer is not None:
        assert "Branko" not in masked
        assert "<PERSON_0>" in masked
        assert "New York" not in masked
        assert "<LOCATION_0>" in masked
    assert "000-12-3456" not in masked
    assert "<SSN_0>" in masked
    assert "4111-2222-3333-4444" not in masked
    assert "<CREDIT_CARD_0>" in masked
    assert "192.168.1.100" not in masked
    assert "<IP_ADDRESS_0>" in masked
    assert "xoxb-" not in masked
    assert "<SLACK_TOKEN_0>" in masked
    assert "sk-abc" not in masked
    assert "<API_KEY_0>" in masked
    assert "MySecretPassword123" not in masked
    assert "<REDACTED_SECRET_0>" in masked
    assert "VLWNOVBWOEBOGBLNVLXKKNO" not in masked
    assert "gsk_b4vpoinewqobgoqwbgeowvioeuwqbiegfUWEOFIGSE" not in masked
    assert "a1b2c3d4e5f60718293a4b5c6d7e8f90" not in masked
    assert "<TOKEN_HASH_0>" in masked


def test_pii_masking_integration():
    sample_raw = "Deploy keys: sk-proj-1234567890abcdef123456 and email dev@org.internal"

    # 1. Test NONE mode
    with patch("services.rag.pii_masker.get_pii_masking_mode", return_value="NONE"):
        assert not pii_masker.should_mask_at_ingestion()
        assert not pii_masker.should_mask_at_retrieval()

    # 2. Test AT_INGESTION mode
    with patch("services.rag.pii_masker.get_pii_masking_mode", return_value="AT_INGESTION"):
        assert pii_masker.should_mask_at_ingestion()
        assert not pii_masker.should_mask_at_retrieval()

        parser = DocumentParser()
        chunks = parser._chunk_text(sample_raw)
        assert len(chunks) > 0
        assert "dev@org.internal" not in chunks[0]
        assert "<EMAIL_0>" in chunks[0]
        assert "<API_KEY_0>" in chunks[0]

    # 3. Test AT_RETRIEVAL mode (default)
    with patch("services.rag.pii_masker.get_pii_masking_mode", return_value="AT_RETRIEVAL"):
        assert not pii_masker.should_mask_at_ingestion()
        assert pii_masker.should_mask_at_retrieval()

        # Ingestion leaves raw content intact
        parser = DocumentParser()
        raw_chunks = parser._chunk_text(sample_raw)
        assert "dev@org.internal" in raw_chunks[0]

        # RAGService search masks it on output
        mock_store = MagicMock()
        mock_store.search.return_value = [
            {"doc_id": "test_doc", "section_header": "Secrets", "text": sample_raw}
        ]
        mock_embedder = AsyncMock()
        mock_embedder.get_embeddings.return_value = [[0.1] * 10]

        service = RAGService(store=mock_store, embedder=mock_embedder)

        async def _run_search():
            return await service.search(query="keys", agent_name="Admin")

        result = asyncio.run(_run_search())
        assert "dev@org.internal" not in result
        assert "<EMAIL_0>" in result
        assert "<API_KEY_0>" in result


def test_vector_store_sql_literal_validation():
    from services.rag.vector_store import _sql_literal

    # Safe literals
    assert _sql_literal("agent-10011") == "'agent-10011'"
    assert _sql_literal("LegalAdvisor/runbook.md") == "'LegalAdvisor/runbook.md'"
    assert _sql_literal("doc_123:456") == "'doc_123:456'"
    assert _sql_literal("doc's title") == "'doc''s title'"

    # Unsafe literals must raise ValueError
    with pytest.raises(ValueError):
        _sql_literal("doc\x00injection")
    with pytest.raises(ValueError):
        _sql_literal("'; DROP TABLE agent_knowledge; --")
    with pytest.raises(ValueError):
        _sql_literal("line\nbreak")
    with pytest.raises(ValueError):
        _sql_literal("")
    with pytest.raises(ValueError):
        _sql_literal("a" * 300)


@pytest.mark.anyio
async def test_rag_service_async_search_offload():
    mock_store = MagicMock()
    mock_store.search.return_value = [{"doc_id": "d1", "section_header": "H1", "text": "architectural guidelines and runbook procedures"}]
    mock_embedder = AsyncMock()
    mock_embedder.get_embeddings.return_value = [[0.1] * 10]

    service = RAGService(store=mock_store, embedder=mock_embedder)

    with patch("asyncio.to_thread", side_effect=asyncio.to_thread) as mock_to_thread:
        res = await service.search(query="test", agent_name="agent-10001")
        assert "architectural guidelines" in res
        assert mock_to_thread.called
        assert mock_to_thread.call_args[0][0] == mock_store.search


