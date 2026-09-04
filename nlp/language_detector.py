"""
Zero-dependency, fast natural language detection for SlackAgenticPanel.

Uses stop-word frequency scoring across common European languages:
- English (default)
- Croatian / Bosnian / Serbian (with high diacritics weighting)
- German
- Spanish
- French
- Italian
"""

import asyncio
import logging
import re
from typing import Final

logger = logging.getLogger(__name__)

# Tokenizer: extracts lowercase words consisting of unicode word characters
WORD_PATTERN: Final[re.Pattern] = re.compile(r"\b[^\W\d_]+\b", re.UNICODE)

# Distinctive diacritics heavily signaling Slavic/Croatian Latin
CROATIAN_CHARS: Final[set[str]] = {"č", "ć", "ž", "š", "đ"}
GERMAN_CHARS: Final[set[str]] = {"ä", "ö", "ü", "ß"}
FRENCH_CHARS: Final[set[str]] = {"é", "è", "ê", "ë", "à", "â", "ô", "û", "ù", "ç", "œ"}
SPANISH_CHARS: Final[set[str]] = {"ñ", "¿", "¡", "á", "é", "í", "ó", "ú"}

# High-frequency stop-words per language (all lowercase)
STOPWORDS_CROATIAN: Final[set[str]] = {
    "i", "u", "je", "da", "se", "na", "za", "su", "ne", "o", "od", "sa", "pa", "to",
    "kako", "što", "sto", "kao", "ali", "ili", "smo", "sam", "može", "moze", "treba",
    "sve", "kod", "kad", "kada", "prije", "nakon", "zbog", "ovaj", "ova", "ovo", "koji",
    "koja", "koje", "tako", "toga", "više", "vise", "nam", "vam", "im", "ih", "ga",
    "ju", "joj", "mu", "biti", "jesu", "bili", "bila", "bilo", "samo", "tko", "gdje",
    "zašto", "zasto", "jesam", "nismo", "niste", "nisu", "nadam", "molim", "hvala",
    "daj", "napravi", "prikaz", "pošalji", "poslati", "provjeri", "reci", "pitanje",
}

STOPWORDS_ENGLISH: Final[set[str]] = {
    "the", "be", "to", "of", "and", "a", "in", "that", "have", "i", "it", "for", "not",
    "on", "with", "he", "as", "you", "do", "at", "this", "but", "his", "by", "from",
    "they", "we", "say", "her", "she", "or", "an", "will", "my", "one", "all", "would",
    "there", "their", "what", "so", "up", "out", "if", "about", "who", "get", "which",
    "go", "me", "when", "make", "can", "like", "time", "no", "just", "him", "know",
    "take", "into", "your", "good", "some", "could", "them", "see", "other", "than",
    "then", "now", "look", "only", "its", "over", "think", "also", "after", "use",
    "how", "our", "work", "first", "well", "way", "even", "new", "want", "because",
    "any", "these", "give", "most", "us", "is", "are", "was", "were", "been", "has", "had",
    "please", "check", "tell", "explain", "why", "should", "does", "did",
}

STOPWORDS_GERMAN: Final[set[str]] = {
    "der", "die", "das", "und", "in", "den", "von", "zu", "mit", "sich", "des", "auf",
    "für", "fuer", "ist", "im", "dem", "nicht", "ein", "eine", "als", "auch", "es",
    "an", "werden", "aus", "er", "hat", "dass", "sie", "nach", "wird", "bei", "einer",
    "um", "am", "sind", "noch", "wie", "einem", "über", "ueber", "einen", "so", "war",
    "haben", "nur", "oder", "aber", "vor", "zur", "bis", "mehr", "durch", "man", "sein",
    "wurde", "bitte", "warum", "kann", "welche", "welcher",
}

STOPWORDS_SPANISH: Final[set[str]] = {
    "el", "la", "de", "que", "y", "a", "en", "un", "ser", "se", "no", "haber", "por",
    "con", "su", "para", "como", "estar", "tener", "le", "lo", "todo", "pero", "más",
    "mas", "hacer", "o", "poder", "este", "ya", "otro", "ese", "ella", "yo", "nosotros",
    "ellos", "una", "unas", "unos", "porqué", "porque", "cómo", "como", "dónde", "donde",
    "por", "favor",
}

STOPWORDS_FRENCH: Final[set[str]] = {
    "le", "la", "les", "de", "des", "du", "un", "une", "et", "à", "a", "en", "est",
    "que", "qui", "dans", "pour", "pas", "sur", "ce", "il", "ils", "elle", "elles",
    "ne", "plus", "pouvoir", "avec", "tout", "faire", "son", "sa", "ses", "par",
    "comme", "mais", "nous", "vous", "leur", "ou", "si", "s'il", "merci",
}

STOPWORDS_ITALIAN: Final[set[str]] = {
    "il", "lo", "la", "i", "gli", "le", "di", "da", "in", "con", "su", "per", "tra",
    "fra", "un", "uno", "una", "e", "ed", "è", "che", "chi", "non", "si", "sono",
    "come", "ma", "più", "piu", "cosa", "questo", "questa", "questi", "queste", "anche",
}

# Morphological suffixes characteristic of Croatian nouns/adjectives
CROATIAN_SUFFIXES: Final[tuple[str, ...]] = (
    "ura", "uru", "urom", "urama",
    "cija", "ciju", "cije", "cijom", "cijama",
    "nje", "nja", "njem", "njima",
    "ost", "osti", "ostima",
)

# Common stems for technical & inquiry words in Croatian
CROATIAN_PROMPT_STEMS: Final[tuple[str, ...]] = (
    "arhitektur", "sustav", "rjesenj", "rješenj", "optimizac", "problem",
    "aplikac", "sigurnost", "skalabilnost", "performans", "baza", "baze", "bazu",
    "bazi", "uspored", "objasn", "provjer", "prikaz", "pokaz", "pokaž",
    "opis", "opiš", "napis", "napiš", "naprav", "stvor", "predloz", "predlož",
    "gresk", "grešk", "podat", "podac", "korisnik", "servis", "modul", "konfigurac",
    "pregled", "sažet", "sazet", "preporuk", "zahtjev", "prijedlog", "koda", "kodu",
)

# Common stems for technical words in English to maintain balanced scoring
ENGLISH_PROMPT_STEMS: Final[tuple[str, ...]] = (
    "architectur", "system", "design", "tradeoff", "benchmark", "database",
    "optimiz", "perform", "secur", "compar", "explain", "analyz", "overview",
    "review", "featur", "solut", "implem", "configur", "dependenc",
)


def detect_language(text: str, default: str = "English") -> str:
    """
    Detects the natural language of input text using stop-word scoring, morphological suffixes,
    stems, and character distributions.
    Returns canonical language name in English: 'English', 'Croatian', 'German', 'Spanish', 'French', 'Italian'.
    Defaults to 'English' if input is empty, ambiguous, or lacks clear language signals.
    """
    if not text or not text.strip():
        return default

    clean_text = text.lower()
    words = WORD_PATTERN.findall(clean_text)
    if not words:
        return default

    scores: dict[str, float] = {
        "English": 0.0,
        "Croatian": 0.0,
        "German": 0.0,
        "Spanish": 0.0,
        "French": 0.0,
        "Italian": 0.0,
    }

    # 1. Check diacritics
    for char in clean_text:
        if char in CROATIAN_CHARS:
            scores["Croatian"] += 2.0
        elif char in GERMAN_CHARS:
            scores["German"] += 2.0
        elif char in FRENCH_CHARS:
            scores["French"] += 2.0
        elif char in SPANISH_CHARS:
            scores["Spanish"] += 2.0

    # 2. Score stop words, stems, and morphological suffixes
    for word in words:
        # Croatian scoring
        if word in STOPWORDS_CROATIAN:
            scores["Croatian"] += 1.5
        elif any(word.startswith(stem) for stem in CROATIAN_PROMPT_STEMS):
            scores["Croatian"] += 1.5
        elif len(word) > 4 and any(word.endswith(suffix) for suffix in CROATIAN_SUFFIXES):
            scores["Croatian"] += 1.5

        # English scoring
        if word in STOPWORDS_ENGLISH:
            scores["English"] += 1.0
        elif any(word.startswith(stem) for stem in ENGLISH_PROMPT_STEMS):
            scores["English"] += 1.2

        if word in STOPWORDS_GERMAN:
            scores["German"] += 1.5
        if word in STOPWORDS_SPANISH:
            scores["Spanish"] += 1.5
        if word in STOPWORDS_FRENCH:
            scores["French"] += 1.5
        if word in STOPWORDS_ITALIAN:
            scores["Italian"] += 1.5

    best_lang, best_score = max(scores.items(), key=lambda item: item[1])

    # If the score is too low (< 1.5), default to English
    if best_score < 1.5:
        return default

    # If English and another language are close, verify whether candidate has higher score
    if best_lang != "English" and scores["English"] > 0:
        if best_score <= scores["English"]:
            return "English"

    return best_lang


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


def normalize_language_name(name: str) -> str:
    """Normalizes any detected language name into canonical English capitalized title."""
    clean = name.strip().lower().strip("'\"`.:;!?()[]{}")
    if clean in LANGUAGE_NAME_MAP:
        return LANGUAGE_NAME_MAP[clean]
    return clean.capitalize()


async def detect_language_llm(
    text: str,
    model: str | None = None,
    timeout_seconds: float = 3.0,
    default: str = "English",
) -> str:
    """
    Asynchronously classifies the natural language of input text using an LLM agent.
    Runs strictly once on the first prompt of a thread.
    Falls back cleanly to local heuristic detect_language() on any error, timeout, or missing key.
    """
    if not text or not text.strip():
        return default

    # If there are no natural language words (> 1 letter), default to English immediately
    stripped = re.sub(r"```[\s\S]*?```", "", text)
    stripped = re.sub(r"[{}\[\]();=+\-*/\\<>_~`$#@%^&|0-9]", " ", stripped)
    meaningful_words = [
        w for w in re.findall(r"\b[^\W\d_]+\b", stripped, flags=re.UNICODE) if len(w) > 1
    ]
    if not meaningful_words:
        return default

    # If the text has high-confidence Croatian diacritics, fast-path immediately
    clean_text = text.lower()
    has_croatian_chars = any(c in CROATIAN_CHARS for c in clean_text)
    if has_croatian_chars and len(clean_text) > 8:
        heuristic = detect_language(text, default=default)
        if heuristic == "Croatian":
            return "Croatian"

    try:
        import litellm
        from starlette.concurrency import run_in_threadpool
        from core.config import get_language_detector_model

        selected_model = model or get_language_detector_model()

        system_prompt = (
            "You are an ultra-fast, accurate language classifier. "
            "Identify the primary natural language of the user's message. "
            "If the message is programming code, SQL, commands, formulas, or ambiguous, you MUST output English. "
            "Output ONLY the canonical language name in English as a single word without any punctuation or explanations."
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ]

        def _call():
            return litellm.completion(
                model=selected_model,
                messages=messages,
                temperature=0.0,
                max_tokens=8,
                num_retries=1,
            )

        resp = await asyncio.wait_for(
            run_in_threadpool(_call),
            timeout=timeout_seconds,
        )

        content = resp.choices[0].message.content
        if content:
            raw_lang = content.strip().split("\n")[0].split()[0]
            normalized = normalize_language_name(raw_lang)
            if normalized:
                logger.info(
                    f"[LANGUAGE DETECTOR LLM] Classified text '{text[:40]}' -> {normalized} (via {selected_model})"
                )
                return normalized
    except Exception as e:
        logger.warning(
            f"[LANGUAGE DETECTOR LLM] LLM classification failed ({e}). Falling back to heuristic detector."
        )

    # Clean fallback to heuristic detector
    return detect_language(text, default=default)
