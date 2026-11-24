"""Slack interactive actions for workflow presets, chains, orchestration modes, and workspace context."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import BackgroundTasks, Response

from config_store.agent_settings import (
    apply_workflow,
    get_active_workflow_id,
    get_agents_config,
    get_fixed_config,
    get_orchestration_mode,
    get_sandwich_config,
    persist_active_workflow,
    set_active_workflow_id,
    set_fixed_config,
    set_orchestration_mode,
    set_sandwich_config,
)
from config_store.app_settings import get_workspace_context
from config_store.workflow_manager import delete_workflow, save_workflow
from core.config import is_slack_admin
from core.models import REQUIRED_DEFAULT_WORKFLOW_IDS, OrchestrationMode
from services.audit_logger import log_audit_event_async
from services.rate_limiter import slack_rate_limiter
from services.slack import (
    get_slack_async_client,
    open_dm_channel,
    post_message_to_slack,
)
from slack_sdk.errors import SlackApiError

logger = logging.getLogger("IzvorBackend.SlackInteractivity.WorkflowPresets")


def _validate_chain_agent_keys(raw_value: str) -> tuple[list[str], list[str]]:
    """
    Parses comma-separated agent keys or names and validates them against known configured agents.
    Returns (valid_keys, invalid_keys).
    """
    from pipeline.entity_resolver import entity_resolver

    known_keys = set(get_agents_config().keys())
    tokens = [k.strip() for k in raw_value.split(",") if k.strip()]
    valid: list[str] = []
    invalid: list[str] = []
    for token in tokens:
        if token in known_keys:
            valid.append(token)
        else:
            resolved = entity_resolver.resolve_agent_name(token)
            if resolved and resolved in known_keys:
                valid.append(resolved)
            else:
                invalid.append(token)
    return valid, invalid


def _extract_selected_preset_id(view_state: dict[str, Any]) -> str | None:
    if isinstance(view_state, dict):
        for block_val in view_state.values():
            if isinstance(block_val, dict) and "select_workflow_preset" in block_val:
                sel_opt = block_val["select_workflow_preset"].get("selected_option") or {}
                val = sel_opt.get("value", "")
                if val.startswith("wf:"):
                    return val.split(":", 1)[1]
    return get_active_workflow_id()


async def _handle_select_workflow_preset(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    selected_option = action.get("selected_option", {})
    val = selected_option.get("value", "")
    if val.startswith("wf:"):
        wf_id = val.split(":", 1)[1]
        if wf_id and wf_id != "none":
            applied = bool(apply_workflow(wf_id))
            actor_type = "slack_admin" if is_slack_admin(user_id) else "slack_user"
            await log_audit_event_async(
                event_type="WORKFLOW_PRESET_MUTATION",
                actor={"type": actor_type, "id": user_id or "unknown"},
                action="apply_workflow_preset",
                target=f"workflow:{wf_id}",
                details={"workflow_id": wf_id, "applied": applied},
                severity="INFO" if applied else "WARNING",
                status="SUCCESS" if applied else "FAILURE",
            )
            if not applied and user_id:
                dm_id = await open_dm_channel(user_id)
                if dm_id:
                    await post_message_to_slack(
                        channel_id=dm_id,
                        text=f"⚠️ *Failed to apply workflow preset `{wf_id}`.* It may be missing or corrupted.",
                    )
    return None


async def _handle_load_workflow_preset_button(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    selected_wf_id = _extract_selected_preset_id(view_state)
    if selected_wf_id and selected_wf_id != "none":
        applied = bool(apply_workflow(selected_wf_id))
        actor_type = "slack_admin" if is_slack_admin(user_id) else "slack_user"
        await log_audit_event_async(
            event_type="WORKFLOW_PRESET_MUTATION",
            actor={"type": actor_type, "id": user_id or "unknown"},
            action="apply_workflow_preset",
            target=f"workflow:{selected_wf_id}",
            details={"workflow_id": selected_wf_id, "applied": applied},
            severity="INFO" if applied else "WARNING",
            status="SUCCESS" if applied else "FAILURE",
        )
        if not applied and user_id:
            dm_id = await open_dm_channel(user_id)
            if dm_id:
                await post_message_to_slack(
                    channel_id=dm_id,
                    text=f"⚠️ *Failed to load workflow preset `{selected_wf_id}`.* It may be missing or corrupted.",
                )
    return None


async def _handle_delete_workflow_preset_button(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    selected_wf_id = _extract_selected_preset_id(view_state)
    if selected_wf_id and selected_wf_id != "none":
        if selected_wf_id in REQUIRED_DEFAULT_WORKFLOW_IDS:
            logger.warning(f"[WORKFLOW] Cannot delete default system preset '{selected_wf_id}'")
            return None

        deleted = bool(delete_workflow(selected_wf_id))
        if deleted and get_active_workflow_id() == selected_wf_id:
            set_active_workflow_id(None)
        await log_audit_event_async(
            event_type="WORKFLOW_PRESET_MUTATION",
            actor={"type": "slack_admin", "id": user_id or "unknown"},
            action="delete_workflow_preset",
            target=f"workflow:{selected_wf_id}",
            details={"workflow_id": selected_wf_id, "deleted": deleted},
            severity="INFO" if deleted else "WARNING",
            status="SUCCESS" if deleted else "FAILURE",
        )
    return None


async def _handle_save_workflow_preset_button(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    preset_name = ""
    if isinstance(view_state, dict):
        for block_val in view_state.values():
            if isinstance(block_val, dict) and "save_workflow_preset_name_input" in block_val:
                preset_name = block_val["save_workflow_preset_name_input"].get("value") or ""
                if preset_name:
                    break
        if not preset_name:
            block_vals = view_state.get("save_workflow_preset_block", {})
            if isinstance(block_vals, dict):
                preset_name = block_vals.get("save_workflow_preset_name_input", {}).get("value") or ""
    preset_name = str(preset_name).strip()
    if not preset_name:
        return None

    mode = get_orchestration_mode()
    proposed_fixed: dict[str, Any] | None = None
    proposed_sandwich: dict[str, Any] | None = None

    if mode == OrchestrationMode.FIXED.value:
        proposed_fixed = dict(get_fixed_config())
        if isinstance(view_state, dict):
            for block_val in view_state.values():
                if isinstance(block_val, dict) and "set_fixed_chain_input" in block_val:
                    f_val = block_val["set_fixed_chain_input"].get("value")
                    if f_val is not None:
                        f_chain, invalid_f = _validate_chain_agent_keys(f_val)
                        if invalid_f:
                            logger.warning(f"[INTERACTIVITY] Unknown fixed chain agent keys rejected: {invalid_f}")
                        proposed_fixed = {"chain": f_chain}
                        break
    else:
        curr_sandwich = get_sandwich_config()
        start_chain = list(curr_sandwich.get("start_chain", []))
        end_chain = list(curr_sandwich.get("end_chain", []))
        if isinstance(view_state, dict):
            s_val = None
            e_val = None
            for block_val in view_state.values():
                if isinstance(block_val, dict):
                    if "set_start_chain_input" in block_val:
                        s_val = block_val["set_start_chain_input"].get("value")
                    if "set_end_chain_input" in block_val:
                        e_val = block_val["set_end_chain_input"].get("value")
            if s_val is not None:
                start_chain = _validate_chain_agent_keys(s_val)[0]
            if e_val is not None:
                end_chain = _validate_chain_agent_keys(e_val)[0]
        proposed_sandwich = {"start_chain": start_chain, "end_chain": end_chain}

    try:
        # Validate & persist preset BEFORE mutating active chains/active_workflow_id in RAM/agent_settings
        if mode == OrchestrationMode.FIXED.value:
            saved = save_workflow(
                name=preset_name,
                mode=mode,
                fixed_config=proposed_fixed,
            )
            if proposed_fixed is not None:
                set_fixed_config(proposed_fixed.get("chain", []))
        else:
            saved = save_workflow(
                name=preset_name,
                mode=mode,
                sandwich_config=proposed_sandwich,
            )
            if proposed_sandwich is not None:
                set_sandwich_config(
                    proposed_sandwich.get("start_chain", []),
                    proposed_sandwich.get("end_chain", []),
                )
        set_active_workflow_id(saved.id)
        await log_audit_event_async(
            event_type="WORKFLOW_PRESET_MUTATION",
            actor={"type": "slack_admin", "id": user_id or "unknown"},
            action="save_workflow_preset",
            target=f"workflow:{saved.id}",
            details={"name": preset_name, "mode": mode, "workflow_id": saved.id},
            severity="INFO",
            status="SUCCESS",
        )
    except (ValueError, OSError) as ve:
        logger.warning(f"[WORKFLOW] Cannot save preset '{preset_name}': {ve}")
        await log_audit_event_async(
            event_type="WORKFLOW_PRESET_MUTATION",
            actor={"type": "slack_admin", "id": user_id or "unknown"},
            action="save_workflow_preset",
            target=f"workflow:{preset_name}",
            details={"name": preset_name, "mode": mode, "error": str(ve)},
            severity="WARNING",
            status="FAILURE",
        )
        if user_id:
            dm_id = await open_dm_channel(user_id)
            if dm_id:
                await post_message_to_slack(
                    channel_id=dm_id,
                    text=(
                        f"⚠️ *Cannot save preset '{preset_name}'*: {ve}. "
                        "System default presets are reserved and immutable."
                    ),
                )
    return None


async def _handle_select_orchestration_mode(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    selected_option = action.get("selected_option", {})
    val = selected_option.get("value", "")
    if val.startswith("mode:"):
        mode = val.split(":", 1)[1]
        set_orchestration_mode(mode)
        await log_audit_event_async(
            event_type="ADMIN_CONFIG_CHANGE",
            actor={"type": "slack_admin", "id": user_id or "unknown"},
            action="select_orchestration_mode",
            target="orchestration.mode",
            details={"mode": mode},
            severity="INFO",
        )
    return None


async def _reject_if_reserved_preset(
    user_id: str | None,
    *,
    action_name: str,
    target: str,
) -> bool:
    """Blocks in-place chain mutations when a reserved system default preset is active."""
    active_wf = get_active_workflow_id()
    if active_wf and active_wf in REQUIRED_DEFAULT_WORKFLOW_IDS:
        logger.warning(f"[WORKFLOW] Blocked in-place chain mutation '{action_name}' on reserved preset '{active_wf}'")
        await log_audit_event_async(
            event_type="ADMIN_CONFIG_CHANGE",
            actor={"type": "slack_admin", "id": user_id or "unknown"},
            action=action_name,
            target=target,
            details={"workflow_id": active_wf, "reason": "reserved_default_preset"},
            severity="WARNING",
            status="BLOCKED",
        )
        if user_id:
            dm_id = await open_dm_channel(user_id)
            if dm_id:
                await post_message_to_slack(
                    channel_id=dm_id,
                    text=(
                        f"⚠️ *System default preset `{active_wf}` is read-only.* "
                        "Use *Save Preset* to create a custom preset with modified chains."
                    ),
                )
        return True
    return False


async def _handle_save_sandwich_chains_button(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    if await _reject_if_reserved_preset(
        user_id, action_name="save_sandwich_chains_button", target="orchestration.sandwich_config"
    ):
        return None
    start_val = ""
    end_val = ""
    if isinstance(view_state, dict):
        for block_val in view_state.values():
            if isinstance(block_val, dict):
                if "set_start_chain_input" in block_val:
                    start_val = block_val["set_start_chain_input"].get("value") or ""
                if "set_end_chain_input" in block_val:
                    end_val = block_val["set_end_chain_input"].get("value") or ""
    start_valid, start_invalid = _validate_chain_agent_keys(start_val)
    end_valid, end_invalid = _validate_chain_agent_keys(end_val)
    if start_invalid or end_invalid:
        logger.warning(
            f"[INTERACTIVITY] Unknown agent keys rejected in sandwich chains: "
            f"start_invalid={start_invalid}, end_invalid={end_invalid}"
        )
    set_sandwich_config(start_valid, end_valid)
    persisted = bool(persist_active_workflow())
    await log_audit_event_async(
        event_type="ADMIN_CONFIG_CHANGE",
        actor={"type": "slack_admin", "id": user_id or "unknown"},
        action="save_sandwich_chains_button",
        target="orchestration.sandwich_config",
        details={
            "start_chain": start_valid,
            "end_chain": end_valid,
            "invalid_keys": start_invalid + end_invalid,
            "persisted": persisted,
        },
        severity="INFO",
        status="SUCCESS",
    )
    return None


async def _handle_clear_sandwich_chains_button(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    if await _reject_if_reserved_preset(
        user_id, action_name="clear_sandwich_chains_button", target="orchestration.sandwich_config"
    ):
        return None
    set_sandwich_config([], [])
    persist_active_workflow()
    await log_audit_event_async(
        event_type="ADMIN_CONFIG_CHANGE",
        actor={"type": "slack_admin", "id": user_id or "unknown"},
        action="clear_sandwich_chains_button",
        target="orchestration.sandwich_config",
        details={"cleared": True},
        severity="INFO",
        status="SUCCESS",
    )
    return None


async def _handle_set_start_chain_input(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    curr_sandwich = get_sandwich_config()
    val_str = action.get("value", "")
    valid, invalid = _validate_chain_agent_keys(val_str)
    if invalid:
        logger.warning(f"[INTERACTIVITY] Unknown agent keys rejected in start_chain: {invalid}")
    set_sandwich_config(valid, curr_sandwich.get("end_chain", []))
    await log_audit_event_async(
        event_type="ADMIN_CONFIG_CHANGE",
        actor={"type": "slack_admin", "id": user_id or "unknown"},
        action="set_start_chain_input",
        target="orchestration.sandwich_config.start_chain",
        details={"start_chain": valid, "invalid_keys": invalid},
        severity="INFO",
    )
    return None


async def _handle_set_end_chain_input(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    curr_sandwich = get_sandwich_config()
    val_str = action.get("value", "")
    valid, invalid = _validate_chain_agent_keys(val_str)
    if invalid:
        logger.warning(f"[INTERACTIVITY] Unknown agent keys rejected in end_chain: {invalid}")
    set_sandwich_config(curr_sandwich.get("start_chain", []), valid)
    await log_audit_event_async(
        event_type="ADMIN_CONFIG_CHANGE",
        actor={"type": "slack_admin", "id": user_id or "unknown"},
        action="set_end_chain_input",
        target="orchestration.sandwich_config.end_chain",
        details={"end_chain": valid, "invalid_keys": invalid},
        severity="INFO",
    )
    return None


async def _handle_save_fixed_chain_button(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    if await _reject_if_reserved_preset(
        user_id, action_name="save_fixed_chain_button", target="orchestration.fixed_config"
    ):
        return None
    fixed_val = ""
    if isinstance(view_state, dict):
        for block_val in view_state.values():
            if isinstance(block_val, dict) and "set_fixed_chain_input" in block_val:
                fixed_val = block_val["set_fixed_chain_input"].get("value") or ""
    valid, invalid = _validate_chain_agent_keys(fixed_val)
    if invalid:
        logger.warning(f"[INTERACTIVITY] Unknown agent keys rejected in fixed chain: {invalid}")
    set_fixed_config(valid)
    persisted = bool(persist_active_workflow())
    await log_audit_event_async(
        event_type="ADMIN_CONFIG_CHANGE",
        actor={"type": "slack_admin", "id": user_id or "unknown"},
        action="save_fixed_chain_button",
        target="orchestration.fixed_config",
        details={"fixed_chain": valid, "invalid_keys": invalid, "persisted": persisted},
        severity="INFO",
        status="SUCCESS",
    )
    return None


async def _handle_clear_fixed_chain_button(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    if await _reject_if_reserved_preset(
        user_id, action_name="clear_fixed_chain_button", target="orchestration.fixed_config"
    ):
        return None
    set_fixed_config([])
    persist_active_workflow()
    await log_audit_event_async(
        event_type="ADMIN_CONFIG_CHANGE",
        actor={"type": "slack_admin", "id": user_id or "unknown"},
        action="clear_fixed_chain_button",
        target="orchestration.fixed_config",
        details={"cleared": True},
        severity="INFO",
        status="SUCCESS",
    )
    return None


async def _handle_set_fixed_chain_input(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    val_str = action.get("value", "")
    valid, invalid = _validate_chain_agent_keys(val_str)
    if invalid:
        logger.warning(f"[INTERACTIVITY] Unknown agent keys rejected in set_fixed_chain_input: {invalid}")
    set_fixed_config(valid)
    await log_audit_event_async(
        event_type="ADMIN_CONFIG_CHANGE",
        actor={"type": "slack_admin", "id": user_id or "unknown"},
        action="set_fixed_chain_input",
        target="orchestration.fixed_config",
        details={"fixed_chain": valid, "invalid_keys": invalid},
        severity="INFO",
    )
    return None


async def _open_workspace_context_modal_safe(trigger_id: str, modal_view: dict[str, Any]) -> None:
    """Safely opens the workspace context modal in the background with rate limiting and error isolation."""
    try:
        await slack_rate_limiter.acquire()
        await get_slack_async_client().views_open(
            trigger_id=trigger_id,
            view=modal_view,
        )
    except SlackApiError as e:
        err_code = e.response.get("error", str(e)) if getattr(e, "response", None) else str(e)
        logger.warning(f"[WORKFLOW] views.open failed for workspace context modal (error: {err_code})")
    except Exception as e:  # noqa: BLE001 - Background modal boundary prevents unhandled task errors
        logger.warning(f"[WORKFLOW] Unexpected error opening workspace context modal: {e}")


async def _handle_open_edit_workspace_context_modal(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    from services.slack_home import build_edit_workspace_context_modal

    trigger_id = payload.get("trigger_id")
    if trigger_id:
        modal_view = build_edit_workspace_context_modal(current_context=get_workspace_context())
        background_tasks.add_task(
            _open_workspace_context_modal_safe,
            trigger_id=trigger_id,
            modal_view=modal_view,
        )
    return None

