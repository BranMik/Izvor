"""
tests/test_paths.py — Unit Tests for Centralized Paths Architecture.

Verifies:
1. Default canonical paths match repository layout.
2. Environment variable overrides (IZVOR_*) properly take effect.
3. Zero internal application imports in core/paths.py (pure standard library).
4. No circular import deadlocks across core, orchestration, services, agents, prompts.
"""

import importlib


import core.paths as paths


import ast


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
    assert (root / "core").is_dir()
    assert (root / "main.py").is_file()

    # Core data directories
    assert paths.DATA_DIR == root / "data"
    assert paths.SETTINGS_DIR == root / "data" / "settings"
    assert paths.BACKUPS_DIR == root / "data" / "backups"
    assert paths.WORKFLOWS_DIR == root / "data" / "workflows"
    assert paths.PRICING_DIR == root / "data" / "pricing"
    assert paths.THREAD_DATA_DIR == root / "data" / "thread"
    assert paths.AUDIT_DIR == root / "audit"
    assert paths.DOWNLOADS_DIR == root / "downloads"

    # Canonical files
    assert paths.DB_PATH == root / "data" / "izvor.db"
    assert paths.SYSTEM_SETTINGS_FILE == root / "data" / "settings" / "system_settings.json"
    assert paths.AGENT_SETTINGS_FILE == root / "data" / "settings" / "agent_settings.json"
    assert paths.APP_SETTINGS_FILE == root / "data" / "settings" / "app_settings.json"
    assert paths.SYSTEM_BACKUP_FILE == root / "data" / "backups" / "system_settings_backup.json"
    assert paths.AGENT_BACKUP_FILE == root / "data" / "backups" / "agent_settings_backup.json"
    assert paths.APP_BACKUP_FILE == root / "data" / "backups" / "app_settings_backup.json"
    assert paths.MODEL_PROVIDER_CATALOG_FILE == root / "data" / "pricing" / "model_provider_catalog.json"
    assert paths.PRICING_CACHE_FILE == root / "data" / "pricing" / "model_pricing_cache.json"
    assert paths.BASELINE_PRICES_FILE == root / "data" / "pricing" / "baseline_prices.json"

    # Prompts & Personas
    assert paths.PROMPTS_DIR == root / "prompts"
    assert paths.PERSONAS_DIR == root / "prompts" / "personas"
    assert paths.SYSTEM_AGENTS_DIR == root / "prompts" / "system_agents"

    # Resources & Notes
    assert paths.AGENTS_RESOURCES_DIR == root / "agents_resources"
    assert paths.THREAD_NOTES_DIR == root / "agents_resources" / "thread_notes"
    assert paths.SYSTEM_RESOURCES_DIR == root / "system_resources"
    assert paths.FLAT_HISTORIES_DIR == root / "flat-histories"
    assert paths.USAGE_STORE_FILE == root / "data" / "thread" / "thread_usage_data.json"


def test_environment_variable_overrides(monkeypatch, tmp_path):
    """Verify that setting IZVOR_* environment variables overrides path resolution on reload."""
    custom_data = tmp_path / "custom_data"
    custom_db = tmp_path / "custom.db"
    custom_audit = tmp_path / "custom_audit"
    custom_downloads = tmp_path / "custom_downloads"
    custom_prompts = tmp_path / "custom_prompts"
    custom_personas = tmp_path / "custom_personas"
    custom_notes = tmp_path / "custom_notes"
    custom_histories = tmp_path / "custom_histories"

    monkeypatch.setenv("IZVOR_DATA_DIR", str(custom_data))
    monkeypatch.setenv("IZVOR_DB_PATH", str(custom_db))
    monkeypatch.setenv("IZVOR_AUDIT_DIR", str(custom_audit))
    monkeypatch.setenv("IZVOR_DOWNLOADS_DIR", str(custom_downloads))
    monkeypatch.setenv("IZVOR_PROMPTS_DIR", str(custom_prompts))
    monkeypatch.setenv("IZVOR_PERSONAS_DIR", str(custom_personas))
    monkeypatch.setenv("IZVOR_THREAD_NOTES_DIR", str(custom_notes))
    monkeypatch.setenv("IZVOR_HISTORIES_DIR", str(custom_histories))

    reloaded = importlib.reload(paths)

    try:
        assert reloaded.DATA_DIR == custom_data
        assert reloaded.DB_PATH == custom_db
        assert reloaded.AUDIT_DIR == custom_audit
        assert reloaded.DOWNLOADS_DIR == custom_downloads
        assert reloaded.PROMPTS_DIR == custom_prompts
        assert reloaded.PERSONAS_DIR == custom_personas
        assert reloaded.THREAD_NOTES_DIR == custom_notes
        assert reloaded.FLAT_HISTORIES_DIR == custom_histories
        assert reloaded.SETTINGS_DIR == custom_data / "settings"
        assert reloaded.SYSTEM_SETTINGS_FILE == custom_data / "settings" / "system_settings.json"
    finally:
        # Restore normal paths module
        monkeypatch.undo()
        importlib.reload(paths)


def test_circular_import_resilience():
    """Verify clean import of all modules across layers without deadlock or failure."""
    import core
    import core.config
    import core.database
    import orchestration.integrity_checks
    import core.model_catalog
    import orchestration.agent_settings
    import orchestration.app_settings
    import orchestration.system_settings
    import orchestration.thread_skills_manager
    import orchestration.workflow_manager
    import services.audit_logger
    import services.flat_history_recorder
    import services.pricing_service
    import services.thread_notes_writer
    import prompts.builder

    # Project root consistency
    assert core.paths.PROJECT_ROOT == core.config.PROJECT_ROOT
    assert core.paths.DB_PATH.name == "izvor.db"
    assert core.paths.AGENT_SETTINGS_FILE.name == "agent_settings.json"
    assert core.paths.SYSTEM_SETTINGS_FILE.name == "system_settings.json"
    assert core.paths.APP_SETTINGS_FILE.name == "app_settings.json"

    # Module constants point to canonical or fixture-isolated paths
    assert orchestration.system_settings.SYSTEM_SETTINGS_FILE == core.paths.SYSTEM_SETTINGS_FILE
    assert orchestration.WORKFLOWS_DIR == core.paths.WORKFLOWS_DIR
    assert services.pricing_service.BASELINE_PRICES_FILE == core.paths.BASELINE_PRICES_FILE
    assert services.thread_notes_writer.DEFAULT_THREAD_NOTES_DIR == core.paths.THREAD_NOTES_DIR
    assert services.flat_history_recorder.DEFAULT_FLAT_HISTORIES_DIR == core.paths.FLAT_HISTORIES_DIR
    assert services.audit_logger.DEFAULT_AUDIT_DIR == core.paths.AUDIT_DIR
    assert prompts.builder.PROMPTS_DIR == core.paths.PROMPTS_DIR
