import asyncio
import json
import logging
import re
import sys
import time
from typing import Any

from fastapi import BackgroundTasks, HTTPException, Request, Response
from slack_sdk.errors import SlackApiError
from starlette.requests import ClientDisconnect

from core.database import get_connection, get_db_transaction
from core.slack_security import verify_slack_signature
from orchestration.agent_router import process_and_respond
from orchestration.thread_state import thread_state_manager
from pipeline.directive_parser import is_stop_command
from pipeline.entity_resolver import entity_resolver
from services.slack_client import publish_app_home

logger = logging.getLogger("CaiLogicsBackend.SlackEvents")


class EventDeduplicationCache(dict):
    """
    Time-to-live (TTL) bounded cache for Slack event deduplication.
    Backed by SQLite table `event_dedup` with in-memory caching for zero latency.
    """

    def __init__(self, ttl_seconds: float = 600.0, maxsize: int = 2000):
        super().__init__()
        self.ttl_seconds = ttl_seconds
        self.maxsize = maxsize

    def _format_key(self, key: Any) -> str:
        if isinstance(key, (tuple, list)):
            return f"{key[0]}:{key[1]}"
        return str(key)

    def _evict_expired(self) -> None:
        now = time.time()
        expired = [k for k, v in self.items() if now - v > self.ttl_seconds]
        for k in expired:
            super().pop(k, None)
        if len(self) > self.maxsize:
            super().clear()
        # Evict from SQLite table event_dedup
        cutoff = now - self.ttl_seconds
        try:
            with get_db_transaction(immediate=True) as conn:
                conn.execute("DELETE FROM event_dedup WHERE received_at < ?", (cutoff,))
        except Exception:
            pass

    def __contains__(self, key: Any) -> bool:
        self._evict_expired()
        if super().__contains__(key):
            return True
        event_id = self._format_key(key)
        try:
            cutoff = time.time() - self.ttl_seconds
            with get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT received_at FROM event_dedup WHERE event_id = ? AND received_at >= ?",
                    (event_id, cutoff),
                )
                row = cursor.fetchone()
                if row:
                    super().__setitem__(key, float(row["received_at"]))
                    return True
        except Exception:
            pass
        return False

    def __setitem__(self, key: Any, value: float) -> None:
        self._evict_expired()
        super().__setitem__(key, value)
        event_id = self._format_key(key)
        try:
            with get_db_transaction(immediate=True) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO event_dedup (event_id, received_at) VALUES (?, ?)",
                    (event_id, float(value)),
                )
        except Exception:
            pass

    def clear(self) -> None:
        super().clear()
        try:
            with get_db_transaction(immediate=True) as conn:
                conn.execute("DELETE FROM event_dedup")
        except Exception:
            pass

    def pop(self, key: Any, default: Any = None) -> Any:
        val = super().pop(key, default)
        event_id = self._format_key(key)
        try:
            with get_db_transaction(immediate=True) as conn:
                conn.execute("DELETE FROM event_dedup WHERE event_id = ?", (event_id,))
        except Exception:
            pass
        return val


# Global cache for request deduplication
PROCESSED_EVENTS: EventDeduplicationCache = EventDeduplicationCache(ttl_seconds=600.0, maxsize=2000)

_FALLBACK_SLASH_RE = re.compile(
    r"(?:^|\s)/(?:grant_skill_for_all|grant-skill-for-all|grant_for_all|grant-for-all|grant_skill|grant-skill|revoke_skill_for_all|revoke-skill-for-all|revoke_for_all|revoke-for-all|revoke_constraint|revoke-constraint|revoke|list_skills|list-skills|rounds|round|next_round|next-round|nextround)\b",
    re.IGNORECASE,
)
_SKILLS_SLASH_CACHE: tuple[tuple[str, ...], re.Pattern] | None = None


def _get_skills_slash_regex() -> re.Pattern | None:
    global _SKILLS_SLASH_CACHE
    try:
        from core.registry import get_skill_registry
        skill_registry = get_skill_registry()

        triggers = tuple(sorted(skill_registry.get_slash_triggers()))
        if not triggers:
            return None
        if _SKILLS_SLASH_CACHE is not None and _SKILLS_SLASH_CACHE[0] == triggers:
            return _SKILLS_SLASH_CACHE[1]
        pattern = re.compile(
            r"(?:^|\s)(?:" + "|".join(re.escape(c) for c in triggers) + r")\b",
            re.IGNORECASE,
        )
        _SKILLS_SLASH_CACHE = (triggers, pattern)
        return pattern
    except (ImportError, AttributeError, KeyError, TypeError, re.error) as e:
        logger.debug(f"[WEBHOOK] Error checking skill_registry slash commands: {e}")
        return None


async def _safe_publish_home(user_id: str) -> None:
    try:
        from orchestration.agent_settings import sync_settings_if_modified

        sync_settings_if_modified()
        await publish_app_home(user_id=user_id)
    except SlackApiError as e:
        logger.error(f"[WEBHOOK] SlackApiError publishing app home on open: {e}")
    except Exception as e:  # noqa: BLE001 - Webhook event handler isolates app home publishing failure
        logger.error(f"[WEBHOOK] Error publishing app home on open: {e}")


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
    if not verify_slack_signature(body_bytes, headers):
        logger.warning("Rejected unauthorized request to /slack/events")
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    # 4. Parse JSON payload
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeError, ValueError) as e:
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
                now = time.time()
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
                skills_slash_re = _get_skills_slash_regex()
                has_slash_command = bool(
                    (skills_slash_re and skills_slash_re.search(raw_text))
                    or _FALLBACK_SLASH_RE.search(raw_text)
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
                    # Dispatch agent discussion loop asynchronously via thread_state_manager
                    thread_state_manager.dispatch_discussion_task(
                        channel_id=channel_id,
                        thread_ts=thread_ts,
                        coro=process_and_respond(
                            channel_id=channel_id,
                            thread_ts=thread_ts,
                            raw_text=raw_text,
                            bot_user_id=bot_user_id,
                            is_bot_trigger=False,
                            run_internal_loop=True,
                            user_id=event.get("user"),
                        )
                    )
                else:
                    logger.info(
                        "[WEBHOOK] Ignoring event - should_process evaluated to False"
                    )

    logger.info("[WEBHOOK] Returning 200 OK immediately to Slack")
    return Response(status_code=200)
