import copy
import inspect
import shutil
from unittest.mock import MagicMock

import pytest

from orchestration.agent_settings import (
    BACKUP_FILE as REAL_BACKUP_FILE,
    SETTINGS_FILE as REAL_SETTINGS_FILE,
    SYSTEM_AGENT_BACKUP_FILE as REAL_SYSTEM_AGENT_BACKUP_FILE,
    SYSTEM_AGENT_SETTINGS_FILE as REAL_SYSTEM_AGENT_SETTINGS_FILE,
    settings_manager,
)
from services.pricing_service import PRICING_CACHE_FILE as REAL_PRICING_CACHE_FILE
from services.audit_logger import audit_logger


@pytest.fixture(autouse=True)
def isolate_audit_logger_for_tests(tmp_path):
    """
    Bulletproof isolation of the AuditLogger.
    Forces all audit logging during pytest runs to write to a temporary directory.
    Because this is a pytest fixture, it cannot be triggered in production.
    """
    test_audit_dir = tmp_path / "audit"
    test_audit_dir.mkdir(parents=True, exist_ok=True)

    # Hijack the singleton's directories
    old_dir = audit_logger.audit_dir

    audit_logger.audit_dir = test_audit_dir
    audit_logger.invalidate_cache()

    yield

    # Restore (optional, but good practice)
    audit_logger.audit_dir = old_dir
    audit_logger.invalidate_cache()


@pytest.fixture(autouse=True)
def isolate_pricing_cache_for_tests(tmp_path, monkeypatch):
    """
    Prevents tests from modifying or overwriting production data/pricing/model_pricing_cache.json.
    Each test gets an isolated copy in tmp_path / "_conftest_iso".
    """
    iso_dir = tmp_path / "_conftest_iso"
    iso_dir.mkdir(parents=True, exist_ok=True)
    temp_pricing_cache = iso_dir / "model_pricing_cache.json"
    if REAL_PRICING_CACHE_FILE.exists():
        shutil.copy2(REAL_PRICING_CACHE_FILE, temp_pricing_cache)
    monkeypatch.setenv("IZVOR_PRICING_CACHE_FILE", str(temp_pricing_cache))
    from services.pricing_service import pricing_service

    pricing_service.configure_paths(cache_file=temp_pricing_cache)
    yield
    pricing_service.reset_paths()


@pytest.fixture(autouse=True)
def isolate_agent_settings_for_tests(tmp_path):
    """
    Prevents tests from modifying or overwriting config/settings/agent_settings.json,
    engine/settings/system_agent_settings.json, and config/workflows/*.json.
    Each test gets an isolated copy in tmp_path / "_conftest_iso".
    """
    iso_dir = tmp_path / "_conftest_iso"
    iso_dir.mkdir(parents=True, exist_ok=True)
    temp_settings = iso_dir / "agent_settings.json"
    temp_backup = iso_dir / "agent_settings_backup.json"
    temp_sys_settings = iso_dir / "system_agent_settings.json"
    temp_sys_backup = iso_dir / "system_agent_settings_backup.json"

    if REAL_SETTINGS_FILE.exists():
        shutil.copy2(REAL_SETTINGS_FILE, temp_settings)
    if REAL_BACKUP_FILE.exists():
        shutil.copy2(REAL_BACKUP_FILE, temp_backup)
    if REAL_SYSTEM_AGENT_SETTINGS_FILE.exists():
        shutil.copy2(REAL_SYSTEM_AGENT_SETTINGS_FILE, temp_sys_settings)
    if REAL_SYSTEM_AGENT_BACKUP_FILE.exists():
        shutil.copy2(REAL_SYSTEM_AGENT_BACKUP_FILE, temp_sys_backup)

    # Isolate workflows directory
    temp_workflows_dir = iso_dir / "workflows"
    temp_workflows_dir.mkdir(parents=True, exist_ok=True)
    real_workflows_dir = settings_manager.workflow_manager.workflows_dir
    if real_workflows_dir.exists():
        for wf_file in real_workflows_dir.glob("*.json"):
            shutil.copy2(wf_file, temp_workflows_dir / wf_file.name)

    orig_active_wf_id = settings_manager.active_workflow_id

    settings_manager.configure_paths(
        settings_file=temp_settings,
        backup_file=temp_backup,
        system_agent_settings_file=temp_sys_settings,
        system_agent_backup_file=temp_sys_backup,
        workflows_dir=temp_workflows_dir,
    )

    # Save original in-memory state
    orig_enabled = set(settings_manager.enabled_agents)
    orig_orchestration_mode = settings_manager.orchestration_mode
    orig_sandwich_start = list(settings_manager.sandwich_start_chain)
    orig_sandwich_end = list(settings_manager.sandwich_end_chain)
    orig_fixed = list(settings_manager.fixed_chain)
    orig_agents_config = copy.deepcopy(settings_manager.agents_config)
    orig_system_agents = copy.deepcopy(settings_manager.system_agents_config)
    orig_orchestrator = copy.deepcopy(settings_manager.orchestrator_config)

    yield

    # Restore in-memory state in-place
    settings_manager.enabled_agents.clear()
    settings_manager.enabled_agents.update(orig_enabled)
    settings_manager.orchestration_mode = orig_orchestration_mode
    settings_manager.sandwich_start_chain.clear()
    settings_manager.sandwich_start_chain.extend(orig_sandwich_start)
    settings_manager.sandwich_end_chain.clear()
    settings_manager.sandwich_end_chain.extend(orig_sandwich_end)
    settings_manager.fixed_chain.clear()
    settings_manager.fixed_chain.extend(orig_fixed)
    settings_manager.agents_config.clear()
    settings_manager.agents_config.update(orig_agents_config)
    settings_manager.system_agents_config.clear()
    settings_manager.system_agents_config.update(orig_system_agents)
    settings_manager.orchestrator_config.clear()
    settings_manager.orchestrator_config.update(orig_orchestrator)

    settings_manager.reset_paths()
    settings_manager.active_workflow_id = orig_active_wf_id


@pytest.fixture(autouse=True)
def isolate_app_settings_for_tests(tmp_path):
    """
    Prevents tests from modifying or overwriting production data/state/app_settings.json.
    Each test gets an isolated copy in tmp_path / "_conftest_iso".
    """
    from orchestration.app_settings import (
        APP_BACKUP_FILE as REAL_APP_BACKUP_FILE,
        APP_SETTINGS_FILE as REAL_APP_SETTINGS_FILE,
        app_settings_manager,
    )

    iso_dir = tmp_path / "_conftest_iso"
    iso_dir.mkdir(parents=True, exist_ok=True)
    temp_app_settings = iso_dir / "app_settings.json"
    temp_app_backup = iso_dir / "app_settings_backup.json"

    if REAL_APP_SETTINGS_FILE.exists():
        shutil.copy2(REAL_APP_SETTINGS_FILE, temp_app_settings)
    if REAL_APP_BACKUP_FILE.exists():
        shutil.copy2(REAL_APP_BACKUP_FILE, temp_app_backup)

    # Save original in-memory state
    orig_expiration = app_settings_manager.thread_skill_expiration_time
    orig_cb_enabled = app_settings_manager.circuit_breaker_enabled
    orig_cb_threshold = app_settings_manager.circuit_breaker_threshold_usd
    orig_daily_budget = app_settings_manager.daily_budget_usd
    orig_scribe_enabled = app_settings_manager.scribe_enabled
    orig_weekly_pricing = app_settings_manager.weekly_pricing_update_enabled
    orig_workspace_context = app_settings_manager.workspace_context

    app_settings_manager.configure_paths(
        settings_file=temp_app_settings,
        backup_file=temp_app_backup,
    )

    yield

    # Restore in-memory state
    app_settings_manager.thread_skill_expiration_time = orig_expiration
    app_settings_manager.circuit_breaker_enabled = orig_cb_enabled
    app_settings_manager.circuit_breaker_threshold_usd = orig_cb_threshold
    app_settings_manager.daily_budget_usd = orig_daily_budget
    app_settings_manager.scribe_enabled = orig_scribe_enabled
    app_settings_manager.weekly_pricing_update_enabled = orig_weekly_pricing
    app_settings_manager.workspace_context = orig_workspace_context
    app_settings_manager.reset_paths()


@pytest.fixture(autouse=True)
def isolate_database_for_tests(tmp_path, monkeypatch):
    """
    Bulletproof SQLite database isolation for tests.
    Each test gets a dedicated temporary SQLite database initialized with all schemas and WAL pragmas.
    """
    from core.database import db_manager, init_db

    iso_dir = tmp_path / "_conftest_iso"
    iso_dir.mkdir(parents=True, exist_ok=True)
    test_db_path = iso_dir / "test_izvor.db"
    monkeypatch.setenv("IZVOR_DB_PATH", str(test_db_path))
    old_custom_path = db_manager._custom_db_path
    db_manager._custom_db_path = test_db_path

    init_db(test_db_path)

    yield

    db_manager.close_all_connections()
    db_manager._custom_db_path = old_custom_path


@pytest.fixture(autouse=True)
def isolate_thread_state_files_for_tests(tmp_path, monkeypatch):
    """
    Prevents tests from modifying or overwriting data/thread/thread_usage_data.json
    and ensures clean thread skills state in SQLite.
    """
    from agents.usage_agent import usage_tracker
    from orchestration.thread_skills_manager import thread_skills_manager

    thread_skills_manager.clear_all()

    iso_dir = tmp_path / "_conftest_iso"
    iso_dir.mkdir(parents=True, exist_ok=True)
    temp_usage_file = iso_dir / "thread_usage_data.json"
    temp_usage_file.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("IZVOR_THREAD_DATA_DIR", str(iso_dir))

    old_usage_storage = usage_tracker.store_file
    usage_tracker.store_file = temp_usage_file
    old_usage_data = dict(usage_tracker.thread_usage_data)
    usage_tracker.thread_usage_data.clear()

    yield

    thread_skills_manager.clear_all()
    usage_tracker.store_file = old_usage_storage
    usage_tracker.thread_usage_data.clear()
    usage_tracker.thread_usage_data.update(old_usage_data)


@pytest.fixture(autouse=True)
def mock_embedding_service_for_tests(monkeypatch):
    from services.rag.embeddings import EmbeddingService

    async def dummy_get_embeddings(self, texts):
        if not texts:
            return []
        return [[0.0] * 768 for _ in texts]

    monkeypatch.setattr(EmbeddingService, "get_embeddings", dummy_get_embeddings)
    yield


@pytest.fixture(autouse=True)
def isolate_system_settings_for_tests(tmp_path):
    """
    Prevents tests from modifying config/settings/system_settings.json
    and data/backups/system_settings_backup.json.
    """
    from orchestration.system_settings import (
        SYSTEM_SETTINGS_FILE as REAL_SYS_SETTINGS_FILE,
        SYSTEM_BACKUP_FILE as REAL_SYS_BACKUP_FILE,
        system_settings_manager,
    )

    iso_dir = tmp_path / "_conftest_iso"
    iso_dir.mkdir(parents=True, exist_ok=True)
    temp_sys_settings = iso_dir / "system_settings.json"
    temp_sys_backup = iso_dir / "system_settings_backup.json"

    if REAL_SYS_SETTINGS_FILE.exists():
        shutil.copy2(REAL_SYS_SETTINGS_FILE, temp_sys_settings)
    if REAL_SYS_BACKUP_FILE.exists():
        shutil.copy2(REAL_SYS_BACKUP_FILE, temp_sys_backup)

    orig_raw_data = copy.deepcopy(getattr(system_settings_manager, "_raw_data", {}))

    system_settings_manager.configure_paths(
        settings_file=temp_sys_settings,
        backup_file=temp_sys_backup,
    )

    yield

    system_settings_manager._raw_data = orig_raw_data
    system_settings_manager.reset_paths()


@pytest.fixture(autouse=True)
def isolate_downloads_dir_for_tests(request, tmp_path, monkeypatch):
    """
    Prevents tests from leaking downloaded test files into data/downloads.
    """
    if "test_paths" in request.node.nodeid:
        yield
        return
    iso_dir = tmp_path / "_conftest_iso"
    iso_dir.mkdir(parents=True, exist_ok=True)
    temp_downloads = iso_dir / "downloads"
    temp_downloads.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("IZVOR_DOWNLOADS_DIR", str(temp_downloads))
    yield


@pytest.fixture(autouse=True)
def isolate_flat_histories_and_notes_for_tests(tmp_path, monkeypatch):
    """
    Prevents tests from creating flat history and thread note files in real directories.
    Provides fast pacing for multi-turn execution without production test seams.
    """
    from services.flat_history_recorder import configure_paths as configure_flat_paths, reset_paths as reset_flat_paths
    from services.thread_notes_writer import configure_paths as configure_note_paths, reset_paths as reset_note_paths

    iso_dir = tmp_path / "_conftest_iso"
    iso_dir.mkdir(parents=True, exist_ok=True)
    temp_flat = iso_dir / "flat-histories"
    temp_flat.mkdir(parents=True, exist_ok=True)
    temp_notes = iso_dir / "thread_notes"
    temp_notes.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("IZVOR_HISTORIES_DIR", str(temp_flat))
    monkeypatch.setenv("IZVOR_THREAD_NOTES_DIR", str(temp_notes))
    monkeypatch.setenv("IZVOR_DEBATE_TURN_PACING_SECONDS", "0.01")
    monkeypatch.setenv("IZVOR_SLACK_POST_PAUSE_SECONDS", "0.01")

    configure_flat_paths(temp_flat)
    configure_note_paths(temp_notes)

    yield

    reset_flat_paths()
    reset_note_paths()


@pytest.fixture(autouse=True)
def isolate_workspace_tests_for_tests():
    """
    Cleans up any scratch or temporary test files written into agents_resources or workspace.
    """
    yield
    from core.paths import AGENTS_RESOURCES_DIR, PROJECT_ROOT

    for base_d in (AGENTS_RESOURCES_DIR, PROJECT_ROOT):
        t_dir = base_d / "tests"
        if t_dir.exists():
            for f in t_dir.glob("*.tmp"):
                try:
                    f.unlink(missing_ok=True)
                except OSError:
                    pass
            for f in t_dir.glob("*scratch*"):
                try:
                    f.unlink(missing_ok=True)
                except OSError:
                    pass


def make_mock_acompletion(agent_turn_handler=None, default_content="Response text"):
    """
    Creates an AsyncMock for litellm.acompletion that automatically handles
    system agent calls (Language Detector, Intent Extractor, Internal Revisor)
    while routing debate agent calls to `agent_turn_handler` or returning a standard mock choice.
    """
    async def _mock_acompletion(*args, **kwargs):
        messages = kwargs.get("messages") or (
            args[0].get("messages")
            if args and isinstance(args[0], dict)
            else (args[2] if len(args) > 2 else [])
        )
        sys_content = ""
        for m in messages:
            if isinstance(m, dict) and m.get("role") == "system":
                sys_content += str(m.get("content", ""))

        if "language_detector" in sys_content.lower() or "language identification" in sys_content.lower():
            m = MagicMock()
            m.message.content = "English"
            m.message.tool_calls = None
            return MagicMock(choices=[m], usage=None)

        if "intent_extractor" in sys_content.lower() or "intent" in sys_content.lower():
            m = MagicMock()
            m.message.content = '{"detected_language": "English", "directives": {}}'
            m.message.tool_calls = None
            return MagicMock(choices=[m], usage=None)

        if "internalrevisor" in sys_content.lower() or "revisor" in sys_content.lower():
            m = MagicMock()
            m.message.content = "VERDICT: APPROVED"
            m.message.tool_calls = None
            return MagicMock(choices=[m], usage=None)

        if agent_turn_handler is not None:
            res = agent_turn_handler(*args, **kwargs)
            if inspect.isawaitable(res):
                return await res
            return res

        m = MagicMock()
        m.message.content = default_content
        m.message.tool_calls = None
        return MagicMock(choices=[m], usage=None)

    return _mock_acompletion

