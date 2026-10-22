"""
Stage 1: Input validation, directive parsing, tag checking, and candidate resolution.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from core.config import AGENTS_CONFIG
from nlp import clean_system_and_tags, detect_language_llm, is_system_message
from orchestration.agent_settings import settings_manager
from orchestration.nomination import (
    get_initial_agents,
    parse_agent_and_prompt,
    resolve_target_file,
)
from orchestration.thread_state import thread_state_manager
from pipeline.directive_parser import directive_parser
from pipeline.entity_resolver import entity_resolver
from services.slack_client import (
    download_slack_file,
    fetch_thread_files,
    post_system_message,
)

if TYPE_CHECKING:
    from orchestration.turn_pipeline import TurnContext

from orchestration.stages.directive_handlers import (
    get_example_agent_keys as _get_example_agent_keys,
    handle_slash_directives,
)

logger = logging.getLogger(__name__)


class ValidationStage:
    """Provides stage 1 validation and parsing for TurnPipeline."""

    def __init__(self, deps: Any):
        if hasattr(deps, "deps"):
            self.deps = deps.deps
            self.pipeline = deps
        else:
            self.deps = deps
            self.pipeline = deps

    def _get_dep(self, attr_name: str, default: Any = None) -> Any:
        val = getattr(self.pipeline, attr_name, None)
        if val is not None:
            return val
        val = getattr(self.deps, attr_name, None)
        if val is not None:
            return val
        return default

    async def stage_validate_and_parse(
        self, ctx: TurnContext
    ) -> dict[str, Any] | None:
        """Stage 1: Directives, file inspection, tag validation, and candidate exclusions."""
        post_sys_msg = self._get_dep("post_system_message_fn", post_system_message)

        if not ctx.is_bot_trigger:
            fetch_files_fn = self._get_dep("files_fetcher", fetch_thread_files)
            ctx.thread_files = await fetch_files_fn(
                ctx.channel_id, ctx.thread_ts
            )
            directives = await directive_parser.parse(
                ctx.raw_text, available_files=ctx.thread_files
            )
            ctx.actions = directives.actions
            ctx.modifiers = directives.modifiers
            ctx.attachments = directives.attachments
            ctx.history_limit = directives.history_limit
            ctx.excluded_agents = directives.excluded_agents
            ctx.included_agents = directives.included_agents
            ctx.raw_text_cleaned = directives.cleaned_prompt
            ctx.is_file_req = directives.is_file_req
            ctx.target_filename = directives.target_filename

            # Record initial prompt for flat history if not already recorded for this thread
            if not thread_state_manager.get_initial_prompt(ctx.channel_id, ctx.thread_ts):
                thread_state_manager.set_initial_prompt(
                    ctx.channel_id, ctx.thread_ts, ctx.raw_text
                )


            # Process administrative slash directives and persistent constraints
            directive_res = await handle_slash_directives(ctx, directives, post_sys_msg)
            if directive_res is not None:
                return directive_res


            # Lock language for this thread on first human prompt or preserve existing
            current_lang = thread_state_manager.get_language(ctx.channel_id, ctx.thread_ts)
            if not current_lang:
                detected_lang = getattr(directives, "detected_language", None)
                if detected_lang and isinstance(detected_lang, str):
                    d_lower = detected_lang.lower().strip()
                    if d_lower.startswith("hr") or "croat" in d_lower:
                        current_lang = "Croatian"
                    elif d_lower.startswith("en") or "engl" in d_lower:
                        current_lang = "English"
                    else:
                        current_lang = detected_lang.capitalize()
                    thread_state_manager.set_language(ctx.channel_id, ctx.thread_ts, current_lang)
                    logger.info(
                        f"[ROUTER] Locked thread language from unified intent extractor for {ctx.thread_key}: {current_lang}"
                    )
                else:
                    raw_candidate = ctx.raw_text_cleaned or ctx.raw_text
                    # Never infer or lock language from system warnings, runner banners, or slash commands
                    if not is_system_message(raw_candidate) and not (raw_candidate.strip().startswith("/") and not raw_candidate.strip().startswith("/[")):
                        pure_user_text = clean_system_and_tags(raw_candidate)
                        if pure_user_text:
                            logger.info(
                                f"[ROUTER] Inferring language for thread {ctx.thread_key} from user prompt (len: {len(pure_user_text)})"
                            )
                            current_lang = await detect_language_llm(pure_user_text)
                            thread_state_manager.set_language(ctx.channel_id, ctx.thread_ts, current_lang)
                            logger.info(
                                f"[ROUTER] Locked thread language for {ctx.thread_key}: {current_lang}"
                            )
            ctx.thread_language = current_lang or "English"

            ambiguous_tags = entity_resolver.detect_ambiguous_tags(
                ctx.raw_text_cleaned
            )
            if ambiguous_tags:
                logger.warning(
                    f"[ROUTER] Ambiguous agent tag(s) detected in user prompt for thread {ctx.thread_key}: {ambiguous_tags}. Halting turn."
                )
                lines = ["⚠️ *Ambiguous Agent Reference*:"]
                for tag_str, candidates in ambiguous_tags:
                    clean_tag = tag_str.strip("/[](){}:,; \t*~_")
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
                    clean_tag = tag_str.strip("/[](){}:,; \t*~_")
                    lines.append(
                        f"Tag '{clean_tag}' could not be matched to any active agent."
                    )
                example_keys = _get_example_agent_keys(3)
                lines.append(
                    f"\nPlease check the agent name or tag a valid agent (e.g. {example_keys}) to proceed."
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

            if getattr(ctx, "target_agent_override", None):
                agent_key = ctx.target_agent_override
                prompt = ctx.raw_text_cleaned
                was_explicit_target = True
            else:
                agent_key, prompt, was_explicit_target = parse_agent_and_prompt(
                    ctx.raw_text_cleaned, ctx.channel_id, ctx.thread_ts
                )
            ctx.agent_key = agent_key
            ctx.prompt = prompt
            ctx.was_explicit_target = was_explicit_target

            effective_excluded = set(thread_state_manager.get_excluded_agents(ctx.channel_id, ctx.thread_ts))
            effective_included = list(thread_state_manager.get_included_agents(ctx.channel_id, ctx.thread_ts))

            first_act = next((a for a in ctx.actions if a.name == "first"), None)
            open_debate_act = next((a for a in ctx.actions if a.name == "open_debate"), None)
            tagged = entity_resolver.extract_tagged_agents(ctx.raw_text_cleaned)

            if first_act:
                target_name = directives.first_agent or first_act.params.get("first_agent")
                if not target_name:
                    msg = "⚠️ *Missing Agent*: Please specify an agent to start the debate.\n_Example: `/first [AgentName]` or `/first AgentName`_"
                    await post_sys_msg(channel_id=ctx.channel_id, thread_ts=ctx.thread_ts, text=msg)
                    return {"ok": True, "halted": True, "action": "first"}

                resolved_first = entity_resolver.resolve_agent_name(target_name)
                if not resolved_first:
                    msg = f"⚠️ *Unrecognized Agent*: Agent '{target_name}' could not be matched to any active agent.\n_Please check the agent name or use a valid agent._"
                    await post_sys_msg(channel_id=ctx.channel_id, thread_ts=ctx.thread_ts, text=msg)
                    return {"ok": False, "halted": True, "reason": "unrecognized_agent", "action": "first"}

                if resolved_first in effective_excluded:
                    msg = f"⚠️ *Agent Excluded*: Agent '{resolved_first}' is excluded from this thread and cannot start the debate."
                    await post_sys_msg(channel_id=ctx.channel_id, thread_ts=ctx.thread_ts, text=msg)
                    return {"ok": True, "halted": True, "reason": "agent_excluded", "action": "first"}

                ctx.agent_key = resolved_first
                ctx.prompt = ctx.raw_text_cleaned
                ctx.was_explicit_target = True
                thread_state_manager.clear_custom_flow(ctx.channel_id, ctx.thread_ts)
                thread_state_manager.set_open_debate(ctx.channel_id, ctx.thread_ts, False)
                ctx.is_open_debate = False

                initial_pool = get_initial_agents()
                remaining_pool = [
                    a for a in initial_pool
                    if a != ctx.agent_key and a not in effective_excluded
                ]
                for inc in effective_included:
                    if inc not in remaining_pool and inc != ctx.agent_key:
                        remaining_pool.append(inc)
                thread_state_manager.set_remaining_agents(
                    ctx.channel_id, ctx.thread_ts, remaining_pool
                )

            elif open_debate_act:
                target_name = directives.open_debate_agent or open_debate_act.params.get("first_agent")
                if not target_name:
                    msg = "⚠️ *Missing Agent*: Please specify an agent to start the open debate.\n_Example: `/open_debate [AgentName]` or `/open_debate AgentName`_"
                    await post_sys_msg(channel_id=ctx.channel_id, thread_ts=ctx.thread_ts, text=msg)
                    return {"ok": True, "halted": True, "action": "open_debate"}

                resolved_open = entity_resolver.resolve_agent_name(target_name)
                if not resolved_open:
                    msg = f"⚠️ *Unrecognized Agent*: Agent '{target_name}' could not be matched to any active agent.\n_Please check the agent name or use a valid agent._"
                    await post_sys_msg(channel_id=ctx.channel_id, thread_ts=ctx.thread_ts, text=msg)
                    return {"ok": False, "halted": True, "reason": "unrecognized_agent", "action": "open_debate"}

                if resolved_open in effective_excluded:
                    msg = f"⚠️ *Agent Excluded*: Agent '{resolved_open}' is excluded from this thread and cannot start the debate."
                    await post_sys_msg(channel_id=ctx.channel_id, thread_ts=ctx.thread_ts, text=msg)
                    return {"ok": True, "halted": True, "reason": "agent_excluded", "action": "open_debate"}

                ctx.agent_key = resolved_open
                ctx.prompt = ctx.raw_text_cleaned
                ctx.was_explicit_target = True
                thread_state_manager.clear_custom_flow(ctx.channel_id, ctx.thread_ts)
                thread_state_manager.set_open_debate(ctx.channel_id, ctx.thread_ts, True)
                ctx.is_open_debate = True

                all_agents = get_initial_agents()
                remaining_pool = [
                    a for a in all_agents
                    if a != ctx.agent_key and a not in effective_excluded
                ]
                for inc in effective_included:
                    if inc not in remaining_pool and inc != ctx.agent_key:
                        remaining_pool.append(inc)
                thread_state_manager.set_remaining_agents(
                    ctx.channel_id, ctx.thread_ts, remaining_pool
                )

            elif tagged and not getattr(ctx, "target_agent_override", None):
                if effective_excluded:
                    tagged = [a for a in tagged if a not in effective_excluded]
                for inc in effective_included:
                    if inc not in tagged:
                        tagged.append(inc)

                if tagged:
                    ctx.agent_key = tagged[0]
                    remaining_tagged = [a for a in tagged if a != ctx.agent_key]
                    thread_state_manager.set_remaining_agents(
                        ctx.channel_id, ctx.thread_ts, remaining_tagged
                    )
                    thread_state_manager.set_custom_flow(
                        ctx.channel_id, ctx.thread_ts, True
                    )
                    thread_state_manager.set_open_debate(ctx.channel_id, ctx.thread_ts, False)
                    ctx.is_open_debate = False
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
                thread_state_manager.set_open_debate(ctx.channel_id, ctx.thread_ts, False)
                ctx.is_open_debate = False
                curr_remaining = thread_state_manager.get_remaining_agents(
                    ctx.channel_id, ctx.thread_ts, default=get_initial_agents()
                )
                if getattr(ctx, "target_agent_override", None) and curr_remaining:
                    if ctx.agent_key in curr_remaining:
                        curr_remaining.remove(ctx.agent_key)
                if effective_excluded:
                    curr_remaining = [
                        a for a in curr_remaining if a not in effective_excluded
                    ]
                for inc in effective_included:
                    if inc not in curr_remaining and inc != ctx.agent_key:
                        curr_remaining.append(inc)

                thread_state_manager.set_remaining_agents(
                    ctx.channel_id, ctx.thread_ts, curr_remaining
                )
                if ctx.agent_key in effective_excluded:
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

            # Save and maintain round sequence for multi-round debate topologies
            if tagged and not (first_act or open_debate_act):
                current_seq = [ctx.agent_key] + [a for a in tagged if a != ctx.agent_key]
            elif ctx.agent_key:
                current_seq = [ctx.agent_key] + thread_state_manager.get_remaining_agents(ctx.channel_id, ctx.thread_ts)
            else:
                stored_seq = thread_state_manager.get_round_sequence(ctx.channel_id, ctx.thread_ts)
                current_seq = stored_seq or get_initial_agents()

            if effective_excluded:
                current_seq = [a for a in current_seq if a not in effective_excluded]
            for inc in effective_included:
                if inc not in current_seq:
                    current_seq.append(inc)

            rounds_act = next((a for a in ctx.actions if a.name == "rounds"), None)
            if rounds_act:
                if not ctx.prompt or not ctx.raw_text_cleaned:
                    ctx.prompt = "Please continue the discussion for the next round based on the arguments so far."
                    ctx.raw_text_cleaned = ctx.prompt

                if not ctx.agent_key and current_seq:
                    ctx.agent_key = current_seq[0]
                    ctx.was_explicit_target = False
                    thread_state_manager.set_remaining_agents(
                        ctx.channel_id, ctx.thread_ts, current_seq[1:]
                    )

            if current_seq:
                thread_state_manager.set_round_sequence(ctx.channel_id, ctx.thread_ts, current_seq)

            if ctx.is_file_req:
                from core.registry import get_skill_registry
                skill_registry = get_skill_registry()

                file_skill = skill_registry.get_file_handler_skill()
                file_skill_name = file_skill.name if file_skill else None
                file_action = next(
                    (
                        a.name
                        for a in getattr(ctx, "actions", [])
                        if a.params.get("target_file") is not None or (file_skill_name and a.name == file_skill_name)
                    ),
                    file_skill_name,
                )
                required_skill = file_action

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
                    if chosen_file.get("is_resource_file"):
                        logger.info(
                            f"[ROUTER] Target file '{chosen_file.get('name')}' resolved from default resources directory."
                        )
                        import json

                        from services.mcp_tools import read_document

                        doc_res_json = read_document(chosen_file.get("rel_path", chosen_file.get("name", "")))
                        try:
                            doc_data = json.loads(doc_res_json)
                        except (json.JSONDecodeError, TypeError, ValueError) as e:
                            doc_data = {"error": f"Failed to parse read_document output: {e}"}

                        if "error" not in doc_data:
                            dl_res = {
                                "ok": True,
                                "file_name": chosen_file.get("name"),
                                "content": doc_data.get("content", ""),
                                "size": chosen_file.get("size", 0),
                                "truncated": False,
                            }
                        else:
                            dl_res = {
                                "ok": False,
                                "file_name": chosen_file.get("name"),
                                "size": chosen_file.get("size", 0),
                                "error": doc_data.get("error"),
                            }
                    else:
                        logger.info(
                            f"[ROUTER] User requested file reading for thread {ctx.thread_key}. Downloading file: {chosen_file.get('name')}"
                        )
                        download_file_fn = getattr(self.deps, "download_file_fn", None) or download_slack_file
                        dl_res = await download_file_fn(chosen_file)


                    if dl_res.get("ok"):
                        thread_state_manager.set_file_context(
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
                                "required_skill": required_skill,
                            },
                        )
                    else:
                        thread_state_manager.set_file_context(
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
                                "required_skill": required_skill,
                            },
                        )
                else:
                    logger.warning(
                        f"[ROUTER] User requested file reading but no files were found in thread {ctx.thread_key}"
                    )
                    # If this was a knowledge base / RAG request, do NOT set a phantom Slack file context!
                    if any(a.name == "knowledge_base_rag" for a in getattr(ctx, "actions", [])):
                        logger.info(
                            f"[ROUTER] Action 'knowledge_base_rag' is active; skipping phantom Slack file context for '{ctx.target_filename}'."
                        )
                        ctx.is_file_req = False
                        thread_state_manager.clear_file_context(ctx.channel_id, ctx.thread_ts)
                    else:
                        thread_state_manager.set_file_context(
                            ctx.channel_id,
                            ctx.thread_ts,
                            {
                                "file_name": ctx.target_filename or "unknown",
                                "content": "[No attached file found in this Slack thread to read.]",
                                "size": 0,
                                "truncated": False,
                                "authorized_agents": authorized_agents,
                                "required_skill": required_skill,
                            },
                        )
            else:
                thread_state_manager.clear_file_context(ctx.channel_id, ctx.thread_ts)
        else:
            ctx.raw_text_cleaned = ctx.raw_text
            agent_key, prompt, was_explicit_target = parse_agent_and_prompt(
                ctx.raw_text_cleaned, ctx.channel_id, ctx.thread_ts
            )
            ctx.agent_key = agent_key
            ctx.prompt = prompt
            ctx.was_explicit_target = was_explicit_target
            ctx.thread_language = (
                thread_state_manager.get_language(ctx.channel_id, ctx.thread_ts) or "English"
            )
            ctx.is_open_debate = thread_state_manager.is_open_debate(
                ctx.channel_id, ctx.thread_ts
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
