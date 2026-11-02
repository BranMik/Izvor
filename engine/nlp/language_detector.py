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

import litellm


from core.config import get_language_detector_model
from prompts.builder import load_system_prompt

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

_DEFAULT_SYSTEM_PROMPT_FALLBACK: str = """<system_agent_specification id="Language_Detector" role="Natural Language Classifier">
  <task_objective>
    Classify the primary natural human language of the user's message and output its canonical English language name as a single word.
  </task_objective>

  <processing_rules>
    1. Identify the surrounding natural language of the message (e.g., Croatian, Bosnian, Serbian, English, German, Spanish, French, Italian, Portuguese).
    2. Technical Term Disambiguation: If the message is predominantly written in a non-English language but contains embedded English technical terms, code identifiers, library/application names, or industry buzzwords, classify the surrounding natural human language (e.g., Croatian), NOT English.
    3. Default to English ONLY when the natural prose itself is written in English, or when the input consists purely of code/data with no natural human prose.
  </processing_rules>

  <output_contract>
    Output ONLY the canonical language name in English as a single word (e.g., Croatian, English, German). Do NOT output punctuation, explanations, or markdown.
  </output_contract>
</system_agent_specification>"""

SYSTEM_PROMPT: Final[str] = load_system_prompt("Language_Detector", fallback=_DEFAULT_SYSTEM_PROMPT_FALLBACK)


def normalize_language_name(name: str) -> str:
    """Normalizes any detected language name into canonical English capitalized title."""
    clean = name.strip().lower().strip("'\"`.:;!?()[]{}*~_")
    if clean in LANGUAGE_NAME_MAP:
        return LANGUAGE_NAME_MAP[clean]
    return clean.capitalize()


def _extract_language_from_content(content: str | None, default: str = "English") -> str:
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
    meaningful_words = [w for w in re.findall(r"\b[^\W\d_]+\b", stripped, flags=re.UNICODE) if len(w) > 1]
    return len(meaningful_words) == 0


def is_system_message(text: str) -> bool:
    """Returns True if the text represents an internal system warning, test banner, or notification."""
    if not text or not text.strip():
        return False
    stripped = text.strip()
    if any(stripped.startswith(prefix) for prefix in ("⚠️", "⚙️", "🔒", "🗑️", "🧪", "⏱️", "🚨")):
        return True
    lower = stripped.lower()
    return bool(
        any(
            marker in lower
            for marker in (
                "ambiguous agent reference",
                "thread constraints updated",
                "revoked all thread constraints",
                "expense circuit breaker tripped",
                "no agents enabled",
                "automated behavioural test scenario",
                "[panel system]",
                "panel system:",
                "e2e test runner",
            )
        )
    )


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
    cleaned = re.sub(r"👤\s*\*?Follow-up Prompt:\*?\s*\n?>?", " ", cleaned, flags=re.IGNORECASE)
    # Strip system warning header lines
    cleaned = re.sub(r"⚠️\s*\*?[^\n]*\n?", " ", cleaned)
    cleaned = re.sub(r"⚙️\s*\*?[^\n]*\n?", " ", cleaned)
    cleaned = re.sub(r"🗑️\s*\*?[^\n]*\n?", " ", cleaned)
    # Strip blockquote markers
    cleaned = re.sub(r"^>\s*\|?\s*", "", cleaned, flags=re.MULTILINE)
    # Strip agent bracket tags: /[LegalAdvisor], [LegalAdvisor], /[Visionary], etc.
    cleaned = re.sub(r"/?\[[a-zA-Z0-9_ -]+\]", " ", cleaned)
    return cleaned.strip()


def _heuristic_detect(text: str, default: str = "English") -> str:
    """Fast deterministic heuristic classification for synchronous callers without LLM."""
    if "¿" in text or "¡" in text:
        return "Spanish"
    lower = text.lower()
    # Distinctive South Slavic characters
    if any(ch in lower for ch in "čćžšđ"):
        return "Croatian"
    words = set(re.findall(r"\b[^\W\d_]+\b", lower, flags=re.UNICODE))
    croatian_words = {
        "je",
        "da",
        "ne",
        "se",
        "za",
        "na",
        "u",
        "koji",
        "koja",
        "koje",
        "kako",
        "što",
        "sto",
        "nije",
        "sam",
        "smo",
        "ste",
        "su",
        "ali",
        "ili",
        "može",
        "moze",
        "kratki",
        "rijeci",
        "rijec",
        "ovaj",
        "ova",
        "ovo",
        "sustav",
        "funkcionira",
        "vise",
        "više",
        "puno",
        "upit",
    }
    if len(words & croatian_words) >= 2:
        return "Croatian"
    german_words = {"und", "der", "die", "das", "ist", "nicht", "für", "mit", "eine", "einer", "wie"}
    if len(words & german_words) >= 2:
        return "German"
    spanish_words = {
        "el",
        "la",
        "los",
        "las",
        "por",
        "para",
        "como",
        "cómo",
        "está",
        "esta",
        "pero",
        "de",
        "y",
        "en",
        "esto",
        "que",
        "es",
        "una",
        "un",
    }
    if len(words & spanish_words) >= 2:
        return "Spanish"
    french_words = {"le", "la", "les", "des", "pour", "dans", "avec", "est", "sont", "comment", "ce", "cette"}
    if len(words & french_words) >= 2:
        return "French"
    italian_words = {"il", "lo", "la", "gli", "per", "con", "sono", "anche", "come", "questo"}
    if len(words & italian_words) >= 2:
        return "Italian"
    return default


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
    clean_text = clean_system_and_tags(text)
    if _is_empty_or_pure_code(clean_text):
        return default

    selected_model = model or get_language_detector_model()
    logger.info(
        f"[LANGUAGE DETECTOR] Starting LLM language detection ({len(clean_text)} chars, model={selected_model})"
    )

    try:
        from core.llm_utils import execute_llm_with_retry

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": clean_text},
        ]

        resp = await asyncio.wait_for(
            execute_llm_with_retry(
                litellm.acompletion,
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
                        model_name=selected_model,
                        prompt_tokens=p_tok,
                        completion_tokens=c_tok,
                    )
            except Exception as ue:  # noqa: BLE001
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
        logger.warning(f"[LANGUAGE DETECTOR LLM] LLM classification failed ({e}). Defaulting to {default}.")
        return default


def detect_language(text: str, default: str = "English") -> str:
    """
    Synchronous entry point for language detection using fast heuristic classification.
    """
    try:
        if is_system_message(text):
            return default
        clean_text = clean_system_and_tags(text)
        if _is_empty_or_pure_code(clean_text):
            return default
        return _heuristic_detect(clean_text, default=default)
    except Exception as e:  # noqa: BLE001 - Language detection error boundary falls back to default language
        logger.warning(f"[LANGUAGE DETECTOR] Sync classification failed ({e}). Defaulting to {default}.")
        return default
