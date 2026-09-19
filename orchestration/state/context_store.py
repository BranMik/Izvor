import logging
from typing import Any

from core.models import FileContext, HistoryLimitConfig

logger = logging.getLogger(__name__)


class ThreadContextStore:
    """Manages thread-level contextual constraints (history limits, word limits, language locks, and file attachments)."""

    def __init__(self) -> None:
        self.thread_history_limits: dict[tuple[str, str], dict[str, int]] = {}
        self.thread_max_words_limits: dict[tuple[str, str], int] = {}
        self.thread_min_words_limits: dict[tuple[str, str], int] = {}
        self.thread_scribe_max_words_limits: dict[tuple[str, str], int] = {}
        self.thread_scribe_min_words_limits: dict[tuple[str, str], int] = {}
        self.thread_languages: dict[tuple[str, str], str] = {}
        self.thread_excluded_agents: dict[tuple[str, str], list[str]] = {}
        self.thread_included_agents: dict[tuple[str, str], list[str]] = {}
        self.thread_format_modifiers: dict[tuple[str, str], dict[str, Any]] = {}
        self.thread_active_file_context: dict[tuple[str, str], dict[str, Any]] = {}

    def get_history_limit(
        self, channel_id: str, thread_ts: str
    ) -> dict[str, int] | None:
        """Returns active history limit dictionary for this thread."""
        return self.thread_history_limits.get((channel_id, thread_ts))

    def set_history_limit(
        self, channel_id: str, thread_ts: str, limit: int, prior_cutoff_index: int
    ) -> None:
        """Sets history limit configuration for this thread."""
        self.thread_history_limits[(channel_id, thread_ts)] = HistoryLimitConfig(
            limit=limit,
            prior_cutoff_index=prior_cutoff_index,
        ).to_dict()
        logger.info(
            f"[CONTEXT STORE] Set history limit for thread {(channel_id, thread_ts)}: limit={limit}, prior_cutoff_index={prior_cutoff_index}"
        )

    def clear_history_limit(self, channel_id: str, thread_ts: str) -> None:
        """Clears active history limit for this thread."""
        self.thread_history_limits.pop((channel_id, thread_ts), None)

    def get_max_words(self, channel_id: str, thread_ts: str) -> int | None:
        """Returns max_words limit for this thread."""
        return self.thread_max_words_limits.get((channel_id, thread_ts))

    def set_max_words(
        self, channel_id: str, thread_ts: str, max_words: int
    ) -> None:
        """Sets max_words limit for this thread."""
        self.thread_max_words_limits[(channel_id, thread_ts)] = max_words
        logger.info(
            f"[CONTEXT STORE] Set max_words limit for thread {(channel_id, thread_ts)}: max_words={max_words}"
        )

    def clear_max_words(self, channel_id: str, thread_ts: str) -> None:
        """Clears max_words limit for this thread."""
        self.thread_max_words_limits.pop((channel_id, thread_ts), None)

    def get_min_words(self, channel_id: str, thread_ts: str) -> int | None:
        """Returns min_words limit for this thread."""
        return self.thread_min_words_limits.get((channel_id, thread_ts))

    def set_min_words(
        self, channel_id: str, thread_ts: str, min_words: int
    ) -> None:
        """Sets min_words limit for this thread."""
        self.thread_min_words_limits[(channel_id, thread_ts)] = min_words
        logger.info(
            f"[CONTEXT STORE] Set min_words limit for thread {(channel_id, thread_ts)}: min_words={min_words}"
        )

    def clear_min_words(self, channel_id: str, thread_ts: str) -> None:
        """Clears min_words limit for this thread."""
        self.thread_min_words_limits.pop((channel_id, thread_ts), None)

    def get_scribe_max_words(self, channel_id: str, thread_ts: str) -> int | None:
        """Returns scribe_max_words limit for this thread."""
        return self.thread_scribe_max_words_limits.get((channel_id, thread_ts))

    def set_scribe_max_words(
        self, channel_id: str, thread_ts: str, max_words: int
    ) -> None:
        """Sets scribe_max_words limit for this thread."""
        self.thread_scribe_max_words_limits[(channel_id, thread_ts)] = max_words
        logger.info(
            f"[CONTEXT STORE] Set scribe_max_words limit for thread {(channel_id, thread_ts)}: {max_words}"
        )

    def clear_scribe_max_words(self, channel_id: str, thread_ts: str) -> None:
        """Clears scribe_max_words limit for this thread."""
        self.thread_scribe_max_words_limits.pop((channel_id, thread_ts), None)

    def get_scribe_min_words(self, channel_id: str, thread_ts: str) -> int | None:
        """Returns scribe_min_words limit for this thread."""
        return self.thread_scribe_min_words_limits.get((channel_id, thread_ts))

    def set_scribe_min_words(
        self, channel_id: str, thread_ts: str, min_words: int
    ) -> None:
        """Sets scribe_min_words limit for this thread."""
        self.thread_scribe_min_words_limits[(channel_id, thread_ts)] = min_words
        logger.info(
            f"[CONTEXT STORE] Set scribe_min_words limit for thread {(channel_id, thread_ts)}: {min_words}"
        )

    def clear_scribe_min_words(self, channel_id: str, thread_ts: str) -> None:
        """Clears scribe_min_words limit for this thread."""
        self.thread_scribe_min_words_limits.pop((channel_id, thread_ts), None)

    def get_language(self, channel_id: str, thread_ts: str) -> str | None:
        """Returns the locked language for this thread, if any."""
        return self.thread_languages.get((channel_id, thread_ts))

    def set_language(self, channel_id: str, thread_ts: str, language: str) -> None:
        """Sets and locks the language for this thread."""
        self.thread_languages[(channel_id, thread_ts)] = language
        logger.info(
            f"[CONTEXT STORE] Set locked language for thread {(channel_id, thread_ts)}: {language}"
        )

    def clear_language(self, channel_id: str, thread_ts: str) -> None:
        """Clears locked language for this thread."""
        self.thread_languages.pop((channel_id, thread_ts), None)

    def get_excluded_agents(self, channel_id: str, thread_ts: str) -> list[str]:
        """Returns list of agents persistently excluded from debate in this thread."""
        return list(self.thread_excluded_agents.get((channel_id, thread_ts), []))

    def set_excluded_agents(self, channel_id: str, thread_ts: str, agents: list[str]) -> None:
        """Sets persistently excluded agents for this thread."""
        self.thread_excluded_agents[(channel_id, thread_ts)] = list(dict.fromkeys(agents))
        logger.info(
            f"[CONTEXT STORE] Set excluded agents for thread {(channel_id, thread_ts)}: {agents}"
        )

    def add_excluded_agent(self, channel_id: str, thread_ts: str, agent: str) -> None:
        """Appends an agent to the persistent excluded list for this thread."""
        curr = self.get_excluded_agents(channel_id, thread_ts)
        if agent not in curr:
            curr.append(agent)
            self.set_excluded_agents(channel_id, thread_ts, curr)

    def remove_excluded_agent(self, channel_id: str, thread_ts: str, agent: str) -> None:
        """Removes an agent from the persistent excluded list for this thread."""
        curr = self.get_excluded_agents(channel_id, thread_ts)
        if agent in curr:
            curr = [a for a in curr if a != agent]
            if curr:
                self.thread_excluded_agents[(channel_id, thread_ts)] = curr
            else:
                self.thread_excluded_agents.pop((channel_id, thread_ts), None)

    def clear_excluded_agents(self, channel_id: str, thread_ts: str) -> None:
        """Clears all excluded agents for this thread."""
        self.thread_excluded_agents.pop((channel_id, thread_ts), None)

    def get_included_agents(self, channel_id: str, thread_ts: str) -> list[str]:
        """Returns list of agents explicitly forced into debate in this thread."""
        return list(self.thread_included_agents.get((channel_id, thread_ts), []))

    def set_included_agents(self, channel_id: str, thread_ts: str, agents: list[str]) -> None:
        """Sets explicitly included agents for this thread."""
        self.thread_included_agents[(channel_id, thread_ts)] = list(dict.fromkeys(agents))
        # Ensure included agents are removed from excluded list
        curr_excluded = self.get_excluded_agents(channel_id, thread_ts)
        remaining_excluded = [a for a in curr_excluded if a not in agents]
        if remaining_excluded != curr_excluded:
            self.set_excluded_agents(channel_id, thread_ts, remaining_excluded)
        logger.info(
            f"[CONTEXT STORE] Set included agents for thread {(channel_id, thread_ts)}: {agents}"
        )

    def add_included_agent(self, channel_id: str, thread_ts: str, agent: str) -> None:
        """Appends an agent to the persistent included list for this thread."""
        curr = self.get_included_agents(channel_id, thread_ts)
        if agent not in curr:
            curr.append(agent)
            self.set_included_agents(channel_id, thread_ts, curr)
        self.remove_excluded_agent(channel_id, thread_ts, agent)

    def clear_included_agents(self, channel_id: str, thread_ts: str) -> None:
        """Clears all explicitly included agents for this thread."""
        self.thread_included_agents.pop((channel_id, thread_ts), None)

    def get_format_modifiers(self, channel_id: str, thread_ts: str) -> dict[str, Any]:
        """Returns custom format modifiers dictionary for this thread."""
        return dict(self.thread_format_modifiers.get((channel_id, thread_ts), {}))

    def set_format_modifiers(
        self, channel_id: str, thread_ts: str, modifiers: dict[str, Any]
    ) -> None:
        """Sets custom format modifiers for this thread."""
        self.thread_format_modifiers[(channel_id, thread_ts)] = dict(modifiers)

    def clear_format_modifiers(self, channel_id: str, thread_ts: str) -> None:
        """Clears custom format modifiers for this thread."""
        self.thread_format_modifiers.pop((channel_id, thread_ts), None)

    def get_file_context(
        self, channel_id: str, thread_ts: str
    ) -> dict[str, Any] | None:
        """Returns active file context dictionary for this thread."""
        return self.thread_active_file_context.get((channel_id, thread_ts))

    def set_file_context(
        self, channel_id: str, thread_ts: str, file_context: dict[str, Any] | FileContext
    ) -> None:
        """Sets active file context for this thread."""
        ctx_dict = (
            file_context.to_dict()
            if isinstance(file_context, FileContext)
            else file_context
        )
        self.thread_active_file_context[(channel_id, thread_ts)] = ctx_dict
        logger.info(
            f"[CONTEXT STORE] Set active file context for thread {(channel_id, thread_ts)}: file='{ctx_dict.get('file_name')}', authorized_agents={ctx_dict.get('authorized_agents')}"
        )

    def clear_file_context(self, channel_id: str, thread_ts: str) -> None:
        """Clears active file context for this thread."""
        self.thread_active_file_context.pop((channel_id, thread_ts), None)

    def revoke_all(self, channel_id: str, thread_ts: str) -> None:
        """Revokes all format constraints, language locks, and agent exclusions/inclusions for this thread."""
        self.clear_max_words(channel_id, thread_ts)
        self.clear_min_words(channel_id, thread_ts)
        self.clear_scribe_max_words(channel_id, thread_ts)
        self.clear_scribe_min_words(channel_id, thread_ts)
        self.clear_excluded_agents(channel_id, thread_ts)
        self.clear_included_agents(channel_id, thread_ts)
        self.clear_format_modifiers(channel_id, thread_ts)
        self.clear_language(channel_id, thread_ts)
        logger.info(f"[CONTEXT STORE] Revoked all constraints for thread {(channel_id, thread_ts)}")

    def clear_thread(self, channel_id: str, thread_ts: str) -> None:
        """Purges context limits and data for this thread."""
        self.clear_history_limit(channel_id, thread_ts)
        self.clear_max_words(channel_id, thread_ts)
        self.clear_min_words(channel_id, thread_ts)
        self.clear_scribe_max_words(channel_id, thread_ts)
        self.clear_scribe_min_words(channel_id, thread_ts)
        self.clear_language(channel_id, thread_ts)
        self.clear_excluded_agents(channel_id, thread_ts)
        self.clear_included_agents(channel_id, thread_ts)
        self.clear_format_modifiers(channel_id, thread_ts)
        self.clear_file_context(channel_id, thread_ts)

    def reset(self) -> None:
        """Resets all context limits and state across all threads."""
        self.thread_history_limits.clear()
        self.thread_max_words_limits.clear()
        self.thread_min_words_limits.clear()
        self.thread_scribe_max_words_limits.clear()
        self.thread_scribe_min_words_limits.clear()
        self.thread_languages.clear()
        self.thread_excluded_agents.clear()
        self.thread_included_agents.clear()
        self.thread_format_modifiers.clear()
        self.thread_active_file_context.clear()
