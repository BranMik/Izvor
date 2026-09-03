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
from core.config import AGENTS_CONFIG
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
    clear_503_errored_agents,
    clear_thread_file_context,
    clear_thread_history_limit,
    clear_thread_max_words,
    clear_thread_min_words,
    clear_thread_state,
    clear_thread_stop,
    get_503_errored_agents,
    get_thread_file_context,
    get_thread_history_limit,
    get_thread_max_words,
    get_thread_min_words,
    has_agent_503_errored,
    interrupt_thread_discussion,
    is_discussion_active,
    is_thread_stopped,
    mark_agent_503_errored,
    request_thread_stop,
    set_thread_file_context,
    set_thread_history_limit,
    set_thread_max_words,
    set_thread_min_words,
)

# Re-exports from debate_orchestrator
from .debate_orchestrator import (
    enforce_valid_nomination,
    get_initial_agents,
    parse_agent_and_prompt,
    resolve_target_file,
)


logger = logging.getLogger(__name__)

# Turn off verbose LiteLLM logging unless debugging
litellm.suppress_debug_info = True
litellm.drop_params = True

# Pacing configuration
SLACK_POST_PAUSE_SECONDS = 0.01 if "pytest" in sys.modules else 2.0


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

    start_time = time.time()
    logger.info(
        f"\n[{start_time}] >>> START process_and_respond for thread_ts: {thread_ts} <<<"
    )

    thread_key = (channel_id, thread_ts)

    if not is_bot_trigger:
        THREAD_GENERATIONS[thread_key] = THREAD_GENERATIONS.get(thread_key, 0) + 1

    my_generation = THREAD_GENERATIONS.get(thread_key, 0)
    current_task = asyncio.current_task()
    if current_task:
        ACTIVE_THREAD_TASKS[thread_key] = current_task

    def is_superseded() -> bool:
        if THREAD_GENERATIONS.get(thread_key, 0) != my_generation:
            return True
        if current_task and current_task.cancelled():
            return True
        return False

    directives = await directive_parser.parse(raw_text)
    history_limit = directives.history_limit
    max_words_limit = directives.max_words
    min_words_limit = directives.min_words
    excluded_agents = directives.excluded_agents
    is_file_req = directives.is_file_req
    target_filename = directives.target_filename
    raw_text_cleaned = directives.cleaned_prompt

    if not is_bot_trigger:
        clear_thread_stop(channel_id, thread_ts)
        thread_files = await fetch_thread_files(channel_id, thread_ts)
        if thread_files:
            directives = await directive_parser.parse(raw_text, available_files=thread_files)
            history_limit = directives.history_limit
            max_words_limit = directives.max_words
            min_words_limit = directives.min_words
            excluded_agents = directives.excluded_agents
            is_file_req = directives.is_file_req
            target_filename = directives.target_filename
            raw_text_cleaned = directives.cleaned_prompt

        if max_words_limit is not None:
            set_thread_max_words(channel_id, thread_ts, max_words_limit)
        if min_words_limit is not None:
            set_thread_min_words(channel_id, thread_ts, min_words_limit)

        ambiguous_tags = entity_resolver.detect_ambiguous_tags(raw_text_cleaned)
        if ambiguous_tags:
            logger.warning(
                f"[ROUTER] Ambiguous agent tag(s) detected in user prompt for thread {thread_key}: {ambiguous_tags}. Halting turn."
            )
            lines = ["⚠️ *Ambiguous Agent Reference*:"]
            for tag_str, candidates in ambiguous_tags:
                clean_tag = tag_str.strip("[](){}:,; \t*~_")
                lines.append(f"Tag '{clean_tag}' matches multiple candidates:")
                for cand in candidates:
                    cfg = (settings_manager.agents_config or AGENTS_CONFIG).get(cand, {})
                    cand_username = cfg.get("username", cand)
                    lines.append(f"• {cand} ({cand_username})")
            first_suggestion = ambiguous_tags[0][1][0]
            lines.append(f"\nPlease specify the exact agent name (e.g. '{first_suggestion}') to proceed.")
            warning_msg = "\n".join(lines)

            await post_system_message(
                channel_id=channel_id,
                thread_ts=thread_ts,
                text=warning_msg,
            )
            return {
                "ok": False,
                "halted": True,
                "reason": "ambiguous_agent",
                "ambiguous_tags": ambiguous_tags,
            }

        unrecognized_tags = entity_resolver.detect_unrecognized_tags(raw_text_cleaned)
        if unrecognized_tags:
            logger.warning(
                f"[ROUTER] Unrecognized agent tag(s) detected in user prompt for thread {thread_key}: {unrecognized_tags}. Halting turn."
            )
            lines = ["⚠️ *Unrecognized Agent Tag*:"]
            for tag_str in unrecognized_tags:
                clean_tag = tag_str.strip("[](){}:,; \t*~_")
                lines.append(f"Tag '{clean_tag}' could not be matched to any active agent.")
            lines.append("\nPlease check the agent name or tag a valid agent (e.g. 'Visionary', 'TechLead', 'Innovator') to proceed.")
            warning_msg = "\n".join(lines)

            await post_system_message(
                channel_id=channel_id,
                thread_ts=thread_ts,
                text=warning_msg,
            )
            return {
                "ok": False,
                "halted": True,
                "reason": "unrecognized_agent",
                "unrecognized_tags": unrecognized_tags,
            }

        agent_key, prompt, was_explicit_target = parse_agent_and_prompt(
            raw_text_cleaned, channel_id, thread_ts
        )

        tagged = entity_resolver.extract_tagged_agents(raw_text_cleaned)
        if tagged:
            if excluded_agents:
                tagged = [a for a in tagged if a not in excluded_agents]
            if tagged:
                agent_key = tagged[0]
                remaining_tagged = [a for a in tagged if a != agent_key]
                REMAINING_AGENTS_MAP[thread_key] = remaining_tagged
                CUSTOM_FLOW_THREADS.add(thread_key)
                was_explicit_target = True
            else:
                # All tagged agents were excluded; halt with system message
                logger.warning(
                    f"[ROUTER] All tagged agents in thread {thread_key} were excluded. Halting turn."
                )
                warning_msg = (
                    "⚠️ *All tagged agents were excluded*: None of the specified agents can respond to this request."
                )
                await post_system_message(
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    text=warning_msg,
                )
                return {
                    "ok": True,
                    "halted": True,
                    "reason": "all_tagged_agents_excluded",
                }
        else:
            CUSTOM_FLOW_THREADS.discard(thread_key)
            if excluded_agents:
                curr_remaining = REMAINING_AGENTS_MAP.get(thread_key, get_initial_agents())
                curr_remaining = [a for a in curr_remaining if a not in excluded_agents]
                REMAINING_AGENTS_MAP[thread_key] = curr_remaining
                if agent_key in excluded_agents:
                    if curr_remaining:
                        agent_key = curr_remaining.pop(0)
                        REMAINING_AGENTS_MAP[thread_key] = curr_remaining
                    else:
                        logger.warning(
                            f"[ROUTER] Agent '{agent_key}' and all remaining agents in thread {thread_key} are excluded. Halting turn."
                        )
                        warning_msg = (
                            f"⚠️ *Agent Excluded*: Agent '{agent_key}' was excluded and no other agents are available to continue."
                        )
                        await post_system_message(
                            channel_id=channel_id,
                            thread_ts=thread_ts,
                            text=warning_msg,
                        )
                        return {
                            "ok": True,
                            "halted": True,
                            "reason": "all_agents_excluded",
                        }

        if is_file_req:
            if tagged:
                authorized_agents = set(tagged)
            elif was_explicit_target:
                authorized_agents = {agent_key}
            else:
                authorized_agents = None

            chosen_file = resolve_target_file(thread_files, target_filename)
            if chosen_file:
                logger.info(
                    f"[ROUTER] User requested file reading for thread {thread_key}. Downloading file: {chosen_file.get('name')}"
                )
                dl_res = await download_slack_file(chosen_file)
                if dl_res.get("ok"):
                    set_thread_file_context(
                        channel_id,
                        thread_ts,
                        {
                            "file_name": dl_res.get(
                                "file_name", chosen_file.get("name")
                            ),
                            "content": dl_res.get("content", ""),
                            "size": dl_res.get("size", 0),
                            "truncated": dl_res.get("truncated", False),
                            "authorized_agents": authorized_agents,
                        },
                    )
                else:
                    set_thread_file_context(
                        channel_id,
                        thread_ts,
                        {
                            "file_name": chosen_file.get("name", "unknown"),
                            "content": f"[Error reading file: {dl_res.get('error')}]",
                            "size": chosen_file.get("size", 0),
                            "truncated": False,
                            "authorized_agents": authorized_agents,
                        },
                    )
            else:
                logger.warning(
                    f"[ROUTER] User requested file reading but no files were found in thread {thread_key}"
                )
                set_thread_file_context(
                    channel_id,
                    thread_ts,
                    {
                        "file_name": target_filename or "unknown",
                        "content": "[No attached file found in this Slack thread to read.]",
                        "size": 0,
                        "truncated": False,
                        "authorized_agents": authorized_agents,
                    },
                )
        else:
            clear_thread_file_context(channel_id, thread_ts)
    else:
        agent_key, prompt, was_explicit_target = parse_agent_and_prompt(
            raw_text_cleaned, channel_id, thread_ts
        )

    if not agent_key:
        logger.info(f"[ROUTER] No agent available to speak in thread {thread_key}. Halting turn.")
        enabled = settings_manager.get_enabled_agents()
        if not enabled:
            await post_system_message(
                channel_id=channel_id,
                thread_ts=thread_ts,
                text="⚠️ *No Agents Enabled*: There are currently no enabled agents in the panel to respond.",
            )
        return {
            "ok": True,
            "halted": True,
            "reason": "no_agents_available",
        }

    agents_map = settings_manager.agents_config or AGENTS_CONFIG
    if agent_key not in agents_map:
        logger.error(f"[ROUTER] Agent '{agent_key}' not found in configuration.")
        remaining = REMAINING_AGENTS_MAP.get(thread_key, [])
        next_candidate = None
        if remaining:
            next_candidate = remaining.pop(0)
        else:
            mode = settings_manager.get_orchestration_mode()
            if mode == "sandwich":
                end_chain = [k for k in settings_manager.get_sandwich_config().get("end_chain", []) if k in agents_map]
                if end_chain:
                    next_candidate = end_chain[0]
            if not next_candidate:
                available = [k for k in settings_manager.get_enabled_agents() if k in agents_map and k != agent_key]
                if available:
                    next_candidate = random.choice(available)

        if next_candidate:
            warning_msg = (
                f"⚠️ *Configuration Error*: Agent '{agent_key}' is not configured. "
                f"Handing off to [{next_candidate}]."
            )
            await post_system_message(
                channel_id=channel_id,
                thread_ts=thread_ts,
                text=warning_msg,
            )
            agent_key = next_candidate
            agent_cfg = agents_map[agent_key]
        else:
            warning_msg = (
                f"⚠️ *Configuration Error*: Agent '{agent_key}' is not configured and no other agents are available."
            )
            await post_system_message(
                channel_id=channel_id,
                thread_ts=thread_ts,
                text=warning_msg,
            )
            return {
                "ok": False,
                "halted": True,
                "reason": "agent_not_configured",
            }
    else:
        agent_cfg = agents_map[agent_key]

    logger.info(
        f"[ROUTER] Parsed Agent: {agent_key} ({agent_cfg['model']}) | Explicit target: {was_explicit_target} | Bot trigger: {is_bot_trigger} | Gen: {my_generation} | History limit: {history_limit}"
    )

    if is_superseded():
        logger.info(f"[ROUTER] Thread {thread_key} turn was superseded. Aborting.")
        return {"ok": False, "cancelled": True, "reason": "superseded"}

    # Circuit-breaker verification: Check if cumulative thread cost reached or exceeded threshold
    if is_circuit_breaker_enabled():
        cost_info = calculate_thread_costs(channel_id, thread_ts)
        current_cost = cost_info.get("grand_total_cost_usd", 0.0)
        threshold = get_circuit_breaker_threshold()
        session_id = f"{channel_id}:{thread_ts}"
        tripped, current_cost, threshold = spend_governance.is_budget_exceeded(
            session_id=session_id,
            threshold_usd=threshold,
            enabled=True,
            current_cost_usd=current_cost,
        )
        if tripped:
            logger.warning(
                f"[CIRCUIT BREAKER] Thread {thread_key} cost (${current_cost:.5f}) reached or exceeded threshold (${threshold:.5f}). Interrupting response."
            )
            circuit_breaker_msg = "EXPENSE LIMIT REACHED FOR THE THREAD. ENDING CALLS"
            slack_res = await post_agent_response(
                channel_id=channel_id,
                thread_ts=thread_ts,
                text=circuit_breaker_msg,
                username=agent_cfg["username"],
                icon_url=agent_cfg.get("icon_url"),
            )
            await asyncio.sleep(SLACK_POST_PAUSE_SECONDS)
            trigger_thread_notes_update(
                channel_id=channel_id,
                thread_ts=thread_ts,
                is_completed=True,
                bot_user_id=bot_user_id,
            )
            trigger_usage_update(
                channel_id=channel_id,
                thread_ts=thread_ts,
                is_completed=True,
            )
            clear_thread_state(channel_id, thread_ts)
            if isinstance(slack_res, dict):

                slack_res["is_completed"] = True
                slack_res["final_text"] = circuit_breaker_msg
                slack_res["username"] = agent_cfg["username"]
                slack_res["agent_key"] = agent_key
            return {
                "ok": True,
                "circuit_breaker_triggered": True,
                "is_completed": True,
                "slack_res": slack_res,
                "final_text": circuit_breaker_msg,
                "username": agent_cfg["username"],
                "agent_key": agent_key,
            }

    # 1. Fetch thread history context
    if in_memory_history is not None:
        history = list(in_memory_history)
        logger.info(f"[ROUTER] Using in-memory history with {len(history)} messages.")
    else:
        history = await fetch_thread_history(channel_id, thread_ts, bot_user_id=bot_user_id)
        logger.info(f"[ROUTER] Fetched {len(history)} messages from thread history.")

    if is_superseded():
        logger.info(f"[ROUTER] Thread {thread_key} turn was superseded. Aborting.")
        return {"ok": False, "cancelled": True, "reason": "superseded"}

    # Determine effective conversation history based on active /history limit
    if not is_bot_trigger:
        if history and history[-1]["role"] == "user":
            prior_messages = history[:-1]
        else:
            prior_messages = history

        if history_limit is not None:
            set_thread_history_limit(
                channel_id=channel_id,
                thread_ts=thread_ts,
                limit=history_limit,
                prior_cutoff_index=len(prior_messages),
            )
            effective_prior = (
                prior_messages[-history_limit:] if history_limit > 0 else []
            )
            logger.info(
                f"[ROUTER] Applying /history {history_limit}: kept {len(effective_prior)} of {len(prior_messages)} prior messages for user prompt."
            )
        else:
            clear_thread_history_limit(channel_id, thread_ts)
            effective_prior = prior_messages

        effective_history = effective_prior
    else:
        hist_limit_info = get_thread_history_limit(channel_id, thread_ts)
        if hist_limit_info is not None:
            limit = hist_limit_info["limit"]
            cutoff = hist_limit_info["prior_cutoff_index"]
            prior_part = history[:cutoff]
            discussion_part = history[cutoff:]
            effective_prior = prior_part[-limit:] if limit > 0 else []
            effective_history = effective_prior + discussion_part
            logger.info(
                f"[ROUTER] Applying active history limit ({limit}): kept {len(effective_prior)}/{len(prior_part)} prior messages + {len(discussion_part)} discussion messages."
            )
        else:
            effective_history = history

    # 2. Build message sequence for LiteLLM
    active_max_words = get_thread_max_words(channel_id, thread_ts)
    active_min_words = get_thread_min_words(channel_id, thread_ts)
    if active_max_words is not None or active_min_words is not None:
        logger.info(
            f"[ROUTER] Applying word limits (min_words={active_min_words}, max_words={active_max_words}) for agent {agent_key} (skipping standard output_constraints)."
        )
        system_prompt = build_agent_prompt(
            agent_key, max_words=active_max_words, min_words=active_min_words
        )
    else:
        system_prompt = agent_cfg["system_prompt"]

    remaining = REMAINING_AGENTS_MAP.get(thread_key, get_initial_agents())
    is_custom_flow = thread_key in CUSTOM_FLOW_THREADS

    force_termination = False
    if was_explicit_target and not is_bot_trigger and not remaining:
        force_termination = True
    elif is_custom_flow and not remaining:
        force_termination = True
    elif is_thread_stopped(channel_id, thread_ts):
        force_termination = True

    if force_termination:
        logger.info(
            f"[ROUTER] Agent {agent_key} forcing termination (no remaining agents in sequence or stop requested)."
        )
    else:
        logger.info(
            f"[ROUTER] Agent {agent_key} remaining available agents: {remaining}"
        )

    mode = get_orchestration_mode()
    next_designated_agent = None
    eligible_remaining_agents = list(remaining)
    if not force_termination and remaining:
        orchestrator = SandwichOrchestrator(
            mode=mode,
            start_chain=get_sandwich_config().get("start_chain", []),
            end_chain=get_sandwich_config().get("end_chain", []),
            fixed_chain=settings_manager.get_fixed_config().get("chain", []),
            agents_map=agents_map,
        )
        next_designated_agent, eligible_remaining_agents = orchestrator.get_phase_info(
            remaining, is_custom_flow=is_custom_flow
        )

    file_context = get_thread_file_context(channel_id, thread_ts)
    messages = build_llm_messages(
        system_prompt=system_prompt,
        effective_history=effective_history,
        prompt=prompt,
        is_bot_trigger=is_bot_trigger,
        agent_key=agent_key,
        agent_cfg=agent_cfg,
        file_context=file_context,
        remaining_agents=eligible_remaining_agents,
        force_termination=force_termination,
        next_designated_agent=next_designated_agent,
    )

    try:
        if is_superseded():
            logger.info(
                f"[ROUTER] Thread {thread_key} turn was superseded before LLM call. Aborting."
            )
            return {"ok": False, "cancelled": True, "reason": "superseded"}

        total_prompt_tokens = 0
        total_completion_tokens = 0

        def add_usage(resp, context: str = ""):
            nonlocal total_prompt_tokens, total_completion_tokens
            if not resp:
                return

            usage = getattr(resp, "usage", None)
            if not usage:
                return
            if isinstance(usage, dict):
                total_prompt_tokens += usage.get("prompt_tokens", 0) or 0
                total_completion_tokens += usage.get("completion_tokens", 0) or 0
            else:
                total_prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
                total_completion_tokens += getattr(usage, "completion_tokens", 0) or 0

        # 3. Call LiteLLM model with MCP tools in a threadpool with automatic retries
        already_503_errored = has_agent_503_errored(channel_id, thread_ts, agent_key)
        allow_503_immediate = already_503_errored

        logger.info(
            f"[LLM] Dispatching litellm.completion for {agent_key} (already_503_errored={already_503_errored})..."
        )
        llm_start = time.time()
        completion_kwargs: dict[str, Any] = {
            "model": agent_cfg["model"],
            "messages": messages,
            "tools": MCP_TOOLS_SCHEMAS,
            "tool_choice": "auto",
            "num_retries": 2,
        }
        if any(keyword in agent_cfg["model"].lower() for keyword in ["muse", "llama", "meta"]):
            if os.getenv("META_API_KEY"):
                completion_kwargs["api_key"] = os.getenv("META_API_KEY")
            if os.getenv("META_API_BASE"):
                completion_kwargs["api_base"] = os.getenv("META_API_BASE")
        response = await execute_llm_with_retry(
            litellm.completion,
            completion_kwargs,
            agent_key=agent_key,
            thread_key=thread_key,
            is_superseded_fn=is_superseded,
            run_in_threadpool_fn=run_in_threadpool,
            allow_503_immediate_retry=allow_503_immediate,
        )
        if response is None and is_superseded():
            return {"ok": False, "cancelled": True, "reason": "superseded"}

        add_usage(response)
        llm_duration = time.time() - llm_start
        logger.info(
            f"[LLM] Received response for {agent_key} in {llm_duration:.2f} seconds."
        )

        if is_superseded():
            logger.info(
                f"[ROUTER] Thread {thread_key} turn was superseded after LLM call. Aborting."
            )
            return {"ok": False, "cancelled": True, "reason": "superseded"}

        response_message = response.choices[0].message

        # 4. Handle Tool Calls if model requested MCP tool execution
        tool_calls = getattr(response_message, "tool_calls", None)
        if tool_calls:
            messages.append(response_message.model_dump())

            for tool_call in tool_calls:
                fn_name = tool_call.function.name
                fn_args = (
                    json.loads(tool_call.function.arguments)
                    if isinstance(tool_call.function.arguments, str)
                    else tool_call.function.arguments
                )
                tool_result = execute_mcp_tool(fn_name, fn_args)

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": fn_name,
                        "content": tool_result,
                    }
                )

            if is_superseded():
                logger.info(
                    f"[ROUTER] Thread {thread_key} turn was superseded during tool execution. Aborting."
                )
                return {"ok": False, "cancelled": True, "reason": "superseded"}

            follow_up_kwargs: dict[str, Any] = {
                "model": agent_cfg["model"],
                "messages": messages,
                "num_retries": 2,
            }
            if any(keyword in agent_cfg["model"].lower() for keyword in ["muse", "llama", "meta"]):
                if os.getenv("META_API_KEY"):
                    follow_up_kwargs["api_key"] = os.getenv("META_API_KEY")
                if os.getenv("META_API_BASE"):
                    follow_up_kwargs["api_base"] = os.getenv("META_API_BASE")
            follow_up = await execute_llm_with_retry(
                litellm.completion,
                follow_up_kwargs,
                agent_key=f"{agent_key} follow-up",
                thread_key=thread_key,
                is_superseded_fn=is_superseded,
                run_in_threadpool_fn=run_in_threadpool,
                allow_503_immediate_retry=allow_503_immediate,
            )
            if follow_up is None and is_superseded():
                return {"ok": False, "cancelled": True, "reason": "superseded"}

            add_usage(follow_up, f"{agent_key} follow-up")
            final_text = follow_up.choices[0].message.content
        else:
            final_text = response_message.content

        if is_superseded():
            logger.info(
                f"[ROUTER] Thread {thread_key} turn was superseded before pacing delay. Aborting."
            )
            return {"ok": False, "cancelled": True, "reason": "superseded"}

        if is_thread_stopped(channel_id, thread_ts):
            force_termination = True
            logger.info(
                f"[ROUTER] Thread {thread_key} received stop request during execution. Forcing termination."
            )

        retries = RATE_LIMIT_RETRY_QUEUE.get(thread_key, [])
        if not final_text:
            if force_termination:
                final_text = "(No text response generated)"
            else:
                if next_designated_agent:
                    next_tag = next_designated_agent
                    final_text = f"(No text response generated) [{next_tag}] next"
                elif eligible_remaining_agents:
                    next_tag = eligible_remaining_agents[0]
                    final_text = f"(No text response generated) [{next_tag}] next"
                elif retries:
                    next_tag = retries[0]
                    final_text = f"(No text response generated) [{next_tag}] next"
                else:
                    final_text = "(No text response generated)"
                    force_termination = True
            logger.warning(f"[ROUTER] final_text was empty for {agent_key}!")
        else:
            final_text = markdown_to_slack(final_text)
            final_text = enforce_valid_nomination(
                final_text,
                agent_key,
                [] if force_termination else eligible_remaining_agents,
                force_termination,
                pending_retries=retries,
                designated_next_agent=next_designated_agent,
            )

        retries = RATE_LIMIT_RETRY_QUEUE.get(thread_key, [])
        if not remaining and retries:
            for r_agent in list(retries):
                if f"[{r_agent}]" in final_text:
                    retries.remove(r_agent)
                    RATE_LIMIT_ALREADY_RETRIED.setdefault(thread_key, set()).add(
                        r_agent
                    )
                    break

        agent_model = agent_cfg.get("model", "")
        if agent_model:
            token_footer = f"{{{agent_model} : {total_prompt_tokens}/{total_completion_tokens}}}"
        else:
            token_footer = f"[{total_prompt_tokens}/{total_completion_tokens}]"

        final_text = f"{final_text.strip()}\n\n{token_footer}"

        generated_tags = re.findall(r"[\[{]\s*([^\]}\r\n]+?)\s*[\]}]", final_text)
        logger.info(
            f"[ROUTER] Agent {agent_key} generated tag(s): {generated_tags}. Full text length: {len(final_text)}"
        )

        await asyncio.sleep(3.0)

        if is_superseded():
            logger.info(
                f"[ROUTER] Thread {thread_key} turn was superseded after pacing delay. Aborting post."
            )
            return {"ok": False, "cancelled": True, "reason": "superseded"}

        logger.info(
            f"[SLACK] Posting response to channel {channel_id}, thread_ts {thread_ts} as {agent_cfg['username']}"
        )
        slack_res = await post_agent_response(
            channel_id=channel_id,
            thread_ts=thread_ts,
            text=final_text,
            username=agent_cfg["username"],
            icon_url=agent_cfg.get("icon_url"),
        )
        posted_ts = slack_res.get("ts")
        logger.info(f"[SLACK] Successfully posted message! Received ts: {posted_ts}")

        await asyncio.sleep(SLACK_POST_PAUSE_SECONDS)

        has_pending_retries = bool(RATE_LIMIT_RETRY_QUEUE.get(thread_key))
        agents_map = settings_manager.agents_config or AGENTS_CONFIG
        has_nominated_next = any(tag in agents_map for tag in generated_tags)
        is_completed = bool(
            force_termination
            or (
                not remaining
                and not has_pending_retries
                and not has_nominated_next
            )
            or (is_thread_stopped(channel_id, thread_ts))
        )

        record_thread_tokens(
            channel_id=channel_id,
            thread_ts=thread_ts,
            model_name=agent_cfg["model"],
            prompt_tokens=total_prompt_tokens,
            completion_tokens=total_completion_tokens,
        )

        trigger_thread_notes_update(
            channel_id=channel_id,
            thread_ts=thread_ts,
            is_completed=is_completed,
            bot_user_id=bot_user_id,
        )

        trigger_usage_update(
            channel_id=channel_id,
            thread_ts=thread_ts,
            is_completed=is_completed,
        )

        if is_completed:
            logger.info(
                f"[ROUTER] Thread {thread_key} reached termination (forced or natural). Cleaning up state."
            )
            clear_thread_state(channel_id, thread_ts)
            RATE_LIMIT_ALREADY_RETRIED.pop(thread_key, None)

        if isinstance(slack_res, dict):
            slack_res["is_completed"] = is_completed
            slack_res["final_text"] = final_text
            slack_res["username"] = agent_cfg["username"]
            slack_res["agent_key"] = agent_key
            slack_res["generated_tags"] = generated_tags

        logger.info(
            f"[{time.time()}] <<< END process_and_respond for thread_ts: {thread_ts} (Total: {time.time() - start_time:.2f}s) <<<\n"
        )
        return slack_res

    except asyncio.CancelledError:
        logger.info(
            f"[ROUTER] Task cancelled for thread {thread_key} (agent {agent_key}, gen {my_generation}). Aborting cleanly."
        )
        return {"ok": False, "cancelled": True, "reason": "cancelled"}
    except Exception as e:
        if is_superseded():
            logger.info(
                f"[ROUTER] Exception occurred on superseded turn for thread {thread_key}. Aborting without posting."
            )
            return {"ok": False, "cancelled": True, "reason": "superseded"}

        logger.exception(f"[ERROR] Exception during agent completion or post: {e}")
        err_category = classify_agent_error(e)
        logger.info(f"[ROUTER] Classified error for {agent_key} as: '{err_category}'")

        stop_requested = is_thread_stopped(channel_id, thread_ts)
        already_503 = has_agent_503_errored(channel_id, thread_ts, agent_key)

        is_first_503_skip = False
        if err_category == "api_key_not_found":
            error_sentence = "API KEY NOT FOUND"
        elif err_category == "model_not_found":
            error_sentence = "MODEL NOT FOUND"
        elif err_category == "rate_limit":
            already_retried = agent_key in RATE_LIMIT_ALREADY_RETRIED.get(
                thread_key, set()
            )
            if not already_retried and not stop_requested:
                error_sentence = (
                    "RATE LIMITS CROSSED, will retry at the end of discussion"
                )
                RATE_LIMIT_RETRY_QUEUE.setdefault(thread_key, []).append(agent_key)
            else:
                error_sentence = "RATE LIMITS CROSSED"
        elif err_category == "service_unavailable":
            if not already_503 and not stop_requested and not force_termination:
                mark_agent_503_errored(channel_id, thread_ts, agent_key)
                if agent_key not in remaining:
                    mode = settings_manager.get_orchestration_mode()
                    if mode == "sandwich":
                        sandwich_cfg = settings_manager.get_sandwich_config()
                        end_chain = sandwich_cfg.get("end_chain", [])
                        end_chain_indices = [
                            i for i, k in enumerate(remaining) if k in end_chain
                        ]
                        if end_chain_indices and agent_key not in end_chain:
                            remaining.insert(end_chain_indices[0], agent_key)
                        else:
                            remaining.append(agent_key)
                    else:
                        remaining.append(agent_key)
                REMAINING_AGENTS_MAP[thread_key] = remaining
                other_candidates = [a for a in remaining if a != agent_key]
                if other_candidates:
                    is_first_503_skip = True
                    error_sentence = (
                        "HIGH DEMAND / SERVICE OVERLOADED (503) - Skipping for now, will retry later"
                    )
                else:
                    error_sentence = "HIGH DEMAND / SERVICE OVERLOADED (503)"
            else:
                error_sentence = "HIGH DEMAND / SERVICE OVERLOADED (503)"
        else:
            error_sentence = "Error processing request - check the logs"

        next_nomination = None
        if not stop_requested:
            if is_first_503_skip:
                other_candidates = [a for a in remaining if a != agent_key]
                if other_candidates:
                    next_agent = random.choice(other_candidates)
                    next_nomination = f"[{next_agent}]"
                elif remaining:
                    next_agent = remaining[0]
                    next_nomination = f"[{next_agent}]"
            elif remaining:
                next_agent = remaining[0]
                next_nomination = f"[{next_agent}]"
            else:
                retries = RATE_LIMIT_RETRY_QUEUE.get(thread_key, [])
                if retries:
                    next_agent = retries.pop(0)
                    RATE_LIMIT_ALREADY_RETRIED.setdefault(thread_key, set()).add(
                        next_agent
                    )
                    next_nomination = f"[{next_agent}]"

        if next_nomination:
            error_post_text = f"{error_sentence}\n\n{next_nomination}"
        else:
            error_post_text = error_sentence

        logger.info(
            f"[SLACK] Posting gracious error message to channel {channel_id}, thread_ts {thread_ts} as {agent_cfg['username']}"
        )
        slack_res = await post_agent_response(
            channel_id=channel_id,
            thread_ts=thread_ts,
            text=error_post_text,
            username=agent_cfg["username"],
            icon_url=agent_cfg.get("icon_url"),
        )

        await asyncio.sleep(SLACK_POST_PAUSE_SECONDS)

        has_pending_retries = bool(RATE_LIMIT_RETRY_QUEUE.get(thread_key))
        is_completed = bool(
            force_termination
            or not next_nomination
            or is_thread_stopped(channel_id, thread_ts)
        )

        record_thread_tokens(
            channel_id=channel_id,
            thread_ts=thread_ts,
            model_name=agent_cfg["model"],
            prompt_tokens=0,
            completion_tokens=0,
        )

        trigger_thread_notes_update(
            channel_id=channel_id,
            thread_ts=thread_ts,
            is_completed=is_completed,
            bot_user_id=bot_user_id,
        )

        trigger_usage_update(
            channel_id=channel_id,
            thread_ts=thread_ts,
            is_completed=is_completed,
        )

        if is_completed:
            clear_thread_state(channel_id, thread_ts)


        if isinstance(slack_res, dict):
            slack_res["is_completed"] = is_completed
            slack_res["final_text"] = error_post_text
            slack_res["username"] = agent_cfg["username"]
            slack_res["agent_key"] = agent_key

        return {
            "ok": False,
            "error": str(e),
            "classified_error": err_category,
            "slack_res": slack_res,
            "is_completed": is_completed,
            "final_text": error_post_text,
            "username": agent_cfg["username"],
            "agent_key": agent_key,
        }
    finally:
        if current_task and ACTIVE_THREAD_TASKS.get(thread_key) == current_task:
            if in_memory_history is None or is_completed:
                ACTIVE_THREAD_TASKS.pop(thread_key, None)


async def orchestrate_discussion(
    channel_id: str,
    thread_ts: str,
    raw_text: str,
    bot_user_id: str | None = None,
    icon_url: str | None = None,
    max_turns: int = 20,
) -> dict[str, Any]:
    """
    Internal multi-agent discussion loop:
    Orchestrates the entire multi-agent discussion chain in-memory without relying on
    Slack Events API webhook bounce-backs.
    """
    thread_key = (channel_id, thread_ts)
    logger.info(
        f"\n[ORCHESTRATOR] Starting internal discussion loop for thread {thread_key}"
    )

    current_task = asyncio.current_task()
    if current_task:
        ACTIVE_THREAD_TASKS[thread_key] = current_task

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
        if current_task and ACTIVE_THREAD_TASKS.get(thread_key) == current_task:
            ACTIVE_THREAD_TASKS.pop(thread_key, None)
