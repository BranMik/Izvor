import logging
from typing import Any

from fastapi import BackgroundTasks, Response

from orchestration.agent_settings import (
    get_agents_config,
    reset_enabled_agents,
    restore_from_backup,
    set_agent_enabled,
    set_agent_model,
    settings_manager,
    toggle_agent,
)
from services.audit_logger import log_audit_event
from services.slack import (
    open_dm_channel,
    post_ephemeral_to_slack,
    post_message_to_slack,
    slack_async_client,
)
from slack_sdk.errors import SlackApiError

logger = logging.getLogger("IzvorBackend.SlackInteractivity.Agents")


async def _deliver_admin_denial_feedback(
    trigger_id: str | None,
    user_id: str | None,
    action_id: str,
    channel_id: str,
) -> None:
    """
    Safely delivers permission denial feedback to the user without crashing the ASGI lifecycle.
    1. Attempts to open an informational modal if a trigger_id is present.
    2. Catches SlackApiError (e.g. expired_trigger_id) and generic errors.
    3. If modal delivery fails or trigger_id is absent:
       - Sends ephemeral message if inside a Slack channel.
       - Sends a direct message (DM) fallback if inside App Home, ensuring the user is never left without feedback.
    """
    modal_opened = False
    clean_action = (
        action_id.split(":", 1)[0].replace("_", " ").title()
        if action_id
        else "System Configuration Change"
    )

    if trigger_id:
        try:
            from services.slack_home import build_admin_required_modal

            modal_view = build_admin_required_modal(user_id=user_id, action_id=action_id)
            await slack_async_client.views_open(trigger_id=trigger_id, view=modal_view)
            modal_opened = True
            logger.info(
                f"[INTERACTIVITY] Successfully opened Admin Access Required modal for user {user_id} (action: {action_id})."
            )
        except SlackApiError as e:
            err_code = (
                e.response.get("error", str(e))
                if hasattr(e, "response") and isinstance(e.response, dict)
                else str(e)
            )
            logger.warning(
                f"[INTERACTIVITY] views.open failed for user {user_id} (error: {err_code}). Dispatching fallback notification."
            )
        except Exception as e:  # noqa: BLE001 - Error boundary prevents modal failure from terminating background worker
            logger.warning(
                f"[INTERACTIVITY] Unexpected error calling views.open for user {user_id}: {e}. Dispatching fallback notification."
            )

    if not modal_opened and user_id:
        if channel_id:
            try:
                await post_ephemeral_to_slack(
                    channel_id=channel_id,
                    user_id=user_id,
                    text=f"⚠️ *Permission Denied:* Administrative privileges are required to perform *{clean_action}*.",
                )
            except Exception as e:  # noqa: BLE001 - Ephemeral notification isolation
                logger.warning(f"[INTERACTIVITY] Ephemeral denial post failed for {user_id}: {e}")
        else:
            # Action originated from App Home (channel_id is empty). Fallback to DM so user gets clear feedback.
            try:
                dm_channel_id = await open_dm_channel(user_id)
                if dm_channel_id:
                    dm_text = (
                        "🔒 *Permission Denied: Workspace Administrator Privileges Required*\n\n"
                        f"You attempted to perform: *{clean_action}* in Izvor Control Hub.\n\n"
                        "To protect system configuration, model routing, and spend limits, this setting can "
                        "only be modified by authorized workspace administrators.\n\n"
                        f"• Your Slack User ID is: `{user_id}`\n"
                        "• Please forward this ID to your workspace administrator to request access."
                    )
                    await post_message_to_slack(channel_id=dm_channel_id, text=dm_text)
                    logger.info(f"[INTERACTIVITY] Dispatched fallback denial DM to user {user_id}.")
            except Exception as e:  # noqa: BLE001 - DM fallback notification isolation
                logger.warning(f"[INTERACTIVITY] Fallback DM delivery failed for user {user_id}: {e}")


from services.slack.views import safe_publish_home


def _deny_non_admin_action(
    payload: dict[str, Any],
    user_id: str | None,
    action_id: str,
    background_tasks: BackgroundTasks,
) -> None:
    logger.warning(
        f"[INTERACTIVITY] Unauthorized non-admin user {user_id} attempted admin action '{action_id}'."
    )
    log_audit_event(
        event_type="ADMIN_ACTION_DENIED",
        actor={"type": "slack_user", "id": user_id or "unknown"},
        action=action_id,
        target="system_configuration",
        details={"attempted_action": action_id},
        severity="CRITICAL",
    )

    trigger_id = payload.get("trigger_id")
    channel_id = payload.get("channel", {}).get("id") or ""

    # Deliver denial feedback safely (modal -> DM/ephemeral fallback) without risking unhandled ASGI crashes
    background_tasks.add_task(
        _deliver_admin_denial_feedback,
        trigger_id=trigger_id,
        user_id=user_id,
        action_id=action_id,
        channel_id=channel_id,
    )

    # Re-sync App Home for user_id to reset uncommitted client-side widgets (e.g. dropdowns, radios)
    if user_id:
        background_tasks.add_task(safe_publish_home, user_id=user_id)


async def _handle_select_agent_model(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    action_id = action.get("action_id", "")
    selected_option = action.get("selected_option", {})
    val = selected_option.get("value", "")
    agent_key = action_id.split(":", 1)[1]
    if agent_key and val:
        logger.info(f"[INTERACTIVITY] Changing model for agent '{agent_key}' to '{val}'")
        set_agent_model(agent_key, val)
        log_audit_event(
            event_type="ADMIN_CONFIG_CHANGE",
            actor={"type": "slack_admin", "id": user_id or "unknown"},
            action="set_agent_model",
            target=f"agent:{agent_key}.model",
            details={"agent": agent_key, "model": val},
            severity="INFO",
        )
    return None


async def _handle_toggle_agent_radio(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    selected_option = action.get("selected_option", {})
    val = selected_option.get("value", "")
    if ":" in val:
        agent_key, state = val.split(":", 1)
        set_agent_enabled(agent_key, state == "on")
        log_audit_event(
            event_type="ADMIN_CONFIG_CHANGE",
            actor={"type": "slack_admin", "id": user_id or "unknown"},
            action="toggle_agent",
            target=f"agent:{agent_key}.enabled",
            details={"agent": agent_key, "enabled": state == "on"},
            severity="INFO",
        )
    return None


async def _handle_toggle_agent(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    agent_key = action.get("value")
    if agent_key:
        toggle_agent(agent_key)
        log_audit_event(
            event_type="ADMIN_CONFIG_CHANGE",
            actor={"type": "slack_admin", "id": user_id or "unknown"},
            action="toggle_agent",
            target=f"agent:{agent_key}.enabled",
            details={"agent": agent_key},
            severity="INFO",
        )
    return None


async def _handle_bulk_enable_all(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    reset_enabled_agents()
    log_audit_event(
        event_type="ADMIN_CONFIG_CHANGE",
        actor={"type": "slack_admin", "id": user_id or "unknown"},
        action="bulk_enable_all_agents",
        target="agents.enabled",
        details={"action": "reset_enabled_agents"},
        severity="INFO",
    )
    return None


async def _handle_bulk_disable(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    action_id = action.get("action_id", "")
    for key in get_agents_config():
        set_agent_enabled(key, False, save=False)
    settings_manager.save()
    log_audit_event(
        event_type="ADMIN_CONFIG_CHANGE",
        actor={"type": "slack_admin", "id": user_id or "unknown"},
        action="bulk_disable_agents",
        target="agents.enabled",
        details={"mode": action_id},
        severity="WARNING",
    )
    return None


async def _handle_restore_from_backup(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    success = restore_from_backup()
    logger.info(
        f"[INTERACTIVITY] restore_from_backup invoked by user {user_id}: success={success}"
    )
    log_audit_event(
        event_type="WORKFLOW_PRESET_MUTATION",
        actor={"type": "slack_admin", "id": user_id or "unknown"},
        action="restore_from_backup",
        target="agent_settings_backup",
        details={"success": success},
        severity="WARNING",
    )
    return None
