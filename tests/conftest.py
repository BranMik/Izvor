import copy
import shutil

import pytest

import orchestration.agent_settings as agent_settings_mod
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
    old_backup = audit_logger.backup_dir
    old_manifest = audit_logger.manifest_file
    
    audit_logger.audit_dir = test_audit_dir
    audit_logger.backup_dir = test_audit_dir / "backups"
    audit_logger.manifest_file = test_audit_dir / "manifest.sha256"
    audit_logger._ensure_dirs()
    
    yield
    
    # Restore (optional, but good practice)
    audit_logger.audit_dir = old_dir
    audit_logger.backup_dir = old_backup
    audit_logger.manifest_file = old_manifest


@pytest.fixture(autouse=True)
def isolate_pricing_cache_for_tests(tmp_path, monkeypatch):
    """
    Prevents tests from modifying or overwriting production data/model_pricing_cache.json.
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
    Prevents tests from modifying or overwriting production data/agent_settings.json.
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
    orig_circuit_breaker_enabled = settings_manager.circuit_breaker_enabled
    orig_circuit_breaker_threshold_usd = settings_manager.circuit_breaker_threshold_usd
    orig_orchestration_mode = settings_manager.orchestration_mode
    orig_sandwich_start = list(settings_manager.sandwich_start_chain)
    orig_sandwich_end = list(settings_manager.sandwich_end_chain)
    orig_fixed = list(settings_manager.fixed_chain)
    orig_agents_config = copy.deepcopy(settings_manager.agents_config)

    yield

    # Restore in-memory state in-place to preserve reference identity (e.g. ENABLED_DEBATE_AGENTS)
    settings_manager.enabled_agents.clear()
    settings_manager.enabled_agents.update(orig_enabled)
    settings_manager.circuit_breaker_enabled = orig_circuit_breaker_enabled
    settings_manager.circuit_breaker_threshold_usd = orig_circuit_breaker_threshold_usd
    settings_manager.orchestration_mode = orig_orchestration_mode
    settings_manager.sandwich_start_chain.clear()
    settings_manager.sandwich_start_chain.extend(orig_sandwich_start)
    settings_manager.sandwich_end_chain.clear()
    settings_manager.sandwich_end_chain.extend(orig_sandwich_end)
    settings_manager.fixed_chain.clear()
    settings_manager.fixed_chain.extend(orig_fixed)
    settings_manager.agents_config.clear()
    settings_manager.agents_config.update(orig_agents_config)
