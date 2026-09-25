"""
Tests for SystemSettingsManager and data/system_settings.json.
Only covers system-level settings changeable only in file (not through UI):
- max_file_size_mb
- rag configuration
"""

import json
import tempfile
from pathlib import Path

from orchestration.system_settings import (
    SystemSettingsManager,
    get_max_file_size_bytes,
    get_max_file_size_mb,
    get_rag_config,
    get_rag_setting,
    set_max_file_size_mb,
    set_rag_setting,
)


def test_system_settings_defaults():
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings_file = Path(tmp_dir) / "system_settings.json"
        mgr = SystemSettingsManager(settings_file=settings_file, auto_load=True)

        assert mgr.max_file_size_mb == 50.0
        assert mgr.get_max_file_size_bytes() == 50 * 1024 * 1024

        rag = mgr.get_rag_config()
        assert rag["max_chunk_chars"] == 3500
        assert rag["chunk_overlap_chars"] == 300
        assert rag["min_chunk_chars"] == 80
        assert rag["default_top_k"] == 6
        assert rag["embedding_model"] == "gemini/gemini-embedding-001"
        assert rag["embedding_dim"] == 3072
        assert rag["embedding_batch_size"] == 32


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
        assert mgr2.get_rag_setting("SET_DEFAULT_TOP_K") == 8  # Case-insensitive alias


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


def test_system_settings_module_convenience(monkeypatch, tmp_path):
    settings_file = tmp_path / "system_settings.json"
    backup_file = tmp_path / "system_settings_backup.json"
    mgr = SystemSettingsManager(settings_file=settings_file, backup_file=backup_file, auto_load=False)
    monkeypatch.setattr("orchestration.system_settings.system_settings_manager", mgr)

    assert get_max_file_size_mb() == 50.0
    set_max_file_size_mb(15.0)
    assert get_max_file_size_mb() == 15.0
    assert get_max_file_size_bytes() == 15 * 1024 * 1024

    assert get_rag_setting("default_top_k") == 6
    set_rag_setting("default_top_k", 10)
    assert get_rag_setting("default_top_k") == 10
    assert get_rag_config()["default_top_k"] == 10
