import asyncio
import json
import logging
import re
import time
from typing import Any

from fastapi import BackgroundTasks, HTTPException, Request, Response
from slack_sdk.errors import SlackApiError
from starlette.requests import ClientDisconnect

import sys

from core.slack_security import verify_slack_signature as _default_verify_slack_signature
from pipeline.directive_parser import is_stop_command
from orchestration.agent_router import process_and_respond
from orchestration.thread_state import thread_state_manager
from pipeline.entity_resolver import entity_resolver
from services.slack_client import publish_app_home as _default_publish_app_home

logger = logging.getLogger("CaiLogicsBackend.SlackEvents")

# Global in-memory cache for request deduplication
PROCESSED_EVENTS: dict[tuple[str, str], float] = {}  # key: (channel_id, ts), value: timestamp


def _verify_signature(body: bytes, headers: Any) -> bool:
    main_mod = sys.modules.get("main")
    if main_mod and hasattr(main_mod, "verify_slack_signature"):
        return main_mod.verify_slack_signature(body, headers)
    return _default_verify_slack_signature(body, headers)


async def _publish_home(user_id: str) -> Any:
    from orchestration.agent_settings import sync_settings_if_modified
    sync_settings_if_modified()
    main_mod = sys.modules.get("main")
    if main_mod and hasattr(main_mod, "publish_app_home"):
        return await main_mod.publish_app_home(user_id=user_id)
    return await _default_publish_app_home(user_id=user_id)


async def _safe_publish_home(user_id: str) -> None:
    try:
        await _publish_home(user_id=user_id)
    except SlackApiError as e:
        logger.error(f"[WEBHOOK] SlackApiError publishing app home on open: {e}")
    except Exception as e:
        logger.error(f"[WEBHOOK] Error publishing app home on open: {e}")


def _get_process_and_respond() -> Any:
    main_mod = sys.modules.get("main")
    if main_mod and hasattr(main_mod, "process_and_respond"):
        return getattr(main_mod, "process_and_respond")
    from orchestration.agent_router import process_and_respond
    return process_and_respond


async def handle_slack_events(request: Request, background_tasks: BackgroundTasks) -> Any:
    """
    Primary Webhook handler receiving Events API payloads from Slack.
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
    if not _verify_signature(body_bytes, headers):
        logger.warning("Rejected unauthorized request to /slack/events")
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    # 4. Parse JSON payload
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeError, ValueError) as e:
        logger.error(f"Failed to parse Slack event JSON: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from e
    except Exception as e:
        logger.error(f"Failed to parse Slack event JSON: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from e

    # 5. Handle Slack URL Verification Challenge
    if data.get("type") == "url_verification":
        logger.info("Handling Slack url_verification challenge")
        return {"challenge": data.get("challenge")}

    # 6. Handle Event Callbacks (message.channels, app_mention)
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
        # 6a. Handle App Home Opened
        if event_type == "app_home_opened":
            user_id = event.get("user")
            tab = event.get("tab")
            if (tab == "home" or tab is None) and user_id:
                logger.info(
                    f"[WEBHOOK] Received app_home_opened event for user {user_id}. Publishing App Home view in background."
                )
                background_tasks.add_task(_safe_publish_home, user_id=user_id)
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

                # 6b. Deduplicate Slack duplicate events
                event_key = (channel_id, ts)
                if event_key in PROCESSED_EVENTS:
                    logger.info(
                        f"[WEBHOOK] Deduplicated event {event_type} for channel {channel_id}, ts {ts} (ALREADY PROCESSED)"
                    )
                    return Response(status_code=200)

                # 6c. Identify if the message is from a bot
                is_bot = (
                    event.get("bot_id")
                    or event.get("subtype") == "bot_message"
                    or (bot_user_id is not None and event.get("user") == bot_user_id)
                )

                # 6d. Extract nominations and tags ([Agent_Key])
                has_tag = False
                for match in re.finditer(r"[\[{]\s*([^\]}\r\n]+?)\s*[\]}]", raw_text):
                    tag = match.group(1)
                    if entity_resolver.resolve_candidates(tag):
                        has_tag = True
                        break

                is_app_name_mention = bool(
                    (bot_user_id and f"<@{bot_user_id}>" in raw_text)
                    or re.search(r"@?CaiLogics\b", raw_text, re.IGNORECASE)
                    or re.search(r"@app_mention\b", raw_text, re.IGNORECASE)
                )

                is_mention_or_dm = (
                    (event_type == "app_mention")
                    or (channel_id and channel_id.startswith("D"))
                    or is_app_name_mention
                )
                should_process = False

                if is_bot:
                    logger.info(
                        f"[WEBHOOK] Skipping bot message in channel {channel_id} (ts: {ts}) - internal backend loop handles agent chaining"
                    )
                    return Response(status_code=200)

                # Check if the user message is a stop command directed at the bot
                if is_stop_command(raw_text, is_mention_or_dm=is_mention_or_dm):
                    logger.info(
                        f"[WEBHOOK] Stop command received from user in channel {channel_id}, thread {thread_ts}. Stopping thread discussion."
                    )
                    PROCESSED_EVENTS[event_key] = now
                    thread_state_manager.request_stop(channel_id, thread_ts)
                    return Response(status_code=200)

                # User messages must either have a bracketed tag, be an app_mention/DM, or contain a recognized slash command
                has_slash_command = False
                try:
                    from skills import skill_registry
                    triggers = skill_registry.get_slash_triggers()
                    if triggers:
                        slash_pattern = r"(?:^|\s)(?:" + "|".join(re.escape(c) for c in triggers.keys()) + r")\b"
                        has_slash_command = bool(re.search(slash_pattern, raw_text, re.IGNORECASE))
                except Exception as e:
                    logger.debug(f"[WEBHOOK] Error checking skill_registry slash commands: {e}")

                if not has_slash_command:
                    has_slash_command = bool(
                        re.search(
                            r"(?:^|\s)/(?:grant_skill_for_all|grant-skill-for-all|grant_for_all|grant-for-all|grant_skill|grant-skill|revoke_skill_for_all|revoke-skill-for-all|revoke_for_all|revoke-for-all|revoke_constraint|revoke-constraint|revoke|list_skills|list-skills|rounds|round|next_round|next-round|nextround)\b",
                            raw_text,
                            re.IGNORECASE,
                        )
                    )

                if has_tag or is_mention_or_dm or has_slash_command:
                    should_process = True
                    if thread_state_manager.is_discussion_active(channel_id, thread_ts):
                        logger.info(
                            f"[WEBHOOK] Ongoing discussion in thread {thread_ts} detected. Interrupting discussion for new query: {raw_text[:50]}"
                        )
                        thread_state_manager.interrupt_discussion(channel_id, thread_ts)
                else:
                    logger.info(
                        f"Skipping plain user message in channel {channel_id} without mention/tag"
                    )

                if should_process:
                    # Add to processed events list to prevent double execution
                    PROCESSED_EVENTS[event_key] = now
                    logger.info(
                        f"[WEBHOOK] DISPATCHING BackgroundTask for '{event_type}' - channel: {channel_id}, thread: {thread_ts}"
                    )
                    # Dispatch agent discussion loop asynchronously and register task with thread_state_manager
                    task = asyncio.create_task(
                        _get_process_and_respond()(
                            channel_id=channel_id,
                            thread_ts=thread_ts,
                            raw_text=raw_text,
                            bot_user_id=bot_user_id,
                            is_bot_trigger=False,
                            run_internal_loop=True,
                        )
                    )

                    def _log_background_task_error(t: asyncio.Task) -> None:
                        if not t.cancelled() and t.exception():
                            logger.error(
                                f"[WEBHOOK] Background task failed for thread {(channel_id, thread_ts)}: {t.exception()}",
                                exc_info=t.exception(),
                            )

                    task.add_done_callback(_log_background_task_error)
                    thread_state_manager.set_active_task(channel_id, thread_ts, task)
                else:
                    logger.info(
                        "[WEBHOOK] Ignoring event - should_process evaluated to False"
                    )

    logger.info("[WEBHOOK] Returning 200 OK immediately to Slack")
    return Response(status_code=200)
