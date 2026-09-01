import logging
import re
from typing import Any

from core.config import AGENTS_CONFIG, DEFAULT_AGENT_KEY
from nlp.nl_patterns import extract_history_limit
from .agent_settings import ENABLED_DEBATE_AGENTS, settings_manager
from .thread_state import REMAINING_AGENTS_MAP, thread_state_manager

logger = logging.getLogger(__name__)


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
        Returns list of enabled optional agents that should speak in the discussion flow.
        Locked agents like AnthrOpus_Sceptic are always included.
        """
        enabled = settings_manager.get_enabled_agents()
        return [
            key
            for key in AGENTS_CONFIG
            if key in enabled
            and key not in ("OpAiTerra_Architect", "Gemini_Visionary")
        ]

    def extract_tagged_agents(self, text: str) -> list[str]:
        """
        Extracts all valid agent keys tagged in [Agent_Key] format in the text,
        ordered by their appearance in the text.
        """
        matches = []
        cleaned = re.sub(r"<@[A-Z0-9]+>", "", text)

        for match in re.finditer(r"\[([a-zA-Z0-9_]+)\]", cleaned):
            tag = match.group(1).lower()
            for key in AGENTS_CONFIG:
                if tag == key.lower():
                    matches.append((match.start(), key))
                    break

        matches.sort(key=lambda x: x[0])

        unique_keys = []
        for pos, key in matches:
            if key not in unique_keys:
                unique_keys.append(key)

        return unique_keys

    def parse_agent_and_prompt(
        self, text: str, channel_id: str, thread_ts: str
    ) -> tuple[str, str, bool]:
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
        for match in re.finditer(r"\[([a-zA-Z0-9_]+)\]", cleaned_text):
            tag = match.group(1)
            for key in AGENTS_CONFIG:
                if tag.lower() == key.lower():
                    tag_matches.append((match, key, tag))
                    break

        if tag_matches:
            matched_item = None
            for match, key, tag in tag_matches:
                if key in remaining:
                    matched_item = (match, key, tag)
                    break

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

        if DEFAULT_AGENT_KEY in remaining:
            remaining.remove(DEFAULT_AGENT_KEY)
        return DEFAULT_AGENT_KEY, cleaned_text, False

    def enforce_valid_nomination(
        self,
        text: str,
        current_agent_key: str,
        remaining: list[str],
        force_termination: bool,
        pending_retries: list[str] | None = None,
    ) -> str:
        """Ensures that the agent's response nominates a valid agent according to panel flow rules."""
        if force_termination or current_agent_key == "OpAiTerra_Architect":
            for key in AGENTS_CONFIG:
                text = re.sub(
                    r"\[" + re.escape(key) + r"\]", key, text, flags=re.IGNORECASE
                )
            return text

        agent_tag_matches = []
        for match in re.finditer(r"\[([a-zA-Z0-9_]+)\]", text):
            tag_name = match.group(1)
            for key in AGENTS_CONFIG:
                if tag_name.lower() == key.lower():
                    agent_tag_matches.append((match, key))
                    break

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
                if agent_tag_matches:
                    last_match, last_key = agent_tag_matches[-1]
                    if last_key == "OpAiTerra_Architect":
                        return text
                    logger.info(
                        f"[ORCHESTRATOR] All agents finished. Correcting tag '[{last_key}]' to [OpAiTerra_Architect]."
                    )
                    start, end = last_match.span()
                    return text[:start] + "[OpAiTerra_Architect]" + text[end:]
                else:
                    logger.info(
                        f"[ORCHESTRATOR] All agents finished. Appending [OpAiTerra_Architect] for final summary."
                    )
                    return f"{text.strip()}\n\n[OpAiTerra_Architect]"


# Default singleton instance
debate_orchestrator = DebateOrchestrator()


# Backward-compatible function aliases
def resolve_target_file(
    available_files: list[dict[str, Any]], target_filename: str | None = None
) -> dict[str, Any] | None:
    return debate_orchestrator.resolve_target_file(available_files, target_filename)


def get_initial_agents() -> list[str]:
    return debate_orchestrator.get_initial_agents()


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
) -> str:
    return debate_orchestrator.enforce_valid_nomination(
        text, current_agent_key, remaining, force_termination, pending_retries
    )
