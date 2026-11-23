"""
Centralized configuration integrity checks module (L1.5).

Provides ConfigIntegrityValidator for startup-time validation of agent, system,
and workflow settings payloads without depending on orchestration or services.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from pathlib import Path
from typing import Any

from core.paths import ENGINE_WORKFLOWS_DIR, PERSONAS_DIR, WORKFLOWS_BACKUP_DIR, WORKFLOWS_DIR

logger = logging.getLogger(__name__)

_DEFAULT_PERSONAS_DIR = PERSONAS_DIR
_DEFAULT_WORKFLOWS_DIR = WORKFLOWS_DIR
_DEFAULT_WORKFLOWS_BACKUP_DIR = WORKFLOWS_BACKUP_DIR


class ConfigIntegrityValidator:
    """Suite of integrity checks for settings, personas, and workflow presets."""

    @classmethod
    def run_all_checks(
        cls,
        payload: dict,
        *,
        personas_dir: Path | None = None,
        workflows_dir: Path | None = None,
        backups_dir: Path | None = None,
        engine_workflows_dir: Path | None = None,
    ) -> dict:
        p_dir = personas_dir or _DEFAULT_PERSONAS_DIR
        w_dir = workflows_dir or _DEFAULT_WORKFLOWS_DIR
        b_dir = backups_dir or _DEFAULT_WORKFLOWS_BACKUP_DIR
        ew_dir = engine_workflows_dir or ENGINE_WORKFLOWS_DIR

        payload = copy.deepcopy(payload)

        payload = cls.check_required_settings_keys(payload)
        payload = cls.check_agent_id_format(payload)
        payload = cls.check_duplicate_agent_names(payload)
        payload = cls.check_missing_prompt_files(payload, p_dir)
        payload = cls.check_workflow_references(payload, w_dir, engine_workflows_dir=ew_dir)
        payload = cls.check_default_workflows_integrity(payload, w_dir, backups_dir=b_dir, engine_workflows_dir=ew_dir)
        payload = cls.check_active_workflow_exists(payload, w_dir, engine_workflows_dir=ew_dir)
        payload = cls.check_orphaned_persona_files(payload, p_dir)
        payload = cls.check_model_strings(payload)
        return payload

    @classmethod
    def check_required_settings_keys(cls, payload: dict) -> dict:
        if not isinstance(payload, dict):
            logger.warning("[INTEGRITY] Agent settings payload is not a dictionary.")
            return payload

        required_top = ("workflow_config", "enabled_agents", "agents")
        for key in required_top:
            if key not in payload:
                logger.warning("[INTEGRITY] Required top-level setting '%s' is missing from agent_settings.", key)

        agents = payload.get("agents")
        if isinstance(agents, dict):
            required_agent_fields = ("username", "displayname", "model")
            for aid, acfg in agents.items():
                if isinstance(acfg, dict):
                    for field in required_agent_fields:
                        if not acfg.get(field):
                            logger.warning("[INTEGRITY] Agent '%s' is missing required field '%s'.", aid, field)
        return payload

    @classmethod
    def check_system_agent_settings_integrity(cls, payload: dict) -> dict:
        if not isinstance(payload, dict):
            logger.warning("[INTEGRITY] System agent settings payload is not a dictionary.")
            return payload

        if "orchestrator" not in payload:
            logger.warning("[INTEGRITY] Required setting 'orchestrator' is missing from system_agent_settings.")
        if "system_agents" not in payload:
            logger.warning("[INTEGRITY] Required setting 'system_agents' is missing from system_agent_settings.")
        else:
            sys_agents = payload.get("system_agents")
            if isinstance(sys_agents, dict):
                for req in ("Challenger", "Arbitrator"):
                    if req not in sys_agents:
                        logger.warning(
                            "[INTEGRITY] Required dialectical system agent '%s' is missing from system_agent_settings.",
                            req,
                        )
        return payload

    @classmethod
    def check_agent_id_format(cls, payload: dict) -> dict:
        if "agents" not in payload:
            return payload

        valid_agents: dict[str, Any] = {}
        for agent_key, agent_data in payload["agents"].items():
            if not re.match(r"^agent-\d+$", agent_key):
                logger.warning(
                    "[INTEGRITY] Agent ID '%s' is malformed (must match ^agent-\\d+$). This agent will be ignored.",
                    agent_key,
                )
                continue
            valid_agents[agent_key] = agent_data

        payload["agents"] = valid_agents
        return payload

    @classmethod
    def check_duplicate_agent_names(cls, payload: dict) -> dict:
        if "agents" not in payload:
            return payload

        seen: dict[str, str] = {}
        for agent_key, agent_data in payload["agents"].items():
            if not isinstance(agent_data, dict):
                continue
            username = agent_data.get("username")
            if not username or not isinstance(username, str):
                continue
            if username in seen:
                logger.warning(
                    "[INTEGRITY] Duplicate agent username '%s' found on IDs "
                    "'%s' and '%s'. Consider renaming one of them.",
                    username,
                    seen[username],
                    agent_key,
                )
            else:
                seen[username] = agent_key

        return payload

    @classmethod
    def check_missing_prompt_files(cls, payload: dict, personas_dir: Path) -> dict:
        if "agents" not in payload:
            return payload

        to_remove: list[str] = []
        for agent_key in payload["agents"]:
            prompt_path = personas_dir / f"{agent_key}.md"
            if not prompt_path.exists():
                logger.warning(
                    "[INTEGRITY] Missing persona prompt file '%s' for agent '%s'. Agent will be removed.",
                    prompt_path.name,
                    agent_key,
                )
                to_remove.append(agent_key)

        for key in to_remove:
            del payload["agents"][key]

        return payload

    @classmethod
    def check_workflow_references(
        cls,
        payload: dict,
        workflows_dir: Path,
        engine_workflows_dir: Path | None = None,
    ) -> dict:
        if "agents" not in payload:
            return payload

        known_ids = set(payload["agents"].keys())

        if not workflows_dir.is_dir():
            return payload

        for wf_file in workflows_dir.glob("*.json"):
            try:
                with open(wf_file, "r", encoding="utf-8") as f:
                    wf_data = json.load(f)
            except (OSError, json.JSONDecodeError, ValueError):
                logger.warning(
                    "[INTEGRITY] Could not parse workflow file '%s'. Skipping.",
                    wf_file.name,
                )
                continue

            if not isinstance(wf_data, dict):
                continue

            refs: list[str] = []
            sandwich_cfg = wf_data.get("sandwich_config", {})
            if isinstance(sandwich_cfg, dict):
                refs.extend(sandwich_cfg.get("start_chain", []))
                refs.extend(sandwich_cfg.get("end_chain", []))
            fixed_cfg = wf_data.get("fixed_config", {})
            if isinstance(fixed_cfg, dict):
                refs.extend(fixed_cfg.get("chain", []))

            for ref in refs:
                if isinstance(ref, str) and ref not in known_ids:
                    logger.warning(
                        "[INTEGRITY] Workflow '%s' references agent '%s' which is not present in agent_settings.",
                        wf_file.stem,
                        ref,
                    )

        return payload

    @classmethod
    def check_default_workflows_integrity(
        cls,
        payload: dict,
        workflows_dir: Path,
        backups_dir: Path | None = None,
        engine_workflows_dir: Path | None = None,
    ) -> dict:
        from config_store.workflow_manager import WorkflowManager
        from core.models import REQUIRED_DEFAULT_WORKFLOW_IDS

        if not workflows_dir.exists():
            workflows_dir.mkdir(parents=True, exist_ok=True)

        e_dir = engine_workflows_dir or ENGINE_WORKFLOWS_DIR
        wm = WorkflowManager(workflows_dir=workflows_dir, engine_workflows_dir=e_dir)

        for wf_id in REQUIRED_DEFAULT_WORKFLOW_IDS:
            existing_path = wm.get_workflow_file_path(wf_id)
            if existing_path is not None:
                continue

            preset = wm.get_workflow(wf_id)
            if preset is not None:
                logger.info(
                    "[INTEGRITY] Default workflow '%s' not found on disk in %s; using dynamic in-memory preset.",
                    wf_id,
                    e_dir,
                )
            else:
                logger.error(
                    "[INTEGRITY] Default workflow '%s' could not be resolved on disk or dynamically.",
                    wf_id,
                )

        return payload

    @classmethod
    def check_system_settings_integrity(cls, data: dict) -> dict:
        if not isinstance(data, dict):
            logger.warning("[INTEGRITY] System settings payload is not a dictionary.")
            return data

        required_keys = (
            "default_fast_model",
            "default_reasoning_model",
            "max_discussion_turns",
            "max_tool_iterations",
            "strict_privacy_mode_enabled",
            "dialectical_stress_test",
            "debate_turn_pacing_seconds",
        )
        for k in required_keys:
            if k not in data:
                logger.warning("[INTEGRITY] Required system setting '%s' is missing from system_settings.json.", k)

        return data

    @classmethod
    def check_active_workflow_exists(
        cls,
        payload: dict,
        workflows_dir: Path,
        engine_workflows_dir: Path | None = None,
    ) -> dict:
        active_wf_id = payload.get("workflow_config")
        if not active_wf_id:
            return payload

        from config_store.workflow_manager import WorkflowManager

        wm = WorkflowManager(workflows_dir=workflows_dir, engine_workflows_dir=engine_workflows_dir or ENGINE_WORKFLOWS_DIR)
        if not wm.get_workflow_file_path(active_wf_id):
            logger.warning(
                "[INTEGRITY] Active workflow '%s' does not exist in workflows directory. Falling back to 'default_review_flow'.",
                active_wf_id,
            )
            payload["workflow_config"] = "default_review_flow"

        return payload

    @classmethod
    def check_orphaned_persona_files(cls, payload: dict, personas_dir: Path) -> dict:
        if "agents" not in payload:
            return payload

        known_ids = set(payload["agents"].keys())

        if not personas_dir.is_dir():
            return payload

        for md_file in personas_dir.glob("agent-*.md"):
            agent_id = md_file.stem
            if agent_id not in known_ids:
                logger.warning(
                    "[INTEGRITY] Orphaned persona file '%s' has no corresponding entry in agent_settings.",
                    md_file.name,
                )

        return payload

    @classmethod
    def check_model_strings(cls, payload: dict) -> dict:
        if "agents" not in payload:
            return payload

        for agent_key, agent_data in payload["agents"].items():
            if not isinstance(agent_data, dict):
                continue
            model = agent_data.get("model", "")
            if not isinstance(model, str) or not model.strip():
                logger.warning(
                    "[INTEGRITY] Agent '%s' has an empty or missing 'model' field.",
                    agent_key,
                )
            elif "/" not in model:
                logger.warning(
                    "[INTEGRITY] Agent '%s' model '%s' does not follow 'provider/model' format.",
                    agent_key,
                    model,
                )

        return payload

    @classmethod
    def check_duplicate_ids_in_raw_json(cls, raw_json_text: str) -> list[str]:
        duplicates: list[str] = []

        def _detect_dupes(pairs: list[tuple[str, Any]]) -> dict:
            seen: set[str] = set()
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in seen:
                    duplicates.append(key)
                    logger.warning(
                        "[INTEGRITY] Duplicate JSON key '%s' detected in "
                        "agent_settings.json. The later value silently "
                        "overwrites the earlier one.",
                        key,
                    )
                seen.add(key)
                result[key] = value
            return result

        try:
            json.loads(raw_json_text, object_pairs_hook=_detect_dupes)
        except (json.JSONDecodeError, ValueError):
            pass

        return duplicates

