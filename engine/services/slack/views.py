import logging
from typing import Any

from slack_sdk.errors import SlackApiError

from services.slack.client_core import get_slack_async_client, get_slack_bot_token

logger = logging.getLogger(__name__)


async def publish_app_home(
    user_id: str,
    enabled_agents: set[str] | None = None,
    circuit_breaker_config: dict[str, Any] | None = None,
    scribe_enabled: bool | None = None,
) -> dict[str, Any]:
    """
    Publishes the multi-agent control panel view to the user's Slack App Home tab.
    """
    bot_token = get_slack_bot_token()
    if not bot_token or bot_token == "xoxb-your-bot-user-oauth-token":
        logger.info(f"[MOCK APP HOME] Would publish App Home view to user {user_id}")
        return {"ok": True, "mock": True}

    try:
        from services.slack_home import build_app_home_view

        view = build_app_home_view(
            enabled_agents=enabled_agents,
            circuit_breaker_config=circuit_breaker_config,
            scribe_enabled=scribe_enabled,
            user_id=user_id,
        )
        client = get_slack_async_client()
        response = await client.views_publish(
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


async def safe_publish_home(user_id: str) -> None:
    """
    Safely syncs settings and publishes the App Home view, swallowing and logging any exceptions.
    Suitable for use in FastAPI background tasks or event callbacks.
    """
    try:
        from orchestration.agent_settings import sync_settings_if_modified

        sync_settings_if_modified()
        await publish_app_home(user_id=user_id)
    except SlackApiError as e:
        logger.error(f"[SLACK] SlackApiError publishing App Home for {user_id}: {e}")
    except Exception as e:  # noqa: BLE001 - Resilient to Slack errors in background tasks
        logger.error(f"[SLACK] Failed to publish App Home for {user_id}: {e}")
