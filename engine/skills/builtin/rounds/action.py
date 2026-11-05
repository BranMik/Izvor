"""
Action handler for the rounds skill.
Executes when /rounds [x] or /next_round is parsed.
Sets or adjusts remaining debate rounds in thread state.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def run(ctx: Any, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    from orchestration.thread_state import thread_state_manager
    params = params or {}
    inp = getattr(ctx, "input", ctx)
    channel_id = getattr(inp, "channel_id", "")
    thread_ts = getattr(inp, "thread_ts", "")
    slash_cmd = str(params.get("slash_cmd", "")).lower()

    is_next_round = bool(
        "next_round" in slash_cmd
        or "next-round" in slash_cmd
        or "nextround" in slash_cmd
        or params.get("is_next_round")
    )

    raw_rounds = params.get("rounds")
    if raw_rounds is not None:
        try:
            num_rounds = int(raw_rounds)
        except (ValueError, TypeError):
            num_rounds = 1 if is_next_round else 2
    else:
        num_rounds = 1 if is_next_round else 2

    num_rounds = min(5, max(1, num_rounds))

    logger.info(
        f"[ROUNDS ACTION] Setting {num_rounds} round(s) for thread {channel_id}:{thread_ts} "
        f"(cmd: {slash_cmd}, is_next_round: {is_next_round})"
    )

    thread_state_manager.set_rounds(channel_id, thread_ts, num_rounds)

    return None
