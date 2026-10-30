import logging
from typing import Any

from fastapi import BackgroundTasks, Response

from agents.thread_notes_agent import (
    get_thread_conclusion_content,
    get_thread_note_content,
)
from agents.usage_agent import calculate_thread_costs
from core.models import OrchestrationMode
from orchestration.agent_settings import (
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
from orchestration.app_settings import (
    get_circuit_breaker_threshold,
    get_daily_budget_usd,
    set_circuit_breaker_enabled,
    set_circuit_breaker_threshold,
    set_convergence_fast_forward_enabled,
    set_daily_budget_usd,
    set_scribe_enabled,
    set_weekly_pricing_update_enabled,
)
from orchestration.workflow_manager import (
    delete_workflow,
    save_workflow,
)
from services.audit_logger import log_audit_event
from services.slack import (
    open_dm_channel,
    post_ephemeral_to_slack,
    slack_async_client,
    update_slack_message,
    upload_text_file_to_slack,
)

logger = logging.getLogger("IzvorBackend.SlackInteractivity.Workflows")


def _validate_chain_agent_keys(raw_value: str) -> tuple[list[str], list[str]]:
    """
    Parses comma-separated agent keys and validates them against known configured agents.
    Returns (valid_keys, invalid_keys).
    """
    known_keys = set(get_agents_config().keys())
    tokens = [k.strip() for k in raw_value.split(",") if k.strip()]
    valid: list[str] = []
    invalid: list[str] = []
    for token in tokens:
        if token in known_keys:
            valid.append(token)
        else:
            invalid.append(token)
    return valid, invalid


async def _handle_scribe_download(
    user_id: str,
    channel_id: str,
    thread_ts: str,
    content: str,
    filename_prefix: str = "scribe_notes",
    title_prefix: str = "Live Discussion Scribe Notes",
) -> None:
    """
    Delivers notes privately to the requesting user:
    1. Sends an ephemeral confirmation in the thread visible only to user.
    2. Uploads the .txt file directly to the user's private DM channel.
    """
    try:
        if channel_id and thread_ts:
            await post_ephemeral_to_slack(
                channel_id=channel_id,
                user_id=user_id,
                text="✅ Prepared notes have been sent as a `.txt` file to your direct message (DM).",
                thread_ts=thread_ts,
            )

        dm_channel_id = await open_dm_channel(user_id) or user_id
        clean_ts = thread_ts.replace(".", "_") if thread_ts else "notes"
        filename = f"{filename_prefix}_{clean_ts}.txt"
        await upload_text_file_to_slack(
            channel_id=dm_channel_id,
            content=content,
            filename=filename,
            title=f"{title_prefix} ({thread_ts})",
            initial_comment=f"Here are your requested {title_prefix} as a .txt file:",
        )
    except Exception as e:  # noqa: BLE001 - Interactivity action handler isolates download delivery failure
        logger.error(f"[INTERACTIVITY] Error delivering scribe download to {user_id}: {e}")


async def _handle_download_scribe_notes(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    action_id = action.get("action_id", "")
    val = action.get("value", "")
    channel_id = ""
    thread_ts = ""
    if ":" in val:
        parts = val.split(":", 1)
        channel_id, thread_ts = parts[0], parts[1]
    else:
        channel_id = payload.get("channel", {}).get("id") or ""
        thread_ts = (
            payload.get("container", {}).get("thread_ts")
            or payload.get("message", {}).get("thread_ts")
            or ""
        )

    is_conclusion = action_id == "download_conclusion_notes_txt"
    if is_conclusion:
        notes_text = (
            get_thread_conclusion_content(channel_id, thread_ts)
            if channel_id and thread_ts
            else None
        )
    else:
        notes_text = (
            get_thread_note_content(channel_id, thread_ts)
            if channel_id and thread_ts
            else None
        )

    if not notes_text:
        msg = payload.get("message", {})
        blocks = msg.get("blocks", [])
        for b in blocks:
            if b.get("type") == "section":
                notes_text = b.get("text", {}).get("text", "")
                break
        if not notes_text:
            notes_text = msg.get("text", "")
    if not notes_text:
        fallback_label = "Discussion Conclusion" if is_conclusion else "Live Discussion Scribe"
        notes_text = f"*(No {fallback_label} notes recorded for this thread)*"

    if user_id:
        background_tasks.add_task(
            _handle_scribe_download,
            user_id=user_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            content=notes_text,
            filename_prefix="conclusion_notes" if is_conclusion else "scribe_notes",
            title_prefix="Discussion Conclusion" if is_conclusion else "Live Discussion Scribe Notes",
        )
    return None


async def _handle_resume_sequence(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    channel_id = payload.get("channel", {}).get("id") or ""
    thread_ts = (
        payload.get("container", {}).get("thread_ts")
        or payload.get("message", {}).get("thread_ts")
        or ""
    )

    if channel_id and thread_ts:
        from pipeline.spend_governance import spend_governance
        from orchestration.app_settings import (
            get_circuit_breaker_threshold,
            is_circuit_breaker_enabled,
            get_daily_budget_usd,
        )

        session_id = f"{channel_id}:{thread_ts}"
        threshold = get_circuit_breaker_threshold()
        cb_enabled = is_circuit_breaker_enabled()
        daily_budget = get_daily_budget_usd()
        cost_info = calculate_thread_costs(channel_id, thread_ts)
        current_cost = cost_info.get("grand_total_cost_usd", 0.0)

        tripped, _, _ = spend_governance.is_budget_exceeded(
            session_id=session_id,
            threshold_usd=threshold,
            enabled=cb_enabled,
            daily_budget_usd=daily_budget,
            current_cost_usd=current_cost,
        )

        if tripped:
            background_tasks.add_task(
                post_ephemeral_to_slack,
                channel_id=channel_id,
                user_id=user_id,
                text=f"The budget limit (${threshold:.5f}) is still too low compared to current cost (${current_cost:.5f}). Please raise it first.",
                thread_ts=thread_ts,
            )
        else:
            msg = payload.get("message", {})
            if msg and msg.get("ts"):
                background_tasks.add_task(
                    update_slack_message,
                    channel_id=channel_id,
                    ts=msg["ts"],
                    text=f"✅ Sequence resumed by <@{user_id}>.",
                    blocks=[],
                )
            log_audit_event(
                event_type="SEQUENCE_RESUMED",
                actor={"type": "slack_admin", "id": user_id or "unknown"},
                action="resume_sequence_action",
                target=f"{channel_id}:{thread_ts}",
                details={"channel_id": channel_id, "thread_ts": thread_ts, "resumed_by": user_id},
                severity="INFO",
            )
            from orchestration.agent_router import orchestrate_discussion
            from orchestration.thread_state import thread_state_manager

            remaining = thread_state_manager.get_remaining_agents(channel_id, thread_ts)
            next_agent = remaining[0] if remaining else None
            resume_prompt = f"/[{next_agent}]" if next_agent else ""

            task_coro = orchestrate_discussion(
                channel_id=channel_id,
                thread_ts=thread_ts,
                raw_text=resume_prompt,
                user_id=user_id,
                bot_user_id=None,
            )
            thread_state_manager.dispatch_discussion_task(channel_id, thread_ts, task_coro)
    return None


async def _handle_toggle_circuit_breaker(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    selected_option = action.get("selected_option", {})
    val = selected_option.get("value", "")
    is_on = val == "cb:on"
    set_circuit_breaker_enabled(is_on)
    log_audit_event(
        event_type="ADMIN_CONFIG_CHANGE",
        actor={"type": "slack_admin", "id": user_id or "unknown"},
        action="toggle_circuit_breaker",
        target="circuit_breaker.enabled",
        details={"enabled": is_on},
        severity="CRITICAL",
    )
    return None


async def _handle_toggle_scribe(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    selected_option = action.get("selected_option", {})
    val = selected_option.get("value", "")
    is_on = val == "scribe:on"
    set_scribe_enabled(is_on)
    log_audit_event(
        event_type="ADMIN_CONFIG_CHANGE",
        actor={"type": "slack_admin", "id": user_id or "unknown"},
        action="toggle_scribe",
        target="discussion_scribe.enabled",
        details={"enabled": is_on},
        severity="CRITICAL",
    )
    return None


async def _handle_toggle_weekly_pricing(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    selected_option = action.get("selected_option", {})
    val = selected_option.get("value", "")
    is_on = val == "pricing_update:on"
    set_weekly_pricing_update_enabled(is_on)
    log_audit_event(
        event_type="ADMIN_CONFIG_CHANGE",
        actor={"type": "slack_admin", "id": user_id or "unknown"},
        action="toggle_weekly_pricing_update",
        target="pricing_service.weekly_update",
        details={"enabled": is_on},
        severity="CRITICAL",
    )
    return None


async def _handle_toggle_convergence_ff(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    selected_option = action.get("selected_option", {})
    val = selected_option.get("value", "")
    is_on = val == "convergence_ff:on"
    set_convergence_fast_forward_enabled(is_on)
    log_audit_event(
        event_type="ADMIN_CONFIG_CHANGE",
        actor={"type": "slack_admin", "id": user_id or "unknown"},
        action="toggle_convergence_fast_forward",
        target="convergence_fast_forward.enabled",
        details={"enabled": is_on},
        severity="CRITICAL",
    )
    return None


async def _handle_set_circuit_breaker_threshold_input(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    val_str = action.get("value", "")
    try:
        val_clean = str(val_str).strip().replace("$", "").replace(",", ".")
        val_float = float(val_clean)
        if val_float > 0:
            set_circuit_breaker_threshold(val_float)
            set_circuit_breaker_enabled(True)
        else:
            set_circuit_breaker_threshold(0.0)
            set_circuit_breaker_enabled(False)
        log_audit_event(
            event_type="ADMIN_CONFIG_CHANGE",
            actor={"type": "slack_admin", "id": user_id or "unknown"},
            action="set_circuit_breaker_threshold",
            target="circuit_breaker.threshold",
            details={"threshold_input": val_clean, "parsed_threshold": val_float},
            severity="CRITICAL",
        )
    except (ValueError, TypeError):
        logger.warning(f"[INTERACTIVITY] Could not parse threshold from input: '{val_str}'")
    return None


async def _handle_save_circuit_breaker_threshold_button(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    val_str = None
    if isinstance(view_state, dict):
        for block_val in view_state.values():
            if isinstance(block_val, dict) and "set_circuit_breaker_threshold_input" in block_val:
                candidate = block_val["set_circuit_breaker_threshold_input"].get("value")
                if candidate is not None and str(candidate).strip():
                    val_str = candidate
                    break
        if val_str is None:
            block_vals = view_state.get("circuit_breaker_threshold_block", {})
            if isinstance(block_vals, dict):
                val_obj = block_vals.get("set_circuit_breaker_threshold_input", {})
                if isinstance(val_obj, dict):
                    candidate = val_obj.get("value")
                    if candidate is not None and str(candidate).strip():
                        val_str = candidate

    val_float = None
    if val_str is not None and str(val_str).strip():
        try:
            val_clean = str(val_str).strip().replace("$", "").replace(",", ".")
            val_float = float(val_clean)
        except (ValueError, TypeError):
            logger.warning(f"[INTERACTIVITY] Could not parse threshold from button save: '{val_str}'")

    if val_float is not None and val_float > 0:
        set_circuit_breaker_threshold(val_float)
        set_circuit_breaker_enabled(True)
    elif val_float == 0.0:
        set_circuit_breaker_threshold(0.0)
        set_circuit_breaker_enabled(False)
    else:
        curr = get_circuit_breaker_threshold()
        if curr > 0:
            set_circuit_breaker_threshold(curr)
            set_circuit_breaker_enabled(True)
        else:
            set_circuit_breaker_threshold(0.0)
            set_circuit_breaker_enabled(False)

    log_audit_event(
        event_type="ADMIN_CONFIG_CHANGE",
        actor={"type": "slack_admin", "id": user_id or "unknown"},
        action="save_circuit_breaker_threshold",
        target="circuit_breaker.threshold",
        details={"saved_threshold": val_float},
        severity="CRITICAL",
    )
    return None


async def _handle_set_circuit_breaker_preset(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    action_id = action.get("action_id", "")
    val_str = action_id.split(":", 1)[1]
    try:
        val_float = float(val_str)
        if val_float > 0:
            set_circuit_breaker_threshold(val_float)
            set_circuit_breaker_enabled(True)
        else:
            set_circuit_breaker_threshold(0.0)
            set_circuit_breaker_enabled(False)
        log_audit_event(
            event_type="ADMIN_CONFIG_CHANGE",
            actor={"type": "slack_admin", "id": user_id or "unknown"},
            action="set_circuit_breaker_preset",
            target="circuit_breaker.threshold",
            details={"preset_value": val_float},
            severity="CRITICAL",
        )
    except (ValueError, TypeError):
        pass
    return None


async def _handle_set_daily_budget_input(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    val_str = action.get("value", "")
    try:
        val_clean = str(val_str).strip().replace("$", "").replace(",", ".")
        val_float = float(val_clean)
        if val_float >= 0:
            set_daily_budget_usd(val_float)
            log_audit_event(
                event_type="ADMIN_CONFIG_CHANGE",
                actor={"type": "slack_admin", "id": user_id or "unknown"},
                action="set_daily_budget_input",
                target="daily_budget.usd",
                details={"budget_input": val_clean, "parsed_budget": val_float},
                severity="CRITICAL",
            )
    except (ValueError, TypeError):
        logger.warning(f"[INTERACTIVITY] Could not parse daily budget from input: '{val_str}'")
    return None


async def _handle_save_daily_budget_button(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    val_str = None
    if isinstance(view_state, dict):
        for block_val in view_state.values():
            if isinstance(block_val, dict) and "set_daily_budget_input" in block_val:
                candidate = block_val["set_daily_budget_input"].get("value")
                if candidate is not None and str(candidate).strip():
                    val_str = candidate
                    break
        if val_str is None:
            for block_id, block_vals in view_state.items():
                if "daily_budget" in block_id and isinstance(block_vals, dict):
                    val_obj = block_vals.get("set_daily_budget_input", {})
                    if isinstance(val_obj, dict):
                        candidate = val_obj.get("value")
                        if candidate is not None and str(candidate).strip():
                            val_str = candidate
                            break

    val_float = None
    if val_str is not None and str(val_str).strip():
        try:
            val_clean = str(val_str).strip().replace("$", "").replace(",", ".")
            val_float = float(val_clean)
        except (ValueError, TypeError):
            logger.warning(f"[INTERACTIVITY] Could not parse daily budget from button save: '{val_str}'")

    if val_float is not None and val_float >= 0:
        set_daily_budget_usd(val_float)
        log_audit_event(
            event_type="ADMIN_CONFIG_CHANGE",
            actor={"type": "slack_admin", "id": user_id or "unknown"},
            action="save_daily_budget",
            target="daily_budget.usd",
            details={"saved_daily_budget": val_float},
            severity="CRITICAL",
        )
    return None


async def _handle_set_daily_budget_preset(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    action_id = action.get("action_id", "")
    val_str = action_id.split(":", 1)[1]
    try:
        val_float = float(val_str)
        if val_float >= 0:
            set_daily_budget_usd(val_float)
            log_audit_event(
                event_type="ADMIN_CONFIG_CHANGE",
                actor={"type": "slack_admin", "id": user_id or "unknown"},
                action="set_daily_budget_preset",
                target="daily_budget.usd",
                details={"preset_value": val_float},
                severity="CRITICAL",
            )
    except (ValueError, TypeError):
        pass
    return None


async def _handle_set_thread_skill_expiry_preset(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    action_id = action.get("action_id", "")
    val_str = action_id.split(":", 1)[1]
    try:
        from orchestration.app_settings import set_thread_skill_expiration_time

        val_float = float(val_str)
        if val_float > 0:
            set_thread_skill_expiration_time(val_float)
            log_audit_event(
                event_type="ADMIN_CONFIG_CHANGE",
                actor={"type": "slack_admin", "id": user_id or "unknown"},
                action="set_thread_skill_expiry_preset",
                target="thread_skill_expiration_time",
                details={"hours": val_float},
                severity="CRITICAL",
            )
    except (ValueError, TypeError):
        pass
    return None


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
            apply_workflow(wf_id)
            log_audit_event(
                event_type="WORKFLOW_PRESET_MUTATION",
                actor={"type": "slack_admin", "id": user_id or "unknown"},
                action="apply_workflow_preset",
                target=f"workflow:{wf_id}",
                details={"workflow_id": wf_id},
                severity="CRITICAL",
            )
    return None


async def _handle_load_workflow_preset_button(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    selected_wf_id = None
    if isinstance(view_state, dict):
        for block_val in view_state.values():
            if isinstance(block_val, dict) and "select_workflow_preset" in block_val:
                sel_opt = block_val["select_workflow_preset"].get("selected_option") or {}
                val = sel_opt.get("value", "")
                if val.startswith("wf:"):
                    selected_wf_id = val.split(":", 1)[1]
                    break
    if not selected_wf_id:
        selected_wf_id = get_active_workflow_id()
    if selected_wf_id and selected_wf_id != "none":
        apply_workflow(selected_wf_id)
        log_audit_event(
            event_type="WORKFLOW_PRESET_MUTATION",
            actor={"type": "slack_admin", "id": user_id or "unknown"},
            action="apply_workflow_preset",
            target=f"workflow:{selected_wf_id}",
            details={"workflow_id": selected_wf_id},
            severity="CRITICAL",
        )
    return None


async def _handle_delete_workflow_preset_button(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    selected_wf_id = None
    if isinstance(view_state, dict):
        for block_val in view_state.values():
            if isinstance(block_val, dict) and "select_workflow_preset" in block_val:
                sel_opt = block_val["select_workflow_preset"].get("selected_option") or {}
                val = sel_opt.get("value", "")
                if val.startswith("wf:"):
                    selected_wf_id = val.split(":", 1)[1]
                    break
    if not selected_wf_id:
        selected_wf_id = get_active_workflow_id()
    if selected_wf_id and selected_wf_id != "none":
        delete_workflow(selected_wf_id)
        if get_active_workflow_id() == selected_wf_id:
            set_active_workflow_id(None)
        log_audit_event(
            event_type="WORKFLOW_PRESET_MUTATION",
            actor={"type": "slack_admin", "id": user_id or "unknown"},
            action="delete_workflow_preset",
            target=f"workflow:{selected_wf_id}",
            details={"workflow_id": selected_wf_id},
            severity="CRITICAL",
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
    if preset_name:
        mode = get_orchestration_mode()
        # Sync any edited sequence from view_state before saving preset
        if mode == OrchestrationMode.FIXED.value and isinstance(view_state, dict):
            for block_val in view_state.values():
                if isinstance(block_val, dict) and "set_fixed_chain_input" in block_val:
                    f_val = block_val["set_fixed_chain_input"].get("value")
                    if f_val is not None:
                        f_chain, invalid_f = _validate_chain_agent_keys(f_val)
                        if invalid_f:
                            logger.warning(f"[INTERACTIVITY] Unknown fixed chain agent keys rejected: {invalid_f}")
                        set_fixed_config(f_chain)
                        break
        elif mode == OrchestrationMode.SANDWICH.value and isinstance(view_state, dict):
            s_val = None
            e_val = None
            for block_val in view_state.values():
                if isinstance(block_val, dict):
                    if "set_start_chain_input" in block_val:
                        s_val = block_val["set_start_chain_input"].get("value")
                    if "set_end_chain_input" in block_val:
                        e_val = block_val["set_end_chain_input"].get("value")
            if s_val is not None or e_val is not None:
                curr_sandwich = get_sandwich_config()
                start_chain = (
                    _validate_chain_agent_keys(s_val)[0]
                    if s_val is not None
                    else curr_sandwich.get("start_chain", [])
                )
                end_chain = (
                    _validate_chain_agent_keys(e_val)[0]
                    if e_val is not None
                    else curr_sandwich.get("end_chain", [])
                )
                set_sandwich_config(start_chain, end_chain)

        if mode == OrchestrationMode.FIXED.value:
            saved = save_workflow(
                name=preset_name,
                mode=mode,
                fixed_config=get_fixed_config(),
            )
        else:
            saved = save_workflow(
                name=preset_name,
                mode=mode,
                sandwich_config=get_sandwich_config(),
            )
        set_active_workflow_id(saved.id)
        log_audit_event(
            event_type="WORKFLOW_PRESET_MUTATION",
            actor={"type": "slack_admin", "id": user_id or "unknown"},
            action="save_workflow_preset",
            target=f"workflow:{saved.id}",
            details={"name": preset_name, "mode": mode, "workflow_id": saved.id},
            severity="CRITICAL",
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
        log_audit_event(
            event_type="ADMIN_CONFIG_CHANGE",
            actor={"type": "slack_admin", "id": user_id or "unknown"},
            action="select_orchestration_mode",
            target="orchestration.mode",
            details={"mode": mode},
            severity="CRITICAL",
        )
    return None


async def _handle_save_sandwich_chains_button(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
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
    persist_active_workflow()
    log_audit_event(
        event_type="ADMIN_CONFIG_CHANGE",
        actor={"type": "slack_admin", "id": user_id or "unknown"},
        action="save_sandwich_chains_button",
        target="orchestration.sandwich_config",
        details={"start_chain": start_valid, "end_chain": end_valid, "invalid_keys": start_invalid + end_invalid},
        severity="CRITICAL",
    )
    return None


async def _handle_clear_sandwich_chains_button(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    set_sandwich_config([], [])
    persist_active_workflow()
    log_audit_event(
        event_type="ADMIN_CONFIG_CHANGE",
        actor={"type": "slack_admin", "id": user_id or "unknown"},
        action="clear_sandwich_chains_button",
        target="orchestration.sandwich_config",
        details={"cleared": True},
        severity="CRITICAL",
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
    log_audit_event(
        event_type="ADMIN_CONFIG_CHANGE",
        actor={"type": "slack_admin", "id": user_id or "unknown"},
        action="set_start_chain_input",
        target="orchestration.sandwich_config.start_chain",
        details={"start_chain": valid, "invalid_keys": invalid},
        severity="CRITICAL",
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
    log_audit_event(
        event_type="ADMIN_CONFIG_CHANGE",
        actor={"type": "slack_admin", "id": user_id or "unknown"},
        action="set_end_chain_input",
        target="orchestration.sandwich_config.end_chain",
        details={"end_chain": valid, "invalid_keys": invalid},
        severity="CRITICAL",
    )
    return None


async def _handle_save_fixed_chain_button(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    fixed_val = ""
    if isinstance(view_state, dict):
        for block_val in view_state.values():
            if isinstance(block_val, dict) and "set_fixed_chain_input" in block_val:
                fixed_val = block_val["set_fixed_chain_input"].get("value") or ""
    valid, invalid = _validate_chain_agent_keys(fixed_val)
    if invalid:
        logger.warning(f"[INTERACTIVITY] Unknown agent keys rejected in fixed chain: {invalid}")
    set_fixed_config(valid)
    persist_active_workflow()
    log_audit_event(
        event_type="ADMIN_CONFIG_CHANGE",
        actor={"type": "slack_admin", "id": user_id or "unknown"},
        action="save_fixed_chain_button",
        target="orchestration.fixed_config",
        details={"fixed_chain": valid, "invalid_keys": invalid},
        severity="CRITICAL",
    )
    return None


async def _handle_clear_fixed_chain_button(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    set_fixed_config([])
    persist_active_workflow()
    log_audit_event(
        event_type="ADMIN_CONFIG_CHANGE",
        actor={"type": "slack_admin", "id": user_id or "unknown"},
        action="clear_fixed_chain_button",
        target="orchestration.fixed_config",
        details={"cleared": True},
        severity="CRITICAL",
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
    log_audit_event(
        event_type="ADMIN_CONFIG_CHANGE",
        actor={"type": "slack_admin", "id": user_id or "unknown"},
        action="set_fixed_chain_input",
        target="orchestration.fixed_config",
        details={"fixed_chain": valid, "invalid_keys": invalid},
        severity="CRITICAL",
    )
    return None


async def _handle_open_edit_workspace_context_modal(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    from orchestration.app_settings import get_workspace_context
    from services.slack_home import build_edit_workspace_context_modal

    trigger_id = payload.get("trigger_id")
    if trigger_id:
        modal_view = build_edit_workspace_context_modal(current_context=get_workspace_context())
        background_tasks.add_task(
            slack_async_client.views_open,
            trigger_id=trigger_id,
            view=modal_view,
        )
    return None
