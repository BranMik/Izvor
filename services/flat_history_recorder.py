"""
Flat History Recorder for Izvor.

Records and maintains a single Markdown conversation history file per Slack thread in flat-histories/.
Filename convention:
  <prompt10>_<thread_ts10>.md
  e.g., 'kako_napra_1712345678.md'
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from core.paths import FLAT_HISTORIES_DIR

logger = logging.getLogger(__name__)

DEFAULT_FLAT_HISTORIES_DIR = FLAT_HISTORIES_DIR
FLAT_HISTORIES_DIR = DEFAULT_FLAT_HISTORIES_DIR

# Known prefixes and indicators for automated test channels and threads
TEST_CHANNEL_PREFIXES = ("C_TEST", "TEST_", "test_", "mock_", "MOCK_")
TEST_CHANNEL_IDS = frozenset({
    "C_TEST",
    "C_FILE_TEST",
    "C123",
    "C12345",
    "C999",
    "test_channel",
    "TEST_CHANNEL",
})


def is_test_thread(
    channel_id: str | None = None,
    thread_ts: str | None = None,
) -> bool:
    """
    Returns True if the thread or channel is identified as part of automated testing
    (unit tests, integration tests, or E2E harness executions).
    """
    # If FLAT_HISTORIES_DIR has been redirected to an isolated test folder
    # (e.g. tmp_path in test_flat_history), allow writing to that test dir.
    try:
        if FLAT_HISTORIES_DIR.resolve() != DEFAULT_FLAT_HISTORIES_DIR.resolve():
            return False
    except OSError as e:
        logger.debug(f"[FLAT HISTORY] Failed to resolve directory paths: {e}")

    # 1. Check explicit test execution environment flags
    if (
        os.getenv("E2E_TESTING") == "1"
        or os.getenv("IS_E2E_TEST") == "1"
        or os.getenv("DISABLE_FLAT_HISTORY") == "1"
    ):
        return True

    # 2. Check if running under pytest and targeting the real project directory
    if os.getenv("PYTEST_CURRENT_TEST"):
        return True

    # 3. Check channel ID heuristics
    cid = (channel_id or "").strip()
    if cid and (cid in TEST_CHANNEL_IDS or cid.startswith(TEST_CHANNEL_PREFIXES)):
        return True

    # 4. Check thread timestamp heuristics
    tts = str(thread_ts or "").strip()
    if tts:
        # E2E test thread ID formats (e.g. T_01, T_08, T_15, T_26)
        if re.match(r"^T_\d+$", tts, re.IGNORECASE):
            return True
        # Synthetic mock timestamps starting with 1700000 (standard test epoch mock)
        if tts.startswith("1700000"):
            return True
        # Synthetic test run timestamps (e.g. 100001, 12345..., 200001, etc.)
        if re.match(r"^(12345|10000|20000|55500|66600|77777|88888|99999)\d*", tts):
            return True

    return False


def generate_flat_history_filename(prompt: str, thread_ts: str) -> str:
    """
    Generates filename starting with first 10 characters of cleaned user prompt
    and first 10 characters of thread_ts identifier.
    Example: 'kako_napra_1712345678.md'
    """
    # 1. Remove Slack user/bot mentions (<@U12345> and @AppName)
    clean = re.sub(r"<@[A-Z0-9]+>", "", prompt)
    clean = re.sub(r"@\w+", "", clean)
    # 2. Remove bracketed agent tags if present: [Agent]
    clean = re.sub(r"\[[^\]]+\]", "", clean)
    # 3. Remove slash commands: /rounds 2, /stop, /next_round, etc.
    clean = re.sub(r"/\w+(?:\s*[:=]?\s*\w+)?", "", clean)
    # 4. Slugify to safe filesystem alphanumeric tokens
    slug = re.sub(r"[^\w\s-]", "", clean.lower())
    slug = re.sub(r"[-\s]+", "_", slug).strip("_")

    prompt_prefix = slug[:10] if slug else "thread"
    clean_ts = re.sub(r"[^\w]", "", str(thread_ts))[:10] or "thread_ts"

    return f"{prompt_prefix}_{clean_ts}.md"


def get_flat_history_filepath(
    channel_id: str, thread_ts: str, prompt: str | None = None
) -> Path:
    """
    Returns the target Path for a thread's flat history file.
    If a file for this thread_ts already exists in flat-histories/, re-uses it (1 thread = 1 file).
    Disambiguates filename collisions between same-second threads using thread_ts suffix.
    """
    FLAT_HISTORIES_DIR.mkdir(parents=True, exist_ok=True)
    all_digits = re.sub(r"[^\w]", "", str(thread_ts))
    clean_ts = all_digits[:10]
    if clean_ts:
        existing = list(FLAT_HISTORIES_DIR.glob(f"*_{clean_ts}*.md"))
        for f in existing:
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
                if f"**Thread TS:** `{thread_ts}`" in content:
                    return f
            except OSError as e:
                logger.debug(f"[FLAT HISTORY] Could not read file {f}: {e}")

    filename = generate_flat_history_filename(prompt or "thread", thread_ts)
    candidate_path = FLAT_HISTORIES_DIR / filename
    if candidate_path.exists():
        try:
            content = candidate_path.read_text(encoding="utf-8", errors="ignore")
            if f"**Thread TS:** `{thread_ts}`" not in content:
                suffix = all_digits[-5:] if len(all_digits) >= 5 else "alt"
                filename = f"{candidate_path.stem}_{suffix}.md"
                candidate_path = FLAT_HISTORIES_DIR / filename
        except OSError:
            pass

    return candidate_path


def format_messages_to_markdown(
    messages: list[dict[str, Any]],
    channel_id: str,
    thread_ts: str,
    initial_prompt: str | None = None,
) -> str:
    """Formats conversation turns into structured Markdown with bold role/agent headers."""
    lines = [
        "# Thread Conversation History",
        "",
        f"- **Thread TS:** `{thread_ts}`",
        f"- **Channel ID:** `{channel_id}`",
        f"- **Last Updated:** `{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}`",
    ]
    if initial_prompt:
        clean_init = initial_prompt.strip()
        lines.append(f"- **Initial Prompt:** {clean_init}")
    lines.extend(["", "---", ""])

    last_role = None
    last_content = None
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "").strip()
        if not content:
            continue
        if role == last_role and content == last_content:
            continue
        last_role = role
        last_content = content

        if role == "user":
            speaker = "User"
            text = content
        elif role == "assistant":
            match = re.match(r"^\[([^\]]+)\]:\s*(.*)", content, flags=re.DOTALL)
            if match:
                speaker = match.group(1).strip()
                text = match.group(2).strip()
            else:
                speaker = msg.get("username") or "Assistant"
                text = content
        else:
            speaker = role.title()
            text = content

        lines.append(f"### **[{speaker}]**\n")
        lines.append(f"{text}\n")
        lines.append("---\n")

    return "\n".join(lines).strip() + "\n"


def record_flat_history(
    channel_id: str,
    thread_ts: str,
    initial_prompt: str | None = None,
    messages: list[dict[str, Any]] | None = None,
    bot_user_id: str | None = None,
    force: bool = False,
) -> Path | None:
    """
    Atomically writes the full conversation history to flat-histories/<prompt10>_<thread_ts10>.md.
    Guarantees 1 thread = 1 file.
    Does NOT write files for testing threads (e2e or unit tests) unless force=True.
    """
    if not force and is_test_thread(channel_id, thread_ts):
        logger.debug(
            f"[FLAT HISTORY] Skipping flat history creation for testing thread: channel={channel_id}, thread_ts={thread_ts}"
        )
        return None

    try:
        target_path = get_flat_history_filepath(channel_id, thread_ts, initial_prompt)
        effective_messages = messages or []
        content = format_messages_to_markdown(
            messages=effective_messages,
            channel_id=channel_id,
            thread_ts=thread_ts,
            initial_prompt=initial_prompt,
        )

        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(FLAT_HISTORIES_DIR),
            prefix=f"{target_path.stem}_",
            suffix=".tmp",
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, target_path)
            logger.info(
                f"[FLAT HISTORY] Saved conversation history for {thread_ts} to {target_path}"
            )
            return target_path
        except OSError:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise
    except Exception:
        logger.exception(
            f"[FLAT HISTORY] Failed to save flat history for thread {thread_ts}"
        )
        return None


async def async_record_flat_history(
    channel_id: str,
    thread_ts: str,
    initial_prompt: str | None = None,
    messages: list[dict[str, Any]] | None = None,
    bot_user_id: str | None = None,
    force: bool = False,
) -> Path | None:
    """Asynchronous wrapper for record_flat_history."""
    return await asyncio.to_thread(
        record_flat_history,
        channel_id=channel_id,
        thread_ts=thread_ts,
        initial_prompt=initial_prompt,
        messages=messages,
        bot_user_id=bot_user_id,
        force=force,
    )
