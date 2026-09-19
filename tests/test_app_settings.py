"""
Tests for AppSettingsManager and data/app_settings.json.
"""

import json
import tempfile
from pathlib import Path

from orchestration.app_settings import (
    AppSettingsManager,
    app_settings_manager,
    get_max_file_size_bytes,
    get_max_file_size_mb,
    get_thread_skill_expiration_time,
    is_circuit_breaker_enabled,
    is_scribe_enabled,
    is_weekly_pricing_update_enabled,
    set_circuit_breaker_enabled,
    set_circuit_breaker_threshold,
    set_max_file_size_mb,
    set_scribe_enabled,
    set_thread_skill_expiration_time,
    set_weekly_pricing_update_enabled,
)


def test_app_settings_defaults():
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings_file = Path(tmp_dir) / "app_settings.json"
        mgr = AppSettingsManager(settings_file=settings_file, auto_load=True)

        assert mgr.thread_skill_expiration_time == 12.0
        assert mgr.circuit_breaker_enabled is False
        assert mgr.circuit_breaker_threshold_usd == 0.0
        assert mgr.scribe_enabled is True
        assert mgr.max_file_size_mb == 50.0
        assert mgr.get_max_file_size_bytes() == 50 * 1024 * 1024
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
        mgr.set_max_file_size_mb(25.0)
        mgr.set_weekly_pricing_update_enabled(False)

        # Reload from disk
        mgr2 = AppSettingsManager(settings_file=settings_file, auto_load=True)
        assert mgr2.get_thread_skill_expiration_time() == 6.0
        assert mgr2.is_circuit_breaker_enabled() is True
        assert mgr2.get_circuit_breaker_threshold() == 2.5
        assert mgr2.is_scribe_enabled() is False
        assert mgr2.get_max_file_size_mb() == 25.0
        assert mgr2.get_max_file_size_bytes() == 25 * 1024 * 1024
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
                "max_file_size_mb": 30.0,
                "weekly_pricing_update_enabled": False,
            }, f)

        # Corrupt main file
        with open(settings_file, "w", encoding="utf-8") as f:
            f.write("corrupted json content")

        mgr = AppSettingsManager(settings_file=settings_file, backup_file=backup_file, auto_load=True)
        assert mgr.thread_skill_expiration_time == 4.0
        assert mgr.circuit_breaker_threshold_usd == 2.0
        assert mgr.max_file_size_mb == 30.0
        assert mgr.get_max_file_size_bytes() == 30 * 1024 * 1024
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

