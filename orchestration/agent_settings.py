import json
import logging
import os
from pathlib import Path
import sys
import time
from typing import Any

from core.config import (
    AGENTS_CONFIG,
    SYSTEM_AGENTS_CONFIG,
)
from core.models import CircuitBreakerConfig, OrchestrationMode
from prompts import build_agent_prompt

logger = logging.getLogger(__name__)

# Paths to settings and emergency backup files
SETTINGS_FILE = Path(__file__).parent.parent / "data" / "agent_settings.json"
BACKUP_FILE = Path(__file__).parent.parent / "data" / "backups" / "agent_settings_backup.json"
PERSONAS_DIR = Path(__file__).parent.parent / "prompts" / "personas"

# Mandatory locked agents (empty by default now that lock system is removed)
LOCKED_AGENTS: set[str] = set()

# 7-day interval in seconds (604,800 seconds)
BACKUP_INTERVAL_SECONDS = 7 * 86400


class AgentSettingsManager:
    """
    Manages persistent agent configurations, system agent models, and thread circuit breaker limits.
    Serves as the Single Source of Truth (SSOT) loaded from data/agent_settings.json.
    Includes atomic file writes, 7-day emergency backups, non-destructive failover, and manual restore.
    """

    def __init__(
        self,
        settings_file: Path | None = None,
        backup_file: Path | None = None,
        locked_agents: set[str] | None = None,
        backup_interval_seconds: float = BACKUP_INTERVAL_SECONDS,
        auto_load: bool = True,
    ):
        self._custom_settings_file: bool = settings_file is not None
        self._custom_backup_file: bool = backup_file is not None
        self.default_settings_file = settings_file or SETTINGS_FILE
        self.default_backup_file = backup_file or BACKUP_FILE
        self.locked_agents = set(locked_agents or set())
        self.backup_interval_seconds = backup_interval_seconds

        if settings_file is not None or backup_file is not None:
            # Isolated dictionaries for custom instances (e.g. tests)
            self.agents_config = {}
            self.system_agents_config = {}
        else:
            # Global default instance bound to core.config
            self.agents_config = AGENTS_CONFIG
            self.system_agents_config = SYSTEM_AGENTS_CONFIG
        self.enabled_agents: set[str] = set()
        self.circuit_breaker_enabled: bool = False
        self.circuit_breaker_threshold_usd: float = 1.0

        # Orchestration flow mode and chains
        self.orchestration_mode: str = OrchestrationMode.SANDWICH.value
        self.sandwich_start_chain: list[str] = []
        self.sandwich_end_chain: list[str] = []
        self.fixed_chain: list[str] = []

        # Emergency backup status
        self.is_running_from_backup: bool = False
        self.backup_error_reason: str | None = None

        if auto_load:
            self.load()

    @property
    def active_settings_file(self) -> Path:
        if self._custom_settings_file:
            return self.default_settings_file
        router_mod = sys.modules.get("agent_router") or sys.modules.get("orchestration.agent_router")
        if router_mod and hasattr(router_mod, "SETTINGS_FILE"):
            return Path(getattr(router_mod, "SETTINGS_FILE"))
        agent_settings_mod = sys.modules.get("agent_settings") or sys.modules.get("orchestration.agent_settings")
        if agent_settings_mod and hasattr(agent_settings_mod, "SETTINGS_FILE"):
            return Path(getattr(agent_settings_mod, "SETTINGS_FILE"))
        return self.default_settings_file

    @property
    def active_backup_file(self) -> Path:
        if self._custom_backup_file:
            return self.default_backup_file
        router_mod = sys.modules.get("agent_router") or sys.modules.get("orchestration.agent_router")
        if router_mod and hasattr(router_mod, "BACKUP_FILE"):
            return Path(getattr(router_mod, "BACKUP_FILE"))
        agent_settings_mod = sys.modules.get("agent_settings") or sys.modules.get("orchestration.agent_settings")
        if agent_settings_mod and hasattr(agent_settings_mod, "BACKUP_FILE"):
            return Path(getattr(agent_settings_mod, "BACKUP_FILE"))
        return self.default_backup_file

    def _atomic_write_json(self, target_path: Path, data: dict[str, Any]) -> None:
        """Writes JSON payload atomically to a temporary file before renaming."""
        target_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_filename = f"{target_path.name}.tmp_{os.getpid()}_{int(time.time() * 1000)}"
        tmp_path = target_path.parent / tmp_filename
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, target_path)
        except Exception:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass
            raise

    def _validate_agent_entry(self, key: str, config: dict[str, Any]) -> bool:
        """Validates that an agent has required parameters and a matching persona file."""
        if not isinstance(config, dict):
            logger.warning(f"[SETTINGS VALIDATION] Agent '{key}' configuration is not a dictionary. Skipping.")
            return False

        model = config.get("model")
        username = config.get("username")
        icon_url = config.get("icon_url")

        if not model or not isinstance(model, str) or not model.strip():
            logger.warning(f"[SETTINGS VALIDATION] Agent '{key}' is missing valid 'model'. Skipping.")
            return False

        if not username or not isinstance(username, str) or not username.strip():
            logger.warning(f"[SETTINGS VALIDATION] Agent '{key}' is missing valid 'username'. Skipping.")
            return False

        if not icon_url or not isinstance(icon_url, str) or not icon_url.strip():
            logger.warning(f"[SETTINGS VALIDATION] Agent '{key}' is missing valid 'icon_url'. Skipping.")
            return False

        # Validate persona prompt markdown file
        persona_file = PERSONAS_DIR / f"{key}.md"
        if not persona_file.exists():
            logger.warning(
                f"[SETTINGS VALIDATION] Persona prompt file '{persona_file.name}' not found for agent '{key}'. Skipping."
            )
            return False

        return True

    def _parse_and_validate_payload(self, data: dict[str, Any], source_name: str) -> bool:
        """Parses and validates a full agent settings payload from JSON."""
        if not isinstance(data, dict):
            logger.error(f"[SETTINGS] Payload from {source_name} is not a valid JSON object.")
            return False

        raw_agents = data.get("agents", {})
        if not isinstance(raw_agents, dict):
            logger.error(f"[SETTINGS] 'agents' key in {source_name} is not a dictionary.")
            return False

        # Sync SHORT_OUTPUT_AGENTS before building agent prompts (only for primary settings file)
        if self.active_settings_file == SETTINGS_FILE:
            from prompts.builder import SHORT_OUTPUT_AGENTS
            new_short_agents = {
                k for k, cfg in raw_agents.items()
                if isinstance(cfg, dict) and cfg.get("is_short_output") is True
            }
            SHORT_OUTPUT_AGENTS.clear()
            SHORT_OUTPUT_AGENTS.update(new_short_agents)

        valid_agents: dict[str, dict[str, Any]] = {}
        for key, cfg in raw_agents.items():
            if self._validate_agent_entry(key, cfg):
                is_short = bool(cfg.get("is_short_output", False))
                skills = [
                    s.strip()
                    for s in cfg.get("skills", [])
                    if isinstance(s, str) and s.strip()
                ]
                valid_agents[key] = {
                    "model": cfg["model"].strip(),
                    "username": cfg["username"].strip(),
                    "icon_url": cfg["icon_url"].strip(),
                    "is_short_output": is_short,
                    "skills": skills,
                    "system_prompt": build_agent_prompt(key, skills=skills),
                }

        if not valid_agents:
            logger.error(f"[SETTINGS] Zero valid agents found in {source_name}. Validation failed.")
            return False

        # Update agents_config in-place so all imported references stay synced
        self.agents_config.clear()
        self.agents_config.update(valid_agents)

        # Parse system agents
        self.system_agents_config.clear()
        raw_system_agents = data.get("system_agents", {})
        if isinstance(raw_system_agents, dict):
            for sys_k, sys_v in raw_system_agents.items():
                if isinstance(sys_v, dict):
                    sys_entry = dict(sys_v)
                    if sys_v.get("model"):
                        sys_entry["model"] = str(sys_v["model"]).strip()
                    if sys_v.get("username"):
                        sys_entry["username"] = str(sys_v["username"]).strip()
                    if sys_v.get("icon_url"):
                        sys_entry["icon_url"] = str(sys_v["icon_url"]).strip()
                    if sys_v.get("agent_key"):
                        sys_entry["agent_key"] = str(sys_v["agent_key"]).strip()
                    if "is_system_agent" in sys_v:
                        sys_entry["is_system_agent"] = bool(sys_v["is_system_agent"])
                    if sys_entry:
                        self.system_agents_config[sys_k] = sys_entry

        # Parse circuit breaker settings
        self.circuit_breaker_enabled = bool(data.get("circuit_breaker_enabled", False))
        try:
            self.circuit_breaker_threshold_usd = float(data.get("circuit_breaker_threshold_usd", 1.0))
        except (ValueError, TypeError):
            self.circuit_breaker_threshold_usd = 1.0

        # Parse orchestration mode
        raw_mode = str(data.get("orchestration_mode", OrchestrationMode.SANDWICH.value)).strip().lower()
        self.orchestration_mode = (
            raw_mode
            if raw_mode in (OrchestrationMode.SANDWICH.value, OrchestrationMode.FIXED.value)
            else OrchestrationMode.SANDWICH.value
        )

        # Parse sandwich config
        raw_sandwich = data.get("sandwich_config", {})
        if isinstance(raw_sandwich, dict):
            raw_start = raw_sandwich.get("start_chain", [])
            raw_end = raw_sandwich.get("end_chain", [])
            self.sandwich_start_chain = [
                k for k in raw_start if isinstance(k, str) and k in self.agents_config
            ] if isinstance(raw_start, list) else []
            self.sandwich_end_chain = [
                k for k in raw_end if isinstance(k, str) and k in self.agents_config
            ] if isinstance(raw_end, list) else []
        else:
            self.sandwich_start_chain = []
            self.sandwich_end_chain = []

        # Parse fixed config
        raw_fixed = data.get("fixed_config", {})
        if isinstance(raw_fixed, dict):
            raw_chain = raw_fixed.get("chain", [])
            self.fixed_chain = [
                k for k in raw_chain if isinstance(k, str) and k in self.agents_config
            ] if isinstance(raw_chain, list) else []
        else:
            self.fixed_chain = []

        # Parse enabled agents
        raw_enabled = data.get("enabled_agents", [])
        if isinstance(raw_enabled, list):
            valid_enabled = {k for k in raw_enabled if k in self.agents_config}
        else:
            valid_enabled = set()

        # If enabled list is empty, default to all valid agents
        if not valid_enabled:
            valid_enabled.update(self.agents_config.keys())

        self.enabled_agents.clear()
        self.enabled_agents.update(valid_enabled)

        logger.info(
            f"[SETTINGS] Successfully validated payload from {source_name}: "
            f"{len(self.agents_config)} agents registered, {len(self.enabled_agents)} enabled, "
            f"mode='{self.orchestration_mode}', "
            f"{len(self.system_agents_config)} system agents."
        )
        return True

    def check_and_update_backup(self) -> bool:
        """
        Updates the emergency backup copy in data/backups/agent_settings_backup.json if:
        1. Backup does not exist, or
        2. Backup is older than backup_interval_seconds (7 days).
        Never overwrites backup while running in emergency backup mode.
        """
        if self.is_running_from_backup:
            return False

        target_file = self.active_settings_file
        backup_file = self.active_backup_file

        if not target_file.exists():
            return False

        needs_backup = False
        if not backup_file.exists():
            needs_backup = True
        else:
            try:
                mtime = backup_file.stat().st_mtime
                if (time.time() - mtime) >= self.backup_interval_seconds:
                    needs_backup = True
            except Exception as e:
                logger.debug(f"[SETTINGS] Error checking backup mtime: {e}")
                needs_backup = True

        if needs_backup:
            try:
                with open(target_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._atomic_write_json(backup_file, data)
                logger.info(f"[SETTINGS] Updated 7-day emergency backup: {backup_file.name}")
                return True
            except Exception as e:
                logger.error(f"[SETTINGS] Failed to update emergency backup: {e}")
                return False

        return False

    def load(self) -> set[str]:
        """
        Loads agent settings and system models from active settings JSON.
        If active settings file is corrupted, unreadable, or missing:
        - Loads from emergency backup into memory without modifying the broken active file.
        - Sets is_running_from_backup = True.
        """
        target_file = self.active_settings_file
        err_reason: str | None = None

        if target_file.exists():
            try:
                with open(target_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if self._parse_and_validate_payload(data, target_file.name):
                    self.is_running_from_backup = False
                    self.backup_error_reason = None
                    self.check_and_update_backup()
                    return set(self.enabled_agents)
                else:
                    err_reason = f"Schema validation failed in {target_file.name}"
            except Exception as e:
                err_reason = f"Unreadable/corrupted JSON in {target_file.name}: {e}"
        else:
            err_reason = f"Active settings file '{target_file.name}' not found."

        # Failover to emergency backup
        backup_file = self.active_backup_file
        logger.warning(
            f"[SETTINGS] Primary settings failed ({err_reason}). Attempting failover to emergency backup: {backup_file.name}"
        )

        if backup_file.exists():
            try:
                with open(backup_file, "r", encoding="utf-8") as f:
                    backup_data = json.load(f)
                if self._parse_and_validate_payload(backup_data, backup_file.name):
                    self.is_running_from_backup = True
                    self.backup_error_reason = err_reason
                    logger.warning(
                        f"[SETTINGS] System is running in EMERGENCY BACKUP MODE using {backup_file.name}. "
                        f"Original file '{target_file.name}' was left untouched."
                    )
                    return set(self.enabled_agents)
            except Exception as backup_err:
                logger.critical(f"[SETTINGS] Failed reading emergency backup: {backup_err}")

        # If both fail, raise RuntimeError
        msg = f"Failed to load agent settings from active file ({err_reason}) and emergency backup."
        logger.critical(f"[SETTINGS] {msg}")
        raise RuntimeError(msg)

    def restore_from_backup(self) -> bool:
        """
        Restores the active settings file by copying the emergency backup over it.
        Resets emergency backup mode upon successful load.
        """
        backup_file = self.active_backup_file
        target_file = self.active_settings_file

        if not backup_file.exists():
            logger.error(f"[SETTINGS] Cannot restore: emergency backup file '{backup_file}' does not exist.")
            return False

        try:
            with open(backup_file, "r", encoding="utf-8") as f:
                backup_data = json.load(f)

            if not self._parse_and_validate_payload(backup_data, backup_file.name):
                logger.error("[SETTINGS] Emergency backup data failed validation. Restore aborted.")
                return False

            self._atomic_write_json(target_file, backup_data)
            self.is_running_from_backup = False
            self.backup_error_reason = None
            self.load()
            logger.info(f"[SETTINGS] Successfully restored active settings file from {backup_file.name}.")
            return True
        except Exception as e:
            logger.error(f"[SETTINGS] Error executing restore from backup: {e}")
            return False

    def save(self) -> None:
        """
        Saves current enabled debate agents, agent configurations, circuit breaker,
        and orchestration chain settings to active JSON.
        Resets emergency backup mode once active file is successfully saved.
        """
        target_file = self.active_settings_file
        try:
            data = {
                "orchestration_mode": self.orchestration_mode,
                "sandwich_config": {
                    "start_chain": self.sandwich_start_chain,
                    "end_chain": self.sandwich_end_chain,
                },
                "fixed_config": {
                    "chain": self.fixed_chain,
                },
                "circuit_breaker_enabled": self.circuit_breaker_enabled,
                "circuit_breaker_threshold_usd": self.circuit_breaker_threshold_usd,
                "enabled_agents": sorted(list(self.enabled_agents)),
                "agents": {
                    k: {
                        "model": v.get("model", ""),
                        "username": v.get("username", k),
                        "icon_url": v.get("icon_url", ""),
                        "is_short_output": bool(v.get("is_short_output", False)),
                        "skills": list(v.get("skills", [])),
                    }
                    for k, v in self.agents_config.items()
                },
                "system_agents": {
                    k: dict(v)
                    for k, v in self.system_agents_config.items()
                },
            }
            self._atomic_write_json(target_file, data)
            self.is_running_from_backup = False
            self.backup_error_reason = None
            logger.info(
                f"[SETTINGS] Saved settings ({len(self.enabled_agents)} enabled agents, "
                f"mode='{self.orchestration_mode}', "
                f"circuit_breaker={self.circuit_breaker_enabled}, "
                f"threshold=${self.circuit_breaker_threshold_usd:.2f}) to {target_file.name}"
            )
            self.check_and_update_backup()
        except Exception as e:
            logger.error(f"[SETTINGS] Error saving agent settings: {e}")

    def get_orchestration_mode(self) -> str:
        return self.orchestration_mode

    def set_orchestration_mode(
        self, mode: str | OrchestrationMode, save: bool = True
    ) -> None:
        mode_val = mode.value if isinstance(mode, OrchestrationMode) else str(mode)
        mode_clean = mode_val.strip().lower()
        if mode_clean in (OrchestrationMode.SANDWICH.value, OrchestrationMode.FIXED.value):
            self.orchestration_mode = mode_clean
            if save:
                self.save()
            logger.info(f"[SETTINGS] Orchestration mode set to '{self.orchestration_mode}'.")

    def get_sandwich_config(self) -> dict[str, list[str]]:
        return {
            "start_chain": list(self.sandwich_start_chain),
            "end_chain": list(self.sandwich_end_chain),
        }

    def set_sandwich_config(
        self, start_chain: list[str], end_chain: list[str], save: bool = True
    ) -> None:
        self.sandwich_start_chain = [
            k for k in start_chain if isinstance(k, str) and k in self.agents_config
        ]
        self.sandwich_end_chain = [
            k for k in end_chain if isinstance(k, str) and k in self.agents_config
        ]
        if save:
            self.save()
        logger.info(
            f"[SETTINGS] Updated sandwich config: start_chain={self.sandwich_start_chain}, end_chain={self.sandwich_end_chain}"
        )

    def get_fixed_config(self) -> dict[str, list[str]]:
        return {
            "chain": list(self.fixed_chain),
        }

    def set_fixed_config(self, chain: list[str], save: bool = True) -> None:
        self.fixed_chain = [
            k for k in chain if isinstance(k, str) and k in self.agents_config
        ]
        if save:
            self.save()
        logger.info(f"[SETTINGS] Updated fixed config: chain={self.fixed_chain}")

    def is_circuit_breaker_enabled(self) -> bool:
        return self.circuit_breaker_enabled

    def set_circuit_breaker_enabled(self, enabled: bool, save: bool = True) -> None:
        self.circuit_breaker_enabled = bool(enabled)
        if save:
            self.save()
        logger.info(f"[SETTINGS] Circuit-breaker enabled set to {self.circuit_breaker_enabled}.")

    def get_circuit_breaker_threshold(self) -> float:
        return float(self.circuit_breaker_threshold_usd)

    def set_circuit_breaker_threshold(self, threshold: float, save: bool = True) -> None:
        try:
            val = float(threshold)
            if val < 0.0:
                val = 0.0
            self.circuit_breaker_threshold_usd = val
            if save:
                self.save()
            logger.info(f"[SETTINGS] Circuit-breaker threshold set to ${self.circuit_breaker_threshold_usd:.2f} USD.")
        except (ValueError, TypeError) as e:
            logger.error(f"[SETTINGS] Invalid circuit-breaker threshold '{threshold}': {e}")

    def get_circuit_breaker_config(self) -> dict[str, Any]:
        return CircuitBreakerConfig(
            enabled=self.circuit_breaker_enabled,
            threshold_usd=self.circuit_breaker_threshold_usd,
        ).to_dict()

    def reset(self, save: bool = True) -> None:
        """Resets all registered agents to enabled state and resets orchestration mode to sandwich default."""
        self.enabled_agents.clear()
        self.enabled_agents.update(self.agents_config.keys())
        self.orchestration_mode = OrchestrationMode.SANDWICH.value
        self.sandwich_start_chain = []
        self.sandwich_end_chain = []
        self.fixed_chain = []
        if save:
            self.save()
        logger.info(f"[SETTINGS] Reset all {len(self.enabled_agents)} agents to ENABLED and mode to default sandwich.")

    def get_enabled_agents(self) -> set[str]:
        return set(self.enabled_agents)

    def set_agent_enabled(self, agent_key: str, enabled: bool) -> bool:
        if agent_key not in self.agents_config:
            logger.warning(f"[SETTINGS] Unknown agent key '{agent_key}'.")
            return False

        if enabled:
            self.enabled_agents.add(agent_key)
        else:
            self.enabled_agents.discard(agent_key)
        self.save()
        logger.info(f"[SETTINGS] Set agent '{agent_key}' enabled={enabled}. Total enabled: {len(self.enabled_agents)}")
        return True

    def toggle_agent(self, agent_key: str) -> bool:
        current_state = agent_key in self.enabled_agents
        new_state = not current_state
        self.set_agent_enabled(agent_key, new_state)
        return agent_key in self.enabled_agents

    def get_agent_skills(self, agent_key: str) -> list[str]:
        """Returns the list of equipped skills for an agent."""
        cfg = self.agents_config.get(agent_key)
        if not cfg:
            return []
        return list(cfg.get("skills", []))

    def set_agent_skills(self, agent_key: str, skills: list[str], save: bool = True) -> bool:
        """Sets the equipped skills for an agent and re-builds its system prompt."""
        if agent_key not in self.agents_config:
            logger.warning(f"[SETTINGS] Cannot set skills for unknown agent '{agent_key}'")
            return False

        sanitized_skills = [
            s.strip() for s in skills if isinstance(s, str) and s.strip()
        ]
        self.agents_config[agent_key]["skills"] = sanitized_skills
        self.agents_config[agent_key]["system_prompt"] = build_agent_prompt(agent_key, skills=sanitized_skills)
        if save:
            self.save()
        logger.info(f"[SETTINGS] Updated skills for '{agent_key}': {sanitized_skills}")
        return True

    def set_agent_model(self, agent_key: str, model: str, save: bool = True) -> bool:
        """Sets the LLM model for a panel agent and persists to settings JSON."""
        if agent_key not in self.agents_config:
            logger.warning(f"[SETTINGS] Cannot set model for unknown agent '{agent_key}'")
            return False

        clean_model = str(model).strip()
        if not clean_model:
            logger.warning(f"[SETTINGS] Invalid empty model for agent '{agent_key}'")
            return False

        self.agents_config[agent_key]["model"] = clean_model
        if save:
            self.save()
        logger.info(f"[SETTINGS] Updated model for '{agent_key}': {clean_model}")
        return True

    def set_system_agent_model(self, sys_agent_key: str, model: str, save: bool = True) -> bool:
        """Sets the LLM model for a system agent and persists to settings JSON."""
        if sys_agent_key not in self.system_agents_config:
            logger.warning(f"[SETTINGS] Cannot set model for unknown system agent '{sys_agent_key}'")
            return False

        clean_model = str(model).strip()
        if not clean_model:
            return False

        self.system_agents_config[sys_agent_key]["model"] = clean_model
        if save:
            self.save()
        logger.info(f"[SETTINGS] Updated system agent model for '{sys_agent_key}': {clean_model}")
        return True


# Default singleton instance initialized automatically
settings_manager = AgentSettingsManager()

# Module-level aliases
ENABLED_DEBATE_AGENTS = settings_manager.enabled_agents
CIRCUIT_BREAKER_ENABLED = settings_manager.circuit_breaker_enabled
CIRCUIT_BREAKER_THRESHOLD_USD = settings_manager.circuit_breaker_threshold_usd


def get_orchestration_mode() -> str:
    return settings_manager.get_orchestration_mode()


def set_orchestration_mode(mode: str | OrchestrationMode, save: bool = True) -> None:
    settings_manager.set_orchestration_mode(mode, save=save)


def get_sandwich_config() -> dict[str, list[str]]:
    return settings_manager.get_sandwich_config()


def set_sandwich_config(start_chain: list[str], end_chain: list[str], save: bool = True) -> None:
    settings_manager.set_sandwich_config(start_chain, end_chain, save=save)


def get_fixed_config() -> dict[str, list[str]]:
    return settings_manager.get_fixed_config()


def set_fixed_config(chain: list[str], save: bool = True) -> None:
    settings_manager.set_fixed_config(chain, save=save)


def is_running_from_backup() -> bool:
    return settings_manager.is_running_from_backup


def get_backup_error_reason() -> str | None:
    return settings_manager.backup_error_reason


def restore_from_backup() -> bool:
    return settings_manager.restore_from_backup()


def is_circuit_breaker_enabled() -> bool:
    return settings_manager.is_circuit_breaker_enabled()


def set_circuit_breaker_enabled(enabled: bool, save: bool = True) -> None:
    settings_manager.set_circuit_breaker_enabled(enabled, save=save)


def get_circuit_breaker_threshold() -> float:
    return settings_manager.get_circuit_breaker_threshold()


def set_circuit_breaker_threshold(threshold: float, save: bool = True) -> None:
    settings_manager.set_circuit_breaker_threshold(threshold, save=save)


def get_circuit_breaker_config() -> dict[str, Any]:
    return settings_manager.get_circuit_breaker_config()


def save_enabled_agents() -> None:
    settings_manager.save()


def load_enabled_agents() -> set[str]:
    return settings_manager.load()


def reset_enabled_agents(save: bool = True) -> None:
    settings_manager.reset(save=save)


def get_enabled_agents() -> set[str]:
    return settings_manager.get_enabled_agents()


def set_agent_enabled(agent_key: str, enabled: bool) -> bool:
    return settings_manager.set_agent_enabled(agent_key, enabled)


def toggle_agent(agent_key: str) -> bool:
    return settings_manager.toggle_agent(agent_key)


def get_agent_skills(agent_key: str) -> list[str]:
    return settings_manager.get_agent_skills(agent_key)


def set_agent_skills(agent_key: str, skills: list[str], save: bool = True) -> bool:
    return settings_manager.set_agent_skills(agent_key, skills, save=save)


def set_agent_model(agent_key: str, model: str, save: bool = True) -> bool:
    return settings_manager.set_agent_model(agent_key, model, save=save)


def set_system_agent_model(sys_agent_key: str, model: str, save: bool = True) -> bool:
    return settings_manager.set_system_agent_model(sys_agent_key, model, save=save)

