"""Slack interactive actions for budget governance, circuit breakers, and feature toggles."""

from __future__ import annotations

from collections.abc import Callable
import logging
import math
from typing import Any

from fastapi import BackgroundTasks, Response

from agents.usage_agent import calculate_thread_costs
from config_store.app_settings import (
    get_circuit_breaker_threshold,
    get_daily_budget_usd,
    is_circuit_breaker_enabled,
    set_circuit_breaker_enabled,
    set_circuit_breaker_threshold,
    set_convergence_fast_forward_enabled,
    set_daily_budget_usd,
    set_filter_unauthorized_skill_agents_enabled,
    set_scribe_enabled,
    set_thread_skill_expiration_time,
    set_weekly_pricing_update_enabled,
)
from services.audit_logger import log_audit_event_async
from services.slack import (
    post_ephemeral_to_slack,
    update_slack_message,
)

logger = logging.getLogger("IzvorBackend.SlackInteractivity.Governance")


def _parse_finite_float(raw_value: Any) -> float:
    cleaned = str(raw_value).strip().replace("$", "").replace(",", ".")
    parsed = float(cleaned)
    if not math.isfinite(parsed):
        raise ValueError(f"Non-finite numeric value: {cleaned}")
    return parsed


def _extract_view_state_input(
    view_state: dict[str, Any],
    action_id: str,
    *,
    preferred_block_substring: str | None = None,
) -> Any:
    if not isinstance(view_state, dict):
        return None
    for block_val in view_state.values():
        if isinstance(block_val, dict) and action_id in block_val:
            candidate = block_val[action_id].get("value")
            if candidate is not None and str(candidate).strip():
                return candidate
    if preferred_block_substring:
        for block_id, block_vals in view_state.items():
            if preferred_block_substring in block_id and isinstance(block_vals, dict):
                val_obj = block_vals.get(action_id, {})
                if isinstance(val_obj, dict):
                    candidate = val_obj.get("value")
                    if candidate is not None and str(candidate).strip():
                        return candidate
    return None


async def _handle_radio_toggle(
    action: dict[str, Any],
    user_id: str | None,
    *,
    expected_on_value: str,
    setter: Callable[[bool], Any],
    audit_action: str,
    audit_target: str,
    warn_on_disable: bool = False,
) -> Response | None:
    selected_option = action.get("selected_option", {})
    val = selected_option.get("value", "")
    is_on = val == expected_on_value
    setter(is_on)
    severity = "WARNING" if (warn_on_disable and not is_on) else "INFO"
    await log_audit_event_async(
        event_type="ADMIN_CONFIG_CHANGE",
        actor={"type": "slack_admin", "id": user_id or "unknown"},
        action=audit_action,
        target=audit_target,
        details={"enabled": is_on},
        severity=severity,
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
    thread_ts = payload.get("container", {}).get("thread_ts") or payload.get("message", {}).get("thread_ts") or ""

    if channel_id and thread_ts:
        from orchestration.agent_router import continue_discussion_from_turn_result, orchestrate_discussion
        from orchestration.thread_state import thread_state_manager
        from orchestration.turn_pipeline import TurnPipeline
        from pipeline.spend_governance import spend_governance
        from services.turn_checkpoint_store import turn_checkpoint_store

        session_id = f"{channel_id}:{thread_ts}"
        threshold = get_circuit_breaker_threshold()
        cb_enabled = is_circuit_breaker_enabled()
        daily_budget = get_daily_budget_usd()
        cost_info = calculate_thread_costs(channel_id, thread_ts)
        current_cost = cost_info.get("grand_total_cost_usd", 0.0)

        tripped, _, effective_limit = spend_governance.is_budget_exceeded(
            session_id=session_id,
            threshold_usd=threshold,
            enabled=cb_enabled,
            daily_budget_usd=daily_budget,
            current_cost_usd=current_cost,
        )

        if tripped:
            limit_val = effective_limit if effective_limit is not None else threshold
            background_tasks.add_task(
                post_ephemeral_to_slack,
                channel_id=channel_id,
                user_id=user_id,
                text=(
                    f"The budget limit (${limit_val:.5f}) is still too low compared to "
                    f"current cost (${current_cost:.5f}). Please raise it first."
                ),
                thread_ts=thread_ts,
            )
            return None

        msg = payload.get("message", {})
        if msg and msg.get("ts"):
            background_tasks.add_task(
                update_slack_message,
                channel_id=channel_id,
                ts=msg["ts"],
                text=f"✅ Sequence resumed by <@{user_id}>.",
                blocks=[],
            )
        await log_audit_event_async(
            event_type="SEQUENCE_RESUMED",
            actor={"type": "slack_admin", "id": user_id or "unknown"},
            action="resume_sequence_action",
            target=f"{channel_id}:{thread_ts}",
            details={"channel_id": channel_id, "thread_ts": thread_ts, "resumed_by": user_id},
            severity="INFO",
        )

        chk = turn_checkpoint_store.get_latest_checkpoint_for_thread(channel_id, thread_ts)
        if chk and chk.pause_reason == "circuit_breaker":
            resume_prompt = (
                chk.prompt
                or thread_state_manager.get_active_prompt(channel_id, thread_ts)
                or thread_state_manager.get_initial_prompt(channel_id, thread_ts)
                or ""
            )
            if chk.messages:

                async def _run_stage4_cb_resume() -> None:
                    pipeline = TurnPipeline()
                    turn_res = await pipeline.resume_from_checkpoint(
                        checkpoint=chk,
                        tool_result=None,
                        user_id=user_id,
                    )
                    if not isinstance(turn_res, dict) or turn_res.get("reason") == "checkpoint_already_claimed":
                        return
                    await continue_discussion_from_turn_result(
                        channel_id=channel_id,
                        thread_ts=thread_ts,
                        turn_res=turn_res,
                        user_id=user_id,
                        accumulated_history=chk.accumulated_history,
                    )

                thread_state_manager.dispatch_discussion_task(channel_id, thread_ts, _run_stage4_cb_resume())
                return None

            if not turn_checkpoint_store.claim_for_resume(chk.checkpoint_id):
                return None
            turn_checkpoint_store.mark_resumed(chk.checkpoint_id)
            target_agent = chk.agent_key
            remaining = thread_state_manager.get_remaining_agents(channel_id, thread_ts)
            if target_agent and target_agent not in remaining:
                thread_state_manager.set_remaining_agents(channel_id, thread_ts, [target_agent] + remaining)
        else:
            resume_prompt = (
                thread_state_manager.get_active_prompt(channel_id, thread_ts)
                or thread_state_manager.get_initial_prompt(channel_id, thread_ts)
                or ""
            )
            remaining = thread_state_manager.get_remaining_agents(channel_id, thread_ts)
            target_agent = remaining[0] if remaining else None

        task_coro = orchestrate_discussion(
            channel_id=channel_id,
            thread_ts=thread_ts,
            raw_text=resume_prompt,
            user_id=user_id,
            bot_user_id=None,
            target_agent_override=target_agent,
            is_bot_trigger=True,
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
    return await _handle_radio_toggle(
        action,
        user_id,
        expected_on_value="cb:on",
        setter=set_circuit_breaker_enabled,
        audit_action="toggle_circuit_breaker",
        audit_target="circuit_breaker.enabled",
        warn_on_disable=True,
    )


async def _handle_toggle_scribe(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    return await _handle_radio_toggle(
        action,
        user_id,
        expected_on_value="scribe:on",
        setter=set_scribe_enabled,
        audit_action="toggle_scribe",
        audit_target="discussion_scribe.enabled",
    )


async def _handle_toggle_weekly_pricing(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    return await _handle_radio_toggle(
        action,
        user_id,
        expected_on_value="pricing_update:on",
        setter=set_weekly_pricing_update_enabled,
        audit_action="toggle_weekly_pricing_update",
        audit_target="pricing_service.weekly_update",
    )


async def _handle_toggle_convergence_ff(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    return await _handle_radio_toggle(
        action,
        user_id,
        expected_on_value="convergence_ff:on",
        setter=set_convergence_fast_forward_enabled,
        audit_action="toggle_convergence_fast_forward",
        audit_target="convergence_fast_forward.enabled",
    )


async def _handle_toggle_skill_filter(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    return await _handle_radio_toggle(
        action,
        user_id,
        expected_on_value="skill_filter:on",
        setter=set_filter_unauthorized_skill_agents_enabled,
        audit_action="toggle_skill_filter",
        audit_target="skill_routing.filter_unauthorized_agents_enabled",
        warn_on_disable=True,
    )


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
        val_float = _parse_finite_float(val_clean)
        if val_float > 0:
            set_circuit_breaker_threshold(val_float)
            set_circuit_breaker_enabled(True)
        else:
            set_circuit_breaker_threshold(0.0)
            set_circuit_breaker_enabled(False)
        await log_audit_event_async(
            event_type="ADMIN_CONFIG_CHANGE",
            actor={"type": "slack_admin", "id": user_id or "unknown"},
            action="set_circuit_breaker_threshold",
            target="circuit_breaker.threshold",
            details={"threshold_input": val_clean, "parsed_threshold": val_float},
            severity="INFO" if val_float > 0 else "WARNING",
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
    val_str = _extract_view_state_input(
        view_state,
        "set_circuit_breaker_threshold_input",
        preferred_block_substring="circuit_breaker_threshold",
    )

    val_float = None
    if val_str is not None and str(val_str).strip():
        try:
            val_float = _parse_finite_float(val_str)
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

    effective_thresh = val_float if val_float is not None else get_circuit_breaker_threshold()
    await log_audit_event_async(
        event_type="ADMIN_CONFIG_CHANGE",
        actor={"type": "slack_admin", "id": user_id or "unknown"},
        action="save_circuit_breaker_threshold",
        target="circuit_breaker.threshold",
        details={"saved_threshold": val_float},
        severity="INFO" if (effective_thresh and effective_thresh > 0) else "WARNING",
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
    val_str = action_id.split(":", 1)[1] if ":" in action_id else ""
    try:
        val_float = _parse_finite_float(val_str)
        if val_float > 0:
            set_circuit_breaker_threshold(val_float)
            set_circuit_breaker_enabled(True)
        else:
            set_circuit_breaker_threshold(0.0)
            set_circuit_breaker_enabled(False)
        await log_audit_event_async(
            event_type="ADMIN_CONFIG_CHANGE",
            actor={"type": "slack_admin", "id": user_id or "unknown"},
            action="set_circuit_breaker_preset",
            target="circuit_breaker.threshold",
            details={"preset_value": val_float},
            severity="INFO" if val_float > 0 else "WARNING",
        )
    except (ValueError, TypeError):
        logger.warning(f"[INTERACTIVITY] Invalid circuit breaker preset value: '{val_str}'")
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
        val_float = _parse_finite_float(val_clean)
        if val_float >= 0:
            set_daily_budget_usd(val_float)
            await log_audit_event_async(
                event_type="ADMIN_CONFIG_CHANGE",
                actor={"type": "slack_admin", "id": user_id or "unknown"},
                action="set_daily_budget_input",
                target="daily_budget.usd",
                details={"budget_input": val_clean, "parsed_budget": val_float},
                severity="INFO",
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
    val_str = _extract_view_state_input(
        view_state,
        "set_daily_budget_input",
        preferred_block_substring="daily_budget",
    )

    val_float = None
    if val_str is not None and str(val_str).strip():
        try:
            val_float = _parse_finite_float(val_str)
        except (ValueError, TypeError):
            logger.warning(f"[INTERACTIVITY] Could not parse daily budget from button save: '{val_str}'")

    if val_float is not None and val_float >= 0:
        set_daily_budget_usd(val_float)
        await log_audit_event_async(
            event_type="ADMIN_CONFIG_CHANGE",
            actor={"type": "slack_admin", "id": user_id or "unknown"},
            action="save_daily_budget",
            target="daily_budget.usd",
            details={"saved_daily_budget": val_float},
            severity="INFO",
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
    val_str = action_id.split(":", 1)[1] if ":" in action_id else ""
    try:
        val_float = _parse_finite_float(val_str)
        if val_float >= 0:
            set_daily_budget_usd(val_float)
            await log_audit_event_async(
                event_type="ADMIN_CONFIG_CHANGE",
                actor={"type": "slack_admin", "id": user_id or "unknown"},
                action="set_daily_budget_preset",
                target="daily_budget.usd",
                details={"preset_value": val_float},
                severity="INFO",
            )
    except (ValueError, TypeError):
        logger.warning(f"[INTERACTIVITY] Invalid daily budget preset value: '{val_str}'")
    return None


async def _handle_set_thread_skill_expiry_preset(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    action_id = action.get("action_id", "")
    val_str = action_id.split(":", 1)[1] if ":" in action_id else ""
    try:
        val_float = _parse_finite_float(val_str)
        if val_float > 0:
            set_thread_skill_expiration_time(val_float)
            await log_audit_event_async(
                event_type="ADMIN_CONFIG_CHANGE",
                actor={"type": "slack_admin", "id": user_id or "unknown"},
                action="set_thread_skill_expiry_preset",
                target="thread_skill_expiration_time",
                details={"hours": val_float},
                severity="INFO",
            )
    except (ValueError, TypeError):
        logger.warning(f"[INTERACTIVITY] Invalid thread skill expiry preset value: '{val_str}'")
    return None

