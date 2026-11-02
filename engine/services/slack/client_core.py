import logging
import time
from typing import Any

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from core.config import (
    SLACK_BOT_TOKEN,
    USAGE_CHANNEL_ID,
    USAGE_CHANNEL_NAME,
)
from services.rate_limiter import slack_rate_limiter

logger = logging.getLogger(__name__)

# Initialize async web client
slack_async_client = AsyncWebClient(token=SLACK_BOT_TOKEN)


def get_slack_bot_token() -> str:
    """Returns the configured Slack bot token."""
    return SLACK_BOT_TOKEN


def get_slack_async_client() -> AsyncWebClient:
    """Returns the canonical AsyncWebClient instance."""
    return slack_async_client

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


async def get_bot_user_id() -> str | None:
    bot_token = get_slack_bot_token()
    if not bot_token or bot_token == "xoxb-your-bot-user-oauth-token":
        return "U_MOCK_BOT"
    try:
        client = get_slack_async_client()
        auth_info = await client.auth_test()
        return auth_info.get("user_id")
    except SlackApiError as e:
        logger.error(f"Slack API error fetching bot user ID: {e}")
        return None
    except Exception as e:  # noqa: BLE001 - Bot user ID lookup boundary returns None
        logger.error(f"Error fetching bot user ID: {e}")
        return None


async def open_dm_channel(user_id: str) -> str | None:
    bot_token = get_slack_bot_token()
    if not bot_token or bot_token == "xoxb-your-bot-user-oauth-token":
        return f"D_MOCK_{user_id}"
    try:
        client = get_slack_async_client()
        res = await client.conversations_open(users=user_id)
        ch = res.get("channel", {})
        return ch.get("id")
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
