"""
Action handler for the revoke skill.
Executes when /revoke [target] is parsed.
Revokes designated thread constraints or all constraints.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def run(ctx: Any, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    from orchestration.thread_state import thread_state_manager

    params = params or {}
    channel_id = getattr(ctx, "channel_id", "")
    thread_ts = getattr(ctx, "thread_ts", "")
    target = str(params.get("target", "") or params.get("arg", "")).strip().lower()

    logger.info(
        f"[REVOKE ACTION] Revoking '{target}' for thread {channel_id}:{thread_ts}"
    )

    if not target or target in ("all", "everything"):
        thread_state_manager.revoke_all(channel_id, thread_ts)
    elif target in ("max_words", "max-words", "maxwords"):
        thread_state_manager.clear_max_words(channel_id, thread_ts)
    elif target in ("min_words", "min-words", "minwords"):
        thread_state_manager.clear_min_words(channel_id, thread_ts)
    elif target in ("scribe_max_words", "scribe-max-words", "scribemaxwords"):
        thread_state_manager.clear_scribe_max_words(channel_id, thread_ts)
    elif target in ("scribe_min_words", "scribe-min-words", "scribeminwords"):
        thread_state_manager.clear_scribe_min_words(channel_id, thread_ts)
    elif target in ("exclude", "exclude_agents", "excluded"):
        thread_state_manager.clear_excluded_agents(channel_id, thread_ts)
    elif target in ("include", "include_agents", "included"):
        thread_state_manager.clear_included_agents(channel_id, thread_ts)
    elif target in ("language", "lang"):
        thread_state_manager.clear_language(channel_id, thread_ts)
    elif target in ("modifiers", "modifier"):
        thread_state_manager.clear_format_modifiers(channel_id, thread_ts)
    else:
        logger.warning(f"[REVOKE ACTION] Unknown revoke target '{target}' for thread {channel_id}:{thread_ts}")

    return None
