import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import json
import logging
import urllib.parse
from typing import Any

from fastapi import BackgroundTasks, HTTPException, Request, Response
from slack_sdk.errors import SlackApiError
from starlette.requests import ClientDisconnect

from adapters.slack.actions import (
    _deny_non_admin_action,
    _execute_and_resume_hitl_approval,
    _handle_bulk_disable,
    _handle_bulk_enable_all,
    _handle_clear_fixed_chain_button,
    _handle_clear_sandwich_chains_button,
    _handle_delete_workflow_preset_button,
    _handle_download_scribe_notes,
    _handle_hitl_approve_tool,
    _handle_hitl_deny_tool,
    _handle_load_workflow_preset_button,
    _handle_open_edit_workspace_context_modal,
    _handle_restore_from_backup,
    _handle_resume_sequence,
    _handle_save_circuit_breaker_threshold_button,
    _handle_save_daily_budget_button,
    _handle_save_fixed_chain_button,
    _handle_save_sandwich_chains_button,
    _handle_save_workflow_preset_button,
    _handle_scribe_download,
    _handle_select_agent_model,
    _handle_select_orchestration_mode,
    _handle_select_workflow_preset,
    _handle_set_circuit_breaker_preset,
    _handle_set_circuit_breaker_threshold_input,
    _handle_set_daily_budget_input,
    _handle_set_daily_budget_preset,
    _handle_set_end_chain_input,
    _handle_set_fixed_chain_input,
    _handle_set_start_chain_input,
    _handle_set_thread_skill_expiry_preset,
    _handle_toggle_agent,
    _handle_toggle_agent_radio,
    _handle_toggle_circuit_breaker,
    _handle_toggle_convergence_ff,
    _handle_toggle_scribe,
    _handle_toggle_weekly_pricing,
    _resume_hitl_denial,
    _validate_chain_agent_keys,
)
from core.config import is_slack_admin
from core.slack_security import verify_slack_signature
from orchestration.agent_settings import sync_settings_if_modified
from services.audit_logger import log_audit_event
from services.slack import (
    open_dm_channel,
    post_ephemeral_to_slack,
    publish_app_home,
    slack_async_client,
    upload_text_file_to_slack,
)

logger = logging.getLogger("IzvorBackend.SlackInteractivity")


@dataclass(frozen=True)
class ActionSpec:
    handler: Callable[
        [dict[str, Any], dict[str, Any], str | None, BackgroundTasks, dict[str, Any]],
        Awaitable[Response | None] | Response | None,
    ]
    admin_required: bool = True
    audit_action: str | None = "ADMIN_CONFIG_CHANGE"
    publish_home: bool = True


async def _safe_publish_home(user_id: str) -> None:
    try:
        sync_settings_if_modified()
        await publish_app_home(user_id=user_id)
    except SlackApiError as e:
        logger.error(f"[INTERACTIVITY] SlackApiError publishing App Home for {user_id}: {e}")
    except Exception as e:  # noqa: BLE001 - Interactivity handler isolates app home publishing failure
        logger.error(f"[INTERACTIVITY] Failed to publish App Home for {user_id}: {e}")


ACTION_REGISTRY: dict[str, ActionSpec] = {
    "download_scribe_notes_txt": ActionSpec(
        handler=_handle_download_scribe_notes, admin_required=False, audit_action=None, publish_home=False
    ),
    "download_conclusion_notes_txt": ActionSpec(
        handler=_handle_download_scribe_notes, admin_required=False, audit_action=None, publish_home=False
    ),
    "hitl_approve_tool": ActionSpec(
        handler=_handle_hitl_approve_tool, admin_required=False, audit_action=None, publish_home=False
    ),
    "hitl_deny_tool": ActionSpec(
        handler=_handle_hitl_deny_tool, admin_required=False, audit_action=None, publish_home=False
    ),
    "resume_sequence_action": ActionSpec(
        handler=_handle_resume_sequence, admin_required=True, audit_action="ADMIN_CONFIG_CHANGE", publish_home=False
    ),
    "toggle_circuit_breaker_radio": ActionSpec(
        handler=_handle_toggle_circuit_breaker,
        admin_required=True,
        audit_action="ADMIN_CONFIG_CHANGE",
        publish_home=True,
    ),
    "toggle_scribe_radio": ActionSpec(
        handler=_handle_toggle_scribe, admin_required=True, audit_action="ADMIN_CONFIG_CHANGE", publish_home=True
    ),
    "toggle_weekly_pricing_update_radio": ActionSpec(
        handler=_handle_toggle_weekly_pricing,
        admin_required=True,
        audit_action="ADMIN_CONFIG_CHANGE",
        publish_home=True,
    ),
    "toggle_convergence_fast_forward_radio": ActionSpec(
        handler=_handle_toggle_convergence_ff,
        admin_required=True,
        audit_action="ADMIN_CONFIG_CHANGE",
        publish_home=True,
    ),
    "set_circuit_breaker_threshold_input": ActionSpec(
        handler=_handle_set_circuit_breaker_threshold_input,
        admin_required=True,
        audit_action="ADMIN_CONFIG_CHANGE",
        publish_home=True,
    ),
    "save_circuit_breaker_threshold_button": ActionSpec(
        handler=_handle_save_circuit_breaker_threshold_button,
        admin_required=True,
        audit_action="ADMIN_CONFIG_CHANGE",
        publish_home=True,
    ),
    "set_daily_budget_input": ActionSpec(
        handler=_handle_set_daily_budget_input,
        admin_required=True,
        audit_action="ADMIN_CONFIG_CHANGE",
        publish_home=True,
    ),
    "save_daily_budget_button": ActionSpec(
        handler=_handle_save_daily_budget_button,
        admin_required=True,
        audit_action="ADMIN_CONFIG_CHANGE",
        publish_home=True,
    ),
    "select_workflow_preset": ActionSpec(
        handler=_handle_select_workflow_preset,
        admin_required=True,
        audit_action="WORKFLOW_PRESET_SELECTED",
        publish_home=True,
    ),
    "load_workflow_preset_button": ActionSpec(
        handler=_handle_load_workflow_preset_button,
        admin_required=True,
        audit_action="WORKFLOW_PRESET_SELECTED",
        publish_home=True,
    ),
    "delete_workflow_preset_button": ActionSpec(
        handler=_handle_delete_workflow_preset_button,
        admin_required=True,
        audit_action="ADMIN_CONFIG_CHANGE",
        publish_home=True,
    ),
    "save_workflow_preset_button": ActionSpec(
        handler=_handle_save_workflow_preset_button,
        admin_required=True,
        audit_action="ADMIN_CONFIG_CHANGE",
        publish_home=True,
    ),
    "select_orchestration_mode": ActionSpec(
        handler=_handle_select_orchestration_mode,
        admin_required=True,
        audit_action="ADMIN_CONFIG_CHANGE",
        publish_home=True,
    ),
    "save_sandwich_chains_button": ActionSpec(
        handler=_handle_save_sandwich_chains_button,
        admin_required=True,
        audit_action="ADMIN_CONFIG_CHANGE",
        publish_home=True,
    ),
    "clear_sandwich_chains_button": ActionSpec(
        handler=_handle_clear_sandwich_chains_button,
        admin_required=True,
        audit_action="ADMIN_CONFIG_CHANGE",
        publish_home=True,
    ),
    "set_start_chain_input": ActionSpec(
        handler=_handle_set_start_chain_input,
        admin_required=True,
        audit_action="ADMIN_CONFIG_CHANGE",
        publish_home=True,
    ),
    "set_end_chain_input": ActionSpec(
        handler=_handle_set_end_chain_input, admin_required=True, audit_action="ADMIN_CONFIG_CHANGE", publish_home=True
    ),
    "save_fixed_chain_button": ActionSpec(
        handler=_handle_save_fixed_chain_button,
        admin_required=True,
        audit_action="ADMIN_CONFIG_CHANGE",
        publish_home=True,
    ),
    "clear_fixed_chain_button": ActionSpec(
        handler=_handle_clear_fixed_chain_button,
        admin_required=True,
        audit_action="ADMIN_CONFIG_CHANGE",
        publish_home=True,
    ),
    "set_fixed_chain_input": ActionSpec(
        handler=_handle_set_fixed_chain_input,
        admin_required=True,
        audit_action="ADMIN_CONFIG_CHANGE",
        publish_home=True,
    ),
    "bulk_enable_all": ActionSpec(
        handler=_handle_bulk_enable_all, admin_required=True, audit_action="ADMIN_CONFIG_CHANGE", publish_home=True
    ),
    "bulk_disable_all": ActionSpec(
        handler=_handle_bulk_disable, admin_required=True, audit_action="ADMIN_CONFIG_CHANGE", publish_home=True
    ),
    "bulk_disable_optional": ActionSpec(
        handler=_handle_bulk_disable, admin_required=True, audit_action="ADMIN_CONFIG_CHANGE", publish_home=True
    ),
    "restore_from_backup": ActionSpec(
        handler=_handle_restore_from_backup, admin_required=True, audit_action="ADMIN_CONFIG_CHANGE", publish_home=True
    ),
    "open_edit_workspace_context_modal": ActionSpec(
        handler=_handle_open_edit_workspace_context_modal,
        admin_required=True,
        audit_action="ADMIN_CONFIG_CHANGE",
        publish_home=False,
    ),
}

PREFIX_ACTION_REGISTRY: list[tuple[str, ActionSpec]] = [
    (
        "set_circuit_breaker_preset:",
        ActionSpec(
            handler=_handle_set_circuit_breaker_preset,
            admin_required=True,
            audit_action="ADMIN_CONFIG_CHANGE",
            publish_home=True,
        ),
    ),
    (
        "set_daily_budget_preset:",
        ActionSpec(
            handler=_handle_set_daily_budget_preset,
            admin_required=True,
            audit_action="ADMIN_CONFIG_CHANGE",
            publish_home=True,
        ),
    ),
    (
        "set_thread_skill_expiry_preset:",
        ActionSpec(
            handler=_handle_set_thread_skill_expiry_preset,
            admin_required=True,
            audit_action="ADMIN_CONFIG_CHANGE",
            publish_home=True,
        ),
    ),
    (
        "select_agent_model:",
        ActionSpec(
            handler=_handle_select_agent_model,
            admin_required=True,
            audit_action="ADMIN_CONFIG_CHANGE",
            publish_home=True,
        ),
    ),
    (
        "toggle_agent_radio:",
        ActionSpec(
            handler=_handle_toggle_agent_radio,
            admin_required=True,
            audit_action="ADMIN_CONFIG_CHANGE",
            publish_home=True,
        ),
    ),
    (
        "toggle_agent:",
        ActionSpec(
            handler=_handle_toggle_agent,
            admin_required=True,
            audit_action="ADMIN_CONFIG_CHANGE",
            publish_home=True,
        ),
    ),
]


def _resolve_action_spec(action_id: str) -> ActionSpec:
    if action_id in ACTION_REGISTRY:
        return ACTION_REGISTRY[action_id]
    for prefix, spec in PREFIX_ACTION_REGISTRY:
        if action_id.startswith(prefix):
            return spec

    async def _unknown_action(
        action: dict[str, Any],
        payload: dict[str, Any],
        user_id: str | None,
        background_tasks: BackgroundTasks,
        view_state: dict[str, Any],
    ) -> Response | None:
        logger.warning(f"[INTERACTIVITY] Unrecognized block action_id: '{action.get('action_id')}'")
        return None

    return ActionSpec(
        handler=_unknown_action,
        admin_required=True,
        audit_action=None,
        publish_home=False,
    )


async def handle_slack_interactivity(request: Request, background_tasks: BackgroundTasks) -> Any:
    """
    Handles Slack Block Kit interactivity (radio button toggles, buttons, etc.) from the App Home page.
    """
    headers = request.headers
    try:
        body_bytes = await request.body()
    except ClientDisconnect:
        logger.info("Slack client disconnected before interactivity body could be read.")
        return Response(status_code=200)

    if not verify_slack_signature(body_bytes, headers):
        logger.warning("Rejected unauthorized request to /slack/actions")
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    payload = None
    body_str = body_bytes.decode("utf-8", errors="replace")

    # 1. Parse URL-encoded Slack payload parameter (Standard Slack interactivity webhook format)
    if "payload=" in body_str:
        try:
            parsed_form = urllib.parse.parse_qs(body_str)
            if parsed_form.get("payload"):
                payload = json.loads(parsed_form["payload"][0])
        except (json.JSONDecodeError, ValueError, KeyError, IndexError, TypeError) as e:
            logger.debug(f"[INTERACTIVITY] parse_qs extraction error: {e}")

    # 2. Try parsing request.form()
    if not payload:
        try:
            form_data = await request.form()
            if "payload" in form_data:
                payload = json.loads(form_data["payload"])
        except (json.JSONDecodeError, ValueError, KeyError, UnicodeDecodeError, AttributeError, TypeError) as e:
            logger.debug(f"[INTERACTIVITY] form-data extraction failed: {e}")

    # 3. Direct JSON payload fallback (used in unit tests / direct API callers)
    if not payload:
        try:
            payload = json.loads(body_str)
        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"Failed to parse interactivity payload: {e}")
            raise HTTPException(status_code=400, detail="Invalid payload") from e
        except Exception as e:
            logger.error(f"Failed to parse interactivity payload: {e}")
            raise HTTPException(status_code=400, detail="Invalid payload") from e

    payload_type = payload.get("type")
    if payload_type == "block_actions":
        user_id = None
        if isinstance(payload.get("user"), dict):
            user_id = payload.get("user", {}).get("id")
        elif isinstance(payload.get("user"), str):
            user_id = payload.get("user")
        if not user_id and isinstance(payload.get("view"), dict):
            user_id = payload.get("view", {}).get("user", {}).get("id")

        actions = payload.get("actions", [])
        view_state = payload.get("view", {}).get("state", {}).get("values", {})

        # Whole-payload pre-authorization pass across ALL actions before executing any
        resolved_actions: list[tuple[dict[str, Any], ActionSpec]] = []
        for action in actions:
            action_id = action.get("action_id", "")
            spec = _resolve_action_spec(action_id)
            if spec.admin_required and not is_slack_admin(user_id):
                _deny_non_admin_action(payload, user_id, action_id, background_tasks)
                trigger_id = payload.get("trigger_id")
                if trigger_id:
                    return Response(status_code=200)
                return Response(status_code=403)
            resolved_actions.append((action, spec))

        should_publish_home = False
        for action, spec in resolved_actions:
            if spec.publish_home:
                should_publish_home = True
            try:
                res = spec.handler(action, payload, user_id, background_tasks, view_state)
                if asyncio.iscoroutine(res):
                    res = await res
                if isinstance(res, Response) and res.status_code != 200:
                    if spec.audit_action:
                        log_audit_event(
                            event_type=spec.audit_action,
                            actor={"type": "slack_user", "id": user_id or "unknown"},
                            action=action.get("action_id", "unknown_action"),
                            target="slack_app_home",
                            details={"action": action.get("action_id"), "status_code": res.status_code},
                            severity="WARNING",
                            status="FAILURE",
                        )
                    return res
                if spec.audit_action:
                    log_audit_event(
                        event_type=spec.audit_action,
                        actor={"type": "slack_user", "id": user_id or "unknown"},
                        action=action.get("action_id", "unknown_action"),
                        target="slack_app_home",
                        details={"action": action.get("action_id")},
                        severity="INFO",
                        status="SUCCESS",
                    )
            except Exception as e:
                if spec.audit_action:
                    log_audit_event(
                        event_type=spec.audit_action,
                        actor={"type": "slack_user", "id": user_id or "unknown"},
                        action=action.get("action_id", "unknown_action"),
                        target="slack_app_home",
                        details={"action": action.get("action_id"), "error": str(e)},
                        severity="ERROR",
                        status="FAILURE",
                    )
                raise

        if user_id and should_publish_home:
            background_tasks.add_task(_safe_publish_home, user_id=user_id)

    elif payload_type == "view_submission":
        view = payload.get("view", {})
        callback_id = view.get("callback_id")
        if callback_id == "submit_workspace_context_modal":
            user_id = None
            if isinstance(payload.get("user"), dict):
                user_id = payload.get("user", {}).get("id")
            elif isinstance(payload.get("user"), str):
                user_id = payload.get("user")
            if not user_id and isinstance(payload.get("view"), dict):
                user_id = payload.get("view", {}).get("user", {}).get("id")

            from orchestration.app_settings import set_workspace_context
            from starlette.responses import JSONResponse

            if not is_slack_admin(user_id):
                logger.warning(f"[INTERACTIVITY] Non-admin user {user_id} attempted to submit workspace context.")
                log_audit_event(
                    event_type="ADMIN_ACTION_DENIED",
                    actor={"type": "slack_user", "id": user_id or "unknown"},
                    action="submit_workspace_context_modal",
                    target="workspace_context",
                    details={"user_id": user_id},
                    severity="CRITICAL",
                )
                return Response(status_code=403)

            values = view.get("state", {}).get("values", {})
            input_val = (
                values.get("workspace_context_input_block", {}).get("workspace_context_input", {}).get("value")
            ) or ""
            clean_val = str(input_val).strip()

            if len(clean_val) > 1000:
                return JSONResponse(
                    content={
                        "response_action": "errors",
                        "errors": {
                            "workspace_context_input_block": (
                                f"Workspace context cannot exceed 1,000 characters (currently {len(clean_val)})."
                            )
                        },
                    }
                )

            set_workspace_context(clean_val)
            logger.info(f"[INTERACTIVITY] Workspace context updated by admin {user_id}: {len(clean_val)} chars.")
            log_audit_event(
                event_type="WORKSPACE_CONTEXT_MUTATION",
                actor={"type": "slack_admin", "id": user_id or "unknown"},
                action="update_workspace_context",
                target="app_settings",
                details={"length": len(clean_val)},
                severity="INFO",
            )
            if user_id:
                background_tasks.add_task(_safe_publish_home, user_id=user_id)
            return Response(status_code=200)

    return Response(status_code=200)


__all__ = [
    "ActionSpec",
    "ACTION_REGISTRY",
    "PREFIX_ACTION_REGISTRY",
    "_resolve_action_spec",
    "_safe_publish_home",
    "handle_slack_interactivity",
    "slack_async_client",
    "_deny_non_admin_action",
    "_execute_and_resume_hitl_approval",
    "_handle_bulk_disable",
    "_handle_bulk_enable_all",
    "_handle_clear_fixed_chain_button",
    "_handle_clear_sandwich_chains_button",
    "_handle_delete_workflow_preset_button",
    "_handle_download_scribe_notes",
    "_handle_hitl_approve_tool",
    "_handle_hitl_deny_tool",
    "_handle_load_workflow_preset_button",
    "_handle_open_edit_workspace_context_modal",
    "_handle_restore_from_backup",
    "_handle_resume_sequence",
    "_handle_save_circuit_breaker_threshold_button",
    "_handle_save_daily_budget_button",
    "_handle_save_fixed_chain_button",
    "_handle_save_sandwich_chains_button",
    "_handle_save_workflow_preset_button",
    "_handle_scribe_download",
    "_handle_select_agent_model",
    "_handle_select_orchestration_mode",
    "_handle_select_workflow_preset",
    "_handle_set_circuit_breaker_preset",
    "_handle_set_circuit_breaker_threshold_input",
    "_handle_set_daily_budget_input",
    "_handle_set_daily_budget_preset",
    "_handle_set_end_chain_input",
    "_handle_set_fixed_chain_input",
    "_handle_set_start_chain_input",
    "_handle_set_thread_skill_expiry_preset",
    "_handle_toggle_agent",
    "_handle_toggle_agent_radio",
    "_handle_toggle_circuit_breaker",
    "_handle_toggle_convergence_ff",
    "_handle_toggle_scribe",
    "_handle_toggle_weekly_pricing",
    "_resume_hitl_denial",
    "_validate_chain_agent_keys",
    "is_slack_admin",
    "log_audit_event",
    "open_dm_channel",
    "post_ephemeral_to_slack",
    "publish_app_home",
    "upload_text_file_to_slack",
    "verify_slack_signature",
]
