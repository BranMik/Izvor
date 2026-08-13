import logging
from typing import List, Dict, Any
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.errors import SlackApiError
from config import SLACK_BOT_TOKEN

logger = logging.getLogger(__name__)

# Initialize async web client
slack_async_client = AsyncWebClient(token=SLACK_BOT_TOKEN)

async def post_agent_response(
    channel_id: str,
    thread_ts: str,
    text: str,
    username: str,
    icon_emoji: str
) -> Dict[str, Any]:
    """
    Posts a message to Slack thread using dynamic persona fields (chat:write.customize).
    """
    if not SLACK_BOT_TOKEN or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token":
        logger.warning(
            f"[MOCK POST] Channel: {channel_id}, Thread: {thread_ts}, Persona: {username} ({icon_emoji})\nText: {text}"
        )
        return {"ok": True, "mock": True}

    try:
        response = await slack_async_client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=text,
            username=username,
            icon_emoji=icon_emoji
        )
        return response.data
    except SlackApiError as e:
        logger.error(f"Error posting message to Slack: {e.response['error']}")
        raise e

async def fetch_thread_history(
    channel_id: str,
    thread_ts: str,
    bot_user_id: str = None
) -> List[Dict[str, str]]:
    """
    Fetches past messages in a Slack thread and formats them as LiteLLM conversation history.
    """
    if not SLACK_BOT_TOKEN or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token":
        return []

    try:
        response = await slack_async_client.conversations_replies(
            channel=channel_id,
            ts=thread_ts,
            limit=50
        )
        messages = response.get("messages", [])
        formatted_history: List[Dict[str, str]] = []

        for msg in messages:
            msg_text = msg.get("text", "")
            username = msg.get("username")
            subtype = msg.get("subtype")
            user_id = msg.get("user")

            # Determine role
            if subtype == "bot_message" or username or user_id == bot_user_id:
                persona_name = username if username else "Assistant"
                formatted_history.append({
                    "role": "assistant",
                    "content": f"[{persona_name}]: {msg_text}"
                })
            else:
                formatted_history.append({
                    "role": "user",
                    "content": msg_text
                })

        return formatted_history
    except SlackApiError as e:
        logger.error(f"Error fetching thread history: {e.response['error']}")
        return []
