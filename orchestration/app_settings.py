import fnmatch
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from core.models import CircuitBreakerConfig

logger = logging.getLogger(__name__)

APP_SETTINGS_FILE = Path(__file__).parent.parent / "data" / "app_settings.json"
APP_BACKUP_FILE = Path(__file__).parent.parent / "data" / "backups" / "app_settings_backup.json"
BACKUP_INTERVAL_SECONDS = 7 * 86400

DEFAULT_PROTECTED_WRITE_PATHS: list[str] = [
    ".env",
    ".git",
    "data/agent_settings.json",
    "data/app_settings.json",
]


class AppSettingsManager:
    """
    Single Source of Truth (SSOT) for application-level operational settings changeable via Slack UI:
    - thread_skill_expiration_time (hours)
    - circuit_breaker_enabled (bool)
    - circuit_breaker_threshold_usd (float)
    - scribe_enabled (bool)
    - weekly_pricing_update_enabled (bool)
    - workspace_write_blacklist (list[str])
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
        self.weekly_pricing_update_enabled: bool = True
        self.workspace_write_blacklist: list[str] = []

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
            self.circuit_breaker_enabled = bool(data.get("circuit_breaker_enabled", False))
            self.circuit_breaker_threshold_usd = float(data.get("circuit_breaker_threshold_usd", 0.0))
            self.scribe_enabled = bool(data.get("scribe_enabled", True))
            self.weekly_pricing_update_enabled = bool(data.get("weekly_pricing_update_enabled", True))
            raw_bl = data.get("workspace_write_blacklist", [])
            self.workspace_write_blacklist = (
                [str(p).strip() for p in raw_bl if str(p).strip()]
                if isinstance(raw_bl, list)
                else []
            )

            self._last_loaded_mtime = mtime
            logger.debug(f"[APP_SETTINGS] Loaded settings from {target_file}")
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
            logger.error(f"[APP_SETTINGS] Failed to load {target_file}: {e}")
            if self.default_backup_file.exists():
                try:
                    with open(self.default_backup_file, "r", encoding="utf-8") as bf:
                        data = json.load(bf)
                    self.thread_skill_expiration_time = float(data.get("thread_skill_expiration_time", 12.0))
                    self.circuit_breaker_enabled = bool(data.get("circuit_breaker_enabled", False))
                    self.circuit_breaker_threshold_usd = float(data.get("circuit_breaker_threshold_usd", 0.0))
                    self.scribe_enabled = bool(data.get("scribe_enabled", True))
                    self.weekly_pricing_update_enabled = bool(data.get("weekly_pricing_update_enabled", True))
                    raw_bl = data.get("workspace_write_blacklist", [])
                    self.workspace_write_blacklist = (
                        [str(p).strip() for p in raw_bl if str(p).strip()]
                        if isinstance(raw_bl, list)
                        else []
                    )
                    logger.warning("[APP_SETTINGS] Recovered settings from backup.")
                except (OSError, json.JSONDecodeError, TypeError, ValueError) as be:
                    logger.error(f"[APP_SETTINGS] Failed to read backup: {be}")

    def save(self) -> None:
        target_file = self.active_settings_file
        target_file.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            "thread_skill_expiration_time": self.thread_skill_expiration_time,
            "circuit_breaker_enabled": self.circuit_breaker_enabled,
            "circuit_breaker_threshold_usd": self.circuit_breaker_threshold_usd,
            "scribe_enabled": self.scribe_enabled,
            "weekly_pricing_update_enabled": self.weekly_pricing_update_enabled,
            "_comment_workspace_write_blacklist": (
                "Automatic hardcoded protection blocks writes to .env, .git, data/agent_settings.json, and data/app_settings.json. "
                "Add any additional relative workspace paths or file patterns here to prevent unauthorized modifications."
            ),
            "workspace_write_blacklist": self.workspace_write_blacklist,
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
            logger.error(f"[APP_SETTINGS] Error saving settings to {target_file}: {e}")
            if tmp_file.exists():
                try:
                    tmp_file.unlink()
                except OSError as ue:
                    logger.debug(f"[APP_SETTINGS] Failed to remove temp file: {ue}")

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
        except (OSError, TypeError, ValueError) as e:
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

    # --- Weekly Model Pricing Update ---
    def is_weekly_pricing_update_enabled(self) -> bool:
        self.load()
        return bool(self.weekly_pricing_update_enabled)

    def set_weekly_pricing_update_enabled(self, enabled: bool, save: bool = True) -> None:
        self.weekly_pricing_update_enabled = bool(enabled)
        if save:
            self.save()
        logger.info(f"[APP_SETTINGS] Set weekly_pricing_update_enabled to {self.weekly_pricing_update_enabled}.")

    # --- Workspace Write Blacklist ---
    def get_workspace_write_blacklist(self) -> list[str]:
        self.load()
        return list(self.workspace_write_blacklist)

    def set_workspace_write_blacklist(self, paths: list[str], save: bool = True) -> None:
        self.workspace_write_blacklist = [str(p).strip() for p in paths if str(p).strip()]
        if save:
            self.save()
        logger.info(f"[APP_SETTINGS] Set workspace_write_blacklist to {self.workspace_write_blacklist}.")

    def is_path_write_blocked(
        self,
        target_path: str | Path,
        base_dir: Path | None = None,
        custom_blacklist: list[str] | None = None,
    ) -> bool:
        """
        Determines whether writing to target_path is blocked by default protections
        (.env, .git, data/agent_settings.json, data/app_settings.json) or
        the configured workspace_write_blacklist.
        """
        if base_dir is None:
            base_dir = Path(__file__).resolve().parent.parent
        else:
            base_dir = Path(base_dir).resolve()

        if isinstance(target_path, str):
            clean_str = target_path.strip().replace("\\", "/")
            p_obj = Path(clean_str)
            target = p_obj if p_obj.is_absolute() else (base_dir / clean_str).resolve()
        else:
            target = target_path.resolve()

        try:
            rel_posix = target.relative_to(base_dir).as_posix()
        except ValueError:
            rel_posix = target.as_posix()

        target_name = target.name.lower()

        # 1. Hardcoded default protections:
        # - .env or any .env.* file (e.g. .env, .env.local)
        if target_name == ".env" or target_name.startswith(".env."):
            return True

        # - .git directory or files inside .git
        parts_lower = [part.lower() for part in target.parts]
        if ".git" in parts_lower:
            return True

        # - agent_settings.json and app_settings.json
        if target_name in ("agent_settings.json", "app_settings.json"):
            return True

        for d_path in DEFAULT_PROTECTED_WRITE_PATHS:
            try:
                d_resolved = (base_dir / d_path).resolve()
                if target == d_resolved or target.is_relative_to(d_resolved):
                    return True
            except (ValueError, OSError):
                pass

        # 2. Configurable workspace_write_blacklist
        blacklist = (
            custom_blacklist
            if custom_blacklist is not None
            else self.get_workspace_write_blacklist()
        )
        for item in blacklist:
            clean_item = str(item).strip()
            if not clean_item:
                continue
            norm_item = clean_item.replace("\\", "/")
            if fnmatch.fnmatch(rel_posix, norm_item) or fnmatch.fnmatch(
                rel_posix, f"*/{norm_item}"
            ):
                return True
            if fnmatch.fnmatch(target_name, norm_item.lower()):
                return True
            try:
                item_resolved = (base_dir / clean_item).resolve()
                if target == item_resolved or target.is_relative_to(item_resolved):
                    return True
            except (ValueError, OSError):
                pass

        return False


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


def is_weekly_pricing_update_enabled() -> bool:
    return app_settings_manager.is_weekly_pricing_update_enabled()


def set_weekly_pricing_update_enabled(enabled: bool, save: bool = True) -> None:
    app_settings_manager.set_weekly_pricing_update_enabled(enabled, save=save)


def get_workspace_write_blacklist() -> list[str]:
    return app_settings_manager.get_workspace_write_blacklist()


def set_workspace_write_blacklist(paths: list[str], save: bool = True) -> None:
    app_settings_manager.set_workspace_write_blacklist(paths, save=save)


def is_path_write_blocked(
    target_path: str | Path,
    base_dir: Path | None = None,
    custom_blacklist: list[str] | None = None,
) -> bool:
    return app_settings_manager.is_path_write_blocked(
        target_path, base_dir=base_dir, custom_blacklist=custom_blacklist
    )

