"""
Stage 1: Input validation, directive parsing, tag checking, and candidate resolution.
"""

from __future__ import annotations

import logging
import re
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

logger = logging.getLogger(__name__)


def _get_example_agent_display(default: str = "Agent") -> str:
    """Returns an example agent display/username from configured agents."""
    for cfg in AGENTS_CONFIG.values():
        if isinstance(cfg, dict):
            name = cfg.get("displayname") or cfg.get("username")
            if name:
                return name
    return default


def _get_example_agent_keys(count: int = 3) -> str:
    """Returns comma-separated quoted example agent IDs."""
    keys = list(AGENTS_CONFIG.keys())[:count]
    if not keys:
        return "'agent-id'"
    return ", ".join(f"'{k}'" for k in keys)


async def _require_directive_admin(
    ctx: TurnContext,
    directive_name: str,
    post_sys_msg: Any,
    details: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """
    Enforces that only authorized Slack Workspace Administrators can execute
    privileged directives (e.g. granting/revoking thread skills or constraints).
    Logs audit events and emits system error messages on unauthorized attempts.
    """
    from core.config import is_slack_admin
    from services.audit_logger import log_audit_event

    user_id = getattr(ctx, "user_id", None)
    if not is_slack_admin(user_id):
        logger.warning(
            f"[VALIDATION] Non-admin user '{user_id}' attempted to execute privileged directive '/{directive_name}'"
        )
        try:
            log_audit_event(
                event_type="UNAUTHORIZED_DIRECTIVE_ATTEMPT",
                actor={"type": "slack_user", "id": user_id or "unknown"},
                action=directive_name,
                target=f"{ctx.channel_id}:{ctx.thread_ts}",
                details=details or {},
                severity="CRITICAL",
                status="BLOCKED",
            )
        except Exception as ae:
            logger.error(f"[VALIDATION] Failed to emit audit event for unauthorized directive: {ae}")

        await post_sys_msg(
            channel_id=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            text=f"⛔ *Permission Denied*: Only workspace administrators can execute `/{directive_name}`.",
        )
        return {"ok": False, "halted": True, "action": directive_name, "reason": "unauthorized"}

    try:
        log_audit_event(
            event_type="ADMIN_DIRECTIVE_EXECUTED",
            actor={"type": "slack_admin", "id": user_id},
            action=directive_name,
            target=f"{ctx.channel_id}:{ctx.thread_ts}",
            details=details or {},
            severity="WARNING",
            status="SUCCESS",
        )
    except Exception as ae:
        logger.error(f"[VALIDATION] Failed to emit admin directive audit event: {ae}")

    return None


class ValidationStage:
    def __init__(self, pipeline: Any):
        self.pipeline = pipeline
    """Provides stage 1 validation and parsing for TurnPipeline."""



    async def stage_validate_and_parse(
        self, ctx: TurnContext
    ) -> dict[str, Any] | None:
        """Stage 1: Directives, file inspection, tag validation, and candidate exclusions."""
        post_sys_msg = self.pipeline._get_hook("post_system_message", post_system_message)

        if not ctx.is_bot_trigger:
            fetch_files_fn = self.pipeline._get_hook(
                "fetch_thread_files", fetch_thread_files
            )
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
            ctx.max_words_limit = directives.max_words
            ctx.min_words_limit = directives.min_words
            ctx.scribe_max_words_limit = directives.scribe_max_words
            ctx.scribe_min_words_limit = directives.scribe_min_words
            ctx.excluded_agents = directives.excluded_agents
            ctx.included_agents = directives.included_agents
            ctx.exclude_off = directives.exclude_off
            ctx.include_off = directives.include_off
            ctx.revoke_target = directives.revoke_target
            ctx.is_file_req = directives.is_file_req
            ctx.target_filename = directives.target_filename
            ctx.raw_text_cleaned = directives.cleaned_prompt

            # Record initial prompt for flat history if not already recorded for this thread
            if not thread_state_manager.get_initial_prompt(ctx.channel_id, ctx.thread_ts):
                thread_state_manager.set_initial_prompt(
                    ctx.channel_id, ctx.thread_ts, ctx.raw_text
                )

            # Check for stop_discussion or terminal action
            if directives.is_stop or any(a.name == "stop_discussion" for a in ctx.actions):
                logger.info(f"[ROUTER] Stop action triggered for thread {ctx.thread_key}.")
                thread_state_manager.request_stop(ctx.channel_id, ctx.thread_ts)
                return {"ok": True, "halted": True, "action": "stop_discussion"}

            thread_state_manager.clear_stop(ctx.channel_id, ctx.thread_ts)

            # Process /grant_skill_for_all action
            grant_act = next((a for a in ctx.actions if a.name in ("grant_skill_for_all", "grant_for_all")), None)
            if grant_act and getattr(grant_act, "origin", "slash") == "slash":
                admin_check = await _require_directive_admin(
                    ctx,
                    directive_name="grant_skill_for_all",
                    post_sys_msg=post_sys_msg,
                    details={"skills": grant_act.params.get("skills", [])},
                )
                if admin_check:
                    return admin_check

                from orchestration.app_settings import app_settings_manager
                from orchestration.thread_skills_manager import thread_skills_manager
                from core.registry import get_skill_registry
                skill_registry = get_skill_registry()

                requested_skills = grant_act.params.get("skills", [])
                if not requested_skills:
                    msg = "⚠️ *Missing Skills*: Please specify at least one skill to grant for all agents.\n_Example: `/grant_skill_for_all web_research codebase_inspection`_"
                    await post_sys_msg(channel_id=ctx.channel_id, thread_ts=ctx.thread_ts, text=msg)
                    return {"ok": True, "halted": True, "action": "grant_skill_for_all"}
                else:
                    known_agent_skills = {s.name for s in skill_registry.get_agent_skills()}
                    valid_skills = [s for s in requested_skills if s in known_agent_skills]
                    invalid_skills = [s for s in requested_skills if s not in known_agent_skills]

                    lines = []
                    if invalid_skills:
                        avail = ", ".join(f"`{s}`" for s in sorted(known_agent_skills))
                        lines.append(f"⚠️ *Unknown or non-agent skill(s)*: {', '.join(f'`{s}`' for s in invalid_skills)}.\n_Available agent skills: {avail}_")

                    if valid_skills:
                        active_skills, _exp_ts = thread_skills_manager.grant_skills(
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

                    return {"ok": True, "halted": True, "action": "grant_skill_for_all"}

            # Process /grant_skill action
            grant_skill_act = next((a for a in ctx.actions if a.name == "grant_skill"), None)
            if grant_skill_act and getattr(grant_skill_act, "origin", "slash") == "slash":
                admin_check = await _require_directive_admin(
                    ctx,
                    directive_name="grant_skill",
                    post_sys_msg=post_sys_msg,
                    details=grant_skill_act.params or {},
                )
                if admin_check:
                    return admin_check

                from orchestration.app_settings import app_settings_manager
                from orchestration.thread_skills_manager import thread_skills_manager
                from core.registry import get_skill_registry
                skill_registry = get_skill_registry()

                requested_skill = grant_skill_act.params.get("skill")
                requested_agents = grant_skill_act.params.get("agents", [])

                if not requested_skill and grant_skill_act.params.get("skills"):
                    requested_skill = grant_skill_act.params["skills"][0]

                example_ag = _get_example_agent_display("Security Architect")
                if not requested_skill and not requested_agents:
                    msg = (
                        "⚠️ *Missing Parameters*: Please specify a skill and at least one agent.\n"
                        f"_Example: `/grant_skill web_research {example_ag}` or `/grant_skill [web_research] [{example_ag}]`_"
                    )
                    await post_sys_msg(channel_id=ctx.channel_id, thread_ts=ctx.thread_ts, text=msg)
                    return {"ok": True, "halted": True, "action": "grant_skill"}

                if not requested_skill:
                    msg = (
                        "⚠️ *Missing Skill*: Please specify a skill to grant.\n"
                        f"_Example: `/grant_skill web_research {example_ag}`_"
                    )
                    await post_sys_msg(channel_id=ctx.channel_id, thread_ts=ctx.thread_ts, text=msg)
                    return {"ok": True, "halted": True, "action": "grant_skill"}

                if not requested_agents:
                    msg = (
                        f"⚠️ *Missing Agent*: Please specify at least one agent to grant `{requested_skill}` to.\n"
                        f"_Example: `/grant_skill {requested_skill} {example_ag}`_"
                    )
                    await post_sys_msg(channel_id=ctx.channel_id, thread_ts=ctx.thread_ts, text=msg)
                    return {"ok": True, "halted": True, "action": "grant_skill"}

                known_agent_skills = {s.name for s in skill_registry.get_agent_skills()}
                is_valid_skill = requested_skill in known_agent_skills

                resolved_agents: list[str] = []
                unresolved_agents: list[str] = []
                for ag in requested_agents:
                    candidates = entity_resolver.resolve_candidates(ag)
                    if candidates:
                        resolved_agents.extend(candidates)
                    else:
                        unresolved_agents.append(ag)
                resolved_agents = list(dict.fromkeys(resolved_agents))

                lines = []
                if not is_valid_skill:
                    avail = ", ".join(f"`{s}`" for s in sorted(known_agent_skills))
                    lines.append(
                        f"⚠️ *Unknown or non-agent skill*: `{requested_skill}`.\n_Available agent skills: {avail}_"
                    )

                if unresolved_agents:
                    registered_agents = list(entity_resolver._get_agents_map().keys())
                    avail_ag = ", ".join(f"`{a}`" for a in sorted(registered_agents))
                    lines.append(
                        f"⚠️ *Unknown agent(s)*: {', '.join(f'`{a}`' for a in unresolved_agents)}.\n_Available agents: {avail_ag}_"
                    )

                if is_valid_skill and resolved_agents:
                    thread_skills_manager.grant_agent_skills(
                        ctx.channel_id, ctx.thread_ts, requested_skill, resolved_agents
                    )
                    ttl_hours = app_settings_manager.get_thread_skill_expiration_time()
                    agents_str = ", ".join(f"`{a}`" for a in resolved_agents)
                    lines.append(
                        f"🎯 *Agent Skill Granted*\n"
                        f"Granted `{requested_skill}` to {agents_str} in this thread for *{ttl_hours:.1f} hours*."
                    )

                if lines:
                    await post_sys_msg(channel_id=ctx.channel_id, thread_ts=ctx.thread_ts, text="\n\n".join(lines))

                return {"ok": True, "halted": True, "action": "grant_skill"}

            # Process /revoke_skill_for_all action
            revoke_act = next((a for a in ctx.actions if a.name in ("revoke_skill_for_all", "revoke_for_all")), None)
            if revoke_act and getattr(revoke_act, "origin", "slash") == "slash":
                admin_check = await _require_directive_admin(
                    ctx,
                    directive_name="revoke_skill_for_all",
                    post_sys_msg=post_sys_msg,
                    details={"skills": revoke_act.params.get("skills", [])},
                )
                if admin_check:
                    return admin_check

                from orchestration.thread_skills_manager import thread_skills_manager

                skills_to_revoke = revoke_act.params.get("skills", [])
                if not skills_to_revoke:
                    msg = "⚠️ *Missing Parameters*: Please specify skills to revoke or `all`.\n_Example: `/revoke_skill_for_all web_research` or `/revoke_skill_for_all all`_"
                    await post_sys_msg(channel_id=ctx.channel_id, thread_ts=ctx.thread_ts, text=msg)
                    return {"ok": True, "halted": True, "action": "revoke_skill_for_all"}
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
                    return {"ok": True, "halted": True, "action": "revoke_skill_for_all"}

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
                        f"Run `/grant_skill_for_all {mentioned_expired[0]}` in this thread to re-enable access for all agents."
                    )
                    await post_sys_msg(channel_id=ctx.channel_id, thread_ts=ctx.thread_ts, text=exp_msg)

            # Execute any registered orchestrator action handlers
            try:
                from core.registry import get_skill_registry
                skill_registry = get_skill_registry()
                for act in ctx.actions:
                    sk = skill_registry.get(act.name)
                    if sk and sk.action_handler:
                        # Privileged actions (auto_detect == False) must originate from slash command
                        if sk.triggers.get("auto_detect", False) is False and getattr(act, "origin", "slash") != "slash":
                            logger.warning(
                                f"[VALIDATION] Refusing to execute privileged action '{act.name}' because origin is '{getattr(act, 'origin', 'unknown')}'"
                            )
                            continue
                        if act.name == "revoke_constraint":
                            admin_check = await _require_directive_admin(
                                ctx,
                                directive_name="revoke_constraint",
                                post_sys_msg=post_sys_msg,
                                details=act.params or {},
                            )
                            if admin_check:
                                return admin_check
                        act_res = await sk.action_handler(ctx, act.params)
                        if act_res and isinstance(act_res, dict) and act_res.get("halted"):
                            return act_res
            except Exception as e:  # noqa: BLE001 - Error boundary isolates dynamic skill action execution from pipeline validation
                logger.error(f"[VALIDATION] Error executing skill action handler: {e}")

            # Process /revoke_constraint action
            if ctx.revoke_target and not any(a.name == "revoke_constraint" for a in (ctx.actions or [])):
                admin_check = await _require_directive_admin(
                    ctx,
                    directive_name="revoke_constraint",
                    post_sys_msg=post_sys_msg,
                    details={"target": ctx.revoke_target},
                )
                if admin_check:
                    return admin_check

                target = ctx.revoke_target.lower()
                if target in ("all", "everything"):
                    thread_state_manager.revoke_all(ctx.channel_id, ctx.thread_ts)
                    msg = "🗑️ *Revoked all thread constraints* (word limits, language lock, inclusions, and exclusions) for this thread."
                elif target in ("max_words", "max-words", "maxwords"):
                    thread_state_manager.clear_max_words(ctx.channel_id, ctx.thread_ts)
                    msg = "🗑️ *Revoked constraint*: `max_words` cleared for this thread."
                elif target in ("min_words", "min-words", "minwords"):
                    thread_state_manager.clear_min_words(ctx.channel_id, ctx.thread_ts)
                    msg = "🗑️ *Revoked constraint*: `min_words` cleared for this thread."
                elif target in ("scribe_max_words", "scribe-max-words", "scribemaxwords"):
                    thread_state_manager.clear_scribe_max_words(ctx.channel_id, ctx.thread_ts)
                    msg = "🗑️ *Revoked constraint*: `scribe_max_words` cleared for this thread."
                elif target in ("scribe_min_words", "scribe-min-words", "scribeminwords"):
                    thread_state_manager.clear_scribe_min_words(ctx.channel_id, ctx.thread_ts)
                    msg = "🗑️ *Revoked constraint*: `scribe_min_words` cleared for this thread."
                elif target in ("exclude", "exclude_agents", "excluded"):
                    thread_state_manager.clear_excluded_agents(ctx.channel_id, ctx.thread_ts)
                    msg = "🗑️ *Revoked constraint*: Agent exclusions cleared for this thread."
                elif target in ("include", "include_agents", "included"):
                    thread_state_manager.clear_included_agents(ctx.channel_id, ctx.thread_ts)
                    msg = "🗑️ *Revoked constraint*: Forced agent inclusions cleared for this thread."
                elif target in ("language", "lang"):
                    thread_state_manager.clear_language(ctx.channel_id, ctx.thread_ts)
                    msg = "🗑️ *Revoked language lock*: Thread language cleared."
                elif target in ("modifiers", "modifier"):
                    thread_state_manager.clear_format_modifiers(ctx.channel_id, ctx.thread_ts)
                    msg = "🗑️ *Revoked constraint*: `format_modifiers` cleared for this thread."
                else:
                    msg = f"⚠️ *Unknown Revoke Target*: `{target}`. Available: `max_words`, `min_words`, `scribe_max_words`, `scribe_min_words`, `exclude`, `include`, `language`, `modifiers`, `all`."
                await post_sys_msg(channel_id=ctx.channel_id, thread_ts=ctx.thread_ts, text=msg)
                return {"ok": True, "halted": True, "action": "revoke_constraint"}

            # Update persistent word limits and off toggles
            from core.config import is_slack_admin
            is_admin = is_slack_admin(getattr(ctx, "user_id", None))

            if ctx.max_words_limit == "off":
                if is_admin:
                    thread_state_manager.clear_max_words(ctx.channel_id, ctx.thread_ts)
                else:
                    logger.warning(f"[VALIDATION] Non-admin '{ctx.user_id}' attempted to turn off max_words")
            elif ctx.max_words_limit is not None and isinstance(ctx.max_words_limit, int):
                thread_state_manager.set_max_words(
                    ctx.channel_id, ctx.thread_ts, ctx.max_words_limit
                )

            if ctx.min_words_limit == "off":
                if is_admin:
                    thread_state_manager.clear_min_words(ctx.channel_id, ctx.thread_ts)
                else:
                    logger.warning(f"[VALIDATION] Non-admin '{ctx.user_id}' attempted to turn off min_words")
            elif ctx.min_words_limit is not None and isinstance(ctx.min_words_limit, int):
                thread_state_manager.set_min_words(
                    ctx.channel_id, ctx.thread_ts, ctx.min_words_limit
                )

            if ctx.scribe_max_words_limit == "off":
                if is_admin:
                    thread_state_manager.clear_scribe_max_words(ctx.channel_id, ctx.thread_ts)
                else:
                    logger.warning(f"[VALIDATION] Non-admin '{ctx.user_id}' attempted to turn off scribe_max_words")
            elif ctx.scribe_max_words_limit is not None and isinstance(ctx.scribe_max_words_limit, int):
                thread_state_manager.set_scribe_max_words(
                    ctx.channel_id, ctx.thread_ts, ctx.scribe_max_words_limit
                )

            if ctx.scribe_min_words_limit == "off":
                if is_admin:
                    thread_state_manager.clear_scribe_min_words(ctx.channel_id, ctx.thread_ts)
                else:
                    logger.warning(f"[VALIDATION] Non-admin '{ctx.user_id}' attempted to turn off scribe_min_words")
            elif ctx.scribe_min_words_limit is not None and isinstance(ctx.scribe_min_words_limit, int):
                thread_state_manager.set_scribe_min_words(
                    ctx.channel_id, ctx.thread_ts, ctx.scribe_min_words_limit
                )

            if ctx.exclude_off:
                if is_admin:
                    thread_state_manager.clear_excluded_agents(ctx.channel_id, ctx.thread_ts)
                else:
                    logger.warning(f"[VALIDATION] Non-admin '{ctx.user_id}' attempted to turn off agent exclusions")
            elif ctx.excluded_agents:
                for ag in ctx.excluded_agents:
                    thread_state_manager.add_excluded_agent(ctx.channel_id, ctx.thread_ts, ag)

            if ctx.include_off:
                if is_admin:
                    thread_state_manager.clear_included_agents(ctx.channel_id, ctx.thread_ts)
                else:
                    logger.warning(f"[VALIDATION] Non-admin '{ctx.user_id}' attempted to turn off agent inclusions")
            elif ctx.included_agents:
                for ag in ctx.included_agents:
                    thread_state_manager.add_included_agent(ctx.channel_id, ctx.thread_ts, ag)

            # Persist custom format modifiers into thread state
            custom_mods = {
                k: v
                for k, v in (ctx.modifiers or {}).items()
                if k not in ("max_words", "min_words", "history", "file_use", "target_file", "excluded_agents")
            }
            if custom_mods and not ctx.is_bot_trigger:
                existing_mods = thread_state_manager.get_format_modifiers(ctx.channel_id, ctx.thread_ts)
                existing_mods.update(custom_mods)
                thread_state_manager.set_format_modifiers(ctx.channel_id, ctx.thread_ts, existing_mods)

            # If user message only contained directive toggles and no discussion prompt, halt cleanly
            is_pure_toggle = bool(
                (
                    ctx.max_words_limit == "off"
                    or ctx.min_words_limit == "off"
                    or ctx.scribe_max_words_limit is not None
                    or ctx.scribe_min_words_limit is not None
                    or ctx.exclude_off
                    or ctx.include_off
                )
                and not ctx.raw_text_cleaned
            )
            if is_pure_toggle:
                msg = "⚙️ *Thread constraints updated.*"
                await post_sys_msg(channel_id=ctx.channel_id, thread_ts=ctx.thread_ts, text=msg)
                return {"ok": True, "halted": True, "action": "update_constraints"}

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
                        download_file_fn = self.pipeline._get_hook(
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
