"""
tests/unit/test_paths.py — Unit Tests for Centralized Paths Architecture.

Verifies:
1. Default canonical paths match 3-tier repository layout (config/, engine/, data/).
2. Environment variable overrides (IZVOR_*) properly take effect.
3. Zero internal application imports in core/paths.py (pure standard library).
4. No circular import deadlocks across core, orchestration, services, agents, prompts.
"""

import ast
import importlib

import core.paths as paths


def test_core_paths_imports_only_stdlib():
    """Verify that core.paths has zero internal application imports via AST inspection."""
    with open(paths.__file__, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())

    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_modules.add(node.module.split(".")[0])

    # Only standard library allowed in core.paths (os, pathlib, __future__)
    allowed = {"__future__", "os", "pathlib"}
    disallowed = imported_modules - allowed
    assert not disallowed, f"Disallowed imports found in core/paths.py: {disallowed}"


def test_default_paths_exist_or_resolve_to_repo_layout():
    """Verify default paths resolve correctly relative to PROJECT_ROOT."""
    root = paths.PROJECT_ROOT
    assert (paths.ENGINE_DIR / "core").is_dir()
    assert (root / "main.py").is_file()

    # 3-tier Architecture Roots
    assert paths.PYPROJECT_FILE == root / "pyproject.toml"
    assert paths.PYPROJECT_FILE.is_file()
    assert paths.CONFIG_DIR == root / "config"
    assert paths.ENGINE_DIR == root / "engine"
    assert paths.DATA_DIR == root / "data"

    # Config Tier
    assert paths.SETTINGS_DIR == root / "config" / "settings"
    assert paths.PERSONAS_DIR == root / "config" / "personas"
    assert paths.WORKFLOWS_DIR == root / "config" / "workflows"
    assert paths.SKILLS_DIR == root / "config" / "skills"
    assert paths.AVATARS_DIR == root / "config" / "avatars"
    assert paths.RAG_KNOWLEDGE_DIR == root / "config" / "rag_knowledge"
    assert paths.AGENT_SETTINGS_FILE == root / "config" / "settings" / "agent_settings.json"
    assert paths.SYSTEM_SETTINGS_FILE == root / "config" / "settings" / "system_settings.json"

    # Engine Tier
    assert paths.ENGINE_SETTINGS_DIR == root / "engine" / "settings"
    assert paths.ENGINE_WORKFLOWS_DIR == root / "engine" / "workflows"
    assert paths.ENGINE_SKILLS_DIR == root / "engine" / "skills" / "builtin"
    assert paths.ENGINE_AVATARS_DIR == root / "engine" / "avatars"
    assert paths.DEFAULT_SYSTEM_AGENT_SETTINGS_FILE == root / "engine" / "settings" / "system_agent_settings.json"
    assert paths.PROMPTS_DIR == root / "engine" / "prompts"
    assert paths.SYSTEM_AGENTS_DIR == root / "engine" / "prompts" / "system_agents"
    assert paths.PROMPT_TEMPLATES_DIR == root / "engine" / "prompts" / "templates"

    # Data Tier
    assert paths.STATE_DIR == root / "data" / "state"
    assert paths.AUDIT_DIR == root / "data" / "audit"
    assert paths.DOWNLOADS_DIR == root / "data" / "downloads"
    assert paths.FLAT_HISTORIES_DIR == root / "data" / "flat-histories"
    assert paths.BACKUPS_DIR == root / "data" / "backups"
    assert paths.WORKFLOWS_BACKUP_DIR == root / "data" / "backups" / "workflows"
    assert paths.PRICING_DIR == root / "data" / "pricing"
    assert paths.THREAD_DATA_DIR == root / "data" / "thread"
    assert paths.RAG_CACHE_DIR == root / "data" / ".rag_cache"
    assert paths.DB_PATH == root / "data" / "izvor.db"
    assert paths.APP_SETTINGS_FILE == root / "data" / "state" / "app_settings.json"
    assert paths.PRESIDIO_SKILL_EXCLUSIONS_FILE == root / "data" / "state" / "presidio_skill_exclusions.json"
    assert paths.SYSTEM_AGENT_SETTINGS_FILE == root / "data" / "settings" / "system_agent_settings.json"
    assert paths.AGENT_BACKUP_FILE == root / "data" / "backups" / "agent_settings_backup.json"
    assert paths.SYSTEM_AGENT_BACKUP_FILE == root / "data" / "backups" / "system_agent_settings_backup.json"
    assert paths.APP_BACKUP_FILE == root / "data" / "backups" / "app_settings_backup.json"
    assert paths.SYSTEM_BACKUP_FILE == root / "data" / "backups" / "system_settings_backup.json"

    # Pricing & Model Catalog
    assert paths.MODEL_PROVIDER_CATALOG_FILE == root / "data" / "pricing" / "model_provider_catalog.json"
    assert paths.PRICING_CACHE_FILE == root / "data" / "pricing" / "model_pricing_cache.json"
    assert paths.BASELINE_PRICES_FILE == root / "data" / "pricing" / "baseline_prices.json"

    # Resources & Notes
    assert paths.AGENTS_RESOURCES_DIR == root / "agents_resources"
    assert paths.THREAD_NOTES_DIR == root / "data" / "thread_notes"
    assert paths.SYSTEM_RESOURCES_DIR == root / "system_resources"


def test_environment_variable_overrides(tmp_path):
    """Verify that _resolve_env_path properly resolves custom directory overrides via dependency injection."""
    custom_data = tmp_path / "custom_data"
    custom_config = tmp_path / "custom_config"
    custom_db = tmp_path / "custom.db"
    custom_audit = tmp_path / "custom_audit"
    custom_downloads = tmp_path / "custom_downloads"
    custom_prompts = tmp_path / "custom_prompts"
    custom_personas = tmp_path / "custom_personas"
    custom_notes = tmp_path / "custom_notes"
    custom_histories = tmp_path / "custom_histories"
    custom_exclusions = tmp_path / "custom_exclusions.json"

    custom_env = {
        "IZVOR_DATA_DIR": str(custom_data),
        "IZVOR_CONFIG_DIR": str(custom_config),
        "IZVOR_DB_PATH": str(custom_db),
        "IZVOR_AUDIT_DIR": str(custom_audit),
        "IZVOR_DOWNLOADS_DIR": str(custom_downloads),
        "IZVOR_PROMPTS_DIR": str(custom_prompts),
        "IZVOR_PERSONAS_DIR": str(custom_personas),
        "IZVOR_THREAD_NOTES_DIR": str(custom_notes),
        "IZVOR_HISTORIES_DIR": str(custom_histories),
        "IZVOR_PRESIDIO_SKILL_EXCLUSIONS_FILE": str(custom_exclusions),
    }

    assert paths._resolve_env_path("IZVOR_DATA_DIR", paths.DATA_DIR, env=custom_env) == custom_data
    assert paths._resolve_env_path("IZVOR_CONFIG_DIR", paths.CONFIG_DIR, env=custom_env) == custom_config
    assert paths._resolve_env_path("IZVOR_DB_PATH", paths.DB_PATH, env=custom_env) == custom_db
    assert paths._resolve_env_path("IZVOR_AUDIT_DIR", paths.AUDIT_DIR, env=custom_env) == custom_audit
    assert paths._resolve_env_path("IZVOR_DOWNLOADS_DIR", paths.DOWNLOADS_DIR, env=custom_env) == custom_downloads
    assert paths._resolve_env_path("IZVOR_PROMPTS_DIR", paths.PROMPTS_DIR, env=custom_env) == custom_prompts
    assert paths._resolve_env_path("IZVOR_PERSONAS_DIR", paths.PERSONAS_DIR, env=custom_env) == custom_personas
    assert paths._resolve_env_path("IZVOR_THREAD_NOTES_DIR", paths.THREAD_NOTES_DIR, env=custom_env) == custom_notes
    assert paths._resolve_env_path("IZVOR_HISTORIES_DIR", paths.FLAT_HISTORIES_DIR, env=custom_env) == custom_histories
    assert paths._resolve_env_path("IZVOR_PRESIDIO_SKILL_EXCLUSIONS_FILE", paths.PRESIDIO_SKILL_EXCLUSIONS_FILE, env=custom_env) == custom_exclusions


def test_circular_import_resilience():
    """Verify clean import of all modules across layers without deadlock or failure."""
    module_names = (
        "core",
        "core.config",
        "core.database",
        "orchestration.integrity_checks",
        "core.model_catalog",
        "config_store.agent_settings",
        "config_store.app_settings",
        "config_store.system_settings",
        "services.thread_skills_store",
        "config_store.workflow_manager",
        "services.audit_logger",
        "services.flat_history_recorder",
        "services.pricing_service",
        "services.thread_notes_writer",
        "prompts.builder",
        "api",
        "api.dependencies",
        "api.router",
        "api.schemas",
    )
    imported = {name: importlib.import_module(name) for name in module_names}
    core_paths = importlib.import_module("core.paths")

    # Project root consistency
    assert core_paths.PROJECT_ROOT == imported["core.config"].PROJECT_ROOT
    assert core_paths.DB_PATH.name == "izvor.db"
