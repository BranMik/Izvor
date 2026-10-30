import logging

logger = logging.getLogger(__name__)


class TurnRetryStateStore:
    """Manages rate-limit retry queues, 503 high-demand error tracking, and turn generations per Slack thread."""

    def __init__(self) -> None:
        self.rate_limit_retry_queue: dict[tuple[str, str], list[str]] = {}
        self.rate_limit_already_retried: dict[tuple[str, str], set[str]] = {}
        self.high_demand_errored_agents: dict[tuple[str, str], set[str]] = {}
        self.thread_generations: dict[tuple[str, str], int] = {}

    def get_retry_queue(self, channel_id: str, thread_ts: str) -> list[str]:
        """Returns the pending rate-limit retry queue for this thread."""
        return list(self.rate_limit_retry_queue.get((channel_id, thread_ts), []))

    def add_to_retry_queue(
        self, channel_id: str, thread_ts: str, agent_key: str
    ) -> None:
        """Appends an agent to the rate-limit retry queue for this thread."""
        thread_key = (channel_id, thread_ts)
        queue = self.rate_limit_retry_queue.setdefault(thread_key, [])
        if agent_key not in queue:
            queue.append(agent_key)
            logger.info(
                f"[RETRY STORE] Added {agent_key} to retry queue for {thread_key}. Current queue: {queue}"
            )

    def pop_retry_agent(
        self, channel_id: str, thread_ts: str, agent_key: str | None = None
    ) -> str | None:
        """Pops a specific agent or the next agent from the retry queue."""
        thread_key = (channel_id, thread_ts)
        queue = self.rate_limit_retry_queue.get(thread_key, [])
        if not queue:
            return None
        if agent_key is None:
            popped = queue.pop(0)
            if not queue:
                self.rate_limit_retry_queue.pop(thread_key, None)
            return popped
        if agent_key in queue:
            queue.remove(agent_key)
            if not queue:
                self.rate_limit_retry_queue.pop(thread_key, None)
            return agent_key
        return None

    def has_pending_retries(self, channel_id: str, thread_ts: str) -> bool:
        """Checks if there are any agents queued for rate-limit retry."""
        return bool(self.rate_limit_retry_queue.get((channel_id, thread_ts)))

    def clear_retry_queue(self, channel_id: str, thread_ts: str) -> None:
        """Clears the retry queue for this thread."""
        self.rate_limit_retry_queue.pop((channel_id, thread_ts), None)

    def get_already_retried(self, channel_id: str, thread_ts: str) -> set[str]:
        """Returns the set of agents that have already been retried once in this thread."""
        return set(self.rate_limit_already_retried.get((channel_id, thread_ts), set()))

    def is_already_retried(
        self, channel_id: str, thread_ts: str, agent_key: str
    ) -> bool:
        """Checks if an agent has already undergone a rate-limit retry."""
        return agent_key in self.rate_limit_already_retried.get(
            (channel_id, thread_ts), set()
        )

    def mark_already_retried(
        self, channel_id: str, thread_ts: str, agent_key: str
    ) -> None:
        """Marks an agent as already retried to prevent infinite retry loops."""
        thread_key = (channel_id, thread_ts)
        self.rate_limit_already_retried.setdefault(thread_key, set()).add(agent_key)

    def clear_already_retried(self, channel_id: str, thread_ts: str) -> None:
        """Clears the already retried set for this thread."""
        self.rate_limit_already_retried.pop((channel_id, thread_ts), None)

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
            f"[RETRY STORE] Marked agent '{agent_key}' as 503-errored for thread {thread_key}. "
            f"All 503 errored agents in thread: {self.high_demand_errored_agents[thread_key]}"
        )

    def clear_503_errored_agents(self, channel_id: str, thread_ts: str) -> None:
        """Clears 503 errored agents tracking for this thread."""
        self.high_demand_errored_agents.pop((channel_id, thread_ts), None)

    def get_generation(self, channel_id: str, thread_ts: str) -> int:
        """Gets current generation counter for this thread."""
        return self.thread_generations.get((channel_id, thread_ts), 0)

    def increment_generation(self, channel_id: str, thread_ts: str) -> int:
        """Increments and returns the generation counter for this thread."""
        thread_key = (channel_id, thread_ts)
        new_gen = self.thread_generations.get(thread_key, 0) + 1
        self.thread_generations[thread_key] = new_gen
        return new_gen

    def clear_generation(self, channel_id: str, thread_ts: str) -> None:
        """Clears generation counter for this thread."""
        self.thread_generations.pop((channel_id, thread_ts), None)

    def clear_thread(self, channel_id: str, thread_ts: str) -> None:
        """Purges retry and generation state for this thread."""
        thread_key = (channel_id, thread_ts)
        self.clear_retry_queue(channel_id, thread_ts)
        self.clear_already_retried(channel_id, thread_ts)
        self.clear_503_errored_agents(channel_id, thread_ts)
        self.thread_generations.pop(thread_key, None)

    def reset(self) -> None:
        """Resets all retry states and generations across all threads."""
        self.rate_limit_retry_queue.clear()
        self.rate_limit_already_retried.clear()
        self.high_demand_errored_agents.clear()
        self.thread_generations.clear()
