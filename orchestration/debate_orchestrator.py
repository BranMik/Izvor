"""
Backward-compatibility facade for debate orchestration.

DEPRECATION NOTICE:
- Nomination and turn-taking logic has moved to `orchestration.nomination`.
- Tag detection and entity resolution should be called directly via `pipeline.entity_resolver`.

This module re-exports those capabilities for backward compatibility.
"""

from __future__ import annotations

import logging
from typing import Any
import warnings

from pipeline.entity_resolver import entity_resolver
from .nomination import (
    TAG_REGEX,
    enforce_valid_nomination,
    get_initial_agents,
    parse_agent_and_prompt,
    resolve_target_file,
)

logger = logging.getLogger(__name__)


class DebateOrchestrator:
    """
    Deprecated facade for panel speaker turn-taking and nomination handoffs.
    Use `orchestration.nomination` and `pipeline.entity_resolver` directly.
    """

    def __init__(self):
        pass

    def resolve_target_file(
        self,
        available_files: list[dict[str, Any]],
        target_filename: str | None = None,
    ) -> dict[str, Any] | None:
        return resolve_target_file(available_files, target_filename)

    def get_initial_agents(self) -> list[str]:
        return get_initial_agents()

    def detect_ambiguous_tags(self, text: str) -> list[tuple[str, list[str]]]:
        return entity_resolver.detect_ambiguous_tags(text)

    def detect_unrecognized_tags(self, text: str) -> list[str]:
        return entity_resolver.detect_unrecognized_tags(text)

    def extract_tagged_agents(self, text: str) -> list[str]:
        return entity_resolver.extract_tagged_agents(text)

    def parse_agent_and_prompt(
        self, text: str, channel_id: str, thread_ts: str
    ) -> tuple[str | None, str, bool]:
        return parse_agent_and_prompt(text, channel_id, thread_ts)

    def enforce_valid_nomination(
        self,
        text: str,
        current_agent_key: str,
        remaining: list[str],
        force_termination: bool,
        pending_retries: list[str] | None = None,
        designated_next_agent: str | None = None,
    ) -> str:
        return enforce_valid_nomination(
            text,
            current_agent_key,
            remaining,
            force_termination,
            pending_retries,
            designated_next_agent,
        )


# Default singleton instance for backward compatibility
debate_orchestrator = DebateOrchestrator()


# Module-level backward-compatible function aliases
def detect_ambiguous_tags(text: str) -> list[tuple[str, list[str]]]:
    return entity_resolver.detect_ambiguous_tags(text)


def detect_unrecognized_tags(text: str) -> list[str]:
    return entity_resolver.detect_unrecognized_tags(text)


def extract_tagged_agents(text: str) -> list[str]:
    return entity_resolver.extract_tagged_agents(text)


__all__ = [
    "TAG_REGEX",
    "DebateOrchestrator",
    "debate_orchestrator",
    "resolve_target_file",
    "get_initial_agents",
    "detect_ambiguous_tags",
    "detect_unrecognized_tags",
    "extract_tagged_agents",
    "parse_agent_and_prompt",
    "enforce_valid_nomination",
]
