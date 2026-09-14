import asyncio
import logging
import os
import time
from typing import Any

from agents import trigger_thread_notes_update, trigger_usage_update
from core.models import FileContext
from orchestration.state import (
    DiscussionStateStore,
    ThreadContextStore,
    TurnRetryStateStore,
)

logger = logging.getLogger(__name__)


class ThreadStateManager:
    """Facade managing runtime discussion states, tasks, limits, retries, and context per Slack thread.

    Decomposed into cohesive domain stores:
    - self.discussion: DiscussionStateStore (tasks, remaining agents, custom flows, stop requests)
    - self.retry: TurnRetryStateStore (rate-limit retries, 503 error backoff, generations)
    - self.context: ThreadContextStore (history limits, word limits, locked languages, file context)
    """

    def __init__(self) -> None:
        self.discussion = DiscussionStateStore()
        self.retry = TurnRetryStateStore()
        self.context = ThreadContextStore()

        # Backward-compatible dictionary references
        self.remaining_agents_map = self.discussion.remaining_agents_map
        self.rate_limit_retry_queue = self.retry.rate_limit_retry_queue
        self.rate_limit_already_retried = self.retry.rate_limit_already_retried
        self.high_demand_errored_agents = self.retry.high_demand_errored_agents
        self.active_thread_tasks = self.discussion.active_thread_tasks
        self.thread_generations = self.retry.thread_generations
        self.custom_flow_threads = self.discussion.custom_flow_threads
        self.stop_requested_threads = self.discussion.stop_requested_threads
        self.thread_history_limits = self.context.thread_history_limits
        self.thread_max_words_limits = self.context.thread_max_words_limits
        self.thread_min_words_limits = self.context.thread_min_words_limits
        self.thread_languages = self.context.thread_languages
        self.thread_active_file_context = self.context.thread_active_file_context
        self.thread_last_activity: dict[tuple[str, str], float] = {}
        self.ttl_seconds: float = float(os.getenv("THREAD_STATE_TTL_SECONDS", "3600.0"))
        self.max_tracked_threads: int = int(os.getenv("MAX_TRACKED_THREADS", "500"))

    def _touch(self, channel_id: str, thread_ts: str) -> None:
        self.thread_last_activity[(channel_id, thread_ts)] = time.time()
        if len(self.thread_last_activity) > self.max_tracked_threads:
            self.cleanup_expired_threads(self.ttl_seconds)

    def touch(self, channel_id: str, thread_ts: str) -> None:
        self._touch(channel_id, thread_ts)

    def get_last_activity(self, channel_id: str, thread_ts: str) -> float | None:
        return self.thread_last_activity.get((channel_id, thread_ts))

    # --- ThreadContextStore Delegation ---
    def get_history_limit(self, channel_id: str, thread_ts: str) -> dict[str, int] | None:
        return self.context.get_history_limit(channel_id, thread_ts)

    def set_history_limit(self, channel_id: str, thread_ts: str, limit: int, prior_cutoff_index: int) -> None:
        self._touch(channel_id, thread_ts)
        self.context.set_history_limit(channel_id, thread_ts, limit, prior_cutoff_index)

    def clear_history_limit(self, channel_id: str, thread_ts: str) -> None:
        self.context.clear_history_limit(channel_id, thread_ts)

    def get_max_words(self, channel_id: str, thread_ts: str) -> int | None:
        return self.context.get_max_words(channel_id, thread_ts)

    def set_max_words(self, channel_id: str, thread_ts: str, max_words: int) -> None:
        self._touch(channel_id, thread_ts)
        self.context.set_max_words(channel_id, thread_ts, max_words)

    def clear_max_words(self, channel_id: str, thread_ts: str) -> None:
        self.context.clear_max_words(channel_id, thread_ts)

    def get_min_words(self, channel_id: str, thread_ts: str) -> int | None:
        return self.context.get_min_words(channel_id, thread_ts)

    def set_min_words(self, channel_id: str, thread_ts: str, min_words: int) -> None:
        self._touch(channel_id, thread_ts)
        self.context.set_min_words(channel_id, thread_ts, min_words)

    def clear_min_words(self, channel_id: str, thread_ts: str) -> None:
        self.context.clear_min_words(channel_id, thread_ts)

    def get_language(self, channel_id: str, thread_ts: str) -> str | None:
        return self.context.get_language(channel_id, thread_ts)

    def set_language(self, channel_id: str, thread_ts: str, language: str) -> None:
        self._touch(channel_id, thread_ts)
        self.context.set_language(channel_id, thread_ts, language)

    def clear_language(self, channel_id: str, thread_ts: str) -> None:
        self.context.clear_language(channel_id, thread_ts)

    def get_file_context(self, channel_id: str, thread_ts: str) -> dict[str, Any] | None:
        return self.context.get_file_context(channel_id, thread_ts)

    def set_file_context(self, channel_id: str, thread_ts: str, file_context: dict[str, Any] | FileContext) -> None:
        self._touch(channel_id, thread_ts)
        self.context.set_file_context(channel_id, thread_ts, file_context)

    def clear_file_context(self, channel_id: str, thread_ts: str) -> None:
        self.context.clear_file_context(channel_id, thread_ts)

    # --- TurnRetryStateStore Delegation ---
    def get_503_errored_agents(self, channel_id: str, thread_ts: str) -> set[str]:
        return self.retry.get_503_errored_agents(channel_id, thread_ts)

    def has_agent_503_errored(self, channel_id: str, thread_ts: str, agent_key: str) -> bool:
        return self.retry.has_agent_503_errored(channel_id, thread_ts, agent_key)

    def mark_agent_503_errored(self, channel_id: str, thread_ts: str, agent_key: str) -> None:
        self._touch(channel_id, thread_ts)
        self.retry.mark_agent_503_errored(channel_id, thread_ts, agent_key)

    def clear_503_errored_agents(self, channel_id: str, thread_ts: str) -> None:
        self.retry.clear_503_errored_agents(channel_id, thread_ts)

    def get_retry_queue(self, channel_id: str, thread_ts: str) -> list[str]:
        return self.retry.get_retry_queue(channel_id, thread_ts)

    def add_to_retry_queue(self, channel_id: str, thread_ts: str, agent_key: str) -> None:
        self._touch(channel_id, thread_ts)
        self.retry.add_to_retry_queue(channel_id, thread_ts, agent_key)

    def pop_retry_agent(self, channel_id: str, thread_ts: str, agent_key: str | None = None) -> str | None:
        self._touch(channel_id, thread_ts)
        return self.retry.pop_retry_agent(channel_id, thread_ts, agent_key)

    def has_pending_retries(self, channel_id: str, thread_ts: str) -> bool:
        return self.retry.has_pending_retries(channel_id, thread_ts)

    def clear_retry_queue(self, channel_id: str, thread_ts: str) -> None:
        self.retry.clear_retry_queue(channel_id, thread_ts)

    def get_already_retried(self, channel_id: str, thread_ts: str) -> set[str]:
        return self.retry.get_already_retried(channel_id, thread_ts)

    def is_already_retried(self, channel_id: str, thread_ts: str, agent_key: str) -> bool:
        return self.retry.is_already_retried(channel_id, thread_ts, agent_key)

    def mark_already_retried(self, channel_id: str, thread_ts: str, agent_key: str) -> None:
        self._touch(channel_id, thread_ts)
        self.retry.mark_already_retried(channel_id, thread_ts, agent_key)

    def clear_already_retried(self, channel_id: str, thread_ts: str) -> None:
        self.retry.clear_already_retried(channel_id, thread_ts)

    def get_generation(self, channel_id: str, thread_ts: str) -> int:
        return self.retry.get_generation(channel_id, thread_ts)

    def increment_generation(self, channel_id: str, thread_ts: str) -> int:
        self._touch(channel_id, thread_ts)
        return self.retry.increment_generation(channel_id, thread_ts)

    # --- DiscussionStateStore Delegation ---
    def get_remaining_agents(self, channel_id: str, thread_ts: str, default: list[str] | None = None) -> list[str]:
        return self.discussion.get_remaining_agents(channel_id, thread_ts, default)

    def set_remaining_agents(self, channel_id: str, thread_ts: str, agents: list[str]) -> None:
        self._touch(channel_id, thread_ts)
        self.discussion.set_remaining_agents(channel_id, thread_ts, agents)

    def pop_remaining_agent(self, channel_id: str, thread_ts: str, agent_key: str | None = None) -> str | None:
        self._touch(channel_id, thread_ts)
        return self.discussion.pop_remaining_agent(channel_id, thread_ts, agent_key)

    def has_remaining_agents_key(self, channel_id: str, thread_ts: str) -> bool:
        return self.discussion.has_remaining_agents_key(channel_id, thread_ts)

    def clear_remaining_agents(self, channel_id: str, thread_ts: str) -> None:
        self.discussion.clear_remaining_agents(channel_id, thread_ts)

    def get_active_task(self, channel_id: str, thread_ts: str) -> asyncio.Task | None:
        return self.discussion.get_active_task(channel_id, thread_ts)

    def set_active_task(self, channel_id: str, thread_ts: str, task: asyncio.Task) -> None:
        self._touch(channel_id, thread_ts)
        self.discussion.set_active_task(channel_id, thread_ts, task)

    def pop_active_task(self, channel_id: str, thread_ts: str) -> asyncio.Task | None:
        return self.discussion.pop_active_task(channel_id, thread_ts)

    def clear_active_task(self, channel_id: str, thread_ts: str, task: asyncio.Task | None = None) -> None:
        self.discussion.clear_active_task(channel_id, thread_ts, task)

    def is_custom_flow(self, channel_id: str, thread_ts: str) -> bool:
        return self.discussion.is_custom_flow(channel_id, thread_ts)

    def set_custom_flow(self, channel_id: str, thread_ts: str, is_custom: bool = True) -> None:
        self._touch(channel_id, thread_ts)
        self.discussion.set_custom_flow(channel_id, thread_ts, is_custom)

    def clear_custom_flow(self, channel_id: str, thread_ts: str) -> None:
        self.discussion.clear_custom_flow(channel_id, thread_ts)

    def is_stopped(self, channel_id: str, thread_ts: str) -> bool:
        return self.discussion.is_stopped(channel_id, thread_ts)

    def clear_stop(self, channel_id: str, thread_ts: str) -> None:
        self.discussion.clear_stop(channel_id, thread_ts)

    # --- Cross-Store Coordination & Lifecycle ---
    def is_discussion_active(self, channel_id: str, thread_ts: str) -> bool:
        return self.discussion.is_active(channel_id, thread_ts) or self.retry.has_pending_retries(channel_id, thread_ts)

    def interrupt_discussion(self, channel_id: str, thread_ts: str) -> None:
        self._touch(channel_id, thread_ts)
        new_gen = self.retry.increment_generation(channel_id, thread_ts)
        logger.info(f"[THREAD STATE] Interrupting discussion for thread {(channel_id, thread_ts)}. New generation: {new_gen}")
        self.discussion.cancel_active_task(channel_id, thread_ts)
        self.discussion.clear_remaining_agents(channel_id, thread_ts)
        self.retry.clear_retry_queue(channel_id, thread_ts)
        self.retry.clear_already_retried(channel_id, thread_ts)
        self.retry.clear_503_errored_agents(channel_id, thread_ts)
        self.context.clear_history_limit(channel_id, thread_ts)
        self.context.clear_file_context(channel_id, thread_ts)
        self.discussion.clear_custom_flow(channel_id, thread_ts)
        self.discussion.clear_stop(channel_id, thread_ts)

    def request_stop(self, channel_id: str, thread_ts: str) -> None:
        self._touch(channel_id, thread_ts)
        thread_key = (channel_id, thread_ts)
        self.discussion.request_stop(channel_id, thread_ts)
        self.discussion.clear_remaining_agents(channel_id, thread_ts)
        self.retry.clear_retry_queue(channel_id, thread_ts)
        self.retry.clear_already_retried(channel_id, thread_ts)
        self.retry.clear_503_errored_agents(channel_id, thread_ts)
        self.context.clear_history_limit(channel_id, thread_ts)
        self.discussion.clear_custom_flow(channel_id, thread_ts)
        logger.info(f"[THREAD STATE] Stop requested for thread {thread_key}. Queues and limits cleared.")
        try:
            trigger_thread_notes_update(channel_id=channel_id, thread_ts=thread_ts, is_completed=True)
            trigger_usage_update(channel_id=channel_id, thread_ts=thread_ts, is_completed=True)
        except Exception as e:
            logger.debug(f"[THREAD STATE] Could not finalize thread note or usage on stop: {e}")

    def cleanup_thread(self, channel_id: str, thread_ts: str) -> None:
        self.discussion.clear_thread(channel_id, thread_ts)
        self.retry.clear_thread(channel_id, thread_ts)
        self.context.clear_thread(channel_id, thread_ts)
        self.thread_last_activity.pop((channel_id, thread_ts), None)

    def clear_state(self, channel_id: str, thread_ts: str) -> None:
        self.cleanup_thread(channel_id, thread_ts)

    def reset_all(self) -> None:
        self.discussion.reset()
        self.retry.reset()
        self.context.reset()
        self.thread_last_activity.clear()

    def cleanup_expired_threads(self, ttl_seconds: float | None = None) -> int:
        if ttl_seconds is None:
            ttl_seconds = self.ttl_seconds
        now = time.time()
        evicted_count = 0

        # Reap completed tasks from active_thread_tasks
        done_keys = [k for k, task in list(self.active_thread_tasks.items()) if task.done()]
        for k in done_keys:
            self.active_thread_tasks.pop(k, None)

        # Prune inactive threads exceeding TTL
        candidates = [key for key, last_time in list(self.thread_last_activity.items()) if (now - last_time) > ttl_seconds]
        for channel_id, thread_ts in candidates:
            if not self.is_discussion_active(channel_id, thread_ts):
                self.cleanup_thread(channel_id, thread_ts)
                evicted_count += 1

        if evicted_count > 0:
            logger.info(f"[THREAD STATE] Pruned {evicted_count} inactive thread state(s) exceeding TTL ({ttl_seconds}s)")
        return evicted_count


# Default singleton instance
thread_state_manager = ThreadStateManager()

__all__ = [
    "DiscussionStateStore",
    "ThreadContextStore",
    "ThreadStateManager",
    "TurnRetryStateStore",
    "thread_state_manager",
]
