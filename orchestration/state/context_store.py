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
        self.thread_languages: dict[tuple[str, str], str] = {}
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

    def clear_thread(self, channel_id: str, thread_ts: str) -> None:
        """Purges context limits and data for this thread."""
        self.clear_history_limit(channel_id, thread_ts)
        self.clear_max_words(channel_id, thread_ts)
        self.clear_min_words(channel_id, thread_ts)
        self.clear_language(channel_id, thread_ts)
        self.clear_file_context(channel_id, thread_ts)

    def reset(self) -> None:
        """Resets all context limits and state across all threads."""
        self.thread_history_limits.clear()
        self.thread_max_words_limits.clear()
        self.thread_min_words_limits.clear()
        self.thread_languages.clear()
        self.thread_active_file_context.clear()
