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
from core.config import MAX_DISCUSSION_TURNS
from core.llm_utils import execute_llm_with_retry
from services.slack_client import (
    fetch_thread_files,
    fetch_thread_history,
    post_agent_response,
    post_system_message,
)

from orchestration.thread_state import thread_state_manager
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
) -> dict[str, Any]:
    """
    Main background handler: parses prompt, builds conversation history,
    calls LiteLLM with agent parameters & MCP tools, and posts response back to Slack thread.
    If run_internal_loop is True, executes the entire multi-agent discussion loop internally.
    """
    if run_internal_loop:
        return await orchestrate_discussion(
            channel_id=channel_id,
            thread_ts=thread_ts,
            raw_text=raw_text,
            bot_user_id=bot_user_id,
            icon_url=icon_url,
        )

    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text=raw_text,
        bot_user_id=bot_user_id,
        icon_url=icon_url,
        is_bot_trigger=is_bot_trigger,
        in_memory_history=in_memory_history,
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
) -> dict[str, Any]:
    """
    Internal multi-agent discussion loop:
    Orchestrates the entire multi-agent discussion chain in-memory without relying on
    Slack Events API webhook bounce-backs.
    """
    if max_turns is None:
        max_turns = MAX_DISCUSSION_TURNS

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

        # Append first agent's post to in-memory history
        if "final_text" in turn_res and "username" in turn_res:
            accumulated_history.append(
                {
                    "role": "assistant",
                    "content": f"[{turn_res['username']}]: {turn_res['final_text']}",
                }
            )

        # 3. Subsequent turns
        active_rounds = thread_state_manager.get_rounds(channel_id, thread_ts)
        round_seq = thread_state_manager.get_round_sequence(channel_id, thread_ts)
        expected_turns = (active_rounds + 1) * max(1, len(round_seq) or 6)
        if expected_turns > max_turns:
            max_turns = expected_turns

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

            if "final_text" in turn_res and "username" in turn_res:
                accumulated_history.append(
                    {
                        "role": "assistant",
                        "content": f"[{turn_res['username']}]: {turn_res['final_text']}",
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
        except Exception as e:
            logger.debug(f"[ORCHESTRATOR] Final flat history sync exception: {e}")

        return last_res
    finally:
        if current_task and thread_state_manager.get_active_task(channel_id, thread_ts) == current_task:
            thread_state_manager.clear_active_task(channel_id, thread_ts, current_task)
