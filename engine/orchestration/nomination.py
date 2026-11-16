"""
Nomination and prompt resolution module for Izvor.

Handles speaker turn-taking, bracketed agent tag parsing, target file resolution,
and valid nomination enforcement without unnecessary class wrapper indirection.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from core.config import AGENTS_CONFIG
from core.models import OrchestrationMode
from pipeline.directive_parser.slash_parser import strip_all_slash_commands
from pipeline.entity_resolver import (
    NOMINATION_TAG_REGEX,
    entity_resolver,
    format_nomination,
)
from pipeline.orchestrator import SandwichOrchestrator

from .agent_settings import settings_manager
from .thread_state import thread_state_manager

logger = logging.getLogger(__name__)

__all__ = [
    "NOMINATION_TAG_REGEX",
    "enforce_valid_nomination",
    "entity_resolver",
    "format_nomination",
    "get_initial_agents",
    "parse_agent_and_prompt",
    "resolve_target_file",
    "strip_all_nominations",
]


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
            from services.local_tools import _resolve_resource_path, get_agents_resources_dir

            resources_dir = get_agents_resources_dir()
            try:
                target_path = _resolve_resource_path(target_filename)
                if target_path.exists() and target_path.is_file():
                    return {
                        "id": f"resource_{target_path.name}",
                        "name": target_path.name,
                        "size": target_path.stat().st_size,
                        "is_resource_file": True,
                        "rel_path": str(target_path.relative_to(resources_dir)).replace("\\", "/"),
                    }
            except (ValueError, PermissionError):
                pass

            # Also check case-insensitive match in active resources_dir
            if resources_dir.exists():
                target_lower = target_filename.lower()
                for entry in resources_dir.iterdir():
                    if entry.is_file() and entry.name.lower() == target_lower:
                        return {
                            "id": f"resource_{entry.name}",
                            "name": entry.name,
                            "size": entry.stat().st_size,
                            "is_resource_file": True,
                            "rel_path": str(entry.relative_to(resources_dir)).replace("\\", "/"),
                        }
        except (OSError, ValueError, AttributeError) as e:
            logger.debug(f"[NOMINATION] Error resolving target file from agents_resources: {e}")

    return None


def get_initial_agents(channel_id: str | None = None, thread_ts: str | None = None) -> list[str]:
    """
    Returns list of agents that should participate in the discussion flow based on active orchestration mode.
    """
    wf_snap = thread_state_manager.get_workflow_snapshot(channel_id, thread_ts) if channel_id and thread_ts else None
    mode = wf_snap.get("mode") if wf_snap else settings_manager.get_orchestration_mode()
    agents = settings_manager.agents_config or AGENTS_CONFIG
    enabled_set = settings_manager.get_enabled_agents()
    enabled = [k for k in agents if k in enabled_set]

    sandwich_cfg = wf_snap.get("sandwich_config") if wf_snap else settings_manager.get_sandwich_config()
    fixed_cfg = wf_snap.get("fixed_config") if wf_snap else settings_manager.get_fixed_config()
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
        init_seq = list(stored_seq) if stored_seq else get_initial_agents(channel_id, thread_ts)
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
    for match in NOMINATION_TAG_REGEX.finditer(cleaned_text):
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
            thread_state_manager.remove_remaining_agent(channel_id, thread_ts, matched_key)
        return matched_key, prompt, True

    if remaining:
        stored_seq = thread_state_manager.get_round_sequence(channel_id, thread_ts)
        if stored_seq and remaining[0] in stored_seq:
            first_agent = thread_state_manager.pop_remaining_agent(channel_id, thread_ts)
            return first_agent, cleaned_text, False

        wf_snap = thread_state_manager.get_workflow_snapshot(channel_id, thread_ts)
        mode = wf_snap.get("mode") if wf_snap else settings_manager.get_orchestration_mode()
        if mode in (
            OrchestrationMode.FIXED,
            OrchestrationMode.SANDWICH,
            OrchestrationMode.SCATTER_GATHER,
            OrchestrationMode.FIXED.value,
            OrchestrationMode.SANDWICH.value,
            OrchestrationMode.SCATTER_GATHER.value,
        ):
            first_agent = thread_state_manager.pop_remaining_agent(channel_id, thread_ts)
            return first_agent, cleaned_text, False

        # If no designated chain, select randomly from remaining pool
        first_agent = thread_state_manager.pop_random_remaining_agent(channel_id, thread_ts)
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

    for match in NOMINATION_TAG_REGEX.finditer(text):
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


def _strip_all_nominations_except_last_canonical(
    text: str,
    target_agent_key: str,
) -> str:
    """Strips inline/lead-in nomination tags and ensures only one canonical trailing tag remains."""
    cleaned = strip_all_nominations(text)
    if not cleaned:
        return format_nomination(target_agent_key)
    return f"{cleaned}\n\n{format_nomination(target_agent_key)}"


def enforce_valid_nomination(
    text: str,
    current_agent_key: str,
    remaining: list[str],
    force_termination: bool,
    pending_retries: list[str] | None = None,
    designated_next_agent: str | None = None,
    control_plane_nomination: str | None = None,
) -> str:
    """Ensures that the agent's response nominates a valid agent according to panel flow rules."""
    if force_termination:
        return strip_all_nominations(text)

    retries_list = list(pending_retries or [])
    allow_self_retry = current_agent_key in retries_list

    def _is_disallowed_self(candidate: str | None) -> bool:
        return bool(candidate and candidate == current_agent_key and not allow_self_retry)

    # Precedence: Explicit control plane nomination tool call (never allow unintended self-nomination)
    if control_plane_nomination:
        resolved_cp = entity_resolver.resolve_agent_name(control_plane_nomination) or control_plane_nomination
        if _is_disallowed_self(resolved_cp):
            logger.warning(
                f"[CONTROL PLANE] Rejected self-nomination '{resolved_cp}' from agent {current_agent_key}."
            )
            resolved_cp = None
        if resolved_cp:
            target = (
                designated_next_agent
                if designated_next_agent
                else (resolved_cp if resolved_cp in remaining else (remaining[0] if remaining else None))
            )
            if _is_disallowed_self(target):
                target = next((k for k in remaining if k != current_agent_key), None)
            if target:
                logger.info(
                    f"[CONTROL PLANE] Enforcing explicit tool nomination for '{target}' from agent {current_agent_key}"
                )
                return _strip_all_nominations_except_last_canonical(text, target)

    agent_tag_matches = []
    for match in NOMINATION_TAG_REGEX.finditer(text):
        raw_tag = match.group(1)
        resolved_key = entity_resolver.resolve_agent_name(raw_tag)
        if resolved_key:
            agent_tag_matches.append((match, resolved_key))

    if designated_next_agent and not _is_disallowed_self(designated_next_agent):
        if agent_tag_matches:
            last_match, last_key = agent_tag_matches[-1]
            if last_key == designated_next_agent and len(agent_tag_matches) == 1:
                return text
        return _strip_all_nominations_except_last_canonical(text, designated_next_agent)

    if remaining:
        non_self_remaining = [k for k in remaining if k != current_agent_key]
        candidate_pool = non_self_remaining or ([] if not allow_self_retry else list(remaining))

        valid_nomination = None
        for _, key in reversed(agent_tag_matches):
            if key in candidate_pool:
                valid_nomination = key
                break

        if valid_nomination:
            logger.info(
                f"[ORCHESTRATOR] Agent {current_agent_key} chose valid remaining agent [{valid_nomination}]."
            )
            if len(agent_tag_matches) == 1 and agent_tag_matches[-1][1] == valid_nomination:
                return text
            return _strip_all_nominations_except_last_canonical(text, valid_nomination)

        if not candidate_pool:
            return strip_all_nominations(text)

        fallback_agent = candidate_pool[0]
        if agent_tag_matches:
            _, last_key = agent_tag_matches[-1]
            logger.warning(
                f"[ORCHESTRATOR] Agent {current_agent_key} nominated '{last_key}', but {len(candidate_pool)} eligible agent(s) remain: {candidate_pool}. "
                f"Correcting nomination tag to [{fallback_agent}]."
            )
        else:
            logger.warning(
                f"[ORCHESTRATOR] Agent {current_agent_key} omitted nomination tag while {len(candidate_pool)} eligible agent(s) remain: {candidate_pool}. "
                f"Appending [{fallback_agent}]."
            )
        return _strip_all_nominations_except_last_canonical(text, fallback_agent)

    if retries_list:
        valid_retry = None
        for _, key in agent_tag_matches:
            if key in retries_list:
                valid_retry = key
                break
        if valid_retry:
            logger.info(
                f"[ORCHESTRATOR] Agent {current_agent_key} nominated valid rate-limit retry agent [{valid_retry}]."
            )
            if len(agent_tag_matches) == 1 and agent_tag_matches[-1][1] == valid_retry:
                return text
            return _strip_all_nominations_except_last_canonical(text, valid_retry)

        fallback_retry = retries_list[0]
        if agent_tag_matches:
            logger.info(
                f"[ORCHESTRATOR] Correcting nomination to pending rate-limit retry agent [{fallback_retry}]."
            )
        else:
            logger.info(
                f"[ORCHESTRATOR] Appending pending rate-limit retry agent [{fallback_retry}]."
            )
        return _strip_all_nominations_except_last_canonical(text, fallback_retry)

    return strip_all_nominations(text)
