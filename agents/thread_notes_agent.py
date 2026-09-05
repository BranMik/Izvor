import asyncio
import logging
import time
from typing import Any


from core.config import THREAD_NOTES_MODEL
from services.slack_client import (
    fetch_thread_history,
    post_thread_note,
    update_thread_note,
)
from .usage_agent import record_thread_tokens, trigger_usage_update

logger = logging.getLogger(__name__)

# Map thread identifier (channel_id, thread_ts) -> live_note_message_ts
THREAD_NOTE_MSG_MAP: dict[tuple[str, str], str] = {}

# Per-thread locks to ensure sequential note updates without race conditions
THREAD_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}

# Track active background worker task and pending updates per thread
_RUNNING_TASKS: dict[tuple[str, str], asyncio.Task] = {}
_PENDING_UPDATES: dict[tuple[str, str], bool] = {}
_PENDING_COMPLETED: dict[tuple[str, str], bool] = {}


def get_thread_lock(channel_id: str, thread_ts: str) -> asyncio.Lock:
    """Returns or creates an asyncio.Lock for a specific thread."""
    thread_key = (channel_id, thread_ts)
    if thread_key not in THREAD_LOCKS:
        THREAD_LOCKS[thread_key] = asyncio.Lock()
    return THREAD_LOCKS[thread_key]


def get_thread_note(channel_id: str, thread_ts: str) -> str | None:
    """Returns the message_ts of the live note associated with a thread."""
    val = THREAD_NOTE_MSG_MAP.get((channel_id, thread_ts))
    if isinstance(val, dict):
        return val.get("note_ts") or val.get("synthesis_ts") or val.get("agents_ts")
    return val


def set_thread_note(channel_id: str, thread_ts: str, message_ts: str | dict[str, Any]) -> None:
    """Stores the message_ts of the live note associated with a thread."""
    thread_key = (channel_id, thread_ts)
    if isinstance(message_ts, dict):
        ts = message_ts.get("note_ts") or message_ts.get("synthesis_ts") or message_ts.get("agents_ts")
        THREAD_NOTE_MSG_MAP[thread_key] = str(ts)
    else:
        THREAD_NOTE_MSG_MAP[thread_key] = str(message_ts)


def clear_thread_note(channel_id: str, thread_ts: str) -> None:
    """Removes live note tracking and cancels any running/pending tasks for a thread."""
    thread_key = (channel_id, thread_ts)
    THREAD_NOTE_MSG_MAP.pop(thread_key, None)
    THREAD_LOCKS.pop(thread_key, None)
    _PENDING_UPDATES[thread_key] = False
    _PENDING_COMPLETED[thread_key] = False
    task = _RUNNING_TASKS.pop(thread_key, None)
    if task and not task.done():
        task.cancel()


from pipeline.discussion_scribe import discussion_scribe


async def generate_thread_notes(
    history: list[dict[str, str]],
    is_completed: bool = False,
    thread_language: str = "English",
) -> tuple[str, dict[str, int]]:
    """
    Generates structured strategic synthesis live notes of the conversation history
    via the DiscussionScribe service. Returns (slack_mrkdwn_content, token_usage_dict).
    """
    brief, tokens = await discussion_scribe.generate_brief(
        history, is_completed=is_completed, thread_language=thread_language
    )
    raw_content = brief.raw_markdown or ""

    # Clean markdown code-blocks if model wrapped in ```
    cleaned_content = raw_content.strip()
    if cleaned_content.startswith("```markdown"):
        cleaned_content = cleaned_content[len("```markdown") :].strip()
    elif cleaned_content.startswith("```"):
        cleaned_content = cleaned_content[len("```") :].strip()
    if cleaned_content.endswith("```"):
        cleaned_content = cleaned_content[:-3].strip()

    prompt_tokens = tokens.get("prompt_tokens", 0)
    completion_tokens = tokens.get("completion_tokens", 0)
    total_tokens = prompt_tokens + completion_tokens
    token_stats = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }

    return cleaned_content, token_stats



async def update_notes_for_thread(
    channel_id: str,
    thread_ts: str,
    is_completed: bool = False,
    bot_user_id: str | None = None,
) -> dict[str, Any]:
    """
    Asynchronously creates or updates the single live in-thread Strategic Synthesis note message for the specified thread.
    """
    thread_key = (channel_id, thread_ts)
    lock = get_thread_lock(channel_id, thread_ts)

    async with lock:
        start_time = time.time()
        logger.info(
            f"[THREAD NOTES] Starting live strategic synthesis sync for thread {thread_ts} in channel {channel_id} (completed={is_completed})..."
        )

        try:
            # 1. Fetch current conversation history (excludes previous scribe notes)
            history = await fetch_thread_history(
                channel_id=channel_id,
                thread_ts=thread_ts,
                bot_user_id=bot_user_id,
            )

            if not history:
                logger.warning(
                    f"[THREAD NOTES] No thread history found for thread {thread_ts}. Skipping notes update."
                )
                return {"ok": False, "reason": "No thread history"}

            # Retrieve locked thread language if set
            from orchestration.thread_state import get_thread_language
            thread_language = get_thread_language(channel_id, thread_ts) or "English"

            # 2. Generate updated structured synthesis notes with mistral-small-latest
            notes_content, token_stats = await generate_thread_notes(
                history, is_completed=is_completed, thread_language=thread_language
            )

            # Append timestamp / token footnote
            timestamp_str = time.strftime("%H:%M:%S")
            footer = f"\n\n_Last updated: {timestamp_str} | Scribe tokens: {token_stats['total_tokens']}_"
            formatted_text = f"{notes_content.strip()}{footer}"

            # 3. Post new note message or update existing note in-place
            note_ts = get_thread_note(channel_id, thread_ts)
            if not note_ts:
                post_res = await post_thread_note(
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    text=formatted_text,
                    pin=True,
                    username="Live Discussion Scribe",
                    icon_emoji=":memo:",
                )
                note_ts = post_res.get("ts")
                if note_ts:
                    set_thread_note(channel_id, thread_ts, note_ts)
                    logger.info(
                        f"[THREAD NOTES] Created and pinned live synthesis note {note_ts} for thread {thread_ts}"
                    )
            else:
                await update_thread_note(
                    channel_id=channel_id,
                    message_ts=note_ts,
                    text=formatted_text,
                )
                logger.info(
                    f"[THREAD NOTES] Updated live synthesis note {note_ts} in thread {thread_ts}"
                )

            duration = time.time() - start_time

            # 4. Mandatory Log: Tokens spent and latency
            logger.info(
                f"[THREAD NOTES] 📊 Live Note Update Completed | "
                f"Thread: {thread_ts} (Channel: {channel_id}) | "
                f"Note TS: {note_ts} | "
                f"Model: {THREAD_NOTES_MODEL} | "
                f"Tokens: Prompt={token_stats['prompt_tokens']}, "
                f"Completion={token_stats['completion_tokens']}, "
                f"Total={token_stats['total_tokens']} | "
                f"Duration: {duration:.2f}s"
            )

            # Record Scribe agent tokens into thread usage tracker and trigger usage update
            try:
                record_thread_tokens(
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    model_name=THREAD_NOTES_MODEL,
                    prompt_tokens=token_stats["prompt_tokens"],
                    completion_tokens=token_stats["completion_tokens"],
                )
                trigger_usage_update(
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    is_completed=is_completed,
                )
            except Exception as usage_err:
                logger.debug(f"[THREAD NOTES] Non-critical usage sync error: {usage_err}")

            return {
                "ok": True,
                "note_ts": note_ts,
                "token_stats": token_stats,
                "duration": duration,
            }

        except asyncio.CancelledError:
            logger.info(f"[THREAD NOTES] Sync cancelled for thread {thread_ts}")
            raise
        except Exception as e:
            logger.exception(
                f"[THREAD NOTES] ⚠️ Error updating live notes for thread {thread_ts}: {e}"
            )
            return {"ok": False, "error": str(e)}


async def _notes_worker(
    channel_id: str,
    thread_ts: str,
    is_completed: bool = False,
    bot_user_id: str | None = None,
) -> None:
    """
    Background worker loop that processes thread notes updates and automatically
    picks up any pending updates queued while the worker was busy.
    """
    thread_key = (channel_id, thread_ts)
    current_completed = is_completed
    try:
        while True:
            _PENDING_UPDATES[thread_key] = False
            current_completed = current_completed or _PENDING_COMPLETED.get(
                thread_key, False
            )
            _PENDING_COMPLETED[thread_key] = False

            await update_notes_for_thread(
                channel_id=channel_id,
                thread_ts=thread_ts,
                is_completed=current_completed,
                bot_user_id=bot_user_id,
            )
            if not _PENDING_UPDATES.get(thread_key, False):
                break
    except asyncio.CancelledError:
        logger.info(f"[THREAD NOTES] Worker task cancelled for thread {thread_ts}")
    finally:
        _RUNNING_TASKS.pop(thread_key, None)
        _PENDING_UPDATES.pop(thread_key, None)
        _PENDING_COMPLETED.pop(thread_key, None)


def trigger_thread_notes_update(
    channel_id: str,
    thread_ts: str,
    is_completed: bool = False,
    bot_user_id: str | None = None,
) -> asyncio.Task | None:
    """
    Dispatches live thread note creation/update as a non-blocking background task.
    Coalesces rapid successive triggers so only one worker runs with pending follow-up.
    """
    thread_key = (channel_id, thread_ts)
    if is_completed:
        _PENDING_COMPLETED[thread_key] = True

    existing_task = _RUNNING_TASKS.get(thread_key)
    if existing_task and not existing_task.done():
        _PENDING_UPDATES[thread_key] = True
        logger.info(
            f"[THREAD NOTES] Notes update already active for thread {thread_ts}. Queued pending sync."
        )
        return existing_task

    try:
        task = asyncio.create_task(
            _notes_worker(
                channel_id=channel_id,
                thread_ts=thread_ts,
                is_completed=is_completed,
                bot_user_id=bot_user_id,
            )
        )
        _RUNNING_TASKS[thread_key] = task
        _PENDING_UPDATES[thread_key] = False
        return task
    except Exception as e:
        logger.error(
            f"[THREAD NOTES] Failed to dispatch background notes update task: {e}"
        )
        return None
