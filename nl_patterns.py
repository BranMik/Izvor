"""
Natural language recognition patterns and command parsers for the Slack Agentic Panel.

This module extracts user directives from Slack messages (both slash command syntax and
natural language phrasing in Croatian and English) for:
- Conversation history limits (/history)
- Word limits (/max_words, /min_words)
- Attachment file instructions (/read-file, /file, natural language file requests)
- Discussion stop commands (/stop)
"""

import re
from typing import Any

# ==============================================================================
# 1. Conversation History Patterns
# ==============================================================================

HISTORY_COMMAND_PATTERN = re.compile(
    r"/history\s*[:=]?\s*\[?(\d+)(?::(?:int|integer))?\]?",
    re.IGNORECASE,
)

HISTORY_NL_PATTERNS = [
    re.compile(
        r"\b(?:uzmi\s+u\s+obzir\s+|pogledaj\s+|samo\s+|take\s+into\s+account\s+|only\s+)?(?:zadnj[a-z]*|posljednj[a-z]*|prethodn[a-z]*|last|past|previous)\s+(\d+)\s*(?:poruk[a-z]*|messages?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:povijest|history)\s*(?:od)?\s*[:=]?\s*(\d+)\s*(?:poruk[a-z]*|messages?)\b",
        re.IGNORECASE,
    ),
]


def extract_history_limit(text: str) -> tuple[int | None, str]:
    """
    Extracts the history limit parameter if present in text and returns (history_limit, cleaned_text).

    Supports:
    - Slash command: /history 5, /history: 5, /history=5, /history [5:int], /history (5)
    - Natural language: "uzmi u obzir zadnjih 5 poruka", "last 3 messages", "povijest od 4 poruke"
    """
    if not text:
        return None, ""
    match = HISTORY_COMMAND_PATTERN.search(text)
    if match:
        limit = int(match.group(1))
        cleaned = HISTORY_COMMAND_PATTERN.sub("", text).strip()
        cleaned = re.sub(r"[ \t]+", " ", cleaned).strip()
        return limit, cleaned
    for pattern in HISTORY_NL_PATTERNS:
        match = pattern.search(text)
        if match:
            limit = int(match.group(1))
            cleaned = pattern.sub("", text).strip()
            cleaned = re.sub(r"[ \t]+", " ", cleaned).strip()
            return limit, cleaned
    return None, text


# ==============================================================================
# 2. Maximum Words (max_words) Patterns
# ==============================================================================

MAX_WORDS_COMMAND_PATTERN = re.compile(
    r"/(?:max_words|max-words|maxwords)\s*[:=]?\s*\[?(\d+)(?::(?:int|integer))?\]?",
    re.IGNORECASE,
)

MAX_WORDS_NL_PATTERNS = [
    re.compile(
        r"\b(?:u\s+|in\s+)?(?:maksimalno|maximalno|najvi[sš]e|najvi[sš]e\s+do|max\.?|maximum|at\s+most|no\s+more\s+than|up\s+to)\s+(?:od\s+)?(\d+)\s*(?:rije[cč]i|words)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:maksimaln[a-z]*\s+duljin[a-z]*|maksimaln[a-z]*\s+du[zž]in[a-z]*|maximaln[a-z]*\s+duljin[a-z]*|maximaln[a-z]*\s+du[zž]in[a-z]*|maximum\s+length|word\s+limit|limit\s+od|limit\s+to)\s*[:=]?\s*(\d+)\s*(?:rije[cč]i|words)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:odgovori|pi[sš]i|odgovor)\s+(?:u\s+|in\s+)?(?:do|ispod|under)\s+(\d+)\s*(?:rije[cč]i|words)\b",
        re.IGNORECASE,
    ),
]


def extract_max_words_limit(text: str) -> tuple[int | None, str]:
    """
    Extracts the max_words limit parameter if present in text and returns (max_words_limit, cleaned_text).

    Supports:
    - Slash command: /max_words 50, /max_words: 50, /max_words=50, /max_words [50:int]
    - Natural language: "odgovori u maksimalno 100 riječi", "najviše 50 riječi", "in max 120 words", "up to 80 words"
    """
    if not text:
        return None, ""
    match = MAX_WORDS_COMMAND_PATTERN.search(text)
    if match:
        limit = max(1, int(match.group(1)))
        cleaned = MAX_WORDS_COMMAND_PATTERN.sub("", text).strip()
        cleaned = re.sub(r"[ \t]+", " ", cleaned).strip()
        return limit, cleaned
    for pattern in MAX_WORDS_NL_PATTERNS:
        match = pattern.search(text)
        if match:
            limit = max(1, int(match.group(1)))
            cleaned = pattern.sub("", text).strip()
            cleaned = re.sub(r"[ \t]+", " ", cleaned).strip()
            return limit, cleaned
    return None, text


# ==============================================================================
# 3. Minimum Words (min_words) Patterns
# ==============================================================================

MIN_WORDS_COMMAND_PATTERN = re.compile(
    r"/(?:min_words|min-words|minwords)\s*[:=]?\s*\[?(\d+)(?::(?:int|integer))?\]?",
    re.IGNORECASE,
)

MIN_WORDS_NL_PATTERNS = [
    re.compile(
        r"\b(?:u\s+|in\s+)?(?:minimalno|najmanje|barem|bar|min\.?|minimum|at\s+least|no\s+less\s+than)\s+(?:od\s+)?(\d+)\s*(?:rije[cč]i|words)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:minimaln[a-z]*\s+duljin[a-z]*|minimaln[a-z]*\s+du[zž]in[a-z]*|minimum\s+length|donj[a-z]*\s+granic[a-z]*)\s*[:=]?\s*(\d+)\s*(?:rije[cč]i|words)\b",
        re.IGNORECASE,
    ),
]


def extract_min_words_limit(text: str) -> tuple[int | None, str]:
    """
    Extracts the min_words limit parameter if present in text and returns (min_words_limit, cleaned_text).

    Supports:
    - Slash command: /min_words 50, /min_words: 50, /min_words=50, /min_words [50:int]
    - Natural language: "odgovori u minimalno 100 riječi", "najmanje 80 riječi", "barem 150 riječi", "at least 100 words"
    """
    if not text:
        return None, ""
    match = MIN_WORDS_COMMAND_PATTERN.search(text)
    if match:
        limit = max(1, int(match.group(1)))
        cleaned = MIN_WORDS_COMMAND_PATTERN.sub("", text).strip()
        cleaned = re.sub(r"[ \t]+", " ", cleaned).strip()
        return limit, cleaned
    for pattern in MIN_WORDS_NL_PATTERNS:
        match = pattern.search(text)
        if match:
            limit = max(1, int(match.group(1)))
            cleaned = pattern.sub("", text).strip()
            cleaned = re.sub(r"[ \t]+", " ", cleaned).strip()
            return limit, cleaned
    return None, text


# ==============================================================================
# 4. Attachment / File Instruction Patterns
# ==============================================================================

FILE_COMMAND_PATTERN = re.compile(
    r"/(?:read[-_]?file|file|attachment)(?:\s*[:=]\s*(\S+)|\s+\[([^\]]+)\]|\s+\(([^)]+)\)|\s+([\w\-.]+\.[a-zA-Z0-9]+))?",
    re.IGNORECASE,
)

FILE_NL_PATTERNS = [
    # Croatian triggers
    re.compile(
        r"\b(?:pro[čc]itaj|pogledaj|analiziraj|provjeri|u[čc]itaj|pregledaj|obradi|otvori)\b.*?\b(?:fajl[a-z]*|datotek[a-z]*|privit[a-z]*|attachment[a-z]*|prilo[zž]en[a-z]*|kod\s+u\s+privitku|dokument[a-z]*)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:u\s+privitku|u\s+fajlu|u\s+datoteci|iz\s+fajla|iz\s+datoteke|iz\s+privitka|(?:u\s+)?prilo[zž]en[a-z]*\s+(?:fajl[a-z]*|datotek[a-z]*|privit[a-z]*|kod[a-z]*|dokument[a-z]*)|uploadao\s+sam\s+fajl|poslao\s+sam\s+fajl|uploadan[a-z]*\s+fajl[a-z]*)\b",
        re.IGNORECASE,
    ),
    # English triggers
    re.compile(
        r"\b(?:read|check|analyze|analyse|inspect|review|examine|look\s+at|open)\b.*?\b(?:file[a-z]*|attachment[a-z]*|document[a-z]*|uploaded\s+file[a-z]*|code\s+in\s+attachment)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:attached\s+file[a-z]*|in\s+the\s+attachment|from\s+the\s+file|uploaded\s+file[a-z]*|in\s+the\s+uploaded\s+file)\b",
        re.IGNORECASE,
    ),
]


def extract_file_instruction(
    text: str, available_files: list[dict[str, Any]] | None = None
) -> tuple[bool, str | None, str]:
    """
    Detects if the user prompt requests reading an attached Slack file via command or natural language.
    Returns: (is_requested: bool, target_filename: str | None, cleaned_text: str)
    """
    if not text:
        return False, None, ""

    is_requested = False
    target_filename = None
    cleaned_text = text

    # 1. Check for slash command (e.g. /read-file, /file sample.py, /read_file: data.csv)
    cmd_match = FILE_COMMAND_PATTERN.search(text)
    if cmd_match:
        is_requested = True
        # Extract matched target if any of the filename groups matched
        target = next((g for g in cmd_match.groups() if g is not None), None)
        if target:
            target_filename = target.strip("[]():,; \t")
        cleaned_text = FILE_COMMAND_PATTERN.sub("", text).strip()
        cleaned_text = re.sub(r"[ \t]+", " ", cleaned_text).strip()
        return is_requested, target_filename, cleaned_text

    # 2. Check for natural language triggers
    for pattern in FILE_NL_PATTERNS:
        if pattern.search(text):
            is_requested = True
            break

    # 3. If available_files is provided, check if any uploaded file's name is explicitly mentioned in the text
    if available_files:
        for f in available_files:
            fname = f.get("name")
            if fname and re.search(
                r"\b" + re.escape(fname) + r"\b", text, re.IGNORECASE
            ):
                is_requested = True
                target_filename = fname
                break

    return is_requested, target_filename, cleaned_text


# ==============================================================================
# 5. Stop Command Patterns
# ==============================================================================

STOP_COMMAND_PATTERN = re.compile(r"(?:^|\s)/stop(?=[\s!.,;?]|$)", re.IGNORECASE)


def is_stop_command(raw_text: str, is_mention_or_dm: bool = False) -> bool:
    """
    Detects if the message is an explicit /stop command directed at the bot.
    Supports:
    - /stop
    """
    if not raw_text:
        return False

    return bool(STOP_COMMAND_PATTERN.search(raw_text))


__all__ = [
    "HISTORY_COMMAND_PATTERN",
    "HISTORY_NL_PATTERNS",
    "extract_history_limit",
    "MAX_WORDS_COMMAND_PATTERN",
    "MAX_WORDS_NL_PATTERNS",
    "extract_max_words_limit",
    "MIN_WORDS_COMMAND_PATTERN",
    "MIN_WORDS_NL_PATTERNS",
    "extract_min_words_limit",
    "FILE_COMMAND_PATTERN",
    "FILE_NL_PATTERNS",
    "extract_file_instruction",
    "STOP_COMMAND_PATTERN",
    "is_stop_command",
]
