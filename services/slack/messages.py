import logging
from pathlib import Path
import re
import sys
import time
from typing import Any

from slack_sdk.errors import SlackApiError

from core.config import (
    resolve_avatar_url,
)
from core.slack_security import (
    sanitize_slack_broadcast_mentions,
    sanitize_slack_blocks,
)
from services.rate_limiter import slack_rate_limiter
from services.security import scan_output_for_secrets
from services.slack.client_core import get_slack_async_client, get_slack_bot_token

logger = logging.getLogger(__name__)


def _get_logger() -> logging.Logger:
    sc = sys.modules.get("services.slack_client")
    if sc is not None and hasattr(sc, "logger"):
        return getattr(sc, "logger")
    return logger


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
    bot_token = get_slack_bot_token()
    if not bot_token or bot_token == "xoxb-your-bot-user-oauth-token":
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
                    local_path = Path(__file__).resolve().parent.parent.parent / str(
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
        client = get_slack_async_client()
        response = await client.chat_postMessage(
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
    bot_token = get_slack_bot_token()
    if not bot_token or bot_token == "xoxb-your-bot-user-oauth-token":
        return []

    try:
        client = get_slack_async_client()
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

            response = await client.conversations_replies(**kwargs)
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

        _get_logger().info(f"\n short_history:\n {short_history_logging}")
        return formatted_history
    except SlackApiError as e:
        logger.error(f"Error fetching thread history: {e.response['error']}")
        return []
    except Exception:
        logger.exception("Unexpected error fetching thread history")
        return []


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

    bot_token = get_slack_bot_token()
    if not bot_token or bot_token == "xoxb-your-bot-user-oauth-token":
        mock_ts = f"mock_note_{int(time.time() * 1000)}.000"
        logger.info(
            f"[MOCK NOTE] Posted mock thread note in channel {channel_id}, thread {thread_ts}: {mock_ts}"
        )
        return {"ok": True, "ts": mock_ts, "mock": True, "blocks": blocks}

    try:
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
        client = get_slack_async_client()
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
        response = await client.chat_postMessage(**kwargs)
        msg_ts = response.get("ts")
        logger.info(
            f"[SLACK NOTE] Posted live note message {msg_ts} in thread {thread_ts} of channel {channel_id} ({username})"
        )

        if pin and msg_ts:
            try:
                await client.pins_add(channel=channel_id, timestamp=msg_ts)
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

    bot_token = get_slack_bot_token()
    if (
        not bot_token
        or bot_token == "xoxb-your-bot-user-oauth-token"
        or str(message_ts).startswith("mock_")
    ):
        logger.info(f"[MOCK NOTE] Unpinned mock note {message_ts} in channel {channel_id}")
        return {"ok": True, "mock": True}

    try:
        await slack_rate_limiter.acquire()
        client = get_slack_async_client()
        await client.pins_remove(channel=channel_id, timestamp=message_ts)
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

    bot_token = get_slack_bot_token()
    if (
        not bot_token
        or bot_token == "xoxb-your-bot-user-oauth-token"
        or (message_ts and str(message_ts).startswith("mock_"))
    ):
        logger.info(f"[MOCK NOTE] Updated mock note {message_ts} ({len(text)} chars)")
        return {"ok": True, "ts": message_ts, "mock": True, "blocks": blocks}

    try:
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
        client = get_slack_async_client()
        kwargs: dict[str, Any] = {
            "channel": channel_id,
            "ts": message_ts,
            "text": text,
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if blocks:
            kwargs["blocks"] = blocks
        await client.chat_update(**kwargs)
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

    bot_token = get_slack_bot_token()
    if not bot_token or bot_token == "xoxb-your-bot-user-oauth-token":
        mock_ts = f"mock_usage_{int(time.time() * 1000)}.000"
        logger.info(
            f"[MOCK USAGE] Posted mock usage message in channel {channel_id}: {mock_ts}"
        )
        return {"ok": True, "ts": mock_ts, "mock": True}

    try:
        client = get_slack_async_client()
        response = await client.chat_postMessage(
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

    bot_token = get_slack_bot_token()
    if (
        not bot_token
        or bot_token == "xoxb-your-bot-user-oauth-token"
        or (message_ts and str(message_ts).startswith("mock_"))
    ):
        logger.info(
            f"[MOCK USAGE] Updated mock usage message {message_ts} ({len(text)} chars)"
        )
        return {"ok": True, "ts": message_ts, "mock": True}

    try:
        client = get_slack_async_client()
        await client.chat_update(
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


async def post_message_to_slack(
    channel_id: str,
    text: str,
    thread_ts: str | None = None,
    username: str | None = None,
    icon_url: str | None = None,
    icon_emoji: str | None = None,
) -> dict[str, Any]:
    bot_token = get_slack_bot_token()
    if not bot_token or bot_token == "xoxb-your-bot-user-oauth-token":
        return {"ok": True, "mock": True}
    try:
        text = sanitize_slack_broadcast_mentions(text)
        kwargs: dict[str, Any] = {
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
        client = get_slack_async_client()
        res = await client.chat_postMessage(**kwargs)
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
    blocks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return await update_thread_note(channel_id=channel_id, message_ts=ts, text=text, blocks=blocks)
