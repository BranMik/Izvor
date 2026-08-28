import asyncio
import logging
import time
from typing import Any

import litellm
from starlette.concurrency import run_in_threadpool

from config import THREAD_NOTES_MODEL, THREAD_NOTES_PROMPT
from llm_utils import (
    extract_anthropic_cache_usage,
    extract_cached_tokens,
    is_anthropic_model,
    is_openai_model,
    log_llm_cache_usage,
    log_openai_cached_tokens,
)
from slack_client import fetch_thread_history, post_thread_note, update_thread_note
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


async def generate_thread_notes(
    history: list[dict[str, str]],
    is_completed: bool = False,
) -> tuple[str, dict[str, int]]:
    """
    Calls LiteLLM using mistral-small-latest to generate structured strategic synthesis
    live notes of the conversation history. Returns (slack_mrkdwn_content, token_usage_dict).
    """
    if is_anthropic_model(THREAD_NOTES_MODEL):
        system_content: Any = [
            {
                "type": "text",
                "text": THREAD_NOTES_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ]
    else:
        system_content = THREAD_NOTES_PROMPT

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_content},
    ]

    # Count agent replies in history
    num_replies = sum(1 for m in history if m.get("role") == "assistant")

    if is_completed:
        status_directive = (
            "The discussion has finished. Generate the FINAL concise strategic synthesis notes (strictly under 500 words maximum ceiling). "
            "Include Core Objective & Problem Statement, Key Perspectives & Insights, Consensus & Strategic Decisions, Risks & Trade-offs, and Action Items/Radne stavke i sljedeći koraci. "
            "CRITICAL: Match the language of the discussion for ALL titles and content (if Croatian, write in Croatian e.g. 'Radne stavke i sljedeći koraci' for Action Items). "
            "Use status indicator _(Completed ✅)_ or _(Završeno ✅)_ in header."
        )
        max_tokens = 1500
    elif num_replies <= 1:
        status_directive = (
            "This is the initial turn of the discussion. Generate the initial concise strategic synthesis note (strictly under 50 words total). "
            "State the initial core objective and initial key perspective. "
            "CRITICAL: Match the language of the discussion for ALL titles and content. "
            "Use status indicator _(Updating live...)_ or _(Ažurira se uživo...)_ in header."
        )
        max_tokens = 350
    else:
        status_directive = (
            "Synthesize the discussion so far into updated concise strategic synthesis notes (strictly under 300-400 words total). "
            "Include Core Objective, Key Perspectives, Consensus, Risks, and Action Items/Radne stavke. "
            "CRITICAL: Match the language of the discussion for ALL titles and content (if Croatian, use Croatian titles). "
            "Use status indicator _(Updating live...)_ or _(Ažurira se uživo...)_ in header."
        )
        max_tokens = 1200

    if history:
        messages.extend(history)
        messages.append(
            {
                "role": "user",
                "content": status_directive,
            }
        )
    else:
        messages.append(
            {
                "role": "user",
                "content": "Initialize discussion notes for this thread.",
            }
        )

    completion_kwargs: dict[str, Any] = {
        "model": THREAD_NOTES_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
    }

    response = await run_in_threadpool(
        litellm.completion,
        **completion_kwargs,
    )
    log_llm_cache_usage(
        THREAD_NOTES_MODEL, response, "Thread_Notes_Agent", logger
    )

    raw_content = response.choices[0].message.content or ""

    # Clean markdown code-blocks if model wrapped in ```
    cleaned_content = raw_content.strip()
    if cleaned_content.startswith("```markdown"):
        cleaned_content = cleaned_content[len("```markdown") :].strip()
    elif cleaned_content.startswith("```"):
        cleaned_content = cleaned_content[len("```") :].strip()
    if cleaned_content.endswith("```"):
        cleaned_content = cleaned_content[:-3].strip()

    # Extract token usage
    usage = getattr(response, "usage", None)
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0

    if usage:
        if isinstance(usage, dict):
            prompt_tokens = usage.get("prompt_tokens", 0) or 0
            completion_tokens = usage.get("completion_tokens", 0) or 0
            total_tokens = usage.get("total_tokens", 0) or (
                prompt_tokens + completion_tokens
            )
        else:
            prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
            completion_tokens = getattr(usage, "completion_tokens", 0) or 0
            total_tokens = getattr(usage, "total_tokens", 0) or (
                prompt_tokens + completion_tokens
            )

    cached_tokens = 0
    if is_openai_model(THREAD_NOTES_MODEL):
        cached_tokens = extract_cached_tokens(response)
    elif is_anthropic_model(THREAD_NOTES_MODEL):
        cached_tokens = extract_anthropic_cache_usage(response).get("cache_read", 0)

    token_stats = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cached_tokens": cached_tokens,
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

            # 2. Generate updated structured synthesis notes with mistral-small-latest
            notes_content, token_stats = await generate_thread_notes(
                history, is_completed=is_completed
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
                    cached_tokens=token_stats.get("cached_tokens", 0),
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
