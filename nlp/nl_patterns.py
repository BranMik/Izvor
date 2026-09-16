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
    r"/(?:max_words|max-words|maxwords)\s*[:=]?\s*\[?(\d+)(?::(?:int|integer))?\]?",
    re.IGNORECASE,
)

MAX_WORDS_NL_PATTERNS: list[re.Pattern[str]] = []


def extract_max_words_limit(text: str) -> tuple[int | None, str]:
    """
    Extracts the max_words limit parameter if present in text and returns (max_words_limit, cleaned_text).

    Supports:
    - Slash command: /max_words 50, /max_words: 50, /max_words=50, /max_words [50:int]
    - Natural language: Delegated to IntentExtractor (Micro-LLM).
    """
    if not text:
        return None, ""
    match = MAX_WORDS_COMMAND_PATTERN.search(text)
    if match:
        limit = max(1, int(match.group(1)))
        cleaned = MAX_WORDS_COMMAND_PATTERN.sub("", text).strip()
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

MIN_WORDS_NL_PATTERNS: list[re.Pattern[str]] = []


def extract_min_words_limit(text: str) -> tuple[int | None, str]:
    """
    Extracts the min_words limit parameter if present in text and returns (min_words_limit, cleaned_text).

    Supports:
    - Slash command: /min_words 50, /min_words: 50, /min_words=50, /min_words [50:int]
    - Natural language: Delegated to IntentExtractor (Micro-LLM).
    """
    if not text:
        return None, ""
    match = MIN_WORDS_COMMAND_PATTERN.search(text)
    if match:
        limit = max(1, int(match.group(1)))
        cleaned = MIN_WORDS_COMMAND_PATTERN.sub("", text).strip()
        cleaned = re.sub(r"[ \t]+", " ", cleaned).strip()
        return limit, cleaned

    return None, text


# ==============================================================================
# 4. Attachment / File Instruction Patterns
# ==============================================================================

FILE_COMMAND_PATTERN = re.compile(
    r"/(?:read[-_]?file|file|attachment|image|screenshot|photo|diagram)(?:\s*[:=]\s*(\S+)|\s+\[([^\]]+)\]|\s+\(([^)]+)\)|\s+([\w\-.]+\.[a-zA-Z0-9]+))?",
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

        cleaned_text = (
            cleaned_text[:start_idx] + " " + cleaned_text[matched_tokens_span_end:]
        )

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
