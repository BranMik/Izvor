import json
import logging
import sys
import urllib.parse
from typing import Any

from fastapi import BackgroundTasks, HTTPException, Request, Response
from slack_sdk.errors import SlackApiError
from starlette.requests import ClientDisconnect

from agents.thread_notes_agent import (
    get_thread_conclusion_content,
    get_thread_note_content,
)
from core.config import AGENTS_CONFIG, is_slack_admin
from core.models import OrchestrationMode
from core.slack_security import verify_slack_signature
from orchestration.agent_settings import (
    apply_workflow,
    get_active_workflow_id,
    get_circuit_breaker_threshold,
    get_fixed_config,
    get_orchestration_mode,
    get_sandwich_config,
    persist_active_workflow,
    reset_enabled_agents,
    restore_from_backup,
    set_active_workflow_id,
    set_agent_enabled,
    set_agent_model,
    set_circuit_breaker_enabled,
    set_circuit_breaker_threshold,
    set_fixed_config,
    set_orchestration_mode,
    set_sandwich_config,
    set_scribe_enabled,
    set_weekly_pricing_update_enabled,
    sync_settings_if_modified,
    toggle_agent,
)
from orchestration.workflow_manager import (
    delete_workflow,
    save_workflow,
)
from services.slack_client import (
    open_dm_channel,
    post_ephemeral_to_slack,
    publish_app_home,
    upload_text_file_to_slack,
    slack_async_client,
)
from services.audit_logger import log_audit_event

logger = logging.getLogger("CaiLogicsBackend.SlackInteractivity")


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
        # 1. Ephemeral confirmation in thread
        if channel_id and thread_ts:
            await post_ephemeral_to_slack(
                channel_id=channel_id,
                user_id=user_id,
                text="✅ Prepared notes have been sent as a `.txt` file to your direct message (DM).",
                thread_ts=thread_ts,
            )

        # 2. Open DM channel with the requesting user
        dm_channel_id = await open_dm_channel(user_id) or user_id

        # 3. Upload .txt file to user DM
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


async def _safe_publish_home(user_id: str) -> None:
    try:
        sync_settings_if_modified()
        await publish_app_home(user_id=user_id)
    except SlackApiError as e:
        logger.error(f"[INTERACTIVITY] SlackApiError publishing App Home for {user_id}: {e}")
    except Exception as e:  # noqa: BLE001 - Interactivity handler isolates app home publishing failure
        logger.error(f"[INTERACTIVITY] Failed to publish App Home for {user_id}: {e}")


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
        should_publish_home = True

        for action in actions:
            action_id = action.get("action_id", "")
            
            # Non-admin actions that any user can trigger
            if action_id not in (
                "download_scribe_notes_txt",
                "download_conclusion_notes_txt",
                "resume_sequence_action",
                "hitl_approve_tool",
                "hitl_deny_tool",
            ):
                if not is_slack_admin(user_id):
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
                    channel_id = payload.get("channel", {}).get("id") or ""
                    if channel_id and user_id:
                        background_tasks.add_task(
                            slack_async_client.chat_postEphemeral,
                            channel=channel_id,
                            user=user_id,
                            text="⚠️ *Permission Denied:* Administrative privileges are required to modify system settings.",
                        )
                    return Response(status_code=403)

            if action_id in ("download_scribe_notes_txt", "download_conclusion_notes_txt"):
                should_publish_home = False
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
            elif action_id == "resume_sequence_action":
                should_publish_home = False
                channel_id = payload.get("channel", {}).get("id") or ""
                thread_ts = (
                    payload.get("container", {}).get("thread_ts")
                    or payload.get("message", {}).get("thread_ts")
                    or ""
                )

                if channel_id and thread_ts:
                    from pipeline.spend_governance import spend_governance
                    from orchestration.agent_settings import get_circuit_breaker_threshold
                    import sys
                    
                    session_id = f"{channel_id}:{thread_ts}"
                    threshold = get_circuit_breaker_threshold()
                    calc_costs_fn = getattr(sys.modules.get("agents"), "calculate_thread_costs", None)
                    current_cost = 0.0
                    if calc_costs_fn:
                        cost_info = calc_costs_fn(channel_id, thread_ts)
                        current_cost = cost_info.get("grand_total_cost_usd", 0.0)

                    tripped, _, _ = spend_governance.is_budget_exceeded(
                        session_id=session_id,
                        threshold_usd=threshold,
                        enabled=True,
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
                                slack_async_client.chat_update,
                                channel=channel_id,
                                ts=msg["ts"],
                                text=f"✅ Sequence resumed by <@{user_id}>.",
                                blocks=[]
                            )
                        from orchestration.turn_pipeline import TurnContext, TurnPipeline
                        from orchestration.thread_state import thread_state_manager
                        
                        ctx = TurnContext(
                            channel_id=channel_id,
                            thread_ts=thread_ts,
                            raw_text="",
                            is_bot_trigger=True,
                            was_explicit_target=False,
                        )
                        pipeline = TurnPipeline()
                        thread_state_manager.dispatch_discussion_task(channel_id, thread_ts, pipeline.run(ctx))
            elif action_id == "toggle_circuit_breaker_radio":
                selected_option = action.get("selected_option", {})
                val = selected_option.get("value", "")
                is_on = (val == "cb:on")
                set_circuit_breaker_enabled(is_on)
                if not is_on:
                    set_circuit_breaker_threshold(0.0)
                log_audit_event(
                    event_type="ADMIN_CONFIG_CHANGE",
                    actor={"type": "slack_admin", "id": user_id or "unknown"},
                    action="toggle_circuit_breaker",
                    target="circuit_breaker.enabled",
                    details={"enabled": is_on},
                    severity="CRITICAL",
                )
            elif action_id == "toggle_scribe_radio":
                selected_option = action.get("selected_option", {})
                val = selected_option.get("value", "")
                is_on = (val == "scribe:on")
                set_scribe_enabled(is_on)
                log_audit_event(
                    event_type="ADMIN_CONFIG_CHANGE",
                    actor={"type": "slack_admin", "id": user_id or "unknown"},
                    action="toggle_scribe",
                    target="discussion_scribe.enabled",
                    details={"enabled": is_on},
                    severity="CRITICAL",
                )
            elif action_id == "toggle_weekly_pricing_update_radio":
                selected_option = action.get("selected_option", {})
                val = selected_option.get("value", "")
                is_on = (val == "pricing_update:on")
                set_weekly_pricing_update_enabled(is_on)
                log_audit_event(
                    event_type="ADMIN_CONFIG_CHANGE",
                    actor={"type": "slack_admin", "id": user_id or "unknown"},
                    action="toggle_weekly_pricing_update",
                    target="pricing_service.weekly_update",
                    details={"enabled": is_on},
                    severity="CRITICAL",
                )
            elif action_id == "set_circuit_breaker_threshold_input":
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
                    logger.warning(
                        f"[INTERACTIVITY] Could not parse threshold from input: '{val_str}'"
                    )
            elif action_id == "save_circuit_breaker_threshold_button":
                val_str = None
                # Search dynamically for the action_id inside any block in view_state
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
                        logger.warning(
                            f"[INTERACTIVITY] Could not parse threshold from button save: '{val_str}'"
                        )
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
            elif action_id.startswith("set_circuit_breaker_preset:"):
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

            elif action_id.startswith("set_thread_skill_expiry_preset:"):
                val_str = action_id.split(":", 1)[1]
                try:
                    from orchestration.app_settings import (
                        set_thread_skill_expiration_time,
                    )
                    val_float = float(val_str)
                    if val_float > 0:
                        set_thread_skill_expiration_time(val_float)
                except (ValueError, TypeError):
                    pass
            elif action_id == "select_workflow_preset":
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
            elif action_id == "load_workflow_preset_button":
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
            elif action_id == "delete_workflow_preset_button":
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
            elif action_id == "save_workflow_preset_button":
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
                                    f_chain = [k.strip() for k in f_val.split(",") if k.strip()]
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
                                [k.strip() for k in s_val.split(",") if k.strip()]
                                if s_val is not None
                                else curr_sandwich.get("start_chain", [])
                            )
                            end_chain = (
                                [k.strip() for k in e_val.split(",") if k.strip()]
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

            elif action_id == "select_orchestration_mode":
                selected_option = action.get("selected_option", {})
                val = selected_option.get("value", "")
                if val.startswith("mode:"):
                    mode = val.split(":", 1)[1]
                    set_orchestration_mode(mode)
            elif action_id == "save_sandwich_chains_button":
                start_val = ""
                end_val = ""
                if isinstance(view_state, dict):
                    for block_val in view_state.values():
                        if isinstance(block_val, dict):
                            if "set_start_chain_input" in block_val:
                                start_val = block_val["set_start_chain_input"].get("value") or ""
                            if "set_end_chain_input" in block_val:
                                end_val = block_val["set_end_chain_input"].get("value") or ""
                start_chain = [k.strip() for k in start_val.split(",") if k.strip()]
                end_chain = [k.strip() for k in end_val.split(",") if k.strip()]
                set_sandwich_config(start_chain, end_chain)
                persist_active_workflow()
            elif action_id == "clear_sandwich_chains_button":
                set_sandwich_config([], [])
                persist_active_workflow()
            elif action_id == "set_start_chain_input":
                curr_sandwich = get_sandwich_config()
                val_str = action.get("value", "")
                start_chain = [k.strip() for k in val_str.split(",") if k.strip()]
                set_sandwich_config(start_chain, curr_sandwich.get("end_chain", []))
            elif action_id == "set_end_chain_input":
                curr_sandwich = get_sandwich_config()
                val_str = action.get("value", "")
                end_chain = [k.strip() for k in val_str.split(",") if k.strip()]
                set_sandwich_config(curr_sandwich.get("start_chain", []), end_chain)
            elif action_id == "save_fixed_chain_button":
                fixed_val = ""
                if isinstance(view_state, dict):
                    for block_val in view_state.values():
                        if isinstance(block_val, dict) and "set_fixed_chain_input" in block_val:
                            fixed_val = block_val["set_fixed_chain_input"].get("value") or ""
                chain = [k.strip() for k in fixed_val.split(",") if k.strip()]
                set_fixed_config(chain)
                persist_active_workflow()
            elif action_id == "clear_fixed_chain_button":
                set_fixed_config([])
                persist_active_workflow()
            elif action_id == "set_fixed_chain_input":
                val_str = action.get("value", "")
                chain = [k.strip() for k in val_str.split(",") if k.strip()]
                set_fixed_config(chain)
            elif action_id.startswith("select_agent_model:"):
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
            elif action_id.startswith("toggle_agent_radio:"):
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
            elif action_id.startswith("toggle_agent:"):
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
            elif action_id == "bulk_enable_all":
                reset_enabled_agents()
                log_audit_event(
                    event_type="ADMIN_CONFIG_CHANGE",
                    actor={"type": "slack_admin", "id": user_id or "unknown"},
                    action="bulk_enable_all_agents",
                    target="agents.enabled",
                    details={"action": "reset_enabled_agents"},
                    severity="CRITICAL",
                )
            elif action_id in ("bulk_disable_all", "bulk_disable_optional"):
                for key in AGENTS_CONFIG:
                    set_agent_enabled(key, False)
                log_audit_event(
                    event_type="ADMIN_CONFIG_CHANGE",
                    actor={"type": "slack_admin", "id": user_id or "unknown"},
                    action="bulk_disable_agents",
                    target="agents.enabled",
                    details={"mode": action_id},
                    severity="CRITICAL",
                )
            elif action_id == "restore_from_backup":
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


            elif action_id == "hitl_approve_tool":
                val = action.get("value", "")
                from services.hitl import approve_hitl_request, get_pending_request, mark_tool_approved

                req = get_pending_request(val)
                if req:
                    if user_id != req.initiator_user_id and not is_slack_admin(user_id):
                        logger.warning(f"[HITL] User {user_id} unauthorized to approve request {val}.")
                        log_audit_event(
                            event_type="HITL_UNAUTHORIZED_ATTEMPT",
                            actor={"type": "slack_user", "id": user_id or "unknown"},
                            action="hitl_approve_tool",
                            target=f"{req.channel_id}:{req.thread_ts}:{req.fn_name}",
                            details={"initiator": req.initiator_user_id},
                            severity="CRITICAL",
                        )
                        channel_id = req.channel_id or payload.get("channel", {}).get("id") or ""
                        if channel_id and user_id:
                            background_tasks.add_task(
                                slack_async_client.chat_postEphemeral,
                                channel=channel_id,
                                user=user_id,
                                text="⚠️ *Permission Denied:* Only the thread initiator or a workspace admin can approve this action.",
                            )
                        return Response(status_code=403)

                    approved_req = approve_hitl_request(val)
                    if approved_req:
                        log_audit_event(
                            event_type="HITL_APPROVED",
                            actor={"type": "slack_user", "id": user_id or "unknown"},
                            action="hitl_approve_tool",
                            target=f"{approved_req.channel_id}:{approved_req.thread_ts}:{approved_req.fn_name}",
                            details={"agent": approved_req.agent_name, "skill": approved_req.skill_name, "args": approved_req.fn_args},
                            severity="HIGH",
                        )
                        msg = payload.get("message", {})
                        if msg and msg.get("ts"):
                            background_tasks.add_task(
                                slack_async_client.chat_update,
                                channel=approved_req.channel_id,
                                ts=msg["ts"],
                                text=f"✅ Approved execution of `{approved_req.fn_name}` by <@{user_id}>. Please reply to the thread to resume the agent.",
                                blocks=[],
                            )
                elif ":" in val:
                    parts = val.split(":", 2)
                    if len(parts) == 3:
                        c_id, t_ts, f_name = parts
                        mark_tool_approved(c_id, t_ts, f_name)
                        log_audit_event(
                            event_type="HITL_APPROVED",
                            actor={"type": "slack_user", "id": user_id or "unknown"},
                            action="hitl_approve_tool",
                            target=f"{c_id}:{t_ts}:{f_name}",
                            details={"legacy": True},
                            severity="HIGH",
                        )
                        msg = payload.get("message", {})
                        if msg and msg.get("ts"):
                            background_tasks.add_task(
                                slack_async_client.chat_update,
                                channel=c_id,
                                ts=msg["ts"],
                                text=f"✅ Approved execution of `{f_name}` by <@{user_id}>. Please reply to the thread to resume the agent.",
                                blocks=[],
                            )
            elif action_id == "hitl_deny_tool":
                val = action.get("value", "")
                from services.hitl import deny_hitl_request, get_pending_request

                req = get_pending_request(val)
                if req:
                    if user_id != req.initiator_user_id and not is_slack_admin(user_id):
                        logger.warning(f"[HITL] User {user_id} unauthorized to deny request {val}.")
                        log_audit_event(
                            event_type="HITL_UNAUTHORIZED_ATTEMPT",
                            actor={"type": "slack_user", "id": user_id or "unknown"},
                            action="hitl_deny_tool",
                            target=f"{req.channel_id}:{req.thread_ts}:{req.fn_name}",
                            details={"initiator": req.initiator_user_id},
                            severity="CRITICAL",
                        )
                        channel_id = req.channel_id or payload.get("channel", {}).get("id") or ""
                        if channel_id and user_id:
                            background_tasks.add_task(
                                slack_async_client.chat_postEphemeral,
                                channel=channel_id,
                                user=user_id,
                                text="⚠️ *Permission Denied:* Only the thread initiator or a workspace admin can deny this action.",
                            )
                        return Response(status_code=403)

                    denied_req = deny_hitl_request(val)
                    if denied_req:
                        log_audit_event(
                            event_type="HITL_DENIED",
                            actor={"type": "slack_user", "id": user_id or "unknown"},
                            action="hitl_deny_tool",
                            target=f"{denied_req.channel_id}:{denied_req.thread_ts}:{denied_req.fn_name}",
                            details={"agent": denied_req.agent_name, "skill": denied_req.skill_name, "args": denied_req.fn_args},
                            severity="HIGH",
                        )
                        msg = payload.get("message", {})
                        if msg and msg.get("ts"):
                            background_tasks.add_task(
                                slack_async_client.chat_update,
                                channel=denied_req.channel_id,
                                ts=msg["ts"],
                                text=f"❌ Denied execution of `{denied_req.fn_name}` by <@{user_id}>. Please reply to the thread to inform the agent.",
                                blocks=[],
                            )
                elif ":" in val:
                    parts = val.split(":", 2)
                    if len(parts) == 3:
                        c_id, t_ts, f_name = parts
                        log_audit_event(
                            event_type="HITL_DENIED",
                            actor={"type": "slack_user", "id": user_id or "unknown"},
                            action="hitl_deny_tool",
                            target=f"{c_id}:{t_ts}:{f_name}",
                            details={"legacy": True},
                            severity="HIGH",
                        )
                        msg = payload.get("message", {})
                        if msg and msg.get("ts"):
                            background_tasks.add_task(
                                slack_async_client.chat_update,
                                channel=c_id,
                                ts=msg["ts"],
                                text=f"❌ Denied execution of `{f_name}` by <@{user_id}>. Please reply to the thread to inform the agent.",
                                blocks=[],
                            )
            elif action_id == "open_edit_workspace_context_modal":
                should_publish_home = False
                from orchestration.app_settings import get_workspace_context
                from services.slack_home_views import build_edit_workspace_context_modal

                if not is_slack_admin(user_id):
                    logger.warning(
                        f"[INTERACTIVITY] Non-admin user {user_id} attempted to open workspace context edit modal."
                    )
                    log_audit_event(
                        event_type="ADMIN_ACTION_DENIED",
                        actor={"type": "slack_user", "id": user_id or "unknown"},
                        action="open_edit_workspace_context_modal",
                        target="workspace_context",
                        details={"user_id": user_id},
                        severity="CRITICAL",
                    )
                    return Response(status_code=403)
                else:
                    trigger_id = payload.get("trigger_id")
                    if trigger_id:
                        modal_view = build_edit_workspace_context_modal(current_context=get_workspace_context())
                        background_tasks.add_task(
                            slack_async_client.views_open,
                            trigger_id=trigger_id,
                            view=modal_view,
                        )

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
                logger.warning(
                    f"[INTERACTIVITY] Non-admin user {user_id} attempted to submit workspace context."
                )
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
                values.get("workspace_context_input_block", {})
                .get("workspace_context_input", {})
                .get("value")
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
            logger.info(
                f"[INTERACTIVITY] Workspace context updated by admin {user_id}: {len(clean_val)} chars."
            )
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
