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
from services.slack import post_ephemeral_to_slack, slack_async_client

logger = logging.getLogger("IzvorBackend.SlackInteractivity.Agents")


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
    if trigger_id:
        from services.slack_home import build_admin_required_modal

        modal_view = build_admin_required_modal(user_id=user_id, action_id=action_id)
        background_tasks.add_task(
            slack_async_client.views_open,
            trigger_id=trigger_id,
            view=modal_view,
        )

    # Re-sync App Home for user_id to reset uncommitted client-side widgets (e.g. dropdowns, radios)
    if user_id:
        from services.slack.views import publish_app_home

        background_tasks.add_task(publish_app_home, user_id=user_id)

    channel_id = payload.get("channel", {}).get("id") or ""
    if channel_id and user_id:
        background_tasks.add_task(
            post_ephemeral_to_slack,
            channel_id=channel_id,
            user_id=user_id,
            text="⚠️ *Permission Denied:* Administrative privileges are required to modify system settings.",
        )


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
            severity="CRITICAL",
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
            severity="CRITICAL",
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
            severity="CRITICAL",
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
        severity="CRITICAL",
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
        severity="CRITICAL",
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
        severity="CRITICAL",
    )
    return None
