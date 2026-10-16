"""
Nomination and prompt resolution module for Izvor.

Handles speaker turn-taking, bracketed agent tag parsing, target file resolution,
and valid nomination enforcement without unnecessary class wrapper indirection.
"""

from __future__ import annotations

import logging
import random
import re
from typing import Any

from core.config import AGENTS_CONFIG
from core.models import OrchestrationMode
from pipeline.directive_parser.slash_parser import strip_all_slash_commands
from pipeline.entity_resolver import (
    NOMINATION_TAG_RE,
    NOMINATION_TAG_REGEX,
    TAG_REGEX,
    entity_resolver,
    format_nomination,
    format_nomination_tag,
)
from pipeline.orchestrator import SandwichOrchestrator

from .agent_settings import settings_manager
from .thread_state import thread_state_manager

logger = logging.getLogger(__name__)


def resolve_target_file(
    available_files: list[dict[str, Any]],
    target_filename: str | None = None,
) -> dict[str, Any] | None:
    """
    Selects matching file from available thread files or falls back to the agents_resources/ directory.
    Strict precedence: Slack thread attachments in available_files take precedence over files in agents_resources/.
    Matches exact name or substring (case-insensitive), or defaults to latest file in available_files.
    """
    if available_files:
        if target_filename:
            # Exact match in Slack thread attachments
            for f in reversed(available_files):
                if f.get("name", "").lower() == target_filename.lower():
                    return f
            # Partial match in Slack thread attachments
            for f in reversed(available_files):
                if target_filename.lower() in f.get("name", "").lower():
                    return f
        else:
            # Default to latest uploaded file in thread
            return available_files[-1]

    # Fallback to local default agents_resources/ directory if target filename is requested
    if target_filename:
        try:
            from services.mcp_tools import AGENTS_RESOURCES_DIR, find_resource_file

            matched_path = find_resource_file(target_filename)
            if matched_path and matched_path.exists() and matched_path.is_file():
                return {
                    "id": f"resource_{matched_path.name}",
                    "name": matched_path.name,
                    "size": matched_path.stat().st_size,
                    "is_resource_file": True,
                    "rel_path": str(matched_path.relative_to(AGENTS_RESOURCES_DIR)).replace("\\", "/"),
                }
        except (OSError, ValueError, ImportError, AttributeError) as e:
            logger.debug(f"[NOMINATION] Error resolving target file from agents_resources: {e}")

    return None


def get_initial_agents() -> list[str]:
    """
    Returns list of agents that should participate in the discussion flow based on active orchestration mode.
    """
    mode = settings_manager.get_orchestration_mode()
    agents = settings_manager.agents_config or AGENTS_CONFIG
    enabled_set = settings_manager.get_enabled_agents()
    enabled = [k for k in agents if k in enabled_set]

    sandwich_cfg = settings_manager.get_sandwich_config()
    fixed_cfg = settings_manager.get_fixed_config()
    orchestrator = SandwichOrchestrator(
        mode=mode,
        start_chain=sandwich_cfg.get("start_chain", []),
        end_chain=sandwich_cfg.get("end_chain", []),
        fixed_chain=fixed_cfg.get("chain", []),
        agents_map=agents,
    )
    return orchestrator.build_initial_sequence(enabled)


def parse_agent_and_prompt(
    text: str, channel_id: str, thread_ts: str
) -> tuple[str | None, str, bool]:
    """
    Parses Slack message text for bracketed agent tags: [Agent_Key].
    Returns (agent_key, cleaned_prompt, was_explicit_target)
    """
    thread_key = (channel_id, thread_ts)
    if not thread_state_manager.has_remaining_agents_key(channel_id, thread_ts):
        stored_seq = thread_state_manager.get_round_sequence(channel_id, thread_ts)
        init_seq = list(stored_seq) if stored_seq else get_initial_agents()
        logger.info(
            f"[ORCHESTRATOR] INITIALIZING new remaining agents list for thread_key: {thread_key}: {init_seq}"
        )
        thread_state_manager.set_remaining_agents(channel_id, thread_ts, init_seq)

    remaining = thread_state_manager.get_remaining_agents(channel_id, thread_ts)
    logger.info(
        f"[ORCHESTRATOR] Current remaining agents for thread_key {thread_key}: {remaining}"
    )
    cleaned_text = strip_all_slash_commands(text)
    cleaned_text = re.sub(r"<@[A-Z0-9]+>", "", cleaned_text).strip()

    tag_matches = []
    for match in TAG_REGEX.finditer(cleaned_text):
        raw_tag = match.group(1)
        resolved_key = entity_resolver.resolve_agent_name(raw_tag)
        if resolved_key:
            tag_matches.append((match, resolved_key, raw_tag))

    if tag_matches:
        matched_item = None
        for match, key, raw_tag in tag_matches:
            if key in remaining:
                matched_item = (match, key, raw_tag)
                break

        if not matched_item:
            matched_item = tag_matches[0]

        match, matched_key, raw_tag = matched_item
        # Strip all matched agent tags from prompt so that multi-tag prompts like
        # "/[Security Architect] /[agent-10003] /[agent-10002] Pa sad ostatak prompta."
        # leave cleanly "Pa sad ostatak prompta."
        prompt = cleaned_text
        for m, _, _ in tag_matches:
            prompt = re.sub(re.escape(m.group(0)), "", prompt)
        prompt = prompt.strip()
        prompt = re.sub(r"^\*+|\*+$", "", prompt).strip()
        if matched_key in remaining:
            remaining.remove(matched_key)
            thread_state_manager.set_remaining_agents(channel_id, thread_ts, remaining)
        return matched_key, prompt, True

    if remaining:
        stored_seq = thread_state_manager.get_round_sequence(channel_id, thread_ts)
        if stored_seq and remaining[0] in stored_seq:
            first_agent = remaining.pop(0)
            thread_state_manager.set_remaining_agents(channel_id, thread_ts, remaining)
            return first_agent, cleaned_text, False

        mode = settings_manager.get_orchestration_mode()
        if mode == OrchestrationMode.FIXED:
            fixed_cfg = settings_manager.get_fixed_config()
            if fixed_cfg.get("chain"):
                first_agent = remaining.pop(0)
                thread_state_manager.set_remaining_agents(channel_id, thread_ts, remaining)
                return first_agent, cleaned_text, False
        elif mode == OrchestrationMode.SANDWICH:
            first_agent = remaining.pop(0)
            thread_state_manager.set_remaining_agents(channel_id, thread_ts, remaining)
            return first_agent, cleaned_text, False

        # If no designated chain, select randomly from remaining pool
        first_agent = random.choice(remaining)
        remaining.remove(first_agent)
        thread_state_manager.set_remaining_agents(channel_id, thread_ts, remaining)
        return first_agent, cleaned_text, False

    return None, cleaned_text, False


LEAD_IN_NOMINATION_PATTERN = (
    r"(?:"
    r"(?:next(?:\s+(?:agent|speaker|turn|persona|up))?\s*(?:is|:|-)?\s*)"
    r"|(?:(?:nomination|nominating|nominate)\s*(?::|-)?\s*)"
    r"|(?:over to(?:\s+you)?\s*,?\s*)"
    r"|(?:(?:handing|passing|turning)(?:\s+over)?\s+to(?:\s+you)?\s*,?\s*)"
    r"|(?:i(?:\s+will|'ll)?\s+nominate\s*(?::|-)?\s*)"
    r"|(?:(?:sljedeći|sljedeci|naredni)(?:\s+(?:agent|govornik))?\s*(?:je|:|-)?\s*)"
    r"|(?:(?:nominacija|nominiram)\s*(?::|-)?\s*)"
    r"|(?:predajem\s+rije[cč](?:\s+za)?\s*,?\s*)"
    r")"
)


def strip_all_nominations(text: str) -> str:
    """
    Completely removes agent nomination tags (including the nominated agent's name
    and any nomination lead-in phrases such as 'Next:', 'Nomination:', 'Over to:', etc.)
    when no agent should be nominated (e.g. force termination or end of panel discussion).
    Any other in-text bracketed agent mentions have their brackets stripped to plain text.
    """
    if not text:
        return text

    agents = settings_manager.agents_config or AGENTS_CONFIG
    agent_tokens: set[str] = set(agents.keys())
    for cfg in agents.values():
        if isinstance(cfg, dict):
            uname = cfg.get("username")
            if uname:
                agent_tokens.add(uname)

    for match in TAG_REGEX.finditer(text):
        raw_tag = match.group(1).strip()
        resolved_key = entity_resolver.resolve_agent_name(raw_tag)
        if resolved_key:
            agent_tokens.add(raw_tag)
            agent_tokens.add(resolved_key)

    if not agent_tokens:
        return text.strip()

    clean = text.rstrip()
    agent_pattern = r"(?:" + "|".join(re.escape(tok) for tok in sorted(agent_tokens, key=len, reverse=True)) + r")"
    tag_bracket_pattern = r"/\[\s*" + agent_pattern + r"\s*\]"

    # 1. Lead-in phrase followed by nomination tag at the end or on a newline/sentence boundary
    lead_in_regex = re.compile(
        r"(?:(?<=[.!?;\n])\s*|\n+|^)\s*"
        + LEAD_IN_NOMINATION_PATTERN
        + tag_bracket_pattern
        + r"(?:\s+next)?(?:\s+to speak)?(?:\s+for (?:your|the) (?:thoughts|review|input))?\s*[.!?]?\s*$",
        re.IGNORECASE | re.DOTALL,
    )

    lead_in_inline_regex = re.compile(
        r"(?:(?<=[.!?;\n])\s*|\n+|^)\s*"
        + LEAD_IN_NOMINATION_PATTERN
        + tag_bracket_pattern
        + r"(?:\s+next)?(?:\s+to speak)?(?:\s+for (?:your|the) (?:thoughts|review|input))?\s*[.!?]?\s*",
        re.IGNORECASE,
    )

    # 2. Standalone trailing nomination tag at end of text (e.g. "... /[agent-10003]", "...\n\n/[agent-10003]")
    trailing_tag_regex = re.compile(
        r"(?:\n+|\s+)\s*" + tag_bracket_pattern + r"\s*(?:next)?\s*[.!?]?\s*$",
        re.IGNORECASE,
    )

    prev = None
    while prev != clean:
        prev = clean
        clean = lead_in_regex.sub("", clean).rstrip()
        clean = trailing_tag_regex.sub("", clean).rstrip()
        clean = lead_in_inline_regex.sub("", clean).rstrip()

    # 3. For any remaining in-text slash bracketed agent mentions (e.g., "/[Security Architect]"),
    # strip the slash so it doesn't trigger Slack nomination parsers.
    for tok in sorted(agent_tokens, key=len, reverse=True):
        clean = re.sub(
            r"/\[\s*" + re.escape(tok) + r"\s*\]",
            tok,
            clean,
            flags=re.IGNORECASE,
        )

    return clean.strip()


def enforce_valid_nomination(
    text: str,
    current_agent_key: str,
    remaining: list[str],
    force_termination: bool,
    pending_retries: list[str] | None = None,
    designated_next_agent: str | None = None,
) -> str:
    """Ensures that the agent's response nominates a valid agent according to panel flow rules."""
    if force_termination:
        return strip_all_nominations(text)

    agent_tag_matches = []
    for match in TAG_REGEX.finditer(text):
        raw_tag = match.group(1)
        resolved_key = entity_resolver.resolve_agent_name(raw_tag)
        if resolved_key:
            agent_tag_matches.append((match, resolved_key))

    if designated_next_agent:
        if agent_tag_matches:
            last_match, last_key = agent_tag_matches[-1]
            if last_key == designated_next_agent:
                return text
            start, end = last_match.span()
            return text[:start] + format_nomination(designated_next_agent) + text[end:]
        else:
            return f"{text.strip()}\n\n{format_nomination(designated_next_agent)}"

    if remaining:
        valid_nomination = None
        for match, key in reversed(agent_tag_matches):
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
                f"[ORCHESTRATOR] Agent {current_agent_key} nominated '{last_key}', but {len(remaining)} agent(s) remain: {remaining}. "
                f"Correcting nomination tag to [{fallback_agent}]."
            )
            start, end = last_match.span()
            return text[:start] + format_nomination(fallback_agent) + text[end:]
        else:
            logger.warning(
                f"[ORCHESTRATOR] Agent {current_agent_key} omitted nomination tag while {len(remaining)} agent(s) remain: {remaining}. "
                f"Appending [{fallback_agent}]."
            )
            return f"{text.strip()}\n\n{format_nomination(fallback_agent)}"
    else:
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
                return text[:start] + format_nomination(fallback_retry) + text[end:]
            else:
                logger.info(
                    f"[ORCHESTRATOR] Appending pending rate-limit retry agent [{fallback_retry}]."
                )
                return f"{text.strip()}\n\n{format_nomination(fallback_retry)}"
        else:
            return strip_all_nominations(text)
