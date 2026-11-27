from __future__ import annotations

import asyncio
import os
import tomllib
from unittest.mock import AsyncMock, MagicMock

import pytest

import main
from core.config import get_app_version
from core.database import db_manager
from core.paths import PYPROJECT_FILE
from core.task_utils import _ACTIVE_TASKS, safe_create_task
from orchestration.protocols import PipelineDeps, resolve_pipeline_deps
from orchestration.turn_pipeline import TurnPipeline


def test_fastapi_app_version_aligned_with_pyproject():
    """TKT-1227: FastAPI version must match pyproject.toml [project].version (SSOT)."""
    with open(PYPROJECT_FILE, "rb") as f:
        expected_version = tomllib.load(f)["project"]["version"].strip()
    assert main.app.version == expected_version
    assert main.app.version == get_app_version()


def test_get_app_version_reads_stripped_version_from_custom_pyproject(tmp_path):
    """get_app_version returns stripped [project].version from an injected pyproject.toml path."""
    custom_pyproject = tmp_path / "pyproject.toml"
    custom_pyproject.write_text('[project]\nname = "izvor"\nversion = "  1.2.3  "\n', encoding="utf-8")
    assert get_app_version(pyproject_path=custom_pyproject) == "1.2.3"


def test_get_app_version_raises_when_pyproject_missing(tmp_path):
    """get_app_version fails fast with FileNotFoundError when pyproject.toml is missing."""
    missing = tmp_path / "nonexistent.toml"
    with pytest.raises(FileNotFoundError, match="Missing pyproject.toml"):
        get_app_version(pyproject_path=missing)


@pytest.mark.parametrize(
    "toml_content",
    [
        '[tool.ruff]\nline-length = 120\n',
        '[project]\nname = "izvor"\n',
        '[project]\nname = "izvor"\nversion = "   "\n',
        '[project]\nname = "izvor"\nversion = 10\n',
    ],
)
def test_get_app_version_raises_value_error_on_invalid_schema(tmp_path, toml_content):
    """get_app_version fails fast with ValueError when [project] or [project].version is invalid."""
    bad_pyproject = tmp_path / "pyproject.toml"
    bad_pyproject.write_text(toml_content, encoding="utf-8")
    with pytest.raises(ValueError):
        get_app_version(pyproject_path=bad_pyproject)


@pytest.mark.anyio
async def test_lifespan_rejects_web_concurrency_gt_1():
    """TKT-1227: WEB_CONCURRENCY > 1 raises RuntimeError fail-fast on startup."""
    prev = os.environ.get("WEB_CONCURRENCY")
    os.environ["WEB_CONCURRENCY"] = "2"
    try:
        with pytest.raises(RuntimeError, match="single worker is required"):
            async with main.lifespan(main.app):
                pass
    finally:
        if prev is None:
            os.environ.pop("WEB_CONCURRENCY", None)
        else:
            os.environ["WEB_CONCURRENCY"] = prev


@pytest.mark.anyio
async def test_drain_active_tasks_and_close_db_shuts_down_cleanly(tmp_path):
    """TKT-1227: Shutdown cancels/drains tracked active tasks and closes SQLite connections."""
    prev_db_path = db_manager.db_path
    db_path = tmp_path / "lifespan_test.db"
    db_manager.configure_paths(db_path=db_path)
    try:
        with db_manager.get_connection() as conn:
            conn.execute("SELECT 1")
        assert len(db_manager._thread_conns) >= 1

        started = asyncio.Event()
        cancelled_flag = False

        async def _background_worker():
            nonlocal cancelled_flag
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled_flag = True
                raise

        task = safe_create_task(_background_worker(), name="test_drain_worker")
        await started.wait()
        assert task in _ACTIVE_TASKS

        await main._drain_active_tasks_and_close_db(timeout=2.0)

        assert cancelled_flag is True
        assert task.done()
        assert task not in _ACTIVE_TASKS
        assert len(db_manager._thread_conns) == 0
    finally:
        db_manager.close_all_connections()
        db_manager.configure_paths(db_path=prev_db_path)


def test_pipeline_deps_preserves_falsy_zero_overrides():
    """TKT-1225: resolve_pipeline_deps preserves explicit 0.0 / 0 numeric overrides via _first_not_none."""
    deps = PipelineDeps(
        debate_turn_pacing_seconds=0.0,
        slack_post_pause_seconds=0.0,
        max_tool_iterations=0,
        timeout_secs=0.0,
        intermediate_delay_secs=0.0,
    )
    resolved = resolve_pipeline_deps(
        deps,
        llm_executor=AsyncMock(),
        slack_poster=AsyncMock(),
        history_fetcher=AsyncMock(),
        files_fetcher=AsyncMock(),
    )
    assert resolved.debate_turn_pacing_seconds == 0.0
    assert resolved.slack_post_pause_seconds == 0.0
    assert resolved.max_tool_iterations == 0
    assert resolved.timeout_secs == 0.0
    assert resolved.intermediate_delay_secs == 0.0


def test_turn_pipeline_setters_propagate_deps_to_all_stages():
    """TKT-1225: Updating TurnPipeline DI setters propagates updated deps to all 5 stages."""
    pipeline = TurnPipeline()
    stages = (
        pipeline.validation_stage,
        pipeline.resolution_stage,
        pipeline.context_prep_stage,
        pipeline.execution_stage,
        pipeline.post_record_stage,
    )

    custom_sys_poster = AsyncMock()
    pipeline.post_system_message_fn = custom_sys_poster
    for stage in stages:
        assert stage.deps.post_system_message_fn is custom_sys_poster

    custom_slack_poster = AsyncMock()
    pipeline.slack_poster = custom_slack_poster
    for stage in stages:
        assert stage.deps.slack_poster is custom_slack_poster

    custom_adapter = MagicMock()
    pipeline.workspace_adapter = custom_adapter
    for stage in stages:
        assert stage.deps.workspace_adapter is custom_adapter


@pytest.mark.anyio
async def test_close_all_connections_spares_live_worker_thread_and_drains_telemetry(tmp_path):
    """TKT-1410: Telemetry futures drain cleanly and close_all_connections does not close foreign live connections."""
    import threading
    from core.database import db_manager
    from services import telemetry

    done_evt = threading.Event()
    release_evt = threading.Event()

    def _worker():
        with db_manager.get_connection() as conn:
            conn.execute("SELECT 1").fetchone()
        done_evt.set()
        release_evt.wait(timeout=2.0)

    prev_db_path = db_manager.db_path
    db_manager.configure_paths(db_path=tmp_path / "lifespan_foreign.db")
    db_manager.init_db()
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    try:
        assert done_evt.wait(timeout=2.0)
        telemetry.record_turn_trace(
            session_id="sess_drain",
            channel_id="C_DRAIN",
            thread_ts="100.1",
            agent_key="agent-10001",
            stage_name="stage4",
            duration_ms=12.5,
            status="SUCCESS",
        )
        await telemetry.drain_turn_traces(timeout=2.0)
        traces = telemetry.get_turn_traces(session_id="sess_drain")
        assert len(traces) == 1
        with db_manager._conns_lock:
            assert t.ident in db_manager._thread_conns
        db_manager.close_all_connections()
        with db_manager._conns_lock:
            assert t.ident in db_manager._thread_conns
    finally:
        release_evt.set()
        t.join(timeout=2.0)
        db_manager.close_all_connections()
        db_manager.configure_paths(db_path=prev_db_path)


@pytest.mark.anyio
async def test_lifespan_fails_fast_when_system_settings_missing(tmp_path):
    """Startup fails fast with FileNotFoundError when system_settings.json and backup are missing."""
    from config_store.system_settings import system_settings_manager

    missing_file = tmp_path / "missing_system_settings.json"
    missing_backup = tmp_path / "missing_system_settings_backup.json"
    system_settings_manager.configure_paths(
        settings_file=missing_file,
        backup_file=missing_backup,
    )
    try:
        with pytest.raises(FileNotFoundError, match="Required system settings file not found"):
            async with main.lifespan(main.app):
                pass
    finally:
        system_settings_manager.reset_paths()
