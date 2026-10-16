import copy
import json
import logging
import os
import sys
import re
import threading
import time
from pathlib import Path
from typing import Any

from core.config import (
    AGENTS_CONFIG,
    ORCHESTRATOR_CONFIG,
    SYSTEM_AGENTS_CONFIG,
)
from core.models import OrchestrationMode
from orchestration.app_settings import AppSettingsManager, app_settings_manager
from orchestration.system_settings import SystemSettingsManager, system_settings_manager
from prompts import build_agent_prompt
from core.integrity_checks import AppIntegrityValidator

from core.paths import (
    AGENT_BACKUP_FILE as BACKUP_FILE,
    AGENT_SETTINGS_FILE as SETTINGS_FILE,
    PERSONAS_DIR,
)

logger = logging.getLogger(__name__)

# Mandatory locked agents (empty by default now that lock system is removed)
LOCKED_AGENTS: set[str] = set()

# 7-day interval in seconds (604,800 seconds)
BACKUP_INTERVAL_SECONDS = 7 * 86400


# Default untagged workflow configuration: Sandwich mode
DEFAULT_WORKFLOW_ID = "default_review_flow"
DEFAULT_SANDWICH_START_CHAIN = ["agent-10001", "agent-10002"]
DEFAULT_SANDWICH_END_CHAIN = ["agent-10008"]


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
        workflows_dir: Path | None = None,
    ):
        self.default_settings_file = settings_file or SETTINGS_FILE
        self._custom_settings_file = settings_file is not None
        self._custom_backup_file = backup_file is not None
        self.default_backup_file = backup_file or BACKUP_FILE
        self.locked_agents = set(locked_agents or set())
        self.backup_interval_seconds = backup_interval_seconds
        self._save_lock = threading.RLock()

        from orchestration.workflow_manager import WorkflowManager, workflow_manager
        if workflows_dir is not None:
            self.workflow_manager = WorkflowManager(workflows_dir=workflows_dir, auto_seed=True)
        else:
            self.workflow_manager = workflow_manager

        if settings_file is not None or backup_file is not None:
            # Isolated dictionaries for custom instances (e.g. tests)
            self._agents_config = {}
            self._system_agents_config = {}
            self._orchestrator_config = {}
            target_dir = Path(settings_file).parent if settings_file is not None else self.default_settings_file.parent
            app_set_file = target_dir / "app_settings.json"
            sys_set_file = target_dir / "system_settings.json"
            self.app_settings = AppSettingsManager(settings_file=app_set_file, auto_load=True)
            self.system_settings = SystemSettingsManager(settings_file=sys_set_file, auto_load=True)
            self._orchestrator_config.update({
                "fast_model": self.system_settings.get_default_fast_model(),
                "reasoning_model": self.system_settings.get_default_reasoning_model(),
                "username": "Orchestrator",
                "icon_url": "static/image_99.png",
            })
        else:
            # Global default instance bound to core.config
            self._agents_config = AGENTS_CONFIG
            self._system_agents_config = SYSTEM_AGENTS_CONFIG
            self._orchestrator_config = ORCHESTRATOR_CONFIG
            self.app_settings = app_settings_manager
            self.system_settings = system_settings_manager
            self._orchestrator_config.clear()
            self._orchestrator_config.update({
                "fast_model": self.system_settings.get_default_fast_model(),
                "reasoning_model": self.system_settings.get_default_reasoning_model(),
                "username": "Orchestrator",
                "icon_url": "static/image_99.png",
            })
        self.enabled_agents: set[str] = set()

        # Orchestration flow mode and chains
        self.orchestration_mode: str = OrchestrationMode.SANDWICH.value
        self.sandwich_start_chain: list[str] = list(DEFAULT_SANDWICH_START_CHAIN)
        self.sandwich_end_chain: list[str] = list(DEFAULT_SANDWICH_END_CHAIN)
        self.fixed_chain: list[str] = []
        self.workflow_config: str | None = DEFAULT_WORKFLOW_ID
        self.active_workflow_id: str | None = DEFAULT_WORKFLOW_ID

        # Emergency backup status
        self.is_running_from_backup: bool = False
        self.backup_error_reason: str | None = None
        self._last_loaded_mtime: float = 0.0
        self._last_workflow_mtime: float = 0.0
        self._raw_data: dict[str, Any] = {}

        if auto_load:
            self.load()

    @property
    def agents_config(self) -> dict[str, Any]:
        return self._agents_config

    @agents_config.setter
    def agents_config(self, val: dict[str, Any]) -> None:
        if hasattr(self, "_agents_config") and isinstance(self._agents_config, dict):
            self._agents_config.clear()
            if isinstance(val, dict):
                self._agents_config.update(val)
        else:
            self._agents_config = dict(val) if isinstance(val, dict) else {}

    @property
    def system_agents_config(self) -> dict[str, Any]:
        return self._system_agents_config

    @system_agents_config.setter
    def system_agents_config(self, val: dict[str, Any]) -> None:
        if hasattr(self, "_system_agents_config") and isinstance(self._system_agents_config, dict):
            self._system_agents_config.clear()
            if isinstance(val, dict):
                self._system_agents_config.update(val)
        else:
            self._system_agents_config = dict(val) if isinstance(val, dict) else {}

    @property
    def orchestrator_config(self) -> dict[str, Any]:
        return self._orchestrator_config

    @orchestrator_config.setter
    def orchestrator_config(self, val: dict[str, Any]) -> None:
        if hasattr(self, "_orchestrator_config") and isinstance(self._orchestrator_config, dict):
            self._orchestrator_config.clear()
            if isinstance(val, dict):
                self._orchestrator_config.update(val)
        else:
            self._orchestrator_config = dict(val) if isinstance(val, dict) else {}

    @property
    def circuit_breaker_enabled(self) -> bool:
        return self.app_settings.is_circuit_breaker_enabled()

    @circuit_breaker_enabled.setter
    def circuit_breaker_enabled(self, val: bool) -> None:
        self.app_settings.set_circuit_breaker_enabled(val, save=False)

    @property
    def circuit_breaker_threshold_usd(self) -> float:
        return self.app_settings.get_circuit_breaker_threshold()

    @circuit_breaker_threshold_usd.setter
    def circuit_breaker_threshold_usd(self, val: float) -> None:
        self.app_settings.set_circuit_breaker_threshold(val, save=False)

    @property
    def scribe_enabled(self) -> bool:
        return self.app_settings.is_scribe_enabled()

    @scribe_enabled.setter
    def scribe_enabled(self, val: bool) -> None:
        self.app_settings.set_scribe_enabled(val, save=False)

    @property
    def weekly_pricing_update_enabled(self) -> bool:
        return self.app_settings.is_weekly_pricing_update_enabled()

    @weekly_pricing_update_enabled.setter
    def weekly_pricing_update_enabled(self, val: bool) -> None:
        self.app_settings.set_weekly_pricing_update_enabled(val, save=False)

    @property
    def active_settings_file(self) -> Path:
        if self._custom_settings_file:
            return self.default_settings_file
        router_mod = sys.modules.get("agent_router") or sys.modules.get("orchestration.agent_router")
        if router_mod and hasattr(router_mod, "SETTINGS_FILE"):
            return Path(router_mod.SETTINGS_FILE)
        agent_settings_mod = sys.modules.get("agent_settings") or sys.modules.get("orchestration.agent_settings")
        if agent_settings_mod and hasattr(agent_settings_mod, "SETTINGS_FILE"):
            return Path(agent_settings_mod.SETTINGS_FILE)
        return self.default_settings_file

    @property
    def active_backup_file(self) -> Path:
        if self._custom_backup_file:
            return self.default_backup_file
        router_mod = sys.modules.get("agent_router") or sys.modules.get("orchestration.agent_router")
        if router_mod and hasattr(router_mod, "BACKUP_FILE"):
            return Path(router_mod.BACKUP_FILE)
        agent_settings_mod = sys.modules.get("agent_settings") or sys.modules.get("orchestration.agent_settings")
        if agent_settings_mod and hasattr(agent_settings_mod, "BACKUP_FILE"):
            return Path(agent_settings_mod.BACKUP_FILE)
        return self.default_backup_file

    def _atomic_write_json(self, target_path: Path, data: dict[str, Any]) -> None:
        """Writes JSON payload atomically to a temporary file before renaming."""
        target_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_filename = f"{target_path.name}.tmp_{os.getpid()}_{int(time.time() * 1000)}"
        tmp_path = target_path.parent / tmp_filename
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            for attempt in range(10):
                try:
                    os.replace(tmp_path, target_path)
                    break
                except (PermissionError, OSError) as pe:
                    if attempt < 9:
                        time.sleep(0.02 * (attempt + 1))
                    else:
                        raise pe
        except Exception:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError as e:
                    logger.debug(f"[SETTINGS] Could not remove temp file {tmp_path}: {e}")
            raise

    def _validate_agent_entry(self, key: str, config: dict[str, Any]) -> bool:
        """Validates that an agent has required parameters and a matching persona file."""
        if not isinstance(config, dict):
            logger.warning(f"[SETTINGS VALIDATION] Agent '{key}' configuration is not a dictionary. Skipping.")
            return False

        model = config.get("model")
        username = config.get("username")
        displayname = config.get("displayname")
        icon_url = config.get("icon_url")

        if not model or not isinstance(model, str) or not model.strip():
            logger.warning(f"[SETTINGS VALIDATION] Agent '{key}' is missing valid 'model'. Skipping.")
            return False

        if not username or not isinstance(username, str) or not username.strip():
            logger.warning(f"[SETTINGS VALIDATION] Agent '{key}' is missing valid 'username'. Skipping.")
            return False

        if not displayname or not isinstance(displayname, str) or not displayname.strip():
            logger.warning(f"[SETTINGS VALIDATION] Agent '{key}' is missing valid 'displayname'. Skipping.")
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

    def _load_workflow_details(self, wf_ref: str | None, fallback_data: dict[str, Any] | None = None) -> None:
        """
        Loads orchestration mode and chains from the referenced workflow preset file.
        If the file is missing or invalid, falls back to default_sandwich, or to fallback_data if provided.
        """
        from orchestration.workflow_manager import slugify

        preset = None
        clean_ref = slugify(wf_ref) if wf_ref else None

        if clean_ref:
            try:
                preset = self.workflow_manager.get_workflow(clean_ref)
            except (OSError, json.JSONDecodeError, ValueError, KeyError) as e:
                logger.warning(f"[SETTINGS] Error retrieving workflow '{clean_ref}': {e}")
            if not preset:
                logger.warning(
                    f"[SETTINGS] Referenced workflow '{clean_ref}' not found in {self.workflow_manager.workflows_dir}. "
                    "Attempting fallback to default preset."
                )
                try:
                    preset = self.workflow_manager.get_workflow("default_review_flow") or self.workflow_manager.get_workflow("default_sandwich")
                except (OSError, json.JSONDecodeError, ValueError, KeyError) as e:
                    logger.warning(f"[SETTINGS] Error loading default_review_flow fallback: {e}")

        # If still not found, check if fallback_data contains legacy embedded chains (backward compatibility)
        if not preset and fallback_data and (fallback_data.get("sandwich_config") or fallback_data.get("fixed_config")):
            logger.info("[SETTINGS] Using legacy embedded workflow configuration from payload.")
            raw_mode = str(fallback_data.get("orchestration_mode", OrchestrationMode.SANDWICH.value)).strip().lower()
            self.orchestration_mode = (
                raw_mode
                if raw_mode in (OrchestrationMode.SANDWICH.value, OrchestrationMode.FIXED.value)
                else OrchestrationMode.SANDWICH.value
            )
            raw_sandwich = fallback_data.get("sandwich_config", {})
            if isinstance(raw_sandwich, dict):
                raw_start = raw_sandwich.get("start_chain", [])
                raw_end = raw_sandwich.get("end_chain", [])
                self.sandwich_start_chain = [
                    k for k in raw_start if isinstance(k, str) and (not self.agents_config or k in self.agents_config)
                ] if isinstance(raw_start, list) else []
                self.sandwich_end_chain = [
                    k for k in raw_end if isinstance(k, str) and (not self.agents_config or k in self.agents_config)
                ] if isinstance(raw_end, list) else []
            else:
                self.sandwich_start_chain = []
                self.sandwich_end_chain = []

            raw_fixed = fallback_data.get("fixed_config", {})
            if isinstance(raw_fixed, dict):
                raw_chain = raw_fixed.get("chain", [])
                self.fixed_chain = [
                    k for k in raw_chain if isinstance(k, str) and (not self.agents_config or k in self.agents_config)
                ] if isinstance(raw_chain, list) else []
            else:
                self.fixed_chain = []
            return

        if preset:
            self.orchestration_mode = preset.mode
            mode_normalized = str(preset.mode or "").strip().lower()
            if mode_normalized == OrchestrationMode.FIXED.value:
                raw_chain = preset.fixed_config.get("chain", [])
                self.fixed_chain = [
                    k for k in raw_chain if isinstance(k, str) and (not self.agents_config or k in self.agents_config)
                ] if isinstance(raw_chain, list) else []
            else:
                raw_start = preset.sandwich_config.get("start_chain", [])
                raw_end = preset.sandwich_config.get("end_chain", [])
                self.sandwich_start_chain = [
                    k for k in raw_start if isinstance(k, str) and (not self.agents_config or k in self.agents_config)
                ] if isinstance(raw_start, list) else []
                self.sandwich_end_chain = [
                    k for k in raw_end if isinstance(k, str) and (not self.agents_config or k in self.agents_config)
                ] if isinstance(raw_end, list) else []

            self.active_workflow_id = preset.id
            self.workflow_config = preset.id

            # Track workflow file mtime
            wf_file = self.workflow_manager.workflows_dir / f"{preset.id}.json"
            if wf_file.exists():
                try:
                    self._last_workflow_mtime = wf_file.stat().st_mtime
                except OSError as e:
                    logger.debug(f"[SETTINGS] Error checking workflow mtime: {e}")
        else:
            # Complete fallback default when no preset is active (Visionary -> Innovator -> ... -> Security Architect)
            self.active_workflow_id = DEFAULT_WORKFLOW_ID
            self.workflow_config = DEFAULT_WORKFLOW_ID
            self.orchestration_mode = OrchestrationMode.SANDWICH.value
            self.sandwich_start_chain = list(DEFAULT_SANDWICH_START_CHAIN)
            self.sandwich_end_chain = list(DEFAULT_SANDWICH_END_CHAIN)
            self.fixed_chain = []

    def persist_active_workflow(self) -> None:
        """Persists current mode and chains to the active workflow JSON file."""
        from orchestration.workflow_manager import slugify

        wf_id = self.workflow_config or self.active_workflow_id
        if not wf_id:
            return
        clean_id = slugify(wf_id)
        existing = self.workflow_manager.get_workflow(clean_id)
        name = existing.name if existing else clean_id.replace("_", " ").title()
        desc = existing.description if existing else ""

        mode_normalized = str(self.orchestration_mode or "").strip().lower()
        if mode_normalized == OrchestrationMode.FIXED.value:
            sandwich_cfg = None
            fixed_cfg = {"chain": list(self.fixed_chain)}
        else:
            sandwich_cfg = {
                "start_chain": list(self.sandwich_start_chain),
                "end_chain": list(self.sandwich_end_chain),
            }
            fixed_cfg = None

        saved = self.workflow_manager.save_workflow(
            name=name,
            mode=self.orchestration_mode,
            sandwich_config=sandwich_cfg,
            fixed_config=fixed_cfg,
            description=desc,
            workflow_id=clean_id,
        )
        self.workflow_config = saved.id
        self.active_workflow_id = saved.id
        wf_file = self.workflow_manager.workflows_dir / f"{saved.id}.json"
        if wf_file.exists():
            try:
                self._last_workflow_mtime = wf_file.stat().st_mtime
            except OSError as e:
                logger.debug(f"[SETTINGS] Error checking workflow mtime: {e}")

    _persist_active_workflow = persist_active_workflow

    def _parse_and_validate_payload(self, data: dict[str, Any], source_name: str) -> bool:
        """Parses and validates a full agent settings payload from JSON."""
        if not isinstance(data, dict):
            logger.error(f"[SETTINGS] Payload from {source_name} is not a valid JSON object.")
            return False

        self._raw_data = copy.deepcopy(data)
        data = AppIntegrityValidator.run_all_checks(data, personas_dir=PERSONAS_DIR)
        
        raw_agents = data.get("agents", {})
        if not isinstance(raw_agents, dict):
            logger.error(f"[SETTINGS] 'agents' key in {source_name} is not a dictionary after validation.")
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
                    "username": cfg.get("username", key),
                    "model": cfg["model"].strip(),
                    "displayname": cfg["displayname"].strip(),
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

        # Parse orchestrator config
        raw_orch = data.get("orchestrator", {})
        default_fast = self.system_settings.get_default_fast_model()
        default_reasoning = self.system_settings.get_default_reasoning_model()
        self.orchestrator_config.clear()
        if isinstance(raw_orch, dict) and raw_orch:
            self.orchestrator_config.update({
                "fast_model": str(raw_orch.get("fast_model", default_fast)).strip(),
                "reasoning_model": str(raw_orch.get("reasoning_model", default_reasoning)).strip(),
                "username": str(raw_orch.get("username", "Orchestrator")).strip(),
                "displayname": str(raw_orch.get("displayname", "Orchestrator")).strip(),
                "icon_url": str(raw_orch.get("icon_url", "static/image_99.png")).strip(),
            })
        else:
            self.orchestrator_config.update({
                "fast_model": default_fast,
                "reasoning_model": default_reasoning,
                "username": "Orchestrator",
                "icon_url": "static/image_99.png",
            })

        # Parse system agents (legacy or secondary entries)
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
                    if sys_v.get("displayname"):
                        sys_entry["displayname"] = str(sys_v["displayname"]).strip()
                    if sys_v.get("icon_url"):
                        sys_entry["icon_url"] = str(sys_v["icon_url"]).strip()
                    if sys_v.get("agent_key"):
                        sys_entry["agent_key"] = str(sys_v["agent_key"]).strip()
                    if "is_system_agent" in sys_v:
                        sys_entry["is_system_agent"] = bool(sys_v["is_system_agent"])
                    if sys_entry:
                        self.system_agents_config[sys_k] = sys_entry

        # Parse circuit breaker settings (backward-compatibility fallback from agent_settings.json)
        if "circuit_breaker_enabled" in data:
            self.set_circuit_breaker_enabled(bool(data.get("circuit_breaker_enabled", False)), save=False)
            if "circuit_breaker_threshold_usd" in data:
                try:
                    self.set_circuit_breaker_threshold(float(data.get("circuit_breaker_threshold_usd", 1.0)), save=False)
                except (ValueError, TypeError):
                    self.set_circuit_breaker_threshold(1.0, save=False)

        # Parse scribe enabled setting (backward-compatibility fallback)
        if "scribe_enabled" in data:
            self.set_scribe_enabled(bool(data.get("scribe_enabled", True)), save=False)

        # Parse active workflow reference ("workflow_config" primary, "active_workflow_id" fallback)
        raw_wf = data.get("workflow_config") or data.get("active_workflow_id")
        if isinstance(raw_wf, str):
            clean_wf = raw_wf.strip()
            clean_wf = clean_wf.removesuffix(".json")
            self.workflow_config = clean_wf
            self.active_workflow_id = clean_wf
        else:
            self.workflow_config = None
            self.active_workflow_id = None

        # Load details (mode, sandwich chains, fixed chain) from the referenced workflow file
        self._load_workflow_details(self.workflow_config, fallback_data=data)

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
            f"workflow_config='{self.workflow_config}', mode='{self.orchestration_mode}', "
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
            except OSError as e:
                logger.debug(f"[SETTINGS] Error checking backup mtime: {e}")
                needs_backup = True

        if needs_backup:
            try:
                with open(target_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._atomic_write_json(backup_file, data)
                logger.info(f"[SETTINGS] Updated 7-day emergency backup: {backup_file.name}")
                return True
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
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
                    raw_text = f.read()
                AppIntegrityValidator.check_duplicate_ids_in_raw_json(raw_text)
                data = json.loads(raw_text)
                if self._parse_and_validate_payload(data, target_file.name):
                    self.is_running_from_backup = False
                    self.backup_error_reason = None
                    try:
                        self._last_loaded_mtime = target_file.stat().st_mtime
                    except OSError as e:
                        logger.debug(f"[SETTINGS] Error checking settings file mtime: {e}")
                    self.check_and_update_backup()
                    return set(self.enabled_agents)
                else:
                    err_reason = f"Schema validation failed in {target_file.name}"
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
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
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as backup_err:
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
        with self._save_lock:
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
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
                logger.error(f"[SETTINGS] Error executing restore from backup: {e}")
                return False

    def save(self) -> None:
        """
        Saves current enabled debate agents, agent configurations, circuit breaker,
        and workflow reference to active JSON.
        Does NOT embed workflow details (chains), keeping workflow files as SSOT.
        Losslessly preserves unrecognized keys and agent attributes.
        Resets emergency backup mode once active file is successfully saved.
        """
        wf_ref = self.workflow_config or self.active_workflow_id
        data = copy.deepcopy(self._raw_data) if hasattr(self, "_raw_data") and isinstance(self._raw_data, dict) else {}
        data["workflow_config"] = wf_ref
        data["enabled_agents"] = sorted(self.enabled_agents)

        # Losslessly update agents
        if "agents" not in data or not isinstance(data["agents"], dict):
            data["agents"] = {}
        for k, v in self.agents_config.items():
            existing_agent = data["agents"].get(k, {}) if isinstance(data["agents"].get(k), dict) else {}
            existing_agent.update({
                "username": v.get("username", k),
                "model": v.get("model", ""),
                "displayname": v.get("displayname", k),
                "icon_url": v.get("icon_url", ""),
                "is_short_output": bool(v.get("is_short_output", False)),
                "skills": list(v.get("skills", [])),
            })
            data["agents"][k] = existing_agent
        for k in list(data["agents"].keys()):
            if k not in self.agents_config:
                del data["agents"][k]

        # Losslessly update system_agents
        if "system_agents" not in data or not isinstance(data["system_agents"], dict):
            data["system_agents"] = {}
        for k, v in self.system_agents_config.items():
            existing_sys = data["system_agents"].get(k, {}) if isinstance(data["system_agents"].get(k), dict) else {}
            existing_sys.update({
                "username": v.get("username", k),
                "model": v.get("model", ""),
                "displayname": v.get("displayname", k),
                "icon_url": v.get("icon_url", ""),
                "is_short_output": bool(v.get("is_short_output", False)),
                "skills": list(v.get("skills", [])),
            })
            data["system_agents"][k] = existing_sys
        for k in list(data["system_agents"].keys()):
            if k not in self.system_agents_config:
                del data["system_agents"][k]

        # Losslessly update orchestrator
        if "orchestrator" not in data or not isinstance(data["orchestrator"], dict):
            data["orchestrator"] = {}
        data["orchestrator"].update({
            "fast_model": self.get_orchestrator_fast_model(),
            "reasoning_model": self.get_orchestrator_reasoning_model(),
            "username": self.orchestrator_config.get("username", "Orchestrator"),
            "displayname": self.orchestrator_config.get("displayname", "Orchestrator"),
            "icon_url": self.orchestrator_config.get("icon_url", "static/image_99.png"),
        })

        self._raw_data = copy.deepcopy(data)

        def do_save():
            with self._save_lock:
                target_file = self.active_settings_file
                try:
                    self._atomic_write_json(target_file, data)
                    self.is_running_from_backup = False
                    self.backup_error_reason = None
                    try:
                        self._last_loaded_mtime = target_file.stat().st_mtime
                    except OSError as e:
                        logger.debug(f"[SETTINGS] Error checking settings file mtime: {e}")
                    logger.info(
                        f"[SETTINGS] Saved settings ({len(self.enabled_agents)} enabled agents, "
                        f"workflow_config='{wf_ref}', "
                        f"circuit_breaker={self.circuit_breaker_enabled}, "
                        f"threshold=${self.circuit_breaker_threshold_usd:.2f}) to {target_file.name}"
                    )
                    self.check_and_update_backup()
                except (OSError, TypeError, ValueError) as e:
                    logger.error(f"[SETTINGS] Error saving agent settings: {e}")

        do_save()

    def sync_if_modified(self) -> bool:
        """
        Reloads configuration from active settings file or active workflow file
        if either was modified on disk since the last load/save.
        """
        target_file = self.active_settings_file
        if target_file.exists():
            try:
                mtime = target_file.stat().st_mtime
                last_mtime = getattr(self, "_last_loaded_mtime", 0.0)
                if mtime > last_mtime:
                    self.load()
                    return True
            except (OSError, RuntimeError) as e:
                logger.debug(f"[SETTINGS] Error checking settings file mtime: {e}")

        # Also check if the active workflow file changed on disk
        wf_id = self.workflow_config or self.active_workflow_id
        if wf_id:
            from orchestration.workflow_manager import slugify
            wf_file = self.workflow_manager.workflows_dir / f"{slugify(wf_id)}.json"
            if wf_file.exists():
                try:
                    wf_mtime = wf_file.stat().st_mtime
                    last_wf_mtime = getattr(self, "_last_workflow_mtime", 0.0)
                    if wf_mtime > last_wf_mtime:
                        self.load()
                        return True
                except (OSError, RuntimeError) as e:
                    logger.debug(f"[SETTINGS] Error checking workflow file mtime: {e}")
        return False

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
            k for k in start_chain if isinstance(k, str) and (not self.agents_config or k in self.agents_config)
        ]
        self.sandwich_end_chain = [
            k for k in end_chain if isinstance(k, str) and (not self.agents_config or k in self.agents_config)
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
            k for k in chain if isinstance(k, str) and (not self.agents_config or k in self.agents_config)
        ]
        if save:
            self.save()
        logger.info(f"[SETTINGS] Updated fixed config: chain={self.fixed_chain}")

    def get_workflow_config(self) -> str | None:
        return self.workflow_config or self.active_workflow_id

    def set_workflow_config(self, workflow_config: str | None, save: bool = True) -> None:
        if workflow_config and workflow_config.endswith(".json"):
            workflow_config = workflow_config[:-5]
        self.workflow_config = workflow_config
        self.active_workflow_id = workflow_config
        self._load_workflow_details(workflow_config)
        if save:
            self.save()
        logger.info(f"[SETTINGS] Workflow config set to: {self.workflow_config}")

    def get_active_workflow_id(self) -> str | None:
        return self.get_workflow_config()

    def set_active_workflow_id(self, workflow_id: str | None, save: bool = True) -> None:
        self.set_workflow_config(workflow_id, save=save)

    def apply_workflow(self, workflow_id: str, save: bool = True) -> bool:
        """
        Applies a workflow preset to the active orchestration configuration.
        Updates orchestration_mode and corresponding chain(s), and updates workflow_config.
        """
        preset = self.workflow_manager.get_workflow(workflow_id)
        if not preset:
            logger.warning(f"[SETTINGS] Cannot apply unknown workflow preset '{workflow_id}'")
            return False

        self.orchestration_mode = preset.mode
        mode_normalized = str(preset.mode or "").strip().lower()
        if mode_normalized == OrchestrationMode.FIXED.value:
            raw_chain = preset.fixed_config.get("chain", [])
            self.fixed_chain = [
                k for k in raw_chain if isinstance(k, str) and (not self.agents_config or k in self.agents_config)
            ]
        else:
            raw_start = preset.sandwich_config.get("start_chain", [])
            raw_end = preset.sandwich_config.get("end_chain", [])
            self.sandwich_start_chain = [
                k for k in raw_start if isinstance(k, str) and (not self.agents_config or k in self.agents_config)
            ]
            self.sandwich_end_chain = [
                k for k in raw_end if isinstance(k, str) and (not self.agents_config or k in self.agents_config)
            ]

        self.workflow_config = preset.id
        self.active_workflow_id = preset.id

        wf_file = self.workflow_manager.workflows_dir / f"{preset.id}.json"
        if wf_file.exists():
            try:
                self._last_workflow_mtime = wf_file.stat().st_mtime
            except OSError as e:
                logger.debug(f"[SETTINGS] Error checking workflow file mtime: {e}")

        if save:
            self.save()
        logger.info(f"[SETTINGS] Successfully applied workflow preset '{preset.name}' ({preset.id})")
        return True

    def is_circuit_breaker_enabled(self) -> bool:
        return self.app_settings.is_circuit_breaker_enabled()

    def set_circuit_breaker_enabled(
        self, enabled: bool, save: bool = True, reset_threshold_on_disable: bool = True
    ) -> None:
        self.app_settings.set_circuit_breaker_enabled(enabled, save=save)
        if not enabled and reset_threshold_on_disable:
            self.app_settings.set_circuit_breaker_threshold(0.0, save=save)

    def get_circuit_breaker_threshold(self) -> float:
        return self.app_settings.get_circuit_breaker_threshold()

    def set_circuit_breaker_threshold(self, threshold: float, save: bool = True) -> None:
        self.app_settings.set_circuit_breaker_threshold(threshold, save=save)

    def get_circuit_breaker_config(self) -> dict[str, Any]:
        return self.app_settings.get_circuit_breaker_config().to_dict()

    def is_scribe_enabled(self) -> bool:
        return self.app_settings.is_scribe_enabled()

    def set_scribe_enabled(self, enabled: bool, save: bool = True) -> None:
        self.app_settings.set_scribe_enabled(enabled, save=save)

    def is_weekly_pricing_update_enabled(self) -> bool:
        return self.app_settings.is_weekly_pricing_update_enabled()

    def set_weekly_pricing_update_enabled(self, enabled: bool, save: bool = True) -> None:
        self.app_settings.set_weekly_pricing_update_enabled(enabled, save=save)

    def reset(self, save: bool = True) -> None:
        """Resets all registered agents to enabled state and resets orchestration mode to sandwich default."""
        self.enabled_agents.clear()
        self.enabled_agents.update(self.agents_config.keys())
        self.orchestration_mode = OrchestrationMode.SANDWICH.value
        self.sandwich_start_chain = list(DEFAULT_SANDWICH_START_CHAIN)
        self.sandwich_end_chain = list(DEFAULT_SANDWICH_END_CHAIN)
        self.fixed_chain = []
        self.workflow_config = DEFAULT_WORKFLOW_ID
        self.active_workflow_id = DEFAULT_WORKFLOW_ID
        if save:
            self.save()
        logger.info(f"[SETTINGS] Reset all {len(self.enabled_agents)} agents to ENABLED and mode to default review sandwich.")

    def get_enabled_agents(self) -> set[str]:
        return set(self.enabled_agents)

    def set_agent_enabled(self, agent_key: str, enabled: bool, save: bool = True) -> bool:
        if agent_key not in self.agents_config:
            logger.warning(f"[SETTINGS] Unknown agent key '{agent_key}'.")
            return False

        if enabled:
            self.enabled_agents.add(agent_key)
        else:
            self.enabled_agents.discard(agent_key)
        if save:
            self.save()
        logger.info(f"[SETTINGS] Set agent '{agent_key}' enabled={enabled}. Total enabled: {len(self.enabled_agents)}")
        return True

    def toggle_agent(self, agent_key: str) -> bool:
        current_state = agent_key in self.enabled_agents
        new_state = not current_state
        self.set_agent_enabled(agent_key, new_state)
        return agent_key in self.enabled_agents

    def get_agent(self, agent_key: str) -> dict[str, Any] | None:
        """Returns the configuration dict for a specific agent key, or None if not found."""
        return self.agents_config.get(agent_key)

    def is_system_agent(self, agent_key: str) -> bool:
        """Checks whether the agent is configured as a system agent in settings."""
        if agent_key in self.system_agents_config:
            return True
        agent_cfg = self.agents_config.get(agent_key)
        if isinstance(agent_cfg, dict) and agent_cfg.get("is_system_agent") is True:
            return True
        return False

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

    def refresh_agent_prompts(self) -> None:
        """Refreshes all in-memory agent system prompts (e.g. after global workspace context updates)."""
        for agent_key, cfg in self.agents_config.items():
            cfg["system_prompt"] = build_agent_prompt(agent_key, skills=cfg.get("skills"))
        logger.info(f"[SETTINGS] Refreshed system prompts for {len(self.agents_config)} agents.")

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

    def get_orchestrator_config(self) -> dict[str, Any]:
        return dict(self.orchestrator_config)

    def get_orchestrator_fast_model(self) -> str:
        return self.orchestrator_config.get("fast_model") or self.system_settings.get_default_fast_model()

    def get_orchestrator_reasoning_model(self) -> str:
        return self.orchestrator_config.get("reasoning_model") or self.system_settings.get_default_reasoning_model()

    def set_orchestrator_fast_model(self, model: str, save: bool = True) -> bool:
        clean = str(model).strip()
        if not clean:
            return False
        self.orchestrator_config["fast_model"] = clean
        if save:
            self.save()
        logger.info(f"[SETTINGS] Updated orchestrator fast_model: {clean}")
        return True

    def set_orchestrator_reasoning_model(self, model: str, save: bool = True) -> bool:
        clean = str(model).strip()
        if not clean:
            return False
        self.orchestrator_config["reasoning_model"] = clean
        if save:
            self.save()
        logger.info(f"[SETTINGS] Updated orchestrator reasoning_model: {clean}")
        return True

    def set_orchestrator_models(
        self,
        fast_model: str | None = None,
        reasoning_model: str | None = None,
        save: bool = True,
    ) -> bool:
        if fast_model:
            self.orchestrator_config["fast_model"] = str(fast_model).strip()
        if reasoning_model:
            self.orchestrator_config["reasoning_model"] = str(reasoning_model).strip()
        if save:
            self.save()
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


def set_circuit_breaker_enabled(
    enabled: bool, save: bool = True, reset_threshold_on_disable: bool = True
) -> None:
    settings_manager.set_circuit_breaker_enabled(
        enabled, save=save, reset_threshold_on_disable=reset_threshold_on_disable
    )


def get_circuit_breaker_threshold() -> float:
    return settings_manager.get_circuit_breaker_threshold()


def set_circuit_breaker_threshold(threshold: float, save: bool = True) -> None:
    settings_manager.set_circuit_breaker_threshold(threshold, save=save)


def get_circuit_breaker_config() -> dict[str, Any]:
    return settings_manager.get_circuit_breaker_config()


def is_scribe_enabled() -> bool:
    return settings_manager.is_scribe_enabled()


def set_scribe_enabled(enabled: bool, save: bool = True) -> None:
    settings_manager.set_scribe_enabled(enabled, save=save)


def is_weekly_pricing_update_enabled() -> bool:
    return settings_manager.is_weekly_pricing_update_enabled()


def set_weekly_pricing_update_enabled(enabled: bool, save: bool = True) -> None:
    settings_manager.set_weekly_pricing_update_enabled(enabled, save=save)


def save_enabled_agents() -> None:
    settings_manager.save()


def load_enabled_agents() -> set[str]:
    return settings_manager.load()


def sync_settings_if_modified() -> bool:
    return settings_manager.sync_if_modified()


def reset_enabled_agents(save: bool = True) -> None:
    settings_manager.reset(save=save)


def get_enabled_agents() -> set[str]:
    return settings_manager.get_enabled_agents()


def set_agent_enabled(agent_key: str, enabled: bool, save: bool = True) -> bool:
    return settings_manager.set_agent_enabled(agent_key, enabled, save=save)


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


def get_workflow_config() -> str | None:
    return settings_manager.get_workflow_config()


def set_workflow_config(workflow_config: str | None, save: bool = True) -> None:
    settings_manager.set_workflow_config(workflow_config, save=save)


def get_active_workflow_id() -> str | None:
    return settings_manager.get_active_workflow_id()


def set_active_workflow_id(workflow_id: str | None, save: bool = True) -> None:
    settings_manager.set_active_workflow_id(workflow_id, save=save)


def apply_workflow(workflow_id: str, save: bool = True) -> bool:
    return settings_manager.apply_workflow(workflow_id, save=save)


def persist_active_workflow() -> None:
    settings_manager.persist_active_workflow()


def get_orchestrator_fast_model() -> str:
    return settings_manager.get_orchestrator_fast_model()


def get_orchestrator_reasoning_model() -> str:
    return settings_manager.get_orchestrator_reasoning_model()


def set_orchestrator_fast_model(model: str, save: bool = True) -> bool:
    return settings_manager.set_orchestrator_fast_model(model, save=save)


def set_orchestrator_reasoning_model(model: str, save: bool = True) -> bool:
    return settings_manager.set_orchestrator_reasoning_model(model, save=save)

