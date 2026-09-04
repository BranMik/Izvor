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

from . import agent_settings as _agent_settings
from . import nomination as _nomination
from . import thread_state as _thread_state
from .thread_state import clear_thread_state, is_thread_stopped, thread_state_manager
from .turn_pipeline import TurnContext, TurnPipeline

__all__ = [
    "process_and_respond",
    "orchestrate_discussion",
]


def __getattr__(name: str) -> Any:
    """Backward-compatible dynamic export for agent_settings, thread_state, and nomination."""
    for mod in (_agent_settings, _thread_state, _nomination):
        if hasattr(mod, name):
            return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    attrs = set(globals().keys())
    for mod in (_agent_settings, _thread_state, _nomination):
        attrs.update(dir(mod))
    return sorted(attrs)



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
