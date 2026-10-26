import asyncio
import logging
from typing import Any

from fastapi import BackgroundTasks, Response

from core.config import is_slack_admin
from services.audit_logger import log_audit_event
from services.slack import (
    post_ephemeral_to_slack,
    post_message_to_slack,
    update_slack_message,
)

logger = logging.getLogger("IzvorBackend.SlackInteractivity.HITL")


async def _execute_and_resume_hitl_approval(req: Any, user_id: str) -> None:
    """Executes the pre-approved HITL tool deterministically in background and resumes the calling agent."""
    from orchestration.agent_settings import settings_manager
    from orchestration.thread_skills_manager import thread_skills_manager
    from orchestration.thread_state import thread_state_manager
    from orchestration.turn_pipeline import TurnContext, TurnPipeline
    from skills.loader import skill_registry

    channel_id = req.channel_id
    thread_ts = req.thread_ts
    agent_name = req.agent_name
    fn_name = req.fn_name
    fn_args = req.fn_args

    logger.info(
        f"[HITL] Auto-executing approved tool '{fn_name}' for agent '{agent_name}' in {(channel_id, thread_ts)}"
    )

    # 1. Resolve agent skills + thread-granted skills
    agents_map = settings_manager.get_agents_config()
    agent_cfg = agents_map.get(agent_name, {})
    agent_skills = list(agent_cfg.get("skills", []))
    thread_skills = thread_skills_manager.get_active_skills(channel_id, thread_ts)
    expired_skills = thread_skills_manager.get_expired_skills(channel_id, thread_ts)
    ttl_hours = thread_skills_manager.get_ttl_seconds() / 3600.0
    effective_skills = list(set(agent_skills + thread_skills))

    # 2. Execute tool deterministically
    try:
        tool_result = await asyncio.to_thread(
            skill_registry.execute_tool,
            fn_name,
            fn_args,
            authorized_skills=effective_skills,
            expired_skills=expired_skills,
            ttl_hours=ttl_hours,
            agent_name=agent_name,
        )
    except Exception as e:  # noqa: BLE001 - Resilient tool execution error capture
        logger.error(f"[HITL] Error executing tool '{fn_name}': {e}", exc_info=True)
        tool_result = f"Error executing tool '{fn_name}': {e}"

    # 3. Post tool execution result to Slack thread so all participants have visibility
    display_res = str(tool_result)
    if len(display_res) > 2000:
        display_res = display_res[:2000] + "\n... [truncated]"

    try:
        await post_message_to_slack(
            channel_id=channel_id,
            thread_ts=thread_ts,
            text=f"🛠️ *Tool `{fn_name}` executed following approval by <@{user_id}>:*\n```{display_res}```",
        )
    except Exception as e:  # noqa: BLE001 - Slack notification delivery boundary
        logger.error(f"[HITL] Failed to post tool result to Slack: {e}")

    # 4. Resume the calling agent with the result context
    import json
    from services.turn_checkpoint_store import turn_checkpoint_store
    from orchestration.agent_router import continue_discussion_from_turn_result

    chk = turn_checkpoint_store.get_checkpoint_by_hitl_request(req.request_id)
    if not chk:
        logger.warning(
            f"[HITL] No checkpoint found matching request_id '{req.request_id}' in {(channel_id, thread_ts)}. "
            f"Falling back to fresh continuation turn."
        )

    pipeline = TurnPipeline()
    if chk and chk.status == "ACTIVE":
        async def _run_resumption():
            turn_res = await pipeline.resume_from_checkpoint(
                checkpoint=chk,
                tool_result=tool_result,
                user_id=user_id,
            )
            await continue_discussion_from_turn_result(
                channel_id=channel_id,
                thread_ts=thread_ts,
                turn_res=turn_res,
                user_id=user_id,
            )

        thread_state_manager.dispatch_discussion_task(channel_id, thread_ts, _run_resumption())
    else:
        resume_prompt = (
            f"[{agent_name}] Tool '{fn_name}' execution was approved by <@{user_id}>.\n"
            f"Tool Result:\n{tool_result}\n\n"
            f"Please analyze this result and provide your response to continue the discussion."
        )
        ctx = TurnContext(
            channel_id=channel_id,
            thread_ts=thread_ts,
            raw_text=resume_prompt,
            user_id=user_id,
            is_bot_trigger=True,
            was_explicit_target=True,
        )
        thread_state_manager.dispatch_discussion_task(channel_id, thread_ts, pipeline.run(ctx))


async def _resume_hitl_denial(req: Any, user_id: str) -> None:
    """Notifies the calling agent that the requested tool was denied by human supervisor and resumes discussion."""
    import json
    from orchestration.agent_router import continue_discussion_from_turn_result
    from orchestration.thread_state import thread_state_manager
    from orchestration.turn_pipeline import TurnContext, TurnPipeline
    from services.turn_checkpoint_store import turn_checkpoint_store

    channel_id = req.channel_id
    thread_ts = req.thread_ts
    agent_name = req.agent_name
    fn_name = req.fn_name

    logger.info(
        f"[HITL] Resuming discussion with denial notification for agent '{agent_name}' in {(channel_id, thread_ts)}"
    )

    chk = turn_checkpoint_store.get_checkpoint_by_hitl_request(req.request_id)
    pipeline = TurnPipeline()
    denial_result = json.dumps({
        "status": "denied",
        "error": f"Tool '{fn_name}' execution was denied by human supervisor <@{user_id}>. Proceed with analysis without calling this tool."
    })

    if chk and chk.status == "ACTIVE":
        async def _run_denial_resumption():
            turn_res = await pipeline.resume_from_checkpoint(
                checkpoint=chk,
                tool_result=denial_result,
                user_id=user_id,
            )
            await continue_discussion_from_turn_result(
                channel_id=channel_id,
                thread_ts=thread_ts,
                turn_res=turn_res,
                user_id=user_id,
            )

        thread_state_manager.dispatch_discussion_task(channel_id, thread_ts, _run_denial_resumption())
    else:
        resume_prompt = (
            f"[{agent_name}] Tool '{fn_name}' execution was denied by supervisor <@{user_id}>.\n"
            f"Please proceed with the discussion without calling or waiting for this tool."
        )
        ctx = TurnContext(
            channel_id=channel_id,
            thread_ts=thread_ts,
            raw_text=resume_prompt,
            user_id=user_id,
            is_bot_trigger=True,
            was_explicit_target=True,
        )
        thread_state_manager.dispatch_discussion_task(channel_id, thread_ts, pipeline.run(ctx))


async def _handle_hitl_approve_tool(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    val = action.get("value", "")
    from services.hitl import approve_hitl_request, get_pending_request

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
                    post_ephemeral_to_slack,
                    channel_id=channel_id,
                    user_id=user_id,
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
                    update_slack_message,
                    channel_id=approved_req.channel_id,
                    ts=msg["ts"],
                    text=f"✅ Approved execution of `{approved_req.fn_name}` by <@{user_id}>. Executing tool and resuming agent...",
                    blocks=[],
                )
            background_tasks.add_task(
                _execute_and_resume_hitl_approval,
                req=approved_req,
                user_id=user_id,
            )
    else:
        channel_id = payload.get("channel", {}).get("id") or ""
        if channel_id and user_id:
            background_tasks.add_task(
                post_ephemeral_to_slack,
                channel_id=channel_id,
                user_id=user_id,
                text="⚠️ *Request Expired or Handled:* This approval request is either invalid, already processed, or has expired.",
            )
    return None


async def _handle_hitl_deny_tool(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
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
                    post_ephemeral_to_slack,
                    channel_id=channel_id,
                    user_id=user_id,
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
                    update_slack_message,
                    channel_id=denied_req.channel_id,
                    ts=msg["ts"],
                    text=f"❌ Denied execution of `{denied_req.fn_name}` by <@{user_id}>. Resuming agent...",
                    blocks=[],
                )
            background_tasks.add_task(
                _resume_hitl_denial,
                req=denied_req,
                user_id=user_id,
            )
    else:
        channel_id = payload.get("channel", {}).get("id") or ""
        if channel_id and user_id:
            background_tasks.add_task(
                post_ephemeral_to_slack,
                channel_id=channel_id,
                user_id=user_id,
                text="⚠️ *Request Expired or Handled:* This denial request is either invalid, already processed, or has expired.",
            )
    return None
