import copy
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from core.models import CircuitBreakerConfig
from core.paths import APP_BACKUP_FILE, APP_SETTINGS_FILE
from orchestration.settings_store import JsonSettingsStore

logger = logging.getLogger(__name__)

BACKUP_INTERVAL_SECONDS = 7 * 86400


class AppSettingsModel(BaseModel):
    thread_skill_expiration_time: float = 12.0
    circuit_breaker_enabled: bool = True
    circuit_breaker_threshold_usd: float = 0.0
    daily_budget_usd: float = 10.0
    scribe_enabled: bool = True
    convergence_fast_forward_enabled: bool = True
    weekly_pricing_update_enabled: bool = True
    workspace_context: str = ""


class AppSettingsManager:
    """
    Single Source of Truth (SSOT) for application-level operational settings changeable via Slack UI:
    - thread_skill_expiration_time (hours)
    - circuit_breaker_enabled (bool)
    - circuit_breaker_threshold_usd (float)
    - scribe_enabled (bool)
    - weekly_pricing_update_enabled (bool)
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

        self._store = JsonSettingsStore[AppSettingsModel](
            settings_file=self.default_settings_file,
            model_cls=AppSettingsModel,
            backup_file=self.default_backup_file,
            default_data=AppSettingsModel().model_dump(),
        )

        self.thread_skill_expiration_time: float = 12.0
        self.circuit_breaker_enabled: bool = True
        self.circuit_breaker_threshold_usd: float = 0.0
        self.daily_budget_usd: float = 10.0
        self.scribe_enabled: bool = True
        self.convergence_fast_forward_enabled: bool = True
        self.weekly_pricing_update_enabled: bool = True
        self.workspace_context: str = ""
        self._raw_data: dict[str, Any] = {}
        self._last_loaded_mtime: float = 0.0

        if auto_load:
            self.load()

    @property
    def active_settings_file(self) -> Path:
        return self.default_settings_file

    def load(self, force: bool = False) -> None:
        target_file = self.active_settings_file
        if not target_file.exists():
            return
        try:
            mtime = target_file.stat().st_mtime
            if not force and mtime == self._last_loaded_mtime and self._last_loaded_mtime > 0:
                return
            model = self._store.load(force=force)
            self.thread_skill_expiration_time = float(model.thread_skill_expiration_time)
            self.circuit_breaker_enabled = bool(model.circuit_breaker_enabled)
            self.circuit_breaker_threshold_usd = float(model.circuit_breaker_threshold_usd)
            self.daily_budget_usd = float(model.daily_budget_usd)
            self.scribe_enabled = bool(model.scribe_enabled)
            self.convergence_fast_forward_enabled = bool(model.convergence_fast_forward_enabled)
            self.weekly_pricing_update_enabled = bool(model.weekly_pricing_update_enabled)
            self.workspace_context = str(model.workspace_context).strip()[:1000]
            self._raw_data = self._store.get_raw_data()
            self._last_loaded_mtime = mtime
            logger.debug(f"[APP_SETTINGS] Loaded settings from {self.active_settings_file}")
        except Exception as e:
            logger.error(f"[APP_SETTINGS] Failed to load {target_file}: {e}")

    def save(self) -> None:
        target_file = self.active_settings_file
        target_file.parent.mkdir(parents=True, exist_ok=True)
        payload = copy.deepcopy(self._raw_data) if hasattr(self, "_raw_data") and isinstance(self._raw_data, dict) else {}
        payload.update({
            "thread_skill_expiration_time": self.thread_skill_expiration_time,
            "circuit_breaker_enabled": self.circuit_breaker_enabled,
            "circuit_breaker_threshold_usd": self.circuit_breaker_threshold_usd,
            "daily_budget_usd": self.daily_budget_usd,
            "scribe_enabled": self.scribe_enabled,
            "convergence_fast_forward_enabled": self.convergence_fast_forward_enabled,
            "weekly_pricing_update_enabled": self.weekly_pricing_update_enabled,
            "workspace_context": self.workspace_context,
        })
        self._store.save(payload)
        self._raw_data = copy.deepcopy(payload)
        if target_file.exists():
            try:
                self._last_loaded_mtime = target_file.stat().st_mtime
            except OSError:
                pass
        self._maybe_update_backup(payload)

    def _maybe_update_backup(self, payload: dict[str, Any]) -> None:
        try:
            now = time.time()
            backup = self.default_backup_file
            if not backup.exists() or (now - backup.stat().st_mtime > BACKUP_INTERVAL_SECONDS):
                backup.parent.mkdir(parents=True, exist_ok=True)
                tmp_b_name = f"{backup.name}.tmp_{os.getpid()}_{int(time.time() * 1000)}"
                tmp_b = backup.parent / tmp_b_name
                with open(tmp_b, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                for attempt in range(10):
                    try:
                        os.replace(tmp_b, backup)
                        break
                    except (PermissionError, OSError) as pe:
                        if attempt < 9:
                            time.sleep(0.02 * (attempt + 1))
                        else:
                            raise pe
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
        if not self.circuit_breaker_enabled:
            self.circuit_breaker_threshold_usd = 0.0
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

    def get_daily_budget_usd(self) -> float:
        self.load()
        return float(self.daily_budget_usd)

    def set_daily_budget_usd(self, val: float, save: bool = True) -> None:
        try:
            self.daily_budget_usd = max(float(val), 0.0)
            if save:
                self.save()
            logger.info(f"[APP_SETTINGS] Set daily_budget_usd to ${self.daily_budget_usd:.2f} USD.")
        except (ValueError, TypeError) as e:
            logger.error(f"[APP_SETTINGS] Invalid daily_budget_usd '{val}': {e}")

    def get_circuit_breaker_config(self) -> CircuitBreakerConfig:
        self.load()
        return CircuitBreakerConfig(
            enabled=self.circuit_breaker_enabled,
            threshold_usd=self.circuit_breaker_threshold_usd,
            daily_budget_usd=self.daily_budget_usd,
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

    # --- Consensus Convergence Fast-Forward ---
    def is_convergence_fast_forward_enabled(self) -> bool:
        self.load()
        return bool(self.convergence_fast_forward_enabled)

    def set_convergence_fast_forward_enabled(self, enabled: bool, save: bool = True) -> None:
        self.convergence_fast_forward_enabled = bool(enabled)
        if save:
            self.save()
        logger.info(f"[APP_SETTINGS] Set convergence_fast_forward_enabled to {self.convergence_fast_forward_enabled}.")


    # --- Workspace Baseline Context ---
    def get_workspace_context(self) -> str:
        self.load()
        return self.workspace_context

    def set_workspace_context(self, context: str, save: bool = True) -> None:
        clean = str(context or "").strip()[:1000]
        self.workspace_context = clean
        if save:
            self.save()
        logger.info(f"[APP_SETTINGS] Set workspace_context ({len(clean)} chars).")
        try:
            from orchestration.agent_settings import settings_manager
            settings_manager.refresh_agent_prompts()
        except (ImportError, AttributeError) as e:
            logger.debug(f"[APP_SETTINGS] Note: could not trigger refresh_agent_prompts: {e}")


app_settings_manager = AppSettingsManager()


# Module-level convenience functions
def get_workspace_context() -> str:
    return app_settings_manager.get_workspace_context()


def set_workspace_context(context: str, save: bool = True) -> None:
    app_settings_manager.set_workspace_context(context, save=save)


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


def get_daily_budget_usd() -> float:
    return app_settings_manager.get_daily_budget_usd()


def set_daily_budget_usd(val: float, save: bool = True) -> None:
    app_settings_manager.set_daily_budget_usd(val, save=save)


def is_convergence_fast_forward_enabled() -> bool:
    return app_settings_manager.is_convergence_fast_forward_enabled()


def set_convergence_fast_forward_enabled(enabled: bool, save: bool = True) -> None:
    app_settings_manager.set_convergence_fast_forward_enabled(enabled, save=save)


