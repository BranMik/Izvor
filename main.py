import asyncio
import logging
import os
import sys
from pathlib import Path

_engine_dir = Path(__file__).resolve().parent / "engine"
if str(_engine_dir) not in sys.path:
    sys.path.insert(0, str(_engine_dir))
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

import secrets
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from adapters.slack import (
    PROCESSED_EVENTS,
    slack_router,
)
from core.config import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    AGENTS_CONFIG,
    get_admin_password,
    get_admin_username,
)
from orchestration.agent_settings import load_enabled_agents
from orchestration.thread_state import thread_state_manager
from services.pricing_service import pricing_service
from services.rag.sync_engine import rag_sync_engine

from orchestration.system_settings import system_settings_manager
from core.logging_filters import setup_credential_redaction_filters
from core.paths import AVATARS_DIR, ENGINE_AVATARS_DIR, PROJECT_ROOT
import skills.loader
from core.llm_utils import set_pii_masker_instance
from core.slack_security import set_audit_logger_hook, set_secret_scanner_hook
from services.audit_logger import log_audit_event
from services.security.pii_masker import pii_masker
from services.security import scan_output_for_secrets

# Wire IoC hooks from core to services
set_audit_logger_hook(log_audit_event)
set_secret_scanner_hook(scan_output_for_secrets)
set_pii_masker_instance(pii_masker)

# Configure logging format
dynamic_log_level = getattr(logging, system_settings_manager.get_log_level(), logging.INFO)
logging.basicConfig(level=dynamic_log_level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
setup_credential_redaction_filters()
logger = logging.getLogger("IzvorBackend")

WIN32_HEARTBEAT_INTERVAL_SECONDS = 0.5
RETENTION_PRUNE_INTERVAL_SECONDS = 86400.0  # 24 hours


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
            await asyncio.to_thread(cleanup_old_downloads)
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
    every 24 hours.
    """
    from services.retention_service import prune_expired_records

    while True:
        try:
            await asyncio.sleep(RETENTION_PRUNE_INTERVAL_SECONDS)
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure SQLite schema and WAL mode are initialized before any queries execute
    from core.database import init_db

    init_db()

    # Enforce non-repudiable audit HMAC secret configuration fail-fast
    from services.audit_logger import validate_audit_configuration

    validate_audit_configuration()

    # Check worker concurrency
    concurrency_env = os.getenv("WEB_CONCURRENCY", "1")
    try:
        workers = int(concurrency_env)
    except ValueError:
        workers = 1
    if workers > 1:
        logger.critical(
            f"[CONCURRENCY] Worker concurrency is set to {workers} (via WEB_CONCURRENCY). "
            "Izvor relies on in-memory asyncio.Task coordination, local thread state, "
            "and single-worker background queues. Running multiple worker processes will cause race "
            "conditions, duplicated agent turns, and split thread states. A single worker is required."
        )

    if not ADMIN_PASSWORD:
        logger.warning(
            "[SECURITY] ADMIN_PASSWORD is not configured in environment/.env. "
            "Protected admin endpoints (/api/*) will reject all requests."
        )
    enabled = load_enabled_agents()
    logger.info(f"[STARTUP] Loaded {len(enabled)} enabled debate agents from persistent storage.")

    # Run startup audit log integrity and HMAC key continuity check
    from orchestration.integrity_checks import AppIntegrityValidator
    from services.audit_logger import send_admin_audit_alert

    audit_status = AppIntegrityValidator.check_audit_log_integrity()
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
    from services.mcp import mcp_client_manager

    set_main_event_loop(asyncio.get_running_loop())

    # Eager startup warm-ups and reconciliations awaited concurrently before signaling readiness
    warmup_results = await asyncio.gather(
        pricing_service.warm_up_cache(),
        rag_sync_engine.reconcile(),
        asyncio.to_thread(prune_expired_records),
        pii_masker.warm_up(),
        mcp_client_manager.discover_all_enabled_servers(),
        return_exceptions=True,
    )
    for res in warmup_results:
        if isinstance(res, Exception):
            logger.warning(f"[STARTUP] Non-fatal exception during startup warm-up: {res}")

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
        set_main_event_loop(None)
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
        try:
            await mcp_client_manager.shutdown()
        except Exception as mcp_err:
            logger.warning(f"[MAIN] Error shutting down MCP client manager: {mcp_err}")


app = FastAPI(
    title="Izvor Multi-Agent Slack Hub",
    description="Multi-agent orchestration platform integrating Slack Events API, LiteLLM, and external MCP client tools.",
    version="1.0.0",
    lifespan=lifespan,
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

# Mount Slack Adapter routes (/slack/events, /slack/actions, /slack/interactivity)
app.include_router(slack_router, prefix="/slack")


@app.get("/")
@app.get("/health")
async def health_check():
    """Basic public health check endpoint."""
    return {
        "status": "healthy",
        "service": "Izvor Multi-Agent Hub",
    }


# HTTP Basic Authentication security scheme for admin endpoints
admin_security = HTTPBasic()

def verify_admin_basic_auth(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(admin_security),
) -> str:
    """
    Verifies HTTP Basic Auth credentials against ADMIN_USERNAME and ADMIN_PASSWORD.
    Uses secrets.compare_digest to defend against timing attacks.
    Applies sliding-window IP rate limiting on brute-force failure attempts.
    """
    from services.audit_logger import log_audit_event
    from services.rate_limiter import ip_rate_limiter

    client_ip = (
        request.client.host if request.client and request.client.host else "unknown_ip"
    )

    admin_pass = get_admin_password()
    admin_user = get_admin_username()

    if not admin_pass:
        logger.error("ADMIN_PASSWORD is not configured in environment/.env")
        log_audit_event(
            event_type="AUTH_FAILURE",
            actor={"type": "api_client", "id": credentials.username},
            action="admin_basic_auth",
            target="/api/*",
            details={"reason": "ADMIN_PASSWORD not configured on server", "client_ip": client_ip},
            severity="CRITICAL",
            status="FAILED",
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Admin authentication is not configured on the server",
        )

    is_correct_username = secrets.compare_digest(credentials.username.encode("utf8"), admin_user.encode("utf8"))
    is_correct_password = secrets.compare_digest(credentials.password.encode("utf8"), admin_pass.encode("utf8"))

    if not (is_correct_username and is_correct_password):
        is_allowed, attempts, should_log = ip_rate_limiter.check_and_record(
            client_ip=client_ip,
            max_requests=5,
            window_seconds=60.0,
        )

        if not is_allowed:
            if should_log:
                log_audit_event(
                    event_type="AUTH_RATE_LIMITED",
                    actor={"type": "api_client", "id": credentials.username},
                    action="admin_basic_auth",
                    target="/api/*",
                    details={
                        "reason": "Excessive failed basic auth attempts (throttled)",
                        "attempts": attempts,
                        "client_ip": client_ip,
                    },
                    severity="CRITICAL",
                    status="BLOCKED",
                )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed authentication attempts. Please wait before retrying.",
            )

        log_audit_event(
            event_type="AUTH_FAILURE",
            actor={"type": "api_client", "id": credentials.username},
            action="admin_basic_auth",
            target="/api/*",
            details={"reason": "Invalid credentials provided", "client_ip": client_ip},
            severity="CRITICAL",
            status="FAILED",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )

    ip_rate_limiter.reset(client_ip)
    return credentials.username


@app.get("/api/health", dependencies=[Depends(verify_admin_basic_auth)])
async def admin_health_check():
    """Admin health check endpoint exposing registered agent catalog."""
    return {
        "status": "healthy",
        "service": "Izvor Multi-Agent Hub",
        "agents": list(AGENTS_CONFIG.keys()),
    }


@app.get("/api/skills", dependencies=[Depends(verify_admin_basic_auth)])
async def get_registered_skills():
    """Returns all registered skills, descriptions, and their tool schemas."""
    from core.registry import get_skill_registry

    skill_registry = get_skill_registry()
    skills_data = []
    for s in skill_registry.list_skills():
        skills_data.append(
            {
                "name": s.name,
                "display_name": s.display_name,
                "description": s.description,
                "tools": [t.get("function", {}).get("name") for t in s.get_tool_schemas()],
                "schemas": s.get_tool_schemas(),
            }
        )
    return {"skills": skills_data}


@app.get("/api/rag/status", dependencies=[Depends(verify_admin_basic_auth)])
async def get_rag_status():
    """Returns RAG manifest status and indexed documents overview."""

    def _read_manifest():
        from services.rag.manifest_manager import ManifestManager

        manifest = ManifestManager()
        records = manifest.get_all_records()
        return {
            "status": "ready",
            "total_documents": len(records),
            "documents": records,
        }

    return await asyncio.to_thread(_read_manifest)


def get_rag_sync_engine():
    return rag_sync_engine


@app.post("/api/rag/reconcile")
async def trigger_rag_reconciliation(
    admin_user: str = Depends(verify_admin_basic_auth),
    sync_engine: Any = Depends(get_rag_sync_engine),
):
    """Triggers an on-demand reconciliation scan of rag_knowledge/."""
    from services.audit_logger import log_audit_event

    try:
        stats = await sync_engine.reconcile()
        log_audit_event(
            event_type="RAG_RECONCILE_COMPLETED",
            actor={"type": "admin_user", "id": admin_user},
            action="reconcile_rag_knowledge",
            target="rag_knowledge/",
            details={"source": "api_endpoint", "stats": stats},
            severity="INFO",
            status="SUCCESS",
        )
        return {"status": "reconciliation_complete", "stats": stats}
    except Exception as e:
        log_audit_event(
            event_type="RAG_RECONCILE_FAILED",
            actor={"type": "admin_user", "id": admin_user},
            action="reconcile_rag_knowledge",
            target="rag_knowledge/",
            details={"source": "api_endpoint", "error": str(e)},
            severity="CRITICAL",
            status="FAILED",
        )
        raise


@app.get("/api/audit/status", dependencies=[Depends(verify_admin_basic_auth)])
async def get_audit_status():
    """Returns non-repudiable audit log cryptographic integrity status and statistics."""
    from orchestration.integrity_checks import AppIntegrityValidator

    return await asyncio.to_thread(AppIntegrityValidator.check_audit_log_integrity)


__all__ = [
    "PROCESSED_EVENTS",
    "app",
    "slack_router",
]
