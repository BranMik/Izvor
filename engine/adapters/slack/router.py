from typing import Any

from fastapi import APIRouter, BackgroundTasks, Request

from adapters.slack.events import handle_slack_events
from adapters.slack.interactivity import handle_slack_interactivity

slack_router = APIRouter(tags=["Slack"])


@slack_router.post("/events")
async def slack_events_endpoint(request: Request, background_tasks: BackgroundTasks) -> Any:
    """Primary webhook endpoint receiving Events API payloads from Slack."""
    return await handle_slack_events(request, background_tasks)


@slack_router.post("/actions")
@slack_router.post("/interactivity")
async def slack_interactivity_endpoint(request: Request, background_tasks: BackgroundTasks) -> Any:
    """Handles Slack Block Kit interactivity from the App Home page."""
    return await handle_slack_interactivity(request, background_tasks)
