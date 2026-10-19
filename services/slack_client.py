import asyncio
import logging
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from core.config import (
    MCP_WORKSPACE_DIR,
    PROJECT_ROOT,
    SLACK_BOT_TOKEN,
    USAGE_CHANNEL_ID,
    USAGE_CHANNEL_NAME,
    resolve_avatar_url,
)
from core.slack_security import sanitize_slack_broadcast_mentions

logger = logging.getLogger(__name__)

# Initialize async web client
slack_async_client = AsyncWebClient(token=SLACK_BOT_TOKEN)


from services.rate_limiter import slack_rate_limiter

# In-memory caches for channel metadata
CHANNEL_NAME_CACHE: dict[str, str] = {}
_RESOLVED_USAGE_CHANNEL_ID: str | None = None



from services.slack_home_views import (
    build_app_home_view,
    build_edit_workspace_context_modal,
)


async def publish_app_home(
    user_id: str,
    enabled_agents: set[str] | None = None,
    circuit_breaker_config: dict[str, Any] | None = None,
    scribe_enabled: bool | None = None,
) -> dict[str, Any]:
    """
    Publishes the multi-agent control panel view to the user's Slack App Home tab.
    """
    if not SLACK_BOT_TOKEN or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token":
        logger.info(f"[MOCK APP HOME] Would publish App Home view to user {user_id}")
        return {"ok": True, "mock": True}

    try:
        view = build_app_home_view(
            enabled_agents=enabled_agents,
            circuit_breaker_config=circuit_breaker_config,
            scribe_enabled=scribe_enabled,
            user_id=user_id,
        )
        response = await slack_async_client.views_publish(
            user_id=user_id,
            view=view,
        )
        logger.info(f"[SLACK] Published App Home view for user {user_id}")
        return response.data
    except SlackApiError as e:
        logger.error(
            f"Error publishing App Home view to user {user_id}: {e.response['error']}"
        )
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001 - Slack API boundary returns error payload instead of crashing callers
        logger.error(f"Error publishing App Home view to user {user_id}: {e}")
        return {"ok": False, "error": str(e)}


async def post_agent_response(
    channel_id: str,
    thread_ts: str,
    text: str,
    username: str,
    icon_url: str | None = None,
    blocks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Posts a message to Slack thread using dynamic persona fields (chat:write.customize).
    """
    if not SLACK_BOT_TOKEN or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token":
        logger.warning(
            f"[MOCK POST] Channel: {channel_id}, Thread: {thread_ts}, Persona: {username} ({icon_url})\nText: {text}"
        )
        return {"ok": True, "mock": True}

    try:
        resolved_icon = resolve_avatar_url(icon_url)
        if resolved_icon:
            # Dynamic cache-busting based on file modification timestamp (mtime)
            mtime_v = None
            if icon_url:
                local_path = Path(str(icon_url).lstrip("/"))
                if not local_path.exists():
                    local_path = Path(__file__).resolve().parent.parent / str(
                        icon_url
                    ).lstrip("/")
                if local_path.exists():
                    mtime_v = int(local_path.stat().st_mtime)
            v_param = f"v={mtime_v}" if mtime_v else "v=3"
            sep = "&" if "?" in resolved_icon else "?"
            final_icon_url = f"{resolved_icon}{sep}{v_param}"
        else:
            final_icon_url = None

        logger.info(
            f"[SLACK] chat_postMessage channel={channel_id} thread_ts={thread_ts} as '{username}' icon_url='{final_icon_url}'"
        )

        from core.slack_security import sanitize_slack_blocks
        from services.security import scan_output_for_secrets

        matched_rule = scan_output_for_secrets(text, user_id=username)
        if blocks:
            blocks, matched_rule_blocks = sanitize_slack_blocks(blocks, user_id=username)
            if matched_rule_blocks:
                matched_rule = matched_rule or matched_rule_blocks

        if matched_rule:
            logger.warning(f"[SECURITY] Message blocked by guardrail rule: {matched_rule}")
            text = f"⚠️ *Message blocked by security policy.* (Rule: {matched_rule})"
            blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]

        text = sanitize_slack_broadcast_mentions(text)

        await slack_rate_limiter.acquire()
        response = await slack_async_client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=text,
            blocks=blocks,
            username=username,
            icon_url=final_icon_url,
            unfurl_links=False,
            unfurl_media=False,
        )
        return response.data
    except SlackApiError as e:
        logger.error(f"Error posting message to Slack: {e.response['error']}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error posting message to Slack: {e}")
        raise


async def post_system_message(
    channel_id: str,
    thread_ts: str,
    text: str,
    username: str | None = None,
    icon_url: str | None = None,
) -> dict[str, Any]:
    """
    Posts a system warning/notice message to a Slack thread using the System Agent persona.
    """
    default_username = "Panel System"
    default_icon = "static/image_99.png"

    try:
        from core.config import SYSTEM_AGENT_AVATAR, SYSTEM_AGENT_USERNAME

        default_username = SYSTEM_AGENT_USERNAME
        default_icon = SYSTEM_AGENT_AVATAR
    except (ImportError, AttributeError, KeyError):
        logger.debug("[SLACK CLIENT] Fallback core.config persona lookup skipped")

    try:
        from orchestration.agent_settings import settings_manager

        orch_cfg = settings_manager.get_orchestrator_config()
        if orch_cfg.get("username"):
            default_username = orch_cfg["username"]
        if orch_cfg.get("icon_url"):
            default_icon = orch_cfg["icon_url"]
    except (ImportError, AttributeError, KeyError, TypeError):
        logger.debug("[SLACK CLIENT] Fallback settings_manager persona lookup skipped")

    final_username = username or default_username
    final_icon = icon_url or default_icon

    # Strip square brackets from system messages to avoid triggering Slack agent nomination / bracket parsers
    sanitized_text = text.replace("[", "").replace("]", "")

    return await post_agent_response(
        channel_id=channel_id,
        thread_ts=thread_ts,
        text=sanitized_text,
        username=final_username,
        icon_url=final_icon,
    )


async def fetch_thread_history(
    channel_id: str,
    thread_ts: str,
    bot_user_id: str | None = None,
    max_messages: int = 500,
) -> list[dict[str, str]]:
    """
    Fetches past messages in a Slack thread and formats them as LiteLLM conversation history.
    Implements cursor-based pagination (up to max_messages) to prevent truncating long discussions.
    """
    if not SLACK_BOT_TOKEN or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token":
        return []

    try:
        messages: list[dict[str, Any]] = []
        cursor: str | None = None
        max_pages = max(1, (max_messages + 99) // 100)

        for _ in range(max_pages):
            kwargs: dict[str, Any] = {
                "channel": channel_id,
                "ts": thread_ts,
                "limit": 100,
            }
            if cursor:
                kwargs["cursor"] = cursor

            response = await slack_async_client.conversations_replies(**kwargs)
            page_messages = response.get("messages", [])
            messages.extend(page_messages)

            if not response.get("has_more"):
                break

            cursor = response.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        if len(messages) > max_messages:
            messages = messages[-max_messages:]

        formatted_history: list[dict[str, str]] = []
        short_history_logging: list[dict[str, str]] = []

        for msg in messages:
            msg_text = msg.get("text", "")
            username = msg.get("username", "")
            subtype = msg.get("subtype")
            user_id = msg.get("user")

            # Ignore ephemeral status messages from thread conversation history
            if msg_text.strip().startswith(("_🔍", "_🌐", "_⚡", "_⏳")):
                continue

            # Ignore internal system warnings, test runner cards, and scribe summaries from LLM history
            if (
                username in ("Panel System", "E2E Test Runner", "Live Discussion Scribe", "Discussion Conclusion")
                or msg.get("is_scribe_note")
                or msg.get("is_conclusion_note")
                or msg_text.strip().startswith(("⚠️", "⚙️", "🔒", "🗑️", "🧪"))
            ):
                continue

            # Clean runner follow-up wrapper if present
            clean_text = re.sub(r"^👤\s*\*?Follow-up Prompt:\*?\s*\n?>?\s*", "", msg_text)

            # Determine role
            if subtype == "bot_message" or username or user_id == bot_user_id:
                persona_name = username if username else "Assistant"
                formatted_history.append(
                    {"role": "assistant", "content": f"[{persona_name}]: {clean_text}"}
                )
                short_history_logging.append(
                    {
                        "role": "assistant",
                        "content": f"[{persona_name}]: {clean_text[:20]}",
                    }
                )
            else:
                formatted_history.append({"role": "user", "content": clean_text})
                short_history_logging.append({"role": "user", "content": f"{clean_text[:20]}..."})

        logger.info(f"\n short_history:\n {short_history_logging}")
        return formatted_history
    except SlackApiError as e:
        logger.error(f"Error fetching thread history: {e.response['error']}")
        return []
    except Exception:
        logger.exception("Unexpected error fetching thread history")
        return []


def _sanitize_and_truncate_slack_text(text: str, max_chars: int = 3800) -> str:
    """Ensures Slack message text neutralizes broadcast mentions and never exceeds Slack API limits (4000 characters)."""
    text = sanitize_slack_broadcast_mentions(text)
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    last_nl = truncated.rfind("\n")
    if last_nl > max_chars - 300:
        truncated = truncated[:last_nl]
    return f"{truncated.strip()}\n\n_...[truncated to fit Slack limits]_"


async def post_thread_note(
    channel_id: str,
    thread_ts: str,
    text: str,
    pin: bool = True,
    username: str = "Live Discussion Scribe",
    icon_emoji: str = ":memo:",
    blocks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Posts a live updatable summary/notes message inside the given Slack thread.
    Optionally pins the note message for quick discovery. Supports Block Kit blocks.
    """
    text = _sanitize_and_truncate_slack_text(text)

    if not SLACK_BOT_TOKEN or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token":
        mock_ts = f"mock_note_{int(time.time() * 1000)}.000"
        logger.info(
            f"[MOCK NOTE] Posted mock thread note in channel {channel_id}, thread {thread_ts}: {mock_ts}"
        )
        return {"ok": True, "ts": mock_ts, "mock": True, "blocks": blocks}

    try:
        from core.slack_security import sanitize_slack_blocks
        from services.security import scan_output_for_secrets

        matched_rule = scan_output_for_secrets(text, user_id=username)
        if blocks:
            blocks, matched_rule_blocks = sanitize_slack_blocks(blocks, user_id=username)
            if matched_rule_blocks:
                matched_rule = matched_rule or matched_rule_blocks

        if matched_rule:
            logger.warning(f"[SECURITY] Note blocked by guardrail rule: {matched_rule}")
            text = f"⚠️ *Note blocked by security policy.* (Rule: {matched_rule})"
            blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]

        await slack_rate_limiter.acquire()
        kwargs: dict[str, Any] = {
            "channel": channel_id,
            "thread_ts": thread_ts,
            "text": text,
            "username": username,
            "icon_emoji": icon_emoji,
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if blocks:
            kwargs["blocks"] = blocks
        response = await slack_async_client.chat_postMessage(**kwargs)
        msg_ts = response.get("ts")
        logger.info(
            f"[SLACK NOTE] Posted live note message {msg_ts} in thread {thread_ts} of channel {channel_id} ({username})"
        )

        if pin and msg_ts:
            try:
                await slack_async_client.pins_add(channel=channel_id, timestamp=msg_ts)
                logger.info(
                    f"[SLACK NOTE] Pinned note message {msg_ts} to channel {channel_id}"
                )
            except SlackApiError as pin_err:
                err = pin_err.response.get("error", "")
                if err in ("already_pinned", "too_many_pins"):
                    logger.info(
                        f"[SLACK NOTE] Note message pinning skipped ({err}) in channel {channel_id}"
                    )
                else:
                    logger.debug(
                        f"[SLACK NOTE] Note message pinning skipped/failed: {err}"
                    )
            except (KeyError, TypeError, OSError) as pin_ex:
                logger.debug(f"[SLACK NOTE] Note message pinning skipped: {pin_ex}")

        return {"ok": True, "ts": msg_ts}
    except SlackApiError as e:
        logger.error(
            f"[SLACK NOTE] Error posting thread note: {e.response.get('error', e)}"
        )
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001 - Slack API boundary returns error payload instead of crashing callers
        logger.error(f"[SLACK NOTE] Unexpected error posting thread note: {e}")
        return {"ok": False, "error": str(e)}


async def unpin_thread_note(
    channel_id: str,
    message_ts: str,
) -> dict[str, Any]:
    """
    Unpins a thread note message from the channel.
    Gracefully handles cases where the message is already unpinned or not found.
    """
    if not message_ts:
        return {"ok": False, "error": "No message_ts provided"}

    if (
        not SLACK_BOT_TOKEN
        or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token"
        or str(message_ts).startswith("mock_")
    ):
        logger.info(f"[MOCK NOTE] Unpinned mock note {message_ts} in channel {channel_id}")
        return {"ok": True, "mock": True}

    try:
        await slack_rate_limiter.acquire()
        await slack_async_client.pins_remove(channel=channel_id, timestamp=message_ts)
        logger.info(f"[SLACK NOTE] Unpinned note message {message_ts} from channel {channel_id}")
        return {"ok": True}
    except SlackApiError as e:
        err = e.response.get("error", "")
        if err in ("not_pinned", "no_pin"):
            logger.debug(f"[SLACK NOTE] Note {message_ts} was already unpinned ({err})")
            return {"ok": True}
        logger.debug(f"[SLACK NOTE] Error unpinning note {message_ts}: {err}")
        return {"ok": False, "error": err}
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[SLACK NOTE] Unexpected error unpinning note {message_ts}: {e}")
        return {"ok": False, "error": str(e)}


async def update_thread_note(
    channel_id: str,
    message_ts: str,
    text: str,
    blocks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Updates an existing live summary/notes message in-place via Slack chat.update.
    Supports Block Kit blocks.
    """
    text = _sanitize_and_truncate_slack_text(text)

    if (
        not SLACK_BOT_TOKEN
        or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token"
        or (message_ts and str(message_ts).startswith("mock_"))
    ):
        logger.info(f"[MOCK NOTE] Updated mock note {message_ts} ({len(text)} chars)")
        return {"ok": True, "ts": message_ts, "mock": True, "blocks": blocks}

    try:
        from core.slack_security import sanitize_slack_blocks
        from services.security import scan_output_for_secrets

        matched_rule = scan_output_for_secrets(text, user_id="system_update")
        if blocks:
            blocks, matched_rule_blocks = sanitize_slack_blocks(blocks, user_id="system_update")
            if matched_rule_blocks:
                matched_rule = matched_rule or matched_rule_blocks

        if matched_rule:
            logger.warning(f"[SECURITY] Update blocked by guardrail rule: {matched_rule}")
            text = f"⚠️ *Update blocked by security policy.* (Rule: {matched_rule})"
            blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]

        await slack_rate_limiter.acquire()
        kwargs: dict[str, Any] = {
            "channel": channel_id,
            "ts": message_ts,
            "text": text,
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if blocks:
            kwargs["blocks"] = blocks
        await slack_async_client.chat_update(**kwargs)
        logger.info(f"[SLACK NOTE] Successfully updated live note message {message_ts}")
        return {"ok": True, "ts": message_ts}
    except SlackApiError as e:
        logger.error(
            f"[SLACK NOTE] Error updating note {message_ts}: {e.response.get('error', e)}"
        )
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001 - Slack API boundary returns error payload instead of crashing callers
        logger.error(f"[SLACK NOTE] Unexpected error updating note {message_ts}: {e}")
        return {"ok": False, "error": str(e)}


async def get_channel_name(channel_id: str) -> str:
    """
    Fetches the human-readable channel name (e.g. '#general') with in-memory caching.
    """
    if not channel_id:
        return "#unknown"
    if channel_id in CHANNEL_NAME_CACHE:
        return CHANNEL_NAME_CACHE[channel_id]

    if not SLACK_BOT_TOKEN or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token":
        name = f"#{channel_id}"
        CHANNEL_NAME_CACHE[channel_id] = name
        return name

    try:
        res = await slack_async_client.conversations_info(channel=channel_id)
        ch_info = res.get("channel", {})
        name = ch_info.get("name")
        if name:
            formatted = f"#{name}"
            CHANNEL_NAME_CACHE[channel_id] = formatted
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
    CHANNEL_NAME_CACHE[channel_id] = fallback
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

    if not SLACK_BOT_TOKEN or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token":
        _RESOLVED_USAGE_CHANNEL_ID = f"mock_{target_name}_channel"
        return _RESOLVED_USAGE_CHANNEL_ID

    try:
        cursor = None
        while True:
            res = await slack_async_client.conversations_list(
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
                            await slack_async_client.conversations_join(channel=ch_id)
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
        create_res = await slack_async_client.conversations_create(
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
                res = await slack_async_client.conversations_list(
                    types="public_channel,private_channel", limit=200
                )
                for ch in res.get("channels", []):
                    if ch.get("name", "").lower() == target_name:
                        _RESOLVED_USAGE_CHANNEL_ID = ch.get("id")
                        return _RESOLVED_USAGE_CHANNEL_ID
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


async def post_usage_message(
    channel_id: str,
    text: str,
    username: str = "Izvor Usage Analyst",
    icon_emoji: str = ":bar_chart:",
) -> dict[str, Any]:
    """
    Posts a new usage tracking report message in the specified usage channel.
    """
    text = _sanitize_and_truncate_slack_text(text)

    if not SLACK_BOT_TOKEN or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token":
        mock_ts = f"mock_usage_{int(time.time() * 1000)}.000"
        logger.info(
            f"[MOCK USAGE] Posted mock usage message in channel {channel_id}: {mock_ts}"
        )
        return {"ok": True, "ts": mock_ts, "mock": True}

    try:
        response = await slack_async_client.chat_postMessage(
            channel=channel_id,
            text=text,
            username=username,
            icon_emoji=icon_emoji,
            unfurl_links=False,
            unfurl_media=False,
        )
        msg_ts = response.get("ts")
        logger.info(
            f"[SLACK USAGE] Posted usage message {msg_ts} in channel {channel_id}"
        )
        return {"ok": True, "ts": msg_ts}
    except SlackApiError as e:
        logger.error(
            f"[SLACK USAGE] Error posting usage message: {e.response.get('error', e)}"
        )
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001 - Usage post error boundary returns error dict
        logger.error(f"[SLACK USAGE] Unexpected error posting usage message: {e}")
        return {"ok": False, "error": str(e)}


async def update_usage_message(
    channel_id: str,
    message_ts: str,
    text: str,
) -> dict[str, Any]:
    """
    Updates an existing usage tracking report message in-place via chat.update.
    """
    text = _sanitize_and_truncate_slack_text(text)

    if (
        not SLACK_BOT_TOKEN
        or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token"
        or (message_ts and str(message_ts).startswith("mock_"))
    ):
        logger.info(
            f"[MOCK USAGE] Updated mock usage message {message_ts} ({len(text)} chars)"
        )
        return {"ok": True, "ts": message_ts, "mock": True}

    try:
        await slack_async_client.chat_update(
            channel=channel_id,
            ts=message_ts,
            text=text,
        )
        logger.info(f"[SLACK USAGE] Successfully updated usage message {message_ts}")
        return {"ok": True, "ts": message_ts}
    except SlackApiError as e:
        logger.error(
            f"[SLACK USAGE] Error updating usage message {message_ts}: {e.response.get('error', e)}"
        )
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001 - Usage update error boundary returns error dict
        logger.error(
            f"[SLACK USAGE] Unexpected error updating usage message {message_ts}: {e}"
        )
        return {"ok": False, "error": str(e)}


async def fetch_thread_files(
    channel_id: str, thread_ts: str, max_messages: int = 500
) -> list[dict[str, Any]]:
    """
    Fetches all file attachment metadata uploaded in a Slack thread.
    Implements cursor-based pagination (up to max_messages) to discover files across long threads.
    Returns a list of file dicts with keys: id, name, title, mimetype, filetype, size, url_private_download, url_private.
    """
    if not SLACK_BOT_TOKEN or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token":
        return []

    try:
        messages: list[dict[str, Any]] = []
        cursor: str | None = None
        max_pages = max(1, (max_messages + 99) // 100)

        for _ in range(max_pages):
            kwargs: dict[str, Any] = {
                "channel": channel_id,
                "ts": thread_ts,
                "limit": 100,
            }
            if cursor:
                kwargs["cursor"] = cursor

            response = await slack_async_client.conversations_replies(**kwargs)
            page_messages = response.get("messages", [])
            messages.extend(page_messages)

            if not response.get("has_more"):
                break

            cursor = response.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        thread_files: list[dict[str, Any]] = []
        for msg in messages:
            files = msg.get("files", [])
            for f in files:
                thread_files.append(
                    {
                        "id": f.get("id"),
                        "name": f.get("name", "unknown_file"),
                        "title": f.get("title", ""),
                        "mimetype": f.get("mimetype", ""),
                        "filetype": f.get("filetype", ""),
                        "size": f.get("size", 0),
                        "url_private_download": f.get("url_private_download")
                        or f.get("url_private", ""),
                        "url_private": f.get("url_private", ""),
                        "user": f.get("user") or msg.get("user"),
                        "ts": msg.get("ts"),
                    }
                )
        logger.info(
            f"[SLACK FILES] Found {len(thread_files)} files in thread {thread_ts}"
        )
        return thread_files
    except SlackApiError as e:
        logger.error(
            f"[SLACK FILES] Error fetching thread files: {e.response.get('error', e)}"
        )
        return []
    except Exception as e:  # noqa: BLE001 - File listing error boundary returns empty list
        logger.error(f"[SLACK FILES] Unexpected error fetching thread files: {e}")
        return []


def sanitize_slack_download_filename(file_name: str, prefix_len: int = 6) -> str:
    """
    Sanitizes untrusted filenames from Slack attachments:
    - Extracts base name, stripping any path traversal characters (../)
    - Sanitizes characters in filename stem and extension
    - Prepends a short UUID prefix to avoid collisions and overwrites
    """
    import uuid

    raw_name = Path(file_name or "file").name
    stem = Path(raw_name).stem
    suffix = Path(raw_name).suffix

    clean_stem = re.sub(r"[^\w\-]", "_", stem).strip("._") or "file"
    clean_suffix = re.sub(r"[^\w\.]", "", suffix)
    short_uuid = uuid.uuid4().hex[:prefix_len]
    return f"{short_uuid}_{clean_stem}{clean_suffix}"


def _extract_pdf_pages(raw_bytes: bytes) -> str:
    """Helper to synchronously parse PDF pages; executed in a background thread."""
    import io
    import pypdf

    pdf_stream = io.BytesIO(raw_bytes)
    reader = pypdf.PdfReader(pdf_stream)
    pages_text = []
    for idx, page in enumerate(reader.pages):
        page_text = (page.extract_text() or "").strip()
        if page_text:
            pages_text.append(f"--- Page {idx + 1} ---\n{page_text}")
        else:
            pages_text.append(f"--- Page {idx + 1} ---\n[No extractable text]")
    return "\n\n".join(pages_text) if pages_text else "[Empty PDF document]"


async def download_slack_file(
    file_info: dict[str, Any], max_chars: int = 50000
) -> dict[str, Any]:
    """
    Downloads a private Slack file attachment using SLACK_BOT_TOKEN and extracts its text content.
    Returns dict: {"ok": bool, "file_name": str, "content": str, "truncated": bool, "size": int, "error": str | None}
    """

    import httpx

    from orchestration.system_settings import (
        get_max_file_size_bytes,
        get_max_file_size_mb,
    )

    file_name = file_info.get("name", "unknown_file")
    url = file_info.get("url_private_download") or file_info.get("url_private")
    file_size = file_info.get("size", 0)

    max_size_bytes = get_max_file_size_bytes()
    max_size_mb = get_max_file_size_mb()

    if file_size > max_size_bytes:
        return {
            "ok": False,
            "file_name": file_name,
            "content": "",
            "truncated": False,
            "size": file_size,
            "error": f"File '{file_name}' ({file_size} bytes) exceeds maximum allowed size of {max_size_mb:.1f} MB.",
        }

    if not url:
        return {
            "ok": False,
            "file_name": file_name,
            "content": "",
            "truncated": False,
            "size": file_size,
            "error": "No download URL available for this file.",
        }

    # Verify origin hostname belongs to allowed Slack domains before attaching sensitive Bearer token
    parsed_url = urllib.parse.urlsplit(url)
    hostname = (parsed_url.hostname or "").lower()
    allowed_hosts = {
        "files.slack.com",
        "files-edu.slack.com",
        "slack-files.com",
    }
    is_slack_host = (
        hostname in allowed_hosts
        or hostname.endswith(".slack.com")
        or hostname.endswith(".slack-files.com")
    )
    if not is_slack_host:
        logger.warning(
            f"[SLACK FILE] Blocked file download attempt to non-Slack domain: {hostname}"
        )
        return {
            "ok": False,
            "file_name": file_name,
            "content": "",
            "truncated": False,
            "size": file_size,
            "error": f"Security violation: File download URL domain '{hostname}' is not a recognized Slack domain.",
        }

    if not SLACK_BOT_TOKEN or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token":
        return {
            "ok": True,
            "file_name": file_name,
            "content": f"[Mock file content for {file_name}]",
            "truncated": False,
            "size": file_size,
            "error": None,
        }

    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    try:
        from orchestration.system_settings import get_slack_file_download_timeout_seconds
        download_timeout = get_slack_file_download_timeout_seconds()
    except Exception:
        download_timeout = 30.0

    try:
        async with httpx.AsyncClient(timeout=download_timeout, follow_redirects=True) as client:
            async with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code != 200:
                    return {
                        "ok": False,
                        "file_name": file_name,
                        "content": "",
                        "truncated": False,
                        "size": file_size,
                        "error": f"Slack file download failed with HTTP status {resp.status_code}",
                    }

                chunks = []
                total_bytes = 0
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    total_bytes += len(chunk)
                    if total_bytes > max_size_bytes:
                        return {
                            "ok": False,
                            "file_name": file_name,
                            "content": "",
                            "truncated": False,
                            "size": total_bytes,
                            "error": f"File '{file_name}' ({total_bytes} bytes) exceeds maximum allowed size of {max_size_mb:.1f} MB.",
                        }
                    chunks.append(chunk)

                raw_bytes = b"".join(chunks)

            # Cache local copy to workspace safely
            try:
                downloads_dir = (MCP_WORKSPACE_DIR / "downloads").resolve()
                downloads_dir.mkdir(parents=True, exist_ok=True)
                safe_name = sanitize_slack_download_filename(file_name, prefix_len=6)
                file_path = (downloads_dir / safe_name).resolve()
                if not file_path.is_relative_to(downloads_dir):
                    raise PermissionError(
                        f"Path traversal detected: '{file_name}' resolves outside downloads directory"
                    )
                file_path.write_bytes(raw_bytes)
                logger.info(f"[SLACK FILE] Cached Slack file safely to workspace: {file_path}")
            except (OSError, PermissionError) as save_err:
                logger.warning(
                    f"[SLACK FILE] Could not save local copy to workspace: {save_err}"
                )

            if file_name.lower().endswith(".pdf"):
                try:
                    text_content = await asyncio.to_thread(_extract_pdf_pages, raw_bytes)
                except Exception as pdf_err:  # noqa: BLE001 - PDF parsing error boundary falls back cleanly
                    logger.warning(
                        f"[SLACK FILE] Could not parse PDF attachment {file_name}: {pdf_err}"
                    )
                    text_content = f"[Failed to extract text from PDF: {pdf_err}]"
            else:
                text_content = raw_bytes.decode("utf-8", errors="replace")

            truncated = False
            if len(text_content) > max_chars:
                text_content = (
                    text_content[:max_chars]
                    + f"\n\n... [TRUNCATED: File exceeds {max_chars} characters] ..."
                )
                truncated = True

            return {
                "ok": True,
                "file_name": file_name,
                "content": text_content,
                "truncated": truncated,
                "size": len(raw_bytes),
                "error": None,
            }
    except (httpx.TimeoutException, httpx.RequestError) as e:
        logger.error(
            f"[SLACK FILE] Network error downloading Slack file {file_name}: {e}"
        )
        return {
            "ok": False,
            "file_name": file_name,
            "content": "",
            "truncated": False,
            "size": file_size,
            "error": str(e),
        }
    except Exception as e:  # noqa: BLE001 - File download error boundary returns error dict
        logger.error(f"[SLACK FILE] Error downloading Slack file {file_name}: {e}")
        return {
            "ok": False,
            "file_name": file_name,
            "content": "",
            "truncated": False,
            "size": file_size,
            "error": str(e),
        }


def extract_thread_files_metadata(
    files_list: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "id": f.get("id"),
            "name": f.get("name"),
            "size": f.get("size"),
            "filetype": f.get("filetype"),
        }
        for f in files_list
    ]


async def get_bot_user_id() -> str | None:
    if not SLACK_BOT_TOKEN or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token":
        return "U_MOCK_BOT"
    try:
        auth_info = await slack_async_client.auth_test()
        return auth_info.get("user_id")
    except SlackApiError as e:
        logger.error(f"Slack API error fetching bot user ID: {e}")
        return None
    except Exception as e:  # noqa: BLE001 - Bot user ID lookup boundary returns None
        logger.error(f"Error fetching bot user ID: {e}")
        return None


async def open_dm_channel(user_id: str) -> str | None:
    if not SLACK_BOT_TOKEN or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token":
        return f"D_MOCK_{user_id}"
    try:
        res = await slack_async_client.conversations_open(users=user_id)
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
    if not SLACK_BOT_TOKEN or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token":
        return {"ok": True, "mock": True}
    try:
        kwargs = {
            "channel": channel_id,
            "user": user_id,
            "text": text,
        }
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        await slack_rate_limiter.acquire()
        res = await slack_async_client.chat_postEphemeral(**kwargs)
        return res.data
    except SlackApiError as e:
        logger.error(f"Slack API error posting ephemeral message: {e}")
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001 - Ephemeral message post boundary returns error dict
        logger.error(f"Error posting ephemeral message: {e}")
        return {"ok": False, "error": str(e)}


async def post_message_to_slack(
    channel_id: str,
    text: str,
    thread_ts: str | None = None,
    username: str | None = None,
    icon_url: str | None = None,
    icon_emoji: str | None = None,
) -> dict[str, Any]:
    if not SLACK_BOT_TOKEN or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token":
        return {"ok": True, "mock": True}
    try:
        text = sanitize_slack_broadcast_mentions(text)
        kwargs = {
            "channel": channel_id,
            "text": text,
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        if username:
            kwargs["username"] = username
        if icon_url:
            kwargs["icon_url"] = icon_url
        if icon_emoji:
            kwargs["icon_emoji"] = icon_emoji
        await slack_rate_limiter.acquire()
        res = await slack_async_client.chat_postMessage(**kwargs)
        return res.data
    except SlackApiError as e:
        logger.error(f"Slack API error posting message to Slack: {e}")
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001 - Message post boundary returns error dict
        logger.error(f"Error posting message to Slack: {e}")
        return {"ok": False, "error": str(e)}


async def update_slack_message(
    channel_id: str,
    ts: str,
    text: str,
) -> dict[str, Any]:
    return await update_thread_note(channel_id=channel_id, message_ts=ts, text=text)


async def read_slack_file_text(
    file_info: dict[str, Any], max_chars: int = 50000
) -> str:
    res = await download_slack_file(file_info, max_chars=max_chars)
    return res.get("content", "")


async def upload_text_file_to_slack(
    channel_id: str,
    content: str,
    filename: str = "scribe_notes.txt",
    title: str = "Discussion Notes",
    initial_comment: str | None = None,
    thread_ts: str | None = None,
) -> dict[str, Any]:
    """
    Uploads a plain text file directly to a Slack channel or DM.
    Supports modern files_upload_v2 with fallback to files_upload and mock support.
    """
    if (
        not SLACK_BOT_TOKEN
        or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token"
        or (channel_id and (str(channel_id).startswith(("mock_", "D_TEST", "D_MOCK", "C_TEST", "C_MOCK")) or "TEST" in str(channel_id)))
    ):
        mock_file_id = f"mock_file_{int(time.time() * 1000)}"
        logger.info(
            f"[MOCK FILE] Uploaded mock file '{filename}' ({len(content)} chars) to {channel_id}: {mock_file_id}"
        )
        return {
            "ok": True,
            "file": {
                "id": mock_file_id,
                "name": filename,
                "title": title,
                "channels": [channel_id],
                "content": content,
            },
            "mock": True,
        }

    try:
        await slack_rate_limiter.acquire()
        kwargs: dict[str, Any] = {
            "channel": channel_id,
            "content": content,
            "filename": filename,
            "title": title,
        }
        if initial_comment:
            kwargs["initial_comment"] = initial_comment
        if thread_ts:
            kwargs["thread_ts"] = thread_ts

        if hasattr(slack_async_client, "files_upload_v2"):
            res = await slack_async_client.files_upload_v2(**kwargs)
        else:
            res = await slack_async_client.files_upload(**kwargs)
        file_data = res.data if hasattr(res, "data") else res
        logger.info(f"[SLACK FILE] Successfully uploaded file '{filename}' to {channel_id}")
        return {"ok": True, "file": file_data}
    except SlackApiError as e:
        logger.error(f"[SLACK FILE] SlackApiError uploading file: {e.response.get('error', e)}")
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001 - File upload boundary returns error dict
        logger.error(f"[SLACK FILE] Error uploading file: {e}")
        return {"ok": False, "error": str(e)}


def cleanup_old_downloads(max_age_seconds: float = 86400.0) -> int:
    """
    Cleans up cached downloaded Slack files older than max_age_seconds (default 24h).
    Checks both MCP_WORKSPACE_DIR / 'downloads' and PROJECT_ROOT / 'data' / 'downloads'.
    Returns the count of evicted files.
    """
    now = time.time()
    evicted_count = 0
    candidate_dirs = [
        (MCP_WORKSPACE_DIR / "downloads").resolve(),
        (PROJECT_ROOT / "data" / "downloads").resolve(),
    ]
    seen_dirs: set[Path] = set()
    for d in candidate_dirs:
        if d in seen_dirs or not d.exists() or not d.is_dir():
            continue
        seen_dirs.add(d)
        try:
            for item in d.iterdir():
                if item.is_file():
                    try:
                        mtime = item.stat().st_mtime
                        if (now - mtime) > max_age_seconds:
                            item.unlink(missing_ok=True)
                            evicted_count += 1
                    except Exception as e:
                        logger.debug(f"[CLEANUP] Failed to prune downloaded file {item}: {e}")
        except Exception as e:
            logger.debug(f"[CLEANUP] Error inspecting downloads directory {d}: {e}")

    if evicted_count > 0:
        logger.info(f"[CLEANUP] Pruned {evicted_count} downloaded file(s) older than {max_age_seconds}s")
    return evicted_count


