import asyncio
import logging
import sys
from typing import Any

import litellm
from starlette.concurrency import run_in_threadpool

from agents import (
    calculate_thread_costs,
    record_thread_tokens,
    trigger_thread_notes_update,
    trigger_usage_update,
)
from core.config import MAX_DISCUSSION_TURNS, get_max_discussion_turns
from core.llm_utils import execute_llm_with_retry
from orchestration.thread_state import thread_state_manager
from services.slack_client import (
    fetch_thread_files,
    fetch_thread_history,
    post_agent_response,
    post_system_message,
)

from .turn_pipeline import TurnContext, TurnPipeline

__all__ = [
    "orchestrate_discussion",
    "post_system_message",
    "process_and_respond",
    "run_in_threadpool",
]

logger = logging.getLogger(__name__)

# Turn off verbose LiteLLM logging unless debugging
litellm.suppress_debug_info = True
litellm.drop_params = True


async def process_and_respond(
    channel_id: str,
    thread_ts: str,
    raw_text: str,
    bot_user_id: str | None = None,
    icon_url: str | None = None,
    is_bot_trigger: bool = False,
    run_internal_loop: bool = False,
    in_memory_history: list[dict[str, Any]] | None = None,
    user_id: str | None = None,
    target_agent_override: str | None = None,
) -> dict[str, Any]:
    """
    Main background handler: parses prompt, builds conversation history,
    calls LiteLLM with agent parameters & MCP tools, and posts response back to Slack thread.
    If run_internal_loop is True, executes the entire multi-agent discussion loop internally.
    """
    # Guardrail Check for user inputs
    if not is_bot_trigger:
        try:
            from services.security.guardrail import guardrail_service
            is_injection = await guardrail_service.is_prompt_injection(raw_text)
            if is_injection:
                logger.warning(f"[GUARDRAIL] Blocked malicious request in {channel_id}")
                await post_system_message(
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    text="⚠️ *Security Guardrail Triggered*\nThis request violates safety policies and has been blocked.",
                )
                thread_state_manager.request_stop(channel_id, thread_ts)
                return {"status": "blocked_by_guardrail"}
        except Exception as e:
            logger.error(f"[GUARDRAIL] Check failed, allowing request: {e}")

    if run_internal_loop:
        return await orchestrate_discussion(
            channel_id=channel_id,
            thread_ts=thread_ts,
            raw_text=raw_text,
            bot_user_id=bot_user_id,
            icon_url=icon_url,
            user_id=user_id,
        )

    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text=raw_text,
        user_id=user_id,
        bot_user_id=bot_user_id,
        icon_url=icon_url,
        is_bot_trigger=is_bot_trigger,
        in_memory_history=in_memory_history,
        target_agent_override=target_agent_override,
    )
    pipeline = TurnPipeline(
        router_module=sys.modules[__name__],
        llm_executor=execute_llm_with_retry,
        slack_poster=post_agent_response,
        history_fetcher=fetch_thread_history,
        files_fetcher=fetch_thread_files,
        token_recorder=record_thread_tokens,
        notes_updater=trigger_thread_notes_update,
        usage_updater=trigger_usage_update,
        calc_costs_fn=calculate_thread_costs,
    )
    return await pipeline.run(ctx)


async def orchestrate_discussion(
    channel_id: str,
    thread_ts: str,
    raw_text: str,
    bot_user_id: str | None = None,
    icon_url: str | None = None,
    max_turns: int | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """
    Internal multi-agent discussion loop:
    Orchestrates the entire multi-agent discussion chain in-memory without relying on
    Slack Events API webhook bounce-backs.
    """
    turn_ceiling = get_max_discussion_turns()
    if max_turns is None:
        max_turns = turn_ceiling
    else:
        max_turns = min(max_turns, turn_ceiling)

    thread_key = (channel_id, thread_ts)
    logger.info(
        f"\n[ORCHESTRATOR] Starting internal discussion loop for thread {thread_key}"
    )

    current_task = asyncio.current_task()
    if current_task:
        thread_state_manager.set_active_task(channel_id, thread_ts, current_task)

    try:
        # 1. Fetch initial thread history from Slack once
        initial_history = await fetch_thread_history(
            channel_id, thread_ts, bot_user_id=bot_user_id
        )
        accumulated_history: list[dict[str, Any]] = list(initial_history)

        # Ensure user prompt is in accumulated_history so subsequent agents see it
        if not accumulated_history or accumulated_history[-1].get("role") != "user" or accumulated_history[-1].get("content") != raw_text:
            accumulated_history.append({"role": "user", "content": raw_text})

        # 2. Run first turn with user prompt
        turn_res = await process_and_respond(
            channel_id=channel_id,
            thread_ts=thread_ts,
            raw_text=raw_text,
            bot_user_id=bot_user_id,
            icon_url=icon_url,
            is_bot_trigger=False,
            run_internal_loop=False,
            in_memory_history=accumulated_history,
            user_id=user_id,
        )

        last_res = turn_res
        if (
            turn_res.get("halted")
            or turn_res.get("cancelled")
            or turn_res.get("is_completed")
            or (not turn_res.get("ok", True) and not turn_res.get("final_text"))
        ):
            logger.info(
                f"[ORCHESTRATOR] Turn 1 completed or ended discussion for thread {thread_key}."
            )
            return turn_res

        if "final_text" in turn_res:
            agent_name = turn_res.get("displayname", turn_res.get("username", "Unknown Agent"))
            accumulated_history.append(
                {
                    "role": "assistant",
                    "content": f"[{agent_name}]: {turn_res['final_text']}",
                }
            )
        if turn_res.get("revisor_warning"):
            accumulated_history.append(
                {
                    "role": "system",
                    "content": f"[Internal Revisor Warning]: {turn_res['revisor_warning']}",
                }
            )

        # Check for Parallel Dialectical Topology (Scatter-Gather) (TKT-811 & TKT-812)
        from core.models import OrchestrationMode
        from orchestration.agent_settings import get_orchestration_mode, get_sandwich_config

        active_mode = get_orchestration_mode()
        if active_mode in (OrchestrationMode.SCATTER_GATHER, "scatter_gather"):
            remaining = thread_state_manager.get_remaining_agents(channel_id, thread_ts)
            sandwich_cfg = get_sandwich_config()
            end_chain = sandwich_cfg.get("end_chain", [])
            scatter_agents = [a for a in remaining if a not in end_chain]

            if scatter_agents:
                logger.info(
                    f"[ORCHESTRATOR] Entering Parallel Scatter Phase for {thread_key}: running {scatter_agents} concurrently."
                )

                async def _run_scatter_branch(agent_k: str) -> dict[str, Any]:
                    # Zero-anchor isolated history snapshot
                    branch_history = list(accumulated_history)
                    return await process_and_respond(
                        channel_id=channel_id,
                        thread_ts=thread_ts,
                        raw_text=raw_text,
                        bot_user_id=bot_user_id,
                        icon_url=icon_url,
                        is_bot_trigger=True,
                        run_internal_loop=False,
                        in_memory_history=branch_history,
                        user_id=user_id,
                        target_agent_override=agent_k,
                    )

                scatter_results = await asyncio.gather(
                    *[_run_scatter_branch(a) for a in scatter_agents],
                    return_exceptions=False,
                )

                for s_res in scatter_results:
                    if isinstance(s_res, dict) and "final_text" in s_res:
                        s_name = s_res.get("displayname", s_res.get("username", "Unknown Agent"))
                        accumulated_history.append({
                            "role": "assistant",
                            "content": f"[{s_name}]: {s_res['final_text']}",
                        })
                    last_res = s_res

                remaining_after = [a for a in remaining if a not in scatter_agents]
                thread_state_manager.set_remaining_agents(channel_id, thread_ts, remaining_after)

                # Gather & Synthesis Phase
                if remaining_after and not thread_state_manager.is_stopped(channel_id, thread_ts):
                    gather_agent = remaining_after[0]
                    logger.info(
                        f"[ORCHESTRATOR] Entering Parallel Gather Phase with synthesis agent {gather_agent}."
                    )
                    gather_res = await process_and_respond(
                        channel_id=channel_id,
                        thread_ts=thread_ts,
                        raw_text="Synthesize the parallel specialist perspectives into a reconciled conclusion.",
                        bot_user_id=bot_user_id,
                        icon_url=icon_url,
                        is_bot_trigger=True,
                        run_internal_loop=False,
                        in_memory_history=accumulated_history,
                        user_id=user_id,
                        target_agent_override=gather_agent,
                    )
                    last_res = gather_res
                    if isinstance(gather_res, dict) and "final_text" in gather_res:
                        g_name = gather_res.get("displayname", gather_res.get("username", "Unknown Agent"))
                        accumulated_history.append({
                            "role": "assistant",
                            "content": f"[{g_name}]: {gather_res['final_text']}",
                        })

                # Discussion completed
                try:
                    from services.flat_history_recorder import record_flat_history

                    init_p = thread_state_manager.get_initial_prompt(channel_id, thread_ts) or raw_text
                    record_flat_history(
                        channel_id=channel_id,
                        thread_ts=thread_ts,
                        initial_prompt=init_p,
                        messages=accumulated_history,
                        bot_user_id=bot_user_id,
                    )
                except Exception as e:
                    logger.debug(f"[ORCHESTRATOR] Flat history sync exception: {e}")

                return last_res

        # 3. Subsequent turns
        active_rounds = thread_state_manager.get_rounds(channel_id, thread_ts)
        round_seq = thread_state_manager.get_round_sequence(channel_id, thread_ts)
        if active_rounds > 0:
            expected_turns = (active_rounds + 1) * max(1, len(round_seq) or 6)
            max_turns = min(get_max_discussion_turns(), expected_turns)
        else:
            max_turns = min(max_turns, get_max_discussion_turns())

        for turn in range(2, max_turns + 1):
            if thread_state_manager.is_stopped(channel_id, thread_ts):
                logger.info(
                    f"[ORCHESTRATOR] Thread {thread_key} stop detected before turn {turn}. Terminating loop."
                )
                thread_state_manager.clear_state(channel_id, thread_ts)
                break

            if turn_res.get("is_completed"):
                logger.info(
                    f"[ORCHESTRATOR] Thread {thread_key} marked completed after turn {turn - 1}. Terminating loop."
                )
                break

            current_prompt = turn_res.get("final_text", "")
            if not current_prompt:
                logger.warning(
                    f"[ORCHESTRATOR] No prompt available from previous turn {turn - 1}. Terminating loop."
                )
                break

            logger.info(
                f"[ORCHESTRATOR] Executing internal turn {turn} for thread {thread_key}"
            )
            turn_res = await process_and_respond(
                channel_id=channel_id,
                thread_ts=thread_ts,
                raw_text=current_prompt,
                bot_user_id=bot_user_id,
                icon_url=icon_url,
                is_bot_trigger=True,
                run_internal_loop=False,
                in_memory_history=accumulated_history,
                user_id=user_id,
            )
            last_res = turn_res

            if (
                turn_res.get("halted")
                or turn_res.get("cancelled")
                or turn_res.get("is_completed")
                or (not turn_res.get("ok", True) and not turn_res.get("final_text"))
            ):
                logger.info(
                    f"[ORCHESTRATOR] Turn {turn} completed or ended discussion for thread {thread_key}."
                )
                break

            if "final_text" in turn_res:
                agent_name = turn_res.get("displayname", turn_res.get("username", "Unknown Agent"))
                accumulated_history.append(
                    {
                        "role": "assistant",
                        "content": f"[{agent_name}]: {turn_res['final_text']}",
                    }
                )
            if turn_res.get("revisor_warning"):
                accumulated_history.append(
                    {
                        "role": "system",
                        "content": f"[Internal Revisor Warning]: {turn_res['revisor_warning']}",
                    }
                )

        try:
            from services.flat_history_recorder import record_flat_history

            init_p = thread_state_manager.get_initial_prompt(channel_id, thread_ts) or raw_text
            record_flat_history(
                channel_id=channel_id,
                thread_ts=thread_ts,
                initial_prompt=init_p,
                messages=accumulated_history,
                bot_user_id=bot_user_id,
            )
        except Exception as e:  # noqa: BLE001 - Error boundary prevents history recording error from failing main response
            logger.debug(f"[ORCHESTRATOR] Final flat history sync exception: {e}")

        return last_res
    except Exception as e:
        logger.exception(
            f"[ORCHESTRATOR] Unhandled exception in discussion loop for thread {thread_key}"
        )
        return {"ok": False, "error": str(e)}
    finally:
        if current_task and thread_state_manager.get_active_task(channel_id, thread_ts) == current_task:
            thread_state_manager.clear_active_task(channel_id, thread_ts, current_task)


async def continue_discussion_from_turn_result(
    channel_id: str,
    thread_ts: str,
    turn_res: dict[str, Any],
    bot_user_id: str | None = None,
    icon_url: str | None = None,
    max_turns: int | None = None,
    user_id: str | None = None,
    accumulated_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Continues multi-agent discussion loop after a turn finishes (e.g. following HITL approval resumption).
    Drives remaining agents until round completion or termination.
    """
    thread_key = (channel_id, thread_ts)
    if turn_res.get("halted") or turn_res.get("cancelled") or turn_res.get("is_completed"):
        return turn_res

    if accumulated_history is None:
        initial_history = await fetch_thread_history(
            channel_id, thread_ts, bot_user_id=bot_user_id
        )
        accumulated_history = list(initial_history)

    if "final_text" in turn_res:
        agent_name = turn_res.get("displayname", turn_res.get("username", "Unknown Agent"))
        accumulated_history.append(
            {
                "role": "assistant",
                "content": f"[{agent_name}]: {turn_res['final_text']}",
            }
        )
    if turn_res.get("revisor_warning"):
        accumulated_history.append(
            {
                "role": "system",
                "content": f"[Internal Revisor Warning]: {turn_res['revisor_warning']}",
            }
        )

    active_rounds = thread_state_manager.get_rounds(channel_id, thread_ts)
    round_seq = thread_state_manager.get_round_sequence(channel_id, thread_ts)
    if active_rounds > 0:
        expected_turns = (active_rounds + 1) * max(1, len(round_seq) or 6)
        max_turns = min(get_max_discussion_turns(), expected_turns)
    else:
        max_turns = min(max_turns or get_max_discussion_turns(), get_max_discussion_turns())

    last_res = turn_res
    turn_num = 2
    while turn_num <= max_turns:
        if thread_state_manager.is_stopped(channel_id, thread_ts):
            logger.info(
                f"[ORCHESTRATOR] Thread {thread_key} stop detected. Terminating loop."
            )
            thread_state_manager.clear_state(channel_id, thread_ts)
            break

        if last_res.get("is_completed"):
            break

        current_prompt = last_res.get("final_text", "")
        if not current_prompt:
            break

        logger.info(
            f"[ORCHESTRATOR] Executing continued internal turn {turn_num} for thread {thread_key}"
        )
        last_res = await process_and_respond(
            channel_id=channel_id,
            thread_ts=thread_ts,
            raw_text=current_prompt,
            bot_user_id=bot_user_id,
            icon_url=icon_url,
            is_bot_trigger=True,
            run_internal_loop=False,
            in_memory_history=accumulated_history,
            user_id=user_id,
        )

        if (
            last_res.get("halted")
            or last_res.get("cancelled")
            or last_res.get("is_completed")
            or (not last_res.get("ok", True) and not last_res.get("final_text"))
        ):
            break

        if "final_text" in last_res:
            agent_name = last_res.get("displayname", last_res.get("username", "Unknown Agent"))
            accumulated_history.append(
                {
                    "role": "assistant",
                    "content": f"[{agent_name}]: {last_res['final_text']}",
                }
            )
        if last_res.get("revisor_warning"):
            accumulated_history.append(
                {
                    "role": "system",
                    "content": f"[Internal Revisor Warning]: {last_res['revisor_warning']}",
                }
            )
        turn_num += 1

    try:
        from services.flat_history_recorder import record_flat_history

        init_p = thread_state_manager.get_initial_prompt(channel_id, thread_ts) or ""
        record_flat_history(
            channel_id=channel_id,
            thread_ts=thread_ts,
            initial_prompt=init_p,
            messages=accumulated_history,
            bot_user_id=bot_user_id,
        )
    except Exception as e:
        logger.debug(f"[ORCHESTRATOR] Flat history sync exception in continue_discussion: {e}")

    return last_res
