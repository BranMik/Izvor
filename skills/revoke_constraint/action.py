"""
Action handler for the revoke_constraint skill.
Executes when /revoke_constraint [target] is parsed.
Revokes designated thread constraints or all constraints.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def run(ctx: Any, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    from core.config import is_slack_admin
    from orchestration.thread_state import thread_state_manager
    from services.slack import post_system_message

    params = params or {}
    channel_id = getattr(ctx, "channel_id", "")
    thread_ts = getattr(ctx, "thread_ts", "")
    user_id = getattr(ctx, "user_id", None)

    if not is_slack_admin(user_id):
        logger.warning(
            f"[REVOKE CONSTRAINT ACTION] Non-admin user '{user_id}' denied execution of revoke_constraint"
        )
        try:
            from services.audit_logger import log_audit_event
            log_audit_event(
                event_type="UNAUTHORIZED_DIRECTIVE_ATTEMPT",
                actor={"type": "slack_user", "id": user_id or "unknown"},
                action="revoke_constraint",
                target=f"{channel_id}:{thread_ts}",
                details=params,
                severity="CRITICAL",
                status="BLOCKED",
            )
        except Exception as ae:  # noqa: BLE001
            logger.error(f"[REVOKE CONSTRAINT ACTION] Failed to log audit event: {ae}")

        if channel_id and thread_ts:
            await post_system_message(
                channel_id=channel_id,
                thread_ts=thread_ts,
                text="⛔ *Permission Denied*: Only workspace administrators can execute `/revoke_constraint`.",
            )
        return {"ok": False, "halted": True, "action": "revoke_constraint", "reason": "unauthorized"}

    target = str(params.get("target", "") or params.get("arg", "")).strip().lower()

    logger.info(
        f"[REVOKE CONSTRAINT ACTION] Revoking '{target}' for thread {channel_id}:{thread_ts}"
    )

    msg = ""
    if not target or target in ("all", "everything"):
        thread_state_manager.revoke_all(channel_id, thread_ts)
        msg = "🗑️ *Revoked all thread constraints* (word limits, language lock, inclusions, and exclusions) for this thread."
    elif target in ("max_words", "max-words", "maxwords"):
        thread_state_manager.clear_max_words(channel_id, thread_ts)
        msg = "🗑️ *Revoked constraint*: `max_words` cleared for this thread."
    elif target in ("min_words", "min-words", "minwords"):
        thread_state_manager.clear_min_words(channel_id, thread_ts)
        msg = "🗑️ *Revoked constraint*: `min_words` cleared for this thread."
    elif target in ("scribe_max_words", "scribe-max-words", "scribemaxwords"):
        thread_state_manager.clear_scribe_max_words(channel_id, thread_ts)
        msg = "🗑️ *Revoked constraint*: `scribe_max_words` cleared for this thread."
    elif target in ("scribe_min_words", "scribe-min-words", "scribeminwords"):
        thread_state_manager.clear_scribe_min_words(channel_id, thread_ts)
        msg = "🗑️ *Revoked constraint*: `scribe_min_words` cleared for this thread."
    elif target in ("exclude", "exclude_agents", "excluded"):
        thread_state_manager.clear_excluded_agents(channel_id, thread_ts)
        msg = "🗑️ *Revoked constraint*: Agent exclusions cleared for this thread."
    elif target in ("include", "include_agents", "included"):
        thread_state_manager.clear_included_agents(channel_id, thread_ts)
        msg = "🗑️ *Revoked constraint*: Forced agent inclusions cleared for this thread."
    elif target in ("language", "lang"):
        thread_state_manager.clear_language(channel_id, thread_ts)
        msg = "🗑️ *Revoked language lock*: Thread language cleared."
    elif target in ("modifiers", "modifier"):
        thread_state_manager.clear_format_modifiers(channel_id, thread_ts)
        msg = "🗑️ *Revoked constraint*: `format_modifiers` cleared for this thread."
    else:
        logger.warning(f"[REVOKE CONSTRAINT ACTION] Unknown revoke target '{target}' for thread {channel_id}:{thread_ts}")
        msg = f"⚠️ *Unknown Revoke Target*: `{target}`. Available: `max_words`, `min_words`, `scribe_max_words`, `scribe_min_words`, `exclude`, `include`, `language`, `modifiers`, `all`."

    if channel_id and thread_ts and msg:
        await post_system_message(channel_id=channel_id, thread_ts=thread_ts, text=msg)

    return {"ok": True, "halted": True, "action": "revoke_constraint"}
