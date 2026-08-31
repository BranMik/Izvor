import logging
import re
from typing import Any

from agent_settings import ENABLED_DEBATE_AGENTS
from config import AGENTS_CONFIG, DEFAULT_AGENT_KEY
from nl_patterns import extract_history_limit
from thread_state import REMAINING_AGENTS_MAP

logger = logging.getLogger(__name__)


def resolve_target_file(
    available_files: list[dict[str, Any]], target_filename: str | None = None
) -> dict[str, Any] | None:
    """
    Selects the matching file from available thread files.
    If target_filename is specified, matches exact name or substring (case-insensitive).
    Otherwise returns the latest uploaded file in the thread.
    """
    if not available_files:
        return None
    if target_filename:
        # Exact match (case-insensitive)
        for f in reversed(available_files):
            if f.get("name", "").lower() == target_filename.lower():
                return f
        # Partial match
        for f in reversed(available_files):
            if target_filename.lower() in f.get("name", "").lower():
                return f
    # Default to latest uploaded file
    return available_files[-1]


def get_initial_agents() -> list[str]:
    """
    Returns the list of enabled optional agents that should speak in the discussion flow.
    Locked agents like AnthrOpus_Sceptic are always included.
    """
    return [
        key
        for key in AGENTS_CONFIG
        if key in ENABLED_DEBATE_AGENTS
        and key not in ("OpAiTerra_Architect", "Gemini_Visionary")
    ]


def extract_tagged_agents(text: str) -> list[str]:
    """
    Extracts all valid agent keys tagged in [Agent_Key] format in the text,
    ordered by their appearance in the text.
    """
    matches = []
    # Clean text to remove bot mentions
    cleaned = re.sub(r"<@[A-Z0-9]+>", "", text)

    # Bracketed tags [Agent_Key]
    for match in re.finditer(r"\[([a-zA-Z0-9_]+)\]", cleaned):
        tag = match.group(1).lower()
        for key in AGENTS_CONFIG:
            if tag == key.lower():
                matches.append((match.start(), key))
                break

    # Sort matches by position in text to preserve order
    matches.sort(key=lambda x: x[0])

    # Deduplicate while preserving order
    unique_keys = []
    for pos, key in matches:
        if key not in unique_keys:
            unique_keys.append(key)

    return unique_keys


def parse_agent_and_prompt(
    text: str, channel_id: str, thread_ts: str
) -> tuple[str, str, bool]:
    """
    Parses Slack message text for bracketed agent tags: [Agent_Key].
    Supported patterns:
    - [Gemini_Visionary] prompt...
    - @CaiLogics Hub [Mistral_TechLead] prompt...
    - Plain text / App mention -> defaults to Gemini_Visionary (full panel flow)
    Note: If no bracketed agent tag is specified, the default flow starts with Gemini_Visionary,
    goes through enabled panel agents, and finishes with OpAiTerra_Architect.
    Returns (agent_key, cleaned_prompt, was_explicit_target)
    """

    thread_key = (channel_id, thread_ts)
    if thread_key not in REMAINING_AGENTS_MAP:
        logger.info(
            f"[ORCHESTRATOR] INITIALIZING new remaining agents list for thread_key: {thread_key}"
        )
        REMAINING_AGENTS_MAP[thread_key] = get_initial_agents()

    remaining = REMAINING_AGENTS_MAP[thread_key]
    logger.info(
        f"[ORCHESTRATOR] Current remaining agents for thread_key {thread_key}: {remaining}"
    )
    _, cleaned_text = extract_history_limit(text)
    cleaned_text = re.sub(r"<@[A-Z0-9]+>", "", cleaned_text).strip()

    # Search for explicit bracketed tags like [Gemini_Visionary] or [Mistral_TechLead]
    tag_matches = []
    for match in re.finditer(r"\[([a-zA-Z0-9_]+)\]", cleaned_text):
        tag = match.group(1)
        for key in AGENTS_CONFIG:
            if tag.lower() == key.lower():
                tag_matches.append((match, key, tag))
                break

    if tag_matches:
        # Prioritize matching an agent that is currently in remaining
        matched_item = None
        for match, key, tag in tag_matches:
            if key in remaining:
                matched_item = (match, key, tag)
                break

        # If none matched remaining (e.g. OpAiTerra_Architect or an agent already spoken):
        if not matched_item:
            for match, key, tag in tag_matches:
                if key == "OpAiTerra_Architect" and not remaining:
                    matched_item = (match, key, tag)
                    break
            if not matched_item:
                matched_item = tag_matches[0]

        match, matched_key, tag = matched_item
        prompt = re.sub(r"\[" + re.escape(tag) + r"\]", "", cleaned_text).strip()
        if matched_key in remaining:
            remaining.remove(matched_key)
        return matched_key, prompt, True

    # Default fallback agent if no bracketed tag matched
    if DEFAULT_AGENT_KEY in remaining:
        remaining.remove(DEFAULT_AGENT_KEY)
    return DEFAULT_AGENT_KEY, cleaned_text, False


def enforce_valid_nomination(
    text: str,
    current_agent_key: str,
    remaining: list[str],
    force_termination: bool,
    pending_retries: list[str] | None = None,
) -> str:
    """
    Ensures that the agent's response nominates a valid agent:
    1. If force_termination or current agent is OpAiTerra_Architect, no nomination is required.
    2. If remaining agents exist:
       - Preserves the agent's nomination if it chose an agent that is still in `remaining`.
       - If the agent prematurely nominated OpAiTerra_Architect or an agent that already spoke,
         programmatically replaces the nomination tag with the first remaining agent (remaining[0]).
       - If no nomination tag is present, appends [remaining[0]].
    3. If remaining list is empty:
       - If pending rate-limit retries exist, nominates the pending retried agent.
       - If no pending retries and current agent is not OpAiTerra_Architect, nominates [OpAiTerra_Architect].
    """
    if force_termination or current_agent_key == "OpAiTerra_Architect":
        # Strip any bracketed agent tags so Slack webhook won't trigger unexpected bot loops
        for key in AGENTS_CONFIG:
            text = re.sub(
                r"\[" + re.escape(key) + r"\]", key, text, flags=re.IGNORECASE
            )
        return text

    # Find all bracketed agent tags matching known AGENTS_CONFIG keys
    agent_tag_matches = []
    for match in re.finditer(r"\[([a-zA-Z0-9_]+)\]", text):
        tag_name = match.group(1)
        for key in AGENTS_CONFIG:
            if tag_name.lower() == key.lower():
                agent_tag_matches.append((match, key))
                break

    if remaining:
        # Check if any detected tag is actually in remaining
        valid_nomination = None
        for match, key in agent_tag_matches:
            if key in remaining:
                valid_nomination = key
                break

        if valid_nomination:
            logger.info(
                f"[ORCHESTRATOR] Agent {current_agent_key} chose valid remaining agent [{valid_nomination}]."
            )
            return text

        fallback_agent = remaining[0]
        if agent_tag_matches:
            last_match, last_key = agent_tag_matches[-1]
            logger.warning(
                f"[ORCHESTRATOR] Agent {current_agent_key} nominated '{last_key}', but {len(remaining)} agent(s) have not spoken yet: {remaining}. "
                f"Programmatically correcting nomination tag to [{fallback_agent}]."
            )
            start, end = last_match.span()
            return text[:start] + f"[{fallback_agent}]" + text[end:]
        else:
            logger.warning(
                f"[ORCHESTRATOR] Agent {current_agent_key} omitted nomination tag while {len(remaining)} agent(s) remain: {remaining}. "
                f"Programmatically appending [{fallback_agent}]."
            )
            return f"{text.strip()}\n\n[{fallback_agent}]"
    else:
        # remaining list is empty -> check pending_retries
        retries_list = pending_retries or []
        if retries_list:
            valid_retry = None
            for match, key in agent_tag_matches:
                if key in retries_list:
                    valid_retry = key
                    break
            if valid_retry:
                logger.info(
                    f"[ORCHESTRATOR] Agent {current_agent_key} nominated valid rate-limit retry agent [{valid_retry}]."
                )
                return text

            fallback_retry = retries_list[0]
            if agent_tag_matches:
                last_match, last_key = agent_tag_matches[-1]
                logger.info(
                    f"[ORCHESTRATOR] Correcting nomination to pending rate-limit retry agent [{fallback_retry}]."
                )
                start, end = last_match.span()
                return text[:start] + f"[{fallback_retry}]" + text[end:]
            else:
                logger.info(
                    f"[ORCHESTRATOR] Appending pending rate-limit retry agent [{fallback_retry}]."
                )
                return f"{text.strip()}\n\n[{fallback_retry}]"
        else:
            # remaining list and retries are empty -> next must be OpAiTerra_Architect
            if agent_tag_matches:
                last_match, last_key = agent_tag_matches[-1]
                if last_key == "OpAiTerra_Architect":
                    return text
                logger.info(
                    f"[ORCHESTRATOR] All intermediate & retried agents have spoken. Correcting tag '[{last_key}]' to [OpAiTerra_Architect]."
                )
                start, end = last_match.span()
                return text[:start] + "[OpAiTerra_Architect]" + text[end:]
            else:
                logger.info(
                    f"[ORCHESTRATOR] All intermediate & retried agents have spoken. Appending [OpAiTerra_Architect] for final summary."
                )
                return f"{text.strip()}\n\n[OpAiTerra_Architect]"
