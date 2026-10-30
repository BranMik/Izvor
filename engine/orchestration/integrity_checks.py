"""
Centralized application integrity checks module.

Provides the AppIntegrityValidator class which runs a suite of startup-time
integrity checks on agent settings payloads. Designed to be extended with
additional checks as the system evolves.
"""

import json
import logging
import re
import copy
import shutil
from pathlib import Path
from typing import Any

from core.paths import ENGINE_WORKFLOWS_DIR, PERSONAS_DIR, WORKFLOWS_BACKUP_DIR, WORKFLOWS_DIR

logger = logging.getLogger(__name__)

# Default paths — can be overridden per-check via kwargs
_DEFAULT_PERSONAS_DIR = PERSONAS_DIR
_DEFAULT_WORKFLOWS_DIR = WORKFLOWS_DIR
_DEFAULT_WORKFLOWS_BACKUP_DIR = ENGINE_WORKFLOWS_DIR


class AppIntegrityValidator:
    """
    Centralized suite for application startup integrity checks.

    Each check is a classmethod that receives the payload dict, validates or
    sanitises it, logs warnings for problems found, and returns the (possibly
    modified) payload. Checks that remove agents always log at WARNING level.

    To add a new check:
        1. Write a new @classmethod following the same signature.
        2. Call it from run_all_checks().
    """

    # ------------------------------------------------------------------
    # Entrypoint
    # ------------------------------------------------------------------
    @classmethod
    def run_all_checks(
        cls,
        payload: dict,
        *,
        personas_dir: Path | None = None,
        workflows_dir: Path | None = None,
        backups_dir: Path | None = None,
    ) -> dict:
        """
        Runs all registered integrity checks on the initial state/payload.
        Returns the sanitised/validated payload.

        Args:
            payload: The raw parsed agent_settings dict.
            personas_dir: Override for the personas directory (testing).
            workflows_dir: Override for the workflows directory (testing).
            backups_dir: Override for the workflows backup directory (testing).
        """
        p_dir = personas_dir or _DEFAULT_PERSONAS_DIR
        w_dir = workflows_dir or _DEFAULT_WORKFLOWS_DIR
        b_dir = backups_dir or _DEFAULT_WORKFLOWS_BACKUP_DIR

        payload = copy.deepcopy(payload)

        payload = cls.check_required_settings_keys(payload)
        payload = cls.check_special_roles(payload)
        payload = cls.check_agent_id_format(payload)
        payload = cls.check_duplicate_agent_names(payload)
        payload = cls.check_missing_prompt_files(payload, p_dir)
        payload = cls.check_workflow_references(payload, w_dir)
        payload = cls.check_default_workflows_integrity(payload, w_dir, backups_dir=b_dir)
        payload = cls.check_active_workflow_exists(payload, w_dir)
        payload = cls.check_orphaned_persona_files(payload, p_dir)
        payload = cls.check_model_strings(payload)
        return payload

    # ------------------------------------------------------------------
    # 0a. Required settings keys
    # ------------------------------------------------------------------
    @classmethod
    def check_required_settings_keys(cls, payload: dict) -> dict:
        """
        Validates that required top-level settings keys and required per-agent fields exist.
        Logs WARNING if any required key is missing.
        """
        if not isinstance(payload, dict):
            logger.warning("[INTEGRITY] Agent settings payload is not a dictionary.")
            return payload

        required_top = ("workflow_config", "enabled_agents", "agents")
        for key in required_top:
            if key not in payload:
                logger.warning(
                    "[INTEGRITY] Required top-level setting '%s' is missing from agent_settings.", key
                )

        agents = payload.get("agents")
        if isinstance(agents, dict):
            required_agent_fields = ("username", "displayname", "model")
            for aid, acfg in agents.items():
                if isinstance(acfg, dict):
                    for field in required_agent_fields:
                        if not acfg.get(field):
                            logger.warning(
                                "[INTEGRITY] Agent '%s' is missing required field '%s'.", aid, field
                            )
        return payload

    # ------------------------------------------------------------------
    # 0c. System agent settings integrity
    # ------------------------------------------------------------------
    @classmethod
    def check_system_agent_settings_integrity(cls, payload: dict) -> dict:
        """
        Validates system agent settings payload.
        Ensures orchestrator and system_agents exist.
        """
        if not isinstance(payload, dict):
            logger.warning("[INTEGRITY] System agent settings payload is not a dictionary.")
            return payload

        if "orchestrator" not in payload:
            logger.warning("[INTEGRITY] Required setting 'orchestrator' is missing from system_agent_settings.")
        if "system_agents" not in payload:
            logger.warning("[INTEGRITY] Required setting 'system_agents' is missing from system_agent_settings.")
        return payload

    # ------------------------------------------------------------------
    # 0b. Agent special roles
    # ------------------------------------------------------------------
    @classmethod
    def check_special_roles(cls, payload: dict) -> dict:
        """
        Validates that every agent has a valid special_role ('NONE', 'CHALLENGER', 'REBUTTAL').
        Normalizes missing/invalid roles to 'NONE' with a WARNING.
        Logs WARNING if zero CHALLENGER or zero REBUTTAL agents exist,
        or if multiple REBUTTAL agents exist (noting only the first available will be used).
        """
        agents = payload.get("agents")
        if not isinstance(agents, dict):
            return payload

        valid_roles = {"NONE", "CHALLENGER", "REBUTTAL"}
        challengers: list[str] = []
        rebuttals: list[str] = []

        for aid, acfg in agents.items():
            if not isinstance(acfg, dict):
                continue
            role = acfg.get("special_role")
            if not role or role not in valid_roles:
                logger.warning(
                    "[INTEGRITY] Agent '%s' has missing or invalid special_role '%s'. Normalizing to 'NONE'.",
                    aid,
                    role,
                )
                acfg["special_role"] = "NONE"
                role = "NONE"

            if role == "CHALLENGER":
                challengers.append(aid)
            elif role == "REBUTTAL":
                rebuttals.append(aid)

        if not challengers:
            logger.warning(
                "[INTEGRITY] Zero agents configured with special_role 'CHALLENGER'. Dialectical stress testing will not activate."
            )
        if not rebuttals:
            logger.warning(
                "[INTEGRITY] Zero agents configured with special_role 'REBUTTAL'. Dialectical stress testing will not activate."
            )
        elif len(rebuttals) > 1:
            logger.warning(
                "[INTEGRITY] Multiple agents (%s) configured with special_role 'REBUTTAL'. Only the first available agent (%s) will be used.",
                rebuttals,
                rebuttals[0],
            )

        return payload

    # ------------------------------------------------------------------
    # 1. Agent ID format — hard gate
    # ------------------------------------------------------------------
    @classmethod
    def check_agent_id_format(cls, payload: dict) -> dict:
        """
        Validates that every agent key matches the required ``agent-\\d+``
        format.  Malformed keys are **removed** from the payload.
        """
        if "agents" not in payload:
            return payload

        valid_agents: dict[str, Any] = {}
        for agent_key, agent_data in payload["agents"].items():
            if not re.match(r"^agent-\d+$", agent_key):
                logger.warning(
                    "[INTEGRITY] Agent ID '%s' is malformed (must match "
                    "^agent-\\d+$). This agent will be ignored.",
                    agent_key,
                )
                continue
            valid_agents[agent_key] = agent_data

        payload["agents"] = valid_agents
        return payload

    # ------------------------------------------------------------------
    # 2. Duplicate human-readable names — warning only
    # ------------------------------------------------------------------
    @classmethod
    def check_duplicate_agent_names(cls, payload: dict) -> dict:
        """
        Warns when two or more agents share the same human-readable *name*.
        Does **not** remove agents — the name is cosmetic — but the warning
        helps catch copy-paste mistakes.
        """
        if "agents" not in payload:
            return payload

        seen: dict[str, str] = {}  # name -> first agent_key
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

    # ------------------------------------------------------------------
    # 3. Missing persona prompt files — hard gate
    # ------------------------------------------------------------------
    @classmethod
    def check_missing_prompt_files(cls, payload: dict, personas_dir: Path) -> dict:
        """
        Verifies that every agent key has a matching ``<agent_id>.md`` file
        in the personas directory.  Agents without a prompt file are
        **removed**.
        """
        if "agents" not in payload:
            return payload

        to_remove: list[str] = []
        for agent_key in payload["agents"]:
            prompt_path = personas_dir / f"{agent_key}.md"
            if not prompt_path.exists():
                logger.warning(
                    "[INTEGRITY] Missing persona prompt file '%s' for "
                    "agent '%s'. Agent will be removed.",
                    prompt_path.name,
                    agent_key,
                )
                to_remove.append(agent_key)

        for key in to_remove:
            del payload["agents"][key]

        return payload

    # ------------------------------------------------------------------
    # 4. Workflow reference validation — warning only
    # ------------------------------------------------------------------
    @classmethod
    def check_workflow_references(cls, payload: dict, workflows_dir: Path) -> dict:
        """
        Scans all workflow JSON files and warns if any chain references an
        agent ID that does not exist in the payload's ``agents`` dict.
        Does **not** modify the payload — workflows may intentionally
        reference agents that are currently disabled.
        """
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

            # Collect every agent reference from all chain types
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
                        "[INTEGRITY] Workflow '%s' references agent '%s' "
                        "which is not present in agent_settings.",
                        wf_file.stem,
                        ref,
                    )

        return payload

    # ------------------------------------------------------------------
    # 5. Default workflows integrity — hard gate
    # ------------------------------------------------------------------
    @classmethod
    def check_default_workflows_integrity(
        cls,
        payload: dict,
        workflows_dir: Path,
        backups_dir: Path | None = None,
        backup_dir: Path | None = None,
    ) -> dict:
        """
        Validates that the required default workflows exist and are valid JSON.
        If missing or corrupted:
          1. Attempts restoration from backups_dir (WORKFLOWS_BACKUP_DIR).
          2. If backup is also absent or invalid, dynamically builds and saves presets
             using build_dynamic_default_workflows() based on active agents.
        """
        from core.models import REQUIRED_DEFAULT_WORKFLOW_IDS, build_dynamic_default_workflows
        from orchestration.workflow_manager import WorkflowManager

        if not workflows_dir.exists():
            workflows_dir.mkdir(parents=True, exist_ok=True)

        b_dir = backups_dir or backup_dir or _DEFAULT_WORKFLOWS_BACKUP_DIR
        wm = WorkflowManager(workflows_dir=workflows_dir, auto_seed=False)

        for wf_id in REQUIRED_DEFAULT_WORKFLOW_IDS:
            wf_file = workflows_dir / f"{wf_id}.json"
            needs_restore = False

            if not wf_file.exists():
                logger.warning(
                    "[INTEGRITY] Default workflow '%s' is missing in %s. Attempting restore from backup.",
                    wf_id,
                    workflows_dir,
                )
                needs_restore = True
            else:
                try:
                    with open(wf_file, "r", encoding="utf-8") as f:
                        disk_data = json.load(f)
                    if not isinstance(disk_data, dict) or not disk_data.get("id"):
                        logger.warning(
                            "[INTEGRITY] Default workflow '%s' is invalid or corrupted. Attempting restore from backup.",
                            wf_id,
                        )
                        needs_restore = True
                except (OSError, json.JSONDecodeError):
                    logger.warning(
                        "[INTEGRITY] Default workflow '%s' is unreadable. Attempting restore from backup.",
                        wf_id,
                    )
                    needs_restore = True

            if needs_restore:
                restored = False
                backup_file = b_dir / f"{wf_id}.json"
                if backup_file.exists():
                    try:
                        with open(backup_file, "r", encoding="utf-8") as bf:
                            b_data = json.load(bf)
                        if isinstance(b_data, dict) and b_data.get("id") == wf_id:
                            shutil.copy2(backup_file, wf_file)
                            logger.info(
                                "[INTEGRITY] Restored default workflow '%s' from backup at %s.",
                                wf_id,
                                backup_file,
                            )
                            restored = True
                    except (OSError, json.JSONDecodeError, ValueError) as ex:
                        logger.warning(
                            "[INTEGRITY] Could not restore workflow '%s' from backup file %s: %s",
                            wf_id,
                            backup_file,
                            ex,
                        )

                if not restored:
                    logger.warning(
                        "[INTEGRITY] Backup for default workflow '%s' not available. Dynamically generating from agent settings.",
                        wf_id,
                    )
                    agent_keys = list(payload.get("agents", {}).keys())
                    dyn_presets = build_dynamic_default_workflows(agent_keys)
                    for preset in dyn_presets:
                        if preset.id == wf_id:
                            try:
                                wm.save_preset(preset)
                                logger.info(
                                    "[INTEGRITY] Dynamically generated and saved default workflow '%s'.",
                                    wf_id,
                                )
                            except Exception as e:  # noqa: BLE001
                                logger.error(
                                    "[INTEGRITY] Failed to save dynamic workflow '%s': %s",
                                    wf_id,
                                    e,
                                )
                            break

        return payload

    # ------------------------------------------------------------------
    # 5b. System settings integrity
    # ------------------------------------------------------------------
    @classmethod
    def check_system_settings_integrity(cls, data: dict, settings_path: Path | None = None) -> dict:
        """
        Validates that required system settings keys exist in system_settings.json.
        Logs WARNING for any missing required key.
        If deprecated challenger_agents or rebuttal_agent exist, strips them and saves.
        """
        if not isinstance(data, dict):
            logger.warning("[INTEGRITY] System settings payload is not a dictionary.")
            return data

        required_keys = (
            "default_fast_model",
            "default_reasoning_model",
            "max_discussion_turns",
            "max_tool_iterations",
            "strict_privacy_mode_enabled",
            "convergence_consecutive_threshold",
            "dialectical_stress_test",
            "debate_turn_pacing_seconds",
        )
        for k in required_keys:
            if k not in data:
                logger.warning(
                    "[INTEGRITY] Required system setting '%s' is missing from system_settings.json.", k
                )

        dirty = False
        for deprecated in ("challenger_agents", "rebuttal_agent"):
            if deprecated in data:
                del data[deprecated]
                dirty = True

        if dirty and settings_path is not None:
            try:
                Path(settings_path).write_text(json.dumps(data, indent=2), encoding="utf-8")
                logger.info("[INTEGRITY] Stripped deprecated roles and saved system settings.")
            except Exception as e:  # noqa: BLE001
                logger.error("[INTEGRITY] Failed to write updated system settings: %s", e)

        return data

    # ------------------------------------------------------------------
    # 6. Active workflow validation — fallback to default
    # ------------------------------------------------------------------
    @classmethod
    def check_active_workflow_exists(cls, payload: dict, workflows_dir: Path) -> dict:
        """
        Validates that the active workflow configured in settings actually exists on disk.
        If it doesn't, falls back to the default review flow.
        """
        active_wf_id = payload.get("workflow_config")
        if not active_wf_id:
            return payload

        wf_file = workflows_dir / f"{active_wf_id}.json"
        if not wf_file.exists():
            logger.warning(
                "[INTEGRITY] Active workflow '%s' does not exist in workflows directory. Falling back to 'default_review_flow'.",
                active_wf_id,
            )
            payload["workflow_config"] = "default_review_flow"

        return payload

    # ------------------------------------------------------------------
    # 7. Orphaned persona files — warning only
    # ------------------------------------------------------------------
    @classmethod
    def check_orphaned_persona_files(cls, payload: dict, personas_dir: Path) -> dict:
        """
        Warns about ``.md`` files in the personas directory whose filename
        matches the ``agent-\\d+`` pattern but has no corresponding entry in
        ``agents``. System personas (Orchestrator, etc.) are excluded.
        """
        if "agents" not in payload:
            return payload

        known_ids = set(payload["agents"].keys())

        if not personas_dir.is_dir():
            return payload

        for md_file in personas_dir.glob("agent-*.md"):
            agent_id = md_file.stem  # e.g. "agent-10001"
            if agent_id not in known_ids:
                logger.warning(
                    "[INTEGRITY] Orphaned persona file '%s' has no "
                    "corresponding entry in agent_settings.",
                    md_file.name,
                )

        return payload

    # ------------------------------------------------------------------
    # 8. Model string validation — warning only
    # ------------------------------------------------------------------
    @classmethod
    def check_model_strings(cls, payload: dict) -> dict:
        """
        Warns when an agent's ``model`` field is empty, whitespace-only, or
        does not contain a ``/`` separator (the expected ``provider/model``
        format, e.g. ``anthropic/claude-opus-5``).
        """
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
                    "[INTEGRITY] Agent '%s' model '%s' does not follow "
                    "'provider/model' format.",
                    agent_key,
                    model,
                )

        return payload

    # ------------------------------------------------------------------
    # 7. Duplicate agent IDs in raw JSON — pre-parse check
    # ------------------------------------------------------------------
    @classmethod
    def check_duplicate_ids_in_raw_json(cls, raw_json_text: str) -> list[str]:
        """
        Detects duplicate agent ID keys in the raw JSON text **before**
        ``json.load()`` silently deduplicates them.

        Call this on the raw file contents before parsing.  Returns a list
        of duplicate key paths found (empty list = clean).

        Uses ``json.JSONDecoder`` with a custom ``object_pairs_hook``.
        """
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
            pass  # parse errors are handled elsewhere

        return duplicates

    # ------------------------------------------------------------------
    # 9. Non-repudiable audit log integrity check
    # ------------------------------------------------------------------
    @classmethod
    def check_audit_log_integrity(
        cls,
        audit_dir: Path | None = None,
        month: str | None = None,
    ) -> dict[str, Any]:
        """
        Validates the non-repudiable audit log cryptographic hash chain and HMAC key continuity.
        Returns a dictionary with status, errors, hmac_continuity, and verification details.
        """
        from services.audit_logger import (
            check_hmac_secret_continuity,
            verify_audit_log_integrity,
        )

        hmac_continuity = check_hmac_secret_continuity(audit_dir=audit_dir)
        if not hmac_continuity["matches"]:
            logger.warning(
                f"[INTEGRITY] AUDIT_HMAC_SECRET continuity check warning: {hmac_continuity['message']}"
            )

        result = verify_audit_log_integrity(audit_dir=audit_dir, month=month)
        if not result.valid:
            for err in result.errors:
                logger.critical(f"[INTEGRITY] Audit log verification failed: {err}")
        else:
            logger.info(
                f"[INTEGRITY] Audit log chain verified successfully ({result.total_records} records in {len(result.scanned_files)} files)."
            )
        return {
            "valid": result.valid,
            "total_records": result.total_records,
            "scanned_files": result.scanned_files,
            "errors": result.errors,
            "last_verified_seq": result.last_verified_seq,
            "last_verified_hash": result.last_verified_hash,
            "hmac_continuity": hmac_continuity,
        }
