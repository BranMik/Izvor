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
    # Croatian & English last/past/previous count
    re.compile(
        r"\b(?:uzmi\s+u\s+obzir\s+|pogledaj\s+|samo\s+|take\s+into\s+account\s+|only\s+|consider\s+|include\s+|use\s+|based\s+on\s+|keep\s+|look\s+at\s+)?(?:the\s+)?(?:zadnj[a-z]*|posljednj[a-z]*|prethodn[a-z]*|last|past|previous)\s+(\d+)\s*(?:poruk[a-z]*|messages?|turns?|posts?)\b",
        re.IGNORECASE,
    ),
    # Croatian & English history limit phrases
    re.compile(
        r"\b(?:povijest|history|context)\s*(?:od|of)?\s*[:=]?\s*(\d+)\s*(?:poruk[a-z]*|messages?|turns?|posts?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:history\s+limit|context\s+limit|limit\s+history\s+to|limit\s+context\s+to|keep\s+only|use\s+only)\s*[:=]?\s*(?:the\s+)?(?:last|past|previous)?\s*(\d+)\s*(?:messages?|turns?|posts?)?\b",
        re.IGNORECASE,
    ),
]


def extract_history_limit(text: str) -> tuple[int | None, str]:
    """
    Extracts the history limit parameter if present in text and returns (history_limit, cleaned_text).

    Supports:
    - Slash command: /history 5, /history: 5, /history=5, /history [5:int], /history (5)
    - Natural language: "uzmi u obzir zadnjih 5 poruka", "last 3 messages", "povijest od 4 poruke",
      "consider the previous 5 messages", "limit history to 3 messages"
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
    # Direct max phrases in HR & EN: "in max 120 words", "u najviše 50 riječi", "up to 80 words", "no more than 100 words"
    re.compile(
        r"\b(?:u\s+|in\s+)?(?:maksimalno|maximalno|najvi[sš]e|najvi[sš]e\s+do|max\.?|maximum|at\s+most|no\s+more\s+than|up\s+to|maximum\s+of|max\s+of)\s+(?:od\s+)?(\d+)\s*(?:rije[cč]i|words)\b",
        re.IGNORECASE,
    ),
    # Limit declarations: "maksimalna duljina: 100 riječi", "maximum length: 50 words", "word limit: 100 words", "max words: 50"
    re.compile(
        r"\b(?:maksimaln[a-z]*\s+duljin[a-z]*|maksimaln[a-z]*\s+du[zž]in[a-z]*|maximaln[a-z]*\s+duljin[a-z]*|maximaln[a-z]*\s+du[zž]in[a-z]*|maximum\s+length|maximum\s+words?|max\s+words?|max\s+word\s+count|word\s+limit|upper\s+limit|limit\s+od|limit\s+to)\s*[:=]?\s*(\d+)\s*(?:rije[cč]i|words)?\b",
        re.IGNORECASE,
    ),
    # Verb triggers: "odgovori u do 50 riječi", "reply in under 50 words", "keep it below 100 words", "write within 80 words"
    re.compile(
        r"\b(?:odgovori|pi[sš]i|odgovor|reply|answer|write|respond|keep\s+it)\s+(?:u\s+|in\s+|with\s+)?(?:do|ispod|under|below|within|less\s+than|fewer\s+than)\s+(\d+)\s*(?:rije[cč]i|words)\b",
        re.IGNORECASE,
    ),
    # Direct "keep/limit to X words"
    re.compile(
        r"\b(?:keep\s+(?:it\s+)?to|limit\s+(?:response\s+)?to)\s+(?:at\s+most\s+|up\s+to\s+|maximum\s+)?(\d+)\s*words\b",
        re.IGNORECASE,
    ),
]


def extract_max_words_limit(text: str) -> tuple[int | None, str]:
    """
    Extracts the max_words limit parameter if present in text and returns (max_words_limit, cleaned_text).

    Supports:
    - Slash command: /max_words 50, /max_words: 50, /max_words=50, /max_words [50:int]
    - Natural language: "odgovori u maksimalno 100 riječi", "najviše 50 riječi", "in max 120 words",
      "up to 80 words", "reply in under 50 words", "word limit: 100 words", "no more than 80 words"
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
    # Direct min phrases in HR & EN: "in min 100 words", "at least 150 words", "no less than 80 words", "minimum of 100 words"
    re.compile(
        r"\b(?:u\s+|in\s+)?(?:minimalno|najmanje|barem|bar|min\.?|minimum|at\s+least|no\s+less\s+than|no\s+fewer\s+than|at\s+a\s+minimum\s+of|minimum\s+of)\s+(?:od\s+)?(\d+)\s*(?:rije[cč]i|words)\b",
        re.IGNORECASE,
    ),
    # Limit declarations: "minimalna duljina: 100 riječi", "minimum length: 150 words", "min words: 100", "min word count: 120"
    re.compile(
        r"\b(?:minimaln[a-z]*\s+duljin[a-z]*|minimaln[a-z]*\s+du[zž]in[a-z]*|minimum\s+length|minimum\s+words?|min\s+words?|min\s+word\s+count|lower\s+limit|donj[a-z]*\s+granic[a-z]*)\s*[:=]?\s*(\d+)\s*(?:rije[cč]i|words)?\b",
        re.IGNORECASE,
    ),
    # Verb triggers: "odgovori u minimalno 100 riječi", "write at least 200 words", "reply with over 150 words"
    re.compile(
        r"\b(?:odgovori|pi[sš]i|odgovor|reply|answer|write|respond)\s+(?:u\s+|in\s+|with\s+)?(?:minimalno|najmanje|barem|bar|min\.?|minimum|at\s+least|no\s+less\s+than|no\s+fewer\s+than|more\s+than|over|above)\s+(?:od\s+)?(\d+)\s*(?:rije[cč]i|words)\b",
        re.IGNORECASE,
    ),
]


def extract_min_words_limit(text: str) -> tuple[int | None, str]:
    """
    Extracts the min_words limit parameter if present in text and returns (min_words_limit, cleaned_text).

    Supports:
    - Slash command: /min_words 50, /min_words: 50, /min_words=50, /min_words [50:int]
    - Natural language: "odgovori u minimalno 100 riječi", "najmanje 80 riječi", "barem 150 riječi",
      "at least 100 words", "write at least 150 words", "minimum length: 120 words", "no less than 80 words"
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
    # Croatian action triggers
    re.compile(
        r"\b(?:pro[čc]itaj|pogledaj|analiziraj|provjeri|u[čc]itaj|pregledaj|obradi|otvori)\b.*?\b(?:fajl[a-z]*|datotek[a-z]*|privit[a-z]*|attachment[a-z]*|prilo[zž]en[a-z]*|kod\s+u\s+privitku|dokument[a-z]*)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:u\s+privitku|u\s+fajlu|u\s+datoteci|iz\s+fajla|iz\s+datoteke|iz\s+privitka|(?:u\s+)?prilo[zž]en[a-z]*\s+(?:fajl[a-z]*|datotek[a-z]*|privit[a-z]*|kod[a-z]*|dokument[a-z]*)|uploadao\s+sam\s+fajl|poslao\s+sam\s+fajl|uploadan[a-z]*\s+fajl[a-z]*)\b",
        re.IGNORECASE,
    ),
    # English action triggers
    re.compile(
        r"\b(?:read|check|analyze|analyse|inspect|review|examine|look\s+at|open|summarize|summarise|evaluate|parse|process|scan|audit|extract\s+from)\b.*?\b(?:file[a-z]*|attachment[a-z]*|document[a-z]*|uploaded\s+file[a-z]*|uploaded\s+code|code\s+in\s+attachment)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:attached\s+file[a-z]*|in\s+the\s+attachment|from\s+the\s+file|uploaded\s+file[a-z]*|in\s+the\s+uploaded\s+file|file\s+attached)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:i\s+have\s+attached|i\s+attached|i\s+uploaded|i\s+sent\s+a\s+file|see\s+(?:the\s+)?(?:attachment|attached\s+file|uploaded\s+file)|refer\s+to\s+(?:the\s+)?(?:attachment|attached\s+file|uploaded\s+file)|check\s+out\s+(?:the\s+)?(?:attachment|attached\s+file|uploaded\s+file))\b",
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


# ==============================================================================
# 6. Exclude Agent Patterns (/exclude [Agent1] [Agent2]... & NL Phrasing)
# ==============================================================================

EXCLUDE_COMMAND_PATTERN = re.compile(
    r"/exclude\s*[:=]?(?:\s*(?:\[[^\]]+\]|[\w\-]+))+(?:[,\s]+(?:\[[^\]]+\]|[\w\-]+))*",
    re.IGNORECASE,
)

EXCLUDE_NL_PATTERNS = [
    # English: "exclude [agent]", "exclude agent X from debate", "leave out X", "skip agent X", "without [agent]"
    re.compile(
        r"\b(?:exclude|leave\s+out|skip|omit|without|don't\s+include|do\s+not\s+include|remove)\s+(?:the\s+)?(?:agent\s+)?(\[[^\]]+\]|[a-zA-Z0-9_]+)(?:\s+from\s+(?:the\s+)?(?:debate|discussion|panel|conversation))?",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:debate|discuss)\s+.*?\bwithout\s+(?:the\s+)?(?:agent\s+)?(\[[^\]]+\]|[a-zA-Z0-9_]+)",
        re.IGNORECASE,
    ),
    # Croatian: "isključi agenta X", "bez [agent]", "izostavi X iz rasprave", "nemoj uključiti agenta X", "preskoči X"
    re.compile(
        r"\b(?:isklju[čc]i|izostavi|presko[čc]i|makni|izbaci|ukloni|nemoj\s+uklju[čc]ivati|nemoj\s+uklju[čc]iti|bez)\s+(?:agenta\s+|agenticu\s+)?(\[[^\]]+\]|[a-zA-Z0-9_]+)(?:\s+(?:iz|u)\s+rasprav[a-z]*)?",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:raspravite|rasprava)\s+.*?\bbez\s+(?:agenta\s+|agentice\s+)?(\[[^\]]+\]|[a-zA-Z0-9_]+)",
        re.IGNORECASE,
    ),
]


def resolve_agent_candidates(name_str: str) -> list[str]:
    """Resolves an agent name or alias string to all matching canonical AGENTS_CONFIG keys."""
    from pipeline.entity_resolver import entity_resolver
    return entity_resolver.resolve_candidates(name_str)


def resolve_agent_name(name_str: str) -> str | None:
    """Resolves an agent name or alias string to its canonical key if unambiguous."""
    from pipeline.entity_resolver import entity_resolver
    return entity_resolver.resolve_agent_name(name_str)



def extract_excluded_agents(text: str) -> tuple[list[str], str]:
    """
    Extracts excluded agent keys from text (via slash command /exclude [Agent1] [Agent2]...
    or natural language phrasing in English and Croatian).
    Returns (excluded_agent_keys: list[str], cleaned_text: str).
    """
    if not text:
        return [], ""

    excluded: list[str] = []
    cleaned_text = text

    # 1. Extract from slash commands: /exclude [Agent1] [Agent2] ...
    token_pattern = re.compile(
        r"(\s*(?:,|and|&)?\s*)(\[[^\]]+\]|[a-zA-Z0-9_]+)", re.IGNORECASE
    )

    while True:
        match = re.search(r"/exclude\s*[:=]?", cleaned_text, re.IGNORECASE)
        if not match:
            break

        start_idx = match.start()
        curr_idx = match.end()
        matched_tokens_span_end = curr_idx
        remainder = cleaned_text[curr_idx:]

        offset = 0
        while True:
            t_match = token_pattern.match(remainder[offset:])
            if not t_match:
                break
            _, token = t_match.groups()
            resolved = resolve_agent_name(token)
            if resolved:
                if resolved not in excluded:
                    excluded.append(resolved)
                offset += len(t_match.group(0))
                matched_tokens_span_end = curr_idx + offset
            elif token.startswith("[") and token.endswith("]"):
                # Bracketed token after /exclude is consumed as part of command
                offset += len(t_match.group(0))
                matched_tokens_span_end = curr_idx + offset
            else:
                # Non-agent word token -> user prompt starts here
                break

        cleaned_text = cleaned_text[:start_idx] + " " + cleaned_text[matched_tokens_span_end:]

    # 2. Extract from natural language patterns
    for pattern in EXCLUDE_NL_PATTERNS:
        while True:
            match = pattern.search(cleaned_text)
            if not match:
                break
            agent_token = match.group(1) if match.groups() else ""
            resolved = resolve_agent_name(agent_token)
            if resolved:
                if resolved not in excluded:
                    excluded.append(resolved)
                matched_phrase = match.group(0)
                cleaned_text = cleaned_text.replace(matched_phrase, " ", 1)
            else:
                # If cannot resolve agent, break to prevent infinite loop on this pattern
                break

    cleaned_text = re.sub(r"[ \t]+", " ", cleaned_text).strip()
    return excluded, cleaned_text


__all__ = [
    "EXCLUDE_COMMAND_PATTERN",
    "EXCLUDE_NL_PATTERNS",
    "FILE_COMMAND_PATTERN",
    "FILE_NL_PATTERNS",
    "HISTORY_COMMAND_PATTERN",
    "HISTORY_NL_PATTERNS",
    "MAX_WORDS_COMMAND_PATTERN",
    "MAX_WORDS_NL_PATTERNS",
    "MIN_WORDS_COMMAND_PATTERN",
    "MIN_WORDS_NL_PATTERNS",
    "STOP_COMMAND_PATTERN",
    "extract_excluded_agents",
    "extract_file_instruction",
    "extract_history_limit",
    "extract_max_words_limit",
    "extract_min_words_limit",
    "is_stop_command",
    "resolve_agent_candidates",
    "resolve_agent_name",
]
