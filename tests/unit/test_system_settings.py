"""
Tests for SystemSettingsManager and data/settings/system_settings.json.
Only covers system-level settings changeable only in file (not through UI):
- max_file_size_mb
- rag configuration
"""

import json
import tempfile
from pathlib import Path

from orchestration.system_settings import (
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
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings_file = Path(tmp_dir) / "system_settings.json"
        mgr = SystemSettingsManager(settings_file=settings_file, auto_load=True)

        assert mgr.max_file_size_mb == 50.0
        assert mgr.get_max_file_size_bytes() == 50 * 1024 * 1024
        assert mgr.default_fast_model == "gemini/gemini-3.8-flash"
        assert mgr.default_reasoning_model == "mistral/mistral-large-latest"
        assert mgr.get_default_fast_model() == "gemini/gemini-3.8-flash"
        assert mgr.get_default_reasoning_model() == "mistral/mistral-large-latest"

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
        # Under pytest, getters scale down pacing unless explicitly set
        assert mgr.get_debate_turn_pacing_seconds() == 0.01
        assert mgr.get_slack_post_pause_seconds() == 0.01


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
    settings_file = tmp_path / "system_settings.json"
    backup_file = tmp_path / "system_settings_backup.json"
    from orchestration.system_settings import system_settings_manager

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

    assert get_default_fast_model() == "gemini/gemini-3.8-flash"
    set_default_fast_model("test/fast-model")
    assert get_default_fast_model() == "test/fast-model"

    assert get_default_reasoning_model() == "mistral/mistral-large-latest"
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


