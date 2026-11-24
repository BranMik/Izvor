import asyncio
import logging
import time
from typing import Any

from core.task_utils import safe_create_task
from nlp.message_builder import markdown_to_slack
from pipeline.discussion_scribe import discussion_scribe
from services.slack import (
    fetch_thread_history,
    post_thread_note,
    unpin_thread_note,
    update_thread_note,
)
from services.thread_notes_writer import save_thread_notes_to_disk

from .usage_agent import record_thread_tokens, trigger_usage_update

logger = logging.getLogger(__name__)

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


def _is_usable_scribe_output(content: str | None) -> bool:
    """Returns True when generated scribe/conclusion text contains substantive content."""
    if not content or not content.strip():
        return False
    stripped = content.strip()
    return not stripped.startswith(("*(Synthesis unavailable:", "*(Conclusion unavailable:"))


def _extract_note_payload(raw_content: str, tokens: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Strips markdown code fences from Scribe output and normalizes token stats with model attribution."""
    cleaned_content = (raw_content or "").strip()
    if cleaned_content.startswith("```markdown"):
        cleaned_content = cleaned_content[len("```markdown") :].strip()
    elif cleaned_content.startswith("```"):
        cleaned_content = cleaned_content[len("```") :].strip()
    if cleaned_content.endswith("```"):
        cleaned_content = cleaned_content[:-3].strip()

    prompt_tokens = tokens.get("prompt_tokens", 0)
    completion_tokens = tokens.get("completion_tokens", 0)
    total_tokens = prompt_tokens + completion_tokens
    token_stats: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }
    if tokens.get("model"):
        token_stats["model"] = tokens["model"]
    return cleaned_content, token_stats



def get_thread_lock(channel_id: str, thread_ts: str) -> asyncio.Lock:
    """Returns or creates an asyncio.Lock for a specific thread."""
    thread_key = (channel_id, thread_ts)
    if thread_key not in THREAD_LOCKS:
        THREAD_LOCKS[thread_key] = asyncio.Lock()
    return THREAD_LOCKS[thread_key]


def _get_notes_row(channel_id: str, thread_ts: str) -> dict[str, Any] | None:
    """Queries persistent thread notes metadata directly from SQLite thread_notes_state."""
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
            return dict(row) if row else None
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[THREAD NOTES] Failed reading notes state from SQLite: {e}")
        return None


def _upsert_notes_state(channel_id: str, thread_ts: str, **updates: Any) -> None:
    """Persists thread notes state updates directly into SQLite thread_notes_state."""
    now = time.time()
    try:
        from core.database import get_db_transaction

        with get_db_transaction(immediate=True) as conn:
            row = conn.execute(
                """
                SELECT live_note_ts, live_note_content, conclusion_note_ts, conclusion_note_content, is_live_pinned
                FROM thread_notes_state
                WHERE channel_id = ? AND thread_ts = ?;
                """,
                (channel_id, thread_ts),
            ).fetchone()

            data: dict[str, Any] = dict(row) if row else {
                "live_note_ts": None,
                "live_note_content": None,
                "conclusion_note_ts": None,
                "conclusion_note_content": None,
                "is_live_pinned": 0,
            }
            for k, v in updates.items():
                if k in data:
                    if k == "is_live_pinned":
                        data[k] = int(bool(v))
                    else:
                        data[k] = str(v) if v is not None else None

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
                    data["live_note_ts"],
                    data["live_note_content"],
                    data["conclusion_note_ts"],
                    data["conclusion_note_content"],
                    data["is_live_pinned"],
                    now,
                ),
            )
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[THREAD NOTES] Failed upserting notes state to SQLite: {e}")


def is_live_note_pinned(channel_id: str, thread_ts: str) -> bool:
    """Returns whether the live synthesis note is marked as pinned in SQLite thread_notes_state."""
    row = _get_notes_row(channel_id, thread_ts)
    return bool(row and row.get("is_live_pinned"))


def set_live_note_pinned(channel_id: str, thread_ts: str, pinned: bool) -> None:
    """Sets whether the live synthesis note is pinned in SQLite thread_notes_state."""
    _upsert_notes_state(channel_id, thread_ts, is_live_pinned=pinned)


def get_thread_note(channel_id: str, thread_ts: str) -> str | None:
    """Returns the message_ts of the live note associated with a thread from SQLite."""
    row = _get_notes_row(channel_id, thread_ts)
    return str(row["live_note_ts"]) if row and row.get("live_note_ts") is not None else None


def set_thread_note(channel_id: str, thread_ts: str, message_ts: str) -> None:
    """Stores the message_ts of the live note associated with a thread in SQLite."""
    _upsert_notes_state(channel_id, thread_ts, live_note_ts=str(message_ts))


def get_thread_note_content(channel_id: str, thread_ts: str) -> str | None:
    """Returns the latest strategic synthesis notes text associated with a thread from SQLite."""
    row = _get_notes_row(channel_id, thread_ts)
    return str(row["live_note_content"]) if row and row.get("live_note_content") is not None else None


def set_thread_note_content(channel_id: str, thread_ts: str, content: str) -> None:
    """Stores the latest strategic synthesis notes text associated with a thread in SQLite."""
    _upsert_notes_state(channel_id, thread_ts, live_note_content=content)


def get_thread_conclusion_note(channel_id: str, thread_ts: str) -> str | None:
    """Returns the message_ts of the conclusion note associated with a thread from SQLite."""
    row = _get_notes_row(channel_id, thread_ts)
    return str(row["conclusion_note_ts"]) if row and row.get("conclusion_note_ts") is not None else None


def set_thread_conclusion_note(channel_id: str, thread_ts: str, message_ts: str) -> None:
    """Stores the message_ts of the conclusion note associated with a thread in SQLite."""
    _upsert_notes_state(channel_id, thread_ts, conclusion_note_ts=str(message_ts))


def get_thread_conclusion_content(channel_id: str, thread_ts: str) -> str | None:
    """Returns the latest conclusion notes text associated with a thread from SQLite."""
    row = _get_notes_row(channel_id, thread_ts)
    return str(row["conclusion_note_content"]) if row and row.get("conclusion_note_content") is not None else None


def set_thread_conclusion_content(channel_id: str, thread_ts: str, content: str) -> None:
    """Stores the latest conclusion notes text associated with a thread in SQLite."""
    _upsert_notes_state(channel_id, thread_ts, conclusion_note_content=content)


def _split_note_into_section_blocks(
    formatted_text: str,
    max_total_chars: int = 3800,
    max_block_chars: int = 2900,
) -> list[dict[str, Any]]:
    """
    Splits note text into one or more Slack section blocks (up to max_total_chars total)
    so each block strictly respects Slack's 3,000-character section block limit.
    """
    clean_text = formatted_text.strip()
    if len(clean_text) > max_total_chars:
        cutoff = max_total_chars - 30
        truncated = clean_text[:cutoff]
        last_nl = truncated.rfind("\n")
        if last_nl > cutoff - 200:
            truncated = truncated[:last_nl]
        clean_text = f"{truncated.strip()}\n_...[truncated]_"

    if len(clean_text) <= max_block_chars:
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": clean_text,
                },
            }
        ]

    # Split across multiple section blocks on newline boundaries
    blocks: list[dict[str, Any]] = []
    lines = clean_text.splitlines(keepends=True)
    current_chunk = ""

    for line in lines:
        if len(current_chunk) + len(line) <= max_block_chars:
            current_chunk += line
        else:
            if current_chunk:
                blocks.append(
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": current_chunk.strip(),
                        },
                    }
                )
                current_chunk = ""
            while len(line) > max_block_chars:
                blocks.append(
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": line[:max_block_chars].strip(),
                        },
                    }
                )
                line = line[max_block_chars:]
            current_chunk = line

    if current_chunk and current_chunk.strip():
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": current_chunk.strip(),
                },
            }
        )

    return blocks if blocks else [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": clean_text,
            },
        }
    ]


def build_scribe_note_blocks(
    formatted_text: str, channel_id: str, thread_ts: str
) -> list[dict[str, Any]]:
    """
    Constructs Slack Block Kit blocks for Live Discussion Scribe message,
    supporting multi-block splitting up to 3,800 chars and an action button to download as .txt.
    """
    blocks = _split_note_into_section_blocks(formatted_text)
    blocks.append(
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
        }
    )
    return blocks


def build_conclusion_note_blocks(
    formatted_text: str, channel_id: str, thread_ts: str
) -> list[dict[str, Any]]:
    """
    Constructs Slack Block Kit blocks for Discussion Conclusion message,
    supporting multi-block splitting up to 3,800 chars and an action button to download as .txt.
    """
    blocks = _split_note_into_section_blocks(formatted_text)
    blocks.append(
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
        }
    )
    return blocks


def evict_thread_note_cache(channel_id: str, thread_ts: str) -> None:
    """Removes in-memory runtime locks and cancels pending tasks without touching SQLite SSOT."""
    from core.task_utils import safe_cancel_task

    thread_key = (channel_id, thread_ts)
    THREAD_LOCKS.pop(thread_key, None)
    _PENDING_UPDATES.pop(thread_key, None)
    _PENDING_COMPLETED.pop(thread_key, None)
    task = _RUNNING_TASKS.pop(thread_key, None)
    safe_cancel_task(task)


def clear_thread_note(channel_id: str, thread_ts: str) -> None:
    """Removes live and conclusion note tracking, cancels tasks, and deletes persistent DB state."""
    evict_thread_note_cache(channel_id, thread_ts)
    try:
        from core.database import get_db_transaction

        with get_db_transaction(immediate=True) as conn:
            conn.execute(
                "DELETE FROM thread_notes_state WHERE channel_id = ? AND thread_ts = ?;",
                (channel_id, thread_ts),
            )
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[THREAD NOTES] Failed deleting notes state from SQLite: {e}")


def reset_thread_notes() -> None:
    """Clears all in-memory thread notes runtime state and cancels any pending tasks across all threads."""
    for task in list(_RUNNING_TASKS.values()):
        if task and not task.done():
            task.cancel()
    THREAD_LOCKS.clear()
    _RUNNING_TASKS.clear()
    _PENDING_UPDATES.clear()
    _PENDING_COMPLETED.clear()
    try:
        from core.database import get_db_transaction

        with get_db_transaction(immediate=True) as conn:
            conn.execute("DELETE FROM thread_notes_state;")
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[THREAD NOTES] Failed clearing SQLite thread_notes_state on reset: {e}")


try:
    from orchestration.thread_state import thread_state_manager

    thread_state_manager.register_cleanup_hook(evict_thread_note_cache)
    thread_state_manager.register_reset_hook(reset_thread_notes)
except (ImportError, AttributeError, TypeError) as _e:
    logger.debug(f"[THREAD NOTES] Could not register thread_state hooks: {_e}")

try:
    from services.retention_service import register_thread_purge_hook

    register_thread_purge_hook("thread_notes", clear_thread_note)
except (ImportError, AttributeError, TypeError) as _re:
    logger.debug(f"[THREAD NOTES] Could not register retention purge hook: {_re}")


async def generate_thread_notes(
    history: list[dict[str, str]],
    is_completed: bool = False,
    thread_language: str = "English",
    scribe_max_words: int | None = None,
    scribe_min_words: int | None = None,
) -> tuple[str, dict[str, Any]]:
    """
    Generates structured strategic synthesis live notes of the conversation history
    via the DiscussionScribe service. Returns (raw_markdown_content, token_usage_dict);
    Slack mrkdwn conversion happens at the posting boundary in update_notes_for_thread.
    """
    brief, tokens = await discussion_scribe.generate_brief(
        history,
        is_completed=is_completed,
        thread_language=thread_language,
        scribe_max_words=scribe_max_words,
        scribe_min_words=scribe_min_words,
    )
    return _extract_note_payload(brief.raw_markdown or "", tokens)


async def generate_conclusion_notes(
    history: list[dict[str, str]],
    thread_language: str = "English",
    scribe_max_words: int | None = None,
    scribe_min_words: int | None = None,
) -> tuple[str, dict[str, Any]]:
    """
    Generates comprehensive Conclusion Notes analyzing problem statement,
    consensus, opposing viewpoints, and agent arguments via DiscussionScribe.
    Returns (raw_markdown_content, token_usage_dict);
    Slack mrkdwn conversion happens at the posting boundary in update_notes_for_thread.
    """
    conclusion, tokens = await discussion_scribe.generate_conclusion(
        history,
        thread_language=thread_language,
        scribe_max_words=scribe_max_words,
        scribe_min_words=scribe_min_words,
    )
    return _extract_note_payload(conclusion.raw_markdown or "", tokens)


async def update_notes_for_thread(
    channel_id: str,
    thread_ts: str,
    is_completed: bool = False,
    bot_user_id: str | None = None,
) -> dict[str, Any]:
    """
    Asynchronously creates or updates the live in-thread Strategic Synthesis note message,
    and upon discussion completion unpins the live note, generates a dialectical Conclusion note,
    pins the conclusion note, and persists both notes to data/thread_notes/ (IZVOR_THREAD_NOTES_DIR).
    """
    lock = get_thread_lock(channel_id, thread_ts)

    from config_store.app_settings import is_scribe_enabled
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
            live_prompt_tokens = token_stats.get("prompt_tokens", 0)
            live_completion_tokens = token_stats.get("completion_tokens", 0)
            live_model = token_stats.get("model") or discussion_scribe.model

            timestamp_str = time.strftime("%H:%M:%S")
            note_ts = get_thread_note(channel_id, thread_ts)
            formatted_text: str | None = None

            # 3. Post/update live note ONLY when substantive output was generated
            if _is_usable_scribe_output(notes_content):
                footer = f"\n\n_Last updated: {timestamp_str} | Scribe tokens: {token_stats['total_tokens']}_"
                formatted_text = _sanitize_note_text(f"{notes_content.strip()}{footer}", "Live Discussion Scribe")
                # Persist raw markdown for .txt downloads; convert to Slack mrkdwn only for the Slack-bound payload
                set_thread_note_content(channel_id, thread_ts, formatted_text)
                slack_text = markdown_to_slack(formatted_text)
                blocks = build_scribe_note_blocks(slack_text, channel_id, thread_ts)

                if not note_ts:
                    post_res = await post_thread_note(
                        channel_id=channel_id,
                        thread_ts=thread_ts,
                        text=slack_text,
                        pin=True,
                        username="Live Discussion Scribe",
                        icon_emoji=":memo:",
                        blocks=blocks,
                    )
                    note_ts = post_res.get("ts")
                    if note_ts:
                        set_thread_note(channel_id, thread_ts, note_ts)
                        set_live_note_pinned(channel_id, thread_ts, True)
                        logger.info(
                            f"[THREAD NOTES] Created and pinned live synthesis note {note_ts} for thread {thread_ts}"
                        )
                else:
                    await update_thread_note(
                        channel_id=channel_id,
                        message_ts=note_ts,
                        text=slack_text,
                        blocks=blocks,
                    )
                    logger.info(
                        f"[THREAD NOTES] Updated live synthesis note {note_ts} in thread {thread_ts}"
                    )
            else:
                existing_live = get_thread_note_content(channel_id, thread_ts)
                if existing_live and existing_live.strip():
                    formatted_text = existing_live
                    logger.warning(
                        f"[THREAD NOTES] Live synthesis returned empty/unavailable output for thread {thread_ts}; "
                        "preserving existing live note."
                    )
                else:
                    logger.warning(
                        f"[THREAD NOTES] Live synthesis returned empty/unavailable output for thread {thread_ts}; "
                        "skipping empty live note post."
                    )

            conclusion_ts = None
            formatted_conclusion_text = None
            conc_model: str | None = None
            conc_prompt_tokens = 0
            conc_completion_tokens = 0

            # 4. If conversation rounds have concluded, unpin live note, generate conclusion note, pin it, and persist
            if is_completed:
                # Unpin Live Scribe Note
                if note_ts and is_live_note_pinned(channel_id, thread_ts):
                    await unpin_thread_note(channel_id, note_ts)
                    set_live_note_pinned(channel_id, thread_ts, False)
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
                conc_prompt_tokens = conc_tokens.get("prompt_tokens", 0)
                conc_completion_tokens = conc_tokens.get("completion_tokens", 0)
                conc_model = conc_tokens.get("model") or discussion_scribe.model
                token_stats["prompt_tokens"] += conc_prompt_tokens
                token_stats["completion_tokens"] += conc_completion_tokens
                token_stats["total_tokens"] += conc_tokens.get("total_tokens", 0)

                if _is_usable_scribe_output(conc_content):
                    conc_footer = f"\n\n_Completed: {timestamp_str} | Conclusion tokens: {conc_tokens['total_tokens']}_"
                    formatted_conclusion_text = _sanitize_note_text(
                        f"{conc_content.strip()}{conc_footer}", "Discussion Conclusion"
                    )
                    # Persist raw markdown for .txt downloads; convert to Slack mrkdwn only for the Slack-bound payload
                    set_thread_conclusion_content(channel_id, thread_ts, formatted_conclusion_text)
                    slack_conclusion_text = markdown_to_slack(formatted_conclusion_text)
                    conc_blocks = build_conclusion_note_blocks(slack_conclusion_text, channel_id, thread_ts)

                    # Post or update in-place Conclusion Note
                    conclusion_ts = get_thread_conclusion_note(channel_id, thread_ts)
                    if not conclusion_ts:
                        post_conc_res = await post_thread_note(
                            channel_id=channel_id,
                            thread_ts=thread_ts,
                            text=slack_conclusion_text,
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
                            text=slack_conclusion_text,
                            blocks=conc_blocks,
                        )
                        logger.info(
                            f"[THREAD NOTES] Updated conclusion note {conclusion_ts} in thread {thread_ts}"
                        )
                else:
                    conclusion_ts = get_thread_conclusion_note(channel_id, thread_ts)
                    logger.warning(
                        f"[THREAD NOTES] Conclusion generation returned empty/unavailable output for thread {thread_ts}; "
                        "skipping conclusion note post."
                    )

                # 5. Persist usable notes to data/thread_notes/ (IZVOR_THREAD_NOTES_DIR)
                if formatted_text or formatted_conclusion_text:
                    try:
                        await asyncio.to_thread(
                            save_thread_notes_to_disk,
                            channel_id=channel_id,
                            thread_ts=thread_ts,
                            initial_prompt=initial_prompt,
                            live_notes_text=formatted_text,
                            conclusion_text=formatted_conclusion_text,
                        )
                    except Exception as disk_err:  # noqa: BLE001
                        logger.warning(
                            f"[THREAD NOTES] Failed saving thread notes to disk: {disk_err}"
                        )

            duration = time.time() - start_time
            if conc_model and conc_model != live_model:
                thread_notes_model = f"{live_model}, {conc_model}"
            else:
                thread_notes_model = live_model

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
                if conc_model and conc_model != live_model:
                    record_thread_tokens(
                        channel_id=channel_id,
                        thread_ts=thread_ts,
                        model_name=live_model,
                        prompt_tokens=live_prompt_tokens,
                        completion_tokens=live_completion_tokens,
                    )
                    record_thread_tokens(
                        channel_id=channel_id,
                        thread_ts=thread_ts,
                        model_name=conc_model,
                        prompt_tokens=conc_prompt_tokens,
                        completion_tokens=conc_completion_tokens,
                    )
                else:
                    record_thread_tokens(
                        channel_id=channel_id,
                        thread_ts=thread_ts,
                        model_name=live_model,
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
    pending_initial_completed = is_completed
    try:
        while True:
            _PENDING_UPDATES[thread_key] = False
            iter_completed = pending_initial_completed or _PENDING_COMPLETED.pop(
                thread_key, False
            )
            pending_initial_completed = False

            await update_notes_for_thread(
                channel_id=channel_id,
                thread_ts=thread_ts,
                is_completed=iter_completed,
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


_trigger_thread_notes_update_override: Any = None


def configure_trigger_thread_notes_update(fn: Any) -> None:
    """Configures an in-memory trigger_thread_notes_update override for tests and DI."""
    global _trigger_thread_notes_update_override
    _trigger_thread_notes_update_override = fn


def reset_trigger_thread_notes_update() -> None:
    """Resets the in-memory trigger_thread_notes_update override."""
    global _trigger_thread_notes_update_override
    _trigger_thread_notes_update_override = None


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
    if _trigger_thread_notes_update_override is not None:
        return _trigger_thread_notes_update_override(
            channel_id=channel_id,
            thread_ts=thread_ts,
            is_completed=is_completed,
            bot_user_id=bot_user_id,
        )

    from config_store.app_settings import is_scribe_enabled
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
        task = safe_create_task(coro, name=f"thread_notes_{thread_ts}")
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
