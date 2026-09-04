"""
Turn Execution Pipeline for SlackAgenticPanel.

Extracts the monolithic turn processing logic from agent_router.py into a structured,
stage-based execution pipeline with an explicit TurnContext state container.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
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
    SLACK_POST_PAUSE_SECONDS,
)
from core.llm_utils import classify_agent_error, execute_llm_with_retry
from nlp.message_builder import (
    build_llm_messages,
    markdown_to_slack,
)
from prompts import build_agent_prompt
from pipeline.directive_parser import directive_parser
from pipeline.entity_resolver import entity_resolver
from pipeline.orchestrator import SandwichOrchestrator
from pipeline.spend_governance import spend_governance
from services.mcp_tools import MCP_TOOLS_SCHEMAS, execute_mcp_tool
from services.slack_client import (
    download_slack_file,
    fetch_thread_files,
    fetch_thread_history,
    post_agent_response,
    post_system_message,
)

from .agent_settings import (
    get_circuit_breaker_threshold,
    get_orchestration_mode,
    get_sandwich_config,
    is_circuit_breaker_enabled,
    settings_manager,
)
from .nomination import (
    enforce_valid_nomination,
    get_initial_agents,
    parse_agent_and_prompt,
    resolve_target_file,
)
from .thread_state import (
    clear_thread_file_context,
    clear_thread_history_limit,
    clear_thread_state,
    clear_thread_stop,
    get_thread_file_context,
    get_thread_history_limit,
    get_thread_max_words,
    get_thread_min_words,
    has_agent_503_errored,
    is_thread_stopped,
    mark_agent_503_errored,
    set_thread_file_context,
    set_thread_history_limit,
    set_thread_max_words,
    set_thread_min_words,
    thread_state_manager,
)

logger = logging.getLogger(__name__)


@dataclass
class TurnContext:
    """Encapsulates all runtime state and metadata for a single agent turn execution."""

    channel_id: str
    thread_ts: str
    raw_text: str
    bot_user_id: str | None = None
    icon_url: str | None = None
    is_bot_trigger: bool = False
    in_memory_history: list[dict[str, Any]] | None = None

    # Lifecycle state
    thread_key: tuple[str, str] = field(init=False)
    start_time: float = field(init=False)
    my_generation: int = 0
    current_task: asyncio.Task | None = None

    # Parsed directives & flags
    raw_text_cleaned: str = ""
    history_limit: int | None = None
    max_words_limit: int | None = None
    min_words_limit: int | None = None
    excluded_agents: list[str] = field(default_factory=list)
    is_file_req: bool = False
    target_filename: str | None = None
    thread_files: list[dict[str, Any]] = field(default_factory=list)

    # Resolved routing
    agent_key: str = ""
    prompt: str = ""
    was_explicit_target: bool = False
    agent_cfg: dict[str, Any] = field(default_factory=dict)
    remaining: list[str] = field(default_factory=list)
    is_custom_flow: bool = False
    force_termination: bool = False
    eligible_remaining_agents: list[str] = field(default_factory=list)
    next_designated_agent: str | None = None

    # LLM & Conversation Context
    effective_history: list[dict[str, Any]] = field(default_factory=list)
    system_prompt: str = ""
    file_context: dict[str, Any] | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)

    # Token accounting & Response
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    final_text: str = ""
    generated_tags: list[str] = field(default_factory=list)
    slack_res: dict[str, Any] | None = None
    is_completed: bool = False

    def __post_init__(self):
        self.thread_key = (self.channel_id, self.thread_ts)
        self.start_time = time.time()
        self.raw_text_cleaned = self.raw_text

    def is_superseded(self) -> bool:
        """Determines if this turn was superseded by a newer query or task cancellation."""
        if thread_state_manager.get_generation(self.channel_id, self.thread_ts) != self.my_generation:
            return True
        if self.current_task and self.current_task.cancelled():
            return True
        return False

    def add_usage(self, resp: Any):
        """Accumulates prompt and completion token counts from an LLM response."""
        if not resp:
            return
        usage = getattr(resp, "usage", None)
        if not usage:
            return
        if isinstance(usage, dict):
            self.total_prompt_tokens += usage.get("prompt_tokens", 0) or 0
            self.total_completion_tokens += usage.get("completion_tokens", 0) or 0
        else:
            self.total_prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
            self.total_completion_tokens += getattr(usage, "completion_tokens", 0) or 0


class TurnPipeline:
    """
    Executes an agent turn through structured stages:
      1. Validation & Directive Parsing
      2. Agent Resolution & Spend Governance
      3. History Slicing & Prompt Construction
      4. LLM Completion & Tool Invocation Loop
      5. Nomination Enforcement & Slack Dispatch
    """

    def __init__(self, router_module=None):
        self._router_module = router_module

    @property
    def router(self):
        if self._router_module is not None:
            return self._router_module
        return sys.modules.get("orchestration.agent_router")

    def _get_hook(self, name: str, default: Any = None) -> Any:
        """Dynamically retrieves a function or constant from the agent_router module to preserve monkeypatches."""
        r = self.router
        if r is not None and hasattr(r, name):
            return getattr(r, name)
        return default

    async def _sleep(self, seconds: float):
        """Asynchronous sleep respecting any monkeypatch on agent_router.asyncio.sleep."""
        r = self.router
        sleep_fn = getattr(getattr(r, "asyncio", asyncio), "sleep", asyncio.sleep)
        await sleep_fn(seconds)

    async def run(self, ctx: TurnContext) -> dict[str, Any]:
        """Runs the entire turn execution lifecycle."""
        logger.info(
            f"\n[{ctx.start_time}] >>> START process_and_respond for thread_ts: {ctx.thread_ts} <<<"
        )

        if not ctx.is_bot_trigger:
            thread_state_manager.increment_generation(ctx.channel_id, ctx.thread_ts)

        ctx.my_generation = thread_state_manager.get_generation(ctx.channel_id, ctx.thread_ts)
        ctx.current_task = asyncio.current_task()
        if ctx.current_task:
            thread_state_manager.set_active_task(ctx.channel_id, ctx.thread_ts, ctx.current_task)

        try:
            # Stage 1: Validate input, parse directives & resolve candidate agents
            halt = await self.stage_validate_and_parse(ctx)
            if halt:
                return halt

            # Stage 2: Resolve agent config & spend governance (circuit breaker)
            halt = await self.stage_resolve_agent_and_governance(ctx)
            if halt:
                return halt

            # Stage 3: Prepare conversation context, history slicing & prompt assembly
            halt = await self.stage_prepare_context(ctx)
            if halt:
                return halt

            # Stage 4: Execute LLM turn and handle any tool calls
            halt = await self.stage_execute_llm(ctx)
            if halt:
                return halt

            # Stage 5: Format response, enforce nominations, post to Slack, and audit
            return await self.stage_post_and_record(ctx)

        except asyncio.CancelledError:
            logger.info(
                f"[ROUTER] Task cancelled for thread {ctx.thread_key} (agent {ctx.agent_key}, gen {ctx.my_generation}). Aborting cleanly."
            )
            return {"ok": False, "cancelled": True, "reason": "cancelled"}
        except Exception as e:
            return await self.handle_error(ctx, e)
        finally:
            if (
                ctx.current_task
                and thread_state_manager.get_active_task(ctx.channel_id, ctx.thread_ts) == ctx.current_task
            ):
                if ctx.in_memory_history is None or ctx.is_completed:
                    thread_state_manager.clear_active_task(ctx.channel_id, ctx.thread_ts, ctx.current_task)

    async def stage_validate_and_parse(
        self, ctx: TurnContext
    ) -> dict[str, Any] | None:
        """Stage 1: Directives, file inspection, tag validation, and candidate exclusions."""
        directives = await directive_parser.parse(ctx.raw_text)
        ctx.history_limit = directives.history_limit
        ctx.max_words_limit = directives.max_words
        ctx.min_words_limit = directives.min_words
        ctx.excluded_agents = directives.excluded_agents
        ctx.is_file_req = directives.is_file_req
        ctx.target_filename = directives.target_filename
        ctx.raw_text_cleaned = directives.cleaned_prompt

        post_sys_msg = self._get_hook("post_system_message", post_system_message)

        if not ctx.is_bot_trigger:
            clear_thread_stop(ctx.channel_id, ctx.thread_ts)
            fetch_files_fn = self._get_hook(
                "fetch_thread_files", fetch_thread_files
            )
            ctx.thread_files = await fetch_files_fn(
                ctx.channel_id, ctx.thread_ts
            )
            if ctx.thread_files:
                directives = await directive_parser.parse(
                    ctx.raw_text, available_files=ctx.thread_files
                )
                ctx.history_limit = directives.history_limit
                ctx.max_words_limit = directives.max_words
                ctx.min_words_limit = directives.min_words
                ctx.excluded_agents = directives.excluded_agents
                ctx.is_file_req = directives.is_file_req
                ctx.target_filename = directives.target_filename
                ctx.raw_text_cleaned = directives.cleaned_prompt

            if ctx.max_words_limit is not None:
                set_thread_max_words(
                    ctx.channel_id, ctx.thread_ts, ctx.max_words_limit
                )
            if ctx.min_words_limit is not None:
                set_thread_min_words(
                    ctx.channel_id, ctx.thread_ts, ctx.min_words_limit
                )

            ambiguous_tags = entity_resolver.detect_ambiguous_tags(
                ctx.raw_text_cleaned
            )
            if ambiguous_tags:
                logger.warning(
                    f"[ROUTER] Ambiguous agent tag(s) detected in user prompt for thread {ctx.thread_key}: {ambiguous_tags}. Halting turn."
                )
                lines = ["⚠️ *Ambiguous Agent Reference*:"]
                for tag_str, candidates in ambiguous_tags:
                    clean_tag = tag_str.strip("[](){}:,; \t*~_")
                    lines.append(
                        f"Tag '{clean_tag}' matches multiple candidates:"
                    )
                    for cand in candidates:
                        cfg = (
                            settings_manager.agents_config or AGENTS_CONFIG
                        ).get(cand, {})
                        cand_username = cfg.get("username", cand)
                        lines.append(f"• {cand} ({cand_username})")
                first_suggestion = ambiguous_tags[0][1][0]
                lines.append(
                    f"\nPlease specify the exact agent name (e.g. '{first_suggestion}') to proceed."
                )
                warning_msg = "\n".join(lines)

                await post_sys_msg(
                    channel_id=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text=warning_msg,
                )
                return {
                    "ok": False,
                    "halted": True,
                    "reason": "ambiguous_agent",
                    "ambiguous_tags": ambiguous_tags,
                }

            unrecognized_tags = entity_resolver.detect_unrecognized_tags(
                ctx.raw_text_cleaned
            )
            if unrecognized_tags:
                logger.warning(
                    f"[ROUTER] Unrecognized agent tag(s) detected in user prompt for thread {ctx.thread_key}: {unrecognized_tags}. Halting turn."
                )
                lines = ["⚠️ *Unrecognized Agent Tag*:"]
                for tag_str in unrecognized_tags:
                    clean_tag = tag_str.strip("[](){}:,; \t*~_")
                    lines.append(
                        f"Tag '{clean_tag}' could not be matched to any active agent."
                    )
                lines.append(
                    "\nPlease check the agent name or tag a valid agent (e.g. 'Visionary', 'TechLead', 'Innovator') to proceed."
                )
                warning_msg = "\n".join(lines)

                await post_sys_msg(
                    channel_id=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text=warning_msg,
                )
                return {
                    "ok": False,
                    "halted": True,
                    "reason": "unrecognized_agent",
                    "unrecognized_tags": unrecognized_tags,
                }

            agent_key, prompt, was_explicit_target = parse_agent_and_prompt(
                ctx.raw_text_cleaned, ctx.channel_id, ctx.thread_ts
            )
            ctx.agent_key = agent_key
            ctx.prompt = prompt
            ctx.was_explicit_target = was_explicit_target

            tagged = entity_resolver.extract_tagged_agents(ctx.raw_text_cleaned)
            if tagged:
                if ctx.excluded_agents:
                    tagged = [a for a in tagged if a not in ctx.excluded_agents]
                if tagged:
                    ctx.agent_key = tagged[0]
                    remaining_tagged = [a for a in tagged if a != ctx.agent_key]
                    thread_state_manager.set_remaining_agents(
                        ctx.channel_id, ctx.thread_ts, remaining_tagged
                    )
                    thread_state_manager.set_custom_flow(
                        ctx.channel_id, ctx.thread_ts, True
                    )
                    ctx.was_explicit_target = True
                else:
                    logger.warning(
                        f"[ROUTER] All tagged agents in thread {ctx.thread_key} were excluded. Halting turn."
                    )
                    warning_msg = "⚠️ *All tagged agents were excluded*: None of the specified agents can respond to this request."
                    await post_sys_msg(
                        channel_id=ctx.channel_id,
                        thread_ts=ctx.thread_ts,
                        text=warning_msg,
                    )
                    return {
                        "ok": True,
                        "halted": True,
                        "reason": "all_tagged_agents_excluded",
                    }
            else:
                thread_state_manager.clear_custom_flow(ctx.channel_id, ctx.thread_ts)
                if ctx.excluded_agents:
                    curr_remaining = thread_state_manager.get_remaining_agents(
                        ctx.channel_id, ctx.thread_ts, default=get_initial_agents()
                    )
                    curr_remaining = [
                        a for a in curr_remaining if a not in ctx.excluded_agents
                    ]
                    thread_state_manager.set_remaining_agents(
                        ctx.channel_id, ctx.thread_ts, curr_remaining
                    )
                    if ctx.agent_key in ctx.excluded_agents:
                        if curr_remaining:
                            ctx.agent_key = curr_remaining.pop(0)
                            thread_state_manager.set_remaining_agents(
                                ctx.channel_id, ctx.thread_ts, curr_remaining
                            )
                        else:
                            logger.warning(
                                f"[ROUTER] Agent '{ctx.agent_key}' and all remaining agents in thread {ctx.thread_key} are excluded. Halting turn."
                            )
                            warning_msg = f"⚠️ *Agent Excluded*: Agent '{ctx.agent_key}' was excluded and no other agents are available to continue."
                            await post_sys_msg(
                                channel_id=ctx.channel_id,
                                thread_ts=ctx.thread_ts,
                                text=warning_msg,
                            )
                            return {
                                "ok": True,
                                "halted": True,
                                "reason": "all_agents_excluded",
                            }

            if ctx.is_file_req:
                if tagged:
                    authorized_agents = set(tagged)
                elif ctx.was_explicit_target:
                    authorized_agents = {ctx.agent_key}
                else:
                    authorized_agents = None

                chosen_file = resolve_target_file(
                    ctx.thread_files, ctx.target_filename
                )
                if chosen_file:
                    logger.info(
                        f"[ROUTER] User requested file reading for thread {ctx.thread_key}. Downloading file: {chosen_file.get('name')}"
                    )
                    download_file_fn = self._get_hook(
                        "download_slack_file", download_slack_file
                    )
                    dl_res = await download_file_fn(chosen_file)
                    if dl_res.get("ok"):
                        set_thread_file_context(
                            ctx.channel_id,
                            ctx.thread_ts,
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
                            ctx.channel_id,
                            ctx.thread_ts,
                            {
                                "file_name": chosen_file.get(
                                    "name", "unknown"
                                ),
                                "content": f"[Error reading file: {dl_res.get('error')}]",
                                "size": chosen_file.get("size", 0),
                                "truncated": False,
                                "authorized_agents": authorized_agents,
                            },
                        )
                else:
                    logger.warning(
                        f"[ROUTER] User requested file reading but no files were found in thread {ctx.thread_key}"
                    )
                    set_thread_file_context(
                        ctx.channel_id,
                        ctx.thread_ts,
                        {
                            "file_name": ctx.target_filename or "unknown",
                            "content": "[No attached file found in this Slack thread to read.]",
                            "size": 0,
                            "truncated": False,
                            "authorized_agents": authorized_agents,
                        },
                    )
            else:
                clear_thread_file_context(ctx.channel_id, ctx.thread_ts)
        else:
            agent_key, prompt, was_explicit_target = parse_agent_and_prompt(
                ctx.raw_text_cleaned, ctx.channel_id, ctx.thread_ts
            )
            ctx.agent_key = agent_key
            ctx.prompt = prompt
            ctx.was_explicit_target = was_explicit_target

        if not ctx.agent_key:
            logger.info(
                f"[ROUTER] No agent available to speak in thread {ctx.thread_key}. Halting turn."
            )
            enabled = settings_manager.get_enabled_agents()
            if not enabled:
                await post_sys_msg(
                    channel_id=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text="⚠️ *No Agents Enabled*: There are currently no enabled agents in the panel to respond.",
                )
            return {
                "ok": True,
                "halted": True,
                "reason": "no_agents_available",
            }

        return None

    async def stage_resolve_agent_and_governance(
        self, ctx: TurnContext
    ) -> dict[str, Any] | None:
        """Stage 2: Agent config lookup, fallback rotation, and circuit breaker verification."""
        post_sys_msg = self._get_hook("post_system_message", post_system_message)
        agents_map = settings_manager.agents_config or AGENTS_CONFIG

        if ctx.agent_key not in agents_map:
            logger.error(
                f"[ROUTER] Agent '{ctx.agent_key}' not found in configuration."
            )
            next_candidate = thread_state_manager.pop_remaining_agent(
                ctx.channel_id, ctx.thread_ts
            )
            if not next_candidate:
                mode = settings_manager.get_orchestration_mode()
                if mode == "sandwich":
                    end_chain = [
                        k
                        for k in settings_manager.get_sandwich_config().get(
                            "end_chain", []
                        )
                        if k in agents_map
                    ]
                    if end_chain:
                        next_candidate = end_chain[0]
                if not next_candidate:
                    available = [
                        k
                        for k in settings_manager.get_enabled_agents()
                        if k in agents_map and k != ctx.agent_key
                    ]
                    if available:
                        next_candidate = random.choice(available)

            if next_candidate:
                warning_msg = (
                    f"⚠️ *Configuration Error*: Agent '{ctx.agent_key}' is not configured. "
                    f"Handing off to [{next_candidate}]."
                )
                await post_sys_msg(
                    channel_id=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text=warning_msg,
                )
                ctx.agent_key = next_candidate
                ctx.agent_cfg = agents_map[ctx.agent_key]
            else:
                warning_msg = (
                    f"⚠️ *Configuration Error*: Agent '{ctx.agent_key}' is not configured and no other agents are available."
                )
                await post_sys_msg(
                    channel_id=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text=warning_msg,
                )
                return {
                    "ok": False,
                    "halted": True,
                    "reason": "agent_not_configured",
                }
        else:
            ctx.agent_cfg = agents_map[ctx.agent_key]

        # Ensure agent's configured avatar icon is propagated to turn context
        if not ctx.icon_url and ctx.agent_cfg.get("icon_url"):
            ctx.icon_url = ctx.agent_cfg.get("icon_url")

        logger.info(
            f"[ROUTER] Parsed Agent: {ctx.agent_key} ({ctx.agent_cfg['model']}) | Explicit target: {ctx.was_explicit_target} | Bot trigger: {ctx.is_bot_trigger} | Gen: {ctx.my_generation} | History limit: {ctx.history_limit}"
        )

        if ctx.is_superseded():
            logger.info(
                f"[ROUTER] Thread {ctx.thread_key} turn was superseded. Aborting."
            )
            return {"ok": False, "cancelled": True, "reason": "superseded"}

        # Circuit breaker verification
        if is_circuit_breaker_enabled():
            calc_costs_fn = self._get_hook(
                "calculate_thread_costs", calculate_thread_costs
            )
            cost_info = calc_costs_fn(ctx.channel_id, ctx.thread_ts)
            current_cost = cost_info.get("grand_total_cost_usd", 0.0)
            threshold = get_circuit_breaker_threshold()
            session_id = f"{ctx.channel_id}:{ctx.thread_ts}"
            tripped, current_cost, threshold = (
                spend_governance.is_budget_exceeded(
                    session_id=session_id,
                    threshold_usd=threshold,
                    enabled=True,
                    current_cost_usd=current_cost,
                )
            )
            if tripped:
                logger.warning(
                    f"[CIRCUIT BREAKER] Thread {ctx.thread_key} cost (${current_cost:.5f}) reached or exceeded threshold (${threshold:.5f}). Interrupting response."
                )
                circuit_breaker_msg = (
                    "EXPENSE LIMIT REACHED FOR THE THREAD. ENDING CALLS"
                )
                post_resp_fn = self._get_hook(
                    "post_agent_response", post_agent_response
                )
                slack_res = await post_resp_fn(
                    channel_id=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text=circuit_breaker_msg,
                    username=ctx.agent_cfg["username"],
                    icon_url=ctx.agent_cfg.get("icon_url"),
                )
                pause_secs = self._get_hook(
                    "SLACK_POST_PAUSE_SECONDS", SLACK_POST_PAUSE_SECONDS
                )
                await self._sleep(pause_secs)

                trig_notes = self._get_hook(
                    "trigger_thread_notes_update", trigger_thread_notes_update
                )
                trig_notes(
                    channel_id=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    is_completed=True,
                    bot_user_id=ctx.bot_user_id,
                )

                trig_usage = self._get_hook(
                    "trigger_usage_update", trigger_usage_update
                )
                trig_usage(
                    channel_id=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    is_completed=True,
                )

                clear_thread_state(ctx.channel_id, ctx.thread_ts)
                if isinstance(slack_res, dict):
                    slack_res["is_completed"] = True
                    slack_res["final_text"] = circuit_breaker_msg
                    slack_res["username"] = ctx.agent_cfg["username"]
                    slack_res["agent_key"] = ctx.agent_key
                return {
                    "ok": True,
                    "circuit_breaker_triggered": True,
                    "is_completed": True,
                    "slack_res": slack_res,
                    "final_text": circuit_breaker_msg,
                    "username": ctx.agent_cfg["username"],
                    "agent_key": ctx.agent_key,
                }

        return None

    async def stage_prepare_context(
        self, ctx: TurnContext
    ) -> dict[str, Any] | None:
        """Stage 3: Fetch thread history, apply history/word limits, and construct LLM prompts."""
        if ctx.in_memory_history is not None:
            history = list(ctx.in_memory_history)
            logger.info(
                f"[ROUTER] Using in-memory history with {len(history)} messages."
            )
        else:
            fetch_hist_fn = self._get_hook(
                "fetch_thread_history", fetch_thread_history
            )
            history = await fetch_hist_fn(
                ctx.channel_id, ctx.thread_ts, bot_user_id=ctx.bot_user_id
            )
            logger.info(
                f"[ROUTER] Fetched {len(history)} messages from thread history."
            )

        if ctx.is_superseded():
            logger.info(
                f"[ROUTER] Thread {ctx.thread_key} turn was superseded. Aborting."
            )
            return {"ok": False, "cancelled": True, "reason": "superseded"}

        if not ctx.is_bot_trigger:
            if history and history[-1]["role"] == "user":
                prior_messages = history[:-1]
            else:
                prior_messages = history

            if ctx.history_limit is not None:
                set_thread_history_limit(
                    channel_id=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    limit=ctx.history_limit,
                    prior_cutoff_index=len(prior_messages),
                )
                effective_prior = (
                    prior_messages[-ctx.history_limit :]
                    if ctx.history_limit > 0
                    else []
                )
                logger.info(
                    f"[ROUTER] Applying /history {ctx.history_limit}: kept {len(effective_prior)} of {len(prior_messages)} prior messages for user prompt."
                )
            else:
                clear_thread_history_limit(ctx.channel_id, ctx.thread_ts)
                effective_prior = prior_messages

            ctx.effective_history = effective_prior
        else:
            hist_limit_info = get_thread_history_limit(
                ctx.channel_id, ctx.thread_ts
            )
            if hist_limit_info is not None:
                limit = hist_limit_info["limit"]
                cutoff = hist_limit_info["prior_cutoff_index"]
                prior_part = history[:cutoff]
                discussion_part = history[cutoff:]
                effective_prior = prior_part[-limit:] if limit > 0 else []
                ctx.effective_history = effective_prior + discussion_part
                logger.info(
                    f"[ROUTER] Applying active history limit ({limit}): kept {len(effective_prior)}/{len(prior_part)} prior messages + {len(discussion_part)} discussion messages."
                )
            else:
                ctx.effective_history = history

        # System prompt generation
        active_max_words = get_thread_max_words(ctx.channel_id, ctx.thread_ts)
        active_min_words = get_thread_min_words(ctx.channel_id, ctx.thread_ts)
        if active_max_words is not None or active_min_words is not None:
            logger.info(
                f"[ROUTER] Applying word limits (min_words={active_min_words}, max_words={active_max_words}) for agent {ctx.agent_key} (skipping standard output_constraints)."
            )
            ctx.system_prompt = build_agent_prompt(
                ctx.agent_key,
                max_words=active_max_words,
                min_words=active_min_words,
            )
        else:
            ctx.system_prompt = ctx.agent_cfg["system_prompt"]

        ctx.remaining = thread_state_manager.get_remaining_agents(
            ctx.channel_id, ctx.thread_ts, default=get_initial_agents()
        )
        ctx.is_custom_flow = thread_state_manager.is_custom_flow(
            ctx.channel_id, ctx.thread_ts
        )

        ctx.force_termination = False
        if (
            ctx.was_explicit_target
            and not ctx.is_bot_trigger
            and not ctx.remaining
        ):
            ctx.force_termination = True
        elif ctx.is_custom_flow and not ctx.remaining:
            ctx.force_termination = True
        elif is_thread_stopped(ctx.channel_id, ctx.thread_ts):
            ctx.force_termination = True

        if ctx.force_termination:
            logger.info(
                f"[ROUTER] Agent {ctx.agent_key} forcing termination (no remaining agents in sequence or stop requested)."
            )
        else:
            logger.info(
                f"[ROUTER] Agent {ctx.agent_key} remaining available agents: {ctx.remaining}"
            )

        mode = get_orchestration_mode()
        ctx.next_designated_agent = None
        ctx.eligible_remaining_agents = list(ctx.remaining)
        if not ctx.force_termination and ctx.remaining:
            orchestrator = SandwichOrchestrator(
                mode=mode,
                start_chain=get_sandwich_config().get("start_chain", []),
                end_chain=get_sandwich_config().get("end_chain", []),
                fixed_chain=settings_manager.get_fixed_config().get(
                    "chain", []
                ),
                agents_map=settings_manager.agents_config or AGENTS_CONFIG,
            )
            (
                ctx.next_designated_agent,
                ctx.eligible_remaining_agents,
            ) = orchestrator.get_phase_info(
                ctx.remaining, is_custom_flow=ctx.is_custom_flow
            )

        ctx.file_context = get_thread_file_context(
            ctx.channel_id, ctx.thread_ts
        )
        ctx.messages = build_llm_messages(
            system_prompt=ctx.system_prompt,
            effective_history=ctx.effective_history,
            prompt=ctx.prompt,
            is_bot_trigger=ctx.is_bot_trigger,
            agent_key=ctx.agent_key,
            agent_cfg=ctx.agent_cfg,
            file_context=ctx.file_context,
            remaining_agents=ctx.eligible_remaining_agents,
            force_termination=ctx.force_termination,
            next_designated_agent=ctx.next_designated_agent,
        )
        return None

    async def stage_execute_llm(self, ctx: TurnContext) -> dict[str, Any] | None:
        """Stage 4: Execute model completion and handle tool calling loop."""
        if ctx.is_superseded():
            logger.info(
                f"[ROUTER] Thread {ctx.thread_key} turn was superseded before LLM call. Aborting."
            )
            return {"ok": False, "cancelled": True, "reason": "superseded"}

        already_503_errored = has_agent_503_errored(
            ctx.channel_id, ctx.thread_ts, ctx.agent_key
        )
        allow_503_immediate = already_503_errored

        logger.info(
            f"[LLM] Dispatching litellm.completion for {ctx.agent_key} (already_503_errored={already_503_errored})..."
        )
        llm_start = time.time()
        completion_kwargs: dict[str, Any] = {
            "model": ctx.agent_cfg["model"],
            "messages": ctx.messages,
            "tools": MCP_TOOLS_SCHEMAS,
            "tool_choice": "auto",
            "num_retries": 2,
        }
        if any(
            keyword in ctx.agent_cfg["model"].lower()
            for keyword in ["muse", "llama", "meta"]
        ):
            if os.getenv("META_API_KEY"):
                completion_kwargs["api_key"] = os.getenv("META_API_KEY")
            if os.getenv("META_API_BASE"):
                completion_kwargs["api_base"] = os.getenv("META_API_BASE")

        exec_retry_fn = self._get_hook(
            "execute_llm_with_retry", execute_llm_with_retry
        )
        threadpool_fn = self._get_hook("run_in_threadpool", None)
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
            return {"ok": False, "cancelled": True, "reason": "superseded"}

        response_message = response.choices[0].message
        tool_calls = getattr(response_message, "tool_calls", None)

        if tool_calls:
            ctx.messages.append(response_message.model_dump())
            exec_mcp_fn = self._get_hook("execute_mcp_tool", execute_mcp_tool)

            for tool_call in tool_calls:
                fn_name = tool_call.function.name
                fn_args = (
                    json.loads(tool_call.function.arguments)
                    if isinstance(tool_call.function.arguments, str)
                    else tool_call.function.arguments
                )
                tool_result = exec_mcp_fn(fn_name, fn_args)

                ctx.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": fn_name,
                        "content": tool_result,
                    }
                )

            if ctx.is_superseded():
                logger.info(
                    f"[ROUTER] Thread {ctx.thread_key} turn was superseded during tool execution. Aborting."
                )
                return {"ok": False, "cancelled": True, "reason": "superseded"}

            follow_up_kwargs: dict[str, Any] = {
                "model": ctx.agent_cfg["model"],
                "messages": ctx.messages,
                "num_retries": 2,
            }
            if any(
                keyword in ctx.agent_cfg["model"].lower()
                for keyword in ["muse", "llama", "meta"]
            ):
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
                return {"ok": False, "cancelled": True, "reason": "superseded"}

            ctx.add_usage(follow_up)
            ctx.final_text = follow_up.choices[0].message.content
        else:
            ctx.final_text = response_message.content

        return None

    async def stage_post_and_record(self, ctx: TurnContext) -> dict[str, Any]:
        """Stage 5: Nomination enforcement, token accounting, Slack posting, and notifications."""
        if ctx.is_superseded():
            logger.info(
                f"[ROUTER] Thread {ctx.thread_key} turn was superseded before pacing delay. Aborting."
            )
            return {"ok": False, "cancelled": True, "reason": "superseded"}

        if is_thread_stopped(ctx.channel_id, ctx.thread_ts):
            ctx.force_termination = True
            logger.info(
                f"[ROUTER] Thread {ctx.thread_key} received stop request during execution. Forcing termination."
            )

        retries = thread_state_manager.get_retry_queue(
            ctx.channel_id, ctx.thread_ts
        )
        if not ctx.final_text:
            if ctx.force_termination:
                ctx.final_text = "(No text response generated)"
            else:
                if ctx.next_designated_agent:
                    next_tag = ctx.next_designated_agent
                    ctx.final_text = (
                        f"(No text response generated) [{next_tag}] next"
                    )
                elif ctx.eligible_remaining_agents:
                    next_tag = ctx.eligible_remaining_agents[0]
                    ctx.final_text = (
                        f"(No text response generated) [{next_tag}] next"
                    )
                elif retries:
                    next_tag = retries[0]
                    ctx.final_text = (
                        f"(No text response generated) [{next_tag}] next"
                    )
                else:
                    ctx.final_text = "(No text response generated)"
                    ctx.force_termination = True
            logger.warning(f"[ROUTER] final_text was empty for {ctx.agent_key}!")
        else:
            ctx.final_text = markdown_to_slack(ctx.final_text)
            ctx.final_text = enforce_valid_nomination(
                ctx.final_text,
                ctx.agent_key,
                []
                if ctx.force_termination
                else ctx.eligible_remaining_agents,
                ctx.force_termination,
                pending_retries=retries,
                designated_next_agent=ctx.next_designated_agent,
            )

        retries = thread_state_manager.get_retry_queue(
            ctx.channel_id, ctx.thread_ts
        )
        if not ctx.remaining and retries:
            for r_agent in list(retries):
                if f"[{r_agent}]" in ctx.final_text:
                    thread_state_manager.pop_retry_agent(
                        ctx.channel_id, ctx.thread_ts, r_agent
                    )
                    thread_state_manager.mark_already_retried(
                        ctx.channel_id, ctx.thread_ts, r_agent
                    )
                    break

        agent_model = ctx.agent_cfg.get("model", "")
        if agent_model:
            token_footer = f"{{{agent_model} : {ctx.total_prompt_tokens}/{ctx.total_completion_tokens}}}"
        else:
            token_footer = (
                f"{{{ctx.total_prompt_tokens}/{ctx.total_completion_tokens}}}"
            )

        ctx.final_text = f"{ctx.final_text}\n\n{token_footer}"
        ctx.generated_tags = re.findall(
            r"[\[{]\s*([^\]}\r\n]+?)\s*[\]}]", ctx.final_text
        )
        logger.info(
            f"[ROUTER] Agent {ctx.agent_key} generated tag(s): {ctx.generated_tags}. Full text length: {len(ctx.final_text)}"
        )

        pacing_secs = self._get_hook(
            "DEBATE_TURN_PACING_SECONDS", DEBATE_TURN_PACING_SECONDS
        )
        await self._sleep(pacing_secs)

        if ctx.is_superseded():
            logger.info(
                f"[ROUTER] Thread {ctx.thread_key} turn was superseded after pacing delay. Aborting post."
            )
            return {"ok": False, "cancelled": True, "reason": "superseded"}

        resolved_turn_icon = ctx.icon_url or ctx.agent_cfg.get("icon_url")
        logger.info(
            f"[SLACK] Posting agent response to channel {ctx.channel_id}, thread_ts {ctx.thread_ts} as {ctx.agent_cfg['username']} (icon: {resolved_turn_icon})"
        )
        post_resp_fn = self._get_hook(
            "post_agent_response", post_agent_response
        )
        slack_res = await post_resp_fn(
            channel_id=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            text=ctx.final_text,
            username=ctx.agent_cfg["username"],
            icon_url=resolved_turn_icon,
        )
        posted_ts = slack_res.get("ts") if isinstance(slack_res, dict) else None
        logger.info(f"[SLACK] Successfully posted message! Received ts: {posted_ts}")

        pause_secs = self._get_hook(
            "SLACK_POST_PAUSE_SECONDS", SLACK_POST_PAUSE_SECONDS
        )
        await self._sleep(pause_secs)

        has_pending_retries = thread_state_manager.has_pending_retries(
            ctx.channel_id, ctx.thread_ts
        )
        agents_map = settings_manager.agents_config or AGENTS_CONFIG
        has_nominated_next = any(tag in agents_map for tag in ctx.generated_tags)
        ctx.is_completed = bool(
            ctx.force_termination
            or (
                not ctx.remaining
                and not has_pending_retries
                and not has_nominated_next
            )
            or (is_thread_stopped(ctx.channel_id, ctx.thread_ts))
        )

        record_tokens_fn = self._get_hook(
            "record_thread_tokens", record_thread_tokens
        )
        record_tokens_fn(
            channel_id=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            model_name=ctx.agent_cfg["model"],
            prompt_tokens=ctx.total_prompt_tokens,
            completion_tokens=ctx.total_completion_tokens,
        )

        trig_notes = self._get_hook(
            "trigger_thread_notes_update", trigger_thread_notes_update
        )
        trig_notes(
            channel_id=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            is_completed=ctx.is_completed,
            bot_user_id=ctx.bot_user_id,
        )

        trig_usage = self._get_hook(
            "trigger_usage_update", trigger_usage_update
        )
        trig_usage(
            channel_id=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            is_completed=ctx.is_completed,
        )

        if ctx.is_completed:
            logger.info(
                f"[ROUTER] Thread {ctx.thread_key} reached termination (forced or natural). Cleaning up state."
            )
            clear_thread_state(ctx.channel_id, ctx.thread_ts)

        if isinstance(slack_res, dict):
            slack_res["is_completed"] = ctx.is_completed
            slack_res["final_text"] = ctx.final_text
            slack_res["username"] = ctx.agent_cfg["username"]
            slack_res["agent_key"] = ctx.agent_key
            slack_res["icon_url"] = resolved_turn_icon
            slack_res["generated_tags"] = ctx.generated_tags

        logger.info(
            f"[{time.time()}] <<< END process_and_respond for thread_ts: {ctx.thread_ts} (Total: {time.time() - ctx.start_time:.2f}s) <<<\n"
        )
        return slack_res

    async def handle_error(
        self, ctx: TurnContext, e: Exception
    ) -> dict[str, Any]:
        """Classifies and handles exceptions, performing 503 rotation or rate-limit retry scheduling."""
        if ctx.is_superseded():
            logger.info(
                f"[ROUTER] Exception occurred on superseded turn for thread {ctx.thread_key}. Aborting without posting."
            )
            return {"ok": False, "cancelled": True, "reason": "superseded"}

        logger.exception(f"[ERROR] Exception during agent completion or post: {e}")
        err_category = classify_agent_error(e)
        logger.info(
            f"[ROUTER] Classified error for {ctx.agent_key} as: '{err_category}'"
        )

        stop_requested = is_thread_stopped(ctx.channel_id, ctx.thread_ts)
        already_503 = has_agent_503_errored(
            ctx.channel_id, ctx.thread_ts, ctx.agent_key
        )

        is_first_503_skip = False
        if err_category == "api_key_not_found":
            error_sentence = "API KEY NOT FOUND"
        elif err_category == "model_not_found":
            error_sentence = "MODEL NOT FOUND"
        elif err_category == "rate_limit":
            already_retried = thread_state_manager.is_already_retried(
                ctx.channel_id, ctx.thread_ts, ctx.agent_key
            )
            if not already_retried and not stop_requested:
                error_sentence = (
                    "RATE LIMITS CROSSED, will retry at the end of discussion"
                )
                thread_state_manager.add_to_retry_queue(
                    ctx.channel_id, ctx.thread_ts, ctx.agent_key
                )
            else:
                error_sentence = "RATE LIMITS CROSSED"
        elif err_category == "service_unavailable":
            if (
                not already_503
                and not stop_requested
                and not ctx.force_termination
            ):
                mark_agent_503_errored(
                    ctx.channel_id, ctx.thread_ts, ctx.agent_key
                )
                if ctx.agent_key not in ctx.remaining:
                    mode = settings_manager.get_orchestration_mode()
                    if mode == "sandwich":
                        sandwich_cfg = settings_manager.get_sandwich_config()
                        end_chain = sandwich_cfg.get("end_chain", [])
                        end_chain_indices = [
                            i
                            for i, k in enumerate(ctx.remaining)
                            if k in end_chain
                        ]
                        if end_chain_indices and ctx.agent_key not in end_chain:
                            ctx.remaining.insert(
                                end_chain_indices[0], ctx.agent_key
                            )
                        else:
                            ctx.remaining.append(ctx.agent_key)
                    else:
                        ctx.remaining.append(ctx.agent_key)
                thread_state_manager.set_remaining_agents(
                    ctx.channel_id, ctx.thread_ts, ctx.remaining
                )
                other_candidates = [
                    a for a in ctx.remaining if a != ctx.agent_key
                ]
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
                other_candidates = [
                    a for a in ctx.remaining if a != ctx.agent_key
                ]
                if other_candidates:
                    next_agent = random.choice(other_candidates)
                    next_nomination = f"[{next_agent}]"
                elif ctx.remaining:
                    next_agent = ctx.remaining[0]
                    next_nomination = f"[{next_agent}]"
            elif ctx.remaining:
                next_agent = ctx.remaining[0]
                next_nomination = f"[{next_agent}]"
            else:
                next_agent = thread_state_manager.pop_retry_agent(
                    ctx.channel_id, ctx.thread_ts
                )
                if next_agent:
                    thread_state_manager.mark_already_retried(
                        ctx.channel_id, ctx.thread_ts, next_agent
                    )
                    next_nomination = f"[{next_agent}]"

        if next_nomination:
            error_post_text = f"{error_sentence}\n\n{next_nomination}"
        else:
            error_post_text = error_sentence

        logger.info(
            f"[SLACK] Posting gracious error message to channel {ctx.channel_id}, thread_ts {ctx.thread_ts} as {ctx.agent_cfg.get('username', ctx.agent_key)}"
        )
        post_resp_fn = self._get_hook(
            "post_agent_response", post_agent_response
        )
        slack_res = await post_resp_fn(
            channel_id=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            text=error_post_text,
            username=ctx.agent_cfg.get("username", ctx.agent_key),
            icon_url=ctx.agent_cfg.get("icon_url"),
        )

        pause_secs = self._get_hook(
            "SLACK_POST_PAUSE_SECONDS", SLACK_POST_PAUSE_SECONDS
        )
        await self._sleep(pause_secs)

        ctx.is_completed = bool(
            ctx.force_termination
            or not next_nomination
            or is_thread_stopped(ctx.channel_id, ctx.thread_ts)
        )

        record_tokens_fn = self._get_hook(
            "record_thread_tokens", record_thread_tokens
        )
        record_tokens_fn(
            channel_id=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            model_name=ctx.agent_cfg.get("model", ""),
            prompt_tokens=0,
            completion_tokens=0,
        )

        trig_notes = self._get_hook(
            "trigger_thread_notes_update", trigger_thread_notes_update
        )
        trig_notes(
            channel_id=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            is_completed=ctx.is_completed,
            bot_user_id=ctx.bot_user_id,
        )

        trig_usage = self._get_hook(
            "trigger_usage_update", trigger_usage_update
        )
        trig_usage(
            channel_id=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            is_completed=ctx.is_completed,
        )

        if ctx.is_completed:
            clear_thread_state(ctx.channel_id, ctx.thread_ts)

        if isinstance(slack_res, dict):
            slack_res["is_completed"] = ctx.is_completed
            slack_res["final_text"] = error_post_text
            slack_res["username"] = ctx.agent_cfg.get(
                "username", ctx.agent_key
            )
            slack_res["agent_key"] = ctx.agent_key

        return {
            "ok": False,
            "error": str(e),
            "classified_error": err_category,
            "slack_res": slack_res,
            "is_completed": ctx.is_completed,
            "final_text": error_post_text,
            "username": ctx.agent_cfg.get("username", ctx.agent_key),
            "agent_key": ctx.agent_key,
        }
