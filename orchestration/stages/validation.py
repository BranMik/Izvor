"""
Stage 1: Input validation, directive parsing, tag checking, and candidate resolution.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from core.config import AGENTS_CONFIG
from nlp import detect_language_llm
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

logger = logging.getLogger(__name__)


class ValidationStageMixin:
    """Provides stage 1 validation and parsing for TurnPipeline."""

    def _get_hook(self, name: str, default: Any = None) -> Any:
        raise NotImplementedError

    async def stage_validate_and_parse(
        self, ctx: TurnContext
    ) -> dict[str, Any] | None:
        """Stage 1: Directives, file inspection, tag validation, and candidate exclusions."""
        post_sys_msg = self._get_hook("post_system_message", post_system_message)

        if not ctx.is_bot_trigger:
            directives = await directive_parser.parse(ctx.raw_text)
            ctx.actions = directives.actions
            ctx.modifiers = directives.modifiers
            ctx.attachments = directives.attachments
            ctx.history_limit = directives.history_limit
            ctx.max_words_limit = directives.max_words
            ctx.min_words_limit = directives.min_words
            ctx.excluded_agents = directives.excluded_agents
            ctx.is_file_req = directives.is_file_req
            ctx.target_filename = directives.target_filename
            ctx.raw_text_cleaned = directives.cleaned_prompt

            # Check for stop_discussion or terminal action
            if directives.is_stop or any(a.name == "stop_discussion" for a in ctx.actions):
                logger.info(f"[ROUTER] Stop action triggered for thread {ctx.thread_key}.")
                thread_state_manager.request_stop(ctx.channel_id, ctx.thread_ts)
                return {"ok": True, "halted": True, "action": "stop_discussion"}

            thread_state_manager.clear_stop(ctx.channel_id, ctx.thread_ts)
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
                ctx.actions = directives.actions
                ctx.modifiers = directives.modifiers
                ctx.attachments = directives.attachments
                ctx.history_limit = directives.history_limit
                ctx.max_words_limit = directives.max_words
                ctx.min_words_limit = directives.min_words
                ctx.excluded_agents = directives.excluded_agents
                ctx.is_file_req = directives.is_file_req
                ctx.target_filename = directives.target_filename
                ctx.raw_text_cleaned = directives.cleaned_prompt

            # Process /grant_for_all action
            grant_act = next((a for a in ctx.actions if a.name == "grant_for_all"), None)
            if grant_act:
                from orchestration.app_settings import app_settings_manager
                from orchestration.thread_skills_manager import thread_skills_manager
                from skills import skill_registry

                requested_skills = grant_act.params.get("skills", [])
                if not requested_skills:
                    msg = "⚠️ *Missing Skills*: Please specify at least one skill to grant for all agents.\n_Example: `/grant_for_all web_research codebase_inspection`_"
                    await post_sys_msg(channel_id=ctx.channel_id, thread_ts=ctx.thread_ts, text=msg)
                    if not ctx.raw_text_cleaned:
                        return {"ok": True, "halted": True, "action": "grant_for_all"}
                else:
                    known_agent_skills = {s.name for s in skill_registry.get_agent_skills()}
                    valid_skills = [s for s in requested_skills if s in known_agent_skills]
                    invalid_skills = [s for s in requested_skills if s not in known_agent_skills]

                    lines = []
                    if invalid_skills:
                        avail = ", ".join(f"`{s}`" for s in sorted(known_agent_skills))
                        lines.append(f"⚠️ *Unknown or non-agent skill(s)*: {', '.join(f'`{s}`' for s in invalid_skills)}.\n_Available agent skills: {avail}_")

                    if valid_skills:
                        active_skills, exp_ts = thread_skills_manager.grant_skills(
                            ctx.channel_id, ctx.thread_ts, valid_skills
                        )
                        ttl_hours = app_settings_manager.get_thread_skill_expiration_time()
                        skills_str = ", ".join(f"`{s}`" for s in valid_skills)
                        active_str = ", ".join(f"`{s}`" for s in active_skills)
                        lines.append(
                            f"🌐 *Thread-Global Skills Granted*\n"
                            f"Added {skills_str} for all agents in this thread for *{ttl_hours:.1f} hours*.\n"
                            f"Currently active in thread: {active_str}"
                        )

                    if lines:
                        await post_sys_msg(channel_id=ctx.channel_id, thread_ts=ctx.thread_ts, text="\n\n".join(lines))

                    if not ctx.raw_text_cleaned:
                        return {"ok": True, "halted": True, "action": "grant_for_all"}

            # Process /revoke_for_all action
            revoke_act = next((a for a in ctx.actions if a.name == "revoke_for_all"), None)
            if revoke_act:
                from orchestration.thread_skills_manager import thread_skills_manager

                skills_to_revoke = revoke_act.params.get("skills", [])
                if not skills_to_revoke:
                    msg = "⚠️ *Missing Parameters*: Please specify skills to revoke or `all`.\n_Example: `/revoke_for_all web_research` or `/revoke_for_all all`_"
                    await post_sys_msg(channel_id=ctx.channel_id, thread_ts=ctx.thread_ts, text=msg)
                    if not ctx.raw_text_cleaned:
                        return {"ok": True, "halted": True, "action": "revoke_for_all"}
                else:
                    remaining = thread_skills_manager.revoke_skills(
                        ctx.channel_id, ctx.thread_ts, skills_to_revoke
                    )
                    if not remaining or any(s.lower() == "all" for s in skills_to_revoke):
                        msg = "🗑️ *Revoked all thread-global skills* for this thread."
                    else:
                        rem_str = ", ".join(f"`{s}`" for s in remaining)
                        msg = f"🗑️ *Revoked specified thread-global skills*. Remaining active in thread: {rem_str}"
                    await post_sys_msg(channel_id=ctx.channel_id, thread_ts=ctx.thread_ts, text=msg)

                    if not ctx.raw_text_cleaned:
                        return {"ok": True, "halted": True, "action": "revoke_for_all"}

            # Check if user prompt attempts to refer to an expired thread skill
            from orchestration.thread_skills_manager import thread_skills_manager
            expired_skills = thread_skills_manager.get_expired_skills(ctx.channel_id, ctx.thread_ts)
            if expired_skills and ctx.raw_text_cleaned:
                mentioned_expired = [
                    s for s in expired_skills
                    if s.lower() in ctx.raw_text_cleaned.lower() or s.replace("_", " ").lower() in ctx.raw_text_cleaned.lower()
                ]
                if mentioned_expired:
                    exp_str = ", ".join(f"`{s}`" for s in mentioned_expired)
                    exp_msg = (
                        f"⚠️ *Expired Global Skill*: The thread-global skill {exp_str} has expired.\n"
                        f"Run `/grant_for_all {mentioned_expired[0]}` in this thread to re-enable access for all agents."
                    )
                    await post_sys_msg(channel_id=ctx.channel_id, thread_ts=ctx.thread_ts, text=exp_msg)

            # Execute any registered orchestrator action handlers
            try:
                from skills import skill_registry
                for act in ctx.actions:
                    sk = skill_registry.get(act.name)
                    if sk and sk.action_handler:
                        act_res = await sk.action_handler(ctx, act.params)
                        if act_res and isinstance(act_res, dict) and act_res.get("halted"):
                            return act_res
            except Exception as e:
                logger.error(f"[VALIDATION] Error executing skill action handler: {e}")

            if ctx.max_words_limit is not None:
                thread_state_manager.set_max_words(
                    ctx.channel_id, ctx.thread_ts, ctx.max_words_limit
                )
            if ctx.min_words_limit is not None:
                thread_state_manager.set_min_words(
                    ctx.channel_id, ctx.thread_ts, ctx.min_words_limit
                )

            # Lock language for this thread on first human prompt or preserve existing
            current_lang = thread_state_manager.get_language(ctx.channel_id, ctx.thread_ts)
            if not current_lang:
                text_to_detect = ctx.raw_text_cleaned or ctx.raw_text
                logger.info(
                    f"[ROUTER] Inferring language for thread {ctx.thread_key} from text: '{text_to_detect[:80]}'"
                )
                current_lang = await detect_language_llm(text_to_detect)
                thread_state_manager.set_language(ctx.channel_id, ctx.thread_ts, current_lang)
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
                    if chosen_file.get("is_resource_file"):
                        logger.info(
                            f"[ROUTER] Target file '{chosen_file.get('name')}' resolved from default resources directory."
                        )
                        import json
                        from services.mcp_tools import read_document

                        doc_res_json = read_document(chosen_file.get("rel_path", chosen_file.get("name", "")))
                        try:
                            doc_data = json.loads(doc_res_json)
                        except Exception as e:
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
                        download_file_fn = self._get_hook(
                            "download_slack_file", download_slack_file
                        )
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
                                "required_skill": "document_reader",
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
                                "required_skill": "document_reader",
                            },
                        )
                else:
                    logger.warning(
                        f"[ROUTER] User requested file reading but no files were found in thread {ctx.thread_key}"
                    )
                    thread_state_manager.set_file_context(
                        ctx.channel_id,
                        ctx.thread_ts,
                        {
                            "file_name": ctx.target_filename or "unknown",
                            "content": "[No attached file found in this Slack thread to read.]",
                            "size": 0,
                            "truncated": False,
                            "authorized_agents": authorized_agents,
                            "required_skill": "document_reader",
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
