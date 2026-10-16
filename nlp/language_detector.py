"""
Natural language detection for Izvor via LLM.

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
    "bosnian": "Bosnian",
    "bosanski": "Bosnian",
    "serbian": "Serbian",
    "srpski": "Serbian",
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
}

SYSTEM_PROMPT: Final[str] = (
    "You are an ultra-fast, accurate language classifier. "
    "Identify the primary natural language of the user's message. "
    "Output ONLY the canonical language name in English as a single word (e.g. Croatian, Bosnian, Serbian, English, German, Spanish, French, Italian). "
    "Do NOT output any punctuation, reasoning, or markdown. "
    "If the message is predominantly non-English but contains English technical terms, library/app names, or buzzwords, identify the surrounding natural language (e.g. Croatian), NOT English. "
    "Only output English if the natural text itself is in English, or if it is purely code/data with no human language."
)


def normalize_language_name(name: str) -> str:
    """Normalizes any detected language name into canonical English capitalized title."""
    clean = name.strip().lower().strip("'\"`.:;!?()[]{}*~_")
    if clean in LANGUAGE_NAME_MAP:
        return LANGUAGE_NAME_MAP[clean]
    return clean.capitalize()


def _extract_language_from_content(
    content: str | None, default: str = "English"
) -> str:
    """Robustly parses language name from model response text or reasoning outputs."""
    if not content or not content.strip():
        return default
    cleaned = content.strip()
    # Check first word
    words = cleaned.split("\n")[0].split()
    if words:
        first_word = re.sub(r"[^\w]", "", words[0]).lower()
        if first_word in LANGUAGE_NAME_MAP:
            return LANGUAGE_NAME_MAP[first_word]
    # Scan words in content for any known language keyword
    tokens = re.findall(r"[^\W\d_]+", cleaned.lower(), flags=re.UNICODE)
    for token in tokens:
        if token in LANGUAGE_NAME_MAP:
            return LANGUAGE_NAME_MAP[token]
    # Normalization fallback
    if words:
        norm = normalize_language_name(words[0])
        if norm and len(norm) > 2:
            return norm
    return default


def _is_empty_or_pure_code(text: str) -> bool:
    """Deterministic pre-check: returns True if text lacks any natural language words (>1 letter)."""
    if not text or not text.strip():
        return True
    stripped = re.sub(r"```[\s\S]*?```", "", text)
    stripped = re.sub(r"[{}\[\]();=+\-*/\\<>_~`$#@%^&|0-9]", " ", stripped)
    meaningful_words = [
        w
        for w in re.findall(r"\b[^\W\d_]+\b", stripped, flags=re.UNICODE)
        if len(w) > 1
    ]
    return len(meaningful_words) == 0


def is_system_message(text: str) -> bool:
    """Returns True if the text represents an internal system warning, test banner, or notification."""
    if not text or not text.strip():
        return False
    stripped = text.strip()
    if any(stripped.startswith(prefix) for prefix in ("⚠️", "⚙️", "🔒", "🗑️", "🧪", "⏱️", "🚨")):
        return True
    lower = stripped.lower()
    return bool(any(marker in lower for marker in ("ambiguous agent reference", "thread constraints updated", "revoked all thread constraints", "expense circuit breaker tripped", "no agents enabled", "automated behavioural test scenario", "[panel system]", "panel system:", "e2e test runner")))


def clean_system_and_tags(text: str) -> str:
    """
    Strips Slack user/bot mentions, bracketed agent tags, runner banners,
    and system warning prefixes so language detection evaluates only genuine user text.
    """
    if not text:
        return ""
    cleaned = text
    # Strip user/bot mentions: <@U12345>
    cleaned = re.sub(r"<@[A-Z0-9]+>", " ", cleaned)
    # Strip runner banners and follow-up wrappers: 🧪 *...* or 👤 *Follow-up Prompt:*\n>
    cleaned = re.sub(r"🧪\s*\*?[^\n]*\n(?:>\s*\|?\s*)?", " ", cleaned)
    cleaned = re.sub(
        r"👤\s*\*?Follow-up Prompt:\*?\s*\n?>?", " ", cleaned, flags=re.IGNORECASE
    )
    # Strip system warning header lines
    cleaned = re.sub(r"⚠️\s*\*?[^\n]*\n?", " ", cleaned)
    cleaned = re.sub(r"⚙️\s*\*?[^\n]*\n?", " ", cleaned)
    cleaned = re.sub(r"🗑️\s*\*?[^\n]*\n?", " ", cleaned)
    # Strip blockquote markers
    cleaned = re.sub(r"^>\s*\|?\s*", "", cleaned, flags=re.MULTILINE)
    # Strip agent bracket tags: /[LegalAdvisor], [LegalAdvisor], /[Visionary], etc.
    cleaned = re.sub(r"/?\[[a-zA-Z0-9_ -]+\]", " ", cleaned)
    return cleaned.strip()


def _clean_slack_tags(text: str) -> str:
    """Strips Slack user/bot mentions, bracketed agent tags, and system wrappers so they don't pollute classification."""
    return clean_system_and_tags(text)


def _sync_llm_classify(
    text: str, model: str | None = None, default: str = "English"
) -> str:
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
        num_retries=1,
    )
    msg = resp.choices[0].message
    content = getattr(msg, "content", None)
    if not content:
        reasoning = getattr(msg, "reasoning_content", None)
        if reasoning:
            extracted = _extract_language_from_content(reasoning, default="")
            if extracted:
                return extracted
        finish_reason = getattr(resp.choices[0], "finish_reason", "unknown")
        logger.warning(
            f"[LANGUAGE DETECTOR] Model {selected_model} returned empty content (finish_reason={finish_reason}). Defaulting to {default}."
        )
        return default
    return _extract_language_from_content(content, default=default)


async def detect_language_llm(
    text: str,
    model: str | None = None,
    timeout_seconds: float = 6.0,
    default: str = "English",
    thread_key: tuple[str, str] | None = None,
) -> str:
    """
    Asynchronously classifies the natural language of input text using an LLM.
    Runs once on the first prompt of a thread.
    Falls back cleanly to `default` ('English') on any error or timeout.
    """
    if is_system_message(text):
        return default
    clean_text = _clean_slack_tags(text)
    if _is_empty_or_pure_code(clean_text):
        return default

    selected_model = model or get_language_detector_model()
    logger.info(
        f"[LANGUAGE DETECTOR] Starting LLM language detection ({len(clean_text)} chars, model={selected_model})"
    )

    try:
        from core.llm_utils import execute_llm_with_retry
        import litellm

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": clean_text},
        ]

        resp = await asyncio.wait_for(
            execute_llm_with_retry(
                litellm.completion,
                kwargs={"model": selected_model, "messages": messages, "num_retries": 1},
                agent_key="language_detector",
                thread_key=thread_key or ("system", "language_detector"),
                max_attempts=2,
            ),
            timeout=timeout_seconds,
        )

        if not resp or not hasattr(resp, "choices") or not resp.choices:
            return default

        if resp and getattr(resp, "usage", None) and thread_key and thread_key != ("system", "language_detector"):
            try:
                from agents.usage_agent import record_thread_tokens
                p_tok = getattr(resp.usage, "prompt_tokens", 0) or 0
                c_tok = getattr(resp.usage, "completion_tokens", 0) or 0
                if p_tok or c_tok:
                    record_thread_tokens(
                        channel_id=thread_key[0],
                        thread_ts=thread_key[1],
                        model=selected_model,
                        prompt_tokens=p_tok,
                        completion_tokens=c_tok,
                    )
            except Exception as ue:
                logger.debug(f"[LANGUAGE DETECTOR] Error recording token spend: {ue}")

        msg = resp.choices[0].message
        content = getattr(msg, "content", None)
        if not content:
            reasoning = getattr(msg, "reasoning_content", None)
            if reasoning:
                extracted = _extract_language_from_content(reasoning, default="")
                if extracted:
                    return extracted
            return default

        detected = _extract_language_from_content(content, default=default)
        logger.info(
            f"[LANGUAGE DETECTOR LLM] Classified text ({len(clean_text)} chars) -> {detected} (via {selected_model})"
        )
        return detected or default
    except Exception as e:  # noqa: BLE001 - Language detection error boundary falls back to default language
        logger.warning(
            f"[LANGUAGE DETECTOR LLM] LLM classification failed ({e}). Defaulting to {default}."
        )
        return default


def detect_language(
    text: str, default: str = "English", model: str | None = None
) -> str:
    """
    Synchronous entry point for language detection via LLM.
    """
    try:
        return _sync_llm_classify(text, model=model, default=default)
    except Exception as e:  # noqa: BLE001 - Language detection error boundary falls back to default language
        logger.warning(
            f"[LANGUAGE DETECTOR] Sync classification failed ({e}). Defaulting to {default}."
        )
        return default
