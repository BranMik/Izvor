"""
Tests for the centralized AppIntegrityValidator in core/integrity_checks.py.
"""

import json
from pathlib import Path


from orchestration.integrity_checks import AppIntegrityValidator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_payload(*agent_ids: str, names: dict[str, str] | None = None) -> dict:
    """Build a minimal valid payload with the given agent IDs."""
    agents = {}
    for aid in agent_ids:
        agents[aid] = {
            "name": (names or {}).get(aid, aid),
            "model": "anthropic/claude-opus-5",
            "username": aid,
            "icon_url": "static/img.png",
            "is_short_output": False,
            "skills": [],
        }
    return {"agents": agents, "enabled_agents": list(agent_ids)}


def _write_persona(personas_dir: Path, agent_id: str) -> None:
    """Create a dummy persona .md file."""
    (personas_dir / f"{agent_id}.md").write_text(f"# {agent_id}", encoding="utf-8")


def _write_workflow(workflows_dir: Path, name: str, chain_refs: list[str], mode: str = "sandwich") -> None:
    """Create a minimal workflow JSON file."""
    if mode == "fixed":
        wf = {"id": name, "mode": "fixed", "fixed_config": {"chain": chain_refs}, "sandwich_config": {}}
    else:
        wf = {"id": name, "mode": "sandwich", "sandwich_config": {"start_chain": chain_refs, "end_chain": []}, "fixed_config": {"chain": []}}
    (workflows_dir / f"{name}.json").write_text(json.dumps(wf), encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. check_agent_id_format
# ---------------------------------------------------------------------------

class TestAgentIdFormat:
    def test_valid_ids_pass(self):
        payload = _make_payload("agent-10001", "agent-99999")
        result = AppIntegrityValidator.check_agent_id_format(payload)
        assert set(result["agents"].keys()) == {"agent-10001", "agent-99999"}

    def test_malformed_ids_removed(self):
        payload = _make_payload("agent-10001")
        payload["agents"]["BadAgent"] = {"name": "Bad"}
        payload["agents"]["agent_no_dash"] = {"name": "NoDash"}
        payload["agents"]["agent-"] = {"name": "Empty"}
        result = AppIntegrityValidator.check_agent_id_format(payload)
        assert list(result["agents"].keys()) == ["agent-10001"]

    def test_no_agents_key_is_noop(self):
        payload = {"some_other_key": "value"}
        result = AppIntegrityValidator.check_agent_id_format(payload)
        assert result == payload

    def test_arbitrary_digit_length(self):
        payload = _make_payload("agent-1", "agent-123456789")
        result = AppIntegrityValidator.check_agent_id_format(payload)
        assert set(result["agents"].keys()) == {"agent-1", "agent-123456789"}


# ---------------------------------------------------------------------------
# 2. check_duplicate_agent_names
# ---------------------------------------------------------------------------

class TestDuplicateAgentNames:
    def test_unique_names_no_warning(self, caplog):
        payload = _make_payload("agent-10001", "agent-10002")
        payload["agents"]["agent-10001"]["name"] = "Alpha"
        payload["agents"]["agent-10002"]["name"] = "Beta"
        AppIntegrityValidator.check_duplicate_agent_names(payload)
        assert "Duplicate agent name" not in caplog.text

    def test_duplicate_names_warn(self, caplog):
        payload = _make_payload("agent-10001", "agent-10002")
        payload["agents"]["agent-10001"]["username"] = "SameName"
        payload["agents"]["agent-10002"]["username"] = "SameName"
        result = AppIntegrityValidator.check_duplicate_agent_names(payload)
        assert "Duplicate agent username" in caplog.text
        # Both agents are kept (warning only)
        assert len(result["agents"]) == 2

    def test_missing_name_ignored(self, caplog):
        payload = _make_payload("agent-10001")
        del payload["agents"]["agent-10001"]["name"]
        AppIntegrityValidator.check_duplicate_agent_names(payload)
        assert "Duplicate agent name" not in caplog.text


# ---------------------------------------------------------------------------
# 3. check_missing_prompt_files
# ---------------------------------------------------------------------------

class TestMissingPromptFiles:
    def test_all_files_present(self, tmp_path):
        personas = tmp_path / "personas"
        personas.mkdir()
        _write_persona(personas, "agent-10001")
        _write_persona(personas, "agent-10002")
        payload = _make_payload("agent-10001", "agent-10002")
        result = AppIntegrityValidator.check_missing_prompt_files(payload, personas)
        assert len(result["agents"]) == 2

    def test_missing_file_removes_agent(self, tmp_path):
        personas = tmp_path / "personas"
        personas.mkdir()
        _write_persona(personas, "agent-10001")
        # agent-10002 has no file
        payload = _make_payload("agent-10001", "agent-10002")
        result = AppIntegrityValidator.check_missing_prompt_files(payload, personas)
        assert list(result["agents"].keys()) == ["agent-10001"]


# ---------------------------------------------------------------------------
# 4. check_workflow_references
# ---------------------------------------------------------------------------

class TestWorkflowReferences:
    def test_valid_references_no_warning(self, tmp_path, caplog):
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        _write_workflow(wf_dir, "test_wf", ["agent-10001", "agent-10002"])
        payload = _make_payload("agent-10001", "agent-10002")
        AppIntegrityValidator.check_workflow_references(payload, wf_dir)
        assert "not present in agent_settings" not in caplog.text

    def test_dangling_reference_warns(self, tmp_path, caplog):
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        _write_workflow(wf_dir, "test_wf", ["agent-10001", "agent-99999"])
        payload = _make_payload("agent-10001")
        AppIntegrityValidator.check_workflow_references(payload, wf_dir)
        assert "agent-99999" in caplog.text
        assert "not present in agent_settings" in caplog.text

    def test_fixed_mode_references(self, tmp_path, caplog):
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        _write_workflow(wf_dir, "fixed_wf", ["agent-10001", "agent-55555"], mode="fixed")
        payload = _make_payload("agent-10001")
        AppIntegrityValidator.check_workflow_references(payload, wf_dir)
        assert "agent-55555" in caplog.text

    def test_missing_workflows_dir_noop(self, tmp_path):
        payload = _make_payload("agent-10001")
        result = AppIntegrityValidator.check_workflow_references(payload, tmp_path / "nonexistent")
        assert len(result["agents"]) == 1


# ---------------------------------------------------------------------------
# 5. check_orphaned_persona_files
# ---------------------------------------------------------------------------

class TestOrphanedPersonaFiles:
    def test_no_orphans(self, tmp_path, caplog):
        personas = tmp_path / "personas"
        personas.mkdir()
        _write_persona(personas, "agent-10001")
        payload = _make_payload("agent-10001")
        AppIntegrityValidator.check_orphaned_persona_files(payload, personas)
        assert "Orphaned persona file" not in caplog.text

    def test_orphan_detected(self, tmp_path, caplog):
        personas = tmp_path / "personas"
        personas.mkdir()
        _write_persona(personas, "agent-10001")
        _write_persona(personas, "agent-99999")  # orphan
        payload = _make_payload("agent-10001")
        AppIntegrityValidator.check_orphaned_persona_files(payload, personas)
        assert "agent-99999.md" in caplog.text

    def test_system_personas_ignored(self, tmp_path, caplog):
        """System persona files (e.g. Orchestrator.md) don't match agent-* glob."""
        personas = tmp_path / "personas"
        personas.mkdir()
        (personas / "Orchestrator.md").write_text("# Orch", encoding="utf-8")
        payload = _make_payload("agent-10001")
        _write_persona(personas, "agent-10001")
        AppIntegrityValidator.check_orphaned_persona_files(payload, personas)
        assert "Orphaned persona file" not in caplog.text


# ---------------------------------------------------------------------------
# 6. check_model_strings
# ---------------------------------------------------------------------------

class TestModelStrings:
    def test_valid_model_no_warning(self, caplog):
        payload = _make_payload("agent-10001")
        AppIntegrityValidator.check_model_strings(payload)
        assert "model" not in caplog.text.lower() or "INTEGRITY" not in caplog.text

    def test_empty_model_warns(self, caplog):
        payload = _make_payload("agent-10001")
        payload["agents"]["agent-10001"]["model"] = ""
        AppIntegrityValidator.check_model_strings(payload)
        assert "empty or missing" in caplog.text

    def test_no_slash_warns(self, caplog):
        payload = _make_payload("agent-10001")
        payload["agents"]["agent-10001"]["model"] = "gpt-4o"
        AppIntegrityValidator.check_model_strings(payload)
        assert "provider/model" in caplog.text


# ---------------------------------------------------------------------------
# 7. check_duplicate_ids_in_raw_json
# ---------------------------------------------------------------------------

class TestDuplicateIdsRawJson:
    def test_no_duplicates(self):
        raw = json.dumps({"agents": {"agent-10001": {}, "agent-10002": {}}})
        dupes = AppIntegrityValidator.check_duplicate_ids_in_raw_json(raw)
        assert dupes == []

    def test_duplicate_detected(self, caplog):
        # Build raw JSON with duplicate key manually
        raw = '{"agents": {"agent-10001": {"name": "A"}, "agent-10001": {"name": "B"}}}'
        dupes = AppIntegrityValidator.check_duplicate_ids_in_raw_json(raw)
        assert "agent-10001" in dupes
        assert "Duplicate JSON key" in caplog.text

    def test_invalid_json_returns_empty(self):
        dupes = AppIntegrityValidator.check_duplicate_ids_in_raw_json("{broken json")
        assert dupes == []


# ---------------------------------------------------------------------------
# run_all_checks integration
# ---------------------------------------------------------------------------

class TestRunAllChecks:
    def test_clean_payload_passes(self, tmp_path):
        personas = tmp_path / "personas"
        personas.mkdir()
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()

        _write_persona(personas, "agent-10001")
        _write_persona(personas, "agent-10002")
        _write_workflow(wf_dir, "test_wf", ["agent-10001"])

        payload = _make_payload("agent-10001", "agent-10002")
        result = AppIntegrityValidator.run_all_checks(
            payload, personas_dir=personas, workflows_dir=wf_dir
        )
        assert set(result["agents"].keys()) == {"agent-10001", "agent-10002"}

    def test_bad_id_and_missing_file_both_filtered(self, tmp_path):
        personas = tmp_path / "personas"
        personas.mkdir()
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()

        _write_persona(personas, "agent-10001")
        # agent-10002 has a persona file but BadAgent does not
        # BadAgent has wrong format, agent-10002 has no persona file
        payload = _make_payload("agent-10001", "agent-10002")
        payload["agents"]["BadAgent"] = {"name": "Bad", "model": "x/y"}

        result = AppIntegrityValidator.run_all_checks(
            payload, personas_dir=personas, workflows_dir=wf_dir
        )
        # BadAgent removed by ID check, agent-10002 removed by missing prompt
        assert list(result["agents"].keys()) == ["agent-10001"]


# ---------------------------------------------------------------------------
# 8. check_special_roles
# ---------------------------------------------------------------------------

class TestSpecialRoles:
    def test_valid_special_roles_pass(self, caplog):
        payload = _make_payload("agent-10001", "agent-10002", "agent-10003")
        payload["agents"]["agent-10001"]["special_role"] = "CHALLENGER"
        payload["agents"]["agent-10002"]["special_role"] = "REBUTTAL"
        payload["agents"]["agent-10003"]["special_role"] = "NONE"
        AppIntegrityValidator.check_special_roles(payload)
        assert "INTEGRITY" not in caplog.text

    def test_missing_special_role_warns(self, caplog):
        payload = _make_payload("agent-10001")
        # Missing special_role
        AppIntegrityValidator.check_special_roles(payload)
        assert "missing or invalid special_role" in caplog.text

    def test_invalid_special_role_warns(self, caplog):
        payload = _make_payload("agent-10001")
        payload["agents"]["agent-10001"]["special_role"] = "SUPERVISOR"
        AppIntegrityValidator.check_special_roles(payload)
        assert "missing or invalid special_role" in caplog.text

    def test_multiple_rebuttal_agents_warn(self, caplog):
        payload = _make_payload("agent-10001", "agent-10002", "agent-10003")
        payload["agents"]["agent-10001"]["special_role"] = "CHALLENGER"
        payload["agents"]["agent-10002"]["special_role"] = "REBUTTAL"
        payload["agents"]["agent-10003"]["special_role"] = "REBUTTAL"
        AppIntegrityValidator.check_special_roles(payload)
        assert "Multiple agents" in caplog.text and "special_role 'REBUTTAL'" in caplog.text

    def test_zero_challenger_agents_warn(self, caplog):
        payload = _make_payload("agent-10001", "agent-10002")
        payload["agents"]["agent-10001"]["special_role"] = "NONE"
        payload["agents"]["agent-10002"]["special_role"] = "REBUTTAL"
        AppIntegrityValidator.check_special_roles(payload)
        assert "Zero agents configured with special_role 'CHALLENGER'" in caplog.text


# ---------------------------------------------------------------------------
# 9. check_required_settings_keys
# ---------------------------------------------------------------------------

class TestRequiredSettingsKeys:
    def test_all_keys_present_no_warning(self, caplog):
        payload = {
            "workflow_config": "default_sandwich",
            "enabled_agents": ["agent-10001"],
            "agents": {
                "agent-10001": {
                    "username": "agent-10001",
                    "displayname": "Agent 1",
                    "model": "provider/model",
                }
            },
            "orchestrator": {},
        }
        AppIntegrityValidator.check_required_settings_keys(payload)
        assert "is missing from agent_settings" not in caplog.text

    def test_missing_keys_warn(self, caplog):
        payload = {"agents": {}}
        AppIntegrityValidator.check_required_settings_keys(payload)
        assert "Required top-level setting" in caplog.text
        assert "is missing from agent_settings" in caplog.text


# ---------------------------------------------------------------------------
# 10. check_system_settings_integrity
# ---------------------------------------------------------------------------

class TestSystemSettingsIntegrity:
    def test_validates_required_system_settings_keys(self, tmp_path):
        settings_file = tmp_path / "system_settings.json"
        data = {
            "default_fast_model": "mistral/mistral-small-latest",
            "default_reasoning_model": "deepseek/deepseek-reasoner",
            "max_discussion_turns": 10,
            "max_tool_iterations": 5,
            "strict_privacy_mode_enabled": True,
            "convergence_consecutive_threshold": 2,
            "dialectical_stress_test": True,
            "debate_turn_pacing_seconds": 1.0,
        }
        settings_file.write_text(json.dumps(data), encoding="utf-8")

        validated = AppIntegrityValidator.check_system_settings_integrity(data, settings_path=settings_file)
        assert validated["default_fast_model"] == "mistral/mistral-small-latest"
        assert validated["max_discussion_turns"] == 10


# ---------------------------------------------------------------------------
# 11. check_default_workflows_integrity fallback
# ---------------------------------------------------------------------------

class TestDefaultWorkflowsIntegrity:
    def test_restores_from_backup_dir(self, tmp_path):
        wf_dir = tmp_path / "workflows"
        backup_dir = tmp_path / "backups" / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        backup_dir.mkdir(parents=True, exist_ok=True)

        # Write canonical backup
        (backup_dir / "default_sandwich.json").write_text(
            json.dumps({"id": "default_sandwich", "name": "Backup Sandwich"}),
            encoding="utf-8"
        )

        payload = {"agents": {"agent-1": {}, "agent-2": {}}}
        AppIntegrityValidator.check_default_workflows_integrity(payload, workflows_dir=wf_dir, backups_dir=backup_dir)

        restored = json.loads((wf_dir / "default_sandwich.json").read_text(encoding="utf-8"))
        assert restored["name"] == "Backup Sandwich"

    def test_falls_back_to_dynamic_when_no_backup(self, tmp_path):
        wf_dir = tmp_path / "workflows"
        backup_dir = tmp_path / "non_existent_backups"
        wf_dir.mkdir(parents=True, exist_ok=True)

        payload = {"agents": {"agent-10001": {}, "agent-10002": {}}}
        AppIntegrityValidator.check_default_workflows_integrity(payload, workflows_dir=wf_dir, backups_dir=backup_dir)

        assert (wf_dir / "default_sandwich.json").exists()
        assert (wf_dir / "default_review_flow.json").exists()
        assert (wf_dir / "parallel_dialectic_flow.json").exists()

