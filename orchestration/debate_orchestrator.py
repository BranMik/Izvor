import logging
import re
from typing import Any

from core.config import AGENTS_CONFIG
from nlp.nl_patterns import extract_history_limit, resolve_agent_candidates, resolve_agent_name
from .agent_settings import ENABLED_DEBATE_AGENTS, settings_manager
from .thread_state import REMAINING_AGENTS_MAP, thread_state_manager

logger = logging.getLogger(__name__)


TAG_REGEX = re.compile(r"[\[{]\s*([^\]}\r\n]+?)\s*[\]}]")


class DebateOrchestrator:
    """Orchestrates panel speaker turn-taking, bracketed agent tag parsing, and nomination handoffs."""

    def __init__(self):
        pass

    def resolve_target_file(
        self,
        available_files: list[dict[str, Any]],
        target_filename: str | None = None,
    ) -> dict[str, Any] | None:
        """
        Selects matching file from available thread files.
        Matches exact name or substring (case-insensitive), or defaults to latest file.
        """
        if not available_files:
            return None
        if target_filename:
            # Exact match
            for f in reversed(available_files):
                if f.get("name", "").lower() == target_filename.lower():
                    return f
            # Partial match
            for f in reversed(available_files):
                if target_filename.lower() in f.get("name", "").lower():
                    return f
        # Default to latest uploaded file
        return available_files[-1]

    def get_initial_agents(self) -> list[str]:
        """
        Returns list of agents that should participate in the discussion flow based on active orchestration mode.
        - Fixed mode: returns fixed_config['chain'] if non-empty, else all enabled agents.
        - Sandwich mode: returns start_chain + middle_pool (enabled minus start/end) + end_chain.
          If both start and end chains are empty, returns all enabled agents.
        """
        mode = settings_manager.get_orchestration_mode()
        enabled = list(settings_manager.get_enabled_agents())
        agents = settings_manager.agents_config or AGENTS_CONFIG

        if mode == "fixed":
            fixed_cfg = settings_manager.get_fixed_config()
            chain = [k for k in fixed_cfg.get("chain", []) if k in agents]
            if chain:
                return list(chain)
            return [k for k in agents if k in enabled]

        # Sandwich mode
        sandwich_cfg = settings_manager.get_sandwich_config()
        start_chain = [k for k in sandwich_cfg.get("start_chain", []) if k in agents]
        end_chain = [k for k in sandwich_cfg.get("end_chain", []) if k in agents]

        # Middle pool: enabled agents not in start_chain and not in end_chain
        middle_pool = [
            k for k in agents
            if k in enabled and k not in start_chain and k not in end_chain
        ]

        full_flow = start_chain + middle_pool + end_chain
        if not full_flow:
            return [k for k in agents if k in enabled]
        return full_flow

    def detect_ambiguous_tags(self, text: str) -> list[tuple[str, list[str]]]:
        """
        Scans message text for bracketed tokens [tag] that match multiple candidate agents.
        Returns a list of tuples: [(raw_tag_str, [matching_candidate_keys])]
        """
        cleaned = re.sub(r"<@[A-Z0-9]+>", "", text)
        cleaned = re.sub(r"\[([^\]]+)\]\([^\)]+\)", "", cleaned)
        ambiguous = []
        for match in TAG_REGEX.finditer(cleaned):
            tag_str = match.group(1).strip()
            if (
                not tag_str
                or re.fullmatch(r"\d+(?:[/\-:]\d+)?", tag_str)
                or re.search(r"[:;]\s*\d+\s*/\s*\d+", tag_str)
            ):
                continue
            candidates = resolve_agent_candidates(tag_str)
            if len(candidates) > 1:
                ambiguous.append((tag_str, candidates))
        return ambiguous

    def detect_unrecognized_tags(self, text: str) -> list[str]:
        """
        Scans message text for bracketed tokens [tag] that do NOT match any active agent.
        Returns a list of unmatched raw tag strings: [unmatched_tag_str1, ...]
        """
        cleaned = re.sub(r"<@[A-Z0-9]+>", "", text)
        cleaned = re.sub(r"\[([^\]]+)\]\([^\)]+\)", "", cleaned)
        unrecognized = []
        for match in TAG_REGEX.finditer(cleaned):
            tag_str = match.group(1).strip()
            if (
                not tag_str
                or re.fullmatch(r"\d+(?:[/\-:]\d+)?", tag_str)
                or re.search(r"[:;]\s*\d+\s*/\s*\d+", tag_str)
            ):
                continue
            candidates = resolve_agent_candidates(tag_str)
            if len(candidates) == 0:
                unrecognized.append(tag_str)
        return unrecognized

    def extract_tagged_agents(self, text: str) -> list[str]:
        """
        Extracts all valid unambiguous agent keys tagged in [Agent_Key] format in the text,
        ordered by their appearance in the text. Supports partial and role-based tags.
        """
        matches = []
        cleaned = re.sub(r"<@[A-Z0-9]+>", "", text)

        for match in TAG_REGEX.finditer(cleaned):
            resolved_key = resolve_agent_name(match.group(1))
            if resolved_key:
                matches.append((match.start(), resolved_key))

        matches.sort(key=lambda x: x[0])

        unique_keys = []
        for pos, key in matches:
            if key not in unique_keys:
                unique_keys.append(key)

        return unique_keys

    def parse_agent_and_prompt(
        self, text: str, channel_id: str, thread_ts: str
    ) -> tuple[str | None, str, bool]:
        """
        Parses Slack message text for bracketed agent tags: [Agent_Key].
        Returns (agent_key, cleaned_prompt, was_explicit_target)
        """
        thread_key = (channel_id, thread_ts)
        if thread_key not in thread_state_manager.remaining_agents_map:
            logger.info(
                f"[ORCHESTRATOR] INITIALIZING new remaining agents list for thread_key: {thread_key}"
            )
            thread_state_manager.remaining_agents_map[thread_key] = (
                self.get_initial_agents()
            )

        remaining = thread_state_manager.remaining_agents_map[thread_key]
        logger.info(
            f"[ORCHESTRATOR] Current remaining agents for thread_key {thread_key}: {remaining}"
        )
        _, cleaned_text = extract_history_limit(text)
        cleaned_text = re.sub(r"<@[A-Z0-9]+>", "", cleaned_text).strip()

        tag_matches = []
        for match in TAG_REGEX.finditer(cleaned_text):
            raw_tag = match.group(1)
            resolved_key = resolve_agent_name(raw_tag)
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
            prompt = re.sub(re.escape(match.group(0)), "", cleaned_text).strip()
            prompt = re.sub(r"^\*+|\*+$", "", prompt).strip()
            if matched_key in remaining:
                remaining.remove(matched_key)
            return matched_key, prompt, True

        if remaining:
            mode = settings_manager.get_orchestration_mode()
            if mode == "fixed":
                fixed_cfg = settings_manager.get_fixed_config()
                if fixed_cfg.get("chain"):
                    first_agent = remaining.pop(0)
                    return first_agent, cleaned_text, False
            elif mode == "sandwich":
                sandwich_cfg = settings_manager.get_sandwich_config()
                if sandwich_cfg.get("start_chain"):
                    first_agent = remaining.pop(0)
                    return first_agent, cleaned_text, False

            # If no designated chain, select randomly from remaining pool
            import random
            first_agent = random.choice(remaining)
            remaining.remove(first_agent)
            return first_agent, cleaned_text, False

        # If remaining is empty, check if sandwich end_chain has agents
        mode = settings_manager.get_orchestration_mode()
        if mode == "sandwich":
            end_chain = settings_manager.get_sandwich_config().get("end_chain", [])
            agents = settings_manager.agents_config or AGENTS_CONFIG
            valid_end = [k for k in end_chain if k in agents]
            if valid_end:
                return valid_end[0], cleaned_text, False

        return None, cleaned_text, False

    def enforce_valid_nomination(
        self,
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
            resolved_key = resolve_agent_name(raw_tag)
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


# Default singleton instance
debate_orchestrator = DebateOrchestrator()


# Backward-compatible function aliases
def resolve_target_file(
    available_files: list[dict[str, Any]], target_filename: str | None = None
) -> dict[str, Any] | None:
    return debate_orchestrator.resolve_target_file(available_files, target_filename)


def get_initial_agents() -> list[str]:
    return debate_orchestrator.get_initial_agents()


def detect_ambiguous_tags(text: str) -> list[tuple[str, list[str]]]:
    return debate_orchestrator.detect_ambiguous_tags(text)


def detect_unrecognized_tags(text: str) -> list[str]:
    return debate_orchestrator.detect_unrecognized_tags(text)


def extract_tagged_agents(text: str) -> list[str]:
    return debate_orchestrator.extract_tagged_agents(text)


def parse_agent_and_prompt(
    text: str, channel_id: str, thread_ts: str
) -> tuple[str, str, bool]:
    return debate_orchestrator.parse_agent_and_prompt(text, channel_id, thread_ts)


def enforce_valid_nomination(
    text: str,
    current_agent_key: str,
    remaining: list[str],
    force_termination: bool,
    pending_retries: list[str] | None = None,
    designated_next_agent: str | None = None,
) -> str:
    return debate_orchestrator.enforce_valid_nomination(
        text, current_agent_key, remaining, force_termination, pending_retries, designated_next_agent
    )

