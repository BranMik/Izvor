import asyncio
import json
import logging
from typing import Any

import litellm
from starlette.concurrency import run_in_threadpool

from config import INTENT_EXTRACTOR_MODEL
from nl_patterns import is_stop_command

logger = logging.getLogger(__name__)

EXTRACTOR_SYSTEM_PROMPT = """You are a precise, language-agnostic intent and parameter extractor for an AI multi-agent Slack platform.
Analyze the user's message and extract any requested constraints or directives:

1. "max_words": Integer maximum word count if the user requested a maximum length ceiling (e.g., "in under 50 words", "u najviše 100 riječi", "max 30 words", "briefly in 40 words", "keep it under 80 words", "up to 60 words", "no more than 75 words"). Otherwise null.
   NOTE: Do NOT extract minimum word requests into max_words (e.g. "at least 100 words" is NOT max_words).

2. "history": Integer number of previous messages/turns to consider if the user requested a history or context window limit (e.g., "last 3 messages", "uzmi u obzir zadnje 2 poruke", "based on past 5 messages", "limit history to 4 messages"). Otherwise null.

3. "file_use": Boolean true if the user asks to read, analyze, inspect, summarize, parse, review, or reference an attached, uploaded, or mentioned file/document/code. Otherwise false.

4. "target_file": Specific filename if explicitly mentioned in the text (e.g., "schema.sql", "main.py", "data.csv"), otherwise null.

Respond ONLY with a valid JSON object matching this schema:
{
  "max_words": <int or null>,
  "history": <int or null>,
  "file_use": <bool>,
  "target_file": <string or null>
}"""


def should_skip_intent_extractor(
    raw_text: str,
    is_file_cmd: bool = False,
    has_slash_max_words: bool = False,
    has_slash_history: bool = False,
    has_thread_files: bool = False,
) -> bool:
    """
    Evaluates whether the LLM intent extractor should be skipped:
    1. Always skip if /stop command is present.
    2. Never skip just because /file command is present alone.
    3. Do not skip if there is /history or /max_words AND a file is attached to the thread (without /file),
       to allow detecting natural language file reading requests.
    4. Skip if /file is called explicitly AND either /max_words or /history (or both) is also called explicitly.
    5. Skip if either /max_words or /history are called explicitly and either no thread files exist or /file is specified.
    """
    # Rule 1: Always skip on stop
    if is_stop_command(raw_text):
        return True

    # Rule 4: If /file is called explicitly AND (has_slash_max_words or has_slash_history) -> skip
    if is_file_cmd and (has_slash_max_words or has_slash_history):
        return True

    # If both max_words and history are explicitly provided and no thread files exist to check
    if (has_slash_max_words or has_slash_history) and not has_thread_files:
        return True

    # Otherwise, run extractor
    return False


async def extract_user_intent(
    text: str,
    available_files: list[dict[str, Any]] | None = None,
    timeout_seconds: float = 3.0,
) -> dict[str, Any]:
    """
    Calls mistral-small-latest to extract structured constraints (max_words, history, file_use, target_file)
    in a language-agnostic manner.
    Returns:
    {
        "max_words": int | None,
        "history": int | None,
        "file_use": bool,
        "target_file": str | None
    }
    """
    default_result: dict[str, Any] = {
        "max_words": None,
        "history": None,
        "file_use": False,
        "target_file": None,
    }

    if not text or not text.strip():
        return default_result

    # Provide context of available file names if present to aid matching
    file_hint = ""
    if available_files:
        filenames = [f.get("name") for f in available_files if f.get("name")]
        if filenames:
            file_hint = (
                f"\n(Files currently available in thread: {', '.join(filenames)})"
            )

    user_content = f"User message: {text}{file_hint}"

    messages = [
        {"role": "system", "content": EXTRACTOR_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    try:

        def _call_llm():
            return litellm.completion(
                model=INTENT_EXTRACTOR_MODEL,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.0,
                num_retries=1,
            )

        resp = await asyncio.wait_for(
            run_in_threadpool(_call_llm),
            timeout=timeout_seconds,
        )

        content = resp.choices[0].message.content
        if not content:
            return default_result

        parsed = json.loads(content)

        # Normalize and validate max_words
        raw_max = parsed.get("max_words")
        max_words = (
            int(raw_max)
            if raw_max is not None and str(raw_max).isdigit() and int(raw_max) > 0
            else None
        )

        # Normalize and validate history
        raw_hist = parsed.get("history")
        history = (
            int(raw_hist)
            if raw_hist is not None and str(raw_hist).isdigit() and int(raw_hist) >= 0
            else None
        )

        # Normalize file_use
        file_use = bool(parsed.get("file_use", False))

        # Normalize target_file
        target_file = parsed.get("target_file")
        if isinstance(target_file, str):
            target_file = target_file.strip() or None
        else:
            target_file = None

        logger.info(
            f"[INTENT EXTRACTOR] Extracted: max_words={max_words}, history={history}, file_use={file_use}, target_file={target_file}"
        )

        return {
            "max_words": max_words,
            "history": history,
            "file_use": file_use,
            "target_file": target_file,
        }

    except asyncio.TimeoutError:
        logger.warning(
            f"[INTENT EXTRACTOR] Extraction timed out after {timeout_seconds}s. Falling back."
        )
        return default_result
    except Exception as e:
        logger.warning(f"[INTENT EXTRACTOR] Extraction failed ({e}). Falling back.")
        return default_result
