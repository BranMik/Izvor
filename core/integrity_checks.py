"""
Centralized application integrity checks module.

Provides the AppIntegrityValidator class which runs a suite of startup-time
integrity checks on agent settings payloads. Designed to be extended with
additional checks as the system evolves.
"""

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default paths — can be overridden per-check via kwargs
_DEFAULT_PERSONAS_DIR = Path(__file__).resolve().parent.parent / "prompts" / "personas"
_DEFAULT_WORKFLOWS_DIR = Path(__file__).resolve().parent.parent / "data" / "workflows"


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
    ) -> dict:
        """
        Runs all registered integrity checks on the initial state/payload.
        Returns the sanitised/validated payload.

        Args:
            payload: The raw parsed agent_settings dict.
            personas_dir: Override for the personas directory (testing).
            workflows_dir: Override for the workflows directory (testing).
        """
        p_dir = personas_dir or _DEFAULT_PERSONAS_DIR
        w_dir = workflows_dir or _DEFAULT_WORKFLOWS_DIR

        payload = cls.check_agent_id_format(payload)
        payload = cls.check_duplicate_agent_names(payload)
        payload = cls.check_missing_prompt_files(payload, p_dir)
        payload = cls.check_workflow_references(payload, w_dir)
        payload = cls.check_orphaned_persona_files(payload, p_dir)
        payload = cls.check_model_strings(payload)
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
            name = agent_data.get("name")
            if not name or not isinstance(name, str):
                continue
            if name in seen:
                logger.warning(
                    "[INTEGRITY] Duplicate agent name '%s' found on IDs "
                    "'%s' and '%s'. Consider renaming one of them.",
                    name,
                    seen[name],
                    agent_key,
                )
            else:
                seen[name] = agent_key

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
    # 5. Orphaned persona files — warning only
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
    # 6. Model string validation — warning only
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
