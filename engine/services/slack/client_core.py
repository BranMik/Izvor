import logging
import threading
import time
from typing import Any

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from core.config import (
    USAGE_CHANNEL_ID,
    USAGE_CHANNEL_NAME,
    get_slack_bot_token,
)
from services.rate_limiter import slack_rate_limiter

logger = logging.getLogger(__name__)

# Lazy, token-aware async web client state
_client_lock = threading.RLock()
_raw_slack_async_client: AsyncWebClient | None = None
_raw_slack_async_client_token: str | None = None
_slack_async_client_override: Any = None


def configure_slack_async_client(client: Any) -> None:
    """Configures or clears an in-memory AsyncWebClient override for test isolation."""
    global _slack_async_client_override
    _slack_async_client_override = client


def reset_slack_caches() -> None:
    """Clears in-memory Slack channel name and resolved usage channel ID caches."""
    global _RESOLVED_USAGE_CHANNEL_ID
    CHANNEL_NAME_CACHE.clear()
    _RESOLVED_USAGE_CHANNEL_ID = None


def reset_slack_async_client() -> None:
    """Resets the in-memory AsyncWebClient override and cached token-bound client."""
    global _raw_slack_async_client, _raw_slack_async_client_token
    configure_slack_async_client(None)
    with _client_lock:
        _raw_slack_async_client = None
        _raw_slack_async_client_token = None
    reset_slack_caches()


def get_slack_async_client() -> AsyncWebClient:
    """Returns configured test override or a lazily constructed, token-aware AsyncWebClient."""
    if _slack_async_client_override is not None:
        return _slack_async_client_override
    active_token = get_slack_bot_token()
    global _raw_slack_async_client, _raw_slack_async_client_token
    with _client_lock:
        if _raw_slack_async_client is None or _raw_slack_async_client_token != active_token:
            _raw_slack_async_client = AsyncWebClient(token=active_token)
            _raw_slack_async_client_token = active_token
        return _raw_slack_async_client


class _SlackAsyncClientProxy:
    """Dynamic proxy delegating all attribute lookups to the canonical or overridden client."""

    def __getattr__(self, name: str) -> Any:
        return getattr(get_slack_async_client(), name)

    def __repr__(self) -> str:
        return repr(get_slack_async_client())

    def __bool__(self) -> bool:
        return bool(get_slack_async_client())


# Canonical singleton proxy instance
slack_async_client: Any = _SlackAsyncClientProxy()

# In-memory caches for channel metadata
CHANNEL_CACHE_TTL_SECONDS = 3600.0
CHANNEL_NAME_CACHE: dict[str, tuple[str, float]] = {}
_RESOLVED_USAGE_CHANNEL_ID: str | None = None


async def get_channel_name(channel_id: str) -> str:
    """
    Fetches the human-readable channel name (e.g. '#general') with in-memory caching and TTL.
    """
    if not channel_id:
        return "#unknown"
    now = time.time()
    if channel_id in CHANNEL_NAME_CACHE:
        name, cached_at = CHANNEL_NAME_CACHE[channel_id]
        if now - cached_at < CHANNEL_CACHE_TTL_SECONDS:
            return name

    bot_token = get_slack_bot_token()
    if not bot_token or bot_token == "xoxb-your-bot-user-oauth-token":
        name = f"#{channel_id}"
        CHANNEL_NAME_CACHE[channel_id] = (name, now)
        return name

    try:
        await slack_rate_limiter.acquire()
        client = get_slack_async_client()
        res = await client.conversations_info(channel=channel_id)
        ch_info = res.get("channel", {})
        name = ch_info.get("name")
        if name:
            formatted = f"#{name}"
            CHANNEL_NAME_CACHE[channel_id] = (formatted, now)
            return formatted
    except SlackApiError as e:
        logger.debug(
            f"[SLACK] SlackApiError fetching channel name for {channel_id}: {e}"
        )
    except (KeyError, TypeError) as e:
        logger.debug(
            f"[SLACK] Parsing error fetching channel name for {channel_id}: {e}"
        )
    except Exception as e:  # noqa: BLE001 - Channel name resolution fallback
        logger.debug(f"[SLACK] Could not fetch channel name for {channel_id}: {e}")

    fallback = f"#{channel_id}"
    CHANNEL_NAME_CACHE[channel_id] = (fallback, now)
    return fallback


async def get_or_create_usage_channel() -> str | None:
    """
    Resolves the ID of the 'izvor-usage' channel.
    1. Checks USAGE_CHANNEL_ID env/config if specified.
    2. Checks in-memory cached channel ID.
    3. Searches workspace channels for 'izvor-usage' (or USAGE_CHANNEL_NAME).
    4. If not found, automatically creates the channel and joins it.
    """
    global _RESOLVED_USAGE_CHANNEL_ID
    if USAGE_CHANNEL_ID:
        return USAGE_CHANNEL_ID

    if _RESOLVED_USAGE_CHANNEL_ID:
        return _RESOLVED_USAGE_CHANNEL_ID

    target_name = USAGE_CHANNEL_NAME.lstrip("#").strip().lower()

    bot_token = get_slack_bot_token()
    if not bot_token or bot_token == "xoxb-your-bot-user-oauth-token":
        _RESOLVED_USAGE_CHANNEL_ID = f"mock_{target_name}_channel"
        return _RESOLVED_USAGE_CHANNEL_ID

    try:
        client = get_slack_async_client()
        cursor = None
        while True:
            await slack_rate_limiter.acquire()
            res = await client.conversations_list(
                types="public_channel,private_channel",
                limit=200,
                cursor=cursor,
            )
            channels = res.get("channels", [])
            for ch in channels:
                if ch.get("name", "").lower() == target_name:
                    ch_id = ch.get("id")
                    _RESOLVED_USAGE_CHANNEL_ID = ch_id
                    logger.info(
                        f"[SLACK] Found existing usage channel '{target_name}' with ID: {ch_id}"
                    )
                    if not ch.get("is_member", True) and ch_id:
                        try:
                            await slack_rate_limiter.acquire()
                            await client.conversations_join(channel=ch_id)
                            logger.info(
                                f"[SLACK] Joined existing usage channel '{target_name}' ({ch_id})"
                            )
                        except SlackApiError as join_err:
                            logger.debug(
                                f"[SLACK] Non-critical join attempt for {target_name}: {join_err}"
                            )
                        except Exception as join_ex:  # noqa: BLE001 - Channel join attempt is non-critical
                            logger.debug(
                                f"[SLACK] Non-critical join attempt for {target_name}: {join_ex}"
                            )
                    return ch_id

            cursor = res.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        logger.info(
            f"[SLACK] Usage channel '{target_name}' not found. Creating channel..."
        )
        await slack_rate_limiter.acquire()
        create_res = await client.conversations_create(
            name=target_name,
            is_private=False,
        )
        created_ch = create_res.get("channel", {})
        ch_id = created_ch.get("id")
        if ch_id:
            _RESOLVED_USAGE_CHANNEL_ID = ch_id
            logger.info(
                f"[SLACK] Successfully created usage channel '{target_name}' with ID: {ch_id}"
            )
            return ch_id

    except SlackApiError as e:
        err = e.response.get("error", "")
        if err == "name_taken":
            logger.info(
                f"[SLACK] Channel '{target_name}' name_taken, attempting retrieval via list..."
            )
            try:
                cursor = None
                while True:
                    await slack_rate_limiter.acquire()
                    res = await client.conversations_list(
                        types="public_channel,private_channel", limit=200, cursor=cursor
                    )
                    for ch in res.get("channels", []):
                        if ch.get("name", "").lower() == target_name:
                            _RESOLVED_USAGE_CHANNEL_ID = ch.get("id")
                            return _RESOLVED_USAGE_CHANNEL_ID
                    cursor = res.get("response_metadata", {}).get("next_cursor")
                    if not cursor:
                        break
            except SlackApiError as err:
                logger.debug(
                    f"[SLACK] conversations_list API error during channel resolution: {err}"
                )
            except Exception as err:  # noqa: BLE001 - Channel listing fallback
                logger.debug(
                    f"[SLACK] Unexpected error during channel resolution: {err}"
                )
        logger.error(
            f"[SLACK] Error resolving/creating usage channel '{target_name}': {e}"
        )
    except Exception as e:  # noqa: BLE001 - Channel creation error boundary
        logger.error(f"[SLACK] Unexpected error resolving/creating usage channel: {e}")

    return None


async def open_dm_channel(user_id: str) -> str | None:
    bot_token = get_slack_bot_token()
    if not bot_token or bot_token == "xoxb-your-bot-user-oauth-token":
        return f"D_MOCK_{user_id}"
    try:
        await slack_rate_limiter.acquire()
        client = get_slack_async_client()
        res = await client.conversations_open(users=user_id)
        data = res.data if isinstance(getattr(res, "data", None), dict) else res
        if not isinstance(data, dict):
            return None
        ch = data.get("channel")
        ch_id = ch.get("id") if isinstance(ch, dict) else None
        return ch_id if isinstance(ch_id, str) and ch_id.strip() else None
    except SlackApiError as e:
        logger.error(f"Slack API error opening DM channel with {user_id}: {e}")
        return None
    except Exception as e:  # noqa: BLE001 - DM channel open boundary returns None
        logger.error(f"Error opening DM channel with {user_id}: {e}")
        return None


async def post_ephemeral_to_slack(
    channel_id: str,
    user_id: str,
    text: str,
    thread_ts: str | None = None,
) -> dict[str, Any]:
    bot_token = get_slack_bot_token()
    if not bot_token or bot_token == "xoxb-your-bot-user-oauth-token":
        return {"ok": True, "mock": True}
    try:
        from core.slack_security import sanitize_slack_broadcast_mentions
        from services.security import scan_output_for_secrets

        matched_rule = scan_output_for_secrets(text, user_id=user_id)
        if matched_rule:
            logger.warning(f"[SECURITY] Ephemeral message blocked by guardrail rule: {matched_rule}")
            text = f"⚠️ *Message blocked by security policy.* (Rule: {matched_rule})"

        text = sanitize_slack_broadcast_mentions(text)
        kwargs: dict[str, Any] = {
            "channel": channel_id,
            "user": user_id,
            "text": text,
        }
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        await slack_rate_limiter.acquire()
        client = get_slack_async_client()
        res = await client.chat_postEphemeral(**kwargs)
        return res.data
    except SlackApiError as e:
        logger.error(f"Slack API error posting ephemeral message: {e}")
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001 - Ephemeral message post boundary returns error dict
        logger.error(f"Error posting ephemeral message: {e}")
        return {"ok": False, "error": str(e)}
