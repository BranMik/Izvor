import asyncio
import logging
from typing import Any

from agents import trigger_thread_notes_update, trigger_usage_update
from core.models import FileContext, HistoryLimitConfig

logger = logging.getLogger(__name__)


class ThreadStateManager:
    """Manages runtime discussion states, tasks, limits, retries, and context per Slack thread."""

    def __init__(self):
        self.remaining_agents_map: dict[tuple[str, str], list[str]] = {}
        self.rate_limit_retry_queue: dict[tuple[str, str], list[str]] = {}
        self.rate_limit_already_retried: dict[tuple[str, str], set[str]] = {}
        self.high_demand_errored_agents: dict[tuple[str, str], set[str]] = {}
        self.active_thread_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self.thread_generations: dict[tuple[str, str], int] = {}
        self.custom_flow_threads: set[tuple[str, str]] = set()
        self.stop_requested_threads: set[tuple[str, str]] = set()
        self.thread_history_limits: dict[tuple[str, str], dict[str, int]] = {}
        self.thread_max_words_limits: dict[tuple[str, str], int] = {}
        self.thread_min_words_limits: dict[tuple[str, str], int] = {}
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
            f"[THREAD STATE] Set history limit for thread {(channel_id, thread_ts)}: limit={limit}, prior_cutoff_index={prior_cutoff_index}"
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
            f"[THREAD STATE] Set max_words limit for thread {(channel_id, thread_ts)}: max_words={max_words}"
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
            f"[THREAD STATE] Set min_words limit for thread {(channel_id, thread_ts)}: min_words={min_words}"
        )

    def clear_min_words(self, channel_id: str, thread_ts: str) -> None:
        """Clears min_words limit for this thread."""
        self.thread_min_words_limits.pop((channel_id, thread_ts), None)

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
            f"[THREAD STATE] Set active file context for thread {(channel_id, thread_ts)}: file='{ctx_dict.get('file_name')}', authorized_agents={ctx_dict.get('authorized_agents')}"
        )

    def clear_file_context(self, channel_id: str, thread_ts: str) -> None:
        """Clears active file context for this thread."""
        self.thread_active_file_context.pop((channel_id, thread_ts), None)

    def get_503_errored_agents(
        self, channel_id: str, thread_ts: str
    ) -> set[str]:
        """Returns set of agent keys that encountered 503/high-demand error in this thread."""
        return self.high_demand_errored_agents.get((channel_id, thread_ts), set())

    def has_agent_503_errored(
        self, channel_id: str, thread_ts: str, agent_key: str
    ) -> bool:
        """Checks if a specific agent has already errored with 503/high-demand in this thread."""
        return agent_key in self.high_demand_errored_agents.get(
            (channel_id, thread_ts), set()
        )

    def mark_agent_503_errored(
        self, channel_id: str, thread_ts: str, agent_key: str
    ) -> None:
        """Marks that an agent has errored with 503/high-demand in this thread."""
        thread_key = (channel_id, thread_ts)
        self.high_demand_errored_agents.setdefault(thread_key, set()).add(agent_key)
        logger.info(
            f"[THREAD STATE] Marked agent '{agent_key}' as 503-errored for thread {thread_key}. "
            f"All 503 errored agents in thread: {self.high_demand_errored_agents[thread_key]}"
        )

    def clear_503_errored_agents(self, channel_id: str, thread_ts: str) -> None:
        """Clears 503 errored agents tracking for this thread."""
        self.high_demand_errored_agents.pop((channel_id, thread_ts), None)

    def is_discussion_active(self, channel_id: str, thread_ts: str) -> bool:
        """Checks if there is an active discussion ongoing in this thread."""
        thread_key = (channel_id, thread_ts)
        task = self.active_thread_tasks.get(thread_key)
        if task and not task.done():
            return True
        if self.remaining_agents_map.get(thread_key):
            return True
        return bool(self.rate_limit_retry_queue.get(thread_key))

    def interrupt_discussion(self, channel_id: str, thread_ts: str) -> None:
        """Interrupts and cancels the current ongoing discussion for this thread."""
        thread_key = (channel_id, thread_ts)
        self.thread_generations[thread_key] = (
            self.thread_generations.get(thread_key, 0) + 1
        )
        logger.info(
            f"[THREAD STATE] Interrupting discussion for thread {thread_key}. New generation: {self.thread_generations[thread_key]}"
        )

        task = self.active_thread_tasks.pop(thread_key, None)
        if task and not task.done() and task is not asyncio.current_task():
            logger.info(
                f"[THREAD STATE] Cancelling active background task for thread {thread_key}"
            )
            task.cancel()

        self.remaining_agents_map.pop(thread_key, None)
        self.rate_limit_retry_queue.pop(thread_key, None)
        self.rate_limit_already_retried.pop(thread_key, None)
        self.high_demand_errored_agents.pop(thread_key, None)
        self.thread_history_limits.pop(thread_key, None)
        self.thread_active_file_context.pop(thread_key, None)
        self.custom_flow_threads.discard(thread_key)
        self.stop_requested_threads.discard(thread_key)

    def request_stop(self, channel_id: str, thread_ts: str) -> None:
        """Marks thread for stopping, cleans remaining queue, and triggers completion notes."""
        thread_key = (channel_id, thread_ts)
        self.stop_requested_threads.add(thread_key)
        self.remaining_agents_map.pop(thread_key, None)
        self.rate_limit_retry_queue.pop(thread_key, None)
        self.rate_limit_already_retried.pop(thread_key, None)
        self.high_demand_errored_agents.pop(thread_key, None)
        self.thread_history_limits.pop(thread_key, None)
        self.custom_flow_threads.discard(thread_key)
        logger.info(
            f"[THREAD STATE] Stop requested for thread {thread_key}. Queues and limits cleared."
        )
        try:
            trigger_thread_notes_update(
                channel_id=channel_id,
                thread_ts=thread_ts,
                is_completed=True,
            )
            trigger_usage_update(
                channel_id=channel_id,
                thread_ts=thread_ts,
                is_completed=True,
            )
        except Exception as e:
            logger.debug(
                f"[THREAD STATE] Could not finalize thread note or usage on stop: {e}"
            )

    def is_stopped(self, channel_id: str, thread_ts: str) -> bool:
        """Checks if stop has been requested for this thread."""
        return (channel_id, thread_ts) in self.stop_requested_threads

    def clear_stop(self, channel_id: str, thread_ts: str) -> None:
        """Clears stop flag for this thread."""
        self.stop_requested_threads.discard((channel_id, thread_ts))

    def clear_state(self, channel_id: str, thread_ts: str) -> None:
        """Cleans up all thread-level discussion state maps and flags."""
        thread_key = (channel_id, thread_ts)
        self.clear_stop(channel_id, thread_ts)
        self.clear_history_limit(channel_id, thread_ts)
        self.clear_max_words(channel_id, thread_ts)
        self.clear_min_words(channel_id, thread_ts)
        self.clear_file_context(channel_id, thread_ts)
        self.custom_flow_threads.discard(thread_key)
        self.remaining_agents_map.pop(thread_key, None)
        self.rate_limit_retry_queue.pop(thread_key, None)
        self.rate_limit_already_retried.pop(thread_key, None)
        self.high_demand_errored_agents.pop(thread_key, None)


# Default singleton instance
thread_state_manager = ThreadStateManager()

# Module-level variable mapping for backward compatibility
REMAINING_AGENTS_MAP = thread_state_manager.remaining_agents_map
RATE_LIMIT_RETRY_QUEUE = thread_state_manager.rate_limit_retry_queue
RATE_LIMIT_ALREADY_RETRIED = thread_state_manager.rate_limit_already_retried
HIGH_DEMAND_ERRORED_AGENTS = thread_state_manager.high_demand_errored_agents
ACTIVE_THREAD_TASKS = thread_state_manager.active_thread_tasks
THREAD_GENERATIONS = thread_state_manager.thread_generations
CUSTOM_FLOW_THREADS = thread_state_manager.custom_flow_threads
STOP_REQUESTED_THREADS = thread_state_manager.stop_requested_threads
THREAD_HISTORY_LIMITS = thread_state_manager.thread_history_limits
THREAD_MAX_WORDS_LIMITS = thread_state_manager.thread_max_words_limits
THREAD_MIN_WORDS_LIMITS = thread_state_manager.thread_min_words_limits
THREAD_ACTIVE_FILE_CONTEXT = thread_state_manager.thread_active_file_context


# Backward-compatible function aliases
def get_503_errored_agents(
    channel_id: str, thread_ts: str
) -> set[str]:
    return thread_state_manager.get_503_errored_agents(channel_id, thread_ts)


def has_agent_503_errored(
    channel_id: str, thread_ts: str, agent_key: str
) -> bool:
    return thread_state_manager.has_agent_503_errored(channel_id, thread_ts, agent_key)


def mark_agent_503_errored(
    channel_id: str, thread_ts: str, agent_key: str
) -> None:
    thread_state_manager.mark_agent_503_errored(channel_id, thread_ts, agent_key)


def clear_503_errored_agents(channel_id: str, thread_ts: str) -> None:
    thread_state_manager.clear_503_errored_agents(channel_id, thread_ts)


def get_thread_history_limit(
    channel_id: str, thread_ts: str
) -> dict[str, int] | None:
    return thread_state_manager.get_history_limit(channel_id, thread_ts)


def set_thread_history_limit(
    channel_id: str, thread_ts: str, limit: int, prior_cutoff_index: int
) -> None:
    thread_state_manager.set_history_limit(
        channel_id, thread_ts, limit, prior_cutoff_index
    )


def clear_thread_history_limit(channel_id: str, thread_ts: str) -> None:
    thread_state_manager.clear_history_limit(channel_id, thread_ts)


def get_thread_max_words(channel_id: str, thread_ts: str) -> int | None:
    return thread_state_manager.get_max_words(channel_id, thread_ts)


def set_thread_max_words(channel_id: str, thread_ts: str, max_words: int) -> None:
    thread_state_manager.set_max_words(channel_id, thread_ts, max_words)


def clear_thread_max_words(channel_id: str, thread_ts: str) -> None:
    thread_state_manager.clear_max_words(channel_id, thread_ts)


def get_thread_min_words(channel_id: str, thread_ts: str) -> int | None:
    return thread_state_manager.get_min_words(channel_id, thread_ts)


def set_thread_min_words(channel_id: str, thread_ts: str, min_words: int) -> None:
    thread_state_manager.set_min_words(channel_id, thread_ts, min_words)


def clear_thread_min_words(channel_id: str, thread_ts: str) -> None:
    thread_state_manager.clear_min_words(channel_id, thread_ts)


def get_thread_file_context(
    channel_id: str, thread_ts: str
) -> dict[str, Any] | None:
    return thread_state_manager.get_file_context(channel_id, thread_ts)


def set_thread_file_context(
    channel_id: str, thread_ts: str, file_context: dict[str, Any] | FileContext
) -> None:
    thread_state_manager.set_file_context(channel_id, thread_ts, file_context)


def clear_thread_file_context(channel_id: str, thread_ts: str) -> None:
    thread_state_manager.clear_file_context(channel_id, thread_ts)


def is_discussion_active(channel_id: str, thread_ts: str) -> bool:
    return thread_state_manager.is_discussion_active(channel_id, thread_ts)


def interrupt_thread_discussion(channel_id: str, thread_ts: str) -> None:
    thread_state_manager.interrupt_discussion(channel_id, thread_ts)


def request_thread_stop(channel_id: str, thread_ts: str) -> None:
    thread_state_manager.request_stop(channel_id, thread_ts)


def is_thread_stopped(channel_id: str, thread_ts: str) -> bool:
    return thread_state_manager.is_stopped(channel_id, thread_ts)


def clear_thread_stop(channel_id: str, thread_ts: str) -> None:
    thread_state_manager.clear_stop(channel_id, thread_ts)


def clear_thread_state(channel_id: str, thread_ts: str) -> None:
    thread_state_manager.clear_state(channel_id, thread_ts)

