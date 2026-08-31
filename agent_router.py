import asyncio
import json
import logging
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
from config import AGENTS_CONFIG, DEFAULT_AGENT_KEY
from mcp_tools import MCP_TOOLS_SCHEMAS, execute_mcp_tool
from nl_patterns import (
    FILE_COMMAND_PATTERN,
    FILE_NL_PATTERNS,
    HISTORY_COMMAND_PATTERN,
    HISTORY_NL_PATTERNS,
    MAX_WORDS_COMMAND_PATTERN,
    MAX_WORDS_NL_PATTERNS,
    MIN_WORDS_COMMAND_PATTERN,
    MIN_WORDS_NL_PATTERNS,
    STOP_COMMAND_PATTERN,
    extract_file_instruction,
    extract_history_limit,
    extract_max_words_limit,
    extract_min_words_limit,
    is_stop_command,
)
from prompts import build_agent_prompt
from slack_client import (
    download_slack_file,
    fetch_thread_files,
    fetch_thread_history,
    post_agent_response,
)

# Re-exports from agent_settings
from agent_settings import (
    CIRCUIT_BREAKER_ENABLED,
    CIRCUIT_BREAKER_THRESHOLD_USD,
    ENABLED_DEBATE_AGENTS,
    LOCKED_AGENTS,
    SETTINGS_FILE,
    get_circuit_breaker_config,
    get_circuit_breaker_threshold,
    get_enabled_agents,
    is_circuit_breaker_enabled,
    load_enabled_agents,
    reset_enabled_agents,
    save_enabled_agents,
    set_agent_enabled,
    set_circuit_breaker_enabled,
    set_circuit_breaker_threshold,
    toggle_agent,
)

# Re-exports from thread_state
from thread_state import (
    ACTIVE_THREAD_TASKS,
    CUSTOM_FLOW_THREADS,
    RATE_LIMIT_ALREADY_RETRIED,
    RATE_LIMIT_RETRY_QUEUE,
    REMAINING_AGENTS_MAP,
    STOP_REQUESTED_THREADS,
    THREAD_ACTIVE_FILE_CONTEXT,
    THREAD_GENERATIONS,
    THREAD_HISTORY_LIMITS,
    THREAD_MAX_WORDS_LIMITS,
    THREAD_MIN_WORDS_LIMITS,
    clear_thread_file_context,
    clear_thread_history_limit,
    clear_thread_max_words,
    clear_thread_min_words,
    clear_thread_state,
    clear_thread_stop,
    get_thread_file_context,
    get_thread_history_limit,
    get_thread_max_words,
    get_thread_min_words,
    interrupt_thread_discussion,
    is_discussion_active,
    is_thread_stopped,
    request_thread_stop,
    set_thread_file_context,
    set_thread_history_limit,
    set_thread_max_words,
    set_thread_min_words,
)

# Re-exports from debate_orchestrator
from debate_orchestrator import (
    enforce_valid_nomination,
    extract_tagged_agents,
    get_initial_agents,
    parse_agent_and_prompt,
    resolve_target_file,
)

# Re-exports from llm_utils
from llm_utils import (
    LLM_MAX_RETRIES,
    LLM_RETRY_BACKOFF_BASE,
    _execute_llm_with_retry,
    classify_agent_error,
    execute_llm_with_retry,
    is_anthropic_model,
    is_openai_model,
)

# Re-exports from message_builder
from message_builder import (
    build_llm_messages,
    markdown_to_slack,
)

# Re-exports from intent_extractor
from intent_extractor import (
    extract_user_intent,
    should_skip_intent_extractor,
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
) -> dict[str, Any]:
    """
    Main background handler: parses prompt, builds conversation history,
    calls LiteLLM with agent parameters & MCP tools, and posts response back to Slack thread.
    """
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

    history_limit, raw_text_cleaned = extract_history_limit(raw_text)
    max_words_limit, raw_text_cleaned = extract_max_words_limit(raw_text_cleaned)
    min_words_limit, raw_text_cleaned = extract_min_words_limit(raw_text_cleaned)

    # Adjust remaining agents list and file instructions if user input
    if not is_bot_trigger:
        clear_thread_stop(channel_id, thread_ts)
        thread_files = await fetch_thread_files(channel_id, thread_ts)
        is_file_req, target_filename, raw_text_cleaned = extract_file_instruction(
            raw_text_cleaned, available_files=thread_files
        )

        # Check skip rules for LLM intent extraction
        has_slash_file = bool(
            re.search(r"/(?:read[-_]?file|file|attachment)", raw_text, re.IGNORECASE)
        )
        has_slash_max_words = bool(
            re.search(r"/(?:max_words|max-words|maxwords)", raw_text, re.IGNORECASE)
        )
        has_slash_history = bool(
            re.search(r"/history", raw_text, re.IGNORECASE)
        )
        has_thread_files = bool(thread_files)

        if not should_skip_intent_extractor(
            raw_text=raw_text,
            is_file_cmd=has_slash_file,
            has_slash_max_words=has_slash_max_words,
            has_slash_history=has_slash_history,
            has_thread_files=has_thread_files,
        ):
            intent = await extract_user_intent(
                raw_text_cleaned, available_files=thread_files
            )
            if max_words_limit is None and intent.get("max_words") is not None:
                max_words_limit = intent["max_words"]
            if history_limit is None and intent.get("history") is not None:
                history_limit = intent["history"]
            if not is_file_req and intent.get("file_use"):
                is_file_req = True
                if not target_filename and intent.get("target_file"):
                    target_filename = intent["target_file"]

        if max_words_limit is not None:
            set_thread_max_words(channel_id, thread_ts, max_words_limit)
        if min_words_limit is not None:
            set_thread_min_words(channel_id, thread_ts, min_words_limit)

        agent_key, prompt, was_explicit_target = parse_agent_and_prompt(
            raw_text_cleaned, channel_id, thread_ts
        )

        tagged = extract_tagged_agents(raw_text_cleaned)
        if tagged:
            agent_key = tagged[0]
            remaining_tagged = [a for a in tagged if a != agent_key]
            REMAINING_AGENTS_MAP[thread_key] = remaining_tagged
            CUSTOM_FLOW_THREADS.add(thread_key)
            was_explicit_target = True
        else:
            CUSTOM_FLOW_THREADS.discard(thread_key)

        if is_file_req:
            # Determine authorized agents:
            # If user explicitly tagged agents, only those tagged agents are authorized
            # If no agent was tagged, authorized_agents is None (all agents authorized)
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

    agent_cfg = AGENTS_CONFIG.get(agent_key, AGENTS_CONFIG[DEFAULT_AGENT_KEY])

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
        if current_cost >= threshold:
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
            return {
                "ok": True,
                "circuit_breaker_triggered": True,
                "slack_res": slack_res,
            }

    # 1. Fetch thread history context
    history = await fetch_thread_history(channel_id, thread_ts, bot_user_id=bot_user_id)
    logger.info(f"[ROUTER] Fetched {len(history)} messages from thread history.")

    if is_superseded():
        logger.info(f"[ROUTER] Thread {thread_key} turn was superseded. Aborting.")
        return {"ok": False, "cancelled": True, "reason": "superseded"}

    # Determine effective conversation history based on active /history limit
    if not is_bot_trigger:
        # User trigger: identify prior messages before this prompt in thread history
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
        # Bot trigger (subsequent automated debate rounds):
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

    # Determine if we should force termination (no more nominations)
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

    file_context = get_thread_file_context(channel_id, thread_ts)
    messages = build_llm_messages(
        system_prompt=system_prompt,
        effective_history=effective_history,
        prompt=prompt,
        is_bot_trigger=is_bot_trigger,
        agent_key=agent_key,
        agent_cfg=agent_cfg,
        file_context=file_context,
        remaining_agents=remaining,
        force_termination=force_termination,
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
        logger.info(f"[LLM] Dispatching litellm.completion for {agent_key}...")
        llm_start = time.time()
        completion_kwargs: dict[str, Any] = {
            "model": agent_cfg["model"],
            "messages": messages,
            "tools": MCP_TOOLS_SCHEMAS,
            "tool_choice": "auto",
            "num_retries": 2,
        }
        response = await execute_llm_with_retry(
            litellm.completion,
            completion_kwargs,
            agent_key=agent_key,
            thread_key=thread_key,
            is_superseded_fn=is_superseded,
            run_in_threadpool_fn=run_in_threadpool,
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
            # Append model's assistant message with tool calls
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

            # Follow-up completion after tool results in a threadpool with automatic retries
            follow_up_kwargs: dict[str, Any] = {
                "model": agent_cfg["model"],
                "messages": messages,
                "num_retries": 2,
            }
            follow_up = await execute_llm_with_retry(
                litellm.completion,
                follow_up_kwargs,
                agent_key=f"{agent_key} follow-up",
                thread_key=thread_key,
                is_superseded_fn=is_superseded,
                run_in_threadpool_fn=run_in_threadpool,
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

        # Re-check stop flag in case a stop was requested while LLM was processing
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
                if remaining:
                    next_tag = remaining[0]
                elif retries:
                    next_tag = retries[0]
                else:
                    next_tag = "OpAiTerra_Architect"
                final_text = f"(No text response generated) [{next_tag}] next"
            logger.warning(f"[ROUTER] final_text was empty for {agent_key}!")
        else:
            final_text = markdown_to_slack(final_text)
            final_text = enforce_valid_nomination(
                final_text,
                agent_key,
                [] if force_termination else remaining,
                force_termination,
                pending_retries=retries,
            )

        # If a pending retry agent was nominated, consume it from RATE_LIMIT_RETRY_QUEUE and track
        retries = RATE_LIMIT_RETRY_QUEUE.get(thread_key, [])
        if not remaining and retries:
            for r_agent in list(retries):
                if f"[{r_agent}]" in final_text:
                    retries.remove(r_agent)
                    RATE_LIMIT_ALREADY_RETRIED.setdefault(thread_key, set()).add(
                        r_agent
                    )
                    break

        # Append token usage to the end of the message text
        final_text = (
            f"{final_text.strip()}\n\n[{total_prompt_tokens}/{total_completion_tokens}]"
        )

        # Log the specific generated tag for the next agent
        generated_tags = re.findall(r"\[([a-zA-Z0-9_]+)\]", final_text)
        logger.info(
            f"[ROUTER] Agent {agent_key} generated tag(s): {generated_tags}. Full text length: {len(final_text)}"
        )

        # Prevent Slack WebSocket client saturation by adding a 3-second pacing delay.
        await asyncio.sleep(3.0)

        if is_superseded():
            logger.info(
                f"[ROUTER] Thread {thread_key} turn was superseded after pacing delay. Aborting post."
            )
            return {"ok": False, "cancelled": True, "reason": "superseded"}

        # 5. Post response back to Slack thread with dynamic persona
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

        # Short pause after Slack post to maintain pacing and avoid rate limits
        await asyncio.sleep(SLACK_POST_PAUSE_SECONDS)

        # Determine if this turn completes the thread discussion
        has_pending_retries = bool(RATE_LIMIT_RETRY_QUEUE.get(thread_key))
        is_completed = bool(
            force_termination
            or (
                agent_key == "OpAiTerra_Architect"
                and not remaining
                and not has_pending_retries
            )
            or (is_thread_stopped(channel_id, thread_ts))
        )

        # Record token consumption for this agent's model
        record_thread_tokens(
            channel_id=channel_id,
            thread_ts=thread_ts,
            model_name=agent_cfg["model"],
            prompt_tokens=total_prompt_tokens,
            completion_tokens=total_completion_tokens,
        )

        # Trigger parallel, non-blocking live thread notes update after posting reply
        trigger_thread_notes_update(
            channel_id=channel_id,
            thread_ts=thread_ts,
            is_completed=is_completed,
            bot_user_id=bot_user_id,
        )

        # Trigger parallel, non-blocking live usage report update in #cailogics-usage
        trigger_usage_update(
            channel_id=channel_id,
            thread_ts=thread_ts,
            is_completed=is_completed,
        )

        # Reset remaining agents/custom flows for this thread when finishing
        if force_termination:
            logger.info(
                f"[ROUTER] Thread {thread_key} reached termination (forced or natural). Cleaning up state."
            )
            clear_thread_state(channel_id, thread_ts)
        elif (
            agent_key == "OpAiTerra_Architect"
            and not remaining
            and not has_pending_retries
        ):
            logger.info(
                f"[ROUTER] OpAiTerra_Architect finished! Deleting state maps for thread {thread_key}"
            )
            clear_thread_history_limit(channel_id, thread_ts)
            clear_thread_max_words(channel_id, thread_ts)
            clear_thread_min_words(channel_id, thread_ts)
            clear_thread_file_context(channel_id, thread_ts)
            REMAINING_AGENTS_MAP.pop(thread_key, None)
            RATE_LIMIT_RETRY_QUEUE.pop(thread_key, None)
            RATE_LIMIT_ALREADY_RETRIED.pop(thread_key, None)

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

        # Build single sentence problem statement and manage rate-limit retry queue
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
        else:
            error_sentence = "Error processing request - check the logs"

        # Determine next nomination to ensure thread continuity
        next_nomination = None
        if not stop_requested:
            if remaining:
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
                elif agent_key != "OpAiTerra_Architect" and not (
                    was_explicit_target and not is_bot_trigger
                ):
                    next_nomination = "[OpAiTerra_Architect]"

        if next_nomination:
            error_post_text = f"{error_sentence}\n\n{next_nomination}"
        else:
            error_post_text = error_sentence

        # Post response back to Slack thread with the responsible agent's persona
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

        # Short pause after posting error message to prevent rapid cascading message spam
        await asyncio.sleep(SLACK_POST_PAUSE_SECONDS)

        has_pending_retries = bool(RATE_LIMIT_RETRY_QUEUE.get(thread_key))
        is_completed = bool(
            force_termination
            or (
                agent_key == "OpAiTerra_Architect"
                and not remaining
                and not has_pending_retries
            )
            or not next_nomination
            or is_thread_stopped(channel_id, thread_ts)
        )

        # Record 0 tokens for failed turn
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

        return {
            "ok": False,
            "error": str(e),
            "classified_error": err_category,
            "slack_res": slack_res,
        }
    finally:
        if current_task and ACTIVE_THREAD_TASKS.get(thread_key) == current_task:
            ACTIVE_THREAD_TASKS.pop(thread_key, None)
