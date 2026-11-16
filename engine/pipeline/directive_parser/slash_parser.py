"""
Generic, deterministic slash command type parsers.
These parsers handle parameter extraction and prompt cleaning driven entirely by
declarative metadata (arg_type, param_name) defined in SKILL.md files.
"""

import re
from typing import Any

from pipeline.entity_resolver import entity_resolver


def has_substantive_prompt(text: str | None) -> bool:
    """
    Returns True if text contains substantive prompt characters beyond Slack user/bot
    mentions (<@U...>), whitespace, or mention wrappers.
    """
    if not text:
        return False
    cleaned = re.sub(r"<@[A-Za-z0-9_]+>", " ", text).strip()
    return bool(cleaned)


def remove_command_span(text: str, start: int, end: int) -> str:
    """
    Removes a command span [start, end] from text while strictly preserving
    surrounding prompt formatting, line breaks, code block indentation, and YAML spacing.
    """
    before = text[:start]
    after = text[end:]

    # If the command was on its own line, consume the trailing newline
    if (not before or before.endswith("\n")) and after.startswith("\n"):
        return before + after[1:]
    if before.endswith("\n") and (not after or after.startswith("\n")):
        return before[:-1] + after

    # Collapse double spaces created at excision boundary on the same line
    if before.endswith(" ") and after.startswith(" "):
        after = after.lstrip(" ")
    elif before and after and not before.endswith(("\n", " ", "\t")) and not after.startswith(("\n", " ", "\t")):
        return before + " " + after

    res = before + after
    if not res.strip():
        return ""
    if res.startswith(" "):
        res = res.lstrip(" ")
    if res.endswith(" "):
        res = res.rstrip(" ")
    return res


def parse_terminal_flag(slash_cmd: str, text: str) -> tuple[bool, str]:
    """
    Detects if raw_text contains an explicit terminal/stop command.
    Returns (matched: bool, cleaned_text: str).
    """
    if not text:
        return False, ""

    pattern = re.compile(rf"(?:^|[\r\n\s]){re.escape(slash_cmd)}(?=[\s!.,;?]|$)", re.IGNORECASE)
    match = pattern.search(text)
    if match:
        cleaned = remove_command_span(text, match.start(), match.end())
        return True, cleaned
    return False, text


def parse_int_or_off(
    slash_cmd: str,
    text: str,
    min_val: int | None = None,
    max_val: int | None = None,
) -> tuple[bool, int | str | None, str]:
    """
    Extracts an integer value, bracketed integer [5:int], or 'off' from a slash command.
    Enforces min_val and max_val bounds when specified.
    Returns (matched: bool, value: int | str | None, cleaned_text: str).
    """
    if not text:
        return False, None, ""

    pattern = re.compile(
        rf"(?:^|[\r\n\s]){re.escape(slash_cmd)}\s*[:=]?\s*(?:\[?(\d+)(?::(?:int|integer))?\]?|(off)\b)",
        re.IGNORECASE,
    )
    match = pattern.search(text)
    if match:
        if match.group(2) and match.group(2).lower() == "off":
            val: int | str | None = "off"
        else:
            raw_int = int(match.group(1))
            val = raw_int
            if min_val is not None:
                val = max(min_val, val)
            if max_val is not None:
                val = min(max_val, val)
        cleaned = remove_command_span(text, match.start(), match.end())
        return True, val, cleaned

    # Fallback: check if command was specified without arguments (e.g. /rounds)
    bare_pattern = re.compile(rf"(?:^|[\r\n\s]){re.escape(slash_cmd)}(?=[\s:=]|$)", re.IGNORECASE)
    bare_match = bare_pattern.search(text)
    if bare_match:
        cleaned = remove_command_span(text, bare_match.start(), bare_match.end())
        return True, None, cleaned

    return False, None, text


def parse_agent_list(slash_cmd: str, text: str) -> tuple[bool, list[str], bool, str]:
    """
    Extracts agent keys from text for commands like /exclude or /include,
    supporting /<cmd> [Agent1] [Agent2]... or /<cmd> off.
    Strictly whitespace-separated, deterministic instruction parsing.
    Returns (matched: bool, agents: list[str], is_off: bool, cleaned_text: str).
    """
    if not text:
        return False, [], False, ""

    off_pattern = re.compile(rf"(?:^|[\r\n\s]){re.escape(slash_cmd)}\s*[:=]?\s*off\b", re.IGNORECASE)
    cleaned_text = text
    is_off = False
    matched_cmd = False

    off_match = off_pattern.search(cleaned_text)
    if off_match:
        is_off = True
        matched_cmd = True
        cleaned_text = remove_command_span(cleaned_text, off_match.start(), off_match.end())
        return True, [], is_off, cleaned_text

    agents: list[str] = []

    while True:
        cmd_match = re.search(rf"(?:^|[\r\n\s]){re.escape(slash_cmd)}(?=[\s:=]|$)", cleaned_text, re.IGNORECASE)
        if not cmd_match:
            break

        matched_cmd = True
        start_idx = cmd_match.start()
        curr_idx = cmd_match.end()

        # Consume optional : or = and spaces immediately following slash_cmd
        delim_match = re.match(r"[ \t]*[:=]?[ \t]*", cleaned_text[curr_idx:])
        if delim_match:
            curr_idx += delim_match.end()

        matched_tokens_span_end = curr_idx
        remainder = cleaned_text[curr_idx:]

        offset = 0
        while offset < len(remainder):
            if offset > 0:
                ws_match = re.match(r"[ \t]+", remainder[offset:])
                if not ws_match:
                    break
                ws_len = ws_match.end()
            else:
                ws_len = 0

            t_match = re.match(r"(/?\[[^\]\r\n]+\]|[a-zA-Z0-9_\-]+)", remainder[offset + ws_len :])
            if not t_match:
                break

            token = t_match.group(1)
            clean_token = token.strip("/[]")
            resolved = entity_resolver.resolve_agent_name(clean_token) or entity_resolver.resolve_agent_name(token)
            if resolved:
                if resolved not in agents:
                    agents.append(resolved)
                offset += ws_len + t_match.end()
                matched_tokens_span_end = curr_idx + offset
            elif (token.startswith("[") and token.endswith("]")) or (token.startswith("/[") and token.endswith("]")):
                offset += ws_len + t_match.end()
                matched_tokens_span_end = curr_idx + offset
            else:
                # Word does not resolve to an agent. Stop argument parsing.
                break

        cleaned_text = remove_command_span(cleaned_text, start_idx, matched_tokens_span_end)

    return matched_cmd, agents, is_off, cleaned_text


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
        rf"(?:^|[\r\n\s]){re.escape(slash_cmd)}(?:\s*[:=]\s*(\S+)|\s+\[([^\]]+)\]|\s+\(([^)]+)\)|\s+([\w\-.]+\.[a-zA-Z0-9]+))?",
        re.IGNORECASE,
    )

    match = cmd_pattern.search(text)
    if match:
        target = next((g for g in match.groups() if g is not None), None)
        target_filename = target.strip("[]():,; \t") if target else None
        cleaned = remove_command_span(text, match.start(), match.end())
        return True, target_filename, cleaned

    return False, None, text


def parse_token(slash_cmd: str, text: str) -> tuple[bool, str | None, str]:
    """
    Extracts a single identifier argument or 'all' (e.g. /revoke max_words, /revoke all).
    Returns (matched: bool, target: str | None, cleaned_text: str).
    """
    if not text:
        return False, None, ""

    pattern = re.compile(
        rf"(?:^|[\r\n\s]){re.escape(slash_cmd)}(?![-_])\s*[:=]?\s*(?:/?\[?([a-zA-Z0-9_-]+)\]?|(all)\b)?",
        re.IGNORECASE,
    )
    match = pattern.search(text)
    if match:
        target_candidate = match.group(1) or match.group(2)
        target = target_candidate.strip().lower() if target_candidate else None
        cleaned = remove_command_span(text, match.start(), match.end())
        return True, target, cleaned

    return False, None, text


def parse_single_agent_directive(slash_cmd: str, text: str) -> tuple[bool, str | None, str]:
    """
    Extracts a single agent target for commands like /first [Agent] or /open_debate [Agent].
    Supports /cmd [Agent], /cmd /[Agent], /cmd: [Agent], /cmd: Agent, /cmd Agent.
    Returns (matched: bool, agent: str | None, cleaned_text: str).
    """
    if not text:
        return False, None, ""

    cmd_match = re.search(rf"(?:^|[\r\n\s]){re.escape(slash_cmd)}(?=[\s:=]|$)", text, re.IGNORECASE)
    if not cmd_match:
        return False, None, text

    start_idx = cmd_match.start()
    curr_idx = cmd_match.end()

    delim_match = re.match(r"[ \t]*[:=]?[ \t]*", text[curr_idx:])
    has_explicit_delim = bool(delim_match and (":" in delim_match.group(0) or "=" in delim_match.group(0)))
    if delim_match:
        curr_idx += delim_match.end()

    remainder = text[curr_idx:]
    t_match = re.match(
        r"(/?\[[^\]\r\n]+\]|<@[A-Za-z0-9_]+>|@[a-zA-Z0-9_-]+|[a-zA-Z0-9_\-]+)",
        remainder,
    )

    if not t_match:
        cleaned_text = remove_command_span(text, start_idx, curr_idx)
        return True, None, cleaned_text

    raw_token = t_match.group(1)
    clean_token = raw_token.strip("/[]@<> \t")
    is_bracketed = (
        (raw_token.startswith("[") or raw_token.startswith("/[")) and raw_token.endswith("]")
    ) or raw_token.startswith(("@", "<@"))

    resolved = entity_resolver.resolve_agent_name(clean_token) or entity_resolver.resolve_agent_name(raw_token)

    if resolved or is_bracketed or has_explicit_delim:
        matched_span_end = curr_idx + t_match.end()
        target = resolved or clean_token
        cleaned_text = remove_command_span(text, start_idx, matched_span_end)
        return True, target, cleaned_text

    # The token is an ordinary word not intended as an agent target
    cleaned_text = remove_command_span(text, start_idx, cmd_match.end())
    return True, None, cleaned_text


def parse_grant_skill(slash_cmd: str, text: str) -> tuple[bool, str, list[str], str]:
    """
    Extracts skill name and agent targets for /grant_skill <skill> /[Agent1] /[Agent2].
    Strictly whitespace-separated, deterministic instruction parsing.
    Returns (matched: bool, skill_name: str, agents_list: list[str], cleaned_text: str).
    """
    if not text:
        return False, "", [], ""

    cmd_match = re.search(rf"(?:^|[\r\n\s]){re.escape(slash_cmd)}(?=[\s:=]|$)", text, re.IGNORECASE)
    if not cmd_match:
        return False, "", [], text

    start_idx = cmd_match.start()
    curr_idx = cmd_match.end()

    delim_match = re.match(r"[ \t]*[:=]?[ \t]*", text[curr_idx:])
    if delim_match:
        curr_idx += delim_match.end()

    matched_tokens_span_end = curr_idx
    remainder = text[curr_idx:]

    offset = 0
    tokens: list[str] = []

    while offset < len(remainder):
        if offset > 0:
            ws_match = re.match(r"[ \t]+", remainder[offset:])
            if not ws_match:
                break
            ws_len = ws_match.end()
        else:
            ws_len = 0

        t_match = re.match(
            r"(/?\[[^\]\r\n]+\]|<@[A-Za-z0-9_]+>|@[a-zA-Z0-9_-]+|[a-zA-Z0-9_-]+)",
            remainder[offset + ws_len :],
        )
        if not t_match:
            break

        raw_token = t_match.group(1)
        token_clean = raw_token.strip("/[]@<>\t ")
        is_bracketed = (
            (raw_token.startswith("[") or raw_token.startswith("/["))
            and raw_token.endswith("]")
            or raw_token.startswith(("@", "<@"))
        )

        if not tokens:
            tokens.append(token_clean)
            offset += ws_len + t_match.end()
            matched_tokens_span_end = curr_idx + offset
        else:
            is_agent = bool(entity_resolver.resolve_agent_name(token_clean))
            is_first_agent = len(tokens) == 1 and token_clean.lower() not in ("and", "&", "to", "for", "with", "please")
            if is_bracketed or is_agent or is_first_agent:
                tokens.append(token_clean)
                offset += ws_len + t_match.end()
                matched_tokens_span_end = curr_idx + offset
            else:
                break

    skill_name = tokens[0] if tokens else ""
    agents_list = tokens[1:] if len(tokens) > 1 else []
    cleaned_text = remove_command_span(text, start_idx, matched_tokens_span_end)
    return True, skill_name, agents_list, cleaned_text


def parse_list(slash_cmd: str, text: str, known_items: set[str] | None = None) -> tuple[bool, list[str], str]:
    """
    Extracts a list of items for commands like /grant_skill_for_all [skill1] [skill2] or /revoke_skill_for_all.
    Strictly whitespace-separated, deterministic instruction parsing.
    Returns (matched: bool, items_list: list[str], cleaned_text: str).
    """
    if not text:
        return False, [], ""

    cmd_match = re.search(rf"(?:^|[\r\n\s]){re.escape(slash_cmd)}(?=[\s:=]|$)", text, re.IGNORECASE)
    if not cmd_match:
        return False, [], text

    start_idx = cmd_match.start()
    curr_idx = cmd_match.end()

    delim_match = re.match(r"[ \t]*[:=]?[ \t]*", text[curr_idx:])
    if delim_match:
        curr_idx += delim_match.end()

    matched_tokens_span_end = curr_idx
    remainder = text[curr_idx:]

    offset = 0
    items_list: list[str] = []

    while offset < len(remainder):
        if offset > 0:
            ws_match = re.match(r"[ \t]+", remainder[offset:])
            if not ws_match:
                break
            ws_len = ws_match.end()
        else:
            ws_len = 0

        t_match = re.match(
            r"(/?\[[^\]\r\n]+\]|[a-zA-Z0-9_-]+)",
            remainder[offset + ws_len :],
        )
        if not t_match:
            break

        raw_token = t_match.group(1)
        token_clean = raw_token.strip("/[]")
        rem_after_token = remainder[offset + ws_len + t_match.end() :].strip()
        is_known = known_items and (token_clean.lower() in known_items or token_clean.lower() == "all")
        is_last_token = not rem_after_token

        if is_known or (offset == 0 and is_last_token):
            items_list.append(token_clean)
            offset += ws_len + t_match.end()
            matched_tokens_span_end = curr_idx + offset
        else:
            break

    cleaned_text = remove_command_span(text, start_idx, matched_tokens_span_end)
    return True, items_list, cleaned_text


def strip_all_slash_commands(text: str) -> str:
    """
    Dynamically strips all registered slash commands and their arguments
    from raw text using triggers from the skill registry while preserving
    prompt indentation and formatting.
    """
    if not text:
        return ""

    from core.registry import get_skill_registry

    skill_registry = get_skill_registry()

    cleaned = text
    # Sort triggers by length descending to match longer commands first
    for trigger, (skill_def, trg_cfg) in sorted(
        skill_registry.get_slash_triggers().items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        arg_type = trg_cfg.get("arg_type")
        if arg_type in ("int", "int_or_off", "integer"):
            _, _, cleaned = parse_int_or_off(trigger, cleaned)
        elif arg_type == "grant_skill" or skill_def.name in ("grant_skill", "revoke_skill"):
            _, _, _, cleaned = parse_grant_skill(trigger, cleaned)
        elif arg_type == "agent_list":
            _, _, _, cleaned = parse_agent_list(trigger, cleaned)
        elif arg_type in ("agent_token", "agent"):
            _, _, cleaned = parse_single_agent_directive(trigger, cleaned)
        elif arg_type in ("file_target", "file"):
            _, _, cleaned = parse_file_target(trigger, cleaned)
        elif arg_type in ("terminal_flag", "flag", "none", None) or skill_def.is_terminal:
            _, cleaned = parse_terminal_flag(trigger, cleaned)
        elif arg_type in ("token", "str"):
            _, _, cleaned = parse_token(trigger, cleaned)
        elif arg_type == "list":
            _, _, cleaned = parse_list(trigger, cleaned)
        else:
            pattern = re.compile(
                rf"(?:^|[\r\n\s]){re.escape(trigger)}(?=[\s:=]|$)(?:\s*[:=]?\s*[^\s/]+)?",
                re.IGNORECASE,
            )
            m = pattern.search(cleaned)
            if m:
                cleaned = remove_command_span(cleaned, m.start(), m.end())

    return cleaned
