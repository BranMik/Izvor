"""
Directive Handlers for Stage 1 Validation.
Processes administrative slash directives (/grant_skill, /revoke_skill, /rounds, /exclude, /include, etc.).
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from core.models import INT_MODIFIER_BOUNDS, coerce_int_modifier
from orchestration.agent_settings import get_agents_config
from orchestration.thread_state import thread_state_manager
from pipeline.directive_parser.slash_parser import has_substantive_prompt

if TYPE_CHECKING:
    from orchestration.turn_pipeline import TurnContext

logger = logging.getLogger(__name__)


def get_example_agent_display(default: str = "Agent") -> str:
    """Returns an example agent display/username from configured agents."""
    agents = get_agents_config()
    for cfg in agents.values():
        if isinstance(cfg, dict):
            name = cfg.get("displayname") or cfg.get("username")
            if name:
                return name
    return default


def get_example_agent_keys(count: int = 3) -> str:
    """Returns comma-separated quoted example agent IDs."""
    agents = get_agents_config()
    keys = list(agents.keys())[:count]
    if not keys:
        return "'agent-id'"
    return ", ".join(f"'{k}'" for k in keys)


async def require_directive_admin(
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

    user_id = getattr(ctx.input, "user_id", None)
    if not is_slack_admin(user_id):
        logger.warning(
            f"[VALIDATION] Non-admin user '{user_id}' attempted to execute privileged directive '/{directive_name}'"
        )
        try:
            log_audit_event(
                event_type="UNAUTHORIZED_DIRECTIVE_ATTEMPT",
                actor={"type": "slack_user", "id": user_id or "unknown"},
                action=directive_name,
                target=f"{ctx.input.channel_id}:{ctx.input.thread_ts}",
                details=details or {},
                severity="CRITICAL",
                status="BLOCKED",
            )
        except Exception as ae:  # noqa: BLE001
            logger.error(f"[VALIDATION] Failed to emit audit event for unauthorized directive: {ae}")

        await post_sys_msg(
            channel_id=ctx.input.channel_id,
            thread_ts=ctx.input.thread_ts,
            text=f"⛔ *Permission Denied*: Only workspace administrators can execute `/{directive_name}`.",
        )
        return {"ok": False, "halted": True, "action": directive_name, "reason": "unauthorized"}

    try:
        log_audit_event(
            event_type="ADMIN_DIRECTIVE_EXECUTED",
            actor={"type": "slack_admin", "id": user_id},
            action=directive_name,
            target=f"{ctx.input.channel_id}:{ctx.input.thread_ts}",
            details=details or {},
            severity="WARNING",
            status="SUCCESS",
        )
    except Exception as ae:  # noqa: BLE001
        logger.error(f"[VALIDATION] Failed to emit admin directive audit event: {ae}")

    return None


async def handle_slash_directives(
    ctx: TurnContext,
    directives: Any,
    post_sys_msg: Any,
) -> dict[str, Any] | None:
    """
    Executes administrative slash directives and persistent modifier settings.
    Returns a result dict if the turn should halt, or None to continue validation.
    """
    # Check for stop_discussion or terminal action
    if directives.is_stop or any(a.name == "stop_discussion" for a in ctx.actions):
        logger.info(f"[ROUTER] Stop action triggered for thread {ctx.thread_key}.")
        thread_state_manager.request_stop(ctx.input.channel_id, ctx.input.thread_ts)
        return {"ok": True, "halted": True, "action": "stop_discussion"}

    thread_state_manager.clear_stop(ctx.input.channel_id, ctx.input.thread_ts)

    # Centralized declarative admin gate for all directive actions
    from core.registry import get_skill_registry

    skill_registry = get_skill_registry()

    validated_actions = []
    for act in list(ctx.actions or []):
        sk = (
            skill_registry.resolve_skill(act.name)
            if hasattr(skill_registry, "resolve_skill")
            else skill_registry.get(act.name)
        )
        if sk and getattr(sk, "requires_admin", False) is True:
            if getattr(act, "origin", "slash") != "slash":
                logger.warning(
                    f"[VALIDATION] Refusing to execute inferred admin-required action '{act.name}' (origin='{getattr(act, 'origin', 'unknown')}')"
                )
                continue
            admin_check = await require_directive_admin(
                ctx,
                directive_name=sk.name,
                post_sys_msg=post_sys_msg,
                details=act.params or {},
            )
            if admin_check:
                return admin_check
        validated_actions.append(act)
    ctx.actions = validated_actions

    # Process /grant_skill_for_all action
    grant_act = next((a for a in ctx.actions if a.name in ("grant_skill_for_all", "grant_for_all")), None)
    if grant_act and getattr(grant_act, "origin", "slash") == "slash":
        from orchestration.app_settings import app_settings_manager
        from orchestration.thread_skills_manager import thread_skills_manager
        from core.registry import get_skill_registry

        skill_registry = get_skill_registry()

        requested_skills = grant_act.params.get("skills", [])
        if not requested_skills:
            msg = "⚠️ *Missing Skills*: Please specify at least one skill to grant for all agents.\n_Example: `/grant_skill_for_all web_research rag`_"
            await post_sys_msg(channel_id=ctx.input.channel_id, thread_ts=ctx.input.thread_ts, text=msg)
            return {"ok": True, "halted": True, "action": "grant_skill_for_all"}
        else:
            known_agent_skills = {s.name for s in skill_registry.get_agent_skills()}
            valid_skills = [s for s in requested_skills if s in known_agent_skills]
            invalid_skills = [s for s in requested_skills if s not in known_agent_skills]

            lines = []
            if invalid_skills:
                avail = ", ".join(f"`{s}`" for s in sorted(known_agent_skills))
                lines.append(
                    f"⚠️ *Unknown or non-agent skill(s)*: {', '.join(f'`{s}`' for s in invalid_skills)}.\n_Available agent skills: {avail}_"
                )

            if valid_skills:
                active_skills, _exp_ts = thread_skills_manager.grant_skills(ctx.input.channel_id, ctx.input.thread_ts, valid_skills)
                ttl_hours = app_settings_manager.get_thread_skill_expiration_time()
                skills_str = ", ".join(f"`{s}`" for s in valid_skills)
                active_str = ", ".join(f"`{s}`" for s in active_skills)
                lines.append(
                    f"🌐 *Thread-Global Skills Granted*\n"
                    f"Added {skills_str} for all agents in this thread for *{ttl_hours:.1f} hours*.\n"
                    f"Currently active in thread: {active_str}"
                )

            if lines:
                await post_sys_msg(channel_id=ctx.input.channel_id, thread_ts=ctx.input.thread_ts, text="\n\n".join(lines))

            if not valid_skills or not has_substantive_prompt(ctx.raw_text_cleaned):
                return {"ok": True, "halted": True, "action": "grant_skill_for_all"}

    # Process /grant_skill action
    grant_skill_act = next((a for a in ctx.actions if a.name == "grant_skill"), None)
    if grant_skill_act and getattr(grant_skill_act, "origin", "slash") == "slash":
        from orchestration.app_settings import app_settings_manager
        from orchestration.thread_skills_manager import thread_skills_manager
        from core.registry import get_skill_registry
        from pipeline.entity_resolver import entity_resolver

        skill_registry = get_skill_registry()

        requested_skill = grant_skill_act.params.get("skill")
        requested_agents = grant_skill_act.params.get("agents", [])

        if not requested_skill and grant_skill_act.params.get("skills"):
            requested_skill = grant_skill_act.params["skills"][0]

        example_ag = get_example_agent_display("Security Architect")
        if not requested_skill and not requested_agents:
            msg = (
                "⚠️ *Missing Parameters*: Please specify a skill and at least one agent.\n"
                f"_Example: `/grant_skill web_research {example_ag}` or `/grant_skill [web_research] [{example_ag}]`_"
            )
            await post_sys_msg(channel_id=ctx.input.channel_id, thread_ts=ctx.input.thread_ts, text=msg)
            return {"ok": True, "halted": True, "action": "grant_skill"}

        if not requested_skill:
            msg = (
                "⚠️ *Missing Skill*: Please specify a skill to grant.\n"
                f"_Example: `/grant_skill web_research {example_ag}`_"
            )
            await post_sys_msg(channel_id=ctx.input.channel_id, thread_ts=ctx.input.thread_ts, text=msg)
            return {"ok": True, "halted": True, "action": "grant_skill"}

        if not requested_agents:
            msg = (
                f"⚠️ *Missing Agent*: Please specify at least one agent to grant `{requested_skill}` to.\n"
                f"_Example: `/grant_skill {requested_skill} {example_ag}`_"
            )
            await post_sys_msg(channel_id=ctx.input.channel_id, thread_ts=ctx.input.thread_ts, text=msg)
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
            lines.append(f"⚠️ *Unknown or non-agent skill*: `{requested_skill}`.\n_Available agent skills: {avail}_")

        if unresolved_agents:
            registered_agents = list(entity_resolver._get_agents_map().keys())
            avail_ag = ", ".join(f"`{a}`" for a in sorted(registered_agents))
            lines.append(
                f"⚠️ *Unknown agent(s)*: {', '.join(f'`{a}`' for a in unresolved_agents)}.\n_Available agents: {avail_ag}_"
            )

        if is_valid_skill and resolved_agents:
            thread_skills_manager.grant_agent_skills(ctx.input.channel_id, ctx.input.thread_ts, requested_skill, resolved_agents)
            ttl_hours = app_settings_manager.get_thread_skill_expiration_time()
            agents_str = ", ".join(f"`{a}`" for a in resolved_agents)
            lines.append(
                f"🎯 *Agent Skill Granted*\n"
                f"Granted `{requested_skill}` to {agents_str} in this thread for *{ttl_hours:.1f} hours*."
            )

        if lines:
            await post_sys_msg(channel_id=ctx.input.channel_id, thread_ts=ctx.input.thread_ts, text="\n\n".join(lines))

        if not (is_valid_skill and resolved_agents) or not has_substantive_prompt(ctx.raw_text_cleaned):
            return {"ok": True, "halted": True, "action": "grant_skill"}

    # Process /revoke_skill_for_all action
    revoke_act = next((a for a in ctx.actions if a.name in ("revoke_skill_for_all", "revoke_for_all")), None)
    if revoke_act and getattr(revoke_act, "origin", "slash") == "slash":
        from orchestration.thread_skills_manager import thread_skills_manager

        skills_to_revoke = revoke_act.params.get("skills", [])
        if not skills_to_revoke:
            msg = "⚠️ *Missing Parameters*: Please specify skills to revoke or `all`.\n_Example: `/revoke_skill_for_all web_research` or `/revoke_skill_for_all all`_"
            await post_sys_msg(channel_id=ctx.input.channel_id, thread_ts=ctx.input.thread_ts, text=msg)
            return {"ok": True, "halted": True, "action": "revoke_skill_for_all"}
        else:
            remaining = thread_skills_manager.revoke_skills(ctx.input.channel_id, ctx.input.thread_ts, skills_to_revoke)
            if not remaining or any(s.lower() == "all" for s in skills_to_revoke):
                msg = "🗑️ *Revoked all thread-global skills* for this thread."
            else:
                rem_str = ", ".join(f"`{s}`" for s in remaining)
                msg = f"🗑️ *Revoked specified thread-global skills*. Remaining active in thread: {rem_str}"
            await post_sys_msg(channel_id=ctx.input.channel_id, thread_ts=ctx.input.thread_ts, text=msg)
            if not has_substantive_prompt(ctx.raw_text_cleaned):
                return {"ok": True, "halted": True, "action": "revoke_skill_for_all"}

    # Check if user prompt attempts to refer to an expired thread skill
    from orchestration.thread_skills_manager import thread_skills_manager

    expired_skills = thread_skills_manager.get_expired_skills(ctx.input.channel_id, ctx.input.thread_ts)
    if expired_skills and ctx.raw_text_cleaned:
        mentioned_expired = [
            s
            for s in expired_skills
            if s.lower() in ctx.raw_text_cleaned.lower() or s.replace("_", " ").lower() in ctx.raw_text_cleaned.lower()
        ]
        if mentioned_expired:
            exp_str = ", ".join(f"`{s}`" for s in mentioned_expired)
            exp_msg = (
                f"⚠️ *Expired Global Skill*: The thread-global skill {exp_str} has expired.\n"
                f"Run `/grant_skill_for_all {mentioned_expired[0]}` in this thread to re-enable access for all agents."
            )
            await post_sys_msg(channel_id=ctx.input.channel_id, thread_ts=ctx.input.thread_ts, text=exp_msg)

    # Execute any registered orchestrator action handlers
    try:
        from core.registry import get_skill_registry

        skill_registry = get_skill_registry()
        for act in ctx.actions:
            sk = (
                skill_registry.resolve_skill(act.name)
                if hasattr(skill_registry, "resolve_skill")
                else skill_registry.get(act.name)
            )
            if sk and sk.action_handler:
                if sk.triggers.get("auto_detect", False) is False and getattr(act, "origin", "slash") != "slash":
                    logger.warning(
                        f"[VALIDATION] Refusing to execute privileged action '{act.name}' because origin is '{getattr(act, 'origin', 'unknown')}'"
                    )
                    continue
                act_res = await sk.action_handler(ctx, act.params)
                if act_res and isinstance(act_res, dict) and act_res.get("halted"):
                    return act_res
    except Exception as e:  # noqa: BLE001 - Error boundary isolates dynamic skill action execution from pipeline validation
        logger.error(f"[VALIDATION] Error executing skill action handler: {e}")

    # Update persistent word limits and off toggles
    from core.config import is_slack_admin

    is_admin = is_slack_admin(getattr(ctx.input, "user_id", None))

    attempted_off = bool(
        (ctx.modifiers or {}).get("max_words") == "off"
        or (ctx.modifiers or {}).get("min_words") == "off"
        or (ctx.modifiers or {}).get("scribe_max_words") == "off"
        or (ctx.modifiers or {}).get("scribe_min_words") == "off"
        or any(a.name == "exclude_agents" and a.params.get("off") for a in ctx.actions)
        or any(a.name == "include_agents" and a.params.get("off") for a in ctx.actions)
        or any(a.name in ("scribe_max_words", "scribe_min_words") and a.params.get("words") == "off" for a in ctx.actions)
    )
    if attempted_off and not is_admin:
        logger.warning(f"[SECURITY] Non-admin '{ctx.input.user_id}' denied from turning off directives on {ctx.thread_key}")
        msg = "⚠️ *Permission Denied*: Only Slack administrators are authorized to turn off safety or constraint directives."
        await post_sys_msg(channel_id=ctx.input.channel_id, thread_ts=ctx.input.thread_ts, text=msg)
        return {"ok": False, "halted": True, "error": "admin_required"}

    max_words_val = (ctx.modifiers or {}).get("max_words")
    if max_words_val == "off":
        if is_admin:
            thread_state_manager.clear_max_words(ctx.input.channel_id, ctx.input.thread_ts)
        else:
            logger.warning(f"[VALIDATION] Non-admin '{ctx.input.user_id}' attempted to turn off max_words")
    else:
        min_b, max_b = INT_MODIFIER_BOUNDS["max_words"]
        coerced = coerce_int_modifier(max_words_val, min_b, max_b) if max_words_val is not None else None
        if coerced is not None:
            thread_state_manager.set_max_words(ctx.input.channel_id, ctx.input.thread_ts, coerced)

    min_words_val = (ctx.modifiers or {}).get("min_words")
    if min_words_val == "off":
        if is_admin:
            thread_state_manager.clear_min_words(ctx.input.channel_id, ctx.input.thread_ts)
        else:
            logger.warning(f"[VALIDATION] Non-admin '{ctx.input.user_id}' attempted to turn off min_words")
    else:
        min_b, max_b = INT_MODIFIER_BOUNDS["min_words"]
        coerced = coerce_int_modifier(min_words_val, min_b, max_b) if min_words_val is not None else None
        if coerced is not None:
            thread_state_manager.set_min_words(ctx.input.channel_id, ctx.input.thread_ts, coerced)

    scribe_max_val = (ctx.modifiers or {}).get("scribe_max_words")
    if scribe_max_val is None:
        for act in ctx.actions:
            if act.name == "scribe_max_words":
                scribe_max_val = act.params.get("words")
                break
    if scribe_max_val == "off":
        if is_admin:
            thread_state_manager.clear_scribe_max_words(ctx.input.channel_id, ctx.input.thread_ts)
        else:
            logger.warning(f"[VALIDATION] Non-admin '{ctx.input.user_id}' attempted to turn off scribe_max_words")
    else:
        min_b, max_b = INT_MODIFIER_BOUNDS["scribe_max_words"]
        coerced = coerce_int_modifier(scribe_max_val, min_b, max_b) if scribe_max_val is not None else None
        if coerced is not None:
            thread_state_manager.set_scribe_max_words(ctx.input.channel_id, ctx.input.thread_ts, coerced)

    scribe_min_val = (ctx.modifiers or {}).get("scribe_min_words")
    if scribe_min_val is None:
        for act in ctx.actions:
            if act.name == "scribe_min_words":
                scribe_min_val = act.params.get("words")
                break
    if scribe_min_val == "off":
        if is_admin:
            thread_state_manager.clear_scribe_min_words(ctx.input.channel_id, ctx.input.thread_ts)
        else:
            logger.warning(f"[VALIDATION] Non-admin '{ctx.input.user_id}' attempted to turn off scribe_min_words")
    else:
        min_b, max_b = INT_MODIFIER_BOUNDS["scribe_min_words"]
        coerced = coerce_int_modifier(scribe_min_val, min_b, max_b) if scribe_min_val is not None else None
        if coerced is not None:
            thread_state_manager.set_scribe_min_words(ctx.input.channel_id, ctx.input.thread_ts, coerced)

    exclude_off = any(a.name == "exclude_agents" and a.params.get("off") for a in ctx.actions)
    if exclude_off:
        if is_admin:
            thread_state_manager.clear_excluded_agents(ctx.input.channel_id, ctx.input.thread_ts)
        else:
            logger.warning(f"[VALIDATION] Non-admin '{ctx.input.user_id}' attempted to turn off agent exclusions")
    elif ctx.excluded_agents:
        for ag in ctx.excluded_agents:
            thread_state_manager.add_excluded_agent(ctx.input.channel_id, ctx.input.thread_ts, ag)

    include_off = any(a.name == "include_agents" and a.params.get("off") for a in ctx.actions)
    if include_off:
        if is_admin:
            thread_state_manager.clear_included_agents(ctx.input.channel_id, ctx.input.thread_ts)
        else:
            logger.warning(f"[VALIDATION] Non-admin '{ctx.input.user_id}' attempted to turn off agent inclusions")
    elif ctx.included_agents:
        for ag in ctx.included_agents:
            thread_state_manager.add_included_agent(ctx.input.channel_id, ctx.input.thread_ts, ag)

    # Persist custom format modifiers into thread state
    custom_mods = {
        k: v
        for k, v in (ctx.modifiers or {}).items()
        if k
        not in (
            "max_words",
            "min_words",
            "history",
            "file_use",
            "target_file",
            "excluded_agents",
            "scribe_max_words",
            "scribe_min_words",
            "language",
        )
    }
    if custom_mods and not ctx.input.is_bot_trigger:
        existing_mods = thread_state_manager.get_format_modifiers(ctx.input.channel_id, ctx.input.thread_ts)
        if isinstance(existing_mods, dict):
            existing_mods.update(custom_mods)
        else:
            existing_mods = dict(custom_mods)
        thread_state_manager.set_format_modifiers(ctx.input.channel_id, ctx.input.thread_ts, existing_mods)

    # If user message only contained directive toggles and no discussion prompt, halt cleanly
    is_pure_toggle = bool(
        (
            max_words_val == "off"
            or min_words_val == "off"
            or scribe_max_val == "off"
            or scribe_min_val == "off"
            or exclude_off
            or include_off
        )
        and not has_substantive_prompt(ctx.raw_text_cleaned)
    )
    if is_pure_toggle:
        msg = "⚙️ *Thread Configuration Updated*: Requested directive constraints have been turned off for this thread."
        await post_sys_msg(channel_id=ctx.input.channel_id, thread_ts=ctx.input.thread_ts, text=msg)
        return {"ok": True, "halted": True, "action": "directive_toggle"}

    return None
