"""
Tests for SystemSettingsManager and data/settings/system_settings.json.
Only covers system-level settings changeable only in file (not through UI):
- max_file_size_mb
- rag configuration
"""

import json
import os
import tempfile
from pathlib import Path

from config_store.system_settings import (
    SystemSettingsManager,
    get_default_fast_model,
    get_default_reasoning_model,
    get_max_file_size_bytes,
    get_max_file_size_mb,
    get_rag_config,
    get_rag_setting,
    get_workspace_write_blacklist_additional,
    is_path_write_blocked,
    set_default_fast_model,
    set_default_reasoning_model,
    set_max_file_size_mb,
    set_rag_setting,
    set_workspace_write_blacklist_additional,
)


def test_system_settings_defaults():
    import pytest

    with tempfile.TemporaryDirectory() as tmp_dir:
        settings_file = Path(tmp_dir) / "system_settings.json"
        backup_file = Path(tmp_dir) / "system_settings_backup.json"
        mgr = SystemSettingsManager(settings_file=settings_file, backup_file=backup_file, auto_load=True)

        assert mgr.max_file_size_mb == 50.0
        assert mgr.get_max_file_size_bytes() == 50 * 1024 * 1024
        assert mgr.default_fast_model == ""
        assert mgr.default_reasoning_model == ""
        with pytest.raises(RuntimeError, match="default_fast_model"):
            mgr.get_default_fast_model()
        with pytest.raises(RuntimeError, match="default_reasoning_model"):
            mgr.get_default_reasoning_model()

        rag = mgr.get_rag_config()
        assert rag["max_chunk_chars"] == 3500
        assert rag["chunk_overlap_chars"] == 300
        assert rag["min_chunk_chars"] == 80
        assert rag["default_top_k"] == 6
        assert rag["embedding_model"] == "gemini/gemini-embedding-001"
        assert rag["embedding_dim"] == 3072
        assert rag["embedding_batch_size"] == 32
        assert mgr.get_workspace_write_blacklist_additional() == []
        assert mgr.debate_turn_pacing_seconds == 3.0
        assert mgr.slack_post_pause_seconds == 2.0
        assert mgr.tool_call_loop_timeout_seconds == 60.0
        assert mgr.log_level == "INFO"
        assert mgr.get_tool_call_loop_timeout_seconds() == 60.0
        assert mgr.get_log_level() == "INFO"
        # Getters return configured values when not manually overridden or env var set
        assert mgr.get_debate_turn_pacing_seconds() == 3.0
        assert mgr.get_slack_post_pause_seconds() == 2.0
        assert mgr.shared_evidence_max_chars == 40000
        assert mgr.get_shared_evidence_max_chars() == 40000


def test_system_settings_custom_persistence():
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings_file = Path(tmp_dir) / "system_settings.json"
        mgr = SystemSettingsManager(settings_file=settings_file, auto_load=True)

        mgr.set_max_file_size_mb(25.0)
        mgr.set_rag_setting("max_chunk_chars", 4200)

        # Reload from disk
        mgr2 = SystemSettingsManager(settings_file=settings_file, auto_load=True)
        assert mgr2.get_max_file_size_mb() == 25.0
        assert mgr2.get_max_file_size_bytes() == 25 * 1024 * 1024
        assert mgr2.get_rag_setting("max_chunk_chars") == 4200


def test_system_settings_default_models_persistence():
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings_file = Path(tmp_dir) / "system_settings.json"
        mgr = SystemSettingsManager(settings_file=settings_file, auto_load=True)

        mgr.set_default_fast_model("openai/gpt-4o-mini")
        mgr.set_default_reasoning_model("anthropic/claude-3-5-sonnet")

        assert mgr.get_default_fast_model() == "openai/gpt-4o-mini"
        assert mgr.get_default_reasoning_model() == "anthropic/claude-3-5-sonnet"

        # Reload from disk
        mgr2 = SystemSettingsManager(settings_file=settings_file, auto_load=True)
        assert mgr2.get_default_fast_model() == "openai/gpt-4o-mini"
        assert mgr2.get_default_reasoning_model() == "anthropic/claude-3-5-sonnet"
        assert mgr2.default_fast_model == "openai/gpt-4o-mini"
        assert mgr2.default_reasoning_model == "anthropic/claude-3-5-sonnet"


def test_system_settings_rag_section_persistence():
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings_file = Path(tmp_dir) / "system_settings.json"
        mgr = SystemSettingsManager(settings_file=settings_file, auto_load=True)

        rag_defaults = mgr.get_rag_config()
        assert rag_defaults["max_chunk_chars"] == 3500
        assert rag_defaults["chunk_overlap_chars"] == 300
        assert rag_defaults["default_top_k"] == 6

        # Set custom values
        mgr.set_rag_setting("max_chunk_chars", 4000)
        mgr.set_rag_setting("chunk_overlap_chars", 400)
        mgr.set_rag_setting("default_top_k", 8)

        # Reload from disk
        mgr2 = SystemSettingsManager(settings_file=settings_file, auto_load=True)
        assert mgr2.get_rag_setting("max_chunk_chars") == 4000
        assert mgr2.get_rag_setting("chunk_overlap_chars") == 400
        assert mgr2.get_rag_setting("default_top_k") == 8


def test_system_settings_backup_resilience():
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings_file = Path(tmp_dir) / "system_settings.json"
        backup_file = Path(tmp_dir) / "system_settings_backup.json"

        # Write valid backup
        with open(backup_file, "w", encoding="utf-8") as f:
            json.dump({
                "max_file_size_mb": 30.0,
                "rag": {
                    "max_chunk_chars": 2800,
                    "chunk_overlap_chars": 250,
                    "default_top_k": 5,
                },
            }, f)

        # Corrupt main file
        with open(settings_file, "w", encoding="utf-8") as f:
            f.write("corrupted json content")

        mgr = SystemSettingsManager(settings_file=settings_file, backup_file=backup_file, auto_load=True)
        assert mgr.max_file_size_mb == 30.0
        assert mgr.get_max_file_size_bytes() == 30 * 1024 * 1024
        assert mgr.get_rag_setting("max_chunk_chars") == 2800
        assert mgr.get_rag_setting("chunk_overlap_chars") == 250
        assert mgr.get_rag_setting("default_top_k") == 5


def test_system_settings_module_convenience(tmp_path):
    import pytest

    settings_file = tmp_path / "system_settings.json"
    backup_file = tmp_path / "system_settings_backup.json"
    from config_store.system_settings import system_settings_manager

    system_settings_manager.configure_paths(settings_file=settings_file, backup_file=backup_file)

    assert get_max_file_size_mb() == 50.0
    set_max_file_size_mb(15.0)
    assert get_max_file_size_mb() == 15.0
    assert get_max_file_size_bytes() == 15 * 1024 * 1024

    assert get_rag_setting("default_top_k") == 6
    set_rag_setting("default_top_k", 10)
    assert get_rag_setting("default_top_k") == 10
    assert get_rag_config()["default_top_k"] == 10

    set_workspace_write_blacklist_additional(["secrets/*", "custom.key"])
    assert get_workspace_write_blacklist_additional() == ["secrets/*", "custom.key"]

    with pytest.raises(RuntimeError, match="default_fast_model"):
        get_default_fast_model()
    set_default_fast_model("test/fast-model")
    assert get_default_fast_model() == "test/fast-model"

    with pytest.raises(RuntimeError, match="default_reasoning_model"):
        get_default_reasoning_model()
    set_default_reasoning_model("test/reasoning-model")
    assert get_default_reasoning_model() == "test/reasoning-model"


def test_workspace_write_blacklist_additional_persistence(tmp_path):
    settings_file = tmp_path / "system_settings.json"
    backup_file = tmp_path / "system_settings_backup.json"
    mgr = SystemSettingsManager(settings_file=settings_file, backup_file=backup_file, auto_load=True)

    assert mgr.get_workspace_write_blacklist_additional() == []
    mgr.set_workspace_write_blacklist_additional(["restricted_dir/*", "secret.token"])

    # Reload from disk
    mgr2 = SystemSettingsManager(settings_file=settings_file, backup_file=backup_file, auto_load=True)
    assert mgr2.get_workspace_write_blacklist_additional() == ["restricted_dir/*", "secret.token"]

    # Verify JSON content contains comment and blacklist at root
    with open(settings_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert "_comment_workspace_write_blacklist_additional" in data
    assert data["workspace_write_blacklist_additional"] == ["restricted_dir/*", "secret.token"]


def test_is_path_write_blocked_defaults(tmp_path):
    # Tests that .env, .git, agent_settings.json, app_settings.json, and system_settings.json are blocked by default
    base_dir = tmp_path

    # .env variations
    assert is_path_write_blocked(".env", base_dir=base_dir) is True
    assert is_path_write_blocked(".env.local", base_dir=base_dir) is True
    assert is_path_write_blocked("subfolder/../../.env", base_dir=base_dir) is True

    # .git variations
    assert is_path_write_blocked(".git", base_dir=base_dir) is True
    assert is_path_write_blocked(".git/config", base_dir=base_dir) is True
    assert is_path_write_blocked(".git/hooks/pre-commit", base_dir=base_dir) is True

    # Config files protection
    assert is_path_write_blocked("config/settings/agent_settings.json", base_dir=base_dir) is True
    assert is_path_write_blocked("agent_settings.json", base_dir=base_dir) is True
    assert is_path_write_blocked("engine/settings/system_agent_settings.json", base_dir=base_dir) is True
    assert is_path_write_blocked("system_agent_settings.json", base_dir=base_dir) is True
    assert is_path_write_blocked("data/state/app_settings.json", base_dir=base_dir) is True
    assert is_path_write_blocked("app_settings.json", base_dir=base_dir) is True
    assert is_path_write_blocked("config/settings/system_settings.json", base_dir=base_dir) is True
    assert is_path_write_blocked("system_settings.json", base_dir=base_dir) is True

    # Python source files must be blocked
    assert is_path_write_blocked("src/app.py", base_dir=base_dir) is True

    # Normal files should not be blocked
    assert is_path_write_blocked("normal_file.txt", base_dir=base_dir) is False


def test_is_path_write_blocked_custom_blacklist(tmp_path):
    base_dir = tmp_path
    custom_bl = ["confidential/*", "*.pem", "custom_locked.md"]

    assert is_path_write_blocked("confidential/secrets.txt", base_dir=base_dir, custom_blacklist=custom_bl) is True
    assert is_path_write_blocked("server.pem", base_dir=base_dir, custom_blacklist=custom_bl) is True
    assert is_path_write_blocked("custom_locked.md", base_dir=base_dir, custom_blacklist=custom_bl) is True
    assert is_path_write_blocked("public/readme.md", base_dir=base_dir, custom_blacklist=custom_bl) is False


def test_timing_and_guardrail_settings(tmp_path):
    settings_file = tmp_path / "system_settings.json"
    mgr = SystemSettingsManager(settings_file=settings_file, auto_load=True)

    mgr.set_debate_turn_pacing_seconds(5.5)
    mgr.set_slack_post_pause_seconds(4.2)
    mgr.set_tool_call_loop_timeout_seconds(45.0)
    mgr.set_log_level("DEBUG")

    assert mgr.get_debate_turn_pacing_seconds() == 5.5
    assert mgr.get_slack_post_pause_seconds() == 4.2
    assert mgr.get_tool_call_loop_timeout_seconds() == 45.0
    assert mgr.get_log_level() == "DEBUG"

    # Reload from disk
    mgr2 = SystemSettingsManager(settings_file=settings_file, auto_load=True)
    assert mgr2.debate_turn_pacing_seconds == 5.5
    assert mgr2.slack_post_pause_seconds == 4.2
    assert mgr2.tool_call_loop_timeout_seconds == 45.0
    assert mgr2.log_level == "DEBUG"


def test_shared_evidence_max_chars_persistence_and_env(tmp_path):
    settings_file = tmp_path / "system_settings.json"
    mgr = SystemSettingsManager(settings_file=settings_file, auto_load=True)

    assert mgr.get_shared_evidence_max_chars() == 40000
    mgr.set_shared_evidence_max_chars(25000)
    assert mgr.get_shared_evidence_max_chars() == 25000

    # Reload from disk
    mgr2 = SystemSettingsManager(settings_file=settings_file, auto_load=True)
    assert mgr2.shared_evidence_max_chars == 25000
    assert mgr2.get_shared_evidence_max_chars() == 25000

    # Verify JSON content on disk
    with open(settings_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["shared_evidence_max_chars"] == 25000

    # Test environment variable override when not manually set
    old_env = os.environ.get("IZVOR_SHARED_EVIDENCE_MAX_CHARS")
    try:
        os.environ["IZVOR_SHARED_EVIDENCE_MAX_CHARS"] = "32000"
        mgr3 = SystemSettingsManager(settings_file=settings_file, auto_load=True)
        assert mgr3.get_shared_evidence_max_chars() == 32000
    finally:
        if old_env is None:
            os.environ.pop("IZVOR_SHARED_EVIDENCE_MAX_CHARS", None)
        else:
            os.environ["IZVOR_SHARED_EVIDENCE_MAX_CHARS"] = old_env


def test_output_constraints_defaults_clamping_and_persistence(tmp_path):
    from config_store.system_settings import (
        get_output_constraints,
        get_output_max_words,
        set_output_constraints,
    )

    settings_file = tmp_path / "system_settings.json"
    mgr = SystemSettingsManager(settings_file=settings_file, auto_load=True)

    assert mgr.get_output_constraints() == {"default_max_words": 200, "short_max_words": 100}
    assert mgr.get_output_max_words(is_short=False) == 200
    assert mgr.get_output_max_words(is_short=True) == 100

    # Clamping to INT_MODIFIER_BOUNDS["max_words"] (1..2000)
    mgr.set_output_constraints(default_max_words=5000, short_max_words=0)
    assert mgr.get_output_constraints() == {"default_max_words": 2000, "short_max_words": 1}

    # Reload from disk
    mgr2 = SystemSettingsManager(settings_file=settings_file, auto_load=True)
    assert mgr2.get_output_constraints() == {"default_max_words": 2000, "short_max_words": 1}

    # Invalid/bool values on disk fall back to defaults (200 / 100), negative values clamp to 1
    settings_file.write_text(
        json.dumps({"output_constraints": {"default_max_words": True, "short_max_words": -15}}),
        encoding="utf-8",
    )
    mgr2.load(force=True)
    assert mgr2.get_output_constraints() == {"default_max_words": 200, "short_max_words": 1}

    # Module-level helpers
    set_output_constraints(default_max_words=280, short_max_words=85)
    assert get_output_constraints() == {"default_max_words": 280, "short_max_words": 85}
    assert get_output_max_words(is_short=False) == 280
    assert get_output_max_words(is_short=True) == 85


def test_system_settings_init_registers_providers_and_persists_skill_sync(tmp_path):
    """TKT-1220: __init__ registers core providers, persists skill_exclusions_sync_interval_seconds, and AgentSettingsManager.reset_paths restores SYSTEM_AGENT_SETTINGS_FILE."""
    from core import database, llm_utils, slack_security
    from core.paths import SYSTEM_AGENT_SETTINGS_FILE
    from config_store.agent_settings import AgentSettingsManager
    from config_store.system_settings import system_settings_manager

    sys_file = tmp_path / "sys_settings.json"
    sys_backup = tmp_path / "sys_backup.json"
    try:
        llm_utils.set_llm_retry_config_provider(None)
        llm_utils.set_privacy_mode_provider(None)
        database.set_db_busy_timeout_provider(None)
        slack_security.set_burst_limits_provider(None)

        mgr = SystemSettingsManager(
            settings_file=sys_file,
            backup_file=sys_backup,
            auto_load=True,
            register_core_providers=True,
        )
        assert llm_utils._llm_retry_config_provider is not None
        assert llm_utils._privacy_mode_provider is not None
        assert database._db_busy_timeout_provider is not None
        assert slack_security._burst_limits_provider is not None

        # TKT-1307: Standalone SystemSettingsManager without register_core_providers=True
        # must NOT overwrite global providers registered by singleton or explicit owner
        from core import config as core_config

        core_config.set_internal_revisor_enabled_provider(lambda: True)
        stray_file = tmp_path / "stray_system_settings.json"
        stray_file.write_text(json.dumps({"internal_revisor_enabled": False}), encoding="utf-8")
        _stray_mgr = SystemSettingsManager(settings_file=stray_file, auto_load=True)
        assert core_config.is_internal_revisor_enabled() is True
        mgr.set_skill_exclusions_sync_interval_seconds(1200.0, save=True)
        mgr2 = SystemSettingsManager(settings_file=sys_file, backup_file=sys_backup, auto_load=True)
        assert mgr2.get_skill_exclusions_sync_interval_seconds() == 1200.0
    finally:
        system_settings_manager._register_core_providers()

    agent_mgr = AgentSettingsManager(auto_load=False)
    agent_mgr.configure_paths(system_agent_settings_file=tmp_path / "custom_sys_agents.json")
    agent_mgr.reset_paths()
    assert agent_mgr.default_system_agent_settings_file == SYSTEM_AGENT_SETTINGS_FILE


def test_system_settings_snapshot_and_p2_fields_persistence_and_clamping(tmp_path):
    """TKT-1225 / TKT-1226 / TKT-1231 / TKT-1232: snapshot immutability and P2 settings schema."""
    sys_file = tmp_path / "system_settings_p2.json"
    sys_backup = tmp_path / "system_backup_p2.json"
    mgr = SystemSettingsManager(settings_file=sys_file, backup_file=sys_backup, auto_load=True)

    # Defaults
    assert mgr.get_guardrail_chunk_chars() == 800
    assert mgr.get_guardrail_chunk_overlap_chars() == 200
    assert mgr.get_guardrail_max_chunks() == 8
    assert mgr.get_guardrail_indirect_embedding_enabled() is False
    assert mgr.get_pii_ner_models() == ["en_core_web_md"]
    assert mgr.get_audit_hmac_key_id() == "default"

    # Mutate + persist
    mgr.set_guardrail_chunk_chars(1000, save=False)
    mgr.set_guardrail_chunk_overlap_chars(150, save=False)
    mgr.set_guardrail_max_chunks(12, save=False)
    mgr.set_guardrail_indirect_embedding_enabled(True, save=False)
    mgr.set_pii_ner_models(["en_core_web_md", "fr_core_news_sm"], save=False)
    mgr.set_audit_hmac_key_id("key-2026-q4", save=True)

    saved_raw = json.loads(sys_file.read_text(encoding="utf-8"))
    assert "_comment_pii_ner_models" in saved_raw
    assert "pyproject.toml" in saved_raw["_comment_pii_ner_models"]

    # Reload
    mgr2 = SystemSettingsManager(settings_file=sys_file, backup_file=sys_backup, auto_load=True)
    assert mgr2.get_guardrail_chunk_chars() == 1000
    assert mgr2.get_guardrail_chunk_overlap_chars() == 150
    assert mgr2.get_guardrail_max_chunks() == 12
    assert mgr2.get_guardrail_indirect_embedding_enabled() is True
    assert mgr2.get_pii_ner_models() == ["en_core_web_md", "fr_core_news_sm"]
    assert mgr2.get_audit_hmac_key_id() == "key-2026-q4"

    # Snapshot isolation (deep copy)
    snap = mgr2.get_snapshot()
    snap.pii_ner_models.append("tampered_model")
    snap.audit_hmac_key_id = "tampered-key"
    fresh_snap = mgr2.get_snapshot()
    assert fresh_snap.pii_ner_models == ["en_core_web_md", "fr_core_news_sm"]
    assert fresh_snap.audit_hmac_key_id == "key-2026-q4"


def test_system_settings_p3_retention_defaults_persistence_and_clamping(tmp_path):
    """TKT-1243: centralized retention settings defaults, clamping, and round-trip persistence."""
    sys_file = tmp_path / "system_settings_p3.json"
    sys_backup = tmp_path / "system_backup_p3.json"
    mgr = SystemSettingsManager(settings_file=sys_file, backup_file=sys_backup, auto_load=True)

    assert mgr.get_retention_checkpoints_days() == 7
    assert mgr.get_retention_traces_days() == 30
    assert mgr.get_retention_event_dedup_days() == 1
    assert mgr.get_retention_expired_grace_days() == 7
    assert mgr.get_retention_thread_sessions_days() == 90
    assert mgr.get_retention_spend_ledger_days() == 0
    assert mgr.get_retention_downloads_hours() == 24
    assert mgr.get_retention_flat_histories_days() == 0
    assert mgr.get_retention_audit_anchors_compact_enabled() is True
    assert mgr.get_retention_prune_interval_seconds() == 86400.0

    mgr.set_retention_expired_grace_days(0, save=False)
    mgr.set_retention_thread_sessions_days(-5, save=False)
    mgr.set_retention_spend_ledger_days(180, save=False)
    mgr.set_retention_downloads_hours(12, save=False)
    mgr.set_retention_flat_histories_days(45, save=False)
    mgr.set_retention_audit_anchors_compact_enabled(False, save=False)
    mgr.set_retention_prune_interval_seconds(10.0, save=True)

    mgr2 = SystemSettingsManager(settings_file=sys_file, backup_file=sys_backup, auto_load=True)
    assert mgr2.get_retention_expired_grace_days() == 1
    assert mgr2.get_retention_thread_sessions_days() == 0
    assert mgr2.get_retention_spend_ledger_days() == 180
    assert mgr2.get_retention_downloads_hours() == 12
    assert mgr2.get_retention_flat_histories_days() == 45
    assert mgr2.get_retention_audit_anchors_compact_enabled() is False
    assert mgr2.get_retention_prune_interval_seconds() == 60.0


def test_validate_required_startup_settings_fails_when_file_missing(tmp_path):
    """Startup validation fails fast with FileNotFoundError when both primary and backup are missing."""
    import pytest

    mgr = SystemSettingsManager(
        settings_file=tmp_path / "missing.json",
        backup_file=tmp_path / "missing_backup.json",
        auto_load=True,
    )
    with pytest.raises(FileNotFoundError, match="Required system settings file not found"):
        mgr.validate_required_startup_settings()


def test_validate_required_startup_settings_fails_when_corrupted_without_backup(tmp_path):
    """Startup validation fails fast with RuntimeError when JSON is corrupted and no backup exists."""
    import pytest

    settings_file = tmp_path / "system_settings.json"
    backup_file = tmp_path / "system_settings_backup.json"
    settings_file.write_text("{not valid json", encoding="utf-8")

    mgr = SystemSettingsManager(
        settings_file=settings_file,
        backup_file=backup_file,
        auto_load=True,
    )
    with pytest.raises(RuntimeError, match="Failed to load valid system settings"):
        mgr.validate_required_startup_settings()


def test_validate_required_startup_settings_recovers_from_backup_when_primary_missing(tmp_path):
    """When primary system_settings.json is missing, load() and startup validation recover from backup."""
    settings_file = tmp_path / "system_settings.json"
    backup_file = tmp_path / "system_settings_backup.json"
    backup_file.write_text(
        json.dumps({
            "default_fast_model": "provider/fast-backup",
            "default_reasoning_model": "provider/reason-backup",
        }),
        encoding="utf-8",
    )

    mgr = SystemSettingsManager(
        settings_file=settings_file,
        backup_file=backup_file,
        auto_load=True,
    )
    mgr.validate_required_startup_settings()
    assert mgr.get_default_fast_model() == "provider/fast-backup"
    assert mgr.get_default_reasoning_model() == "provider/reason-backup"


def test_validate_required_startup_settings_fails_when_models_missing_or_blank(tmp_path):
    """Startup validation fails fast with ValueError when required default models are missing or blank."""
    import pytest

    settings_file = tmp_path / "system_settings.json"
    backup_file = tmp_path / "system_settings_backup.json"
    settings_file.write_text(json.dumps({"log_level": "INFO"}), encoding="utf-8")

    mgr = SystemSettingsManager(
        settings_file=settings_file,
        backup_file=backup_file,
        auto_load=True,
    )
    with pytest.raises(ValueError, match="default_fast_model"):
        mgr.validate_required_startup_settings()

    settings_file.write_text(
        json.dumps({"default_fast_model": "provider/fast", "default_reasoning_model": "   "}),
        encoding="utf-8",
    )
    mgr.load(force=True)
    with pytest.raises(ValueError, match="default_reasoning_model"):
        mgr.validate_required_startup_settings()


def test_core_config_default_models_fail_fast_without_hardcoded_fallback(tmp_path):
    """core.config default model getters and SSOT loader fail fast when unconfigured (no hardcoded fallback)."""
    import pytest
    from config_store.system_settings import system_settings_manager
    from core.config import (
        _load_default_models_from_ssot,
        get_default_fast_model,
        get_default_reasoning_model,
        set_default_model_providers,
    )

    missing_primary = tmp_path / "missing.json"
    missing_backup = tmp_path / "missing_backup.json"
    with pytest.raises(FileNotFoundError, match="Required system settings file not found"):
        _load_default_models_from_ssot(settings_file=missing_primary, backup_file=missing_backup)

    corrupt_primary = tmp_path / "corrupt.json"
    corrupt_primary.write_text("{bad json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Failed to load default models"):
        _load_default_models_from_ssot(settings_file=corrupt_primary, backup_file=missing_backup)

    backup_only = tmp_path / "backup.json"
    backup_only.write_text(
        json.dumps({"default_fast_model": "p/fast", "default_reasoning_model": "p/reason"}),
        encoding="utf-8",
    )
    assert _load_default_models_from_ssot(settings_file=missing_primary, backup_file=backup_only) == (
        "p/fast",
        "p/reason",
    )

    empty_mgr = SystemSettingsManager(
        settings_file=missing_primary,
        backup_file=missing_backup,
        auto_load=True,
    )
    try:
        set_default_model_providers(
            fast_provider=lambda: empty_mgr.get_default_fast_model(),
            reasoning_provider=lambda: empty_mgr.get_default_reasoning_model(),
        )
        with pytest.raises(RuntimeError, match="default_fast_model"):
            get_default_fast_model()
        with pytest.raises(RuntimeError, match="default_reasoning_model"):
            get_default_reasoning_model()
    finally:
        system_settings_manager._register_core_providers()




