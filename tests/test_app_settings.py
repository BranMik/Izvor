"""
Tests for AppSettingsManager and data/app_settings.json.
Only covers application settings changeable via the Slack UI.
"""

import json
import tempfile
from pathlib import Path

from orchestration.app_settings import (
    AppSettingsManager,
    get_thread_skill_expiration_time,
    get_workspace_write_blacklist,
    is_circuit_breaker_enabled,
    is_path_write_blocked,
    is_scribe_enabled,
    is_weekly_pricing_update_enabled,
    set_circuit_breaker_enabled,
    set_circuit_breaker_threshold,
    set_scribe_enabled,
    set_thread_skill_expiration_time,
    set_weekly_pricing_update_enabled,
    set_workspace_write_blacklist,
)


def test_app_settings_defaults():
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings_file = Path(tmp_dir) / "app_settings.json"
        mgr = AppSettingsManager(settings_file=settings_file, auto_load=True)

        assert mgr.thread_skill_expiration_time == 12.0
        assert mgr.circuit_breaker_enabled is False
        assert mgr.circuit_breaker_threshold_usd == 0.0
        assert mgr.scribe_enabled is True
        assert mgr.weekly_pricing_update_enabled is True
        assert mgr.is_weekly_pricing_update_enabled() is True


def test_app_settings_custom_persistence():
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings_file = Path(tmp_dir) / "app_settings.json"
        mgr = AppSettingsManager(settings_file=settings_file, auto_load=True)

        mgr.set_thread_skill_expiration_time(6.0)
        mgr.set_circuit_breaker_enabled(True)
        mgr.set_circuit_breaker_threshold(2.5)
        mgr.set_scribe_enabled(False)
        mgr.set_weekly_pricing_update_enabled(False)

        # Reload from disk
        mgr2 = AppSettingsManager(settings_file=settings_file, auto_load=True)
        assert mgr2.get_thread_skill_expiration_time() == 6.0
        assert mgr2.is_circuit_breaker_enabled() is True
        assert mgr2.get_circuit_breaker_threshold() == 2.5
        assert mgr2.is_scribe_enabled() is False
        assert mgr2.is_weekly_pricing_update_enabled() is False


def test_app_settings_circuit_breaker_independent():
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings_file = Path(tmp_dir) / "app_settings.json"
        mgr = AppSettingsManager(settings_file=settings_file, auto_load=True)

        mgr.set_circuit_breaker_enabled(False)
        assert mgr.is_circuit_breaker_enabled() is False

        mgr.set_circuit_breaker_threshold(3.0)
        assert mgr.get_circuit_breaker_threshold() == 3.0
        assert mgr.is_circuit_breaker_enabled() is False


def test_app_settings_backup_resilience():
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings_file = Path(tmp_dir) / "app_settings.json"
        backup_file = Path(tmp_dir) / "app_settings_backup.json"

        # Write valid backup
        with open(backup_file, "w", encoding="utf-8") as f:
            json.dump({
                "thread_skill_expiration_time": 4.0,
                "circuit_breaker_enabled": True,
                "circuit_breaker_threshold_usd": 2.0,
                "scribe_enabled": True,
                "weekly_pricing_update_enabled": False,
            }, f)

        # Corrupt main file
        with open(settings_file, "w", encoding="utf-8") as f:
            f.write("corrupted json content")

        mgr = AppSettingsManager(settings_file=settings_file, backup_file=backup_file, auto_load=True)
        assert mgr.thread_skill_expiration_time == 4.0
        assert mgr.circuit_breaker_threshold_usd == 2.0
        assert mgr.is_weekly_pricing_update_enabled() is False


def test_app_settings_module_convenience(monkeypatch, tmp_path):
    settings_file = tmp_path / "app_settings.json"
    backup_file = tmp_path / "app_settings_backup.json"
    mgr = AppSettingsManager(settings_file=settings_file, backup_file=backup_file, auto_load=False)
    monkeypatch.setattr("orchestration.app_settings.app_settings_manager", mgr)

    assert is_weekly_pricing_update_enabled() is True
    set_weekly_pricing_update_enabled(False)
    assert is_weekly_pricing_update_enabled() is False
    set_weekly_pricing_update_enabled(True)
    assert is_weekly_pricing_update_enabled() is True

    set_thread_skill_expiration_time(8.0)
    assert get_thread_skill_expiration_time() == 8.0

    set_circuit_breaker_enabled(True)
    assert is_circuit_breaker_enabled() is True

    set_circuit_breaker_threshold(1.25)
    assert mgr.get_circuit_breaker_threshold() == 1.25

    set_scribe_enabled(False)
    assert is_scribe_enabled() is False

    set_workspace_write_blacklist(["secrets/*", "custom.key"])
    assert get_workspace_write_blacklist() == ["secrets/*", "custom.key"]


def test_workspace_write_blacklist_persistence(tmp_path):
    settings_file = tmp_path / "app_settings.json"
    backup_file = tmp_path / "app_settings_backup.json"
    mgr = AppSettingsManager(settings_file=settings_file, backup_file=backup_file, auto_load=True)

    assert mgr.get_workspace_write_blacklist() == []
    mgr.set_workspace_write_blacklist(["restricted_dir/*", "secret.token"])

    # Reload from disk
    mgr2 = AppSettingsManager(settings_file=settings_file, backup_file=backup_file, auto_load=True)
    assert mgr2.get_workspace_write_blacklist() == ["restricted_dir/*", "secret.token"]

    # Verify JSON content contains comment and blacklist
    with open(settings_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert "_comment_workspace_write_blacklist" in data
    assert data["workspace_write_blacklist"] == ["restricted_dir/*", "secret.token"]


def test_is_path_write_blocked_defaults(tmp_path):
    # Tests that .env, .git, agent_settings.json, and app_settings.json are blocked by default
    base_dir = tmp_path

    # .env variations
    assert is_path_write_blocked(".env", base_dir=base_dir) is True
    assert is_path_write_blocked(".env.local", base_dir=base_dir) is True
    assert is_path_write_blocked("subfolder/../../.env", base_dir=base_dir) is True

    # .git variations
    assert is_path_write_blocked(".git", base_dir=base_dir) is True
    assert is_path_write_blocked(".git/config", base_dir=base_dir) is True
    assert is_path_write_blocked(".git/hooks/pre-commit", base_dir=base_dir) is True

    # agent_settings.json & app_settings.json
    assert is_path_write_blocked("data/agent_settings.json", base_dir=base_dir) is True
    assert is_path_write_blocked("agent_settings.json", base_dir=base_dir) is True
    assert is_path_write_blocked("data/app_settings.json", base_dir=base_dir) is True
    assert is_path_write_blocked("app_settings.json", base_dir=base_dir) is True

    # Normal files should not be blocked
    assert is_path_write_blocked("normal_file.txt", base_dir=base_dir) is False
    assert is_path_write_blocked("src/app.py", base_dir=base_dir) is False


def test_is_path_write_blocked_custom_blacklist(tmp_path):
    base_dir = tmp_path
    custom_bl = ["confidential/*", "*.pem", "custom_locked.md"]

    assert is_path_write_blocked("confidential/secrets.txt", base_dir=base_dir, custom_blacklist=custom_bl) is True
    assert is_path_write_blocked("server.pem", base_dir=base_dir, custom_blacklist=custom_bl) is True
    assert is_path_write_blocked("custom_locked.md", base_dir=base_dir, custom_blacklist=custom_bl) is True
    assert is_path_write_blocked("public/readme.md", base_dir=base_dir, custom_blacklist=custom_bl) is False

