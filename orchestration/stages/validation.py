"""
Stage 1: Input validation, directive parsing, tag checking, and candidate resolution.
"""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from core.config import AGENTS_CONFIG
from pipeline.directive_parser import directive_parser
from pipeline.entity_resolver import entity_resolver
from services.slack_client import (
    download_slack_file,
    fetch_thread_files,
    post_system_message,
)
from nlp import detect_language, detect_language_llm
from orchestration.agent_settings import settings_manager
from orchestration.nomination import (
    get_initial_agents,
    parse_agent_and_prompt,
    resolve_target_file,
)
from orchestration.thread_state import (
    clear_thread_file_context,
    clear_thread_stop,
    get_thread_language,
    set_thread_file_context,
    set_thread_language,
    set_thread_max_words,
    set_thread_min_words,
    thread_state_manager,
)

if TYPE_CHECKING:
    from orchestration.turn_pipeline import TurnContext

logger = logging.getLogger(__name__)


class ValidationStageMixin:
    """Provides stage 1 validation and parsing for TurnPipeline."""

    def _get_hook(self, name: str, default: Any = None) -> Any:
        raise NotImplementedError

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

            # Lock language for this thread on first human prompt or preserve existing
            current_lang = get_thread_language(ctx.channel_id, ctx.thread_ts)
            if not current_lang:
                current_lang = await detect_language_llm(
                    ctx.raw_text_cleaned or ctx.raw_text
                )
                set_thread_language(ctx.channel_id, ctx.thread_ts, current_lang)
                logger.info(
                    f"[ROUTER] Locked thread language for {ctx.thread_key}: {current_lang}"
                )
            ctx.thread_language = current_lang

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
            ctx.thread_language = (
                get_thread_language(ctx.channel_id, ctx.thread_ts) or "English"
            )

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
