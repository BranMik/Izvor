import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from core.models import OrchestrationMode, WorkflowPreset

logger = logging.getLogger(__name__)

WORKFLOWS_DIR = Path(__file__).parent.parent / "data" / "workflows"


def slugify(name: str) -> str:
    """Converts a human-readable workflow name into a safe file slug."""
    slug = re.sub(r"[^\w\s-]", "", name.lower())
    slug = re.sub(r"[-\s]+", "_", slug).strip("_")
    return slug or "workflow"


class WorkflowManager:
    """
    Manages named orchestration workflow presets stored as individual JSON files in data/workflows/.
    Provides atomic file writes, automatic default seeding, validation, and CRUD operations.
    """

    def __init__(self, workflows_dir: Path | None = None, auto_seed: bool = True):
        self.workflows_dir = Path(workflows_dir or WORKFLOWS_DIR)
        self.workflows_dir.mkdir(parents=True, exist_ok=True)
        if auto_seed:
            self.seed_default_workflows()

    def seed_default_workflows(self) -> None:
        """Seeds initial default workflows if no JSON presets currently exist."""
        try:
            existing = list(self.workflows_dir.glob("*.json"))
            if existing:
                return

            default_presets = [
                WorkflowPreset(
                    id="default_sandwich",
                    name="Default Sandwich Review",
                    mode=OrchestrationMode.SANDWICH.value,
                    description="Standard multi-agent panel with kickoff and concluding wrap-up.",
                    sandwich_config={
                        "start_chain": [
                            "ScopeOptimizer",
                            "Visionary",
                            "UserAdvocate",
                            "Sceptic",
                            "Innovator",
                            "Architect",
                        ],
                        "end_chain": ["Architect", "Sceptic"],
                    },
                    fixed_config={"chain": []},
                ),
                WorkflowPreset(
                    id="quick_tech_triage",
                    name="Quick Tech Triage",
                    mode=OrchestrationMode.FIXED.value,
                    description="Focused linear pipeline for rapid technical problem triage.",
                    sandwich_config={"start_chain": [], "end_chain": []},
                    fixed_config={"chain": ["TechLead", "Architect"]},
                ),
            ]

            for preset in default_presets:
                self.save_preset(preset)
            logger.info(f"[WORKFLOW] Seeded {len(default_presets)} default workflows in {self.workflows_dir}")
        except Exception as e:
            logger.error(f"[WORKFLOW] Failed to seed default workflows: {e}", exc_info=True)

    def list_workflows(self) -> list[WorkflowPreset]:
        """Returns all valid workflow presets ordered by name."""
        presets: list[WorkflowPreset] = []
        if not self.workflows_dir.exists():
            return presets

        for file_path in sorted(self.workflows_dir.glob("*.json")):
            try:
                content = file_path.read_text(encoding="utf-8")
                data = json.loads(content)
                if not isinstance(data, dict):
                    continue
                # Ensure id matches filename stem if missing
                if not data.get("id"):
                    data["id"] = file_path.stem
                if not data.get("name"):
                    data["name"] = file_path.stem.replace("_", " ").title()
                presets.append(WorkflowPreset.from_dict(data))
            except Exception as e:
                logger.warning(f"[WORKFLOW] Failed to load preset from {file_path.name}: {e}")

        presets.sort(key=lambda p: p.name.lower())
        return presets

    def get_workflow(self, workflow_id: str) -> WorkflowPreset | None:
        """Retrieves a workflow preset by its unique ID."""
        clean_id = slugify(workflow_id)
        file_path = self.workflows_dir / f"{clean_id}.json"
        if not file_path.exists():
            return None

        try:
            content = file_path.read_text(encoding="utf-8")
            data = json.loads(content)
            if not isinstance(data, dict):
                return None
            if not data.get("id"):
                data["id"] = clean_id
            return WorkflowPreset.from_dict(data)
        except Exception as e:
            logger.error(f"[WORKFLOW] Error reading workflow '{workflow_id}': {e}")
            return None

    def save_preset(self, preset: WorkflowPreset) -> WorkflowPreset:
        """Atomically saves a WorkflowPreset to its JSON file."""
        clean_id = slugify(preset.id or preset.name)
        preset.id = clean_id
        target_path = self.workflows_dir / f"{clean_id}.json"

        data = preset.to_dict()
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(self.workflows_dir),
            prefix=f"{clean_id}_",
            suffix=".tmp",
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, target_path)
            logger.info(f"[WORKFLOW] Saved workflow '{clean_id}' to {target_path}")
            return preset
        except Exception as e:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            logger.error(f"[WORKFLOW] Failed to atomically save workflow '{clean_id}': {e}")
            raise

    def save_workflow(
        self,
        name: str,
        mode: str,
        sandwich_config: dict[str, list[str]] | None = None,
        fixed_config: dict[str, list[str]] | None = None,
        description: str = "",
        workflow_id: str | None = None,
    ) -> WorkflowPreset:
        """Creates and saves a new or updated workflow preset."""
        w_id = workflow_id or slugify(name)
        preset = WorkflowPreset(
            id=w_id,
            name=name.strip(),
            mode=mode,
            description=description.strip(),
            sandwich_config=dict(sandwich_config or {"start_chain": [], "end_chain": []}),
            fixed_config=dict(fixed_config or {"chain": []}),
        )
        return self.save_preset(preset)

    def delete_workflow(self, workflow_id: str) -> bool:
        """Deletes the JSON file for the specified workflow ID."""
        clean_id = slugify(workflow_id)
        file_path = self.workflows_dir / f"{clean_id}.json"
        if not file_path.exists():
            return False

        try:
            file_path.unlink()
            logger.info(f"[WORKFLOW] Deleted workflow preset '{clean_id}'")
            return True
        except Exception as e:
            logger.error(f"[WORKFLOW] Failed to delete workflow '{clean_id}': {e}")
            return False


# Global default manager instance
workflow_manager = WorkflowManager()


def list_workflows() -> list[WorkflowPreset]:
    return workflow_manager.list_workflows()


def get_workflow(workflow_id: str) -> WorkflowPreset | None:
    return workflow_manager.get_workflow(workflow_id)


def save_workflow(
    name: str,
    mode: str,
    sandwich_config: dict[str, list[str]] | None = None,
    fixed_config: dict[str, list[str]] | None = None,
    description: str = "",
    workflow_id: str | None = None,
) -> WorkflowPreset:
    return workflow_manager.save_workflow(
        name=name,
        mode=mode,
        sandwich_config=sandwich_config,
        fixed_config=fixed_config,
        description=description,
        workflow_id=workflow_id,
    )


def delete_workflow(workflow_id: str) -> bool:
    return workflow_manager.delete_workflow(workflow_id)
