import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from core.models import CircuitBreakerConfig

logger = logging.getLogger(__name__)

APP_SETTINGS_FILE = Path(__file__).parent.parent / "data" / "app_settings.json"
APP_BACKUP_FILE = Path(__file__).parent.parent / "data" / "backups" / "app_settings_backup.json"
BACKUP_INTERVAL_SECONDS = 7 * 86400


class AppSettingsManager:
    """
    Single Source of Truth (SSOT) for application-level operational settings:
    - thread_skill_expiration_time (hours)
    - circuit_breaker_enabled (bool)
    - circuit_breaker_threshold_usd (float)
    - scribe_enabled (bool)
    """

    def __init__(
        self,
        settings_file: Path | None = None,
        backup_file: Path | None = None,
        auto_load: bool = True,
    ):
        self._custom_settings_file: bool = settings_file is not None
        self.default_settings_file = settings_file or APP_SETTINGS_FILE
        self.default_backup_file = backup_file or APP_BACKUP_FILE

        self.thread_skill_expiration_time: float = 12.0
        self.circuit_breaker_enabled: bool = False
        self.circuit_breaker_threshold_usd: float = 0.0
        self.scribe_enabled: bool = True

        self._last_loaded_mtime: float = 0.0

        if auto_load:
            self.load()

    @property
    def active_settings_file(self) -> Path:
        return self.default_settings_file

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

            self.thread_skill_expiration_time = float(data.get("thread_skill_expiration_time", 12.0))
            self.circuit_breaker_enabled = bool(data.get("circuit_breaker_enabled", True))
            self.circuit_breaker_threshold_usd = float(data.get("circuit_breaker_threshold_usd", 1.5))
            self.scribe_enabled = bool(data.get("scribe_enabled", True))

            self._last_loaded_mtime = mtime
            logger.debug(f"[APP_SETTINGS] Loaded settings from {target_file}")
        except Exception as e:
            logger.error(f"[APP_SETTINGS] Failed to load {target_file}: {e}")
            if self.default_backup_file.exists():
                try:
                    with open(self.default_backup_file, "r", encoding="utf-8") as bf:
                        data = json.load(bf)
                    self.thread_skill_expiration_time = float(data.get("thread_skill_expiration_time", 12.0))
                    self.circuit_breaker_enabled = bool(data.get("circuit_breaker_enabled", True))
                    self.circuit_breaker_threshold_usd = float(data.get("circuit_breaker_threshold_usd", 1.5))
                    self.scribe_enabled = bool(data.get("scribe_enabled", True))
                    logger.warning("[APP_SETTINGS] Recovered settings from backup.")
                except Exception as be:
                    logger.error(f"[APP_SETTINGS] Failed to read backup: {be}")

    def save(self) -> None:
        target_file = self.active_settings_file
        target_file.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            "thread_skill_expiration_time": self.thread_skill_expiration_time,
            "circuit_breaker_enabled": self.circuit_breaker_enabled,
            "circuit_breaker_threshold_usd": self.circuit_breaker_threshold_usd,
            "scribe_enabled": self.scribe_enabled,
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
        except Exception as e:
            logger.error(f"[APP_SETTINGS] Error saving settings to {target_file}: {e}")
            if tmp_file.exists():
                try:
                    tmp_file.unlink()
                except Exception:
                    pass

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
                logger.info("[APP_SETTINGS] Updated emergency backup.")
        except Exception as e:
            logger.warning(f"[APP_SETTINGS] Backup update failed: {e}")

    # --- Thread Skill Expiration ---
    def get_thread_skill_expiration_time(self) -> float:
        self.load()
        return float(self.thread_skill_expiration_time)

    def set_thread_skill_expiration_time(self, hours: float, save: bool = True) -> None:
        val = max(0.1, float(hours))
        self.thread_skill_expiration_time = val
        if save:
            self.save()
        logger.info(f"[APP_SETTINGS] Set thread_skill_expiration_time to {val} hours.")

    # --- Circuit Breaker ---
    def is_circuit_breaker_enabled(self) -> bool:
        self.load()
        return bool(self.circuit_breaker_enabled)

    def set_circuit_breaker_enabled(self, enabled: bool, save: bool = True) -> None:
        self.circuit_breaker_enabled = bool(enabled)
        if save:
            self.save()
        logger.info(f"[APP_SETTINGS] Set circuit_breaker_enabled to {self.circuit_breaker_enabled}.")

    def get_circuit_breaker_threshold(self) -> float:
        self.load()
        return float(self.circuit_breaker_threshold_usd)

    def set_circuit_breaker_threshold(self, val: float, save: bool = True) -> None:
        try:
            self.circuit_breaker_threshold_usd = max(float(val), 0.0)
            if save:
                self.save()
            logger.info(f"[APP_SETTINGS] Set circuit_breaker_threshold to ${self.circuit_breaker_threshold_usd:.2f} USD.")
        except (ValueError, TypeError) as e:
            logger.error(f"[APP_SETTINGS] Invalid circuit-breaker threshold '{val}': {e}")

    def get_circuit_breaker_config(self) -> CircuitBreakerConfig:
        self.load()
        return CircuitBreakerConfig(
            enabled=self.circuit_breaker_enabled,
            threshold_usd=self.circuit_breaker_threshold_usd,
        )

    # --- Live Discussion Scribe ---
    def is_scribe_enabled(self) -> bool:
        self.load()
        return bool(self.scribe_enabled)

    def set_scribe_enabled(self, enabled: bool, save: bool = True) -> None:
        self.scribe_enabled = bool(enabled)
        if save:
            self.save()
        logger.info(f"[APP_SETTINGS] Set scribe_enabled to {self.scribe_enabled}.")


app_settings_manager = AppSettingsManager()

# Module-level convenience functions
def get_thread_skill_expiration_time() -> float:
    return app_settings_manager.get_thread_skill_expiration_time()

def set_thread_skill_expiration_time(hours: float, save: bool = True) -> None:
    app_settings_manager.set_thread_skill_expiration_time(hours, save=save)

def is_circuit_breaker_enabled() -> bool:
    return app_settings_manager.is_circuit_breaker_enabled()

def set_circuit_breaker_enabled(enabled: bool, save: bool = True) -> None:
    app_settings_manager.set_circuit_breaker_enabled(enabled, save=save)

def get_circuit_breaker_threshold() -> float:
    return app_settings_manager.get_circuit_breaker_threshold()

def set_circuit_breaker_threshold(val: float, save: bool = True) -> None:
    app_settings_manager.set_circuit_breaker_threshold(val, save=save)

def is_scribe_enabled() -> bool:
    return app_settings_manager.is_scribe_enabled()

def set_scribe_enabled(enabled: bool, save: bool = True) -> None:
    app_settings_manager.set_scribe_enabled(enabled, save=save)

def get_circuit_breaker_config() -> CircuitBreakerConfig:
    return app_settings_manager.get_circuit_breaker_config()
