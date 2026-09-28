"""
Generic, deterministic slash command type parsers.
These parsers handle parameter extraction and prompt cleaning driven entirely by
declarative metadata (arg_type, param_name) defined in SKILL.md files.
"""

import re
from typing import Any

from pipeline.entity_resolver import entity_resolver


def clean_spaces(text: str) -> str:
    """Collapses multiple spaces/tabs into a single space and strips ends."""
    return re.sub(r"[ \t]+", " ", text).strip()


def parse_terminal_flag(slash_cmd: str, text: str) -> tuple[bool, str]:
    """
    Detects if raw_text contains an explicit terminal/stop command.
    Returns (matched: bool, cleaned_text: str).
    """
    if not text:
        return False, ""

    pattern = re.compile(rf"(?:^|\s){re.escape(slash_cmd)}(?=[\s!.,;?]|$)", re.IGNORECASE)
    match = pattern.search(text)
    if match:
        cleaned = pattern.sub(" ", text)
        return True, clean_spaces(cleaned)
    return False, text


def parse_int_or_off(
    slash_cmd: str, text: str, min_val: int | None = None
) -> tuple[bool, int | str | None, str]:
    """
    Extracts an integer value, bracketed integer [5:int], or 'off' from a slash command.
    Returns (matched: bool, value: int | str | None, cleaned_text: str).
    """
    if not text:
        return False, None, ""

    pattern = re.compile(
        rf"(?:^|\s){re.escape(slash_cmd)}\s*[:=]?\s*(?:\[?(\d+)(?::(?:int|integer))?\]?|(off)\b)",
        re.IGNORECASE,
    )
    match = pattern.search(text)
    if match:
        if match.group(2) and match.group(2).lower() == "off":
            val: int | str | None = "off"
        else:
            raw_int = int(match.group(1))
            val = max(min_val, raw_int) if min_val is not None else raw_int
        cleaned = pattern.sub(" ", text)
        return True, val, clean_spaces(cleaned)

    # Fallback: check if command was specified without arguments (e.g. /rounds)
    bare_pattern = re.compile(
        rf"(?:^|\s){re.escape(slash_cmd)}(?=[\s:=]|$)", re.IGNORECASE
    )
    if bare_pattern.search(text):
        cleaned = bare_pattern.sub(" ", text)
        return True, None, clean_spaces(cleaned)

    return False, None, text


def parse_agent_list(
    slash_cmd: str, text: str
) -> tuple[bool, list[str], bool, str]:
    """
    Extracts agent keys from text for commands like /exclude or /include,
    supporting /<cmd> [Agent1] [Agent2]... or /<cmd> off.
    Returns (matched: bool, agents: list[str], is_off: bool, cleaned_text: str).
    """
    if not text:
        return False, [], False, ""

    off_pattern = re.compile(
        rf"(?:^|\s){re.escape(slash_cmd)}\s*[:=]?\s*off\b", re.IGNORECASE
    )
    cleaned_text = text
    is_off = False
    matched_cmd = False

    if off_pattern.search(cleaned_text):
        is_off = True
        matched_cmd = True
        cleaned_text = off_pattern.sub(" ", cleaned_text)
        return True, [], is_off, clean_spaces(cleaned_text)

    token_pattern = re.compile(
        r"(\s*(?:,|and|&)?\s*)(/?\[[^\]]+\]|[a-zA-Z0-9_]+)", re.IGNORECASE
    )
    agents: list[str] = []

    while True:
        cmd_match = re.search(
            rf"(?:^|\s){re.escape(slash_cmd)}\s*[:=]?", cleaned_text, re.IGNORECASE
        )
        if not cmd_match:
            break

        matched_cmd = True
        start_idx = cmd_match.start()
        curr_idx = cmd_match.end()
        matched_tokens_span_end = curr_idx
        remainder = cleaned_text[curr_idx:]

        offset = 0
        while True:
            t_match = token_pattern.match(remainder[offset:])
            if not t_match:
                break
            _, token = t_match.groups()
            resolved = entity_resolver.resolve_agent_name(token)
            if resolved:
                if resolved not in agents:
                    agents.append(resolved)
                offset += len(t_match.group(0))
                matched_tokens_span_end = curr_idx + offset
            elif (token.startswith("[") and token.endswith("]")) or (token.startswith("/[") and token.endswith("]")):
                offset += len(t_match.group(0))
                matched_tokens_span_end = curr_idx + offset
            else:
                break

        cleaned_text = (
            cleaned_text[:start_idx] + " " + cleaned_text[matched_tokens_span_end:]
        )

    return matched_cmd, agents, is_off, clean_spaces(cleaned_text)


def parse_file_target(
    slash_cmd: str,
    text: str,
    available_files: list[dict[str, Any]] | None = None,
) -> tuple[bool, str | None, str]:
    """
    Detects if the user prompt requests reading an attached Slack file via slash command.
    Returns (matched: bool, target_filename: str | None, cleaned_text: str).
    """
    if not text:
        return False, None, ""

    cmd_pattern = re.compile(
        rf"(?:^|\s){re.escape(slash_cmd)}(?:\s*[:=]\s*(\S+)|\s+\[([^\]]+)\]|\s+\(([^)]+)\)|\s+([\w\-.]+\.[a-zA-Z0-9]+))?",
        re.IGNORECASE,
    )

    match = cmd_pattern.search(text)
    if match:
        target = next((g for g in match.groups() if g is not None), None)
        target_filename = target.strip("[]():,; \t") if target else None
        cleaned = cmd_pattern.sub(" ", text)
        return True, target_filename, clean_spaces(cleaned)

    # Check available files by name match in text
    if available_files:
        for f in available_files:
            fname = f.get("name")
            if fname and re.search(r"\b" + re.escape(fname) + r"\b", text, re.IGNORECASE):
                return True, fname, text

    return False, None, text


def parse_token(slash_cmd: str, text: str) -> tuple[bool, str | None, str]:
    """
    Extracts a single identifier argument or 'all' (e.g. /revoke max_words, /revoke all).
    Returns (matched: bool, target: str | None, cleaned_text: str).
    """
    if not text:
        return False, None, ""

    pattern = re.compile(
        rf"(?:^|\s){re.escape(slash_cmd)}(?![-_])\s*[:=]?\s*(?:\[?([a-zA-Z0-9_-]+)\]?|all\b)?",
        re.IGNORECASE,
    )
    match = pattern.search(text)
    if match:
        raw_target = match.group(1) or "all"
        target = raw_target.strip().lower()
        cleaned = pattern.sub(" ", text)
        return True, target, clean_spaces(cleaned)

    return False, None, text


def parse_grant_skill(
    slash_cmd: str, text: str
) -> tuple[bool, str, list[str], str]:
    """
    Extracts skill name and agent targets for /grant_skill <skill> [Agent1] [Agent2].
    Returns (matched: bool, skill_name: str, agents_list: list[str], cleaned_text: str).
    """
    if not text:
        return False, "", [], ""

    cmd_match = re.search(
        rf"(?:^|\s){re.escape(slash_cmd)}(?=[\s:=]|$)", text, re.IGNORECASE
    )
    if not cmd_match:
        return False, "", [], text

    start_idx = cmd_match.start()
    curr_idx = cmd_match.end()
    remainder = text[curr_idx:]

    token_pattern = re.compile(
        r"(\s*(?:,|and|&)?\s*)(\[[^\]]+\]|<@[A-Za-z0-9_]+>|@[a-zA-Z0-9_-]+|[a-zA-Z0-9_-]+)",
        re.IGNORECASE,
    )
    offset = 0
    tokens: list[str] = []

    while True:
        t_match = token_pattern.match(remainder[offset:])
        if not t_match:
            break
        _, raw_token = t_match.groups()
        token_clean = raw_token.strip("[]@<>\t ")
        is_bracketed = (
            raw_token.startswith("[") and raw_token.endswith("]") or raw_token.startswith(("@", "<@"))
        )

        if not tokens:
            tokens.append(token_clean)
            offset += len(t_match.group(0))
        else:
            is_agent = bool(entity_resolver.resolve_candidates(token_clean))
            if is_agent or is_bracketed or len(tokens) == 1:
                tokens.append(token_clean)
                offset += len(t_match.group(0))
            else:
                break

    matched_tokens_span_end = curr_idx + offset
    skill_name = tokens[0] if tokens else ""
    agents_list = tokens[1:] if len(tokens) > 1 else []
    cleaned_text = text[:start_idx] + " " + text[matched_tokens_span_end:]
    return True, skill_name, agents_list, clean_spaces(cleaned_text)


def parse_list(
    slash_cmd: str, text: str, known_items: set[str] | None = None
) -> tuple[bool, list[str], str]:
    """
    Extracts a list of items for commands like /list_skills [skill1] [skill2] or /revoke_for_all.
    Returns (matched: bool, items_list: list[str], cleaned_text: str).
    """
    if not text:
        return False, [], ""

    cmd_match = re.search(
        rf"(?:^|\s){re.escape(slash_cmd)}(?=[\s:=]|$)", text, re.IGNORECASE
    )
    if not cmd_match:
        return False, [], text

    start_idx = cmd_match.start()
    curr_idx = cmd_match.end()
    remainder = text[curr_idx:]

    token_pattern = re.compile(
        r"(\s*(?:,|and|&)?\s*)(\[[^\]]+\]|[a-zA-Z0-9_-]+)", re.IGNORECASE
    )
    offset = 0
    items_list: list[str] = []

    while True:
        t_match = token_pattern.match(remainder[offset:])
        if not t_match:
            break
        _, raw_token = t_match.groups()
        token_clean = raw_token.strip("[]")
        rem_after_token = remainder[offset + len(t_match.group(0)):].strip()
        is_known = known_items and (
            token_clean.lower() in known_items or token_clean.lower() == "all"
        )
        is_last_token = not rem_after_token

        if is_known or (offset == 0 and is_last_token):
            items_list.append(token_clean)
            offset += len(t_match.group(0))
        else:
            break

    matched_tokens_span_end = curr_idx + offset
    cleaned_text = text[:start_idx] + " " + text[matched_tokens_span_end:]
    return True, items_list, clean_spaces(cleaned_text)


def strip_all_slash_commands(text: str) -> str:
    """
    Dynamically strips all registered slash commands and their arguments
    from raw text using triggers from the skill registry.
    """
    if not text:
        return ""

    from skills import skill_registry

    cleaned = text
    # Sort triggers by length descending to match longer commands first
    for trigger, (skill_def, trg_cfg) in sorted(
        skill_registry.get_slash_triggers().items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        arg_type = trg_cfg.get("arg_type")
        if arg_type in ("int", "int_or_off"):
            _, _, cleaned = parse_int_or_off(trigger, cleaned)
        elif arg_type == "agent_list":
            _, _, _, cleaned = parse_agent_list(trigger, cleaned)
        elif arg_type == "file_target":
            _, _, cleaned = parse_file_target(trigger, cleaned)
        elif arg_type == "terminal_flag" or skill_def.is_terminal:
            _, cleaned = parse_terminal_flag(trigger, cleaned)
        elif arg_type == "token":
            _, _, cleaned = parse_token(trigger, cleaned)
        elif arg_type == "list":
            _, _, cleaned = parse_list(trigger, cleaned)
        else:
            pattern = re.compile(
                rf"(?:^|\s){re.escape(trigger)}(?=[\s:=]|$)(?:\s*[:=]?\s*[^\s/]+)?",
                re.IGNORECASE,
            )
            cleaned = clean_spaces(pattern.sub(" ", cleaned))

    return clean_spaces(cleaned)


