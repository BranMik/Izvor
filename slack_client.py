import logging
import time
from typing import Any

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from config import AGENTS_CONFIG, SLACK_BOT_TOKEN, USAGE_CHANNEL_ID, USAGE_CHANNEL_NAME

logger = logging.getLogger(__name__)

# Initialize async web client
slack_async_client = AsyncWebClient(token=SLACK_BOT_TOKEN)

# In-memory caches for channel metadata
CHANNEL_NAME_CACHE: dict[str, str] = {}
_RESOLVED_USAGE_CHANNEL_ID: str | None = None


def build_app_home_view(enabled_agents: set[str] | None = None) -> dict[str, Any]:
    """
    Generates the Block Kit Home Tab view for the Slack Application Home page.
    Displays all panel agents with radio buttons (ON/OFF) and indicates locked agents.
    """
    from agent_router import LOCKED_AGENTS, get_enabled_agents

    if enabled_agents is None:
        enabled_agents = get_enabled_agents()

    total_agents = len(AGENTS_CONFIG)
    active_count = len(enabled_agents)

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "🤖 CaiLogics Multi-Agent Panel Control Hub",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*Dobrodošli u CaiLogics kontrolni panel!*\n"
                    "Ovdje možete uključiti ili isključiti agente koji sudjeluju u automatskoj debati kada korisnik ne taga niti jednog agenta.\n\n"
                    "💡 *Napomena:* Isključeni agenti i dalje se mogu direktno pozvati tagiranjem (npr. `[MistLar_TechLead]` ili `@MistLar TechLead`)."
                ),
            },
        },
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*📊 Status panela:*\n🟢 *{active_count}* / *{total_agents}* agenata aktivno",
                },
                {
                    "type": "mrkdwn",
                    "text": "*🔒 Obavezni agenti:*\nVisionary, Architect, Sceptic",
                },
            ],
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "✅ Uključi sve agente",
                        "emoji": True,
                    },
                    "action_id": "bulk_enable_all",
                    "style": "primary",
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "🔒 Samo obavezni agenti",
                        "emoji": True,
                    },
                    "action_id": "bulk_disable_optional",
                },
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*🔒 Obavezni agenti (uvijek uključeni u automatsku debatu):*",
            },
        },
    ]

    # 1. Add locked agents first
    for key in ("Gemini_Visionary", "AnthrOpus_Sceptic", "OpAiTerra_Architect"):
        cfg = AGENTS_CONFIG.get(key, {})
        username = cfg.get("username", key)
        model = cfg.get("model", "")
        role_desc = {
            "Gemini_Visionary": "🎯 Početni agent & Moderator rasprave",
            "AnthrOpus_Sceptic": "🔍 Kritička analiza & Preispitivanje pretpostavki",
            "OpAiTerra_Architect": "🏛️ Glavni arhitekt & Završni konsenzus",
        }.get(key, "Obavezni član panela")

        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{username}*  •  `{model}`\n_{role_desc}_",
                },
                "accessory": {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "🔒 Locked ON",
                        "emoji": True,
                    },
                    "action_id": f"locked_agent_{key}",
                    "value": "locked",
                },
            }
        )

    blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*⚙️ Konfigurabilni agenti za automatsku debatu:*",
            },
        }
    )

    # 2. Add toggleable agents with radio buttons (ON/OFF)
    for key, cfg in AGENTS_CONFIG.items():
        if key in LOCKED_AGENTS:
            continue

        username = cfg.get("username", key)
        model = cfg.get("model", "")
        is_on = key in enabled_agents

        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{username}*\n`{model}`",
                },
                "accessory": {
                    "type": "radio_buttons",
                    "action_id": f"toggle_agent_radio:{key}",
                    "options": [
                        {
                            "text": {"type": "plain_text", "text": "🟢 ON", "emoji": True},
                            "value": f"{key}:on",
                        },
                        {
                            "text": {"type": "plain_text", "text": "🔴 OFF", "emoji": True},
                            "value": f"{key}:off",
                        },
                    ],
                    "initial_option": {
                        "text": {
                            "type": "plain_text",
                            "text": "🟢 ON" if is_on else "🔴 OFF",
                            "emoji": True,
                        },
                        "value": f"{key}:on" if is_on else f"{key}:off",
                    },
                },
            }
        )

    blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "⚡ _CaiLogics Multi-Agent Platform • Promjene stupaju na snagu odmah za nove rasprave._",
                }
            ],
        }
    )

    return {"type": "home", "blocks": blocks}


async def publish_app_home(
    user_id: str, enabled_agents: set[str] | None = None
) -> dict[str, Any]:
    """
    Publishes the multi-agent control panel view to the user's Slack App Home tab.
    """
    if not SLACK_BOT_TOKEN or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token":
        logger.info(f"[MOCK APP HOME] Would publish App Home view to user {user_id}")
        return {"ok": True, "mock": True}

    try:
        view = build_app_home_view(enabled_agents)
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


async def post_agent_response(
    channel_id: str,
    thread_ts: str,
    text: str,
    username: str,
    icon_url: str = None,
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
        # Append a cache-buster query parameter to force Slack to update its CDN cache for new icons
        final_icon_url = f"{icon_url}?v=2" if icon_url else None

        response = await slack_async_client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=text,
            username=username,
            icon_url=final_icon_url,
        )
        return response.data
    except SlackApiError as e:
        logger.error(f"Error posting message to Slack: {e.response['error']}")
        raise


async def fetch_thread_history(
    channel_id: str, thread_ts: str, bot_user_id: str = None
) -> list[dict[str, str]]:
    """
    Fetches past messages in a Slack thread and formats them as LiteLLM conversation history.
    """
    if not SLACK_BOT_TOKEN or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token":
        return []

    try:
        response = await slack_async_client.conversations_replies(
            channel=channel_id, ts=thread_ts, limit=100
        )
        messages = response.get("messages", [])
        formatted_history: list[dict[str, str]] = []
        short_history_logging: list[dict[str, str]] = []

        for msg in messages:
            msg_text = msg.get("text", "")
            username = msg.get("username", "")
            subtype = msg.get("subtype")
            user_id = msg.get("user")

            # Ignore scribe note messages from thread conversation history
            if (
                username
                and (
                    "Live Discussion Scribe" in username
                    or "Discussion Scribe" in username
                    or "Scribe" in username
                )
                or "📋 *Bilješke rasprave" in msg_text
                or "📋 *Strateška sinteza" in msg_text
                or "📋 *Discussion Notes" in msg_text
                or "📋 *Strategic Synthesis" in msg_text
                or "📋 *Live Discussion Notes" in msg_text
                or "📋 *Final Discussion Notes" in msg_text
                or ":memo:" in msg_text
                or ":clipboard:" in msg_text
            ):
                continue

            # Determine role
            if subtype == "bot_message" or username or user_id == bot_user_id:
                persona_name = username if username else "Assistant"
                formatted_history.append(
                    {"role": "assistant", "content": f"[{persona_name}]: {msg_text}"}
                )
                short_history_logging.append(
                    {
                        "role": "assistant",
                        "content": f"[{persona_name}]: {msg_text[:20]}",
                    }
                )
            else:
                formatted_history.append({"role": "user", "content": msg_text})
                short_history_logging.append({"role": "user", "content": msg_text})

        logger.info(f"\n short_history:\n {short_history_logging}")
        return formatted_history
    except SlackApiError as e:
        logger.error(f"Error fetching thread history: {e.response['error']}")
        return []


def _sanitize_and_truncate_slack_text(text: str, max_chars: int = 3800) -> str:
    """Ensures Slack message text never exceeds Slack API limits (4000 characters)."""
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    # Break cleanly at last newline if possible
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
) -> dict[str, Any]:
    """
    Posts a live updatable summary/notes message inside the given Slack thread.
    Optionally pins the note message for quick discovery.
    """
    text = _sanitize_and_truncate_slack_text(text)

    if not SLACK_BOT_TOKEN or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token":
        mock_ts = f"mock_note_{int(time.time() * 1000)}.000"
        logger.info(
            f"[MOCK NOTE] Posted mock thread note in channel {channel_id}, thread {thread_ts}: {mock_ts}"
        )
        return {"ok": True, "ts": mock_ts, "mock": True}

    try:
        response = await slack_async_client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=text,
            username=username,
            icon_emoji=icon_emoji,
        )
        msg_ts = response.get("ts")
        logger.info(
            f"[SLACK NOTE] Posted live note message {msg_ts} in thread {thread_ts} of channel {channel_id} ({username})"
        )

        if pin and msg_ts:
            try:
                await slack_async_client.pins_add(channel=channel_id, timestamp=msg_ts)
                logger.info(f"[SLACK NOTE] Pinned note message {msg_ts} to channel {channel_id}")
            except SlackApiError as pin_err:
                err = pin_err.response.get("error", "")
                if err != "already_pinned":
                    logger.debug(f"[SLACK NOTE] Note message pinning skipped/failed: {err}")
            except Exception as pin_ex:
                logger.debug(f"[SLACK NOTE] Note message pinning skipped: {pin_ex}")

        return {"ok": True, "ts": msg_ts}
    except SlackApiError as e:
        logger.error(f"[SLACK NOTE] Error posting thread note: {e.response.get('error', e)}")
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.error(f"[SLACK NOTE] Unexpected error posting thread note: {e}")
        return {"ok": False, "error": str(e)}


async def update_thread_note(
    channel_id: str,
    message_ts: str,
    text: str,
) -> dict[str, Any]:
    """
    Updates an existing live summary/notes message in-place via Slack chat.update.
    """
    text = _sanitize_and_truncate_slack_text(text)

    if (
        not SLACK_BOT_TOKEN
        or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token"
        or (message_ts and str(message_ts).startswith("mock_"))
    ):
        logger.info(
            f"[MOCK NOTE] Updated mock note {message_ts} ({len(text)} chars)"
        )
        return {"ok": True, "ts": message_ts, "mock": True}

    try:
        await slack_async_client.chat_update(
            channel=channel_id,
            ts=message_ts,
            text=text,
        )
        logger.info(f"[SLACK NOTE] Successfully updated live note message {message_ts}")
        return {"ok": True, "ts": message_ts}
    except SlackApiError as e:
        logger.error(f"[SLACK NOTE] Error updating note {message_ts}: {e.response.get('error', e)}")
        return {"ok": False, "error": str(e)}
    except Exception as e:
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
    except Exception as e:
        logger.debug(f"[SLACK] Could not fetch channel name for {channel_id}: {e}")

    fallback = f"#{channel_id}"
    CHANNEL_NAME_CACHE[channel_id] = fallback
    return fallback


async def get_or_create_usage_channel() -> str | None:
    """
    Resolves the ID of the 'cailogics-usage' channel.
    1. Checks USAGE_CHANNEL_ID env/config if specified.
    2. Checks in-memory cached channel ID.
    3. Searches workspace channels for 'cailogics-usage' (or USAGE_CHANNEL_NAME).
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
        # Search public and private channels
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
                    # Attempt to join channel if bot is not already a member
                    if not ch.get("is_member", True) and ch_id:
                        try:
                            await slack_async_client.conversations_join(channel=ch_id)
                            logger.info(f"[SLACK] Joined existing usage channel '{target_name}' ({ch_id})")
                        except Exception as join_ex:
                            logger.debug(f"[SLACK] Non-critical join attempt for {target_name}: {join_ex}")
                    return ch_id

            cursor = res.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        # Channel was not found in workspace -> automatically create it
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
            except Exception:
                pass
        logger.error(f"[SLACK] Error resolving/creating usage channel '{target_name}': {e}")
    except Exception as e:
        logger.error(f"[SLACK] Unexpected error resolving/creating usage channel: {e}")

    return None


async def post_usage_message(
    channel_id: str,
    text: str,
    username: str = "CaiLogics Usage Analyst",
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
        )
        msg_ts = response.get("ts")
        logger.info(
            f"[SLACK USAGE] Posted usage message {msg_ts} in channel {channel_id}"
        )
        return {"ok": True, "ts": msg_ts}
    except SlackApiError as e:
        logger.error(f"[SLACK USAGE] Error posting usage message: {e.response.get('error', e)}")
        return {"ok": False, "error": str(e)}
    except Exception as e:
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
        logger.error(f"[SLACK USAGE] Error updating usage message {message_ts}: {e.response.get('error', e)}")
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.error(f"[SLACK USAGE] Unexpected error updating usage message {message_ts}: {e}")
        return {"ok": False, "error": str(e)}


