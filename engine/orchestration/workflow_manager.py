import json
import logging
import os
import re
import tempfile
from pathlib import Path

from core.models import (
    REQUIRED_DEFAULT_WORKFLOW_IDS,
    OrchestrationMode,
    WorkflowPreset,
    build_dynamic_default_workflows,
)
from core.paths import ENGINE_WORKFLOWS_DIR, WORKFLOWS_BACKUP_DIR, WORKFLOWS_DIR

logger = logging.getLogger(__name__)


def slugify(name: str) -> str:
    """Converts a human-readable workflow name into a safe file slug."""
    slug = re.sub(r"[^\w\s-]", "", name.lower())
    slug = re.sub(r"[-\s]+", "_", slug).strip("_")
    return slug or "workflow"


class WorkflowManager:
    """
    Manages named orchestration workflow presets across a two-tier composite architecture:
    1. engine/workflows/ (immutable golden system defaults)
    2. config/workflows/ (implementer-owned custom presets)
    Provides atomic file writes, validation, path resolution, and CRUD operations.
    """

    def __init__(
        self,
        workflows_dir: Path | None = None,
        engine_workflows_dir: Path | None = None,
        auto_seed: bool = False,
    ):
        self.default_workflows_dir = Path(workflows_dir or WORKFLOWS_DIR)
        self.default_engine_workflows_dir = Path(engine_workflows_dir or ENGINE_WORKFLOWS_DIR)
        self.workflows_dir = self.default_workflows_dir
        self.engine_workflows_dir = self.default_engine_workflows_dir
        self.workflows_dir.mkdir(parents=True, exist_ok=True)

    def configure_paths(
        self,
        workflows_dir: Path | str | None = None,
        engine_workflows_dir: Path | str | None = None,
    ) -> None:
        """Configures active directory paths (used for dependency injection and test isolation)."""
        if workflows_dir is not None:
            self.workflows_dir = Path(workflows_dir)
            self.workflows_dir.mkdir(parents=True, exist_ok=True)
        if engine_workflows_dir is not None:
            self.engine_workflows_dir = Path(engine_workflows_dir)

    def reset_paths(self) -> None:
        """Resets active directory paths back to default canonical paths."""
        self.workflows_dir = self.default_workflows_dir
        self.engine_workflows_dir = self.default_engine_workflows_dir

    def _load_preset_from_file(self, file_path: Path) -> WorkflowPreset | None:
        try:
            content = file_path.read_text(encoding="utf-8")
            data = json.loads(content)
            if not isinstance(data, dict):
                return None
            if not data.get("id"):
                data["id"] = file_path.stem
            if not data.get("name"):
                data["name"] = file_path.stem.replace("_", " ").title()
            return WorkflowPreset.from_dict(data)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.warning(f"[WORKFLOW] Failed to load preset from {file_path.name}: {e}")
            return None

    def list_workflows(self) -> list[WorkflowPreset]:
        """Returns all valid workflow presets (built-in system presets merged with custom presets)."""
        presets_by_id: dict[str, WorkflowPreset] = {}

        # 1. Load built-in engine presets first (immutable baseline)
        if self.engine_workflows_dir.exists():
            for file_path in sorted(self.engine_workflows_dir.glob("*.json")):
                preset = self._load_preset_from_file(file_path)
                if preset:
                    presets_by_id[preset.id] = preset

        # Fallback: if engine defaults are missing on disk, build dynamic defaults
        if not presets_by_id:
            from core.config import AGENTS_CONFIG

            dyn_presets = build_dynamic_default_workflows(list(AGENTS_CONFIG.keys()))
            for p in dyn_presets:
                presets_by_id[p.id] = p

        # 2. Load custom presets from workflows_dir (cannot overwrite reserved default IDs)
        if self.workflows_dir.exists():
            for file_path in sorted(self.workflows_dir.glob("*.json")):
                preset = self._load_preset_from_file(file_path)
                if preset:
                    if preset.id in REQUIRED_DEFAULT_WORKFLOW_IDS:
                        logger.warning(
                            f"[WORKFLOW] Ignoring custom preset in {file_path.name} which attempts to shadow system default '{preset.id}'"
                        )
                        continue
                    presets_by_id[preset.id] = preset

        presets = list(presets_by_id.values())
        presets.sort(key=lambda p: p.name.lower())
        return presets

    def get_workflow_file_path(self, workflow_id: str) -> Path | None:
        """Resolves the physical filesystem path for a workflow ID (custom or engine)."""
        clean_id = slugify(workflow_id)
        custom_path = self.workflows_dir / f"{clean_id}.json"
        if custom_path.exists():
            return custom_path
        engine_path = self.engine_workflows_dir / f"{clean_id}.json"
        if engine_path.exists():
            return engine_path
        return None

    def get_workflow(self, workflow_id: str) -> WorkflowPreset | None:
        """Retrieves a workflow preset by its unique ID (checking custom then engine)."""
        clean_id = slugify(workflow_id)
        file_path = self.get_workflow_file_path(clean_id)
        if file_path:
            return self._load_preset_from_file(file_path)

        # Fallback: check dynamic defaults if it is a known required default ID
        if clean_id in REQUIRED_DEFAULT_WORKFLOW_IDS:
            from core.config import AGENTS_CONFIG

            dyn_presets = build_dynamic_default_workflows(list(AGENTS_CONFIG.keys()))
            for p in dyn_presets:
                if p.id == clean_id:
                    return p
        return None

    def save_preset(self, preset: WorkflowPreset) -> WorkflowPreset:
        """Atomically saves a WorkflowPreset to its JSON file in workflows_dir (custom tier only)."""
        clean_id = slugify(preset.id or preset.name)
        if clean_id in REQUIRED_DEFAULT_WORKFLOW_IDS:
            raise ValueError(f"Cannot overwrite reserved system default workflow '{clean_id}'")
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
            logger.info(f"[WORKFLOW] Saved custom workflow '{clean_id}' to {target_path}")
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
        mode_normalized = str(mode or "").strip().lower()
        if mode_normalized == OrchestrationMode.FIXED.value:
            sw_cfg = {"start_chain": [], "end_chain": []}
            fx_cfg = dict(fixed_config or {"chain": []})
        elif mode_normalized == OrchestrationMode.SCATTER_GATHER.value:
            sw_cfg = dict(sandwich_config or {"start_chain": [], "end_chain": []})
            fx_cfg = {"chain": []}
        else:
            mode_normalized = OrchestrationMode.SANDWICH.value
            sw_cfg = dict(sandwich_config or {"start_chain": [], "end_chain": []})
            fx_cfg = {"chain": []}

        preset = WorkflowPreset(
            id=w_id,
            name=name.strip(),
            mode=mode_normalized,
            description=description.strip(),
            sandwich_config=sw_cfg,
            fixed_config=fx_cfg,
        )
        return self.save_preset(preset)

    def delete_workflow(self, workflow_id: str) -> bool:
        """Deletes the JSON file for a custom workflow preset. System defaults cannot be deleted."""
        clean_id = slugify(workflow_id)
        if clean_id in REQUIRED_DEFAULT_WORKFLOW_IDS:
            logger.warning(f"[WORKFLOW] Cannot delete system default workflow '{clean_id}'")
            return False

        file_path = self.workflows_dir / f"{clean_id}.json"
        if not file_path.exists():
            return False

        try:
            file_path.unlink()
            logger.info(f"[WORKFLOW] Deleted custom workflow preset '{clean_id}'")
            return True
        except OSError as e:
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
