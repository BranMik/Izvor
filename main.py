import asyncio
import logging
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
    LOG_LEVEL,
)
from core.slack_security import verify_slack_signature
from orchestration.agent_router import process_and_respond
from orchestration.agent_settings import load_enabled_agents
from orchestration.thread_state import thread_state_manager
from services.pricing_service import warm_up_pricing_cache
from services.rag.sync_engine import rag_sync_engine
from services.slack_client import publish_app_home

# Configure logging format
numeric_log_level = getattr(logging, LOG_LEVEL, logging.INFO)
logging.basicConfig(
    level=numeric_log_level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("CaiLogicsBackend")

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
    Periodically purges inactive expired thread states to prevent unbounded memory growth.
    """
    while True:
        try:
            await asyncio.sleep(600)  # clean every 10 minutes
            cleaned = thread_state_manager.cleanup_expired_threads()
            if cleaned > 0:
                logger.info(f"[CLEANUP] Pruned {cleaned} inactive thread state(s)")
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("[CLEANUP] Error during periodic thread state cleanup")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not ADMIN_PASSWORD:
        logger.warning(
            "[SECURITY] ADMIN_PASSWORD is not configured in environment/.env. "
            "Protected admin endpoints (/api/*) will reject all requests."
        )
    enabled = load_enabled_agents()
    logger.info(f"[STARTUP] Loaded {len(enabled)} enabled debate agents from persistent storage.")
    loop_task = asyncio.create_task(keep_loop_alive()) if sys.platform == "win32" else None
    warmup_task = asyncio.create_task(warm_up_pricing_cache())
    cleanup_task = asyncio.create_task(periodic_thread_state_cleanup())
    rag_sync_task = asyncio.create_task(rag_sync_engine.reconcile())
    try:
        yield
    finally:
        if loop_task:
            loop_task.cancel()
        warmup_task.cancel()
        cleanup_task.cancel()
        rag_sync_task.cancel()


app = FastAPI(
    title="CaiLogics Multi-Agent Slack Hub",
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
    """Basic health check endpoint."""
    return {
        "status": "healthy",
        "service": "CaiLogics Multi-Agent Hub",
        "agents": list(AGENTS_CONFIG.keys()),
    }


# HTTP Basic Authentication security scheme for admin endpoints
admin_security = HTTPBasic()


def verify_admin_basic_auth(
    credentials: HTTPBasicCredentials = Depends(admin_security),
) -> str:
    """
    Verifies HTTP Basic Auth credentials against ADMIN_USERNAME and ADMIN_PASSWORD.
    Uses secrets.compare_digest to defend against timing attacks.
    """
    if not ADMIN_PASSWORD:
        logger.error("ADMIN_PASSWORD is not configured in environment/.env")
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
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


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


@app.post("/api/rag/reconcile", dependencies=[Depends(verify_admin_basic_auth)])
async def trigger_rag_reconciliation():
    """Triggers an on-demand reconciliation scan of rag_knowledge/."""
    stats = await rag_sync_engine.reconcile()
    return {"status": "reconciliation_complete", "stats": stats}




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
