"""
Stage 4: LLM execution, 5-second intermediate status updates, and 60-second tool execution loops.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import os
import time
from typing import TYPE_CHECKING, Any

import litellm

from core.config import TOOL_INTERMEDIATE_UPDATE_DELAY_SECONDS
from orchestration.system_settings import (
    get_tool_call_loop_timeout_seconds,
    system_settings_manager,
)
from core.llm_utils import execute_llm_with_retry
from orchestration.agent_settings import settings_manager
from orchestration.stages.tool_status import _build_tool_status_text
from orchestration.protocols import PipelineDeps
from orchestration.thread_state import thread_state_manager
from services.local_tools import execute_local_tool
from services.mcp.schema_translator import parse_mcp_tool_name
from services.slack import post_agent_response
from skills.loader import LOAD_SKILL_SCHEMA

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


def _is_explicit_meta_provider(model: str | None) -> bool:
    """Returns True only if the model explicitly targets a Meta custom provider endpoint."""
    if not model:
        return False
    m = str(model).lower().strip()
    return m.startswith("meta/") or m.startswith("custom_meta/")


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


def _build_completion_kwargs(
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | None = None,
) -> dict[str, Any]:
    """Builds model-agnostic LiteLLM completion kwargs with optional tools and Meta overrides."""
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
    }
    if tools is not None:
        kwargs["tools"] = tools
    if tool_choice is not None:
        kwargs["tool_choice"] = tool_choice
    if _is_explicit_meta_provider(model):
        if os.getenv("META_API_KEY"):
            kwargs["api_key"] = os.getenv("META_API_KEY")
        if os.getenv("META_API_BASE"):
            kwargs["api_base"] = os.getenv("META_API_BASE")
    return kwargs


def _unlock_skill_tools(
    *,
    skill_def: Any,
    ctx: "TurnContext",
    active_tools: list[dict[str, Any]],
) -> None:
    """Appends schemas unlocked by load_skill, scoping control_plane nominations to non-self peers."""
    if skill_def.name == "control_plane":
        from skills.builtin.control_plane.tools import build_control_plane_tool_schemas

        eligible_peers: list[str] = []
        seen_keys: set[str] = set()
        raw_candidates = (
            []
            if getattr(ctx, "force_termination", False)
            else list(getattr(ctx, "eligible_remaining_agents", None) or [])
        )
        for peer_key in raw_candidates:
            if not isinstance(peer_key, str) or peer_key == ctx.agent_key or peer_key in seen_keys:
                continue
            seen_keys.add(peer_key)
            agent_cfg = settings_manager.get_agent(peer_key)
            label = agent_cfg.get("username", peer_key) if isinstance(agent_cfg, dict) else peer_key
            eligible_peers.append(label)

        candidate_schemas = build_control_plane_tool_schemas(
            eligible_agents=eligible_peers or None,
        )
        if not eligible_peers:
            candidate_schemas = [
                s for s in candidate_schemas
                if s.get("function", {}).get("name") != "nominate_next_speaker"
            ]
    else:
        candidate_schemas = skill_def.get_tool_schemas()

    existing_names = {
        s.get("function", {}).get("name")
        for s in active_tools
        if isinstance(s, dict) and "function" in s
    }
    for s_schema in candidate_schemas:
        s_name = s_schema.get("function", {}).get("name")
        if s_name == "signal_consensus_convergence":
            from orchestration.app_settings import is_convergence_fast_forward_enabled

            if not is_convergence_fast_forward_enabled():
                continue
        if s_name:
            if s_name not in existing_names:
                active_tools.append(s_schema)
                existing_names.add(s_name)
        elif s_schema not in active_tools:
            active_tools.append(s_schema)


def _is_tool_error(result: Any) -> bool:
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


def _ensure_all_tool_calls_responded(ctx: "TurnContext") -> None:
    """Appends synthetic aborted responses for any unresponded assistant tool_calls in ctx.messages."""
    responded_ids = {
        m.get("tool_call_id") for m in ctx.messages if isinstance(m, dict) and m.get("role") == "tool"
    }
    for msg in list(ctx.messages):
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
            if not tc_id or tc_id in responded_ids:
                continue
            fn_obj = tc.get("function", {}) if isinstance(tc, dict) else getattr(tc, "function", None)
            tc_fn = fn_obj.get("name", "tool") if isinstance(fn_obj, dict) else getattr(fn_obj, "name", "tool")
            ctx.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "name": tc_fn,
                    "content": (
                        "<untrusted_file_content>\n"
                        f"TOOL EXECUTION ABORTED: Tool '{tc_fn}' was skipped because the tool loop timeout expired. "
                        "Synthesize your final response using the evidence already gathered above.\n"
                        "</untrusted_file_content>"
                    ),
                }
            )
            responded_ids.add(tc_id)


class ExecutionStage:
    """Provides stage 4 LLM execution and tool calling loops for TurnPipeline."""

    def __init__(self, deps: PipelineDeps):
        self.deps = deps

    @property
    def skill_registry(self) -> Any:
        reg = getattr(self.deps, "skill_registry", None)
        if reg is None:
            from skills.loader import get_skill_registry

            reg = get_skill_registry()
        return reg

    @property
    def spend_governance(self) -> Any:
        sg = getattr(self.deps, "spend_governance", None)
        if sg is None:
            from pipeline.spend_governance import spend_governance as default_sg

            sg = default_sg
        return sg

    def _is_mid_turn_budget_exceeded(self, ctx: TurnContext) -> bool:
        """
        Calculates thread cost plus current turn's accumulated token usage,
        and checks if the circuit breaker limit or daily budget is exceeded.
        """
        try:
            from agents.usage_agent import calculate_thread_costs
            from orchestration.app_settings import (
                get_circuit_breaker_threshold,
                get_daily_budget_usd,
                is_circuit_breaker_enabled,
            )
            from services.pricing_service import pricing_service

            model = ctx.agent_cfg.get("model", "") if ctx.agent_cfg else ""
            prompt_tokens = getattr(ctx.output, "total_prompt_tokens", 0) or 0
            completion_tokens = getattr(ctx.output, "total_completion_tokens", 0) or 0
            custom_calc_fn = getattr(self.deps, "calc_costs_fn", None)
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
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[EXECUTION] Error checking mid-turn budget: {e}")
            return False

    def _save_execution_checkpoint(
        self,
        ctx: TurnContext,
        pause_reason: str,
        active_tools: list[dict[str, Any]] | None = None,
        pending_tool_call: dict[str, Any] | None = None,
        hitl_request_id: str | None = None,
    ) -> str:
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
        except Exception as e:  # noqa: BLE001
            logger.error(f"[EXECUTION] Failed to save {pause_reason} checkpoint: {e}")
            return ""

    async def stage_execute_llm(self, ctx: TurnContext) -> dict[str, Any] | None:
        """Stage 4: Execute model completion and handle tool calling loop."""
        if ctx.is_superseded():
            logger.info(f"[ROUTER] Thread {ctx.thread_key} turn was superseded before LLM call. Aborting.")
            return {"ok": False, "cancelled": True, "reason": "superseded"}

        already_503_errored = thread_state_manager.has_agent_503_errored(ctx.input.channel_id, ctx.input.thread_ts, ctx.agent_key)
        allow_503_immediate = already_503_errored

        logger.info(
            f"[LLM] Dispatching litellm.completion for {ctx.agent_key} (already_503_errored={already_503_errored})..."
        )
        llm_start = time.time()
        skill_registry = self.skill_registry
        # Resolve initial tools via Skills Framework (Progressive Disclosure)
        agent_skills = ctx.agent_cfg.get("skills")
        if agent_skills is None:
            agent_skills = settings_manager.get_agent_skills(ctx.agent_key)

        from orchestration.app_settings import app_settings_manager
        from orchestration.thread_skills_manager import thread_skills_manager
        from core.config import is_slack_admin

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

        # Deterministic short-circuit for pure-skill turns when agent lacks required skills/authorization
        # and cannot fit/perform downstream debate (no prior verified output or tight max_words < 60).
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
                + thread_skills_manager.get_expired_agent_skills(ctx.input.channel_id, ctx.input.thread_ts, ctx.agent_key)
            )
        )
        thread_ttl_hours = app_settings_manager.get_thread_skill_expiration_time()

        # If agent has no authorized skills (pure debate agent), omit tools entirely
        # For skilled agents, start strictly with LOAD_SKILL_SCHEMA (progressive disclosure)
        active_tools: list[dict[str, Any]] = []
        if agent_skills:
            active_tools = [LOAD_SKILL_SCHEMA]
        completion_kwargs = _build_completion_kwargs(
            model=ctx.agent_cfg["model"],
            messages=ctx.messages,
            tools=active_tools if agent_skills else None,
            tool_choice="auto" if agent_skills else None,
        )

        exec_retry_fn = getattr(self.deps, "llm_executor", None) or execute_llm_with_retry
        intermediate_delay_secs = getattr(self.deps, "intermediate_delay_secs", None)
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
                ws_adapter = getattr(self.deps, "workspace_adapter", None)
                if ws_adapter is not None:
                    await ws_adapter.update_agent_turn(
                        channel_id=ctx.input.channel_id,
                        message_ts=ctx.intermediate_msg_ts,
                        text=new_text,
                    )
                else:
                    update_fn = getattr(self.deps, "update_message_fn", None)
                    if update_fn is not None:
                        await update_fn(
                            channel_id=ctx.input.channel_id,
                            ts=ctx.intermediate_msg_ts,
                            text=new_text,
                        )
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[ROUTER] Failed to update intermediate status in-place: {e}")

        async def _delayed_intermediate_post() -> None:
            nonlocal last_status_text
            try:
                await asyncio.sleep(intermediate_delay_secs)
                if not ctx.is_superseded() and not ctx.intermediate_msg_ts:
                    status_text = _build_tool_status_text(username, current_status_tool_calls or [])
                    last_status_text = status_text
                    post_resp_fn = getattr(self.deps, "slack_poster", None) or post_agent_response
                    logger.info(
                        f"[ROUTER] Turn delay reached ({intermediate_delay_secs}s). Posting intermediate status for {username}."
                    )
                    res = await post_resp_fn(
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
            except Exception as e:  # noqa: BLE001 - Error boundary prevents background Slack status post failure from interrupting execution
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
                ctx.output.final_text = response_message.content or ""
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
            with contextlib.suppress(asyncio.CancelledError, Exception):
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
        """
        Shared Stage-4 / Checkpoint-Resume tool execution loop enforcing authorization,
        load_skill schema unlocking, HITL approval & checkpoint saving, control-plane error
        guards, evidence caching, mid-turn circuit breakers, timeouts, and final synthesis.
        """
        skill_registry = self.skill_registry
        exec_retry_fn = getattr(self.deps, "llm_executor", None) or execute_llm_with_retry
        custom_tool_executor = getattr(self.deps, "tool_executor", None)
        max_tool_iterations = getattr(self.deps, "max_tool_iterations", None)
        if max_tool_iterations is None:
            max_tool_iterations = system_settings_manager.get_max_tool_iterations()
        timeout_secs = getattr(self.deps, "timeout_secs", None)
        if timeout_secs is None:
            timeout_secs = get_tool_call_loop_timeout_seconds()

        response_message = initial_response_message
        if response_message is None:
            follow_up_kwargs = _build_completion_kwargs(
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
            ctx.output.final_text = response_message.content or ctx.output.final_text or ""
            return None

        iteration = 0
        substantive_tool_outputs_count = 0

        async def _run_tool_calling_loop():
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
                        tool_result_str = f"<untrusted_file_content>\nTOOL EXECUTION ERROR: Invalid JSON arguments: {decode_err}\n</untrusted_file_content>"
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
                        # Dynamically unlock tools declared by this skill only if authorized
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
                                except Exception as e:  # noqa: BLE001 - Best-effort on-demand MCP discovery
                                    logger.warning(f"[EXECUTION] Failed on-demand MCP discovery for '{skill_def.mcp_server}': {e}")

                            _unlock_skill_tools(
                                skill_def=skill_def,
                                ctx=ctx,
                                active_tools=active_tools,
                            )
                    else:
                        # 1. First verify tool authorization before cache or execution
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

                            # 2. Next check HITL approval
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
                                        f"<untrusted_file_content>\n"
                                        f"TOOL EXECUTION PAUSED: Tool '{fn_name}' requires human approval. "
                                        f"An approval request has been sent to the Slack thread. "
                                        f"Your turn is paused now. Explain briefly what approval is required and end your message without nominating other agents. "
                                        f"Do NOT retry calling this tool until approval is granted."
                                        f"\n</untrusted_file_content>"
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

                                    # TKT-904: Synthesize responses for remaining sibling tool calls in batch
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

                                    self._save_execution_checkpoint(
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

                            # 3. Only check evidence cache after Authorization and HITL pass
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
                                # 4. Low-level execution via custom executor hook or skill_registry
                                if custom_tool_executor is not None and custom_tool_executor is not execute_local_tool:
                                    sig = inspect.signature(custom_tool_executor)
                                    kwargs = {}
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
                    # Track skill used
                    if fn_name == "load_skill":
                        s_name = fn_args.get("skill_name", "").strip()
                    else:
                        s_name = skill_registry.find_skill_for_tool(fn_name) or fn_name
                    if s_name and not _is_tool_error(tool_result):
                        if s_name not in ctx.output.skills_used:
                            ctx.output.skills_used.append(s_name)

                    # Handle explicit control plane tools (guarded against denied/errored executions - C6)
                    if fn_name == "nominate_next_speaker" and not _is_tool_error(tool_result):
                        from pipeline.entity_resolver import entity_resolver

                        nom_agent = fn_args.get("agent_name")
                        nom_str = str(nom_agent).strip() if nom_agent else ""
                        resolved_nom = entity_resolver.resolve_agent_name(nom_str) or nom_str
                        if nom_str and resolved_nom != ctx.agent_key:
                            ctx.control.nomination = nom_str
                            ctx.control.nomination_rationale = str(fn_args.get("rationale", "")).strip()
                    elif fn_name == "request_user_clarification" and not _is_tool_error(tool_result):
                        ctx.control.clarification_requested = True
                        ctx.control.clarification_question = str(fn_args.get("question", "")).strip()
                        ctx.output.halted = True
                    elif fn_name == "signal_consensus_convergence" and not _is_tool_error(tool_result):
                        ctx.control.consensus_signaled = True
                        ctx.control.consensus_summary = str(fn_args.get("summary_of_agreement", "")).strip()
                        thread_state_manager.set_consensus_reached(ctx.input.channel_id, ctx.input.thread_ts, True)

                    from core.security_utils import wrap_untrusted_content

                    tool_result_str = wrap_untrusted_content(str(tool_result))

                    if cached_evidence is None and not getattr(ctx.output, "halted", False):
                        res_str = tool_result if isinstance(tool_result, str) else str(tool_result)
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
                follow_up_kwargs = _build_completion_kwargs(
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
                    ctx.output.final_text = response_message.content or ctx.output.final_text or ""
                    break

                if tool_calls and self._is_mid_turn_budget_exceeded(ctx):
                    logger.warning(
                        f"[CIRCUIT BREAKER] Mid-turn tool calling budget exceeded for thread {ctx.thread_key} during iteration {iteration}. Halting tool loop."
                    )
                    self._save_execution_checkpoint(
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
            loop_res = await asyncio.wait_for(_run_tool_calling_loop(), timeout=timeout_secs)
            if loop_res is not None:
                return loop_res
            ctx.output.final_text = response_message.content or ctx.output.final_text or ""
        except asyncio.TimeoutError:
            logger.warning(f"[ROUTER] Tool calling loop timed out after {timeout_secs}s for {ctx.agent_key}.")
            _ensure_all_tool_calls_responded(ctx)
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

        # Execute final tool-free synthesis outside the tool-loop timeout so synthesis is never killed by timeout_secs.
        needs_final_synthesis = (
            (tool_calls and not (response_message.content and str(response_message.content).strip()))
            or timed_out_with_evidence
        ) and (
            not getattr(ctx.output, "waiting_hitl", False)
            and not getattr(ctx.output, "halted", False)
            and not ctx.is_superseded()
        )
        if needs_final_synthesis:
            _ensure_all_tool_calls_responded(ctx)
            if on_status_update is not None:
                await on_status_update(None)
            logger.info(
                f"[ROUTER] Running final tool-free synthesis call for {ctx.agent_key} "
                f"(iteration={iteration}/{max_tool_iterations}, timed_out_with_evidence={timed_out_with_evidence})."
            )
            ctx.messages.append(
                {
                    "role": "user",
                    "content": (
                        "Maximum tool call iterations reached. Do NOT request any further tools. "
                        "Provide your complete final response now using only the gathered context and tool outputs above."
                    ),
                }
            )
            synthesis_kwargs = _build_completion_kwargs(
                model=ctx.agent_cfg.get("model", ""),
                messages=ctx.messages,
            )
            synthesis_res = await exec_retry_fn(
                litellm.acompletion,
                synthesis_kwargs,
                agent_key=f"{ctx.agent_key} synthesis",
                thread_key=ctx.thread_key,
                is_superseded_fn=ctx.is_superseded,
                max_attempts=max(1, int(system_settings_manager.get_tool_call_num_retries()) + 1),
                allow_503_immediate_retry=allow_503_immediate,
                turn_token_mapping=ctx.turn_token_mapping,
            )
            if synthesis_res is None and ctx.is_superseded():
                return {"ok": False, "cancelled": True, "reason": "superseded"}
            if synthesis_res is not None and hasattr(synthesis_res, "choices") and synthesis_res.choices:
                ctx.add_usage(synthesis_res)
                response_message = synthesis_res.choices[0].message
                ctx.output.final_text = response_message.content or ctx.output.final_text or ""
            if not ctx.output.final_text and timed_out_with_evidence:
                ctx.output.final_text = "⚠️ _Tool execution timed out. Continuing discussion._"

        return None
