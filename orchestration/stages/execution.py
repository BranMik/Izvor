"""
Stage 4: LLM execution, 5-second intermediate status updates, and 60-second tool execution loops.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
from typing import TYPE_CHECKING, Any

import litellm
from starlette.concurrency import run_in_threadpool

from agents import record_thread_tokens
from core.config import TOOL_INTERMEDIATE_UPDATE_DELAY_SECONDS
from orchestration.system_settings import get_tool_call_loop_timeout_seconds
from core.llm_utils import execute_llm_with_retry
from orchestration.agent_settings import settings_manager
from orchestration.stages.tool_status import _build_tool_status_text
from orchestration.thread_state import thread_state_manager
from services.mcp_tools import execute_mcp_tool
from services.slack_client import post_agent_response
from skills import LOAD_SKILL_SCHEMA, skill_registry

if TYPE_CHECKING:
    from orchestration.turn_pipeline import TurnContext

logger = logging.getLogger(__name__)


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
    from services.rag.pii_masker import pii_masker
    if isinstance(val, str):
        return pii_masker.unmask(val, mapping)
    elif isinstance(val, dict):
        return {k: _unmask_argument_values(v, mapping) for k, v in val.items()}
    elif isinstance(val, list):
        return [_unmask_argument_values(item, mapping) for item in val]
    return val



class ExecutionStage:
    def __init__(self, pipeline: Any):
        self.pipeline = pipeline
    """Provides stage 4 LLM execution and tool calling loops for TurnPipeline."""



    async def stage_execute_llm(self, ctx: TurnContext) -> dict[str, Any] | None:
        """Stage 4: Execute model completion and handle tool calling loop."""
        if ctx.is_superseded():
            logger.info(
                f"[ROUTER] Thread {ctx.thread_key} turn was superseded before LLM call. Aborting."
            )
            return {"ok": False, "cancelled": True, "reason": "superseded"}

        already_503_errored = thread_state_manager.has_agent_503_errored(
            ctx.channel_id, ctx.thread_ts, ctx.agent_key
        )
        allow_503_immediate = already_503_errored

        logger.info(
            f"[LLM] Dispatching litellm.completion for {ctx.agent_key} (already_503_errored={already_503_errored})..."
        )
        llm_start = time.time()
        # Resolve initial tools via Skills Framework (Progressive Disclosure)
        agent_skills = ctx.agent_cfg.get("skills")
        if agent_skills is None:
            agent_skills = settings_manager.get_agent_skills(ctx.agent_key)

        from orchestration.app_settings import app_settings_manager
        from orchestration.thread_skills_manager import thread_skills_manager
        global_skills = skill_registry.get_global_skills()
        active_thread_skills = thread_skills_manager.get_active_skills(ctx.channel_id, ctx.thread_ts)
        active_agent_skills = thread_skills_manager.get_active_agent_skills(ctx.channel_id, ctx.thread_ts, ctx.agent_key)
        combined_extra = list(dict.fromkeys(global_skills + active_thread_skills + active_agent_skills))
        if combined_extra:
            merged = list(agent_skills or [])
            for s in combined_extra:
                if s not in merged:
                    merged.append(s)
            agent_skills = merged

        expired_thread_skills = list(
            dict.fromkeys(
                thread_skills_manager.get_expired_skills(ctx.channel_id, ctx.thread_ts)
                + thread_skills_manager.get_expired_agent_skills(ctx.channel_id, ctx.thread_ts, ctx.agent_key)
            )
        )
        thread_ttl_hours = app_settings_manager.get_thread_skill_expiration_time()

        completion_kwargs: dict[str, Any] = {
            "model": ctx.agent_cfg["model"],
            "messages": ctx.messages,
            "num_retries": 2,
        }


        # If agent has no authorized skills (pure debate agent), omit tools entirely
        # For skilled agents, start strictly with LOAD_SKILL_SCHEMA (progressive disclosure)
        active_tools: list[dict[str, Any]] = []
        if agent_skills:
            active_tools = [LOAD_SKILL_SCHEMA]
            completion_kwargs["tools"] = active_tools
            completion_kwargs["tool_choice"] = "auto"
        if _is_explicit_meta_provider(ctx.agent_cfg.get("model")):
            if os.getenv("META_API_KEY"):
                completion_kwargs["api_key"] = os.getenv("META_API_KEY")
            if os.getenv("META_API_BASE"):
                completion_kwargs["api_base"] = os.getenv("META_API_BASE")

        exec_retry_fn = self.pipeline._get_hook(
            "execute_llm_with_retry", execute_llm_with_retry
        )
        threadpool_fn = self.pipeline._get_hook("run_in_threadpool", None)
        custom_runner = (
            threadpool_fn
            if (threadpool_fn is not None and threadpool_fn is not run_in_threadpool)
            else None
        )

        response = await exec_retry_fn(
            litellm.acompletion,
            completion_kwargs,
            agent_key=ctx.agent_key,
            thread_key=ctx.thread_key,
            is_superseded_fn=ctx.is_superseded,
            run_in_threadpool_fn=custom_runner,
            allow_503_immediate_retry=allow_503_immediate,
        )

        if response is None and ctx.is_superseded():
            return {"ok": False, "cancelled": True, "reason": "superseded"}

        ctx.add_usage(response)
        llm_duration = time.time() - llm_start
        logger.info(
            f"[LLM] Received response for {ctx.agent_key} in {llm_duration:.2f} seconds."
        )

        if ctx.is_superseded():
            logger.info(
                f"[ROUTER] Thread {ctx.thread_key} turn was superseded after LLM call. Aborting."
            )
            if ctx.total_prompt_tokens > 0 or ctx.total_completion_tokens > 0:
                record_tokens_fn = self.pipeline._get_hook(
                    "record_thread_tokens", record_thread_tokens
                )
                if record_tokens_fn:
                    record_tokens_fn(
                        channel_id=ctx.channel_id,
                        thread_ts=ctx.thread_ts,
                        model_name=ctx.agent_cfg.get("model", "") if ctx.agent_cfg else "",
                        prompt_tokens=ctx.total_prompt_tokens,
                        completion_tokens=ctx.total_completion_tokens,
                    )
            return {"ok": False, "cancelled": True, "reason": "superseded"}

        response_message = response.choices[0].message
        tool_calls = getattr(response_message, "tool_calls", None)

        if tool_calls:
            exec_mcp_fn = self.pipeline._get_hook("execute_mcp_tool", None)
            max_tool_iterations = 3
            iteration = 0
            timeout_secs = self.pipeline._get_hook(
                "TOOL_CALL_LOOP_TIMEOUT_SECONDS", get_tool_call_loop_timeout_seconds()
            )
            intermediate_delay_secs = self.pipeline._get_hook(
                "TOOL_INTERMEDIATE_UPDATE_DELAY_SECONDS",
                TOOL_INTERMEDIATE_UPDATE_DELAY_SECONDS,
            )

            # Start delayed intermediate status post task
            async def _delayed_intermediate_post():
                try:
                    await asyncio.sleep(intermediate_delay_secs)
                    if not ctx.is_superseded() and not ctx.intermediate_msg_ts:
                        username = ctx.agent_cfg.get("username", ctx.agent_key)
                        resolved_icon = ctx.icon_url or ctx.agent_cfg.get("icon_url")
                        status_text = _build_tool_status_text(username, tool_calls)
                        post_resp_fn = self.pipeline._get_hook("post_agent_response", post_agent_response)
                        logger.info(
                            f"[ROUTER] Tool delay reached ({intermediate_delay_secs}s). Posting intermediate status for {username}."
                        )
                        res = await post_resp_fn(
                            channel_id=ctx.channel_id,
                            thread_ts=ctx.thread_ts,
                            text=status_text,
                            username=username,
                            icon_url=resolved_icon,
                        )
                        if isinstance(res, dict) and res.get("ts"):
                            ctx.intermediate_msg_ts = res["ts"]
                except asyncio.CancelledError:
                    pass
                except Exception as e:  # noqa: BLE001 - Error boundary prevents background Slack status post failure from interrupting tool execution
                    logger.warning(f"[ROUTER] Failed to post intermediate tool status: {e}")

            delay_task = asyncio.create_task(_delayed_intermediate_post())

            async def _run_tool_calling_loop():
                nonlocal iteration, response_message, tool_calls
                while tool_calls and iteration < max_tool_iterations:
                    iteration += 1
                    ctx.messages.append(response_message.model_dump())

                    for tool_call in tool_calls:
                        fn_name = tool_call.function.name
                        try:
                            raw_args = (
                                json.loads(tool_call.function.arguments)
                                if isinstance(tool_call.function.arguments, str)
                                else tool_call.function.arguments
                            )
                        except (json.JSONDecodeError, TypeError) as decode_err:
                            logger.warning(
                                f"[TOOL CALL] Malformed JSON in arguments for {fn_name}: {decode_err}"
                            )
                            tool_result_str = f"<untrusted_file_content>\nTOOL EXECUTION ERROR: Invalid JSON arguments: {decode_err}\n</untrusted_file_content>"
                            ctx.messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "name": fn_name,
                                "content": tool_result_str,
                            })
                            continue
                        fn_args = _unmask_argument_values(
                            raw_args, getattr(ctx, "turn_token_mapping", {})
                        )
                        if fn_name == "load_skill":
                            tool_result = skill_registry.execute_tool(
                                fn_name,
                                fn_args,
                                authorized_skills=agent_skills,
                                expired_skills=expired_thread_skills,
                                ttl_hours=thread_ttl_hours,
                            )
                            # Dynamically unlock tools declared by this skill only if authorized
                            skill_name = fn_args.get("skill_name", "").strip()
                            skill_def = skill_registry.get(skill_name)
                            is_globally_allowed = bool(skill_def and skill_def.is_global)
                            if skill_def and ((agent_skills and skill_name in agent_skills) or is_globally_allowed):
                                existing_names = {
                                    s.get("function", {}).get("name")
                                    for s in active_tools
                                    if isinstance(s, dict) and "function" in s
                                }
                                for s_schema in skill_def.get_tool_schemas():
                                    s_name = s_schema.get("function", {}).get("name")
                                    if s_name:
                                        if s_name not in existing_names:
                                            active_tools.append(s_schema)
                                            existing_names.add(s_name)
                                    elif s_schema not in active_tools:
                                        active_tools.append(s_schema)
                        elif exec_mcp_fn is not None and exec_mcp_fn is not execute_mcp_tool:
                            sig = inspect.signature(exec_mcp_fn)
                            kwargs = {"agent_name": ctx.agent_key} if "agent_name" in sig.parameters else {}
                            if inspect.iscoroutinefunction(exec_mcp_fn):
                                tool_result = await exec_mcp_fn(fn_name, fn_args, **kwargs)
                            else:
                                tool_result = await asyncio.to_thread(exec_mcp_fn, fn_name, fn_args, **kwargs)
                        else:
                            s_name = skill_registry.find_skill_for_tool(fn_name)
                            skill_def = skill_registry.get(s_name) if s_name else None
                            
                            if skill_def and skill_def.requires_human_ITL:
                                from services.hitl import consume_tool_approval, request_human_approval
                                if not consume_tool_approval(ctx.channel_id, ctx.thread_ts, fn_name, fn_args):
                                    await request_human_approval(
                                        ctx.channel_id, ctx.thread_ts, ctx.agent_key,
                                        skill_def.name, fn_name, fn_args,
                                        initiator_user_id=ctx.user_id,
                                    )
                                    tool_result_str = f"<untrusted_file_content>\nTOOL EXECUTION PAUSED: Tool '{fn_name}' requires human approval. I have sent an approval request to the Slack thread. End your turn now and do NOT retry calling this tool until the human approves it.\n</untrusted_file_content>"
                                    ctx.messages.append({
                                        "role": "tool",
                                        "tool_call_id": tool_call.id,
                                        "name": fn_name,
                                        "content": tool_result_str,
                                    })
                                    if s_name and s_name not in ctx.skills_used:
                                        ctx.skills_used.append(s_name)
                                    continue
                            
                            tool_result = await asyncio.to_thread(
                                skill_registry.execute_tool,
                                fn_name,
                                fn_args,
                                authorized_skills=agent_skills,
                                expired_skills=expired_thread_skills,
                                ttl_hours=thread_ttl_hours,
                                agent_name=ctx.agent_key,
                            )
                        # Track skill used
                        if fn_name == "load_skill":
                            s_name = fn_args.get("skill_name", "").strip()
                        else:
                            s_name = skill_registry.find_skill_for_tool(fn_name) or fn_name
                        if s_name and "error" not in str(tool_result):
                            if s_name not in ctx.skills_used:
                                ctx.skills_used.append(s_name)

                        if isinstance(tool_result, str):
                            tool_result_str = f"<untrusted_file_content>\n{tool_result}\n</untrusted_file_content>"
                        else:
                            tool_result_str = tool_result

                        ctx.messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "name": fn_name,
                                "content": tool_result_str,
                            }
                        )

                    if ctx.is_superseded():
                        logger.info(
                            f"[ROUTER] Thread {ctx.thread_key} turn was superseded during tool execution. Aborting."
                        )
                        if ctx.total_prompt_tokens > 0 or ctx.total_completion_tokens > 0:
                            record_tokens_fn = self.pipeline._get_hook(
                                "record_thread_tokens", record_thread_tokens
                            )
                            if record_tokens_fn:
                                record_tokens_fn(
                                    channel_id=ctx.channel_id,
                                    thread_ts=ctx.thread_ts,
                                    model_name=ctx.agent_cfg.get("model", "") if ctx.agent_cfg else "",
                                    prompt_tokens=ctx.total_prompt_tokens,
                                    completion_tokens=ctx.total_completion_tokens,
                                )
                        return {"ok": False, "cancelled": True, "reason": "superseded"}

                    follow_up_kwargs: dict[str, Any] = {
                        "model": ctx.agent_cfg["model"],
                        "messages": ctx.messages,
                        "tools": active_tools,
                        "tool_choice": "auto",
                        "num_retries": 2,
                    }
                    if _is_explicit_meta_provider(ctx.agent_cfg.get("model")):
                        if os.getenv("META_API_KEY"):
                            follow_up_kwargs["api_key"] = os.getenv("META_API_KEY")
                        if os.getenv("META_API_BASE"):
                            follow_up_kwargs["api_base"] = os.getenv("META_API_BASE")

                    follow_up = await exec_retry_fn(
                        litellm.acompletion,
                        follow_up_kwargs,
                        agent_key=f"{ctx.agent_key} follow-up",
                        thread_key=ctx.thread_key,
                        is_superseded_fn=ctx.is_superseded,
                        run_in_threadpool_fn=custom_runner,
                        allow_503_immediate_retry=allow_503_immediate,
                    )
                    if follow_up is None and ctx.is_superseded():
                        if ctx.total_prompt_tokens > 0 or ctx.total_completion_tokens > 0:
                            record_tokens_fn = self.pipeline._get_hook(
                                "record_thread_tokens", record_thread_tokens
                            )
                            if record_tokens_fn:
                                record_tokens_fn(
                                    channel_id=ctx.channel_id,
                                    thread_ts=ctx.thread_ts,
                                    model_name=ctx.agent_cfg.get("model", "") if ctx.agent_cfg else "",
                                    prompt_tokens=ctx.total_prompt_tokens,
                                    completion_tokens=ctx.total_completion_tokens,
                                )
                        return {"ok": False, "cancelled": True, "reason": "superseded"}

                    ctx.add_usage(follow_up)
                    response_message = follow_up.choices[0].message
                    tool_calls = getattr(response_message, "tool_calls", None)

                return None

            try:
                loop_res = await asyncio.wait_for(
                    _run_tool_calling_loop(), timeout=timeout_secs
                )
                if loop_res is not None:
                    return loop_res
                ctx.final_text = response_message.content or ctx.final_text or ""
            except asyncio.TimeoutError:
                logger.warning(
                    f"[ROUTER] Tool calling loop timed out after {timeout_secs}s for {ctx.agent_key}."
                )
                if not ctx.final_text:
                    ctx.final_text = (
                        response_message.content
                        if (response_message and response_message.content)
                        else "⚠️ _Tool execution timed out. Continuing discussion._"
                    )
            finally:
                if not delay_task.done():
                    delay_task.cancel()
        else:
            ctx.final_text = response_message.content or ""

        return None
