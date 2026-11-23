"""Action handler for the forget_thread GDPR purge orchestrator skill."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def run(ctx: Any, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Executes channel-scoped GDPR thread erasure and halts further turn stages."""
    from services.audit_logger import log_audit_event_async
    from services.retention_service import forget_thread
    from services.slack.messages import post_system_message

    inp = getattr(ctx, "input", ctx)
    channel_id = str(getattr(inp, "channel_id", "") or "")
    thread_ts = str(getattr(inp, "thread_ts", "") or "")
    user_id = str(getattr(inp, "user_id", "") or "unknown")

    if not channel_id or not thread_ts:
        return {
            "ok": False,
            "halted": True,
            "action": "forget_thread",
            "error": "Missing channel_id or thread_ts.",
        }

    summary = await asyncio.to_thread(forget_thread, channel_id, thread_ts)
    await log_audit_event_async(
        event_type="GDPR_THREAD_FORGOTTEN",
        actor={"type": "user", "id": user_id},
        action="forget_thread",
        target=f"{channel_id}:{thread_ts}",
        details=summary,
        severity="WARNING",
        status="SUCCESS",
    )
    try:
        await post_system_message(
            channel_id,
            "🧹 *Thread Data Forgotten (GDPR Purge)*: All persisted histories, notes, downloads, and memory caches for this channel thread have been permanently deleted.",
            thread_ts=thread_ts,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[FORGET THREAD ACTION] Failed to post confirmation to {channel_id}:{thread_ts}: {exc}")

    return {
        "ok": True,
        "halted": True,
        "action": "forget_thread",
        "deleted": summary,
    }

