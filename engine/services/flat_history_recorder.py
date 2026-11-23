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

_configured_flat_histories_dir: Path | None = None


def configure_paths(flat_histories_dir: Path | None = None) -> None:
    """Sets a custom directory for flat conversation history files (e.g. for test isolation)."""
    global _configured_flat_histories_dir
    _configured_flat_histories_dir = flat_histories_dir


def reset_paths() -> None:
    """Resets flat histories directory to default paths SSOT."""
    global _configured_flat_histories_dir
    _configured_flat_histories_dir = None


def get_flat_histories_dir() -> Path:
    """Resolves the active flat histories directory taking configured paths and IZVOR_HISTORIES_DIR into account."""
    if _configured_flat_histories_dir is not None:
        return _configured_flat_histories_dir
    env_dir = os.getenv("IZVOR_HISTORIES_DIR")
    if env_dir:
        return Path(env_dir)
    return FLAT_HISTORIES_DIR


def generate_flat_history_filename(prompt: str, thread_ts: str, channel_id: str | None = None) -> str:
    """
    Generates filename starting with first 10 characters of cleaned user prompt,
    optional sanitized channel_id, and first 10 characters of thread_ts identifier.
    Example: 'kako_napra_C123_1712345678.md' or 'kako_napra_1712345678.md'
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
    clean_cid = re.sub(r"[^\w]", "", str(channel_id))[:12] if channel_id else ""

    if clean_cid:
        return f"{prompt_prefix}_{clean_cid}_{clean_ts}.md"
    return f"{prompt_prefix}_{clean_ts}.md"


def get_flat_history_filepath(
    channel_id: str, thread_ts: str, prompt: str | None = None
) -> Path:
    """
    Returns the target Path for a thread's flat history file.
    Re-uses an existing file only when BOTH Channel ID and Thread TS match (1 channel+thread = 1 file).
    Disambiguates filename collisions between same-second threads using thread_ts suffix.
    """
    hist_dir = get_flat_histories_dir()
    hist_dir.mkdir(parents=True, exist_ok=True)
    all_digits = re.sub(r"[^\w]", "", str(thread_ts))
    clean_ts = all_digits[:10]
    expected_ts_hdr = f"**Thread TS:** `{thread_ts}`"
    expected_cid_hdr = f"**Channel ID:** `{channel_id}`"
    if clean_ts:
        existing = list(hist_dir.glob(f"*_{clean_ts}*.md"))
        for f in existing:
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
                if expected_ts_hdr in content and expected_cid_hdr in content:
                    return f
            except OSError as e:
                logger.debug(f"[FLAT HISTORY] Could not read file {f}: {e}")

    filename = generate_flat_history_filename(prompt or "thread", thread_ts, channel_id=channel_id)
    candidate_path = hist_dir / filename
    if candidate_path.exists():
        try:
            content = candidate_path.read_text(encoding="utf-8", errors="ignore")
            if expected_ts_hdr not in content or expected_cid_hdr not in content:
                suffix = all_digits[-5:] if len(all_digits) >= 5 else "alt"
                filename = f"{candidate_path.stem}_{suffix}.md"
                candidate_path = hist_dir / filename
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
    force: bool = False,
) -> Path | None:
    """
    Atomically writes the full conversation history to flat-histories/<prompt10>_<thread_ts10>.md.
    Guarantees 1 thread = 1 file.
    """
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
            dir=str(target_path.parent),
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
    force: bool = False,
) -> Path | None:
    """Asynchronous wrapper for record_flat_history."""
    return await asyncio.to_thread(
        record_flat_history,
        channel_id=channel_id,
        thread_ts=thread_ts,
        initial_prompt=initial_prompt,
        messages=messages,
        force=force,
    )
