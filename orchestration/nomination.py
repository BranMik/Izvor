"""
Nomination and prompt resolution module for SlackAgenticPanel.

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
from nlp.nl_patterns import extract_history_limit
from pipeline.entity_resolver import entity_resolver
from pipeline.orchestrator import SandwichOrchestrator

from .agent_settings import settings_manager
from .thread_state import thread_state_manager

logger = logging.getLogger(__name__)

TAG_REGEX = re.compile(r"[\[{]\s*([^\]}\r\n]+?)\s*[\]}]")


def resolve_target_file(
    available_files: list[dict[str, Any]],
    target_filename: str | None = None,
) -> dict[str, Any] | None:
    """
    Selects matching file from available thread files or falls back to the resources/ directory.
    Strict precedence: Slack thread attachments in available_files take precedence over files in resources/.
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

    # Fallback to local default resources/ directory if target filename is requested
    if target_filename:
        try:
            from services.mcp_tools import RESOURCES_DIR, find_resource_file

            matched_path = find_resource_file(target_filename)
            if matched_path and matched_path.exists() and matched_path.is_file():
                return {
                    "id": f"resource_{matched_path.name}",
                    "name": matched_path.name,
                    "size": matched_path.stat().st_size,
                    "is_resource_file": True,
                    "rel_path": str(matched_path.relative_to(RESOURCES_DIR)).replace("\\", "/"),
                }
        except Exception as e:
            logger.debug(f"[NOMINATION] Error resolving target file from resources: {e}")

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
    if thread_key not in thread_state_manager.remaining_agents_map:
        stored_seq = thread_state_manager.get_round_sequence(channel_id, thread_ts)
        init_seq = list(stored_seq) if stored_seq else get_initial_agents()
        logger.info(
            f"[ORCHESTRATOR] INITIALIZING new remaining agents list for thread_key: {thread_key}: {init_seq}"
        )
        thread_state_manager.remaining_agents_map[thread_key] = init_seq

    remaining = thread_state_manager.remaining_agents_map[thread_key]
    logger.info(
        f"[ORCHESTRATOR] Current remaining agents for thread_key {thread_key}: {remaining}"
    )
    _, cleaned_text = extract_history_limit(text)
    cleaned_text = re.sub(r"<@[A-Z0-9]+>", "", cleaned_text).strip()

    tag_matches = []
    for match in TAG_REGEX.finditer(cleaned_text):
        raw_tag = match.group(1)
        resolved_key = entity_resolver.resolve_agent_name(raw_tag)
        if resolved_key:
            tag_matches.append((match, resolved_key, raw_tag))

    if tag_matches:
        matched_item = None
        for match, key, raw_tag in reversed(tag_matches):
            if key in remaining:
                matched_item = (match, key, raw_tag)
                break

        if not matched_item:
            matched_item = tag_matches[-1]

        match, matched_key, raw_tag = matched_item
        prompt = re.sub(re.escape(match.group(0)), "", cleaned_text).strip()
        prompt = re.sub(r"^\*+|\*+$", "", prompt).strip()
        if matched_key in remaining:
            remaining.remove(matched_key)
        return matched_key, prompt, True

    if remaining:
        stored_seq = thread_state_manager.get_round_sequence(channel_id, thread_ts)
        if stored_seq and remaining[0] in stored_seq:
            first_agent = remaining.pop(0)
            return first_agent, cleaned_text, False

        mode = settings_manager.get_orchestration_mode()
        if mode == OrchestrationMode.FIXED:
            fixed_cfg = settings_manager.get_fixed_config()
            if fixed_cfg.get("chain"):
                first_agent = remaining.pop(0)
                return first_agent, cleaned_text, False
        elif mode == OrchestrationMode.SANDWICH:
            sandwich_cfg = settings_manager.get_sandwich_config()
            if sandwich_cfg.get("start_chain"):
                first_agent = remaining.pop(0)
                return first_agent, cleaned_text, False
            else:
                first_agent = remaining.pop(0)
                return first_agent, cleaned_text, False

        # If no designated chain, select randomly from remaining pool
        first_agent = random.choice(remaining)
        remaining.remove(first_agent)
        return first_agent, cleaned_text, False

    return None, cleaned_text, False


def enforce_valid_nomination(
    text: str,
    current_agent_key: str,
    remaining: list[str],
    force_termination: bool,
    pending_retries: list[str] | None = None,
    designated_next_agent: str | None = None,
) -> str:
    """Ensures that the agent's response nominates a valid agent according to panel flow rules."""
    agents = settings_manager.agents_config or AGENTS_CONFIG
    if force_termination:
        for key in agents:
            text = re.sub(
                r"\[" + re.escape(key) + r"\]", key, text, flags=re.IGNORECASE
            )
        return text.strip()

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
            return text[:start] + f"[{designated_next_agent}]" + text[end:]
        else:
            return f"{text.strip()}\n\n[{designated_next_agent}]"

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
            return text[:start] + f"[{fallback_agent}]" + text[end:]
        else:
            logger.warning(
                f"[ORCHESTRATOR] Agent {current_agent_key} omitted nomination tag while {len(remaining)} agent(s) remain: {remaining}. "
                f"Appending [{fallback_agent}]."
            )
            return f"{text.strip()}\n\n[{fallback_agent}]"
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
                return text[:start] + f"[{fallback_retry}]" + text[end:]
            else:
                logger.info(
                    f"[ORCHESTRATOR] Appending pending rate-limit retry agent [{fallback_retry}]."
                )
                return f"{text.strip()}\n\n[{fallback_retry}]"
        else:
            for key in agents:
                text = re.sub(
                    r"\[" + re.escape(key) + r"\]", key, text, flags=re.IGNORECASE
                )
            return text.strip()
