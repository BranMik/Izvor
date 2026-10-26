import copy
import shutil

import pytest

import orchestration.agent_settings as agent_settings_mod
import orchestration.app_settings as app_settings_mod
import services.pricing_service as pricing_service_mod
from orchestration.agent_settings import (
    BACKUP_FILE as REAL_BACKUP_FILE,
)
from orchestration.agent_settings import (
    SETTINGS_FILE as REAL_SETTINGS_FILE,
)
from orchestration.agent_settings import (
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
    Each test gets an isolated copy in tmp_path.
    """
    temp_pricing_cache = tmp_path / "model_pricing_cache.json"
    if REAL_PRICING_CACHE_FILE.exists():
        shutil.copy2(REAL_PRICING_CACHE_FILE, temp_pricing_cache)
    monkeypatch.setattr(pricing_service_mod, "PRICING_CACHE_FILE", temp_pricing_cache)
    yield


@pytest.fixture(autouse=True)
def isolate_agent_settings_for_tests(tmp_path, monkeypatch):
    """
    Prevents tests from modifying or overwriting production data/settings/agent_settings.json.
    Each test gets an isolated copy in tmp_path.
    """
    temp_settings = tmp_path / "agent_settings.json"
    temp_backup = tmp_path / "agent_settings_backup.json"

    if REAL_SETTINGS_FILE.exists():
        shutil.copy2(REAL_SETTINGS_FILE, temp_settings)
    if REAL_BACKUP_FILE.exists():
        shutil.copy2(REAL_BACKUP_FILE, temp_backup)

    monkeypatch.setattr(agent_settings_mod, "SETTINGS_FILE", temp_settings)
    monkeypatch.setattr(agent_settings_mod, "BACKUP_FILE", temp_backup)

    # Save original in-memory state
    orig_enabled = set(settings_manager.enabled_agents)
    orig_orchestration_mode = settings_manager.orchestration_mode
    orig_sandwich_start = list(settings_manager.sandwich_start_chain)
    orig_sandwich_end = list(settings_manager.sandwich_end_chain)
    orig_fixed = list(settings_manager.fixed_chain)
    orig_agents_config = copy.deepcopy(settings_manager.agents_config)

    yield

    # Restore in-memory state in-place to preserve reference identity (e.g. ENABLED_DEBATE_AGENTS)
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


@pytest.fixture(autouse=True)
def isolate_app_settings_for_tests(tmp_path, monkeypatch):
    """
    Prevents tests from modifying or overwriting production data/settings/app_settings.json.
    Each test gets an isolated copy in tmp_path.
    """
    from orchestration.app_settings import (
        APP_BACKUP_FILE as REAL_APP_BACKUP_FILE,
        APP_SETTINGS_FILE as REAL_APP_SETTINGS_FILE,
        app_settings_manager,
    )

    temp_app_settings = tmp_path / "app_settings.json"
    temp_app_backup = tmp_path / "app_settings_backup.json"

    if REAL_APP_SETTINGS_FILE.exists():
        shutil.copy2(REAL_APP_SETTINGS_FILE, temp_app_settings)
    if REAL_APP_BACKUP_FILE.exists():
        shutil.copy2(REAL_APP_BACKUP_FILE, temp_app_backup)

    monkeypatch.setattr(app_settings_mod, "APP_SETTINGS_FILE", temp_app_settings)
    monkeypatch.setattr(app_settings_mod, "APP_BACKUP_FILE", temp_app_backup)

    # Save original in-memory state
    orig_expiration = app_settings_manager.thread_skill_expiration_time
    orig_cb_enabled = app_settings_manager.circuit_breaker_enabled
    orig_cb_threshold = app_settings_manager.circuit_breaker_threshold_usd
    orig_daily_budget = app_settings_manager.daily_budget_usd
    orig_scribe_enabled = app_settings_manager.scribe_enabled
    orig_weekly_pricing = app_settings_manager.weekly_pricing_update_enabled
    orig_workspace_context = app_settings_manager.workspace_context

    orig_default_settings_file = app_settings_manager.default_settings_file
    orig_default_backup_file = app_settings_manager.default_backup_file
    app_settings_manager.default_settings_file = temp_app_settings
    app_settings_manager.default_backup_file = temp_app_backup
    app_settings_manager._store.settings_file = temp_app_settings
    app_settings_manager._store.backup_file = temp_app_backup
    app_settings_manager._store.invalidate_cache()
    orig_last_mtime = app_settings_manager._last_loaded_mtime

    yield

    # Restore in-memory state
    app_settings_manager.thread_skill_expiration_time = orig_expiration
    app_settings_manager.circuit_breaker_enabled = orig_cb_enabled
    app_settings_manager.circuit_breaker_threshold_usd = orig_cb_threshold
    app_settings_manager.daily_budget_usd = orig_daily_budget
    app_settings_manager.scribe_enabled = orig_scribe_enabled
    app_settings_manager.weekly_pricing_update_enabled = orig_weekly_pricing
    app_settings_manager.workspace_context = orig_workspace_context
    app_settings_manager.default_settings_file = orig_default_settings_file
    app_settings_manager.default_backup_file = orig_default_backup_file
    app_settings_manager._last_loaded_mtime = orig_last_mtime
    app_settings_manager._store.settings_file = orig_default_settings_file
    app_settings_manager._store.backup_file = orig_default_backup_file
    app_settings_manager._store.invalidate_cache()


@pytest.fixture(autouse=True)
def isolate_database_for_tests(tmp_path, monkeypatch):
    """
    Bulletproof SQLite database isolation for tests.
    Each test gets a dedicated temporary SQLite database initialized with all schemas and WAL pragmas.
    """
    import core.database as db_mod
    from core.database import db_manager, init_db

    test_db_path = tmp_path / "test_izvor.db"
    monkeypatch.setenv("IZVOR_DB_PATH", str(test_db_path))
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", test_db_path)
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
    import agents.usage_agent as usage_mod
    from orchestration.thread_skills_manager import thread_skills_manager

    thread_skills_manager.clear_all()

    temp_usage_file = tmp_path / "thread_usage_data.json"
    temp_usage_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(usage_mod, "USAGE_STORE_FILE", temp_usage_file)
    from agents.usage_agent import usage_tracker
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
        if not texts: return []
        return [[0.0] * 768 for _ in texts]
    monkeypatch.setattr(EmbeddingService, 'get_embeddings', dummy_get_embeddings)
    yield
