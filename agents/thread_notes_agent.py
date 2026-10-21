import asyncio
import logging
import time
from typing import Any

from core.config import get_thread_notes_model
from pipeline.discussion_scribe import discussion_scribe
from services.slack_client import (
    fetch_thread_history,
    post_thread_note,
    unpin_thread_note,
    update_thread_note,
)
from services.thread_notes_writer import save_thread_notes_to_disk

from .usage_agent import record_thread_tokens, trigger_usage_update

logger = logging.getLogger(__name__)

# Map thread identifier (channel_id, thread_ts) -> live_note_message_ts
THREAD_NOTE_MSG_MAP: dict[tuple[str, str], str] = {}

# Map thread identifier (channel_id, thread_ts) -> latest_notes_text
THREAD_NOTE_TEXT_MAP: dict[tuple[str, str], str] = {}

# Map thread identifier (channel_id, thread_ts) -> conclusion_message_ts
THREAD_CONCLUSION_MSG_MAP: dict[tuple[str, str], str] = {}

# Map thread identifier (channel_id, thread_ts) -> latest_conclusion_text
THREAD_CONCLUSION_TEXT_MAP: dict[tuple[str, str], str] = {}

# Track whether live note is currently pinned
THREAD_LIVE_PINNED_MAP: dict[tuple[str, str], bool] = {}

# Per-thread locks to ensure sequential note updates without race conditions
THREAD_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}

# Track active background worker task and pending updates per thread
_RUNNING_TASKS: dict[tuple[str, str], asyncio.Task] = {}
_PENDING_UPDATES: dict[tuple[str, str], bool] = {}
_PENDING_COMPLETED: dict[tuple[str, str], bool] = {}


def _sanitize_note_text(text: str, persona_name: str = "Live Discussion Scribe") -> str:
    """Scans and sanitizes note text for leaked credentials and neutralizes broadcast mentions."""
    from core.slack_security import sanitize_slack_broadcast_mentions
    from services.security import scan_output_for_secrets

    matched_rule = scan_output_for_secrets(text, user_id=persona_name)
    if matched_rule:
        logger.warning(f"[SECURITY] Scribe note blocked by guardrail rule: {matched_rule}")
        return f"⚠️ *Note content redacted by security policy.* (Rule: {matched_rule})"
    return sanitize_slack_broadcast_mentions(text)



def get_thread_lock(channel_id: str, thread_ts: str) -> asyncio.Lock:
    """Returns or creates an asyncio.Lock for a specific thread."""
    thread_key = (channel_id, thread_ts)
    if thread_key not in THREAD_LOCKS:
        THREAD_LOCKS[thread_key] = asyncio.Lock()
    return THREAD_LOCKS[thread_key]


def _load_notes_state_from_db(channel_id: str, thread_ts: str) -> None:
    """Loads thread notes metadata from SQLite thread_notes_state into L1 memory cache on cache miss."""
    thread_key = (channel_id, thread_ts)
    try:
        from core.database import get_connection

        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT live_note_ts, live_note_content, conclusion_note_ts, conclusion_note_content, is_live_pinned
                FROM thread_notes_state
                WHERE channel_id = ? AND thread_ts = ?;
                """,
                (channel_id, thread_ts),
            ).fetchone()
            if row:
                if row["live_note_ts"] is not None:
                    THREAD_NOTE_MSG_MAP[thread_key] = str(row["live_note_ts"])
                if row["live_note_content"] is not None:
                    THREAD_NOTE_TEXT_MAP[thread_key] = str(row["live_note_content"])
                if row["conclusion_note_ts"] is not None:
                    THREAD_CONCLUSION_MSG_MAP[thread_key] = str(row["conclusion_note_ts"])
                if row["conclusion_note_content"] is not None:
                    THREAD_CONCLUSION_TEXT_MAP[thread_key] = str(row["conclusion_note_content"])
                THREAD_LIVE_PINNED_MAP[thread_key] = bool(row["is_live_pinned"])
    except Exception as e:
        logger.debug(f"[THREAD NOTES] Failed loading notes state from SQLite: {e}")


def _sync_notes_state_to_db(channel_id: str, thread_ts: str) -> None:
    """Persists current in-memory notes state for a thread to SQLite thread_notes_state."""
    thread_key = (channel_id, thread_ts)
    live_ts = THREAD_NOTE_MSG_MAP.get(thread_key)
    live_content = THREAD_NOTE_TEXT_MAP.get(thread_key)
    conclusion_ts = THREAD_CONCLUSION_MSG_MAP.get(thread_key)
    conclusion_content = THREAD_CONCLUSION_TEXT_MAP.get(thread_key)
    is_live_pinned = int(bool(THREAD_LIVE_PINNED_MAP.get(thread_key, False)))
    now = time.time()

    try:
        from core.database import get_db_transaction

        with get_db_transaction(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO thread_notes_state (
                    channel_id, thread_ts, live_note_ts, live_note_content,
                    conclusion_note_ts, conclusion_note_content, is_live_pinned, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel_id, thread_ts) DO UPDATE SET
                    live_note_ts = excluded.live_note_ts,
                    live_note_content = excluded.live_note_content,
                    conclusion_note_ts = excluded.conclusion_note_ts,
                    conclusion_note_content = excluded.conclusion_note_content,
                    is_live_pinned = excluded.is_live_pinned,
                    updated_at = excluded.updated_at;
                """,
                (
                    channel_id,
                    thread_ts,
                    live_ts,
                    live_content,
                    conclusion_ts,
                    conclusion_content,
                    is_live_pinned,
                    now,
                ),
            )
    except Exception as e:
        logger.debug(f"[THREAD NOTES] Failed syncing notes state to SQLite: {e}")


def get_thread_note(channel_id: str, thread_ts: str) -> str | None:
    """Returns the message_ts of the live note associated with a thread."""
    thread_key = (channel_id, thread_ts)
    if thread_key not in THREAD_NOTE_MSG_MAP:
        _load_notes_state_from_db(channel_id, thread_ts)
    val = THREAD_NOTE_MSG_MAP.get(thread_key)
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
    _sync_notes_state_to_db(channel_id, thread_ts)


def get_thread_note_content(channel_id: str, thread_ts: str) -> str | None:
    """Returns the latest strategic synthesis notes text associated with a thread."""
    thread_key = (channel_id, thread_ts)
    if thread_key not in THREAD_NOTE_TEXT_MAP:
        _load_notes_state_from_db(channel_id, thread_ts)
    return THREAD_NOTE_TEXT_MAP.get(thread_key)


def set_thread_note_content(channel_id: str, thread_ts: str, content: str) -> None:
    """Stores the latest strategic synthesis notes text associated with a thread."""
    THREAD_NOTE_TEXT_MAP[(channel_id, thread_ts)] = content
    _sync_notes_state_to_db(channel_id, thread_ts)


def get_thread_conclusion_note(channel_id: str, thread_ts: str) -> str | None:
    """Returns the message_ts of the conclusion note associated with a thread."""
    thread_key = (channel_id, thread_ts)
    if thread_key not in THREAD_CONCLUSION_MSG_MAP:
        _load_notes_state_from_db(channel_id, thread_ts)
    return THREAD_CONCLUSION_MSG_MAP.get(thread_key)


def set_thread_conclusion_note(channel_id: str, thread_ts: str, message_ts: str) -> None:
    """Stores the message_ts of the conclusion note associated with a thread."""
    THREAD_CONCLUSION_MSG_MAP[(channel_id, thread_ts)] = str(message_ts)
    _sync_notes_state_to_db(channel_id, thread_ts)


def get_thread_conclusion_content(channel_id: str, thread_ts: str) -> str | None:
    """Returns the latest conclusion notes text associated with a thread."""
    thread_key = (channel_id, thread_ts)
    if thread_key not in THREAD_CONCLUSION_TEXT_MAP:
        _load_notes_state_from_db(channel_id, thread_ts)
    return THREAD_CONCLUSION_TEXT_MAP.get(thread_key)


def set_thread_conclusion_content(channel_id: str, thread_ts: str, content: str) -> None:
    """Stores the latest conclusion notes text associated with a thread."""
    THREAD_CONCLUSION_TEXT_MAP[(channel_id, thread_ts)] = content
    _sync_notes_state_to_db(channel_id, thread_ts)


def build_scribe_note_blocks(
    formatted_text: str, channel_id: str, thread_ts: str
) -> list[dict[str, Any]]:
    """
    Constructs Slack Block Kit blocks for Live Discussion Scribe message,
    including a section with the notes and an action button to download as .txt.
    """
    section_text = (
        formatted_text
        if len(formatted_text) <= 2900
        else formatted_text[:2890] + "\n_...[truncated]_"
    )
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": section_text,
            },
        },
        {
            "type": "actions",
            "block_id": "scribe_actions_block",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "📥 Download Notes (.txt)",
                        "emoji": True,
                    },
                    "action_id": "download_scribe_notes_txt",
                    "value": f"{channel_id}:{thread_ts}",
                }
            ],
        },
    ]


def build_conclusion_note_blocks(
    formatted_text: str, channel_id: str, thread_ts: str
) -> list[dict[str, Any]]:
    """
    Constructs Slack Block Kit blocks for Discussion Conclusion message,
    including a section with the conclusion and an action button to download as .txt.
    """
    section_text = (
        formatted_text
        if len(formatted_text) <= 2900
        else formatted_text[:2890] + "\n_...[truncated]_"
    )
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": section_text,
            },
        },
        {
            "type": "actions",
            "block_id": "conclusion_actions_block",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "📥 Download Conclusion (.txt)",
                        "emoji": True,
                    },
                    "action_id": "download_conclusion_notes_txt",
                    "value": f"{channel_id}:{thread_ts}",
                }
            ],
        },
    ]


def clear_thread_note(channel_id: str, thread_ts: str) -> None:
    """Removes live and conclusion note tracking and cancels any running/pending tasks for a thread."""
    thread_key = (channel_id, thread_ts)
    THREAD_NOTE_MSG_MAP.pop(thread_key, None)
    THREAD_NOTE_TEXT_MAP.pop(thread_key, None)
    THREAD_CONCLUSION_MSG_MAP.pop(thread_key, None)
    THREAD_CONCLUSION_TEXT_MAP.pop(thread_key, None)
    THREAD_LIVE_PINNED_MAP.pop(thread_key, None)
    THREAD_LOCKS.pop(thread_key, None)
    _PENDING_UPDATES.pop(thread_key, None)
    _PENDING_COMPLETED.pop(thread_key, None)
    task = _RUNNING_TASKS.pop(thread_key, None)
    if task and not task.done():
        task.cancel()
    try:
        from core.database import get_db_transaction

        with get_db_transaction(immediate=True) as conn:
            conn.execute(
                "DELETE FROM thread_notes_state WHERE channel_id = ? AND thread_ts = ?;",
                (channel_id, thread_ts),
            )
    except Exception as e:
        logger.debug(f"[THREAD NOTES] Failed deleting notes state from SQLite: {e}")


def reset_thread_notes() -> None:
    """Clears all in-memory thread notes state and cancels any pending tasks across all threads."""
    for task in list(_RUNNING_TASKS.values()):
        if task and not task.done():
            task.cancel()
    THREAD_NOTE_MSG_MAP.clear()
    THREAD_NOTE_TEXT_MAP.clear()
    THREAD_CONCLUSION_MSG_MAP.clear()
    THREAD_CONCLUSION_TEXT_MAP.clear()
    THREAD_LIVE_PINNED_MAP.clear()
    THREAD_LOCKS.clear()
    _RUNNING_TASKS.clear()
    _PENDING_UPDATES.clear()
    _PENDING_COMPLETED.clear()
    try:
        from core.database import get_db_transaction

        with get_db_transaction(immediate=True) as conn:
            conn.execute("DELETE FROM thread_notes_state;")
    except Exception as e:
        logger.debug(f"[THREAD NOTES] Failed clearing SQLite thread_notes_state on reset: {e}")


try:
    from orchestration.thread_state import thread_state_manager

    thread_state_manager.register_cleanup_hook(clear_thread_note)
    thread_state_manager.register_reset_hook(reset_thread_notes)
except (ImportError, AttributeError, TypeError) as _e:
    logger.debug(f"[THREAD NOTES] Could not register thread_state hooks: {_e}")


async def generate_thread_notes(
    history: list[dict[str, str]],
    is_completed: bool = False,
    thread_language: str = "English",
    scribe_max_words: int | None = None,
    scribe_min_words: int | None = None,
) -> tuple[str, dict[str, int]]:
    """
    Generates structured strategic synthesis live notes of the conversation history
    via the DiscussionScribe service. Returns (slack_mrkdwn_content, token_usage_dict).
    """
    brief, tokens = await discussion_scribe.generate_brief(
        history,
        is_completed=is_completed,
        thread_language=thread_language,
        scribe_max_words=scribe_max_words,
        scribe_min_words=scribe_min_words,
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


async def generate_conclusion_notes(
    history: list[dict[str, str]],
    thread_language: str = "English",
    scribe_max_words: int | None = None,
    scribe_min_words: int | None = None,
) -> tuple[str, dict[str, int]]:
    """
    Generates comprehensive Conclusion Notes analyzing problem statement,
    consensus, opposing viewpoints, and agent arguments via DiscussionScribe.
    Returns (slack_mrkdwn_content, token_usage_dict).
    """
    conclusion, tokens = await discussion_scribe.generate_conclusion(
        history,
        thread_language=thread_language,
        scribe_max_words=scribe_max_words,
        scribe_min_words=scribe_min_words,
    )
    raw_content = conclusion.raw_markdown or ""

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
    Asynchronously creates or updates the live in-thread Strategic Synthesis note message,
    and upon discussion completion unpins the live note, generates a dialectical Conclusion note,
    pins the conclusion note, and persists both notes to agents_resources/thread_notes/.
    """
    lock = get_thread_lock(channel_id, thread_ts)
    thread_key = (channel_id, thread_ts)

    from orchestration.agent_settings import is_scribe_enabled
    if not is_scribe_enabled():
        logger.info(
            f"[THREAD NOTES] Scribe is disabled in settings. Skipping update for thread {thread_ts} in channel {channel_id}."
        )
        return {"ok": False, "reason": "Scribe is disabled"}

    async with lock:
        start_time = time.time()
        logger.info(
            f"[THREAD NOTES] Starting live strategic synthesis sync for thread {thread_ts} in channel {channel_id} (completed={is_completed})..."
        )

        try:
            # 1. Fetch current conversation history (excludes previous scribe/conclusion notes)
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

            # Retrieve locked thread language and scribe word constraints if set
            from orchestration.thread_state import thread_state_manager
            thread_language = thread_state_manager.get_language(channel_id, thread_ts) or "English"
            scribe_max_words = thread_state_manager.get_scribe_max_words(channel_id, thread_ts)
            scribe_min_words = thread_state_manager.get_scribe_min_words(channel_id, thread_ts)
            initial_prompt = thread_state_manager.get_initial_prompt(channel_id, thread_ts)
            if not initial_prompt:
                for h in history:
                    if h.get("role") == "user":
                        initial_prompt = h.get("content")
                        break

            # 2. Generate updated live synthesis notes
            notes_content, token_stats = await generate_thread_notes(
                history,
                is_completed=is_completed,
                thread_language=thread_language,
                scribe_max_words=scribe_max_words,
                scribe_min_words=scribe_min_words,
            )

            # Append timestamp / token footnote
            timestamp_str = time.strftime("%H:%M:%S")
            footer = f"\n\n_Last updated: {timestamp_str} | Scribe tokens: {token_stats['total_tokens']}_"
            formatted_text = f"{notes_content.strip()}{footer}"
            formatted_text = _sanitize_note_text(formatted_text, "Live Discussion Scribe")
            set_thread_note_content(channel_id, thread_ts, formatted_text)
            blocks = build_scribe_note_blocks(formatted_text, channel_id, thread_ts)

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
                    blocks=blocks,
                )
                note_ts = post_res.get("ts")
                if note_ts:
                    set_thread_note(channel_id, thread_ts, note_ts)
                    THREAD_LIVE_PINNED_MAP[thread_key] = True
                    _sync_notes_state_to_db(channel_id, thread_ts)
                    logger.info(
                        f"[THREAD NOTES] Created and pinned live synthesis note {note_ts} for thread {thread_ts}"
                    )
            else:
                await update_thread_note(
                    channel_id=channel_id,
                    message_ts=note_ts,
                    text=formatted_text,
                    blocks=blocks,
                )
                logger.info(
                    f"[THREAD NOTES] Updated live synthesis note {note_ts} in thread {thread_ts}"
                )

            conclusion_ts = None
            formatted_conclusion_text = None

            # 4. If conversation rounds have concluded, unpin live note, generate conclusion note, pin it, and persist
            if is_completed:
                # Unpin Live Scribe Note
                if note_ts and THREAD_LIVE_PINNED_MAP.get(thread_key, True):
                    await unpin_thread_note(channel_id, note_ts)
                    THREAD_LIVE_PINNED_MAP[thread_key] = False
                    _sync_notes_state_to_db(channel_id, thread_ts)
                    logger.info(
                        f"[THREAD NOTES] Unpinned live note {note_ts} upon completion for thread {thread_ts}"
                    )

                # Generate dedicated Conclusion Note
                conc_content, conc_tokens = await generate_conclusion_notes(
                    history,
                    thread_language=thread_language,
                    scribe_max_words=scribe_max_words,
                    scribe_min_words=scribe_min_words,
                )
                token_stats["prompt_tokens"] += conc_tokens.get("prompt_tokens", 0)
                token_stats["completion_tokens"] += conc_tokens.get("completion_tokens", 0)
                token_stats["total_tokens"] += conc_tokens.get("total_tokens", 0)

                conc_footer = f"\n\n_Completed: {timestamp_str} | Conclusion tokens: {conc_tokens['total_tokens']}_"
                formatted_conclusion_text = f"{conc_content.strip()}{conc_footer}"
                formatted_conclusion_text = _sanitize_note_text(formatted_conclusion_text, "Discussion Conclusion")
                set_thread_conclusion_content(channel_id, thread_ts, formatted_conclusion_text)
                conc_blocks = build_conclusion_note_blocks(formatted_conclusion_text, channel_id, thread_ts)

                # Post or update in-place Conclusion Note
                conclusion_ts = get_thread_conclusion_note(channel_id, thread_ts)
                if not conclusion_ts:
                    post_conc_res = await post_thread_note(
                        channel_id=channel_id,
                        thread_ts=thread_ts,
                        text=formatted_conclusion_text,
                        pin=True,
                        username="Discussion Conclusion",
                        icon_emoji=":scales:",
                        blocks=conc_blocks,
                    )
                    conclusion_ts = post_conc_res.get("ts")
                    if conclusion_ts:
                        set_thread_conclusion_note(channel_id, thread_ts, conclusion_ts)
                        logger.info(
                            f"[THREAD NOTES] Created and pinned conclusion note {conclusion_ts} for thread {thread_ts}"
                        )
                else:
                    await update_thread_note(
                        channel_id=channel_id,
                        message_ts=conclusion_ts,
                        text=formatted_conclusion_text,
                        blocks=conc_blocks,
                    )
                    logger.info(
                        f"[THREAD NOTES] Updated conclusion note {conclusion_ts} in thread {thread_ts}"
                    )

                # 5. Persist both notes to agents_resources/thread_notes/
                try:
                    save_thread_notes_to_disk(
                        channel_id=channel_id,
                        thread_ts=thread_ts,
                        initial_prompt=initial_prompt,
                        live_notes_text=formatted_text,
                        conclusion_text=formatted_conclusion_text,
                    )
                except Exception as disk_err:
                    logger.warning(
                        f"[THREAD NOTES] Failed saving thread notes to disk: {disk_err}"
                    )

            duration = time.time() - start_time
            thread_notes_model = get_thread_notes_model()

            # Mandatory Log: Tokens spent and latency
            logger.info(
                f"[THREAD NOTES] 📊 Live Note Update Completed | "
                f"Thread: {thread_ts} (Channel: {channel_id}) | "
                f"Note TS: {note_ts} | "
                f"Conclusion TS: {conclusion_ts} | "
                f"Model: {thread_notes_model} | "
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
                    model_name=thread_notes_model,
                    prompt_tokens=token_stats["prompt_tokens"],
                    completion_tokens=token_stats["completion_tokens"],
                )
                trigger_usage_update(
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    is_completed=is_completed,
                )
            except (KeyError, ValueError, TypeError, AttributeError, OSError) as usage_err:
                logger.debug(f"[THREAD NOTES] Non-critical usage sync error: {usage_err}")

            return {
                "ok": True,
                "note_ts": note_ts,
                "conclusion_ts": conclusion_ts,
                "token_stats": token_stats,
                "duration": duration,
            }

        except asyncio.CancelledError:
            logger.info(f"[THREAD NOTES] Sync cancelled for thread {thread_ts}")
            raise
        except (TimeoutError, asyncio.TimeoutError) as e:
            logger.warning(
                f"[THREAD NOTES] Timeout updating live notes for thread {thread_ts}: {e}"
            )
            return {"ok": False, "error": str(e)}
        except (KeyError, ValueError, TypeError) as e:
            logger.error(
                f"[THREAD NOTES] Data error updating live notes for thread {thread_ts}: {e}"
            )
            return {"ok": False, "error": str(e)}
        except Exception as e:
            logger.exception(
                f"[THREAD NOTES] ⚠️ Error updating live notes for thread {thread_ts}"
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
    from orchestration.agent_settings import is_scribe_enabled
    if not is_scribe_enabled():
        logger.info(
            f"[THREAD NOTES] Scribe is disabled in settings. Skipping update for thread {thread_ts}."
        )
        return None

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

    coro = _notes_worker(
        channel_id=channel_id,
        thread_ts=thread_ts,
        is_completed=is_completed,
        bot_user_id=bot_user_id,
    )
    try:
        task = asyncio.create_task(coro)
        _RUNNING_TASKS[thread_key] = task
        _PENDING_UPDATES[thread_key] = False
        return task
    except RuntimeError as e:
        coro.close()
        logger.error(
            f"[THREAD NOTES] Runtime error dispatching background notes update task: {e}"
        )
        return None
    except Exception as e:  # noqa: BLE001 - Background task dispatcher error boundary
        coro.close()
        logger.error(
            f"[THREAD NOTES] Failed to dispatch background notes update task: {e}"
        )
        return None
