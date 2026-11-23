"""Slack interactive actions for private Scribe and Discussion Conclusion note downloads."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import BackgroundTasks, Response
from slack_sdk.errors import SlackApiError

from agents.thread_notes_agent import (
    get_thread_conclusion_content,
    get_thread_note_content,
)
from services.audit_logger import log_audit_event_async
from services.slack import (
    open_dm_channel,
    post_ephemeral_to_slack,
    upload_text_file_to_slack,
)

logger = logging.getLogger("IzvorBackend.SlackInteractivity.Scribe")


async def _handle_scribe_download(
    user_id: str,
    channel_id: str,
    thread_ts: str,
    content: str,
    filename_prefix: str = "scribe_notes",
    title_prefix: str = "Live Discussion Scribe Notes",
) -> None:
    """Delivers notes privately to the requesting user's DM channel with ephemeral thread feedback."""
    try:
        dm_channel_id = await open_dm_channel(user_id)
        if not dm_channel_id:
            logger.error(f"[INTERACTIVITY] Could not resolve DM channel for user {user_id}")
            if channel_id and thread_ts:
                await post_ephemeral_to_slack(
                    channel_id=channel_id,
                    user_id=user_id,
                    text=(
                        "❌ Could not open a direct message channel to deliver your notes. "
                        "Please ensure direct messages from apps are allowed and the bot has the 'im:write' permission."
                    ),
                    thread_ts=thread_ts,
                )
            return

        clean_ts = thread_ts.replace(".", "_") if thread_ts else "notes"
        filename = f"{filename_prefix}_{clean_ts}.txt"
        upload_res = await upload_text_file_to_slack(
            channel_id=dm_channel_id,
            content=content,
            filename=filename,
            title=f"{title_prefix} ({thread_ts})",
            initial_comment=f"Here are your requested {title_prefix} as a .txt file:",
        )

        if not upload_res.get("ok"):
            err_msg = upload_res.get("error", "Unknown error")
            logger.error(f"[INTERACTIVITY] Failed to upload notes file to DM for {user_id}: {err_msg}")
            if channel_id and thread_ts:
                await post_ephemeral_to_slack(
                    channel_id=channel_id,
                    user_id=user_id,
                    text=f"❌ Failed to deliver notes file to your direct message: {err_msg}",
                    thread_ts=thread_ts,
                )
            return

        if channel_id and thread_ts:
            await post_ephemeral_to_slack(
                channel_id=channel_id,
                user_id=user_id,
                text="✅ Prepared notes have been sent as a `.txt` file to your direct message (DM).",
                thread_ts=thread_ts,
            )
    except (SlackApiError, OSError, RuntimeError, ValueError, TypeError) as e:
        logger.error(f"[INTERACTIVITY] Error delivering scribe download to {user_id}: {e}")
        if channel_id and thread_ts:
            try:
                await post_ephemeral_to_slack(
                    channel_id=channel_id,
                    user_id=user_id,
                    text=f"❌ An unexpected error occurred while preparing your notes download: {e}",
                    thread_ts=thread_ts,
                )
            except (SlackApiError, OSError, RuntimeError, ValueError, TypeError) as eph_err:
                logger.debug(f"[INTERACTIVITY] Ephemeral error notification failed: {eph_err}")


async def _handle_download_scribe_notes(
    action: dict[str, Any],
    payload: dict[str, Any],
    user_id: str | None,
    background_tasks: BackgroundTasks,
    view_state: dict[str, Any],
) -> Response | None:
    """
    Validates channel ownership of the button payload (anti-IDOR) and queues DM note delivery.
    Rejects missing or mismatched event channel context with HTTP 403 and a CRITICAL audit event.
    """
    action_id = action.get("action_id", "")
    val = action.get("value", "")

    channel_obj = payload.get("channel") if isinstance(payload.get("channel"), dict) else {}
    container_obj = payload.get("container") if isinstance(payload.get("container"), dict) else {}
    message_obj = payload.get("message") if isinstance(payload.get("message"), dict) else {}

    event_channel_id = str(channel_obj.get("id") or container_obj.get("channel_id") or "").strip()

    if ":" in val:
        parsed_channel_id, thread_ts = (part.strip() for part in val.split(":", 1))
    else:
        parsed_channel_id = event_channel_id
        thread_ts = str(container_obj.get("thread_ts") or message_obj.get("thread_ts") or "").strip()

    if not event_channel_id or not parsed_channel_id or parsed_channel_id != event_channel_id:
        logger.warning(
            "[SECURITY] Blocked scribe note download with mismatched or missing channel ownership "
            "(parsed=%r, event=%r, user=%r)",
            parsed_channel_id,
            event_channel_id,
            user_id,
        )
        await log_audit_event_async(
            event_type="UNAUTHORIZED_SCRIBE_DOWNLOAD",
            actor={"type": "slack_user", "id": user_id or "unknown"},
            action="download_scribe_notes",
            target=f"{parsed_channel_id or 'missing'}:{thread_ts or 'missing'}",
            details={
                "parsed_channel_id": parsed_channel_id,
                "event_channel_id": event_channel_id,
                "thread_ts": thread_ts,
                "action_id": action_id,
            },
            severity="CRITICAL",
            status="BLOCKED",
        )
        return Response(status_code=403)

    is_conclusion = action_id == "download_conclusion_notes_txt"
    if is_conclusion:
        notes_text = (
            get_thread_conclusion_content(parsed_channel_id, thread_ts)
            if parsed_channel_id and thread_ts
            else None
        )
    else:
        notes_text = (
            get_thread_note_content(parsed_channel_id, thread_ts)
            if parsed_channel_id and thread_ts
            else None
        )

    if not notes_text:
        blocks = message_obj.get("blocks", [])
        for b in blocks:
            if b.get("type") == "section":
                notes_text = b.get("text", {}).get("text", "")
                break
        if not notes_text:
            notes_text = message_obj.get("text", "")
    if not notes_text:
        fallback_label = "Discussion Conclusion" if is_conclusion else "Live Discussion Scribe"
        notes_text = f"*(No {fallback_label} notes recorded for this thread)*"

    if user_id:
        background_tasks.add_task(
            _handle_scribe_download,
            user_id=user_id,
            channel_id=parsed_channel_id,
            thread_ts=thread_ts,
            content=notes_text,
            filename_prefix="conclusion_notes" if is_conclusion else "scribe_notes",
            title_prefix="Discussion Conclusion" if is_conclusion else "Live Discussion Scribe Notes",
        )
    return None

