from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = PROJECT_ROOT / "engine"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))


@dataclass(frozen=True)
class EvalIsolationPaths:
    """Isolated temporary directory layout yielded by eval_isolation()."""

    root: Path
    lancedb_dir: Path
    agents_resources_dir: Path
    guardrail_seeded: bool


@contextmanager
def eval_isolation(copy_guardrail_signatures: bool = False) -> Iterator[EvalIsolationPaths]:
    """
    Isolates all mutable Izvor singletons (SQLite DB, settings, backups, audit logs,
    flat histories, thread notes, local_tools sandbox, LanceDB guardrail) into a temporary
    directory while preserving real LLM/embedding API access and remaining safe under pytest.
    """
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        load_dotenv(dotenv_path=env_file)
    else:
        load_dotenv()

    from config_store.agent_settings import (
        BACKUP_FILE as REAL_BACKUP_FILE,
        SETTINGS_FILE as REAL_SETTINGS_FILE,
        SYSTEM_AGENT_BACKUP_FILE as REAL_SYS_AGENT_BACKUP_FILE,
        SYSTEM_AGENT_SETTINGS_FILE as REAL_SYS_AGENT_SETTINGS_FILE,
        settings_manager,
    )
    from config_store.app_settings import (
        APP_BACKUP_FILE as REAL_APP_BACKUP_FILE,
        APP_SETTINGS_FILE as REAL_APP_SETTINGS_FILE,
        app_settings_manager,
    )
    from config_store.system_settings import (
        SYSTEM_BACKUP_FILE as REAL_SYS_BACKUP_FILE,
        SYSTEM_SETTINGS_FILE as REAL_SYS_SETTINGS_FILE,
        system_settings_manager,
    )
    from core.database import db_manager
    from core.llm_utils import reset_default_llm_guard, reset_llm_completion_fn
    from core.model_catalog import reset_env_provider
    from core.paths import PRESIDIO_SKILL_EXCLUSIONS_FILE, WORKFLOWS_DIR as REAL_WORKFLOWS_DIR
    from services.audit_logger import audit_logger
    from services.flat_history_recorder import (
        configure_paths as configure_flat_paths,
        reset_paths as reset_flat_paths,
    )
    from services.local_tools import (
        configure_paths as configure_local_tools_paths,
        reset_paths as reset_local_tools_paths,
    )
    from services.pricing_service import (
        PRICING_CACHE_FILE as REAL_PRICING_CACHE_FILE,
        pricing_service,
    )
    from services.rag.config import LANCEDB_DIR as REAL_LANCEDB_DIR
    from services.rag.embeddings import EmbeddingService
    from services.security.guardrail import guardrail_service
    from services.security.pii_masker import pii_masker
    from services.thread_notes_writer import (
        configure_paths as configure_note_paths,
        reset_paths as reset_note_paths,
    )

    reset_default_llm_guard()
    reset_llm_completion_fn()
    reset_env_provider()
    EmbeddingService.reset_embeddings_fn()

    in_pytest = bool(os.getenv("PYTEST_CURRENT_TEST"))
    prev_db_path = db_manager.db_path
    prev_audit_dir = audit_logger.audit_dir
    prev_pricing_cache = pricing_service.active_cache_file
    prev_settings_file = settings_manager.default_settings_file
    prev_backup_file = settings_manager.default_backup_file
    prev_sys_agent_file = settings_manager.default_system_agent_settings_file
    prev_sys_agent_backup = settings_manager.default_system_agent_backup_file
    prev_workflows_dir = settings_manager.workflow_manager.workflows_dir
    prev_app_settings = app_settings_manager.default_settings_file
    prev_app_backup = app_settings_manager.default_backup_file
    prev_sys_settings = system_settings_manager.default_settings_file
    prev_sys_backup = system_settings_manager.default_backup_file
    prev_exclusions_file = getattr(pii_masker, "exclusions_file", PRESIDIO_SKILL_EXCLUSIONS_FILE)
    prev_guardrail_dir = guardrail_service.db_dir

    with tempfile.TemporaryDirectory(prefix="izvor_eval_", ignore_cleanup_errors=True) as tmp_str:
        iso = Path(tmp_str)
        audit_dir = iso / "audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        temp_flat = iso / "flat-histories"
        temp_flat.mkdir(parents=True, exist_ok=True)
        temp_notes = iso / "thread_notes"
        temp_notes.mkdir(parents=True, exist_ok=True)
        temp_res = iso / "agents_resources"
        temp_res.mkdir(parents=True, exist_ok=True)
        temp_lancedb = iso / "lancedb"
        temp_lancedb.mkdir(parents=True, exist_ok=True)

        temp_pricing = iso / "model_pricing_cache.json"
        if REAL_PRICING_CACHE_FILE.exists():
            shutil.copy2(REAL_PRICING_CACHE_FILE, temp_pricing)

        temp_settings = iso / "agent_settings.json"
        temp_backup = iso / "agent_settings_backup.json"
        temp_sys_ag = iso / "system_agent_settings.json"
        temp_sys_ag_bak = iso / "system_agent_settings_backup.json"
        if REAL_SETTINGS_FILE.exists():
            shutil.copy2(REAL_SETTINGS_FILE, temp_settings)
        if REAL_BACKUP_FILE.exists():
            shutil.copy2(REAL_BACKUP_FILE, temp_backup)
        if REAL_SYS_AGENT_SETTINGS_FILE.exists():
            shutil.copy2(REAL_SYS_AGENT_SETTINGS_FILE, temp_sys_ag)
        if REAL_SYS_AGENT_BACKUP_FILE.exists():
            shutil.copy2(REAL_SYS_AGENT_BACKUP_FILE, temp_sys_ag_bak)

        temp_wf = iso / "workflows"
        temp_wf.mkdir(parents=True, exist_ok=True)
        wf_source_dir = prev_workflows_dir if prev_workflows_dir.exists() else REAL_WORKFLOWS_DIR
        if wf_source_dir.exists():
            for wf_file in wf_source_dir.glob("*.json"):
                shutil.copy2(wf_file, temp_wf / wf_file.name)

        temp_app = iso / "app_settings.json"
        temp_app_bak = iso / "app_settings_backup.json"
        if REAL_APP_SETTINGS_FILE.exists():
            shutil.copy2(REAL_APP_SETTINGS_FILE, temp_app)
        if REAL_APP_BACKUP_FILE.exists():
            shutil.copy2(REAL_APP_BACKUP_FILE, temp_app_bak)

        temp_sys = iso / "system_settings.json"
        temp_sys_bak = iso / "system_settings_backup.json"
        if REAL_SYS_SETTINGS_FILE.exists():
            shutil.copy2(REAL_SYS_SETTINGS_FILE, temp_sys)
        if REAL_SYS_BACKUP_FILE.exists():
            shutil.copy2(REAL_SYS_BACKUP_FILE, temp_sys_bak)

        temp_excl = iso / "presidio_skill_exclusions.json"
        if PRESIDIO_SKILL_EXCLUSIONS_FILE.exists():
            shutil.copy2(PRESIDIO_SKILL_EXCLUSIONS_FILE, temp_excl)

        guardrail_seeded = False
        if copy_guardrail_signatures:
            real_guardrail_table = REAL_LANCEDB_DIR / "guardrail_signatures.lance"
            if real_guardrail_table.exists():
                shutil.copytree(
                    real_guardrail_table,
                    temp_lancedb / "guardrail_signatures.lance",
                    dirs_exist_ok=True,
                )
                guardrail_seeded = True

        audit_logger.configure_paths(audit_dir=audit_dir)
        pricing_service.configure_paths(cache_file=temp_pricing)
        settings_manager.configure_paths(
            settings_file=temp_settings,
            backup_file=temp_backup,
            system_agent_settings_file=temp_sys_ag,
            system_agent_backup_file=temp_sys_ag_bak,
            workflows_dir=temp_wf,
        )
        settings_manager.load()
        app_settings_manager.configure_paths(settings_file=temp_app, backup_file=temp_app_bak)
        app_settings_manager.load()
        system_settings_manager.configure_paths(settings_file=temp_sys, backup_file=temp_sys_bak)
        system_settings_manager.load()
        pii_masker.configure_paths(exclusions_file=temp_excl)
        db_manager.configure_paths(db_path=iso / "eval_izvor.db")
        configure_flat_paths(temp_flat)
        configure_note_paths(temp_notes)
        configure_local_tools_paths(workspace_dir=PROJECT_ROOT, agents_resources_dir=temp_res)
        guardrail_service.clear_cache()
        guardrail_service.configure_paths(db_dir=temp_lancedb)

        try:
            yield EvalIsolationPaths(
                root=iso,
                lancedb_dir=temp_lancedb,
                agents_resources_dir=temp_res,
                guardrail_seeded=guardrail_seeded,
            )
        finally:
            guardrail_service.clear_cache()
            db_manager.close_all_connections()
            if in_pytest:
                db_manager.configure_paths(db_path=prev_db_path)
                audit_logger.configure_paths(audit_dir=prev_audit_dir)
                pricing_service.configure_paths(cache_file=prev_pricing_cache)
                settings_manager.configure_paths(
                    settings_file=prev_settings_file,
                    backup_file=prev_backup_file,
                    system_agent_settings_file=prev_sys_agent_file,
                    system_agent_backup_file=prev_sys_agent_backup,
                    workflows_dir=prev_workflows_dir,
                )
                app_settings_manager.configure_paths(
                    settings_file=prev_app_settings,
                    backup_file=prev_app_backup,
                )
                system_settings_manager.configure_paths(
                    settings_file=prev_sys_settings,
                    backup_file=prev_sys_backup,
                )
                pii_masker.configure_paths(exclusions_file=prev_exclusions_file)
                guardrail_service.configure_paths(db_dir=prev_guardrail_dir)
                reset_local_tools_paths()
                reset_note_paths()
                reset_flat_paths()
            else:
                guardrail_service.reset_paths()
                reset_local_tools_paths()
                reset_note_paths()
                reset_flat_paths()
                db_manager.reset_paths()
                pii_masker.reset_paths()
                system_settings_manager.reset_paths()
                app_settings_manager.reset_paths()
                settings_manager.reset_paths()
                pricing_service.reset_paths()
                audit_logger.reset_paths()
            db_manager.close_all_connections()

