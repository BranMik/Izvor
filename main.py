import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import secrets
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from adapters.slack import (
    PROCESSED_EVENTS,
    slack_router,
)
from adapters.slack import (
    handle_slack_events as slack_events,
)
from adapters.slack import (
    handle_slack_interactivity as slack_interactivity,
)
from core.config import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    AGENTS_CONFIG,
)
from core.slack_security import verify_slack_signature
from orchestration.agent_router import process_and_respond
from orchestration.agent_settings import load_enabled_agents
from orchestration.thread_state import thread_state_manager
from services.pricing_service import warm_up_pricing_cache
from services.rag.sync_engine import rag_sync_engine
from services.slack_client import publish_app_home

from orchestration.system_settings import system_settings_manager
from core.logging_filters import setup_credential_redaction_filters

# Configure logging format
dynamic_log_level = getattr(logging, system_settings_manager.get_log_level(), logging.INFO)
logging.basicConfig(
    level=dynamic_log_level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
setup_credential_redaction_filters()
logger = logging.getLogger("IzvorBackend")

BASE_DIR = Path(__file__).resolve().parent


async def keep_loop_alive():
    """
    Heartbeat task to prevent Windows ProactorEventLoop from freezing
    when there are no active incoming HTTP sockets.
    """
    while True:
        await asyncio.sleep(0.5)


async def periodic_thread_state_cleanup():
    """
    Periodically purges inactive expired thread states to prevent unbounded memory growth
    and purges expired cached downloaded Slack files.
    """
    from services.slack_client import cleanup_old_downloads

    while True:
        try:
            await asyncio.sleep(600)  # clean every 10 minutes
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
    from core.integrity_checks import AppIntegrityValidator
    from services.audit_logger import send_admin_audit_alert

    while True:
        try:
            await asyncio.sleep(43200)  # check every 12 hours
            res = await asyncio.to_thread(AppIntegrityValidator.check_audit_log_integrity)
            hmac_check = res.get("hmac_continuity", {})
            if hmac_check and not hmac_check.get("matches", True):
                logger.warning(
                    f"[SECURITY GUARD] Periodic AUDIT_HMAC_SECRET mismatch: {hmac_check.get('message')}"
                )
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure SQLite schema and WAL mode are initialized before any queries execute
    from core.database import init_db
    init_db()

    # Check worker concurrency
    concurrency_env = os.getenv("WEB_CONCURRENCY") or os.getenv("GUNICORN_WORKERS") or os.getenv("UVICORN_WORKERS") or "1"
    try:
        workers = int(concurrency_env)
    except ValueError:
        workers = 1
    if workers > 1:
        logger.critical(
            f"[CONCURRENCY] Worker concurrency is set to {workers} (via WEB_CONCURRENCY / UVICORN_WORKERS). "
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
    from core.integrity_checks import AppIntegrityValidator
    from services.audit_logger import send_admin_audit_alert

    audit_status = AppIntegrityValidator.check_audit_log_integrity()
    hmac_check = audit_status.get("hmac_continuity", {})

    if hmac_check and not hmac_check.get("matches", True):
        status = hmac_check.get("status")
        msg = hmac_check.get("message")
        logger.warning(f"[SECURITY STARTUP] AUDIT_HMAC_SECRET mismatch: {msg}")

        if not audit_status["valid"]:
            # Combined alert: secret mismatch caused chain verification failure
            err_msg = "; ".join(audit_status["errors"])
            from core.task_utils import safe_create_task

            safe_create_task(
                send_admin_audit_alert(
                    title=f"Startup Audit Integrity Alert: Secret Mismatch ({status})",
                    message="Server startup detected that AUDIT_HMAC_SECRET does not match the key used for existing audit logs, causing verification failure.",
                    details={
                        "Status": status,
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
                    title=f"AUDIT_HMAC_SECRET Discrepancy ({status})",
                    message="Server startup detected that AUDIT_HMAC_SECRET does not match the key used for existing audit logs.",
                    details={
                        "Status": status,
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

    loop_task = asyncio.create_task(keep_loop_alive()) if sys.platform == "win32" else None
    warmup_task = asyncio.create_task(warm_up_pricing_cache())
    cleanup_task = asyncio.create_task(periodic_thread_state_cleanup())
    audit_guard_task = asyncio.create_task(periodic_audit_integrity_guard())
    rag_sync_task = asyncio.create_task(rag_sync_engine.reconcile())
    try:
        yield
    finally:
        if loop_task:
            loop_task.cancel()
        warmup_task.cancel()
        cleanup_task.cancel()
        audit_guard_task.cancel()
        rag_sync_task.cancel()


app = FastAPI(
    title="Izvor Multi-Agent Slack Hub",
    description="Multi-agent orchestration platform integrating Slack Events API, LiteLLM, and MCP tools.",
    version="1.0.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

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
    credentials: HTTPBasicCredentials = Depends(admin_security),
) -> str:
    """
    Verifies HTTP Basic Auth credentials against ADMIN_USERNAME and ADMIN_PASSWORD.
    Uses secrets.compare_digest to defend against timing attacks.
    Emits non-repudiable audit events for both successful and failed attempts.
    """
    from services.audit_logger import log_audit_event

    if not ADMIN_PASSWORD:
        logger.error("ADMIN_PASSWORD is not configured in environment/.env")
        log_audit_event(
            event_type="AUTH_FAILURE",
            actor={"type": "api_client", "id": credentials.username},
            action="admin_basic_auth",
            target="/api/*",
            details={"reason": "ADMIN_PASSWORD not configured on server"},
            severity="CRITICAL",
            status="FAILED",
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Admin authentication is not configured on the server",
        )

    is_correct_username = secrets.compare_digest(
        credentials.username.encode("utf8"), ADMIN_USERNAME.encode("utf8")
    )
    is_correct_password = secrets.compare_digest(
        credentials.password.encode("utf8"), ADMIN_PASSWORD.encode("utf8")
    )

    if not (is_correct_username and is_correct_password):
        log_audit_event(
            event_type="AUTH_FAILURE",
            actor={"type": "api_client", "id": credentials.username},
            action="admin_basic_auth",
            target="/api/*",
            details={"reason": "Invalid credentials provided"},
            severity="CRITICAL",
            status="FAILED",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )

    log_audit_event(
        event_type="AUTH_SUCCESS",
        actor={"type": "admin_user", "id": credentials.username},
        action="admin_basic_auth",
        target="/api/*",
        details={"status": "Authenticated successfully"},
        severity="INFO",
        status="SUCCESS",
    )
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
    from services.rag.manifest_manager import ManifestManager
    manifest = ManifestManager()
    records = manifest.get_all_records()
    return {
        "status": "ready",
        "total_documents": len(records),
        "documents": records,
    }


@app.post("/api/rag/reconcile")
async def trigger_rag_reconciliation(admin_user: str = Depends(verify_admin_basic_auth)):
    """Triggers an on-demand reconciliation scan of rag_knowledge/."""
    from services.audit_logger import log_audit_event

    log_audit_event(
        event_type="RAG_RECONCILE_TRIGGERED",
        actor={"type": "admin_user", "id": admin_user},
        action="reconcile_rag_knowledge",
        target="rag_knowledge/",
        details={"source": "api_endpoint"},
        severity="WARNING",
        status="SUCCESS",
    )
    stats = await rag_sync_engine.reconcile()
    return {"status": "reconciliation_complete", "stats": stats}


@app.get("/api/audit/status", dependencies=[Depends(verify_admin_basic_auth)])
async def get_audit_status():
    """Returns non-repudiable audit log cryptographic integrity status and statistics."""
    from core.integrity_checks import AppIntegrityValidator

    return AppIntegrityValidator.check_audit_log_integrity()





__all__ = [
    "PROCESSED_EVENTS",
    "app",
    "process_and_respond",
    "publish_app_home",
    "slack_events",
    "slack_interactivity",
    "slack_router",
    "verify_slack_signature",
]
