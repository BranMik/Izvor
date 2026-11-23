"""
Thread Notes Disk Persistence Service for Izvor.

Saves finalized Live Scribe Notes and Conclusion Notes to data/thread_notes/ (IZVOR_THREAD_NOTES_DIR).
Filename convention:
  <prompt_slug>_<channel_id>_<ts5>_live_notes.md
  <prompt_slug>_<channel_id>_<ts5>_conclusion.md
where ts5 is the last 5 digits of thread_ts (guaranteeing non-overlap for future identical prompts and channel isolation).
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import time
from pathlib import Path

from core.paths import THREAD_NOTES_DIR

logger = logging.getLogger(__name__)

_configured_thread_notes_dir: Path | None = None


def configure_paths(thread_notes_dir: Path | None = None) -> None:
    """Sets a custom directory for thread notes disk persistence (e.g. for test isolation)."""
    global _configured_thread_notes_dir
    _configured_thread_notes_dir = thread_notes_dir


def reset_paths() -> None:
    """Resets thread notes directory to default paths SSOT."""
    global _configured_thread_notes_dir
    _configured_thread_notes_dir = None


def get_thread_notes_dir() -> Path:
    """Resolves the active thread notes directory taking configured paths and IZVOR_THREAD_NOTES_DIR into account."""
    if _configured_thread_notes_dir is not None:
        return _configured_thread_notes_dir
    env_dir = os.getenv("IZVOR_THREAD_NOTES_DIR")
    if env_dir:
        return Path(env_dir)
    return THREAD_NOTES_DIR


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


def generate_notes_filename(
    prompt: str | None,
    thread_ts: str,
    note_type: str,
    channel_id: str | None = None,
) -> str:
    """
    Generates standard filename: <prompt_slug>[_<channel_id>]_<ts5>_<note_type>.md
    e.g. 'build_scalable_db_C123_84521_live_notes.md' or 'build_scalable_db_84521_live_notes.md'
    """
    slug = sanitize_prompt_slug(prompt)
    ts5 = extract_ts5_suffix(thread_ts)
    clean_cid = re.sub(r"[^a-zA-Z0-9_-]", "", str(channel_id))[:16] if channel_id else ""
    if clean_cid:
        return f"{slug}_{clean_cid}_{ts5}_{note_type}.md"
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
    Atomically writes the live notes and/or conclusion notes to data/thread_notes/ (IZVOR_THREAD_NOTES_DIR).
    If called repeatedly on thread re-engagement, safely updates the existing files.
    """
    out_paths: dict[str, str] = {}
    target_dir = get_thread_notes_dir()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error(f"[THREAD NOTES WRITER] Could not create directory {target_dir}: {e}")
        return out_paths

    # 1. Write live notes if present
    if live_notes_text and live_notes_text.strip():
        filename = generate_notes_filename(initial_prompt, thread_ts, "live_notes", channel_id=channel_id)
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
        filename = generate_notes_filename(initial_prompt, thread_ts, "conclusion", channel_id=channel_id)
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
    except OSError as e:
        if tmp_path_obj is not None:
            try:
                tmp_path_obj.unlink(missing_ok=True)
            except OSError:
                pass
        logger.error(f"[THREAD NOTES WRITER] Failed to atomically write {target_path}: {e}")
        return False
