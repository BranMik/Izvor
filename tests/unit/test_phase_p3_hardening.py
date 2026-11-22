import asyncio
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.models import FileContext
from core.slack_security import _extract_client_ip, _record_auth_failure_audit
from orchestration.thread_state import ThreadStateManager
from services.audit_logger import AuditLogger, _cross_process_file_lock
from services.flat_history_recorder import get_flat_history_filepath
from services.pricing_service import PricingService
from services.rag.manifest_manager import ManifestManager
from services.slack import cleanup_old_downloads
from services.thread_notes_writer import _atomic_write_file
from services.web_tools import PinnedIPTransport
from config.skills.knowledge_base_rag.tools import TOOL_HANDLERS, async_execute_search_knowledge_rag
from skills.loader import SkillDefinition, SkillRegistry



# =========================================================================
# TKT-723: Intra-Turn Tool-Loop Spend Governance
# =========================================================================
def test_tkt_723_mid_turn_budget_check():
    async def _test():
        from orchestration.stages.execution import ExecutionStage
        from orchestration.protocols import PipelineDeps
        from orchestration.turn_pipeline import TurnContext

        mock_spend_gov = MagicMock()
        mock_spend_gov.is_budget_exceeded.return_value = (True, 15.0, "Budget limit")
        deps = PipelineDeps(
            calc_costs_fn=lambda c, t: {"grand_total_cost_usd": 10.0},
            spend_governance=mock_spend_gov,
        )
        stage = ExecutionStage(deps)

        ctx = TurnContext(
            channel_id="C_TEST",
            thread_ts="1710000000.123456",
            raw_text="Run tools",
            user_id="U_TEST",
            agent_key="agent-10001",
            agent_cfg={"model": "gemini/gemini-3.8-flash"},
        )
        ctx.messages = [{"role": "user", "content": "hi"}]
        ctx.output.total_prompt_tokens = 50000
        ctx.output.total_completion_tokens = 10000

        assert stage._is_mid_turn_budget_exceeded(ctx) is True

    asyncio.run(_test())


# =========================================================================
# TKT-724: Audit Logger Cross-Process Lock & File-Size Invalidation
# =========================================================================
def test_tkt_724_audit_cross_process_lock_and_size_invalidation(tmp_path: Path):
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    logger = AuditLogger(audit_dir=audit_dir)
    logger.invalidate_cache()

    rec1 = logger.log_event(
        event_type="TEST_EVENT_1",
        actor={"type": "system", "id": "test_1"},
        action="action_1",
        target="target_1",
    )
    assert rec1["seq"] == 1

    month_file = logger.get_month_file_path()
    assert month_file.exists()

    # Simulate an external CLI or separate process writing to the monthly file
    external_record = (
        '{"seq": 2, "timestamp": "2026-10-16T20:00:00Z", "event_type": "CLI_EVENT", '
        '"severity": "INFO", "actor": {"id": "cli", "type": "system"}, "action": "cli_write", '
        f'"target": "cli", "details": {{}}, "status": "SUCCESS", "prev_hash": "{rec1["entry_hash"]}", '
        '"entry_hash": "external_entry_hash_99999"}\n'
    )
    with open(month_file, "a", encoding="utf-8") as f:
        f.write(external_record)

    # Calling log_event should detect file size change, invalidate in-memory cache, and pick up seq 2
    rec3 = logger.log_event(
        event_type="TEST_EVENT_3",
        actor={"type": "system", "id": "test_3"},
        action="action_3",
        target="target_3",
    )
    assert rec3["seq"] == 3
    assert rec3["prev_hash"] == "external_entry_hash_99999"

    # Test cross-process lock contextmanager directly
    lock_path = audit_dir / ".test.lock"
    with _cross_process_file_lock(lock_path) as lock_f:
        assert lock_f is not None


# =========================================================================
# TKT-725: Thread State Eviction Liveness Decoupling & LRU Capacity
# =========================================================================
def test_tkt_725_thread_state_eviction_liveness_and_lru():
    mgr = ThreadStateManager()
    mgr.reset_all()

    # 1. Thread with remaining agents but NO running task should be evicted if expired
    mgr.set_remaining_agents("C1", "100.0", ["agent-1", "agent-2"])
    mgr.thread_last_activity[("C1", "100.0")] = time.time() - 7200.0  # 2h old

    # 2. Thread with active running task should NOT be evicted even if expired
    mgr.set_remaining_agents("C2", "200.0", ["agent-1"])
    mgr.thread_last_activity[("C2", "200.0")] = time.time() - 7200.0
    task_mock = MagicMock()
    task_mock.done.return_value = False
    mgr.set_active_task("C2", "200.0", task_mock)

    evicted = mgr.cleanup_expired_threads(ttl_seconds=3600.0)
    assert evicted == 1
    assert ("C1", "100.0") not in mgr.thread_last_activity
    assert ("C2", "200.0") in mgr.thread_last_activity

    # 3. LRU capacity enforcement
    mgr.reset_all()
    mgr.configure_capacity(max_tracked_threads=3)
    try:
        # Add 4 threads with increasing timestamps
        mgr._touch("C", "10")
        mgr.thread_last_activity[("C", "10")] = 100.0
        mgr._touch("C", "20")
        mgr.thread_last_activity[("C", "20")] = 200.0
        mgr._touch("C", "30")
        mgr.thread_last_activity[("C", "30")] = 300.0
        mgr._touch("C", "40")
        mgr.thread_last_activity[("C", "40")] = 400.0

        # Trigger check capacity
        mgr._check_capacity()
        assert len(mgr.thread_last_activity) <= 3
        # Oldest ("C", "10") must have been evicted first
        assert ("C", "10") not in mgr.thread_last_activity
        assert ("C", "40") in mgr.thread_last_activity
    finally:
        mgr.reset_capacity()


# =========================================================================
# TKT-726: Native Async Tool Dispatch in SkillRegistry
# =========================================================================
def test_tkt_726_skill_registry_execute_tool_async():
    async def _test():
        registry = SkillRegistry()

        # Register custom async tool
        async def sample_async_tool(args: dict, agent_name: str = ""):
            return f"async_result:{args.get('val')}:{agent_name}"

        skill = SkillDefinition(
            name="test_async_skill",
            display_name="Test Async Skill",
            description="Testing async tool dispatch",
            tools=["test_async_tool"],
            custom_tool_schemas=[{
                "type": "function",
                "function": {
                    "name": "test_async_tool",
                    "parameters": {"type": "object", "properties": {"val": {"type": "string"}}},
                },
            }],
            tool_handlers={"test_async_tool": sample_async_tool},
        )
        registry._skills["test_async_skill"] = skill

        # Test authorized async dispatch
        res = await registry.execute_tool_async(
            "test_async_tool",
            {"val": "hello"},
            authorized_skills=["test_async_skill"],
            agent_name="agent-10001",
        )
        assert res == "async_result:hello:agent-10001"

        # Test unauthorized dispatch (fail-closed)
        unauth_res = await registry.execute_tool_async(
            "test_async_tool",
            {"val": "hello"},
            authorized_skills=["other_skill"],
            agent_name="agent-10001",
        )
        assert "Permission denied" in unauth_res

        # Test RAG async tool handler
        import inspect
        assert TOOL_HANDLERS["search_knowledge_rag"] is async_execute_search_knowledge_rag
        assert inspect.iscoroutinefunction(TOOL_HANDLERS["search_knowledge_rag"])

    asyncio.run(_test())



# =========================================================================
# TKT-727: Forwarded IP Trust Gating & Auth Failure Tracker Bounds
# =========================================================================
def test_tkt_727_proxy_header_trust_and_auth_tracker():
    # 1. When TRUST_PROXY_HEADERS is unset/false, returns direct_client
    assert (
        _extract_client_ip(
            {"x-forwarded-for": "203.0.113.195, 70.41.3.18"},
            env={"TRUST_PROXY_HEADERS": "0"},
        )
        == "direct_client"
    )

    # 2. When TRUST_PROXY_HEADERS is true, extracts CF-Connecting-IP first, else rightmost X-Forwarded-For hop
    assert (
        _extract_client_ip(
            {"x-forwarded-for": "203.0.113.195, 70.41.3.18"},
            env={"TRUST_PROXY_HEADERS": "1"},
        )
        == "70.41.3.18"
    )
    assert (
        _extract_client_ip(
            {"CF-Connecting-IP": "198.51.100.77", "X-Forwarded-For": "203.0.113.195, 70.41.3.18"},
            env={"TRUST_PROXY_HEADERS": "1"},
        )
        == "198.51.100.77"
    )
    assert _extract_client_ip({}, env={"TRUST_PROXY_HEADERS": "1"}) == "direct_client"

    # 3. Auth failure tracker bounding
    from core.slack_security import _MAX_TRACKER_ENTRIES, _auth_failure_lock, _auth_failure_tracker

    with _auth_failure_lock:
        _auth_failure_tracker.clear()
        for i in range(_MAX_TRACKER_ENTRIES + 10):
            _auth_failure_tracker[f"ip_{i}"] = {"window_start": time.time(), "logged_count": 1, "suppressed_count": 0}

    with patch("services.audit_logger.log_audit_event"):
        _record_auth_failure_audit("ip_new")
        with _auth_failure_lock:
            assert len(_auth_failure_tracker) <= _MAX_TRACKER_ENTRIES


# =========================================================================
# TKT-728: Downloads Cleanup, Fast mtime RAG Diff, Flat History Disambiguation
# =========================================================================
def test_tkt_728_cleanup_old_downloads(tmp_path: Path):
    downloads_dir = tmp_path / "downloads"
    downloads_dir.mkdir()

    old_file = downloads_dir / "old.pdf"
    old_file.write_bytes(b"%PDF-old")
    # Set mtime to 48 hours ago
    past_mtime = time.time() - (48 * 3600)
    os.utime(old_file, (past_mtime, past_mtime))

    new_file = downloads_dir / "new.pdf"
    new_file.write_bytes(b"%PDF-new")

    evicted = cleanup_old_downloads(max_age_seconds=86400.0, downloads_dir=downloads_dir)
    assert evicted == 1
    assert not old_file.exists()
    assert new_file.exists()


def test_tkt_728_flat_history_disambiguation(tmp_path: Path):
    from services.flat_history_recorder import configure_paths, reset_paths

    configure_paths(tmp_path)
    try:
        # Create existing file for thread 1712345678.000100
        f1 = tmp_path / "thread_1712345678.md"
        f1.write_text("# History\n- **Thread TS:** `1712345678.000100`\n", encoding="utf-8")

        # Query for the same thread should return existing file
        found1 = get_flat_history_filepath("C1", "1712345678.000100")
        assert found1 == f1

        # Query for a different thread occurring in the same second (1712345678.000200)
        found2 = get_flat_history_filepath("C1", "1712345678.000200")
        assert found2 != f1
        assert "00200.md" in found2.name or found2.stem.endswith("_00200")
    finally:
        reset_paths()


def test_tkt_728_thread_notes_atomic_tmp_cleanup(tmp_path: Path):
    target_path = tmp_path / "test_note.md"
    with patch("os.replace", side_effect=OSError("Disk write error")):
        ok = _atomic_write_file(target_path, "note content")
        assert ok is False
        # All temp files should be unlinked
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0


def test_tkt_728_rag_diff_mtime_fastpath(tmp_path: Path):
    manifest_mgr = ManifestManager(manifest_path=tmp_path / "manifest.json")
    doc_file = tmp_path / "shared" / "guide.md"
    doc_file.parent.mkdir(parents=True)
    doc_file.write_text("Original content", encoding="utf-8")
    stat = doc_file.stat()

    manifest_mgr.record_document(
        rel_doc_id="shared/guide.md",
        agent_id="shared",
        sha256="fake_sha",
        mtime=stat.st_mtime,
        chunk_ids=["c1"],
    )

    with patch("services.rag.manifest_manager.compute_sha256") as mock_sha:
        diff = manifest_mgr.diff_filesystem(root_dir=tmp_path, use_mtime_fastpath=True)
        assert "shared/guide.md" in diff["unchanged"]
        # compute_sha256 should NOT be called for unchanged mtime
        mock_sha.assert_not_called()


# =========================================================================
# TKT-729: Fail-Closed PinnedIPTransport, Pricing Cache & FileContext
# =========================================================================
def test_tkt_729_pinned_ip_transport_fail_closed():
    # Creating transport without valid httpx pool attributes should raise RuntimeError
    with patch("httpx.AsyncHTTPTransport.__init__"):
        with pytest.raises(RuntimeError, match="PinnedIPTransport cannot bind PinnedIPBackend"):
            PinnedIPTransport("93.184.216.34")


def test_tkt_729_file_context_serialization():
    ctx = FileContext(
        file_name="spec.pdf",
        content="Document text",
        size=1024,
        truncated=False,
        authorized_agents={"agent-10002", "agent-10001"},
    )
    d = ctx.to_dict()
    assert d["authorized_agents"] == ["agent-10001", "agent-10002"]

    # Reconstruct from dict
    restored = FileContext.from_dict(d)
    assert restored.authorized_agents == {"agent-10001", "agent-10002"}
    assert isinstance(restored.authorized_agents, set)


def test_tkt_729_pricing_service_mem_cache_and_atomic_save(tmp_path: Path):
    cache_file = tmp_path / "model_pricing_cache.json"
    service = PricingService(cache_file=cache_file)

    data = {"test-model": {"prompt_price_per_1m": 1.0, "completion_price_per_1m": 2.0}}
    service.save_cache(data)
    assert cache_file.exists()

    # First load should read from disk and populate memory cache
    loaded1 = service.load_cache()
    assert loaded1 == data

    # Provide custom read_text_fn to verify subsequent load uses memory cache without disk read
    mock_read = MagicMock(side_effect=RuntimeError("Disk read should not occur when mtime cache is valid"))
    loaded2 = service.load_cache(read_text_fn=mock_read)
    assert loaded2 == data
    mock_read.assert_not_called()
