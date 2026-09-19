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

HISTORY_NL_PATTERNS: list[re.Pattern[str]] = []


def extract_history_limit(text: str) -> tuple[int | None, str]:
    """
    Extracts the history limit parameter if present in text and returns (history_limit, cleaned_text).

    Supports:
    - Slash command: /history 5, /history: 5, /history=5, /history [5:int], /history (5)
    - Natural language: Delegated to IntentExtractor (Micro-LLM).
    """
    if not text:
        return None, ""
    match = HISTORY_COMMAND_PATTERN.search(text)
    if match:
        limit = int(match.group(1))
        cleaned = HISTORY_COMMAND_PATTERN.sub("", text).strip()
        cleaned = re.sub(r"[ \t]+", " ", cleaned).strip()
        return limit, cleaned

    return None, text


# ==============================================================================
# 2. Maximum Words (max_words) Patterns
# ==============================================================================

MAX_WORDS_COMMAND_PATTERN = re.compile(
    r"/(?:max_words|max-words|maxwords)\s*[:=]?\s*(?:\[?(\d+)(?::(?:int|integer))?\]?|(off)\b)",
    re.IGNORECASE,
)

MAX_WORDS_NL_PATTERNS: list[re.Pattern[str]] = []


def extract_max_words_limit(text: str) -> tuple[int | str | None, str]:
    """
    Extracts the max_words limit parameter if present in text and returns (max_words_limit, cleaned_text).
    Returns an int limit, 'off' if explicitly turned off, or None.

    Supports:
    - Slash command: /max_words 50, /max_words: 50, /max_words=50, /max_words [50:int], /max_words off
    - Natural language: Delegated to IntentExtractor (Micro-LLM).
    """
    if not text:
        return None, ""
    match = MAX_WORDS_COMMAND_PATTERN.search(text)
    if match:
        if match.group(2) and match.group(2).lower() == "off":
            limit: int | str | None = "off"
        else:
            limit = max(1, int(match.group(1)))
        cleaned = MAX_WORDS_COMMAND_PATTERN.sub("", text).strip()
        cleaned = re.sub(r"[ \t]+", " ", cleaned).strip()
        return limit, cleaned

    return None, text


# ==============================================================================
# 3. Minimum Words (min_words) Patterns
# ==============================================================================

MIN_WORDS_COMMAND_PATTERN = re.compile(
    r"/(?:min_words|min-words|minwords)\s*[:=]?\s*(?:\[?(\d+)(?::(?:int|integer))?\]?|(off)\b)",
    re.IGNORECASE,
)

MIN_WORDS_NL_PATTERNS: list[re.Pattern[str]] = []


def extract_min_words_limit(text: str) -> tuple[int | str | None, str]:
    """
    Extracts the min_words limit parameter if present in text and returns (min_words_limit, cleaned_text).
    Returns an int limit, 'off' if explicitly turned off, or None.

    Supports:
    - Slash command: /min_words 50, /min_words: 50, /min_words=50, /min_words [50:int], /min_words off
    - Natural language: Delegated to IntentExtractor (Micro-LLM).
    """
    if not text:
        return None, ""
    match = MIN_WORDS_COMMAND_PATTERN.search(text)
    if match:
        if match.group(2) and match.group(2).lower() == "off":
            limit: int | str | None = "off"
        else:
            limit = max(1, int(match.group(1)))
        cleaned = MIN_WORDS_COMMAND_PATTERN.sub("", text).strip()
        cleaned = re.sub(r"[ \t]+", " ", cleaned).strip()
        return limit, cleaned

    return None, text


# ==============================================================================
# 3b. Scribe Maximum and Minimum Words Patterns
# ==============================================================================

SCRIBE_MAX_WORDS_COMMAND_PATTERN = re.compile(
    r"/(?:scribe_max_words|scribe-max-words|scribemaxwords)\s*[:=]?\s*(?:\[?(\d+)(?::(?:int|integer))?\]?|(off)\b)",
    re.IGNORECASE,
)

SCRIBE_MIN_WORDS_COMMAND_PATTERN = re.compile(
    r"/(?:scribe_min_words|scribe-min-words|scribeminwords)\s*[:=]?\s*(?:\[?(\d+)(?::(?:int|integer))?\]?|(off)\b)",
    re.IGNORECASE,
)


def extract_scribe_max_words_limit(text: str) -> tuple[int | str | None, str]:
    """
    Extracts the scribe_max_words limit parameter if present in text and returns (limit, cleaned_text).
    Returns an int limit, 'off' if explicitly turned off, or None.
    """
    if not text:
        return None, ""
    match = SCRIBE_MAX_WORDS_COMMAND_PATTERN.search(text)
    if match:
        if match.group(2) and match.group(2).lower() == "off":
            limit: int | str | None = "off"
        else:
            limit = max(1, int(match.group(1)))
        cleaned = SCRIBE_MAX_WORDS_COMMAND_PATTERN.sub("", text).strip()
        cleaned = re.sub(r"[ \t]+", " ", cleaned).strip()
        return limit, cleaned

    return None, text


def extract_scribe_min_words_limit(text: str) -> tuple[int | str | None, str]:
    """
    Extracts the scribe_min_words limit parameter if present in text and returns (limit, cleaned_text).
    Returns an int limit, 'off' if explicitly turned off, or None.
    """
    if not text:
        return None, ""
    match = SCRIBE_MIN_WORDS_COMMAND_PATTERN.search(text)
    if match:
        if match.group(2) and match.group(2).lower() == "off":
            limit: int | str | None = "off"
        else:
            limit = max(1, int(match.group(1)))
        cleaned = SCRIBE_MIN_WORDS_COMMAND_PATTERN.sub("", text).strip()
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

FILE_NL_PATTERNS: list[re.Pattern[str]] = []


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

EXCLUDE_NL_PATTERNS: list[re.Pattern[str]] = []


def resolve_agent_candidates(name_str: str) -> list[str]:
    """Resolves an agent name or alias string to all matching canonical AGENTS_CONFIG keys."""
    from pipeline.entity_resolver import entity_resolver

    return entity_resolver.resolve_candidates(name_str)


def resolve_agent_name(name_str: str) -> str | None:
    """Resolves an agent name or alias string to its canonical key if unambiguous."""
    from pipeline.entity_resolver import entity_resolver

    return entity_resolver.resolve_agent_name(name_str)


EXCLUDE_OFF_PATTERN = re.compile(r"/exclude\s*[:=]?\s*off\b", re.IGNORECASE)
INCLUDE_OFF_PATTERN = re.compile(r"/include\s*[:=]?\s*off\b", re.IGNORECASE)
REVOKE_COMMAND_PATTERN = re.compile(r"/revoke\s*[:=]?\s*(?:\[?([a-zA-Z0-9_-]+)\]?|all\b)?", re.IGNORECASE)


def extract_excluded_agents(text: str) -> tuple[list[str], bool, str]:
    """
    Extracts excluded agent keys from text (via slash command /exclude [Agent1] [Agent2]...
    or /exclude off).
    Returns (excluded_agent_keys: list[str], is_off: bool, cleaned_text: str).
    """
    if not text:
        return [], False, ""

    cleaned_text = text
    is_off = False

    # Check for /exclude off
    if EXCLUDE_OFF_PATTERN.search(cleaned_text):
        is_off = True
        cleaned_text = EXCLUDE_OFF_PATTERN.sub("", cleaned_text).strip()
        cleaned_text = re.sub(r"[ \t]+", " ", cleaned_text).strip()
        return [], is_off, cleaned_text

    excluded: list[str] = []

    # Extract from slash commands: /exclude [Agent1] [Agent2] ...
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

        cleaned_text = (
            cleaned_text[:start_idx] + " " + cleaned_text[matched_tokens_span_end:]
        )

    cleaned_text = re.sub(r"[ \t]+", " ", cleaned_text).strip()
    return excluded, is_off, cleaned_text


def extract_included_agents(text: str) -> tuple[list[str], bool, str]:
    """
    Extracts included agent keys from text (via slash command /include [Agent1] [Agent2]...
    or /include off).
    Returns (included_agent_keys: list[str], is_off: bool, cleaned_text: str).
    """
    if not text:
        return [], False, ""

    cleaned_text = text
    is_off = False

    # Check for /include off
    if INCLUDE_OFF_PATTERN.search(cleaned_text):
        is_off = True
        cleaned_text = INCLUDE_OFF_PATTERN.sub("", cleaned_text).strip()
        cleaned_text = re.sub(r"[ \t]+", " ", cleaned_text).strip()
        return [], is_off, cleaned_text

    included: list[str] = []

    # Extract from slash commands: /include [Agent1] [Agent2] ...
    token_pattern = re.compile(
        r"(\s*(?:,|and|&)?\s*)(\[[^\]]+\]|[a-zA-Z0-9_]+)", re.IGNORECASE
    )

    while True:
        match = re.search(r"/include\s*[:=]?", cleaned_text, re.IGNORECASE)
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
                if resolved not in included:
                    included.append(resolved)
                offset += len(t_match.group(0))
                matched_tokens_span_end = curr_idx + offset
            elif token.startswith("[") and token.endswith("]"):
                # Bracketed token after /include is consumed as part of command
                offset += len(t_match.group(0))
                matched_tokens_span_end = curr_idx + offset
            else:
                # Non-agent word token -> user prompt starts here
                break

        cleaned_text = (
            cleaned_text[:start_idx] + " " + cleaned_text[matched_tokens_span_end:]
        )

    cleaned_text = re.sub(r"[ \t]+", " ", cleaned_text).strip()
    return included, is_off, cleaned_text


def extract_revoke_instruction(text: str) -> tuple[str | None, str]:
    """
    Extracts /revoke command instruction (e.g. /revoke max_words, /revoke all).
    Returns (target: str | None, cleaned_text: str).
    """
    if not text:
        return None, ""

    cleaned_text = text
    match = REVOKE_COMMAND_PATTERN.search(cleaned_text)
    if match:
        raw_target = match.group(1) or "all"
        target = raw_target.strip().lower()
        cleaned_text = REVOKE_COMMAND_PATTERN.sub("", cleaned_text).strip()
        cleaned_text = re.sub(r"[ \t]+", " ", cleaned_text).strip()
        return target, cleaned_text

    return None, text


__all__ = [
    "EXCLUDE_COMMAND_PATTERN",
    "EXCLUDE_NL_PATTERNS",
    "EXCLUDE_OFF_PATTERN",
    "FILE_COMMAND_PATTERN",
    "FILE_NL_PATTERNS",
    "HISTORY_COMMAND_PATTERN",
    "HISTORY_NL_PATTERNS",
    "INCLUDE_OFF_PATTERN",
    "MAX_WORDS_COMMAND_PATTERN",
    "MAX_WORDS_NL_PATTERNS",
    "MIN_WORDS_COMMAND_PATTERN",
    "MIN_WORDS_NL_PATTERNS",
    "REVOKE_COMMAND_PATTERN",
    "SCRIBE_MAX_WORDS_COMMAND_PATTERN",
    "SCRIBE_MIN_WORDS_COMMAND_PATTERN",
    "STOP_COMMAND_PATTERN",
    "extract_excluded_agents",
    "extract_file_instruction",
    "extract_history_limit",
    "extract_included_agents",
    "extract_max_words_limit",
    "extract_min_words_limit",
    "extract_revoke_instruction",
    "extract_scribe_max_words_limit",
    "extract_scribe_min_words_limit",
    "is_stop_command",
    "resolve_agent_candidates",
    "resolve_agent_name",
]
