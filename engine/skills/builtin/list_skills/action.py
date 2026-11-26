"""
Action handler for the list_skills skill.
Executes when /list_skills, /list-skills, or /skills is parsed.
Deterministically formats the thread-aware skills directory from SkillRegistry,
posts it as a system message, and halts further agent invocation.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def run(ctx: Any, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    from core.registry import get_skill_registry
    from services.slack.messages import post_system_message

    # /list_skills is strictly exclusive: presents platform skills directory and halts discussion
    inp = getattr(ctx, "input", ctx)
    channel_id = getattr(inp, "channel_id", "")
    thread_ts = getattr(inp, "thread_ts", "")

    skills_directory_text = get_skill_registry().format_skills_directory_markdown(
        channel_id=channel_id,
        thread_ts=thread_ts,
    )

    # Post message as system message (Slack delivery failures must not undo standalone command halt)
    try:
        await post_system_message(
            channel_id=channel_id,
            thread_ts=thread_ts,
            text=skills_directory_text,
        )
    except Exception as post_err:  # noqa: BLE001
        logger.warning(f"[LIST SKILLS] Failed to post skills directory to {channel_id}:{thread_ts}: {post_err}")

    # Halt pipeline so no more agents are called
    return {"ok": True, "halted": True, "action": "list_skills"}
