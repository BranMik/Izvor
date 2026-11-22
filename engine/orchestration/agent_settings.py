import copy
import json
import logging
import threading
import time
from pathlib import Path
from types import MappingProxyType
from typing import Any

from core.config import (
    AGENTS_CONFIG,
    ORCHESTRATOR_CONFIG,
    SYSTEM_AGENTS_CONFIG,
)
from core.models import OrchestrationMode
from orchestration.app_settings import AppSettingsManager, app_settings_manager
from orchestration.system_settings import SystemSettingsManager, system_settings_manager
from orchestration.integrity_checks import AppIntegrityValidator
from orchestration.settings_store import atomic_write_json
from prompts.builder import build_agent_prompt, is_dialectical_system_agent

from core.paths import (
    AGENT_BACKUP_FILE as BACKUP_FILE,
    AGENT_SETTINGS_FILE as SETTINGS_FILE,
    DEFAULT_SYSTEM_AGENT_SETTINGS_FILE,
    PERSONAS_DIR,
    SYSTEM_AGENT_BACKUP_FILE,
    SYSTEM_AGENT_SETTINGS_FILE,
)

logger = logging.getLogger(__name__)

# 7-day interval in seconds (604,800 seconds)
BACKUP_INTERVAL_SECONDS = 7 * 86400

# Default untagged workflow configuration: Sandwich mode
DEFAULT_WORKFLOW_ID = "default_review_flow"


class AgentSettingsManager:
    """
    Manages persistent agent configurations, system agent models, and thread circuit breaker limits.
    Serves as the Single Source of Truth (SSOT) loaded from config/settings/agent_settings.json
    and engine/settings/system_agent_settings.json.
    Includes atomic file writes, 7-day emergency backups, non-destructive failover, and manual restore.
    """

    def __init__(
        self,
        settings_file: Path | None = None,
        backup_file: Path | None = None,
        system_agent_settings_file: Path | None = None,
        system_agent_backup_file: Path | None = None,
        backup_interval_seconds: float = BACKUP_INTERVAL_SECONDS,
        auto_load: bool = True,
        workflows_dir: Path | None = None,
    ):
        self.default_settings_file = settings_file or SETTINGS_FILE
        self._custom_settings_file = settings_file is not None
        self._custom_backup_file = backup_file is not None
        self.default_backup_file = backup_file or BACKUP_FILE

        self.default_system_agent_settings_file = system_agent_settings_file or SYSTEM_AGENT_SETTINGS_FILE
        self._custom_system_agent_settings_file = system_agent_settings_file is not None
        self.default_system_agent_backup_file = system_agent_backup_file or SYSTEM_AGENT_BACKUP_FILE
        self._custom_system_agent_backup_file = system_agent_backup_file is not None

        self.backup_interval_seconds = backup_interval_seconds
        self._save_lock = threading.RLock()

        from orchestration.workflow_manager import WorkflowManager, workflow_manager
        if workflows_dir is not None:
            self.workflow_manager = WorkflowManager(workflows_dir=workflows_dir, auto_seed=True)
        else:
            self.workflow_manager = workflow_manager

        if settings_file is not None or backup_file is not None:
            self._agents_config = {}
            self._system_agents_config = {}
            self._orchestrator_config = {}
            target_dir = Path(settings_file).parent if settings_file is not None else self.default_settings_file.parent
            app_set_file = target_dir / "app_settings.json"
            sys_set_file = target_dir / "system_settings.json"
            self.app_settings = AppSettingsManager(settings_file=app_set_file, auto_load=True)
            self.system_settings = SystemSettingsManager(settings_file=sys_set_file, auto_load=True)
        else:
            self._agents_config = AGENTS_CONFIG
            self._system_agents_config = SYSTEM_AGENTS_CONFIG
            self._orchestrator_config = ORCHESTRATOR_CONFIG
            self.app_settings = app_settings_manager
            self.system_settings = system_settings_manager

        self._orchestrator_config.update({
            "fast_model": self.system_settings.get_default_fast_model(),
            "reasoning_model": self.system_settings.get_default_reasoning_model(),
            "username": "Orchestrator",
            "icon_url": "static/image_99.png",
        })
        self.enabled_agents: set[str] = set()

        # Orchestration flow mode and chains
        self.orchestration_mode: str = OrchestrationMode.SANDWICH.value
        self.sandwich_start_chain: list[str] = []
        self.sandwich_end_chain: list[str] = []
        self.fixed_chain: list[str] = []
        self.workflow_config: str | None = DEFAULT_WORKFLOW_ID
        self.active_workflow_id: str | None = DEFAULT_WORKFLOW_ID

        # Emergency backup status
        self.is_running_from_backup: bool = False
        self.backup_error_reason: str | None = None
        self._last_loaded_mtime: float = 0.0
        self._last_workflow_mtime: float = 0.0
        self._raw_data: dict[str, Any] = {}
        self._custom_challenger_agent: str | None = None
        self._custom_arbitrator_agent: str | None = None
        self._rendered_output_constraints: tuple[int, int] | None = None

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

    def get_agents_config(self) -> MappingProxyType[str, dict[str, Any]]:
        """Returns a read-only view of the currently configured agents."""
        return MappingProxyType(self._agents_config)

    def get_challenger_agent(self) -> str:
        if getattr(self, "_custom_challenger_agent", None):
            return self._custom_challenger_agent
        return "Challenger"

    def set_challenger_agent(self, agent_id: str | None, save: bool = False) -> None:
        self._custom_challenger_agent = agent_id

    def get_arbitrator_agent(self) -> str:
        if getattr(self, "_custom_arbitrator_agent", None):
            return self._custom_arbitrator_agent
        return "Arbitrator"

    def set_arbitrator_agent(self, agent_id: str | None, save: bool = False) -> None:
        self._custom_arbitrator_agent = agent_id

    def configure_paths(
        self,
        settings_file: Path | str | None = None,
        backup_file: Path | str | None = None,
        system_agent_settings_file: Path | str | None = None,
        system_agent_backup_file: Path | str | None = None,
        workflows_dir: Path | str | None = None,
        engine_workflows_dir: Path | str | None = None,
    ) -> None:
        if settings_file is not None:
            self.default_settings_file = Path(settings_file)
            self._custom_settings_file = True
        if backup_file is not None:
            self.default_backup_file = Path(backup_file)
            self._custom_backup_file = True
        if system_agent_settings_file is not None:
            self.default_system_agent_settings_file = Path(system_agent_settings_file)
            self._custom_system_agent_settings_file = True
        if system_agent_backup_file is not None:
            self.default_system_agent_backup_file = Path(system_agent_backup_file)
            self._custom_system_agent_backup_file = True
        if workflows_dir is not None or engine_workflows_dir is not None:
            self.workflow_manager.configure_paths(
                workflows_dir=workflows_dir,
                engine_workflows_dir=engine_workflows_dir,
            )

    def reset_paths(self) -> None:
        """Resets configured paths back to canonical system defaults."""
        self._custom_settings_file = False
        self._custom_backup_file = False
        self._custom_system_agent_settings_file = False
        self._custom_system_agent_backup_file = False
        self.default_settings_file = SETTINGS_FILE
        self.default_backup_file = BACKUP_FILE
        self.default_system_agent_settings_file = SYSTEM_AGENT_SETTINGS_FILE
        self.default_system_agent_backup_file = SYSTEM_AGENT_BACKUP_FILE
        self._custom_challenger_agent = None
        self._custom_arbitrator_agent = None
        self._rendered_output_constraints = None
        self.workflow_manager.reset_paths()

    @property
    def active_settings_file(self) -> Path:
        if self._custom_settings_file:
            return self.default_settings_file
        return Path(SETTINGS_FILE)

    @property
    def active_backup_file(self) -> Path:
        if self._custom_backup_file:
            return self.default_backup_file
        return Path(BACKUP_FILE)

    @property
    def active_system_agent_settings_file(self) -> Path:
        if self._custom_system_agent_settings_file:
            return self.default_system_agent_settings_file
        return Path(SYSTEM_AGENT_SETTINGS_FILE)

    @property
    def active_system_agent_backup_file(self) -> Path:
        if self._custom_system_agent_backup_file:
            return self.default_system_agent_backup_file
        return Path(SYSTEM_AGENT_BACKUP_FILE)

    def _atomic_write_json(self, target_path: Path, data: dict[str, Any]) -> None:
        """Writes JSON payload atomically using shared atomic_write_json."""
        atomic_write_json(target_path, data)

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
        If the file is missing or invalid, falls back to default preset or dynamic fallback.
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
                    f"[SETTINGS] Referenced workflow '{clean_ref}' not found in active workflow paths. "
                    "Attempting fallback to default preset."
                )
                try:
                    preset = self.workflow_manager.get_workflow("default_review_flow") or self.workflow_manager.get_workflow("default_sandwich")
                except (OSError, json.JSONDecodeError, ValueError, KeyError) as e:
                    logger.warning(f"[SETTINGS] Error loading default_review_flow fallback: {e}")

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
            wf_file = self.workflow_manager.get_workflow_file_path(preset.id)
            if wf_file and wf_file.exists():
                try:
                    self._last_workflow_mtime = wf_file.stat().st_mtime
                except OSError as e:
                    logger.debug(f"[SETTINGS] Error checking workflow mtime: {e}")
        else:
            # Fallback default when no preset is active
            self.active_workflow_id = DEFAULT_WORKFLOW_ID
            self.workflow_config = DEFAULT_WORKFLOW_ID
            self.orchestration_mode = OrchestrationMode.SANDWICH.value
            sw_start, sw_end = self._get_dynamic_fallback_chains()
            self.sandwich_start_chain = sw_start
            self.sandwich_end_chain = sw_end
            self.fixed_chain = []

    def _get_dynamic_fallback_chains(self) -> tuple[list[str], list[str]]:
        """
        Derives fallback start_chain and end_chain dynamically from enabled agents
        when no workflow preset can be loaded. Logs a WARNING.
        """
        logger.warning(
            "[SETTINGS] No active workflow preset loaded. Deriving dynamic fallback chains from enabled agents."
        )
        available = [a for a in sorted(self.enabled_agents) if a in self.agents_config]
        if not available:
            available = sorted(self.agents_config.keys())
        if not available:
            return [], []
        start_chain = available[:min(2, len(available))]
        end_chain = [available[-1]] if len(available) > 1 else list(start_chain)
        return start_chain, end_chain

    def persist_active_workflow(self) -> None:
        """Persists current mode and chains to the active workflow JSON file."""
        from orchestration.workflow_manager import slugify
        from core.models import REQUIRED_DEFAULT_WORKFLOW_IDS

        wf_id = self.workflow_config or self.active_workflow_id
        if not wf_id:
            return
        clean_id = slugify(wf_id)
        if clean_id in REQUIRED_DEFAULT_WORKFLOW_IDS:
            logger.info(
                f"[SETTINGS] Active workflow '{clean_id}' is a reserved system default preset. Skipping file persistence."
            )
            return

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
        wf_file = self.workflow_manager.get_workflow_file_path(saved.id)
        if wf_file and wf_file.exists():
            try:
                self._last_workflow_mtime = wf_file.stat().st_mtime
            except OSError as e:
                logger.debug(f"[SETTINGS] Error checking workflow mtime: {e}")

    def _parse_and_validate_payload(self, data: dict[str, Any], source_name: str) -> bool:
        """Parses and validates debate agent settings payload from JSON."""
        if not isinstance(data, dict):
            logger.error(f"[SETTINGS] Payload from {source_name} is not a valid JSON object.")
            return False

        self._raw_data = copy.deepcopy(data)
        data = AppIntegrityValidator.run_all_checks(data, personas_dir=PERSONAS_DIR)

        raw_agents = data.get("agents", {})
        if not isinstance(raw_agents, dict):
            logger.error(f"[SETTINGS] 'agents' key in {source_name} is not a dictionary after validation.")
            return False


        oc = self.system_settings.get_output_constraints()
        default_max_w = oc["default_max_words"]
        short_max_w = oc["short_max_words"]
        self._rendered_output_constraints = (default_max_w, short_max_w)

        valid_agents: dict[str, dict[str, Any]] = {}
        for key, cfg in raw_agents.items():
            if self._validate_agent_entry(key, cfg):
                is_short = bool(cfg.get("is_short_output", False))
                skills = [
                    s.strip()
                    for s in cfg.get("skills", [])
                    if isinstance(s, str) and s.strip()
                ]
                agent_entry: dict[str, Any] = {
                    "username": cfg.get("username", key),
                    "model": cfg["model"].strip(),
                    "displayname": cfg["displayname"].strip(),
                    "icon_url": cfg["icon_url"].strip(),
                    "is_short_output": is_short,
                    "skills": skills,
                    "system_prompt": build_agent_prompt(
                        key,
                        skills=skills,
                        is_short_output=is_short,
                        default_max_words=default_max_w,
                        short_max_words=short_max_w,
                    ),
                }
                valid_agents[key] = agent_entry

        if not valid_agents:
            logger.error(f"[SETTINGS] Zero valid agents found in {source_name}. Validation failed.")
            return False

        # Upsert then prune removed keys so readers never observe an empty agents_config dict
        self._agents_config.update(valid_agents)
        for stale_key in list(self._agents_config.keys()):
            if stale_key not in valid_agents:
                self._agents_config.pop(stale_key, None)

        # Parse orchestrator config if explicitly present in payload
        if "orchestrator" in data and isinstance(data["orchestrator"], dict):
            raw_orch = data["orchestrator"]
            default_fast = self.system_settings.get_default_fast_model()
            default_reasoning = self.system_settings.get_default_reasoning_model()
            self.orchestrator_config.update({
                "fast_model": str(raw_orch.get("fast_model", default_fast)).strip(),
                "reasoning_model": str(raw_orch.get("reasoning_model", default_reasoning)).strip(),
                "username": str(raw_orch.get("username", "Orchestrator")).strip(),
                "displayname": str(raw_orch.get("displayname", "Orchestrator")).strip(),
                "icon_url": str(raw_orch.get("icon_url", "static/image_99.png")).strip(),
            })

        # Parse system agents if explicitly present in payload
        if "system_agents" in data and isinstance(data["system_agents"], dict):
            for sys_k, sys_v in data["system_agents"].items():
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

        # Parse active workflow reference ("workflow_config" primary, "active_workflow_id" fallback)
        raw_wf = data.get("workflow_config")
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
        raw_enabled = data.get("enabled_agents")
        if raw_enabled is not None:
            if isinstance(raw_enabled, list):
                valid_enabled = {k for k in raw_enabled if k in self.agents_config}
            else:
                valid_enabled = set()
        else:
            # If enabled_agents is omitted, default to all valid agents
            valid_enabled = set(self.agents_config.keys())

        self.enabled_agents = set(valid_enabled)

        logger.info(
            f"[SETTINGS] Successfully validated payload from {source_name}: "
            f"{len(self.agents_config)} agents registered, {len(self.enabled_agents)} enabled, "
            f"workflow_config='{self.workflow_config}', mode='{self.orchestration_mode}', "
            f"{len(self.system_agents_config)} system agents."
        )
        return True

    def _load_and_validate_system_agent_settings(self) -> None:
        """
        Loads orchestrator and system agents configuration from
        engine/settings/system_agent_settings.json with emergency backup failover.
        """
        target_file = self.active_system_agent_settings_file
        backup_file = self.active_system_agent_backup_file
        data = None

        if target_file.exists():
            try:
                with open(target_file, "r", encoding="utf-8") as f:
                    raw_text = f.read()
                AppIntegrityValidator.check_duplicate_ids_in_raw_json(raw_text)
                data = json.loads(raw_text)
            except Exception as e:
                logger.warning(f"[SETTINGS] Failed to read {target_file.name}: {e}. Trying backup.")

        if not isinstance(data, dict) and backup_file.exists():
            try:
                with open(backup_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                logger.warning(f"[SETTINGS] Loaded system agent settings from backup {backup_file.name}")
            except Exception as e:
                logger.error(f"[SETTINGS] Failed reading system agent backup: {e}")

        if not isinstance(data, dict) and DEFAULT_SYSTEM_AGENT_SETTINGS_FILE.exists():
            try:
                with open(DEFAULT_SYSTEM_AGENT_SETTINGS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                logger.info(f"[SETTINGS] Loaded baseline system agent settings from {DEFAULT_SYSTEM_AGENT_SETTINGS_FILE.name}")
            except Exception as e:
                logger.error(f"[SETTINGS] Failed reading baseline system agent settings: {e}")

        if isinstance(data, dict):
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
                        if sys_v.get("displayname"):
                            sys_entry["displayname"] = str(sys_v["displayname"]).strip()
                        if sys_v.get("icon_url"):
                            sys_entry["icon_url"] = str(sys_v["icon_url"]).strip()
                        if sys_v.get("agent_key"):
                            sys_entry["agent_key"] = str(sys_v["agent_key"]).strip()
                        if "is_system_agent" in sys_v:
                            sys_entry["is_system_agent"] = bool(sys_v["is_system_agent"])
                        if "skills" in sys_v and isinstance(sys_v["skills"], list):
                            sys_entry["skills"] = [str(s).strip() for s in sys_v["skills"] if isinstance(s, str) and s.strip()]
                        else:
                            sys_entry["skills"] = []
                        if is_dialectical_system_agent(sys_k):
                            oc = self.system_settings.get_output_constraints()
                            sys_entry["system_prompt"] = build_agent_prompt(
                                sys_k,
                                skills=sys_entry["skills"],
                                default_max_words=oc["default_max_words"],
                                short_max_words=oc["short_max_words"],
                            )
                        if sys_entry:
                            self.system_agents_config[sys_k] = sys_entry

            self.check_and_update_system_agent_backup()

    def check_and_update_system_agent_backup(self) -> bool:
        """Updates emergency backup copy for system_agent_settings.json."""
        target_file = self.active_system_agent_settings_file
        backup_file = self.active_system_agent_backup_file

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
            except OSError:
                needs_backup = True

        if needs_backup:
            try:
                with open(target_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._atomic_write_json(backup_file, data)
                logger.info(f"[SETTINGS] Updated emergency system agent backup: {backup_file.name}")
                return True
            except Exception as e:
                logger.error(f"[SETTINGS] Failed to update system agent backup: {e}")
                return False

        return False

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
        Loads agent settings from active settings JSON.
        First loads system agents and orchestrator from active_system_agent_settings_file.
        If active settings file is corrupted, unreadable, or missing:
        - Loads from emergency backup into memory without modifying the broken active file.
        - Sets is_running_from_backup = True.
        """
        with self._save_lock:
            return self._load_unlocked()

    def _load_unlocked(self) -> set[str]:
        # Load system agent settings
        self._load_and_validate_system_agent_settings()

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
                    try:
                        from services.security.pii_masker import pii_masker
                        pii_masker.refresh_allowlist()
                    except Exception:
                        pass
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
                    try:
                        from services.security.pii_masker import pii_masker
                        pii_masker.refresh_allowlist()
                    except Exception:
                        pass
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
        Saves current enabled debate agents, agent configurations,
        and workflow reference to config/settings/agent_settings.json.
        Does NOT save orchestrator or system agents (saved via save_system_agent_settings).
        """
        wf_ref = self.workflow_config or self.active_workflow_id
        data = copy.deepcopy(self._raw_data) if hasattr(self, "_raw_data") and isinstance(self._raw_data, dict) else {}
        # Ensure system agents and orchestrator are not saved to agent_settings.json
        data.pop("system_agents", None)
        data.pop("orchestrator", None)

        data["workflow_config"] = wf_ref
        data["enabled_agents"] = sorted(self.enabled_agents)

        # Losslessly update agents
        if "agents" not in data or not isinstance(data["agents"], dict):
            data["agents"] = {}
        for k, v in self.agents_config.items():
            existing_agent = data["agents"].get(k, {}) if isinstance(data["agents"].get(k), dict) else {}
            agent_payload = {
                "username": v.get("username", k),
                "model": v.get("model", ""),
                "displayname": v.get("displayname", k),
                "icon_url": v.get("icon_url", ""),
                "is_short_output": bool(v.get("is_short_output", False)),
                "skills": list(v.get("skills", [])),
            }
            existing_agent.update(agent_payload)
            existing_agent.pop("special_role", None)
            existing_agent.pop("challenger_priority", None)
            data["agents"][k] = existing_agent
        for k in list(data["agents"].keys()):
            if k not in self.agents_config:
                del data["agents"][k]

        self._raw_data = copy.deepcopy(data)

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
                    f"workflow_config='{wf_ref}') to {target_file.name}"
                )
                self.check_and_update_backup()
                try:
                    from services.security.pii_masker import pii_masker
                    pii_masker.refresh_allowlist()
                except Exception:
                    pass
            except (OSError, TypeError, ValueError) as e:
                logger.error(f"[SETTINGS] Error saving agent settings: {e}")

    def save_system_agent_settings(self) -> None:
        """Saves current orchestrator and system agents configuration to engine/settings/system_agent_settings.json."""
        with self._save_lock:
            data = {
                "orchestrator": {
                    "fast_model": self.get_orchestrator_fast_model(),
                    "reasoning_model": self.get_orchestrator_reasoning_model(),
                    "username": self.orchestrator_config.get("username", "Orchestrator"),
                    "displayname": self.orchestrator_config.get("displayname", "Orchestrator"),
                    "icon_url": self.orchestrator_config.get("icon_url", "static/image_99.png"),
                },
                "system_agents": copy.deepcopy(self.system_agents_config),
            }
            target_file = self.active_system_agent_settings_file
            try:
                self._atomic_write_json(target_file, data)
                self.check_and_update_system_agent_backup()
                logger.info(f"[SETTINGS] Saved system agent settings to {target_file.name}")
            except Exception as e:
                logger.error(f"[SETTINGS] Error saving system agent settings: {e}")

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
            wf_file = self.workflow_manager.get_workflow_file_path(slugify(wf_id))
            if wf_file and wf_file.exists():
                try:
                    wf_mtime = wf_file.stat().st_mtime
                    last_wf_mtime = getattr(self, "_last_workflow_mtime", 0.0)
                    if wf_mtime > last_wf_mtime:
                        self.load()
                        return True
                except (OSError, RuntimeError) as e:
                    logger.debug(f"[SETTINGS] Error checking workflow mtime: {e}")

        return False

    def reload(self) -> set[str]:
        """Forces an explicit reload of agent settings from the active settings file."""
        return self.load()

    def get_orchestration_mode(self) -> str:
        return self.orchestration_mode

    def set_orchestration_mode(self, mode: str | OrchestrationMode, save: bool = True) -> None:
        raw_val = mode.value if isinstance(mode, OrchestrationMode) else str(mode)
        clean_val = raw_val.strip().lower()
        valid_modes = {m.value for m in OrchestrationMode}
        if clean_val not in valid_modes:
            logger.warning(
                f"[SETTINGS] Invalid orchestration mode '{raw_val}'. "
                f"Valid options: {valid_modes}. Setting to sandwich."
            )
            clean_val = OrchestrationMode.SANDWICH.value
        self.orchestration_mode = clean_val
        if save:
            self.save()
        logger.info(f"[SETTINGS] Orchestration mode updated to '{self.orchestration_mode}'")

    def get_sandwich_config(self) -> dict[str, list[str]]:
        return {
            "start_chain": list(self.sandwich_start_chain),
            "end_chain": list(self.sandwich_end_chain),
        }

    def set_sandwich_config(self, start_chain: list[str], end_chain: list[str], save: bool = True) -> None:
        self.sandwich_start_chain = [
            k for k in start_chain if isinstance(k, str) and (not self.agents_config or k in self.agents_config)
        ]
        self.sandwich_end_chain = [
            k for k in end_chain if isinstance(k, str) and (not self.agents_config or k in self.agents_config)
        ]
        if save:
            self.save()
        logger.info(
            f"[SETTINGS] Updated sandwich config: start={self.sandwich_start_chain}, end={self.sandwich_end_chain}"
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

        wf_file = self.workflow_manager.get_workflow_file_path(preset.id)
        if wf_file and wf_file.exists():
            try:
                self._last_workflow_mtime = wf_file.stat().st_mtime
            except OSError as e:
                logger.debug(f"[SETTINGS] Error checking workflow file mtime: {e}")

        if save:
            self.save()
        logger.info(f"[SETTINGS] Successfully applied workflow preset '{preset.name}' ({preset.id})")
        return True

    def get_workflow_snapshot(self) -> dict[str, Any]:
        """Returns a snapshot of the currently active workflow settings."""
        return {
            "workflow_id": self.get_active_workflow_id(),
            "mode": self.get_orchestration_mode(),
            "sandwich_config": self.get_sandwich_config(),
            "fixed_config": self.get_fixed_config(),
        }

    def reset(self, save: bool = True) -> None:
        """Resets all registered agents to enabled state and restores the default workflow preset."""
        self.enabled_agents.clear()
        self.enabled_agents.update(self.agents_config.keys())
        self._custom_challenger_agent = None
        self._custom_arbitrator_agent = None
        self._load_workflow_details(DEFAULT_WORKFLOW_ID)
        if save:
            self.save()
        logger.info(
            f"[SETTINGS] Reset all {len(self.enabled_agents)} agents to ENABLED "
            f"and workflow to '{self.active_workflow_id}' ({self.orchestration_mode})."
        )

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
        return self.agents_config.get(agent_key) or self.system_agents_config.get(agent_key)

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
        cfg = self.agents_config.get(agent_key) or self.system_agents_config.get(agent_key)
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
        oc = self.system_settings.get_output_constraints()
        self._rendered_output_constraints = (oc["default_max_words"], oc["short_max_words"])
        self.agents_config[agent_key]["system_prompt"] = build_agent_prompt(
            agent_key,
            skills=sanitized_skills,
            is_short_output=bool(self.agents_config[agent_key].get("is_short_output", False)),
            default_max_words=oc["default_max_words"],
            short_max_words=oc["short_max_words"],
        )
        if save:
            self.save()
        logger.info(f"[SETTINGS] Updated skills for '{agent_key}': {sanitized_skills}")
        return True

    def refresh_agent_prompts(self) -> None:
        """Refreshes all in-memory agent system prompts."""
        oc = self.system_settings.get_output_constraints()
        default_max_w = oc["default_max_words"]
        short_max_w = oc["short_max_words"]
        for agent_key, cfg in self.agents_config.items():
            cfg["system_prompt"] = build_agent_prompt(
                agent_key,
                skills=cfg.get("skills"),
                is_short_output=bool(cfg.get("is_short_output", False)),
                default_max_words=default_max_w,
                short_max_words=short_max_w,
            )
        for sys_key, sys_cfg in self.system_agents_config.items():
            if is_dialectical_system_agent(sys_key):
                sys_cfg["system_prompt"] = build_agent_prompt(
                    sys_key,
                    skills=sys_cfg.get("skills"),
                    default_max_words=default_max_w,
                    short_max_words=short_max_w,
                )
        self._rendered_output_constraints = (default_max_w, short_max_w)
        logger.info(f"[SETTINGS] Refreshed system prompts for {len(self.agents_config)} agents and dialectical system agents.")

    def sync_output_constraints_if_changed(self) -> bool:
        """Refreshes cached agent prompts if output_constraints changed in system_settings."""
        oc = self.system_settings.get_output_constraints()
        current = (oc["default_max_words"], oc["short_max_words"])
        if self._rendered_output_constraints != current:
            self.refresh_agent_prompts()
            return True
        return False

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
        """Sets the LLM model for a system agent and persists to system_agent_settings.json."""
        if sys_agent_key not in self.system_agents_config:
            logger.warning(f"[SETTINGS] Cannot set model for unknown system agent '{sys_agent_key}'")
            return False

        clean_model = str(model).strip()
        if not clean_model:
            return False

        self.system_agents_config[sys_agent_key]["model"] = clean_model
        if save:
            self.save_system_agent_settings()
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
            self.save_system_agent_settings()
        logger.info(f"[SETTINGS] Updated orchestrator fast_model: {clean}")
        return True

    def set_orchestrator_reasoning_model(self, model: str, save: bool = True) -> bool:
        clean = str(model).strip()
        if not clean:
            return False
        self.orchestrator_config["reasoning_model"] = clean
        if save:
            self.save_system_agent_settings()
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
            self.save_system_agent_settings()
        return True


# Default singleton instance initialized automatically
settings_manager = AgentSettingsManager()



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


def get_agents_config() -> MappingProxyType[str, dict[str, Any]]:
    return settings_manager.get_agents_config()


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


def set_agent_model(agent_key: str, model: str, save: bool = True) -> bool:
    return settings_manager.set_agent_model(agent_key, model, save=save)


def get_active_workflow_id() -> str | None:
    return settings_manager.get_active_workflow_id()


def set_active_workflow_id(workflow_id: str | None, save: bool = True) -> None:
    settings_manager.set_active_workflow_id(workflow_id, save=save)


def apply_workflow(workflow_id: str, save: bool = True) -> bool:
    return settings_manager.apply_workflow(workflow_id, save=save)


def get_workflow_snapshot() -> dict[str, Any]:
    return {
        "workflow_id": get_active_workflow_id(),
        "mode": get_orchestration_mode(),
        "sandwich_config": get_sandwich_config(),
        "fixed_config": get_fixed_config(),
    }


def persist_active_workflow() -> None:
    settings_manager.persist_active_workflow()


def get_challenger_agent() -> str:
    return settings_manager.get_challenger_agent()


def set_challenger_agent(agent_id: str | None, save: bool = False) -> None:
    settings_manager.set_challenger_agent(agent_id, save=save)


def get_arbitrator_agent() -> str:
    return settings_manager.get_arbitrator_agent()


def set_arbitrator_agent(agent_id: str | None, save: bool = False) -> None:
    settings_manager.set_arbitrator_agent(agent_id, save=save)
