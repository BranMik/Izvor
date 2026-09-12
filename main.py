import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from adapters.slack import (
    PROCESSED_EVENTS,
    handle_slack_events as slack_events,
    handle_slack_interactivity as slack_interactivity,
    slack_router,
)
from core.config import AGENTS_CONFIG, LOG_LEVEL
from core.slack_security import verify_slack_signature
from orchestration.agent_router import process_and_respond
from orchestration.agent_settings import load_enabled_agents
from orchestration.thread_state import thread_state_manager
from services.pricing_service import warm_up_pricing_cache
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
    enabled = load_enabled_agents()
    logger.info(f"[STARTUP] Loaded {len(enabled)} enabled debate agents from persistent storage.")
    loop_task = asyncio.create_task(keep_loop_alive()) if sys.platform == "win32" else None
    warmup_task = asyncio.create_task(warm_up_pricing_cache())
    cleanup_task = asyncio.create_task(periodic_thread_state_cleanup())
    try:
        yield
    finally:
        if loop_task:
            loop_task.cancel()
        warmup_task.cancel()
        cleanup_task.cancel()


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


@app.get("/api/skills")
async def get_registered_skills():
    """Returns all registered skills, descriptions, and their tool schemas."""
    from skills import skill_registry
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
