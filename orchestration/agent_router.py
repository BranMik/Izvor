import asyncio
import json
import logging
import os
import random
import re
import sys
import time
from typing import Any

import litellm
from starlette.concurrency import run_in_threadpool

from agents import (
    calculate_thread_costs,
    record_thread_tokens,
    trigger_thread_notes_update,
    trigger_usage_update,
)
from core.config import (
    AGENTS_CONFIG,
    DEBATE_TURN_PACING_SECONDS,
    MAX_DISCUSSION_TURNS,
    SLACK_POST_PAUSE_SECONDS,
)
from core.llm_utils import (
    LLM_MAX_RETRIES,
    LLM_RETRY_BACKOFF_BASE,
    _execute_llm_with_retry,
    classify_agent_error,
    execute_llm_with_retry,
    is_anthropic_model,
    is_high_demand_503_error,
    is_openai_model,
)
from nlp.message_builder import (
    build_llm_messages,
    markdown_to_slack,
)
from pipeline.directive_parser import directive_parser
from pipeline.entity_resolver import entity_resolver
from pipeline.orchestrator import SandwichOrchestrator
from pipeline.spend_governance import spend_governance
from prompts import build_agent_prompt
from services.mcp_tools import MCP_TOOLS_SCHEMAS, execute_mcp_tool
from services.slack_client import (
    download_slack_file,
    fetch_thread_files,
    fetch_thread_history,
    post_agent_response,
    post_system_message,
)

# Re-exports from agent_settings
from .agent_settings import (
    CIRCUIT_BREAKER_ENABLED,
    CIRCUIT_BREAKER_THRESHOLD_USD,
    ENABLED_DEBATE_AGENTS,
    LOCKED_AGENTS,
    SETTINGS_FILE,
    get_backup_error_reason,
    get_circuit_breaker_config,
    get_circuit_breaker_threshold,
    get_enabled_agents,
    get_fixed_config,
    get_orchestration_mode,
    get_sandwich_config,
    is_circuit_breaker_enabled,
    is_running_from_backup,
    load_enabled_agents,
    reset_enabled_agents,
    restore_from_backup,
    save_enabled_agents,
    set_agent_enabled,
    set_circuit_breaker_enabled,
    set_circuit_breaker_threshold,
    set_fixed_config,
    set_orchestration_mode,
    set_sandwich_config,
    settings_manager,
    toggle_agent,
)

# Re-exports from thread_state
from .thread_state import (
    ACTIVE_THREAD_TASKS,
    CUSTOM_FLOW_THREADS,
    HIGH_DEMAND_ERRORED_AGENTS,
    RATE_LIMIT_ALREADY_RETRIED,
    RATE_LIMIT_RETRY_QUEUE,
    REMAINING_AGENTS_MAP,
    STOP_REQUESTED_THREADS,
    THREAD_ACTIVE_FILE_CONTEXT,
    THREAD_GENERATIONS,
    THREAD_HISTORY_LIMITS,
    THREAD_MAX_WORDS_LIMITS,
    THREAD_MIN_WORDS_LIMITS,
    add_to_retry_queue,
    clear_503_errored_agents,
    clear_active_task,
    clear_already_retried,
    clear_custom_flow,
    clear_remaining_agents,
    clear_retry_queue,
    clear_thread_file_context,
    clear_thread_history_limit,
    clear_thread_max_words,
    clear_thread_min_words,
    clear_thread_state,
    clear_thread_stop,
    get_503_errored_agents,
    get_active_task,
    get_already_retried,
    get_remaining_agents,
    get_retry_queue,
    get_thread_file_context,
    get_thread_generation,
    get_thread_history_limit,
    get_thread_max_words,
    get_thread_min_words,
    has_agent_503_errored,
    has_pending_retries,
    increment_thread_generation,
    interrupt_thread_discussion,
    is_custom_flow,
    is_discussion_active,
    is_already_retried,
    is_thread_stopped,
    mark_agent_503_errored,
    mark_already_retried,
    pop_active_task,
    pop_remaining_agent,
    pop_retry_agent,
    request_thread_stop,
    reset_all_thread_states,
    set_active_task,
    set_custom_flow,
    set_remaining_agents,
    set_thread_file_context,
    set_thread_history_limit,
    set_thread_max_words,
    set_thread_min_words,
    thread_state_manager,
)

# Re-exports from nomination
from .nomination import (
    enforce_valid_nomination,
    get_initial_agents,
    parse_agent_and_prompt,
    resolve_target_file,
)
from .turn_pipeline import TurnContext, TurnPipeline


logger = logging.getLogger(__name__)

# Turn off verbose LiteLLM logging unless debugging
litellm.suppress_debug_info = True
litellm.drop_params = True

# Pacing configuration (re-exported for backward compatibility and test patching)
# SLACK_POST_PAUSE_SECONDS and DEBATE_TURN_PACING_SECONDS are imported from core.config


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
    pipeline = TurnPipeline(sys.modules[__name__])
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
        for turn in range(2, max_turns + 1):
            if is_thread_stopped(channel_id, thread_ts):
                logger.info(
                    f"[ORCHESTRATOR] Thread {thread_key} stop detected before turn {turn}. Terminating loop."
                )
                clear_thread_state(channel_id, thread_ts)
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

        return last_res
    finally:
        if current_task and thread_state_manager.get_active_task(channel_id, thread_ts) == current_task:
            thread_state_manager.clear_active_task(channel_id, thread_ts, current_task)
