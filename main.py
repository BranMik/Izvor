import asyncio
from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path
import sys

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

_engine_dir = Path(__file__).resolve().parent / "engine"
if str(_engine_dir) not in sys.path:
    sys.path.insert(0, str(_engine_dir))

from adapters.slack import slack_router
from api import api_router, public_router
from core.paths import AVATARS_DIR, ENGINE_AVATARS_DIR
from skills.registry import skill_registry
from core.logging_filters import setup_credential_redaction_filters
from core.config import get_admin_password, get_app_version
from adapters.slack.workspace_adapter import SlackWorkspaceAdapter
from agents.thread_notes_agent import clear_thread_note
from agents.usage_agent import cleanup_thread_usage, record_thread_tokens
from config_store.agent_settings import (
    load_enabled_agents,
    register_on_agents_changed,
    set_prompt_builder_hook,
)
from config_store.system_settings import system_settings_manager
from core.database import db_manager
from core.llm_utils import set_pii_masker_instance
from core.registry import (
    register_token_recorder,
    set_default_workspace_adapter_factory,
    set_initial_agents_provider,
    set_thread_state_provider,
)
from core.slack_security import set_audit_logger_hook, set_secret_scanner_hook
from core.task_utils import _ACTIVE_TASKS
from orchestration.nomination import get_initial_agents
from orchestration.thread_state import thread_state_manager
from prompts.builder import build_agent_prompt
from services.audit_logger import log_audit_event
from services.pricing_service import pricing_service
from services.rag.sync_engine import rag_sync_engine
from services.retention_service import register_thread_purge_hook
from services.security.pii_masker import pii_masker
from services.security import scan_output_for_secrets

# Wire IoC hooks from core to services and orchestration
set_audit_logger_hook(log_audit_event)
set_secret_scanner_hook(scan_output_for_secrets)
set_pii_masker_instance(pii_masker)
set_prompt_builder_hook(build_agent_prompt)
register_on_agents_changed(pii_masker.refresh_allowlist)
set_default_workspace_adapter_factory(SlackWorkspaceAdapter)
register_token_recorder(record_thread_tokens)
set_initial_agents_provider(get_initial_agents)
set_thread_state_provider(lambda: thread_state_manager)
register_thread_purge_hook("usage_tracker", cleanup_thread_usage)
register_thread_purge_hook("thread_notes", clear_thread_note)
register_thread_purge_hook("thread_state", thread_state_manager.cleanup_thread)

# Configure logging format
dynamic_log_level = getattr(logging, system_settings_manager.get_log_level(), logging.INFO)
logging.basicConfig(level=dynamic_log_level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
setup_credential_redaction_filters()
logger = logging.getLogger("IzvorBackend")

WIN32_HEARTBEAT_INTERVAL_SECONDS = 0.5


async def keep_loop_alive():
    """
    Heartbeat task to prevent Windows ProactorEventLoop from freezing
    when there are no active incoming HTTP sockets.
    """
    while True:
        await asyncio.sleep(WIN32_HEARTBEAT_INTERVAL_SECONDS)


async def periodic_thread_state_cleanup():
    """
    Periodically purges inactive expired thread states to prevent unbounded memory growth
    and purges expired cached downloaded Slack files.
    """
    from services.slack import cleanup_old_downloads

    while True:
        try:
            await asyncio.sleep(system_settings_manager.get_thread_cleanup_interval_seconds())
            cleaned = thread_state_manager.cleanup_expired_threads()
            if cleaned > 0:
                logger.info(f"[CLEANUP] Pruned {cleaned} inactive thread state(s)")
            downloads_hours = system_settings_manager.get_retention_downloads_hours()
            if downloads_hours > 0:
                await asyncio.to_thread(cleanup_old_downloads, float(downloads_hours) * 3600.0)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("[CLEANUP] Error during periodic thread state cleanup")


async def periodic_audit_integrity_guard():
    """
    Periodically validates the active month's audit log integrity in a worker thread.
    If tampering or chain corruption is detected, dispatches an urgent admin alert.
    """
    from orchestration.integrity_checks import AppIntegrityValidator
    from services.audit_logger import send_admin_audit_alert

    while True:
        try:
            await asyncio.sleep(system_settings_manager.get_audit_integrity_check_interval_seconds())
            res = await asyncio.to_thread(AppIntegrityValidator.check_audit_log_integrity)
            hmac_check = res.get("hmac_continuity", {})
            if hmac_check and not hmac_check.get("matches", True):
                logger.warning(f"[SECURITY GUARD] Periodic AUDIT_HMAC_SECRET mismatch: {hmac_check.get('message')}")
            if not res["valid"]:
                err_msg = "; ".join(res["errors"])
                logger.critical(f"[SECURITY GUARD] Periodic audit integrity violation: {err_msg}")
                await send_admin_audit_alert(
                    title="Periodic Audit Guard Integrity Breach",
                    message="Periodic scan identified corruption or tampering in the active audit trail.",
                    details={"Violations": err_msg, "Files": ", ".join(res["scanned_files"])},
                )
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("[SECURITY GUARD] Error during periodic audit integrity check")


async def periodic_retention_prune():
    """
    Periodically prunes expired database records (checkpoints, traces, event deduplication, etc.)
    at the interval configured in system_settings.json.
    """
    from services.retention_service import prune_expired_records

    while True:
        try:
            await asyncio.sleep(system_settings_manager.get_retention_prune_interval_seconds())
            await asyncio.to_thread(prune_expired_records)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("[RETENTION] Error during periodic retention prune")


async def periodic_skill_exclusions_sync():
    """
    Periodically checks for newly added or modified skills and syncs Presidio
    exclusions every 24 hours (or configured interval).
    """
    from core.registry import get_skill_registry

    while True:
        try:
            await asyncio.sleep(system_settings_manager.get_skill_exclusions_sync_interval_seconds())
            reg = get_skill_registry()
            await asyncio.to_thread(reg.reload)
            stats = await asyncio.to_thread(pii_masker.sync_skill_exclusions)
            if stats.get("new", 0) > 0 or stats.get("updated", 0) > 0 or stats.get("deleted", 0) > 0:
                logger.info(
                    f"[SKILL SYNC] Periodic sync updated skill exclusions: {stats['new']} new, "
                    f"{stats['updated']} updated, {stats['deleted']} deleted."
                )
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("[SKILL SYNC] Error during periodic skill exclusions sync")


async def _drain_active_tasks_and_close_db(timeout: float = 5.0) -> None:
    """Cancels remaining tracked tasks and closes all pooled SQLite connections."""
    active_snapshot = [t for t in list(_ACTIVE_TASKS) if not t.done()]
    for task in active_snapshot:
        task.cancel()
    if active_snapshot:
        try:
            await asyncio.wait_for(
                asyncio.gather(*active_snapshot, return_exceptions=True),
                timeout=timeout,
            )
        except TimeoutError:
            logger.warning("[MAIN] Timed out draining %d active tasks during shutdown", len(active_snapshot))

    try:
        from services.audit_logger import audit_logger

        audit_tasks = [t for t in list(getattr(audit_logger, "_background_audit_tasks", ())) if not t.done()]
        if audit_tasks:
            await asyncio.wait_for(
                asyncio.gather(*audit_tasks, return_exceptions=True),
                timeout=2.0,
            )
    except (ImportError, TimeoutError, Exception) as audit_drain_err:  # noqa: BLE001
        logger.debug(f"[MAIN] Audit task drain note: {audit_drain_err}")

    try:
        from services.telemetry import drain_turn_traces

        await drain_turn_traces(timeout=2.0)
    except (ImportError, Exception) as trace_drain_err:  # noqa: BLE001
        logger.debug(f"[MAIN] Telemetry trace drain note: {trace_drain_err}")

    try:
        db_manager.close_all_connections()
    except OSError as db_err:
        logger.warning(f"[MAIN] Error closing database connections during shutdown: {db_err}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail fast before DB initialization when multi-worker execution is configured
    concurrency_env = os.getenv("WEB_CONCURRENCY", "1")
    try:
        workers = int(concurrency_env)
    except ValueError:
        workers = 1
    if workers > 1:
        concurrency_msg = (
            f"[CONCURRENCY] Worker concurrency is set to {workers} (via WEB_CONCURRENCY). "
            "Izvor relies on in-memory asyncio.Task coordination, local thread state, "
            "and single-worker background queues. Running multiple worker processes will cause race "
            "conditions, duplicated agent turns, and split thread states. A single worker is required."
        )
        logger.critical(concurrency_msg)
        raise RuntimeError(concurrency_msg)

    # Fail fast if system_settings.json (and its emergency backup) is missing, invalid, or lacks required models
    from config_store.system_settings import validate_system_settings_startup
    from core.database import init_db
    from services.readiness import readiness_registry

    try:
        validate_system_settings_startup()
        readiness_registry.mark_component("system_settings", "ready", critical=True)
    except Exception as sys_settings_err:
        readiness_registry.mark_component(
            "system_settings",
            "failed",
            detail=f"{type(sys_settings_err).__name__}: {str(sys_settings_err)[:160]}",
            critical=True,
        )
        raise

    # Ensure SQLite schema and WAL mode are initialized before any queries execute
    try:
        init_db()
        readiness_registry.mark_component("database", "ready", critical=True)
    except Exception as db_init_err:
        readiness_registry.mark_component(
            "database",
            "failed",
            detail=f"{type(db_init_err).__name__}: {str(db_init_err)[:160]}",
            critical=True,
        )
        raise

    # Enforce non-repudiable audit HMAC secret configuration fail-fast
    from services.audit_logger import validate_audit_configuration

    validate_audit_configuration()

    if not get_admin_password():
        logger.warning(
            "[SECURITY] ADMIN_PASSWORD is not configured in environment/.env. "
            "Protected admin endpoints (/api/*) will reject all requests."
        )
    enabled = load_enabled_agents()
    logger.info(f"[STARTUP] Loaded {len(enabled)} enabled debate agents from persistent storage.")
    skill_registry.reload()
    logger.info(f"[STARTUP] Loaded {len(skill_registry.list_skills())} declarative skills from disk.")

    # Run startup audit log integrity and HMAC key continuity check
    from orchestration.integrity_checks import AppIntegrityValidator
    from services.audit_logger import send_admin_audit_alert

    audit_status = AppIntegrityValidator.check_audit_log_integrity()
    readiness_registry.mark_component(
        "audit_integrity",
        "ready" if audit_status.get("valid", False) else "degraded",
        detail="; ".join(audit_status.get("errors", []))[:200],
        critical=False,
    )
    hmac_check = audit_status.get("hmac_continuity", {})

    if hmac_check and not hmac_check.get("matches", True):
        hmac_status = hmac_check.get("status")
        msg = hmac_check.get("message")
        logger.warning(f"[SECURITY STARTUP] AUDIT_HMAC_SECRET mismatch: {msg}")

        if not audit_status["valid"]:
            # Combined alert: secret mismatch caused chain verification failure
            err_msg = "; ".join(audit_status["errors"])
            from core.task_utils import safe_create_task

            safe_create_task(
                send_admin_audit_alert(
                    title=f"Startup Audit Integrity Alert: Secret Mismatch ({hmac_status})",
                    message="Server startup detected that AUDIT_HMAC_SECRET does not match the key used for existing audit logs, causing verification failure.",
                    details={
                        "Status": hmac_status,
                        "Warning": msg,
                        "Last File": hmac_check.get("last_file") or "N/A",
                        "Last Seq": str(hmac_check.get("last_seq", 0)),
                        "Errors": err_msg[:500],
                    },
                ),
                name="startup_audit_alert_secret_mismatch_failure",
            )
        else:
            from core.task_utils import safe_create_task

            safe_create_task(
                send_admin_audit_alert(
                    title=f"AUDIT_HMAC_SECRET Discrepancy ({hmac_status})",
                    message="Server startup detected that AUDIT_HMAC_SECRET does not match the key used for existing audit logs.",
                    details={
                        "Status": hmac_status,
                        "Warning": msg,
                        "Last File": hmac_check.get("last_file") or "N/A",
                        "Last Seq": str(hmac_check.get("last_seq", 0)),
                    },
                ),
                name="startup_audit_alert_discrepancy",
            )
    elif not audit_status["valid"]:
        err_msg = "; ".join(audit_status["errors"])
        logger.critical(f"[SECURITY STARTUP] Audit log integrity violation detected: {err_msg}")
        from core.task_utils import safe_create_task

        safe_create_task(
            send_admin_audit_alert(
                title="Startup Audit Log Integrity Violation",
                message="Application startup detected corruption or tampering in non-repudiable audit logs.",
                details={"Violations": err_msg, "Files": ", ".join(audit_status["scanned_files"])},
            ),
            name="startup_audit_alert_integrity_violation",
        )

    from services.audit_logger import set_main_event_loop
    from services.retention_service import prune_expired_records
    from services.mcp.client_manager import mcp_client_manager
    from services.telemetry import start_telemetry_flusher

    set_main_event_loop(asyncio.get_running_loop())

    # Eager startup warm-ups and reconciliations awaited concurrently before signaling readiness
    warmup_specs = [
        ("pricing_cache", pricing_service.warm_up_cache()),
        ("rag_sync", rag_sync_engine.reconcile()),
        ("retention_prune", asyncio.to_thread(prune_expired_records)),
        ("pii_masker", pii_masker.warm_up()),
        ("mcp_discovery", mcp_client_manager.discover_all_enabled_servers()),
    ]
    warmup_results = await asyncio.gather(
        *(coro for _, coro in warmup_specs),
        return_exceptions=True,
    )
    for (comp_name, _), res in zip(warmup_specs, warmup_results):
        if isinstance(res, BaseException):
            logger.warning(f"[STARTUP] Non-fatal exception during startup warm-up ({comp_name}): {res}")
            readiness_registry.mark_component(
                comp_name,
                "degraded",
                detail=f"{type(res).__name__}: {str(res)[:160]}",
                critical=False,
            )
        else:
            readiness_registry.mark_component(comp_name, "ready", critical=False)

    start_telemetry_flusher()
    readiness_registry.mark_startup_complete(True)
    logger.info("[STARTUP] Application Startup Complete")

    background_tasks: list[asyncio.Task] = []
    if sys.platform == "win32":
        background_tasks.append(asyncio.create_task(keep_loop_alive(), name="keep_loop_alive"))
    background_tasks.append(asyncio.create_task(periodic_thread_state_cleanup(), name="periodic_thread_cleanup"))
    background_tasks.append(asyncio.create_task(periodic_audit_integrity_guard(), name="periodic_audit_guard"))
    background_tasks.append(asyncio.create_task(periodic_retention_prune(), name="periodic_retention_prune"))
    background_tasks.append(asyncio.create_task(periodic_skill_exclusions_sync(), name="periodic_skill_exclusions_sync"))
    try:
        yield
    finally:
        readiness_registry.mark_startup_complete(False)
        set_main_event_loop(None)
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)

        await _drain_active_tasks_and_close_db(timeout=5.0)

        try:
            await mcp_client_manager.shutdown()
        except (OSError, RuntimeError, TimeoutError, ValueError) as mcp_err:
            logger.warning(f"[MAIN] Error shutting down MCP client manager: {mcp_err}")


app = FastAPI(
    title="Izvor Multi-Agent Slack Hub",
    description="Multi-agent orchestration platform integrating Slack Events API, LiteLLM, and external MCP client tools.",
    version=get_app_version(),
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


class MultiDirStaticFiles(StaticFiles):
    """StaticFiles subclass that searches across multiple root directories in order."""

    def __init__(self, directories: list[Path | str], html: bool = False, check_dir: bool = False):
        existing_dirs = [Path(d) for d in directories if Path(d).exists()]
        primary_dir = existing_dirs[0] if existing_dirs else Path(directories[0])
        super().__init__(directory=primary_dir, html=html, check_dir=False)
        self.all_directories = [str(d) for d in existing_dirs]


app.mount(
    "/static",
    MultiDirStaticFiles(directories=[AVATARS_DIR, ENGINE_AVATARS_DIR]),
    name="static",
)
app.mount(
    "/avatars",
    MultiDirStaticFiles(directories=[AVATARS_DIR, ENGINE_AVATARS_DIR]),
    name="avatars",
)

# Mount first-party HTTP/REST API routers and Slack Adapter routes
app.include_router(public_router)
app.include_router(api_router, prefix="/api")
app.include_router(slack_router, prefix="/slack")


__all__ = [
    "app",
    "lifespan",
]
