import asyncio
import json
import logging
from pathlib import Path
import re
import time
import urllib.parse

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.requests import ClientDisconnect

from core.config import AGENTS_CONFIG
from nlp.nl_patterns import resolve_agent_candidates
from orchestration.agent_router import (
    LOCKED_AGENTS,
    get_circuit_breaker_config,
    get_circuit_breaker_threshold,
    get_fixed_config,
    get_orchestration_mode,
    get_sandwich_config,
    interrupt_thread_discussion,
    is_circuit_breaker_enabled,
    is_discussion_active,
    is_stop_command,
    load_enabled_agents,
    process_and_respond,
    request_thread_stop,
    reset_enabled_agents,
    restore_from_backup,
    set_agent_enabled,
    set_circuit_breaker_enabled,
    set_circuit_breaker_threshold,
    set_fixed_config,
    set_orchestration_mode,
    set_sandwich_config,
    toggle_agent,
)
from services.pricing_service import warm_up_pricing_cache
from services.slack_client import publish_app_home
from core.slack_security import verify_slack_signature

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
    enabled = load_enabled_agents()
    logger.info(f"[STARTUP] Loaded {len(enabled)} enabled debate agents from persistent storage.")
    asyncio.create_task(keep_loop_alive())
    asyncio.create_task(warm_up_pricing_cache())


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

    # 1. Log Slack retries if present (deduplication cache handles duplicate events)
    retry_num = headers.get("x-slack-retry-num") or headers.get("X-Slack-Retry-Num")
    if retry_num:
        logger.info(f"[WEBHOOK] Received Slack retry request (X-Slack-Retry-Num: {retry_num})")

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
        # 5a. Handle App Home Opened
        if event_type == "app_home_opened":
            user_id = event.get("user")
            tab = event.get("tab")
            if tab == "home" and user_id:
                logger.info(
                    f"[WEBHOOK] Received app_home_opened event for user {user_id}. Publishing App Home view."
                )
                try:
                    await publish_app_home(user_id=user_id)
                except Exception as e:
                    logger.error(f"[WEBHOOK] Error publishing app home on open: {e}")
            return Response(status_code=200)

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

                # 5c. Extract nominations and tags ([Agent_Key])
                has_tag = False
                for match in re.finditer(r"\[([a-zA-Z0-9_]+)\]", raw_text):
                    tag = match.group(1)
                    if resolve_agent_candidates(tag):
                        has_tag = True
                        break

                # 5d. Determine processing authorization
                is_app_name_mention = False
                if bot_user_id and f"<@{bot_user_id}>" in raw_text:
                    is_app_name_mention = True
                elif re.search(r"@?CaiLogics\b", raw_text, re.IGNORECASE):
                    is_app_name_mention = True
                elif re.search(r"@app_mention\b", raw_text, re.IGNORECASE):
                    is_app_name_mention = True

                is_mention_or_dm = (
                    (event_type == "app_mention")
                    or (channel_id and channel_id.startswith("D"))
                    or is_app_name_mention
                )
                should_process = False

                if is_bot:
                    # Ignore Live Discussion Scribe note messages completely
                    event_username = event.get("username") or ""
                    if (
                        "Live Discussion Scribe" in event_username
                        or "Discussion Scribe" in event_username
                        or "Live Discussion Notes" in raw_text
                        or "Final Discussion Notes" in raw_text
                        or "Bilješke rasprave" in raw_text
                        or "Strateška sinteza" in raw_text
                        or "Strategic Synthesis" in raw_text
                    ):
                        logger.info(
                            f"Skipping Live Discussion Scribe note message in channel {channel_id}"
                        )
                    # Only process bot messages if they contain a valid nomination tag
                    elif has_tag:
                        should_process = True
                    else:
                        logger.info(
                            f"Skipping non-nomination bot message in channel {channel_id}"
                        )
                else:
                    # Check if the user message is a stop command directed at the bot
                    if is_stop_command(raw_text, is_mention_or_dm=is_mention_or_dm):
                        logger.info(
                            f"[WEBHOOK] Stop command received from user in channel {channel_id}, thread {thread_ts}. Stopping thread discussion."
                        )
                        PROCESSED_EVENTS[event_key] = now
                        request_thread_stop(channel_id, thread_ts)
                        return Response(status_code=200)

                    # User messages must either have a bracketed tag or be an app_mention/DM
                    if has_tag or is_mention_or_dm:
                        should_process = True
                        if is_discussion_active(channel_id, thread_ts):
                            logger.info(
                                f"[WEBHOOK] Ongoing discussion in thread {thread_ts} detected. Interrupting discussion for new query: {raw_text[:50]}"
                            )
                            interrupt_thread_discussion(channel_id, thread_ts)
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


@app.post("/slack/actions")
@app.post("/slack/interactivity")
async def slack_interactivity(request: Request, background_tasks: BackgroundTasks):
    """
    Handles Slack Block Kit interactivity (radio button toggles, buttons, etc.) from the App Home page.
    """
    headers = request.headers
    try:
        body_bytes = await request.body()
    except ClientDisconnect:
        logger.info("Slack client disconnected before interactivity body could be read.")
        return Response(status_code=200)

    if not verify_slack_signature(body_bytes, headers):
        logger.warning("Rejected unauthorized request to /slack/actions")
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    payload = None
    body_str = body_bytes.decode("utf-8", errors="replace")

    # 1. Parse URL-encoded Slack payload parameter (Standard Slack interactivity webhook format)
    if "payload=" in body_str:
        try:
            parsed_form = urllib.parse.parse_qs(body_str)
            if "payload" in parsed_form and parsed_form["payload"]:
                payload = json.loads(parsed_form["payload"][0])
        except Exception as e:
            logger.debug(f"[INTERACTIVITY] parse_qs extraction error: {e}")

    # 2. Try parsing request.form()
    if not payload:
        try:
            form_data = await request.form()
            if "payload" in form_data:
                payload = json.loads(form_data["payload"])
        except Exception:
            pass

    # 3. Direct JSON payload fallback (used in unit tests / direct API callers)
    if not payload:
        try:
            payload = json.loads(body_str)
        except Exception as e:
            logger.error(f"Failed to parse interactivity payload: {e}")
            raise HTTPException(status_code=400, detail="Invalid payload") from e

    payload_type = payload.get("type")
    if payload_type == "block_actions":
        user_id = None
        if isinstance(payload.get("user"), dict):
            user_id = payload.get("user", {}).get("id")
        elif isinstance(payload.get("user"), str):
            user_id = payload.get("user")
        if not user_id and isinstance(payload.get("view"), dict):
            user_id = payload.get("view", {}).get("user", {}).get("id")

        actions = payload.get("actions", [])
        view_state = payload.get("view", {}).get("state", {}).get("values", {})

        for action in actions:
            action_id = action.get("action_id", "")
            if action_id == "toggle_circuit_breaker_radio":
                selected_option = action.get("selected_option", {})
                val = selected_option.get("value", "")
                set_circuit_breaker_enabled(val == "cb:on")
            elif action_id == "set_circuit_breaker_threshold_input":
                val_str = action.get("value", "")
                try:
                    val_clean = str(val_str).strip().replace("$", "").replace(",", ".")
                    val_float = float(val_clean)
                    if val_float >= 0:
                        set_circuit_breaker_threshold(val_float)
                except (ValueError, TypeError):
                    logger.warning(
                        f"[INTERACTIVITY] Could not parse threshold from input: '{val_str}'"
                    )
            elif action_id == "save_circuit_breaker_threshold_button":
                val_str = ""
                # Search dynamically for the action_id inside any block in view_state
                if isinstance(view_state, dict):
                    for block_id, block_val in view_state.items():
                        if isinstance(block_val, dict) and "set_circuit_breaker_threshold_input" in block_val:
                            val_str = block_val["set_circuit_breaker_threshold_input"].get("value", "")
                            if val_str:
                                break
                    if not val_str:
                        block_vals = view_state.get("circuit_breaker_threshold_block", {})
                        if isinstance(block_vals, dict):
                            val_obj = block_vals.get("set_circuit_breaker_threshold_input", {})
                            if isinstance(val_obj, dict):
                                val_str = val_obj.get("value", "")
                try:
                    val_clean = str(val_str).strip().replace("$", "").replace(",", ".")
                    val_float = float(val_clean)
                    if val_float >= 0:
                        set_circuit_breaker_threshold(val_float)
                except (ValueError, TypeError):
                    logger.warning(
                        f"[INTERACTIVITY] Could not parse threshold from button save: '{val_str}'"
                    )
            elif action_id.startswith("set_circuit_breaker_preset:"):
                val_str = action_id.split(":", 1)[1]
                try:
                    val_float = float(val_str)
                    set_circuit_breaker_threshold(val_float)
                except (ValueError, TypeError):
                    pass
            elif action_id == "select_orchestration_mode":
                selected_option = action.get("selected_option", {})
                val = selected_option.get("value", "")
                if val.startswith("mode:"):
                    mode = val.split(":", 1)[1]
                    set_orchestration_mode(mode)
            elif action_id == "save_sandwich_chains_button":
                start_val = ""
                end_val = ""
                if isinstance(view_state, dict):
                    for block_id, block_val in view_state.items():
                        if isinstance(block_val, dict):
                            if "set_start_chain_input" in block_val:
                                start_val = block_val["set_start_chain_input"].get("value") or ""
                            if "set_end_chain_input" in block_val:
                                end_val = block_val["set_end_chain_input"].get("value") or ""
                start_chain = [k.strip() for k in start_val.split(",") if k.strip()]
                end_chain = [k.strip() for k in end_val.split(",") if k.strip()]
                set_sandwich_config(start_chain, end_chain)
            elif action_id == "clear_sandwich_chains_button":
                set_sandwich_config([], [])
            elif action_id == "set_start_chain_input":
                curr_sandwich = get_sandwich_config()
                val_str = action.get("value", "")
                start_chain = [k.strip() for k in val_str.split(",") if k.strip()]
                set_sandwich_config(start_chain, curr_sandwich.get("end_chain", []))
            elif action_id == "set_end_chain_input":
                curr_sandwich = get_sandwich_config()
                val_str = action.get("value", "")
                end_chain = [k.strip() for k in val_str.split(",") if k.strip()]
                set_sandwich_config(curr_sandwich.get("start_chain", []), end_chain)
            elif action_id == "save_fixed_chain_button":
                fixed_val = ""
                if isinstance(view_state, dict):
                    for block_id, block_val in view_state.items():
                        if isinstance(block_val, dict) and "set_fixed_chain_input" in block_val:
                            fixed_val = block_val["set_fixed_chain_input"].get("value") or ""
                chain = [k.strip() for k in fixed_val.split(",") if k.strip()]
                set_fixed_config(chain)
            elif action_id == "clear_fixed_chain_button":
                set_fixed_config([])
            elif action_id == "set_fixed_chain_input":
                val_str = action.get("value", "")
                chain = [k.strip() for k in val_str.split(",") if k.strip()]
                set_fixed_config(chain)
            elif action_id.startswith("toggle_agent_radio:"):
                selected_option = action.get("selected_option", {})
                val = selected_option.get("value", "")
                if ":" in val:
                    agent_key, state = val.split(":", 1)
                    set_agent_enabled(agent_key, state == "on")
            elif action_id.startswith("toggle_agent:"):
                agent_key = action.get("value")
                if agent_key:
                    toggle_agent(agent_key)
            elif action_id == "bulk_enable_all":
                reset_enabled_agents()
            elif action_id in ("bulk_disable_all", "bulk_disable_optional"):
                for key in AGENTS_CONFIG:
                    set_agent_enabled(key, False)
            elif action_id == "restore_from_backup":
                success = restore_from_backup()
                logger.info(
                    f"[INTERACTIVITY] restore_from_backup invoked by user {user_id}: success={success}"
                )

        if user_id:
            try:
                await publish_app_home(user_id=user_id)
            except Exception as e:
                logger.error(f"[INTERACTIVITY] Failed to publish App Home for {user_id}: {e}")

    return Response(status_code=200)

