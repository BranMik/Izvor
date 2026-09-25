import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SYSTEM_SETTINGS_FILE = Path(__file__).parent.parent / "data" / "system_settings.json"
SYSTEM_BACKUP_FILE = Path(__file__).parent.parent / "data" / "backups" / "system_settings_backup.json"
BACKUP_INTERVAL_SECONDS = 7 * 86400


class SystemSettingsManager:
    """
    Single Source of Truth (SSOT) for system-level settings that are changeable ONLY in file (not through UI):
    - max_file_size_mb (float)
    - rag (dict): semantic chunking and LanceDB embedding settings
    """

    _RAG_KEY_ALIASES: dict[str, str] = {
        "set_default_top_k": "default_top_k",
        "top_k": "default_top_k",
    }

    def __init__(
        self,
        settings_file: Path | None = None,
        backup_file: Path | None = None,
        auto_load: bool = True,
    ):
        self._custom_settings_file: bool = settings_file is not None
        self.default_settings_file = settings_file or SYSTEM_SETTINGS_FILE
        self.default_backup_file = backup_file or SYSTEM_BACKUP_FILE

        self.max_file_size_mb: float = 50.0

        self.rag_config: dict[str, Any] = {
            "max_chunk_chars": 3500,
            "chunk_overlap_chars": 300,
            "min_chunk_chars": 80,
            "default_top_k": 6,
            "embedding_model": "gemini/gemini-embedding-001",
            "embedding_dim": 3072,
            "embedding_batch_size": 32,
        }

        self._last_loaded_mtime: float = 0.0

        if auto_load:
            self.load()

    @property
    def active_settings_file(self) -> Path:
        return self.default_settings_file

    def _parse_rag_section(self, rag_data: Any) -> None:
        if isinstance(rag_data, dict):
            def _get_val(keys: list[str], default: Any) -> Any:
                for k in keys:
                    if k in rag_data and rag_data[k] is not None:
                        return rag_data[k]
                return default

            self.rag_config = {
                "max_chunk_chars": int(_get_val(["max_chunk_chars", "MAX_CHUNK_CHARS"], 3500)),
                "chunk_overlap_chars": int(_get_val(["chunk_overlap_chars", "CHUNK_OVERLAP_CHARS"], 300)),
                "min_chunk_chars": int(_get_val(["min_chunk_chars", "MIN_CHUNK_CHARS"], 80)),
                "default_top_k": int(_get_val(["default_top_k", "DEFAULT_TOP_K", "set_default_top_k", "SET_DEFAULT_TOP_K"], 6)),
                "embedding_model": str(_get_val(["embedding_model", "EMBEDDING_MODEL"], "gemini/gemini-embedding-001")),
                "embedding_dim": int(_get_val(["embedding_dim", "EMBEDDING_DIM"], 3072)),
                "embedding_batch_size": int(_get_val(["embedding_batch_size", "EMBEDDING_BATCH_SIZE"], 32)),
            }

    def load(self) -> None:
        target_file = self.active_settings_file
        if not target_file.exists():
            return

        try:
            mtime = target_file.stat().st_mtime
            if mtime == self._last_loaded_mtime and self._last_loaded_mtime > 0:
                return

            with open(target_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            self.max_file_size_mb = float(data.get("max_file_size_mb", 50.0))
            self._parse_rag_section(data.get("rag"))

            self._last_loaded_mtime = mtime
            logger.debug(f"[SYSTEM_SETTINGS] Loaded settings from {target_file}")
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
            logger.error(f"[SYSTEM_SETTINGS] Failed to load {target_file}: {e}")
            if self.default_backup_file.exists():
                try:
                    with open(self.default_backup_file, "r", encoding="utf-8") as bf:
                        data = json.load(bf)
                    self.max_file_size_mb = float(data.get("max_file_size_mb", 50.0))
                    self._parse_rag_section(data.get("rag"))
                    logger.warning("[SYSTEM_SETTINGS] Recovered settings from backup.")
                except (OSError, json.JSONDecodeError, TypeError, ValueError) as be:
                    logger.error(f"[SYSTEM_SETTINGS] Failed to read backup: {be}")

    def save(self) -> None:
        target_file = self.active_settings_file
        target_file.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            "max_file_size_mb": self.max_file_size_mb,
            "rag": self.rag_config,
        }

        tmp_file = target_file.with_suffix(".tmp")
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_file, target_file)
            self._last_loaded_mtime = target_file.stat().st_mtime

            # Update backup if needed
            self._maybe_update_backup(payload)
        except (OSError, TypeError, ValueError) as e:
            logger.error(f"[SYSTEM_SETTINGS] Error saving settings to {target_file}: {e}")
            if tmp_file.exists():
                try:
                    tmp_file.unlink()
                except OSError as ue:
                    logger.debug(f"[SYSTEM_SETTINGS] Failed to remove temp file: {ue}")

    def _maybe_update_backup(self, payload: dict[str, Any]) -> None:
        try:
            now = time.time()
            backup = self.default_backup_file
            if not backup.exists() or (now - backup.stat().st_mtime > BACKUP_INTERVAL_SECONDS):
                backup.parent.mkdir(parents=True, exist_ok=True)
                tmp_b = backup.with_suffix(".tmp")
                with open(tmp_b, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2)
                os.replace(tmp_b, backup)
                logger.info("[SYSTEM_SETTINGS] Updated emergency backup.")
        except (OSError, TypeError, ValueError) as e:
            logger.warning(f"[SYSTEM_SETTINGS] Backup update failed: {e}")

    # --- File Size Limit ---
    def get_max_file_size_mb(self) -> float:
        self.load()
        return float(self.max_file_size_mb)

    def get_max_file_size_bytes(self) -> int:
        return int(self.get_max_file_size_mb() * 1024 * 1024)

    def set_max_file_size_mb(self, val: float, save: bool = True) -> None:
        try:
            self.max_file_size_mb = max(0.1, float(val))
            if save:
                self.save()
            logger.info(f"[SYSTEM_SETTINGS] Set max_file_size_mb to {self.max_file_size_mb} MB.")
        except (ValueError, TypeError) as e:
            logger.error(f"[SYSTEM_SETTINGS] Invalid max_file_size_mb '{val}': {e}")

    # --- RAG Settings ---
    def get_rag_config(self) -> dict[str, Any]:
        self.load()
        return dict(self.rag_config)

    def get_rag_setting(self, key: str, default: Any = None) -> Any:
        self.load()
        key_lower = key.lower()
        resolved_key = self._RAG_KEY_ALIASES.get(key_lower, key_lower)
        for k, v in self.rag_config.items():
            if k.lower() == resolved_key:
                return v
        return default

    def set_rag_setting(self, key: str, value: Any, save: bool = True) -> None:
        self.load()
        self.rag_config[key] = value
        if save:
            self.save()
        logger.info(f"[SYSTEM_SETTINGS] Set RAG setting '{key}' to {value}.")


system_settings_manager = SystemSettingsManager()


# Module-level convenience functions
def get_max_file_size_mb() -> float:
    return system_settings_manager.get_max_file_size_mb()


def get_max_file_size_bytes() -> int:
    return system_settings_manager.get_max_file_size_bytes()


def set_max_file_size_mb(val: float, save: bool = True) -> None:
    system_settings_manager.set_max_file_size_mb(val, save=save)


def get_rag_config() -> dict[str, Any]:
    return system_settings_manager.get_rag_config()


def get_rag_setting(key: str, default: Any = None) -> Any:
    return system_settings_manager.get_rag_setting(key, default)


def set_rag_setting(key: str, value: Any, save: bool = True) -> None:
    system_settings_manager.set_rag_setting(key, value, save=save)
