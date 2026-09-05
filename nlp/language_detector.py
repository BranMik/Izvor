"""
Natural language detection for SlackAgenticPanel via LLM.

Uses an ultra-fast LLM call (Mistral Small by default) to accurately classify
natural language, with clean normalization to canonical English language names.
All heuristic/deterministic Python text scoring has been removed in favor of
pure LLM classification.
"""

import asyncio
import logging
import re
from typing import Final

from core.config import get_language_detector_model

logger = logging.getLogger(__name__)

# Normalization map for canonical English language names
LANGUAGE_NAME_MAP: Final[dict[str, str]] = {
    "croatian": "Croatian",
    "hrvatski": "Croatian",
    "bosnian": "Croatian",
    "bosanski": "Croatian",
    "serbian": "Croatian",
    "srpski": "Croatian",
    "english": "English",
    "engleski": "English",
    "german": "German",
    "deutsch": "German",
    "njemački": "German",
    "njemacki": "German",
    "spanish": "Spanish",
    "español": "Spanish",
    "espanol": "Spanish",
    "španjolski": "Spanish",
    "spanjolski": "Spanish",
    "french": "French",
    "français": "French",
    "francais": "French",
    "francuski": "French",
    "italian": "Italian",
    "italiano": "Italian",
    "talijanski": "Italian",
    "portuguese": "Portuguese",
    "português": "Portuguese",
    "portugalski": "Portuguese",
    "russian": "Russian",
    "ruski": "Russian",
}

SYSTEM_PROMPT: Final[str] = (
    "You are an ultra-fast, accurate language classifier. "
    "Identify the primary natural language of the user's message. "
    "Output ONLY the canonical language name in English as a single word (e.g. Croatian, English, German, Spanish, French, Italian). "
    "Do NOT output any punctuation, reasoning, or markdown. "
    "If the message is programming code, SQL, technical data, shell commands, or ambiguous with no natural language, output English."
)


def normalize_language_name(name: str) -> str:
    """Normalizes any detected language name into canonical English capitalized title."""
    clean = name.strip().lower().strip("'\"`.:;!?()[]{}*~_")
    if clean in LANGUAGE_NAME_MAP:
        return LANGUAGE_NAME_MAP[clean]
    return clean.capitalize()


def _is_empty_or_pure_code(text: str) -> bool:
    """Deterministic pre-check: returns True if text lacks any natural language words (>1 letter)."""
    if not text or not text.strip():
        return True
    stripped = re.sub(r"```[\s\S]*?```", "", text)
    stripped = re.sub(r"[{}\[\]();=+\-*/\\<>_~`$#@%^&|0-9]", " ", stripped)
    meaningful_words = [
        w for w in re.findall(r"\b[^\W\d_]+\b", stripped, flags=re.UNICODE) if len(w) > 1
    ]
    return len(meaningful_words) == 0


def _clean_slack_tags(text: str) -> str:
    """Strips Slack user/bot mentions like <@U0BR4EH6XMY> so they don't pollute classification."""
    cleaned = re.sub(r"<@[A-Z0-9]+>", " ", text).strip()
    return cleaned or text


def _sync_llm_classify(text: str, model: str | None = None, default: str = "English") -> str:
    """Synchronous execution of LLM language classification."""
    clean_text = _clean_slack_tags(text)
    if _is_empty_or_pure_code(clean_text):
        return default

    selected_model = model or get_language_detector_model()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": clean_text},
    ]

    import litellm

    resp = litellm.completion(
        model=selected_model,
        messages=messages,
        max_tokens=8,
        num_retries=1,
    )
    content = resp.choices[0].message.content
    if content:
        raw_lang = content.strip().split("\n")[0].split()[0]
        return normalize_language_name(raw_lang) or default
    return default


async def detect_language_llm(
    text: str,
    model: str | None = None,
    timeout_seconds: float = 3.0,
    default: str = "English",
) -> str:
    """
    Asynchronously classifies the natural language of input text using an LLM.
    Runs once on the first prompt of a thread.
    Falls back cleanly to `default` ('English') on any error or timeout.
    """
    clean_text = _clean_slack_tags(text)
    if _is_empty_or_pure_code(clean_text):
        return default

    selected_model = model or get_language_detector_model()
    logger.info(
        f"[LANGUAGE DETECTOR] Starting LLM language detection for text: '{clean_text[:80]}' "
        f"(model={selected_model})"
    )

    try:
        from starlette.concurrency import run_in_threadpool

        def _call():
            return _sync_llm_classify(clean_text, model=selected_model, default=default)

        detected = await asyncio.wait_for(
            run_in_threadpool(_call),
            timeout=timeout_seconds,
        )
        logger.info(
            f"[LANGUAGE DETECTOR LLM] Classified text '{clean_text[:60]}' -> {detected} (via {selected_model})"
        )
        return detected or default
    except Exception as e:
        logger.warning(
            f"[LANGUAGE DETECTOR LLM] LLM classification failed ({e}). Defaulting to {default}."
        )
        return default


def detect_language(text: str, default: str = "English", model: str | None = None) -> str:
    """
    Synchronous entry point for language detection via LLM.
    """
    try:
        return _sync_llm_classify(text, model=model, default=default)
    except Exception as e:
        logger.warning(f"[LANGUAGE DETECTOR] Sync classification failed ({e}). Defaulting to {default}.")
        return default
