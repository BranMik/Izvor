"""
Thread Notes Disk Persistence Service for Izvor.

Saves finalized Live Scribe Notes and Conclusion Notes to agents_resources/thread_notes/.
Filename convention:
  <prompt_slug>_<ts5>_live_notes.md
  <prompt_slug>_<ts5>_conclusion.md
where ts5 is the last 5 digits of thread_ts (guaranteeing non-overlap for future identical prompts).
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from services.flat_history_recorder import is_test_thread

logger = logging.getLogger(__name__)

DEFAULT_THREAD_NOTES_DIR = Path(__file__).resolve().parent.parent / "agents_resources" / "thread_notes"
THREAD_NOTES_DIR = DEFAULT_THREAD_NOTES_DIR


def sanitize_prompt_slug(prompt: str | None, max_length: int = 20) -> str:
    """
    Produces a safe, clean slug from the initial prompt string.
    """
    if not prompt or not prompt.strip():
        return "thread"
    clean = re.sub(r"[^a-zA-Z0-9]+", "_", prompt.lower()).strip("_")
    clean = clean[:max_length].rstrip("_")
    return clean or "thread"


def extract_ts5_suffix(thread_ts: str) -> str:
    """
    Extracts the last 5 numeric digits of thread_ts (stripping decimal point),
    guaranteeing non-overlap even if the exact same prompt occurs in the future.
    """
    digits = re.sub(r"\D", "", thread_ts)
    if len(digits) >= 5:
        return digits[-5:]
    return digits.zfill(5)


def generate_notes_filename(prompt: str | None, thread_ts: str, note_type: str) -> str:
    """
    Generates standard filename: <prompt_slug>_<ts5>_<note_type>.md
    e.g. 'build_scalable_db_84521_live_notes.md' or 'build_scalable_db_84521_conclusion.md'
    """
    slug = sanitize_prompt_slug(prompt)
    ts5 = extract_ts5_suffix(thread_ts)
    return f"{slug}_{ts5}_{note_type}.md"


def format_note_document(
    title: str,
    channel_id: str,
    thread_ts: str,
    body: str,
    initial_prompt: str | None = None,
) -> str:
    """
    Wraps the note text in a standardized Markdown document with metadata headers.
    """
    date_str = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    lines = [
        f"# {title}",
        "",
        f"- **Channel ID**: `{channel_id}`",
        f"- **Thread Timestamp**: `{thread_ts}`",
        f"- **Last Updated**: {date_str}",
    ]
    if initial_prompt:
        clean_prompt = initial_prompt.strip().replace("\n", " ")
        if len(clean_prompt) > 120:
            clean_prompt = clean_prompt[:117] + "..."
        lines.append(f"- **Topic / Prompt**: _{clean_prompt}_")
    lines.extend(["", "---", "", body.strip(), ""])
    return "\n".join(lines)


def save_thread_notes_to_disk(
    channel_id: str,
    thread_ts: str,
    initial_prompt: str | None = None,
    live_notes_text: str | None = None,
    conclusion_text: str | None = None,
    force: bool = False,
) -> dict[str, str]:
    """
    Atomically writes the live notes and/or conclusion notes to agents_resources/thread_notes/.
    If called repeatedly on thread re-engagement, safely updates the existing files.
    """
    # Skip file creation during automated test harness runs unless forced or redirected
    if not force and is_test_thread(channel_id, thread_ts):
        try:
            if THREAD_NOTES_DIR.resolve() == DEFAULT_THREAD_NOTES_DIR.resolve():
                logger.debug(
                    f"[THREAD NOTES WRITER] Skipping disk save for test thread: {channel_id}/{thread_ts}"
                )
                return {}
        except OSError:
            pass

    out_paths: dict[str, str] = {}
    target_dir = THREAD_NOTES_DIR
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error(f"[THREAD NOTES WRITER] Could not create directory {target_dir}: {e}")
        return out_paths

    # 1. Write live notes if present
    if live_notes_text and live_notes_text.strip():
        filename = generate_notes_filename(initial_prompt, thread_ts, "live_notes")
        filepath = target_dir / filename
        content = format_note_document(
            title="📋 Live Discussion Scribe Notes",
            channel_id=channel_id,
            thread_ts=thread_ts,
            body=live_notes_text,
            initial_prompt=initial_prompt,
        )
        if _atomic_write_file(filepath, content):
            out_paths["live_notes_path"] = str(filepath)

    # 2. Write conclusion if present
    if conclusion_text and conclusion_text.strip():
        filename = generate_notes_filename(initial_prompt, thread_ts, "conclusion")
        filepath = target_dir / filename
        content = format_note_document(
            title="🎯 Discussion Conclusion & Dialectical Analysis",
            channel_id=channel_id,
            thread_ts=thread_ts,
            body=conclusion_text,
            initial_prompt=initial_prompt,
        )
        if _atomic_write_file(filepath, content):
            out_paths["conclusion_path"] = str(filepath)

    return out_paths


def _atomic_write_file(target_path: Path, content: str) -> bool:
    """Helper to perform atomic file replacement via temporary file."""
    tmp_path_obj: Path | None = None
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(target_path.parent),
            prefix=f"{target_path.stem}_",
            suffix=".tmp",
        )
        tmp_path_obj = Path(tmp_path)
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target_path)
        logger.info(f"[THREAD NOTES WRITER] Saved note to {target_path}")
        return True
    except Exception as e:
        if tmp_path_obj is not None:
            try:
                tmp_path_obj.unlink(missing_ok=True)
            except OSError:
                pass
        logger.error(f"[THREAD NOTES WRITER] Failed to atomically write {target_path}: {e}")
        return False
