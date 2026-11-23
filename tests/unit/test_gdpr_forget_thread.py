"""Unit tests for TKT-1233: channel-scoped GDPR forget_thread, HTTP DELETE endpoint, and /forget_thread skill."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import pytest

from core.paths import set_downloads_dir_override
from services import flat_history_recorder, thread_notes_writer
from services.retention_service import forget_thread
from services.security.guardrail import _EMBEDDING_LRU_CACHE
from services.slack.files import cleanup_old_downloads, get_thread_downloads_subdir


def test_forget_thread_strict_channel_isolation_and_downloads_and_guardrail_purge(tmp_path: Path):
    """Same thread_ts in two different channels must stay isolated; forgetting C_ONE never deletes C_TWO."""
    flat_dir = tmp_path / "flat"
    notes_dir = tmp_path / "notes"
    dl_dir = tmp_path / "downloads"
    flat_history_recorder.configure_paths(flat_dir)
    thread_notes_writer.configure_paths(thread_notes_dir=notes_dir)
    set_downloads_dir_override(dl_dir)

    shared_ts = "1712345678.00100"
    try:
        # Write flat history, thread notes, and downloads for BOTH C_ONE and C_TWO sharing the same thread_ts
        p1_flat = flat_history_recorder.record_flat_history(
            channel_id="C_ONE",
            thread_ts=shared_ts,
            initial_prompt="Shared discussion topic",
            messages=[{"role": "user", "content": "Channel one secret"}],
        )
        p2_flat = flat_history_recorder.record_flat_history(
            channel_id="C_TWO",
            thread_ts=shared_ts,
            initial_prompt="Shared discussion topic",
            messages=[{"role": "user", "content": "Channel two secret"}],
        )
        assert p1_flat is not None and p2_flat is not None
        assert p1_flat != p2_flat
        assert "C_ONE" in p1_flat.name and "C_TWO" in p2_flat.name

        notes1 = thread_notes_writer.save_thread_notes_to_disk(
            channel_id="C_ONE",
            thread_ts=shared_ts,
            initial_prompt="Shared discussion topic",
            live_notes_text="Channel one live notes",
            conclusion_text="Channel one conclusion",
            force=True,
        )
        notes2 = thread_notes_writer.save_thread_notes_to_disk(
            channel_id="C_TWO",
            thread_ts=shared_ts,
            initial_prompt="Shared discussion topic",
            live_notes_text="Channel two live notes",
            conclusion_text="Channel two conclusion",
            force=True,
        )
        assert Path(notes1["live_notes_path"]) != Path(notes2["live_notes_path"])

        dl1_sub = get_thread_downloads_subdir("C_ONE", shared_ts, downloads_dir=dl_dir)
        dl2_sub = get_thread_downloads_subdir("C_TWO", shared_ts, downloads_dir=dl_dir)
        dl1_sub.mkdir(parents=True, exist_ok=True)
        dl2_sub.mkdir(parents=True, exist_ok=True)
        dl1_file = dl1_sub / "abc_doc1.txt"
        dl2_file = dl2_sub / "def_doc2.txt"
        dl1_file.write_text("file one", encoding="utf-8")
        dl2_file.write_text("file two", encoding="utf-8")

        _EMBEDDING_LRU_CACHE["gdpr_probe"] = [0.5, 0.5]
        assert len(_EMBEDDING_LRU_CACHE) >= 1

        # Forget ONLY C_ONE
        purged = forget_thread("C_ONE", shared_ts)
        assert purged["flat_histories"] == 1
        assert purged["thread_notes"] == 2
        assert purged["downloads"] == 1
        assert not p1_flat.exists()
        assert not Path(notes1["live_notes_path"]).exists()
        assert not Path(notes1["conclusion_path"]).exists()
        assert not dl1_sub.exists()
        assert len(_EMBEDDING_LRU_CACHE) == 0

        # C_TWO artifacts must remain completely untouched
        assert p2_flat.exists()
        assert Path(notes2["live_notes_path"]).exists()
        assert Path(notes2["conclusion_path"]).exists()
        assert dl2_file.exists()
    finally:
        flat_history_recorder.reset_paths()
        thread_notes_writer.reset_paths()
        set_downloads_dir_override(None)


def test_cleanup_old_downloads_recurses_thread_subdirs_and_prunes_empty_dirs(tmp_path: Path):
    """cleanup_old_downloads recurses into thread-scoped subdirectories and removes empty ones."""
    dl_root = tmp_path / "downloads"
    sub_old = get_thread_downloads_subdir("C_CLEAN", "1700000000.1", downloads_dir=dl_root)
    sub_keep = get_thread_downloads_subdir("C_KEEP", "1700000000.2", downloads_dir=dl_root)
    sub_old.mkdir(parents=True, exist_ok=True)
    sub_keep.mkdir(parents=True, exist_ok=True)

    f_old = sub_old / "old.txt"
    f_keep = sub_keep / "fresh.txt"
    f_old.write_text("expired", encoding="utf-8")
    f_keep.write_text("active", encoding="utf-8")
    stale_time = f_old.stat().st_mtime - 90000.0
    os.utime(f_old, (stale_time, stale_time))

    evicted = cleanup_old_downloads(max_age_seconds=3600.0, downloads_dir=dl_root)
    assert evicted == 1
    assert not f_old.exists()
    assert not sub_old.exists()
    assert f_keep.exists()
    assert sub_keep.exists()


def test_http_delete_thread_requires_admin_auth_and_emits_audit(tmp_path: Path):
    """DELETE /api/threads/{channel_id}/{thread_ts} requires Basic Auth and emits GDPR_THREAD_FORGOTTEN."""
    from main import app, verify_admin_basic_auth

    flat_dir = tmp_path / "flat"
    flat_history_recorder.configure_paths(flat_dir)
    target_ts = "1712345699.00555"
    hist_path = flat_history_recorder.record_flat_history(
        channel_id="C_HTTP",
        thread_ts=target_ts,
        initial_prompt="API GDPR test",
        messages=[{"role": "user", "content": "Delete via HTTP"}],
    )
    assert hist_path is not None and hist_path.exists()

    client = TestClient(app)
    try:
        # 1. Unauthenticated request must fail with 401
        unauth_resp = client.delete(f"/api/threads/C_HTTP/{target_ts}")
        assert unauth_resp.status_code == 401
        assert hist_path.exists()

        # 2. Authenticated admin request deletes thread data and emits GDPR_THREAD_FORGOTTEN
        app.dependency_overrides[verify_admin_basic_auth] = lambda: "sec_admin"
        with patch("services.audit_logger.log_audit_event_async", new_callable=AsyncMock) as mock_audit:
            resp = client.delete(f"/api/threads/C_HTTP/{target_ts}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "forgotten"
        assert body["channel_id"] == "C_HTTP"
        assert body["thread_ts"] == target_ts
        assert body["deleted"]["flat_histories"] == 1
        assert not hist_path.exists()

        mock_audit.assert_awaited_once()
        assert mock_audit.call_args.kwargs["event_type"] == "GDPR_THREAD_FORGOTTEN"
        assert mock_audit.call_args.kwargs["actor"] == {"type": "admin_user", "id": "sec_admin"}
        assert mock_audit.call_args.kwargs["target"] == f"C_HTTP:{target_ts}"
    finally:
        app.dependency_overrides.clear()
        flat_history_recorder.reset_paths()


@pytest.mark.anyio
async def test_slash_forget_thread_skill_admin_and_origin_enforcement(tmp_path: Path):
    """H20: /forget_thread skill requires admin AND origin=='slash', emits GDPR_THREAD_FORGOTTEN, and halts."""
    from core.models import ActionDirective
    from core.registry import get_skill_registry
    from orchestration.stages.directive_handlers import handle_slash_directives
    from orchestration.turn_pipeline import TurnContext, TurnInput
    from pipeline.directive_parser import DirectiveParser

    skill_registry = get_skill_registry()
    skill_registry.reload()
    skill_def = skill_registry.get("forget_thread")
    assert skill_def is not None
    assert skill_def.requires_admin is True

    # Parser parses /forget_thread cleanly without setting is_stop
    parsed = await DirectiveParser().parse("/forget_thread")
    assert not parsed.is_stop
    assert any(a.name == "forget_thread" and getattr(a, "origin", "slash") == "slash" for a in parsed.actions)

    flat_dir = tmp_path / "flat"
    flat_history_recorder.configure_paths(flat_dir)
    tts = "1712345700.00999"
    hist_path = flat_history_recorder.record_flat_history(
        channel_id="C_SLASH",
        thread_ts=tts,
        initial_prompt="Slash GDPR test",
        messages=[{"role": "user", "content": "Sensitive data"}],
    )
    assert hist_path is not None and hist_path.exists()

    post_sys_mock = AsyncMock()
    try:
        # 1. Non-admin user blocked even on origin='slash'
        ctx_non_admin = TurnContext(
            input=TurnInput(channel_id="C_SLASH", thread_ts=tts, raw_text="/forget_thread", user_id="U_REGULAR")
        )
        ctx_non_admin.actions = [ActionDirective(name="forget_thread", origin="slash")]
        with patch("core.config.is_slack_admin", return_value=False):
            res_denied = await handle_slash_directives(ctx_non_admin, SimpleNamespace(is_stop=False, actions=ctx_non_admin.actions), post_sys_mock)
        assert res_denied is not None and res_denied.get("halted") is True
        assert hist_path.exists()

        # 2. Inferred action rejected even when user IS admin
        ctx_inferred = TurnContext(
            input=TurnInput(channel_id="C_SLASH", thread_ts=tts, raw_text="please forget", user_id="U_ADMIN")
        )
        ctx_inferred.actions = [ActionDirective(name="forget_thread", origin="inferred")]
        with patch("core.config.is_slack_admin", return_value=True):
            res_inferred = await handle_slash_directives(ctx_inferred, SimpleNamespace(is_stop=False, actions=ctx_inferred.actions), post_sys_mock)
        assert res_inferred is None
        assert hist_path.exists()

        # 3. Authorized admin with origin='slash' executes GDPR purge, emits audit, and halts turn
        ctx_admin = TurnContext(
            input=TurnInput(channel_id="C_SLASH", thread_ts=tts, raw_text="/forget_thread", user_id="U_ADMIN")
        )
        ctx_admin.actions = [ActionDirective(name="forget_thread", origin="slash")]
        with (
            patch("core.config.is_slack_admin", return_value=True),
            patch("services.audit_logger.log_audit_event_async", new_callable=AsyncMock) as mock_audit,
        ):
            res_ok = await handle_slash_directives(ctx_admin, SimpleNamespace(is_stop=False, actions=ctx_admin.actions), post_sys_mock)

        assert res_ok is not None
        assert res_ok["ok"] is True
        assert res_ok["halted"] is True
        assert res_ok["action"] == "forget_thread"
        assert res_ok["deleted"]["flat_histories"] == 1
        assert not hist_path.exists()
        mock_audit.assert_awaited_once()
        assert mock_audit.call_args.kwargs["event_type"] == "GDPR_THREAD_FORGOTTEN"
        post_sys_mock.assert_awaited()
    finally:
        flat_history_recorder.reset_paths()

