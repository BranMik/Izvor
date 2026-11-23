from collections import OrderedDict
import json
import logging
import re
import threading
import time
from typing import Any

from fastapi import BackgroundTasks, HTTPException, Request, Response
from starlette.requests import ClientDisconnect

from core.database import get_connection, get_db_transaction
from core.slack_security import verify_slack_signature
from orchestration.agent_router import process_and_respond
from orchestration.thread_state import thread_state_manager
from pipeline.directive_parser import is_stop_command
from pipeline.entity_resolver import entity_resolver
from services.slack.views import safe_publish_home as _safe_publish_home

logger = logging.getLogger("IzvorBackend.SlackEvents")


class EventDeduplicationCache:
    """
    Time-to-live (TTL) bounded cache for Slack event deduplication.
    Pure SQLite state engine backed by table `event_dedup`.
    """

    def __init__(
        self,
        ttl_seconds: float = 3600.0,
        maxsize: int = 2000,
        db_evict_interval: float = 60.0,
    ):
        self.ttl_seconds = ttl_seconds
        self.maxsize = maxsize
        self.db_evict_interval = db_evict_interval
        self._last_db_evict: float = 0.0
        self._fallback_lock = threading.RLock()
        self._fallback_cache: OrderedDict[str, float] = OrderedDict()

    def _fallback_evict_locked(self, now: float) -> None:
        cutoff = now - self.ttl_seconds
        stale = [k for k, ts in self._fallback_cache.items() if ts < cutoff]
        for k in stale:
            self._fallback_cache.pop(k, None)
        while len(self._fallback_cache) > self.maxsize:
            self._fallback_cache.popitem(last=False)

    def _format_key(self, key: Any) -> str:
        if isinstance(key, (tuple, list)):
            return f"{key[0]}:{key[1]}"
        return str(key)

    def _evict_expired(self, force: bool = False) -> None:
        now = time.time()
        if force or (now - self._last_db_evict >= self.db_evict_interval):
            self._last_db_evict = now
            cutoff = now - self.ttl_seconds
            try:
                with get_db_transaction(immediate=True) as conn:
                    conn.execute("DELETE FROM event_dedup WHERE received_at < ?", (cutoff,))
            except Exception as e:  # noqa: BLE001
                logger.debug("Event dedup periodic eviction error: %s", e)

    def _enforce_maxsize(self, conn: Any) -> None:
        cursor = conn.execute("SELECT COUNT(*) FROM event_dedup")
        row = cursor.fetchone()
        count = int(row[0]) if row else 0
        if count > self.maxsize:
            num_to_evict = max(1, int(count * 0.2))
            conn.execute(
                """
                DELETE FROM event_dedup
                WHERE event_id IN (
                    SELECT event_id FROM event_dedup ORDER BY received_at ASC LIMIT ?
                )
                """,
                (num_to_evict,),
            )

    def claim_event(self, key: Any) -> bool:
        """
        Atomically claims an event in SQLite event_dedup table via INSERT OR IGNORE.
        Returns True if the event is new (claim acquired), False if duplicate (already claimed).
        """
        now = time.time()
        self._evict_expired()
        event_id = self._format_key(key)
        cutoff = now - self.ttl_seconds

        try:
            with get_db_transaction(immediate=True) as conn:
                conn.execute(
                    "DELETE FROM event_dedup WHERE event_id = ? AND received_at < ?",
                    (event_id, cutoff),
                )
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO event_dedup (event_id, received_at) VALUES (?, ?)",
                    (event_id, float(now)),
                )
                if cursor.rowcount > 0:
                    self._enforce_maxsize(conn)
                    return True
                return False
        except Exception as e:  # noqa: BLE001
            logger.warning("Event dedup DB error, falling back to in-memory LRU dedup: %s", e)
            with self._fallback_lock:
                self._fallback_evict_locked(now)
                if event_id in self._fallback_cache:
                    self._fallback_cache.move_to_end(event_id)
                    return False
                self._fallback_cache[event_id] = float(now)
                self._fallback_evict_locked(now)
                return True

    def __contains__(self, key: Any) -> bool:
        self._evict_expired()
        event_id = self._format_key(key)
        now = time.time()
        cutoff = now - self.ttl_seconds
        try:
            with get_connection() as conn:
                cursor = conn.execute(
                    "SELECT 1 FROM event_dedup WHERE event_id = ? AND received_at >= ?",
                    (event_id, cutoff),
                )
                if cursor.fetchone() is not None:
                    return True
        except Exception as e:  # noqa: BLE001
            logger.debug("Event dedup lookup error: %s", e)
        with self._fallback_lock:
            self._fallback_evict_locked(now)
            return event_id in self._fallback_cache

    def __getitem__(self, key: Any) -> float:
        self._evict_expired()
        event_id = self._format_key(key)
        now = time.time()
        cutoff = now - self.ttl_seconds
        try:
            with get_connection() as conn:
                cursor = conn.execute(
                    "SELECT received_at FROM event_dedup WHERE event_id = ? AND received_at >= ?",
                    (event_id, cutoff),
                )
                row = cursor.fetchone()
                if row is not None:
                    return float(row["received_at"])
        except Exception as e:  # noqa: BLE001
            logger.debug("Event dedup getitem error: %s", e)
        with self._fallback_lock:
            self._fallback_evict_locked(now)
            if event_id in self._fallback_cache:
                return float(self._fallback_cache[event_id])
        raise KeyError(key)

    def get(self, key: Any, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def __setitem__(self, key: Any, value: float) -> None:
        self._evict_expired()
        event_id = self._format_key(key)
        try:
            with get_db_transaction(immediate=True) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO event_dedup (event_id, received_at) VALUES (?, ?)",
                    (event_id, float(value)),
                )
                self._enforce_maxsize(conn)
        except Exception as e:  # noqa: BLE001
            logger.debug("Event dedup insert error: %s", e)

    def __len__(self) -> int:
        self._evict_expired()
        cutoff = time.time() - self.ttl_seconds
        try:
            with get_connection() as conn:
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM event_dedup WHERE received_at >= ?",
                    (cutoff,),
                )
                row = cursor.fetchone()
                return int(row[0]) if row else 0
        except Exception as e:  # noqa: BLE001
            logger.debug("Event dedup len error: %s", e)
            return 0

    def clear(self) -> None:
        try:
            with get_db_transaction(immediate=True) as conn:
                conn.execute("DELETE FROM event_dedup")
        except Exception as e:  # noqa: BLE001
            logger.debug("Event dedup clear error: %s", e)
        with self._fallback_lock:
            self._fallback_cache.clear()

    def pop(self, key: Any, default: Any = None) -> Any:
        event_id = self._format_key(key)
        with self._fallback_lock:
            fallback_val = self._fallback_cache.pop(event_id, None)
        try:
            with get_db_transaction(immediate=True) as conn:
                cursor = conn.execute("SELECT received_at FROM event_dedup WHERE event_id = ?", (event_id,))
                row = cursor.fetchone()
                if row is not None:
                    conn.execute("DELETE FROM event_dedup WHERE event_id = ?", (event_id,))
                    return float(row["received_at"])
        except Exception as e:  # noqa: BLE001
            logger.debug("Event dedup pop error: %s", e)
        return fallback_val if fallback_val is not None else default


_ALLOWED_MESSAGE_SUBTYPES: frozenset[str | None] = frozenset({None, "file_share", "thread_broadcast"})


# Global cache for request deduplication
PROCESSED_EVENTS: EventDeduplicationCache = EventDeduplicationCache(ttl_seconds=600.0, maxsize=2000)

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


def is_explicit_app_mention(raw_text: str, bot_user_id: str | None = None) -> bool:
    """
    Checks if raw_text contains an explicit mention of the app.
    Requires an explicit '@' prefix (@Izvor, @app_mention)
    or Slack user mention format (<@bot_user_id>) so common nouns
    (like Croatian 'izvor') do not trigger false mentions.
    """
    if not raw_text:
        return False
    if bot_user_id and f"<@{bot_user_id}>" in raw_text:
        return True
    return bool(
        re.search(r"@Izvor\b", raw_text, re.IGNORECASE)
        or re.search(r"@app_mention\b", raw_text, re.IGNORECASE)
    )


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
        authorizations = data.get("authorizations")
        bot_user_id = (
            authorizations[0].get("user_id")
            if (authorizations and isinstance(authorizations, list) and len(authorizations) > 0)
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
            raw_text = event.get("text") or ""
            subtype = event.get("subtype")
            files = event.get("files") or []
            has_files = bool(isinstance(files, list) and files)

            logger.info(
                f"[WEBHOOK] Received {event_type} (subtype={subtype}, has_files={has_files}) - channel: {channel_id}, ts: {ts}, thread_ts: {thread_ts}"
            )

            # 6c-pre. Reject bot messages and non-user message subtypes before processing
            is_bot = (
                event.get("bot_id")
                or subtype == "bot_message"
                or (bot_user_id is not None and event.get("user") == bot_user_id)
            )
            if is_bot:
                logger.info(
                    f"[WEBHOOK] Skipping bot message in channel {channel_id} (ts: {ts}) - internal backend loop handles agent chaining"
                )
                return Response(status_code=200)

            if subtype not in _ALLOWED_MESSAGE_SUBTYPES:
                logger.info(
                    f"[WEBHOOK] Skipping disallowed message subtype '{subtype}' in channel {channel_id} (ts: {ts})"
                )
                return Response(status_code=200)

            if channel_id and ts and (raw_text or has_files):
                # 6b. Deduplicate Slack duplicate events (fast check)
                event_key = (channel_id, ts)
                if event_key in PROCESSED_EVENTS:
                    logger.info(
                        f"[WEBHOOK] Deduplicated event {event_type} for channel {channel_id}, ts {ts} (ALREADY PROCESSED)"
                    )
                    return Response(status_code=200)

                # 6d. Extract nominations and tags (/[Agent_Key] or /{Agent_Key})
                has_tag = False
                for match in re.finditer(r"/\s*[\[{]\s*([^\]}\r\n]+?)\s*[\]}]", raw_text):
                    tag = match.group(1)
                    if entity_resolver.resolve_candidates(tag):
                        has_tag = True
                        break

                is_app_name_mention = is_explicit_app_mention(raw_text, bot_user_id)

                is_mention_or_dm = (
                    (event_type == "app_mention") or (channel_id and channel_id.startswith("D")) or is_app_name_mention
                )
                should_process = False

                # Check if the user message is a stop command directed at the bot
                if is_stop_command(raw_text, is_mention_or_dm=is_mention_or_dm):
                    if not PROCESSED_EVENTS.claim_event(event_key):
                        logger.info(
                            f"[WEBHOOK] Deduplicated stop command for channel {channel_id}, ts {ts} (ALREADY PROCESSED)"
                        )
                        return Response(status_code=200)
                    logger.info(
                        f"[WEBHOOK] Stop command received from user in channel {channel_id}, thread {thread_ts}. Stopping thread discussion."
                    )
                    thread_state_manager.request_stop(channel_id, thread_ts)
                    return Response(status_code=200)

                # User messages must either have a bracketed tag, be an app_mention/DM, or contain a recognized slash command
                skills_slash_re = _get_skills_slash_regex()
                has_slash_command = bool(skills_slash_re and skills_slash_re.search(raw_text))

                if has_tag or is_mention_or_dm or has_slash_command:
                    should_process = True
                else:
                    logger.info(f"Skipping plain user message in channel {channel_id} without mention/tag")

                if should_process:
                    # Atomically claim event in processed events list to prevent double execution
                    if not PROCESSED_EVENTS.claim_event(event_key):
                        logger.info(
                            f"[WEBHOOK] Deduplicated event {event_type} for channel {channel_id}, ts {ts} (ALREADY PROCESSED)"
                        )
                        return Response(status_code=200)

                    if thread_state_manager.is_discussion_active(channel_id, thread_ts):
                        logger.info(
                            f"[WEBHOOK] Ongoing discussion in thread {thread_ts} detected. Interrupting discussion for new query (len: {len(raw_text)})"
                        )
                        thread_state_manager.interrupt_discussion(channel_id, thread_ts)

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
                        ),
                    )
                else:
                    logger.info("[WEBHOOK] Ignoring event - should_process evaluated to False")

    logger.info("[WEBHOOK] Returning 200 OK immediately to Slack")
    return Response(status_code=200)
