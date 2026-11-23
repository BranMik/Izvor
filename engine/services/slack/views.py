import asyncio
import hashlib
import json
import logging
from typing import Any
import weakref

from slack_sdk.errors import SlackApiError

from core.slack_security import sanitize_slack_blocks
from services.rate_limiter import slack_rate_limiter
from services.slack.client_core import get_slack_async_client, get_slack_bot_token

logger = logging.getLogger(__name__)

_MAX_VIEW_HASH_CACHE = 1000
_LAST_PUBLISHED_VIEW_HASH: dict[str, str] = {}
_LOOP_USER_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = (
    weakref.WeakKeyDictionary()
)


def _get_user_home_lock(user_id: str) -> asyncio.Lock:
    """Returns a per-user asyncio.Lock scoped safely to the active event loop."""
    loop = asyncio.get_running_loop()
    per_loop = _LOOP_USER_LOCKS.setdefault(loop, {})
    lock = per_loop.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        per_loop[user_id] = lock
    return lock


def reset_app_home_publish_cache() -> None:
    """Clears App Home published view deduplication hashes and lock entries."""
    _LAST_PUBLISHED_VIEW_HASH.clear()
    _LOOP_USER_LOCKS.clear()


async def publish_app_home(
    user_id: str,
    enabled_agents: set[str] | None = None,
    circuit_breaker_config: dict[str, Any] | None = None,
    scribe_enabled: bool | None = None,
) -> dict[str, Any]:
    """
    Publishes the multi-agent control panel view to the user's Slack App Home tab.
    Serializes per user_id, sanitizes Block Kit payloads, deduplicates identical view hashes,
    and respects slack_rate_limiter.
    """
    bot_token = get_slack_bot_token()
    if not bot_token or bot_token == "xoxb-your-bot-user-oauth-token":
        logger.info(f"[MOCK APP HOME] Would publish App Home view to user {user_id}")
        return {"ok": True, "mock": True}

    lock = _get_user_home_lock(user_id)
    async with lock:
        try:
            from services.slack_home import build_app_home_view

            view = build_app_home_view(
                enabled_agents=enabled_agents,
                circuit_breaker_config=circuit_breaker_config,
                scribe_enabled=scribe_enabled,
                user_id=user_id,
            )

            if isinstance(view, dict) and isinstance(view.get("blocks"), list):
                sanitized_blocks, matched_rule = sanitize_slack_blocks(view["blocks"], user_id=user_id)
                if matched_rule:
                    logger.warning(
                        f"[SLACK] App Home blocks for user {user_id} blocked by security policy: {matched_rule}"
                    )
                    view = {
                        **view,
                        "blocks": [
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": f"⚠️ *App Home content blocked by security policy.* (Rule: {matched_rule})",
                                },
                            }
                        ],
                    }
                else:
                    view = {**view, "blocks": sanitized_blocks}

            view_hash = hashlib.sha256(
                json.dumps(view, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
            if _LAST_PUBLISHED_VIEW_HASH.get(user_id) == view_hash:
                logger.debug(f"[SLACK] Deduplicated unchanged App Home view for user {user_id}")
                return {"ok": True, "deduplicated": True}

            await slack_rate_limiter.acquire()
            client = get_slack_async_client()
            response = await client.views_publish(
                user_id=user_id,
                view=view,
            )
            if len(_LAST_PUBLISHED_VIEW_HASH) >= _MAX_VIEW_HASH_CACHE and user_id not in _LAST_PUBLISHED_VIEW_HASH:
                oldest_key = next(iter(_LAST_PUBLISHED_VIEW_HASH))
                _LAST_PUBLISHED_VIEW_HASH.pop(oldest_key, None)
            _LAST_PUBLISHED_VIEW_HASH[user_id] = view_hash

            logger.info(f"[SLACK] Published App Home view for user {user_id}")
            return response.data if hasattr(response, "data") else dict(response)
        except SlackApiError as e:
            err_msg = e.response.get("error", str(e)) if isinstance(e.response, dict) else str(e)
            logger.error(f"Error publishing App Home view to user {user_id}: {err_msg}")
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
        from config_store.agent_settings import sync_settings_if_modified

        sync_settings_if_modified()
        await publish_app_home(user_id=user_id)
    except SlackApiError as e:
        logger.error(f"[SLACK] SlackApiError publishing App Home for {user_id}: {e}")
    except Exception as e:  # noqa: BLE001 - Resilient to Slack errors in background tasks
        logger.error(f"[SLACK] Failed to publish App Home for {user_id}: {e}")
