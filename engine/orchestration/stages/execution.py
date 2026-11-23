"""Stage 4: LLM execution, intermediate status updates, and coordination of Stage 4 collaborators."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any

import litellm
from slack_sdk.errors import SlackApiError

from config_store.agent_settings import settings_manager
from config_store.app_settings import app_settings_manager
from core.config import TOOL_INTERMEDIATE_UPDATE_DELAY_SECONDS, is_slack_admin
from core.llm_utils import execute_llm_with_retry
from orchestration.protocols import PipelineDeps, create_default_workspace_adapter
from orchestration.stages.control_plane_handler import ControlPlaneHandler
from orchestration.stages.tool_loop_runner import ToolLoopRunner
from orchestration.stages.tool_status import _build_tool_status_text
from orchestration.stages.turn_finalizer import TurnFinalizer, build_completion_kwargs
from orchestration.thread_state import thread_state_manager
from services.slack import post_agent_response
from services.thread_skills_store import thread_skills_manager
from skills.parser import LOAD_SKILL_SCHEMA

if TYPE_CHECKING:
    from orchestration.turn_pipeline import TurnContext

logger = logging.getLogger(__name__)


class ExecutionStage:
    """Thin coordinator for Stage 4 LLM execution, status updates, and tool loops."""

    def __init__(self, deps: PipelineDeps) -> None:
        self._deps = deps
        self.control_plane_handler = ControlPlaneHandler(deps)
        self.turn_finalizer = TurnFinalizer(deps)
        self.tool_loop_runner = ToolLoopRunner(
            deps,
            control_plane_handler=self.control_plane_handler,
            turn_finalizer=self.turn_finalizer,
        )

    @property
    def deps(self) -> PipelineDeps:
        return self._deps

    @deps.setter
    def deps(self, value: PipelineDeps) -> None:
        self._deps = value
        self.control_plane_handler.deps = value
        self.turn_finalizer.deps = value
        self.tool_loop_runner.deps = value

    @property
    def skill_registry(self) -> Any:
        return self.tool_loop_runner.skill_registry

    @property
    def spend_governance(self) -> Any:
        return self.tool_loop_runner.spend_governance

    def _is_mid_turn_budget_exceeded(self, ctx: TurnContext) -> bool:
        return self.tool_loop_runner.is_mid_turn_budget_exceeded(ctx)

    def _save_execution_checkpoint(
        self,
        ctx: TurnContext,
        pause_reason: str,
        active_tools: list[dict[str, Any]] | None = None,
        pending_tool_call: dict[str, Any] | None = None,
        hitl_request_id: str | None = None,
    ) -> str:
        return self.tool_loop_runner.save_execution_checkpoint(
            ctx=ctx,
            pause_reason=pause_reason,
            active_tools=active_tools,
            pending_tool_call=pending_tool_call,
            hitl_request_id=hitl_request_id,
        )

    async def stage_execute_llm(self, ctx: TurnContext) -> dict[str, Any] | None:
        """Stage 4: Execute model completion and delegate tool-calling turns to ToolLoopRunner."""
        if ctx.is_superseded():
            logger.info(f"[ROUTER] Thread {ctx.thread_key} turn was superseded before LLM call. Aborting.")
            return {"ok": False, "cancelled": True, "reason": "superseded"}

        already_503_errored = thread_state_manager.has_agent_503_errored(
            ctx.input.channel_id, ctx.input.thread_ts, ctx.agent_key
        )
        allow_503_immediate = already_503_errored

        logger.info(
            f"[LLM] Dispatching litellm.completion for {ctx.agent_key} (already_503_errored={already_503_errored})..."
        )
        llm_start = time.time()
        skill_registry = self.skill_registry

        agent_skills = ctx.agent_cfg.get("skills")
        if agent_skills is None:
            agent_skills = settings_manager.get_agent_skills(ctx.agent_key)

        global_skills = skill_registry.get_global_skills()
        active_thread_skills = thread_skills_manager.get_active_skills(ctx.input.channel_id, ctx.input.thread_ts)
        active_agent_skills = thread_skills_manager.get_active_agent_skills(
            ctx.input.channel_id, ctx.input.thread_ts, ctx.agent_key
        )
        combined_extra = list(dict.fromkeys(global_skills + active_thread_skills + active_agent_skills))
        if combined_extra:
            merged = list(agent_skills or [])
            for s in combined_extra:
                if s not in merged:
                    merged.append(s)
            agent_skills = merged

        if agent_skills:
            is_caller_admin = is_slack_admin(getattr(ctx.input, "user_id", None))
            thread_granted = set(active_thread_skills) | set(active_agent_skills)
            filtered = skill_registry.filter_skills_for_caller(
                agent_skills,
                is_admin=is_caller_admin,
                thread_granted_skills=thread_granted,
            )
            if isinstance(filtered, list):
                agent_skills = filtered
        ctx.authorized_skills = list(agent_skills or [])

        if getattr(ctx, "is_pure_skill_task", False):
            from core.models import resolve_active_max_words
            from nlp.message_builder import (
                build_deterministic_missing_skill_notice,
                has_prior_verified_skill_output,
            )

            file_ctx = getattr(ctx, "file_context", None)
            file_name = file_ctx.get("file_name") if isinstance(file_ctx, dict) else None
            auth_agents = file_ctx.get("authorized_agents") if isinstance(file_ctx, dict) else None
            unauthorized_file = bool(auth_agents is not None and ctx.agent_key not in auth_agents)

            missing_skills = [
                s for s in (getattr(ctx, "missing_required_skills", None) or []) if s not in ctx.authorized_skills
            ]
            file_req_skill = file_ctx.get("required_skill") if isinstance(file_ctx, dict) else None
            if file_req_skill and file_req_skill not in ctx.authorized_skills and file_req_skill not in missing_skills:
                missing_skills.append(file_req_skill)

            if missing_skills or unauthorized_file:
                prior_verified = has_prior_verified_skill_output(
                    getattr(ctx, "shared_round_evidence", None),
                    getattr(ctx, "effective_history", None),
                )
                active_max_words = resolve_active_max_words(
                    getattr(ctx, "modifiers", None),
                    thread_state_manager.get_max_words(ctx.input.channel_id, ctx.input.thread_ts),
                )

                if not prior_verified or (active_max_words is not None and active_max_words < 60):
                    logger.info(
                        f"[LLM] Short-circuiting pure-skill missing-skill turn for {ctx.agent_key} "
                        f"(missing={missing_skills}, unauthorized_file={unauthorized_file}, "
                        f"max_words={active_max_words}, prior_verified={prior_verified})."
                    )
                    ctx.output.final_text = build_deterministic_missing_skill_notice(
                        missing_skills=missing_skills,
                        file_name=file_name,
                        authorized_agents=auth_agents if unauthorized_file else None,
                        channel_id=ctx.input.channel_id,
                        thread_ts=ctx.input.thread_ts,
                        agent_key=ctx.agent_key,
                    )
                    ctx.output.is_deterministic_missing_skill = True
                    return None

        expired_thread_skills = list(
            dict.fromkeys(
                thread_skills_manager.get_expired_skills(ctx.input.channel_id, ctx.input.thread_ts)
                + thread_skills_manager.get_expired_agent_skills(
                    ctx.input.channel_id, ctx.input.thread_ts, ctx.agent_key
                )
            )
        )
        thread_ttl_hours = app_settings_manager.get_thread_skill_expiration_time()

        active_tools: list[dict[str, Any]] = [LOAD_SKILL_SCHEMA] if agent_skills else []
        completion_kwargs = build_completion_kwargs(
            model=ctx.agent_cfg["model"],
            messages=ctx.messages,
            tools=active_tools if agent_skills else None,
            tool_choice="auto" if agent_skills else None,
        )

        exec_retry_fn = getattr(self._deps, "llm_executor", None) or execute_llm_with_retry
        intermediate_delay_secs = getattr(self._deps, "intermediate_delay_secs", None)
        if intermediate_delay_secs is None:
            intermediate_delay_secs = TOOL_INTERMEDIATE_UPDATE_DELAY_SECONDS

        username = ctx.agent_cfg.get("username", ctx.agent_key) if ctx.agent_cfg else ctx.agent_key
        resolved_icon = ctx.icon_url or (ctx.agent_cfg.get("icon_url") if ctx.agent_cfg else None)
        current_status_tool_calls: list[Any] | None = None
        last_status_text: str | None = None

        async def _update_intermediate_status_if_active(tool_calls_for_status: list[Any] | None) -> None:
            nonlocal last_status_text
            if ctx.is_superseded() or not ctx.intermediate_msg_ts:
                return
            new_text = _build_tool_status_text(username, tool_calls_for_status or [])
            if new_text == last_status_text:
                return
            last_status_text = new_text
            try:
                ws_adapter = getattr(self._deps, "workspace_adapter", None)
                if ws_adapter is not None:
                    await ws_adapter.update_agent_turn(
                        channel_id=ctx.input.channel_id,
                        message_ts=ctx.intermediate_msg_ts,
                        text=new_text,
                    )
                else:
                    update_fn = getattr(self._deps, "update_message_fn", None)
                    if update_fn is not None:
                        await update_fn(
                            channel_id=ctx.input.channel_id,
                            ts=ctx.intermediate_msg_ts,
                            text=new_text,
                        )
            except (SlackApiError, OSError, RuntimeError, ValueError, TypeError) as e:
                logger.debug(f"[ROUTER] Failed to update intermediate status in-place: {e}")

        async def _delayed_intermediate_post() -> None:
            nonlocal last_status_text
            try:
                await asyncio.sleep(intermediate_delay_secs)
                if not ctx.is_superseded() and not ctx.intermediate_msg_ts:
                    status_text = _build_tool_status_text(username, current_status_tool_calls or [])
                    last_status_text = status_text
                    ws_adapter = getattr(self._deps, "workspace_adapter", None)
                    if ws_adapter is None:
                        post_resp_fn = getattr(self._deps, "slack_poster", None) or post_agent_response
                        ws_adapter = create_default_workspace_adapter(post_agent_fn=post_resp_fn)
                    logger.info(
                        f"[ROUTER] Turn delay reached ({intermediate_delay_secs}s). Posting intermediate status for {username}."
                    )
                    res = await ws_adapter.post_agent_turn(
                        channel_id=ctx.input.channel_id,
                        thread_ts=ctx.input.thread_ts,
                        text=status_text,
                        username=username,
                        icon_url=resolved_icon,
                    )
                    if isinstance(res, dict) and res.get("ts"):
                        ctx.intermediate_msg_ts = res["ts"]
            except asyncio.CancelledError:
                pass
            except (SlackApiError, OSError, RuntimeError, ValueError, TypeError) as e:
                logger.warning(f"[ROUTER] Failed to post intermediate tool status: {e}")

        delay_task = asyncio.create_task(_delayed_intermediate_post())

        try:
            response = await exec_retry_fn(
                litellm.acompletion,
                completion_kwargs,
                agent_key=ctx.agent_key,
                thread_key=ctx.thread_key,
                is_superseded_fn=ctx.is_superseded,
                allow_503_immediate_retry=allow_503_immediate,
                turn_token_mapping=ctx.turn_token_mapping,
            )

            if response is None and ctx.is_superseded():
                return {"ok": False, "cancelled": True, "reason": "superseded"}

            ctx.add_usage(response)
            llm_duration = time.time() - llm_start
            logger.info(f"[LLM] Received response for {ctx.agent_key} in {llm_duration:.2f} seconds.")

            if ctx.is_superseded():
                logger.info(f"[ROUTER] Thread {ctx.thread_key} turn was superseded after LLM call. Aborting.")
                return {"ok": False, "cancelled": True, "reason": "superseded"}

            if not (hasattr(response, "choices") and response.choices):
                return None

            response_message = response.choices[0].message
            tool_calls = getattr(response_message, "tool_calls", None)

            if not tool_calls or not isinstance(tool_calls, (list, tuple)):
                self.turn_finalizer.finalize_message(ctx, response_message)
                return None

            current_status_tool_calls = list(tool_calls)
            await _update_intermediate_status_if_active(current_status_tool_calls)

            return await self.execute_tool_loop(
                ctx,
                active_tools=active_tools,
                agent_skills=list(agent_skills or []),
                expired_thread_skills=expired_thread_skills,
                thread_ttl_hours=thread_ttl_hours,
                initial_response_message=response_message,
                allow_503_immediate=allow_503_immediate,
                on_status_update=_update_intermediate_status_if_active,
            )
        finally:
            if not delay_task.done():
                delay_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await delay_task

    async def execute_tool_loop(
        self,
        ctx: TurnContext,
        *,
        active_tools: list[dict[str, Any]],
        agent_skills: list[str],
        expired_thread_skills: list[str],
        thread_ttl_hours: float,
        initial_response_message: Any | None = None,
        allow_503_immediate: bool = False,
        on_status_update: Any = None,
    ) -> dict[str, Any] | None:
        """Delegates Stage-4 and checkpoint-resumed tool execution loops to ToolLoopRunner."""
        return await self.tool_loop_runner.run(
            ctx,
            active_tools=active_tools,
            agent_skills=agent_skills,
            expired_thread_skills=expired_thread_skills,
            thread_ttl_hours=thread_ttl_hours,
            initial_response_message=initial_response_message,
            allow_503_immediate=allow_503_immediate,
            on_status_update=on_status_update,
        )
