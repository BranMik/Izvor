import asyncio
import logging
from pathlib import Path
import re
import time

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.requests import ClientDisconnect

from agent_router import process_and_respond
from config import AGENTS_CONFIG
from slack_security import verify_slack_signature

# Global in-memory cache for request deduplication
PROCESSED_EVENTS = {}  # key: (channel_id, ts), value: timestamp

# Configure logging format
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("CaiLogicsBackend")

app = FastAPI(
    title="CaiLogics Multi-Agent Slack Hub",
    description="Multi-agent orchestration platform integrating Slack Events API, LiteLLM, and MCP tools.",
    version="1.0.0",
)

# Definira točnu putanju do mape projekta gdje se nalazi main.py
BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def keep_loop_alive():
    """
    Heartbeat task to prevent Windows ProactorEventLoop from freezing
    when there are no active incoming HTTP sockets.
    """
    while True:
        await asyncio.sleep(0.5)


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(keep_loop_alive())


@app.get("/")
@app.get("/health")
async def health_check():
    """Basic health check endpoint."""
    return {
        "status": "healthy",
        "service": "CaiLogics Multi-Agent Hub",
        "agents": list(AGENTS_CONFIG.keys()),
    }


@app.post("/slack/events")
async def slack_events(request: Request, background_tasks: BackgroundTasks):
    """
    Primary Webhook endpoint receiving Events API payloads from Slack.
    Handles URL verification challenges, security signature checks, deduplication,
    retries handling, and triggers agent completion.
    """
    headers = request.headers

    # 1. Immediately acknowledge and ignore Slack retries before reading body
    # This prevents reading the body of a client connection that Slack has already aborted.
    retry_num = headers.get("x-slack-retry-num") or headers.get("X-Slack-Retry-Num")
    if retry_num:
        logger.info(f"Ignoring Slack retry request (X-Slack-Retry-Num: {retry_num})")
        return Response(status_code=200)

    # 2. Read request body bytes safely
    try:
        body_bytes = await request.body()
    except ClientDisconnect:
        logger.info("Slack client disconnected before body could be read.")
        return Response(status_code=200)

    # 3. Verify Slack HMAC Signature
    if not verify_slack_signature(body_bytes, headers):
        logger.warning("Rejected unauthorized request to /slack/events")
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    # 4. Parse JSON payload
    try:
        data = await request.json()
    except Exception as e:
        logger.error(f"Failed to parse Slack event JSON: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from e

    # 4. Handle Slack URL Verification Challenge
    if data.get("type") == "url_verification":
        logger.info("Handling Slack url_verification challenge")
        return {"challenge": data.get("challenge")}

    # 5. Handle Event Callbacks (message.channels, app_mention)
    if data.get("type") == "event_callback":
        event = data.get("event", {})
        # Slack Events API uses 'authorizations' in modern apps, fallback to 'authed_users'
        authorizations = data.get("authorizations")
        if (
            authorizations
            and isinstance(authorizations, list)
            and len(authorizations) > 0
        ):
            bot_user_id = authorizations[0].get("user_id")
        else:
            bot_user_id = (
                data.get("authed_users", [None])[0]
                if data.get("authed_users")
                else None
            )

        event_type = event.get("type")
        if event_type in ["message", "app_mention"]:
            channel_id = event.get("channel")
            ts = event.get("ts")
            thread_ts = event.get("thread_ts") or ts
            raw_text = event.get("text", "")

            logger.info(
                f"[WEBHOOK] Received {event_type} - channel: {channel_id}, ts: {ts}, thread_ts: {thread_ts}"
            )

            if channel_id and ts and raw_text:
                # Deduplication cache cleanup (prevent memory leaks)
                now = time.time()
                if len(PROCESSED_EVENTS) > 1000:
                    # Remove items older than 10 minutes
                    expired = [k for k, v in PROCESSED_EVENTS.items() if now - v > 600]
                    for k in expired:
                        PROCESSED_EVENTS.pop(k, None)
                    # If still too large, clear completely
                    if len(PROCESSED_EVENTS) > 1000:
                        PROCESSED_EVENTS.clear()

                # 5a. Deduplicate Slack duplicate events
                event_key = (channel_id, ts)
                if event_key in PROCESSED_EVENTS:
                    logger.info(
                        f"[WEBHOOK] Deduplicated event {event_type} for channel {channel_id}, ts {ts} (ALREADY PROCESSED)"
                    )
                    return Response(status_code=200)

                # 5b. Identify if the message is from a bot
                is_bot = (
                    event.get("bot_id")
                    or event.get("subtype") == "bot_message"
                    or (bot_user_id is not None and event.get("user") == bot_user_id)
                )

                # 5c. Extract nominations and tags
                has_tag = False
                tag_match = re.search(r"\[([a-zA-Z0-9_]+)\]", raw_text)
                if tag_match:
                    tag = tag_match.group(1)
                    if any(tag.lower() == k.lower() for k in AGENTS_CONFIG):
                        has_tag = True

                has_name_mention = False
                for key in AGENTS_CONFIG:
                    if re.search(r"@?" + re.escape(key), raw_text, re.IGNORECASE):
                        has_name_mention = True
                        break
                    persona_name = AGENTS_CONFIG[key].get("username", "")
                    if persona_name and re.search(
                        r"@?" + re.escape(persona_name), raw_text, re.IGNORECASE
                    ):
                        has_name_mention = True
                        break

                # 5d. Determine processing authorization
                is_mention_or_dm = (event_type == "app_mention") or (
                    channel_id and channel_id.startswith("D")
                )
                should_process = False

                if is_bot:
                    # Only process bot messages if they contain a valid nomination tag
                    if has_tag:
                        should_process = True
                    else:
                        logger.info(
                            f"Skipping non-nomination bot message in channel {channel_id}"
                        )
                else:
                    # User messages must either have a tag, name mention, or be an app_mention/DM
                    if has_tag or has_name_mention or is_mention_or_dm:
                        should_process = True
                    else:
                        logger.info(
                            f"Skipping plain user message in channel {channel_id} without mention/tag"
                        )

                if should_process:
                    # Add to processed events list to prevent double execution
                    PROCESSED_EVENTS[event_key] = now
                    logger.info(
                        f"[WEBHOOK] DISPATCHING BackgroundTask for '{event_type}' - channel: {channel_id}, thread: {thread_ts}, is_bot: {is_bot}"
                    )
                    # Use FastAPI BackgroundTasks to run agent processing after returning 200 OK
                    background_tasks.add_task(
                        process_and_respond,
                        channel_id=channel_id,
                        thread_ts=thread_ts,
                        raw_text=raw_text,
                        bot_user_id=bot_user_id,
                        is_bot_trigger=is_bot,
                    )
                else:
                    logger.info(
                        f"[WEBHOOK] Ignoring event - should_process evaluated to False (is_bot: {is_bot})"
                    )

    logger.info("[WEBHOOK] Returning 200 OK immediately to Slack")
    # Return HTTP 200 OK after completion
    return Response(status_code=200)
