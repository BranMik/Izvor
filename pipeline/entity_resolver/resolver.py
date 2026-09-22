import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Matches [TagName] but avoids matching token count brackets like [150/50] or URLs
TAG_REGEX = re.compile(r"\[([a-zA-Z0-9_\- ]+)\]")


class EntityResolver:
    """
    Service for resolving, matching, and validating agent identities and tags
    across multi-agent conversations.
    """

    def __init__(self, agents_config: dict[str, dict[str, Any]] | None = None):
        self._agents_config = agents_config

    def _get_agents_map(self) -> dict[str, dict[str, Any]]:
        if self._agents_config is not None:
            return self._agents_config
        try:
            from orchestration.agent_settings import settings_manager
            if settings_manager.agents_config:
                return settings_manager.agents_config
        except (ImportError, AttributeError, TypeError) as e:
            logger.debug(f"[RESOLVER] settings_manager lookup skipped: {e}")
        try:
            from core.config import AGENTS_CONFIG
            if AGENTS_CONFIG:
                return AGENTS_CONFIG
        except (ImportError, AttributeError, TypeError) as e:
            logger.debug(f"[RESOLVER] AGENTS_CONFIG lookup skipped: {e}")
        return {}

    def normalize(self, s: str) -> str:
        """Removes whitespace, underscores, and hyphens and lowercases string."""
        return re.sub(r"[\s_\-]+", "", s).lower()

    def resolve_candidates(self, name_str: str) -> list[str]:
        """
        Resolves a name, tag, or username to all matching registered agent keys.
        Returns:
          - [canonical_key] if exactly 1 match is found.
          - [candidate1, candidate2, ...] if ambiguous.
          - [] if no matching agents exist.
        """
        if not name_str:
            return []

        agents = self._get_agents_map()
        cleaned = name_str.strip().strip("[](){}:,; \t*~_").strip()
        if not cleaned:
            return []

        # 1. Exact key match (case-insensitive)
        for key in agents:
            if cleaned.lower() == key.lower():
                return [key]

        # 2. Normalized key / username match
        norm_cleaned = self.normalize(cleaned)
        if not norm_cleaned:
            return []

        for key, cfg in agents.items():
            if norm_cleaned == self.normalize(key):
                return [key]
            username = cfg.get("username", "")
            if username and norm_cleaned == self.normalize(username):
                return [key]

        # 3. Substring/prefix/role match against key or username
        matches = []
        for key, cfg in agents.items():
            if norm_cleaned in self.normalize(key) or (
                cfg.get("username") and norm_cleaned in self.normalize(cfg["username"])
            ):
                matches.append(key)

        return list(dict.fromkeys(matches))

    def resolve_agent_name(self, name_str: str) -> str | None:
        """
        Resolves an agent name or tag to its canonical key if unambiguous.
        Returns None if unresolved or ambiguous.
        """
        candidates = self.resolve_candidates(name_str)
        if len(candidates) == 1:
            return candidates[0]
        return None

    def extract_tagged_agents(self, text: str) -> list[str]:
        """
        Extracts all valid, unambiguous agent keys tagged in [Agent_Tag] format,
        in the order of appearance.
        """
        matches: list[tuple[int, str]] = []
        cleaned = re.sub(r"<@[A-Z0-9]+>", "", text)
        cleaned = re.sub(r"\[([^\]]+)\]\([^\)]+\)", "", cleaned)

        for match in TAG_REGEX.finditer(cleaned):
            resolved_key = self.resolve_agent_name(match.group(1))
            if resolved_key:
                matches.append((match.start(), resolved_key))

        matches.sort(key=lambda x: x[0])

        unique_keys = []
        for _, key in matches:
            if key not in unique_keys:
                unique_keys.append(key)

        return unique_keys

    def detect_ambiguous_tags(self, text: str) -> list[tuple[str, list[str]]]:
        """
        Detects any [bracketed] tags in text that match more than one registered agent.
        Returns list of tuples: `[(raw_tag_str, [candidate_key_1, candidate_key_2, ...])]`.
        """
        ambiguous: list[tuple[str, list[str]]] = []
        cleaned = re.sub(r"<@[A-Z0-9]+>", "", text)
        cleaned = re.sub(r"\[([^\]]+)\]\([^\)]+\)", "", cleaned)

        for match in TAG_REGEX.finditer(cleaned):
            tag_content = match.group(1).strip()
            # Skip token counters like [150/50]
            if re.match(r"^\d+\s*/\s*\d+$", tag_content):
                continue
            candidates = self.resolve_candidates(tag_content)
            if len(candidates) > 1:
                ambiguous.append((tag_content, candidates))

        return ambiguous

    def detect_unrecognized_tags(self, text: str) -> list[str]:
        """
        Detects any [bracketed] tags in user input that do NOT match any known agent,
        excluding system indicators, token counts, or command parameters.
        """
        unrecognized: list[str] = []
        cleaned = re.sub(r"<@[A-Z0-9]+>", "", text)
        cleaned = re.sub(r"\[([^\]]+)\]\([^\)]+\)", "", cleaned)

        ignored_tags = {
            "cailogics",
            "app_mention",
            "live discussion notes",
            "final discussion notes",
            "bilješke rasprave",
            "strateška sinteza",
            "strategic synthesis",
            "completed",
            "updating live",
        }

        for match in TAG_REGEX.finditer(cleaned):
            tag_content = match.group(1).strip()
            # Skip token usage [150/50]
            if re.match(r"^\d+\s*/\s*\d+$", tag_content):
                continue
            if tag_content.lower() in ignored_tags:
                continue
            candidates = self.resolve_candidates(tag_content)
            if not candidates:
                unrecognized.append(tag_content)

        return unrecognized

    def sanitize_agent_tags(self, text: str, agent_keys: list[str] | set[str]) -> str:
        """
        Removes square brackets from agent tags to prevent re-triggering nominations.
        E.g., `[Architect]` -> `Architect`.
        """
        sanitized = text
        for key in agent_keys:
            sanitized = re.sub(
                r"\[" + re.escape(key) + r"\]", key, sanitized, flags=re.IGNORECASE
            )
        return sanitized

    def strip_agent_nominations(self, text: str) -> str:
        """
        Completely removes nominations (lead-in phrases and agent names) when an agent
        is blocked from nominating.
        """
        from orchestration.nomination import strip_all_nominations

        return strip_all_nominations(text)


# Global default instance
entity_resolver = EntityResolver()
