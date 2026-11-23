"""Stage 4 iterative tool-calling loop runner with HITL pause, budget guards, and argument unmasking."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sqlite3
from typing import TYPE_CHECKING, Any

import litellm

from config_store.system_settings import (
    get_tool_call_loop_timeout_seconds,
    system_settings_manager,
)
from core.llm_utils import execute_llm_with_retry
from orchestration.protocols import PipelineDeps
from orchestration.stages.control_plane_handler import ControlPlaneHandler
from orchestration.stages.turn_finalizer import (
    TurnFinalizer,
    build_completion_kwargs,
    ensure_all_tool_calls_responded,
)
from orchestration.thread_state import thread_state_manager
from services.local_tools import execute_local_tool
from services.mcp.schema_translator import parse_mcp_tool_name

if TYPE_CHECKING:
    from orchestration.turn_pipeline import TurnContext

logger = logging.getLogger(__name__)

OUTBOUND_NO_UNMASK_TOOLS: frozenset[str] = frozenset({"web_search", "fetch_web_page"})


def _should_unmask_tool_args(fn_name: str) -> bool:
    """Returns False when outbound web or MCP PII policy forbids sending raw PII in arguments."""
    if fn_name in OUTBOUND_NO_UNMASK_TOOLS:
        return False
    parsed_mcp = parse_mcp_tool_name(fn_name)
    if parsed_mcp is not None:
        server_name, _ = parsed_mcp
        srv_cfg = system_settings_manager.get_mcp_server_config(server_name)
        if srv_cfg is None:
            return False
        outbound_masking = (
            srv_cfg.get("outbound_pii_masking", True)
            if isinstance(srv_cfg, dict)
            else getattr(srv_cfg, "outbound_pii_masking", True)
        )
        if bool(outbound_masking):
            return False
    return True


def _unmask_argument_values(val: Any, mapping: dict[str, str]) -> Any:
    """Recursively unmasks string argument values if any PII token mappings exist."""
    if not mapping or val is None:
        return val
    from services.security.pii_masker import pii_masker

    if isinstance(val, str):
        return pii_masker.unmask(val, mapping)
    elif isinstance(val, dict):
        return {k: _unmask_argument_values(v, mapping) for k, v in val.items()}
    elif isinstance(val, list):
        return [_unmask_argument_values(item, mapping) for item in val]
    return val


def _is_tool_error(result: Any) -> bool:
    """Returns True when a tool result represents an error or denied execution."""
    if isinstance(result, Exception):
        return True
    if isinstance(result, dict):
        if result.get("is_error") is True:
            return True
        if "error" in result and len(result) <= 2:
            return True
    if isinstance(result, str):
        lowered = result.strip().lower()
        if lowered.startswith(("error:", "tool execution error:", "permission denied", "permissionerror:")):
            return True
        if lowered.startswith('{"error":') and len(result) < 300:
            return True
    return False


class ToolLoopRunner:
    """Executes Stage-4 and checkpoint-resumed iterative tool loops."""

    def __init__(
        self,
        deps: PipelineDeps,
        *,
        control_plane_handler: ControlPlaneHandler | None = None,
        turn_finalizer: TurnFinalizer | None = None,
    ) -> None:
        self._deps = deps
        self.control_plane_handler = control_plane_handler or ControlPlaneHandler(deps)
        self.turn_finalizer = turn_finalizer or TurnFinalizer(deps)

    @property
    def deps(self) -> PipelineDeps:
        return self._deps

    @deps.setter
    def deps(self, value: PipelineDeps) -> None:
        self._deps = value
        self.control_plane_handler.deps = value
        self.turn_finalizer.deps = value

    @property
    def skill_registry(self) -> Any:
        reg = getattr(self._deps, "skill_registry", None)
        if reg is None:
            from skills.registry import get_skill_registry

            reg = get_skill_registry()
        return reg

    @property
    def spend_governance(self) -> Any:
        sg = getattr(self._deps, "spend_governance", None)
        if sg is None:
            from pipeline.spend_governance import spend_governance as default_sg

            sg = default_sg
        return sg

    def is_mid_turn_budget_exceeded(self, ctx: TurnContext) -> bool:
        """
        Calculates thread cost plus current turn's accumulated token usage,
        and checks if the circuit breaker limit or daily budget is exceeded.
        """
        try:
            from agents.usage_agent import calculate_thread_costs
            from config_store.app_settings import (
                get_circuit_breaker_threshold,
                get_daily_budget_usd,
                is_circuit_breaker_enabled,
            )
            from services.pricing_service import pricing_service

            model = ctx.agent_cfg.get("model", "") if ctx.agent_cfg else ""
            prompt_tokens = getattr(ctx.output, "total_prompt_tokens", 0) or 0
            completion_tokens = getattr(ctx.output, "total_completion_tokens", 0) or 0
            custom_calc_fn = getattr(self._deps, "calc_costs_fn", None)
            if custom_calc_fn is not None and custom_calc_fn is not calculate_thread_costs:
                unpersisted_prompt = prompt_tokens
                unpersisted_completion = completion_tokens
                calc_costs_fn = custom_calc_fn
            else:
                persisted_prompt = getattr(ctx.output, "persisted_prompt_tokens", 0) or 0
                persisted_completion = getattr(ctx.output, "persisted_completion_tokens", 0) or 0
                unpersisted_prompt = max(0, prompt_tokens - persisted_prompt)
                unpersisted_completion = max(0, completion_tokens - persisted_completion)
                calc_costs_fn = calculate_thread_costs

            incremental_cost = 0.0
            if model and (unpersisted_prompt > 0 or unpersisted_completion > 0):
                pricing = pricing_service.get_pricing(model)
                input_rate_1m = float(pricing.get("input_price_per_1m", 0.0) or 0.0)
                output_rate_1m = float(pricing.get("output_price_per_1m", 0.0) or 0.0)
                incremental_cost = ((unpersisted_prompt / 1_000_000.0) * input_rate_1m) + (
                    (unpersisted_completion / 1_000_000.0) * output_rate_1m
                )

            cost_info = calc_costs_fn(ctx.input.channel_id, ctx.input.thread_ts)
            current_cost = float(cost_info.get("grand_total_cost_usd", 0.0) or 0.0) + incremental_cost
            threshold = get_circuit_breaker_threshold()
            session_id = f"{ctx.input.channel_id}:{ctx.input.thread_ts}"
            daily_budget = get_daily_budget_usd()

            tripped, _, _ = self.spend_governance.is_budget_exceeded(
                session_id=session_id,
                threshold_usd=threshold,
                enabled=is_circuit_breaker_enabled(),
                current_cost_usd=current_cost,
                daily_budget_usd=daily_budget,
                exclude_reservation_id=f"{ctx.input.channel_id}:{ctx.input.thread_ts}:{ctx.agent_key}",
            )
            return bool(tripped)
        except (ValueError, TypeError, KeyError, RuntimeError, AttributeError, OSError, sqlite3.Error) as e:
            logger.debug(f"[EXECUTION] Error checking mid-turn budget: {e}")
            return False

    def save_execution_checkpoint(
        self,
        ctx: TurnContext,
        pause_reason: str,
        active_tools: list[dict[str, Any]] | None = None,
        pending_tool_call: dict[str, Any] | None = None,
        hitl_request_id: str | None = None,
    ) -> str:
        """Persists a TurnCheckpoint when tool execution pauses for HITL or mid-turn budget guard."""
        import uuid

        from core.domain import TurnCheckpoint
        from services.turn_checkpoint_store import turn_checkpoint_store

        chk_id = str(uuid.uuid4())
        try:
            chk = TurnCheckpoint(
                checkpoint_id=chk_id,
                session_id=getattr(ctx, "session_id", f"{ctx.input.channel_id}:{ctx.input.thread_ts}"),
                channel_id=ctx.input.channel_id,
                thread_ts=ctx.input.thread_ts,
                agent_key=ctx.agent_key,
                pause_reason=pause_reason,
                hitl_request_id=hitl_request_id,
                prompt=(ctx.prompt or ctx.raw_text_cleaned or ctx.input.raw_text or ""),
                messages=ctx.messages or [],
                pending_tool_call=pending_tool_call,
                active_tools=active_tools or [],
                skills_used=ctx.output.skills_used or [],
                token_mapping=ctx.turn_token_mapping or {},
                accumulated_history=ctx.input.in_memory_history or [],
                prompt_tokens=ctx.output.total_prompt_tokens,
                completion_tokens=ctx.output.total_completion_tokens,
            )
            turn_checkpoint_store.save_checkpoint(chk)
            ctx.output.checkpoint_id = chk_id
            return chk_id
        except (OSError, sqlite3.Error, ValueError, TypeError, RuntimeError) as e:
            logger.error(f"[EXECUTION] Failed to save {pause_reason} checkpoint: {e}")
            return ""

    async def run(
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
        """Runs the iterative tool execution loop until completion, HITL pause, budget halt, or timeout."""
        skill_registry = self.skill_registry
        exec_retry_fn = getattr(self._deps, "llm_executor", None) or execute_llm_with_retry
        custom_tool_executor = getattr(self._deps, "tool_executor", None)
        max_tool_iterations = getattr(self._deps, "max_tool_iterations", None)
        if max_tool_iterations is None:
            max_tool_iterations = system_settings_manager.get_max_tool_iterations()
        timeout_secs = getattr(self._deps, "timeout_secs", None)
        if timeout_secs is None:
            timeout_secs = get_tool_call_loop_timeout_seconds()

        response_message = initial_response_message
        if response_message is None:
            follow_up_kwargs = build_completion_kwargs(
                model=ctx.agent_cfg.get("model", ""),
                messages=ctx.messages,
                tools=active_tools if active_tools else None,
                tool_choice="auto" if active_tools else None,
            )
            initial_resp = await exec_retry_fn(
                litellm.acompletion,
                follow_up_kwargs,
                agent_key=f"{ctx.agent_key} resumed",
                thread_key=ctx.thread_key,
                is_superseded_fn=ctx.is_superseded,
                max_attempts=max(1, int(system_settings_manager.get_tool_call_num_retries()) + 1),
                allow_503_immediate_retry=allow_503_immediate,
                turn_token_mapping=ctx.turn_token_mapping,
            )
            if initial_resp is None:
                if ctx.is_superseded():
                    return {"ok": False, "cancelled": True, "reason": "superseded"}
                return None
            ctx.add_usage(initial_resp)
            if ctx.is_superseded():
                return {"ok": False, "cancelled": True, "reason": "superseded"}
            if not (hasattr(initial_resp, "choices") and initial_resp.choices):
                return None
            response_message = initial_resp.choices[0].message

        tool_calls = getattr(response_message, "tool_calls", None)
        if not tool_calls or not isinstance(tool_calls, (list, tuple)):
            self.turn_finalizer.finalize_message(ctx, response_message)
            return None

        iteration = 0
        substantive_tool_outputs_count = 0

        async def _execute_loop() -> dict[str, Any] | None:
            nonlocal iteration, response_message, tool_calls, substantive_tool_outputs_count
            while tool_calls and isinstance(tool_calls, (list, tuple)) and iteration < max_tool_iterations:
                iteration += 1
                if on_status_update is not None:
                    await on_status_update(list(tool_calls))
                dumped_msg = (
                    response_message.model_dump()
                    if hasattr(response_message, "model_dump")
                    else {"role": "assistant", "content": getattr(response_message, "content", "") or ""}
                )
                ctx.messages.append(dumped_msg)

                for tool_call in tool_calls:
                    fn_name = tool_call.function.name
                    try:
                        raw_args = (
                            json.loads(tool_call.function.arguments)
                            if isinstance(tool_call.function.arguments, str)
                            else tool_call.function.arguments
                        )
                    except (json.JSONDecodeError, TypeError) as decode_err:
                        logger.warning(f"[TOOL CALL] Malformed JSON in arguments for {fn_name}: {decode_err}")
                        tool_result_str = (
                            f"<untrusted_file_content>\nTOOL EXECUTION ERROR: Invalid JSON arguments: {decode_err}\n</untrusted_file_content>"
                        )
                        ctx.messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "name": fn_name,
                                "content": tool_result_str,
                            }
                        )
                        continue
                    should_unmask = _should_unmask_tool_args(fn_name)
                    fn_args = (
                        _unmask_argument_values(raw_args, getattr(ctx, "turn_token_mapping", {}))
                        if should_unmask
                        else raw_args
                    )
                    cached_evidence = None

                    from core.domain import ToolPrincipal

                    principal = ToolPrincipal(kind="agent", id=ctx.agent_key)
                    if fn_name == "load_skill":
                        tool_result = await skill_registry.execute_tool_async(
                            fn_name,
                            fn_args,
                            authorized_skills=agent_skills,
                            expired_skills=expired_thread_skills,
                            ttl_hours=thread_ttl_hours,
                            agent_name=principal,
                        )
                        skill_name = fn_args.get("skill_name", "").strip()
                        skill_def = skill_registry.get(skill_name)
                        is_globally_allowed = bool(
                            skill_def and skill_def.is_global and not getattr(skill_def, "requires_admin", False)
                        )
                        if skill_def and ((agent_skills and skill_name in agent_skills) or is_globally_allowed):
                            if getattr(skill_def, "mcp_server", ""):
                                try:
                                    from services.mcp.client_manager import get_mcp_client_manager

                                    await get_mcp_client_manager().discover_server_tools(skill_def.mcp_server)
                                except (OSError, RuntimeError, TimeoutError, ValueError, TypeError) as e:
                                    logger.warning(
                                        f"[EXECUTION] Failed on-demand MCP discovery for '{skill_def.mcp_server}': {e}"
                                    )

                            self.control_plane_handler.unlock_tools(
                                skill_def=skill_def,
                                ctx=ctx,
                                active_tools=active_tools,
                            )
                    else:
                        from services.local_tools import is_agent_authorized_for_tool

                        if not is_agent_authorized_for_tool(
                            principal, fn_name, authorized_skills=agent_skills, skill_registry=skill_registry
                        ):
                            tool_result = await skill_registry.execute_tool_async(
                                fn_name,
                                fn_args,
                                authorized_skills=agent_skills,
                                expired_skills=expired_thread_skills,
                                ttl_hours=thread_ttl_hours,
                                agent_name=principal,
                            )
                        else:
                            s_name = skill_registry.find_skill_for_tool(fn_name)
                            skill_def = skill_registry.get(s_name) if s_name else None

                            from services.mcp.client_manager import get_mcp_client_manager

                            is_mcp_mutating = get_mcp_client_manager().is_tool_mutating(fn_name)
                            requires_hitl = bool(skill_def and skill_def.requires_human_ITL) or is_mcp_mutating

                            if requires_hitl:
                                from services.hitl import consume_tool_approval, request_human_approval

                                if not consume_tool_approval(
                                    ctx.input.channel_id, ctx.input.thread_ts, fn_name, fn_args, agent_name=ctx.agent_key
                                ):
                                    effective_skill_name = skill_def.name if skill_def else (s_name or "mcp_tool")
                                    req_id = await request_human_approval(
                                        ctx.input.channel_id,
                                        ctx.input.thread_ts,
                                        ctx.agent_key,
                                        effective_skill_name,
                                        fn_name,
                                        fn_args,
                                        initiator_user_id=ctx.input.user_id,
                                    )
                                    tool_result_str = (
                                        "<untrusted_file_content>\n"
                                        f"TOOL EXECUTION PAUSED: Tool '{fn_name}' requires human approval. "
                                        "An approval request has been sent to the Slack thread. "
                                        "Your turn is paused now. Explain briefly what approval is required and end your message without nominating other agents. "
                                        "Do NOT retry calling this tool until approval is granted."
                                        "\n</untrusted_file_content>"
                                    )
                                    ctx.messages.append(
                                        {
                                            "role": "tool",
                                            "tool_call_id": tool_call.id,
                                            "name": fn_name,
                                            "content": tool_result_str,
                                        }
                                    )
                                    ctx.output.halted = True
                                    ctx.output.waiting_hitl = True

                                    responded_ids = {
                                        m.get("tool_call_id") for m in ctx.messages if m.get("role") == "tool"
                                    }
                                    for sibling_tc in tool_calls:
                                        sibling_id = getattr(sibling_tc, "id", None)
                                        if sibling_id and sibling_id not in responded_ids:
                                            sibling_fn = getattr(sibling_tc.function, "name", "tool")
                                            ctx.messages.append(
                                                {
                                                    "role": "tool",
                                                    "tool_call_id": sibling_id,
                                                    "name": sibling_fn,
                                                    "content": (
                                                        "<untrusted_file_content>\n"
                                                        f"TOOL EXECUTION DEFERRED: Sibling tool '{sibling_fn}' "
                                                        f"was not executed because turn paused for human approval on '{fn_name}'."
                                                        "\n</untrusted_file_content>"
                                                    ),
                                                }
                                            )
                                            responded_ids.add(sibling_id)

                                    self.save_execution_checkpoint(
                                        ctx=ctx,
                                        pause_reason="hitl",
                                        active_tools=active_tools,
                                        pending_tool_call={
                                            "id": getattr(tool_call, "id", None),
                                            "name": fn_name,
                                            "args": fn_args,
                                        },
                                        hitl_request_id=req_id,
                                    )
                                    if s_name and s_name not in ctx.output.skills_used:
                                        ctx.output.skills_used.append(s_name)
                                    break

                            if thread_state_manager.evidence.is_cacheable_tool(fn_name):
                                cached_evidence = thread_state_manager.evidence.get_evidence(
                                    ctx.input.channel_id, ctx.input.thread_ts, fn_name, fn_args, agent_key=ctx.agent_key
                                )

                            if cached_evidence is not None:
                                tool_result = cached_evidence.result
                                logger.info(
                                    f"[EVIDENCE] Reusing cached round evidence for tool '{fn_name}' from agent {cached_evidence.agent_key} in {ctx.input.channel_id}:{ctx.input.thread_ts}"
                                )
                            else:
                                if custom_tool_executor is not None and custom_tool_executor is not execute_local_tool:
                                    sig = inspect.signature(custom_tool_executor)
                                    kwargs: dict[str, Any] = {}
                                    if "agent_name" in sig.parameters:
                                        kwargs["agent_name"] = ctx.agent_key
                                    if "authorized_skills" in sig.parameters:
                                        kwargs["authorized_skills"] = agent_skills
                                    if inspect.iscoroutinefunction(custom_tool_executor):
                                        tool_result = await custom_tool_executor(fn_name, fn_args, **kwargs)
                                    else:
                                        tool_result = await asyncio.to_thread(
                                            custom_tool_executor, fn_name, fn_args, **kwargs
                                        )
                                else:
                                    tool_result = await skill_registry.execute_tool_async(
                                        fn_name,
                                        fn_args,
                                        authorized_skills=agent_skills,
                                        expired_skills=expired_thread_skills,
                                        ttl_hours=thread_ttl_hours,
                                        agent_name=principal,
                                    )

                    if fn_name == "load_skill":
                        s_name = fn_args.get("skill_name", "").strip()
                    else:
                        s_name = skill_registry.find_skill_for_tool(fn_name) or fn_name
                    if s_name and not _is_tool_error(tool_result):
                        if s_name not in ctx.output.skills_used:
                            ctx.output.skills_used.append(s_name)

                    if not _is_tool_error(tool_result):
                        self.control_plane_handler.apply_tool_effect(
                            fn_name=fn_name,
                            fn_args=fn_args,
                            ctx=ctx,
                        )

                    from core.security_utils import wrap_untrusted_content
                    from services.security.guardrail import guardrail_service

                    CONTROL_PLANE_OR_META_TOOLS = (
                        "load_skill",
                        "nominate_next_speaker",
                        "request_user_clarification",
                        "signal_consensus_convergence",
                    )
                    raw_tool_str = str(tool_result)
                    if fn_name not in CONTROL_PLANE_OR_META_TOOLS and raw_tool_str.strip():
                        screening = await guardrail_service.screen_untrusted_tool_output(fn_name, raw_tool_str)
                        screened_str = screening.sanitized_content
                    else:
                        screened_str = raw_tool_str

                    tool_result_str = wrap_untrusted_content(screened_str)

                    if cached_evidence is None and not getattr(ctx.output, "halted", False):
                        res_str = screened_str if isinstance(tool_result, str) else screened_str
                        if not (res_str.strip().lower().startswith("permission denied") or '"error":' in res_str):
                            thread_state_manager.add_round_evidence(
                                channel_id=ctx.input.channel_id,
                                thread_ts=ctx.input.thread_ts,
                                tool_name=fn_name,
                                arguments=fn_args,
                                result=res_str,
                                agent_key=ctx.agent_key,
                            )

                    ctx.messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": fn_name,
                            "content": tool_result_str,
                        }
                    )
                    if fn_name != "load_skill" and not _is_tool_error(tool_result):
                        substantive_tool_outputs_count += 1

                if ctx.is_superseded():
                    logger.info(
                        f"[ROUTER] Thread {ctx.thread_key} turn was superseded during tool execution. Aborting."
                    )
                    raise asyncio.CancelledError(
                        f"Thread {ctx.thread_key} turn was superseded during tool execution."
                    )

                is_paused = bool(getattr(ctx.output, "waiting_hitl", False) or getattr(ctx.output, "halted", False))
                follow_up_kwargs = build_completion_kwargs(
                    model=ctx.agent_cfg.get("model", ""),
                    messages=ctx.messages,
                    tools=None if is_paused else (active_tools if active_tools else None),
                    tool_choice=None if is_paused else ("auto" if active_tools else None),
                )

                follow_up = await exec_retry_fn(
                    litellm.acompletion,
                    follow_up_kwargs,
                    agent_key=f"{ctx.agent_key} follow-up",
                    thread_key=ctx.thread_key,
                    is_superseded_fn=ctx.is_superseded,
                    max_attempts=max(1, int(system_settings_manager.get_tool_call_num_retries()) + 1),
                    allow_503_immediate_retry=allow_503_immediate,
                    turn_token_mapping=ctx.turn_token_mapping,
                )
                if follow_up is None:
                    if ctx.is_superseded():
                        raise asyncio.CancelledError(f"Thread {ctx.thread_key} turn was superseded during follow-up.")
                    tool_calls = None
                    break

                ctx.add_usage(follow_up)
                if not (hasattr(follow_up, "choices") and follow_up.choices):
                    tool_calls = None
                    break

                response_message = follow_up.choices[0].message
                raw_tool_calls = getattr(response_message, "tool_calls", None)
                tool_calls = raw_tool_calls if isinstance(raw_tool_calls, (list, tuple)) else None

                if is_paused:
                    tool_calls = None
                    self.turn_finalizer.finalize_message(ctx, response_message)
                    break

                if tool_calls and self.is_mid_turn_budget_exceeded(ctx):
                    logger.warning(
                        f"[CIRCUIT BREAKER] Mid-turn tool calling budget exceeded for thread {ctx.thread_key} during iteration {iteration}. Halting tool loop."
                    )
                    self.save_execution_checkpoint(
                        ctx=ctx,
                        pause_reason="circuit_breaker",
                        active_tools=active_tools,
                    )
                    ctx.output.halted = True
                    ctx.output.final_text = (
                        response_message.content
                        or "⚠️ _Thread cost safety limit reached during tool execution. Turn paused._"
                    )
                    tool_calls = None
                    break

            return None

        timed_out_with_evidence = False
        try:
            loop_res = await asyncio.wait_for(_execute_loop(), timeout=timeout_secs)
            if loop_res is not None:
                return loop_res
            self.turn_finalizer.finalize_message(ctx, response_message)
        except TimeoutError:
            logger.warning(f"[ROUTER] Tool calling loop timed out after {timeout_secs}s for {ctx.agent_key}.")
            ensure_all_tool_calls_responded(ctx)
            if substantive_tool_outputs_count > 0 and not (
                ctx.output.final_text and str(ctx.output.final_text).strip()
            ):
                timed_out_with_evidence = True
            elif not ctx.output.final_text:
                ctx.output.final_text = (
                    response_message.content
                    if (response_message and response_message.content)
                    else "⚠️ _Tool execution timed out. Continuing discussion._"
                )

        return await self.turn_finalizer.synthesize_if_needed(
            ctx,
            response_message=response_message,
            tool_calls=tool_calls,
            timed_out_with_evidence=timed_out_with_evidence,
            iteration=iteration,
            max_tool_iterations=max_tool_iterations,
            allow_503_immediate=allow_503_immediate,
            on_status_update=on_status_update,
        )

