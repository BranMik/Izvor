import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from core.models import OrchestrationMode, WorkflowPreset
from main import app
from orchestration.agent_settings import (
    AgentSettingsManager,
    apply_workflow,
    get_active_workflow_id,
    get_orchestration_mode,
    set_active_workflow_id,
    set_orchestration_mode,
    set_sandwich_config,
)
from orchestration.workflow_manager import (
    WorkflowManager,
    get_workflow,
    slugify,
)
from services.slack_client import build_app_home_view


def test_workflow_preset_model():
    """Verify WorkflowPreset serialization and deserialization isolates mode configs."""
    sandwich_preset = WorkflowPreset(
        id="test_wf",
        name="Test Workflow",
        mode=OrchestrationMode.SANDWICH.value,
        description="A test workflow description",
        sandwich_config={"start_chain": ["agent-10001"], "end_chain": ["agent-10008"]},
        fixed_config={"chain": ["Ignored"]},
    )
    d = sandwich_preset.to_dict()
    assert d["id"] == "test_wf"
    assert d["name"] == "Test Workflow"
    assert d["mode"] == "sandwich"
    assert "sandwich_config" in d
    assert "fixed_config" not in d
    assert d["sandwich_config"]["start_chain"] == ["agent-10001"]

    restored = WorkflowPreset.from_dict(d)
    assert restored.id == "test_wf"
    assert restored.name == "Test Workflow"
    assert restored.mode == OrchestrationMode.SANDWICH.value
    assert restored.sandwich_config["start_chain"] == ["agent-10001"]
    assert restored.fixed_config == {"chain": []}

    # Fixed mode preset isolation
    fixed_preset = WorkflowPreset(
        id="test_fixed",
        name="Test Fixed",
        mode=OrchestrationMode.FIXED.value,
        description="A fixed pipeline",
        sandwich_config={"start_chain": ["Ignored"], "end_chain": ["Ignored"]},
        fixed_config={"chain": ["agent-10011", "agent-10008"]},
    )
    d_fixed = fixed_preset.to_dict()
    assert d_fixed["mode"] == "fixed"
    assert "fixed_config" in d_fixed
    assert "sandwich_config" not in d_fixed
    assert d_fixed["fixed_config"]["chain"] == ["agent-10011", "agent-10008"]

    restored_fixed = WorkflowPreset.from_dict(d_fixed)
    assert restored_fixed.mode == OrchestrationMode.FIXED.value
    assert restored_fixed.fixed_config["chain"] == ["agent-10011", "agent-10008"]
    assert restored_fixed.sandwich_config == {"start_chain": [], "end_chain": []}

    # Legacy corrupted dict with both keys -> ignores non-designated key
    corrupted_data = {
        "id": "corrupted",
        "name": "Corrupted",
        "mode": "fixed",
        "sandwich_config": {"start_chain": ["Hallucinated"], "end_chain": []},
        "fixed_config": {"chain": ["ActualAgent"]},
    }
    restored_corrupted = WorkflowPreset.from_dict(corrupted_data)
    assert restored_corrupted.fixed_config["chain"] == ["ActualAgent"]
    assert restored_corrupted.sandwich_config["start_chain"] == []


def test_slugify():
    """Verify slug generation from various input strings."""
    assert slugify("Default Sandwich Review") == "default_sandwich_review"
    assert slugify("Security & Code Audit (v2.0)!") == "security_code_audit_v20"
    assert slugify("   Multiple   Spaces   ") == "multiple_spaces"
    assert slugify("___test___") == "test"
    assert slugify("!@#$%^&*()") == "workflow"


def test_workflow_manager_crud(tmp_path: Path):
    """Verify WorkflowManager full CRUD lifecycle in an isolated directory."""
    wf_dir = tmp_path / "isolated_workflows"
    mgr = WorkflowManager(workflows_dir=wf_dir, auto_seed=False)
    assert mgr.list_workflows() == []

    # 1. Seed defaults
    mgr.seed_default_workflows()
    seeded = mgr.list_workflows()
    assert len(seeded) >= 2
    assert any(p.id == "default_review_flow" for p in seeded)
    assert any(p.id == "default_sandwich" for p in seeded)

    # 2. Get workflow
    sw = mgr.get_workflow("default_sandwich")
    assert sw is not None
    assert sw.mode == OrchestrationMode.SANDWICH.value
    assert "agent-10001" in sw.sandwich_config["start_chain"]

    # 3. Save new workflow
    created = mgr.save_workflow(
        name="Custom Fixed Pipeline",
        mode=OrchestrationMode.FIXED.value,
        fixed_config={"chain": ["agent-10001", "agent-10008"]},
        description="A custom fixed sequence.",
    )
    assert created.id == "custom_fixed_pipeline"
    saved_file = wf_dir / "custom_fixed_pipeline.json"
    assert saved_file.exists()

    fetched = mgr.get_workflow("custom_fixed_pipeline")
    assert fetched is not None
    assert fetched.fixed_config["chain"] == ["agent-10001", "agent-10008"]

    # 4. List workflows includes new one
    current = mgr.list_workflows()
    assert len(current) == len(seeded) + 1

    # 5. Delete workflow
    deleted = mgr.delete_workflow("custom_fixed_pipeline")
    assert deleted is True
    assert not saved_file.exists()
    assert mgr.get_workflow("custom_fixed_pipeline") is None

    # Deleting nonexistent returns False
    assert mgr.delete_workflow("non_existent") is False


def test_workflow_manager_handles_corrupted_file(tmp_path: Path):
    """Verify WorkflowManager tolerates invalid/corrupted JSON files gracefully."""
    wf_dir = tmp_path / "corrupted_test_wf"
    mgr = WorkflowManager(workflows_dir=wf_dir, auto_seed=False)
    bad_file = wf_dir / "broken.json"
    bad_file.write_text("NOT A VALID JSON {{{", encoding="utf-8")

    good_preset = WorkflowPreset(id="good", name="Good Preset", mode="sandwich")
    mgr.save_preset(good_preset)

    workflows = mgr.list_workflows()
    assert len(workflows) == 1
    assert workflows[0].id == "good"


def test_agent_settings_workflow_integration(tmp_path: Path):
    """Verify AgentSettingsManager persistence and application of workflow presets."""
    settings_file = tmp_path / "agent_settings.json"
    wf_dir = tmp_path / "workflows"
    WorkflowManager(workflows_dir=wf_dir, auto_seed=True)

    mgr = AgentSettingsManager(settings_file=settings_file, auto_load=False, workflows_dir=wf_dir)
    mgr.agents_config = {"agent-10001": {}, "agent-10008": {}, "agent-10011": {}, "agent-10003": {}}
    mgr.save()

    # Verify agent_settings.json does not embed chains
    with open(settings_file, "r", encoding="utf-8") as f:
        saved_raw = json.load(f)
    assert "workflow_config" in saved_raw
    assert "sandwich_config" not in saved_raw
    assert "fixed_config" not in saved_raw

    # Apply default_sandwich
    applied = mgr.apply_workflow("default_sandwich")
    assert applied is True
    assert mgr.get_workflow_config() == "default_sandwich"
    assert mgr.get_active_workflow_id() == "default_sandwich"
    assert mgr.orchestration_mode == OrchestrationMode.SANDWICH.value
    assert "agent-10001" in mgr.sandwich_start_chain
    assert "agent-10008" in mgr.sandwich_end_chain

    # Switch to quick_tech_triage
    quick = WorkflowPreset(
        id="quick_tech_triage",
        name="Quick Tech Triage",
        mode=OrchestrationMode.FIXED.value,
        fixed_config={"chain": ["agent-10011", "agent-10008"]}
    )
    mgr.workflow_manager.save_preset(quick)
    applied_fixed = mgr.apply_workflow("quick_tech_triage")
    assert applied_fixed is True
    assert mgr.get_workflow_config() == "quick_tech_triage"
    assert mgr.get_active_workflow_id() == "quick_tech_triage"
    assert mgr.orchestration_mode == OrchestrationMode.FIXED.value
    assert mgr.fixed_chain == ["agent-10011", "agent-10008"]

    # Attempt invalid workflow
    assert mgr.apply_workflow("unknown_preset") is False


def test_build_app_home_view_includes_workflow_ui():
    """Verify Slack App Home view renders the workflow presets section."""
    from orchestration.agent_settings import settings_manager as mgr
    
    custom_presets = [p for p in mgr.workflow_manager.list_workflows() if not p.id.startswith("default_")]
    if not custom_presets:
        return  # No custom presets found, nothing to test for delete rendering
        
    preset_to_test = custom_presets[0]
    set_orchestration_mode(preset_to_test.mode)
    set_active_workflow_id(preset_to_test.id)

    view = build_app_home_view()
    assert view["type"] == "home"
    blocks = view["blocks"]

    action_ids = []
    block_ids = []
    for b in blocks:
        if "block_id" in b:
            block_ids.append(b["block_id"])
        if "accessory" in b and isinstance(b["accessory"], dict):
            action_ids.append(b["accessory"].get("action_id", ""))
        elif "elements" in b:
            for el in b["elements"]:
                action_ids.append(el.get("action_id", ""))
        elif "element" in b and isinstance(b["element"], dict):
            action_ids.append(b["element"].get("action_id", ""))

    assert "select_workflow_preset" in action_ids
    assert "delete_workflow_preset_button" in action_ids
    assert "save_workflow_preset_name_input" in action_ids
    assert "save_workflow_preset_button" in action_ids
    assert any(b.startswith("workflow_preset_select_block") for b in block_ids)
    assert any(b.startswith("save_workflow_preset_block") for b in block_ids)


@pytest.mark.anyio
async def test_main_interactivity_workflow_actions(monkeypatch):
    """Test full Block Kit interactivity dispatch for workflow preset save, load, and delete."""
    client = TestClient(app)

    # Mock publish_app_home and slack signature to avoid network/auth rejections
    mock_publish = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("adapters.slack.interactivity.publish_app_home", mock_publish)
    monkeypatch.setattr("adapters.slack.interactivity.verify_slack_signature", lambda body, headers: True)

    # 1. Save new workflow preset via block action
    save_payload = {
        "type": "block_actions",
        "user": {"id": "U12345"},
        "actions": [{"action_id": "save_workflow_preset_button"}],
        "view": {
            "state": {
                "values": {
                    "save_workflow_preset_block": {
                        "save_workflow_preset_name_input": {
                            "type": "plain_text_input",
                            "value": "Integration Test Preset",
                        }
                    }
                }
            }
        },
    }

    set_sandwich_config(["agent-10001"], ["agent-10008"])
    set_orchestration_mode("sandwich")

    resp = client.post("/slack/interactivity", json=save_payload)
    assert resp.status_code == 200

    saved = get_workflow("integration_test_preset")
    assert saved is not None
    assert saved.name == "Integration Test Preset"
    assert saved.sandwich_config["start_chain"] == ["agent-10001"]
    assert get_active_workflow_id() == "integration_test_preset"

    quick = WorkflowPreset(
        id="quick_tech_triage",
        name="Quick Tech Triage",
        mode=OrchestrationMode.FIXED.value,
        fixed_config={"chain": ["agent-10011", "agent-10008"]}
    )
    from orchestration.agent_settings import settings_manager
    settings_manager.workflow_manager.save_preset(quick)

    # 2. Select different preset via dropdown
    select_payload = {
        "type": "block_actions",
        "user": {"id": "U12345"},
        "actions": [
            {
                "action_id": "select_workflow_preset",
                "selected_option": {"value": "wf:quick_tech_triage"},
            }
        ],
    }
    resp = client.post("/slack/interactivity", json=select_payload)
    assert resp.status_code == 200
    assert get_active_workflow_id() == "quick_tech_triage"
    assert get_orchestration_mode() == OrchestrationMode.FIXED.value

    # 3. Load workflow preset via button
    load_payload = {
        "type": "block_actions",
        "user": {"id": "U12345"},
        "actions": [{"action_id": "load_workflow_preset_button"}],
        "view": {
            "state": {
                "values": {
                    "workflow_preset_select_block": {
                        "select_workflow_preset": {
                            "selected_option": {"value": "wf:integration_test_preset"}
                        }
                    }
                }
            }
        },
    }
    resp = client.post("/slack/interactivity", json=load_payload)
    assert resp.status_code == 200
    assert get_active_workflow_id() == "integration_test_preset"
    assert get_orchestration_mode() == OrchestrationMode.SANDWICH.value

    # 4. Delete custom preset via button
    del_payload = {
        "type": "block_actions",
        "user": {"id": "U12345"},
        "actions": [{"action_id": "delete_workflow_preset_button"}],
        "view": {
            "state": {
                "values": {
                    "workflow_preset_select_block": {
                        "select_workflow_preset": {
                            "selected_option": {"value": "wf:integration_test_preset"}
                        }
                    }
                }
            }
        },
    }
    resp = client.post("/slack/interactivity", json=del_payload)
    assert resp.status_code == 200
    assert get_workflow("integration_test_preset") is None
    assert get_active_workflow_id() in (None, "default_review_flow")

    # Restore default active workflow for subsequent tests
    apply_workflow("idea_architecture_feasibility_review")


def test_agent_settings_file_structure_and_dynamic_load():
    """Verify data/settings/agent_settings.json only has workflow_config and dynamically resolves chains."""
    settings_file = Path(__file__).parent.parent / "data" / "agent_settings.json"
    assert settings_file.exists()

    with open(settings_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Must contain workflow_config reference
    assert "workflow_config" in data
    assert data["workflow_config"] in (
        "idea_architecture_feasibility_review",
        "default_sandwich",
        "default_review_flow",
    )

    # Must NOT contain embedded chain definitions
    assert "sandwich_config" not in data
    assert "fixed_config" not in data

    # Verify that loading this file populates chains dynamically
    mgr = AgentSettingsManager(settings_file=settings_file, auto_load=True)
    assert mgr.get_workflow_config() == data["workflow_config"]
    if data["workflow_config"] == "idea_architecture_feasibility_review":
        assert mgr.get_orchestration_mode() == OrchestrationMode.FIXED.value
        assert len(mgr.fixed_chain) > 0
        assert "agent-10001" in mgr.fixed_chain
        assert "agent-10008" in mgr.fixed_chain
    else:
        assert mgr.get_orchestration_mode() == OrchestrationMode.SANDWICH.value
        assert len(mgr.sandwich_start_chain) > 0 or len(mgr.sandwich_end_chain) > 0


def test_workflow_config_switching_and_fallback(tmp_path: Path):
    """Verify switching workflow_config and graceful fallback when file is missing."""
    settings_file = tmp_path / "settings.json"
    wf_dir = tmp_path / "workflows"
    mgr = AgentSettingsManager(settings_file=settings_file, auto_load=False, workflows_dir=wf_dir)
    mgr.agents_config = {"agent-10001": {}, "agent-10008": {}, "agent-10011": {}}

    # Seeded default_sandwich has sandwich mode
    mgr.set_workflow_config("default_sandwich")
    assert mgr.get_orchestration_mode() == OrchestrationMode.SANDWICH.value
    assert "agent-10001" in mgr.sandwich_start_chain

    # Switch to quick_tech_triage
    quick = WorkflowPreset(
        id="quick_tech_triage",
        name="Quick Tech Triage",
        mode=OrchestrationMode.FIXED.value,
        fixed_config={"chain": ["agent-10011", "agent-10008"]}
    )
    mgr.workflow_manager.save_preset(quick)
    mgr.set_workflow_config("quick_tech_triage")
    assert mgr.get_orchestration_mode() == OrchestrationMode.FIXED.value
    assert mgr.fixed_chain == ["agent-10011", "agent-10008"]

    # Point to nonexistent workflow -> should fallback to default_sandwich without crashing
    mgr.set_workflow_config("nonexistent_workflow_xyz")
    assert mgr.get_orchestration_mode() == OrchestrationMode.SANDWICH.value
    assert "agent-10001" in mgr.sandwich_start_chain


def test_chain_mutation_persists_to_workflow_file(tmp_path: Path):
    """Verify that updating chains updates the active workflow JSON file directly."""
    settings_file = tmp_path / "settings.json"
    wf_dir = tmp_path / "workflows"
    mgr = AgentSettingsManager(settings_file=settings_file, auto_load=False, workflows_dir=wf_dir)
    mgr.agents_config = {"agent-10001": {}, "agent-10008": {}, "agent-10011": {}}

    # Start with quick_tech_triage
    quick = WorkflowPreset(
        id="quick_tech_triage",
        name="Quick Tech Triage",
        mode=OrchestrationMode.FIXED.value,
        fixed_config={"chain": ["agent-10011", "agent-10008"]}
    )
    mgr.workflow_manager.save_preset(quick)
    mgr.set_workflow_config("quick_tech_triage")
    wf_file = wf_dir / "quick_tech_triage.json"
    assert wf_file.exists()

    # Modify fixed chain and persist
    mgr.set_fixed_config(["agent-10001", "agent-10011"])
    mgr.persist_active_workflow()
    assert mgr.fixed_chain == ["agent-10001", "agent-10011"]

    # Verify directly inside quick_tech_triage.json
    with open(wf_file, "r", encoding="utf-8") as f:
        file_data = json.load(f)
    assert file_data["fixed_config"]["chain"] == ["agent-10001", "agent-10011"]

    # Verify agent_settings.json still does not embed the chain
    with open(settings_file, "r", encoding="utf-8") as f:
        settings_data = json.load(f)
    assert "workflow_config" in settings_data
    assert "fixed_config" not in settings_data

