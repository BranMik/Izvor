import asyncio
import logging
import os
import time
from typing import Any

from core.database import get_connection, get_db_transaction
from core.models import FileContext
from orchestration.state import (
    DiscussionStateStore,
    RoundEvidenceStore,
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
    - self.evidence: RoundEvidenceStore (shared read-only tool results per round)
    """

    def __init__(self) -> None:
        self.discussion = DiscussionStateStore()
        self.retry = TurnRetryStateStore()
        self.context = ThreadContextStore()
        self.evidence = RoundEvidenceStore()

        self.thread_last_activity: dict[tuple[str, str], float] = {}
        self._cleanup_hooks: list[Any] = []
        self._reset_hooks: list[Any] = []

        def _spend_cleanup(c_id: str, t_ts: str) -> None:
            try:
                from pipeline.spend_governance import spend_governance

                spend_governance.clear_session(f"{c_id}:{t_ts}")
            except Exception as e:  # noqa: BLE001
                logger.debug("Spend cleanup hook error: %s", e)

        def _spend_reset() -> None:
            try:
                from pipeline.spend_governance import spend_governance

                spend_governance.clear_all_sessions()
            except Exception as e:  # noqa: BLE001
                logger.debug("Spend reset hook error: %s", e)

        self.register_cleanup_hook(_spend_cleanup)
        self.register_reset_hook(_spend_reset)

        self._max_tracked_threads_override: int | None = None
        self._ttl_seconds_override: float | None = None
        self._task_dispatcher_override: Any = None

    def configure_capacity(
        self,
        max_tracked_threads: int | None = None,
        ttl_seconds: float | None = None,
    ) -> None:
        self._max_tracked_threads_override = max_tracked_threads
        self._ttl_seconds_override = ttl_seconds

    def reset_capacity(self) -> None:
        self._max_tracked_threads_override = None
        self._ttl_seconds_override = None

    def configure_task_dispatcher(self, fn: Any) -> None:
        self._task_dispatcher_override = fn

    def reset_task_dispatcher(self) -> None:
        self._task_dispatcher_override = None

    @property
    def ttl_seconds(self) -> float:
        if self._ttl_seconds_override is not None:
            return float(self._ttl_seconds_override)
        return float(os.getenv("THREAD_STATE_TTL_SECONDS", "3600.0"))

    @property
    def max_tracked_threads(self) -> int:
        if self._max_tracked_threads_override is not None:
            return int(self._max_tracked_threads_override)
        return int(os.getenv("MAX_TRACKED_THREADS", "500"))

    def _check_capacity(self) -> None:
        if len(self.thread_last_activity) > self.max_tracked_threads:
            self.cleanup_expired_threads(self.ttl_seconds)
            if len(self.thread_last_activity) > self.max_tracked_threads:
                sorted_threads = sorted(self.thread_last_activity.items(), key=lambda item: item[1])
                for (c_id, t_ts), _ in sorted_threads:
                    if len(self.thread_last_activity) <= self.max_tracked_threads:
                        break
                    task = self.get_active_task(c_id, t_ts)
                    if task and not task.done():
                        continue
                    self.cleanup_thread(c_id, t_ts)

    def _touch(self, channel_id: str, thread_ts: str, sync_db: bool = False) -> None:
        now = time.time()
        self.thread_last_activity[(channel_id, thread_ts)] = now
        if sync_db:
            try:
                with get_db_transaction(immediate=True) as conn:
                    conn.execute(
                        "UPDATE thread_sessions SET last_activity = ? WHERE channel_id = ? AND thread_ts = ?",
                        (now, channel_id, thread_ts),
                    )
            except Exception as e:  # noqa: BLE001
                logger.debug("Thread session touch DB error: %s", e)
        self._check_capacity()

    def touch(self, channel_id: str, thread_ts: str) -> None:
        self._touch(channel_id, thread_ts, sync_db=True)

    def get_last_activity(self, channel_id: str, thread_ts: str) -> float | None:
        if (channel_id, thread_ts) in self.thread_last_activity:
            return self.thread_last_activity[(channel_id, thread_ts)]
        try:
            with get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT last_activity FROM thread_sessions WHERE channel_id = ? AND thread_ts = ?",
                    (channel_id, thread_ts),
                )
                row = cursor.fetchone()
                if row:
                    act = float(row["last_activity"])
                    self.thread_last_activity[(channel_id, thread_ts)] = act
                    return act
        except Exception as e:  # noqa: BLE001
            logger.debug("Thread session get_last_activity DB error: %s", e)
        return None

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

    def get_scribe_max_words(self, channel_id: str, thread_ts: str) -> int | None:
        return self.context.get_scribe_max_words(channel_id, thread_ts)

    def set_scribe_max_words(self, channel_id: str, thread_ts: str, max_words: int) -> None:
        self._touch(channel_id, thread_ts)
        self.context.set_scribe_max_words(channel_id, thread_ts, max_words)

    def clear_scribe_max_words(self, channel_id: str, thread_ts: str) -> None:
        self.context.clear_scribe_max_words(channel_id, thread_ts)

    def get_scribe_min_words(self, channel_id: str, thread_ts: str) -> int | None:
        return self.context.get_scribe_min_words(channel_id, thread_ts)

    def set_scribe_min_words(self, channel_id: str, thread_ts: str, min_words: int) -> None:
        self._touch(channel_id, thread_ts)
        self.context.set_scribe_min_words(channel_id, thread_ts, min_words)

    def clear_scribe_min_words(self, channel_id: str, thread_ts: str) -> None:
        self.context.clear_scribe_min_words(channel_id, thread_ts)

    def get_language(self, channel_id: str, thread_ts: str) -> str | None:
        return self.context.get_language(channel_id, thread_ts)

    def set_language(self, channel_id: str, thread_ts: str, language: str) -> None:
        self._touch(channel_id, thread_ts)
        self.context.set_language(channel_id, thread_ts, language)

    def clear_language(self, channel_id: str, thread_ts: str) -> None:
        self.context.clear_language(channel_id, thread_ts)

    def get_excluded_agents(self, channel_id: str, thread_ts: str) -> list[str]:
        return self.context.get_excluded_agents(channel_id, thread_ts)

    def set_excluded_agents(self, channel_id: str, thread_ts: str, agents: list[str]) -> None:
        self._touch(channel_id, thread_ts)
        self.context.set_excluded_agents(channel_id, thread_ts, agents)

    def add_excluded_agent(self, channel_id: str, thread_ts: str, agent: str) -> None:
        self._touch(channel_id, thread_ts)
        self.context.add_excluded_agent(channel_id, thread_ts, agent)

    def remove_excluded_agent(self, channel_id: str, thread_ts: str, agent: str) -> None:
        self._touch(channel_id, thread_ts)
        self.context.remove_excluded_agent(channel_id, thread_ts, agent)

    def clear_excluded_agents(self, channel_id: str, thread_ts: str) -> None:
        self.context.clear_excluded_agents(channel_id, thread_ts)

    def get_included_agents(self, channel_id: str, thread_ts: str) -> list[str]:
        return self.context.get_included_agents(channel_id, thread_ts)

    def set_included_agents(self, channel_id: str, thread_ts: str, agents: list[str]) -> None:
        self._touch(channel_id, thread_ts)
        self.context.set_included_agents(channel_id, thread_ts, agents)

    def add_included_agent(self, channel_id: str, thread_ts: str, agent: str) -> None:
        self._touch(channel_id, thread_ts)
        self.context.add_included_agent(channel_id, thread_ts, agent)

    def remove_included_agent(self, channel_id: str, thread_ts: str, agent: str) -> None:
        self._touch(channel_id, thread_ts)
        self.context.remove_included_agent(channel_id, thread_ts, agent)

    def clear_included_agents(self, channel_id: str, thread_ts: str) -> None:
        self.context.clear_included_agents(channel_id, thread_ts)

    def get_format_modifiers(self, channel_id: str, thread_ts: str) -> dict[str, Any]:
        return self.context.get_format_modifiers(channel_id, thread_ts)

    def set_format_modifiers(
        self, channel_id: str, thread_ts: str, modifiers: dict[str, Any]
    ) -> None:
        self._touch(channel_id, thread_ts)
        self.context.set_format_modifiers(channel_id, thread_ts, modifiers)

    def clear_format_modifiers(self, channel_id: str, thread_ts: str) -> None:
        self.context.clear_format_modifiers(channel_id, thread_ts)

    def revoke_all(self, channel_id: str, thread_ts: str) -> None:
        self._touch(channel_id, thread_ts)
        self.context.revoke_all(channel_id, thread_ts)

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

    def remove_remaining_agent(self, channel_id: str, thread_ts: str, agent_key: str) -> bool:
        self._touch(channel_id, thread_ts)
        return self.discussion.remove_remaining_agent(channel_id, thread_ts, agent_key)

    def pop_random_remaining_agent(self, channel_id: str, thread_ts: str) -> str | None:
        self._touch(channel_id, thread_ts)
        return self.discussion.pop_random_remaining_agent(channel_id, thread_ts)


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

    def dispatch_discussion_task(self, channel_id: str, thread_ts: str, coro: Any) -> None:
        """Dispatches an asynchronous discussion agent loop, attaching error tracking and state registry natively."""
        if self._task_dispatcher_override is not None:
            return self._task_dispatcher_override(channel_id, thread_ts, coro)
        from core.task_utils import safe_create_task

        self._touch(channel_id, thread_ts)
        task = safe_create_task(coro, name=f"discussion:{channel_id}:{thread_ts}")
        self.discussion.set_active_task(channel_id, thread_ts, task)

    def is_custom_flow(self, channel_id: str, thread_ts: str) -> bool:
        return self.discussion.is_custom_flow(channel_id, thread_ts)

    def set_custom_flow(self, channel_id: str, thread_ts: str, is_custom: bool = True) -> None:
        self._touch(channel_id, thread_ts)
        self.discussion.set_custom_flow(channel_id, thread_ts, is_custom)

    def clear_custom_flow(self, channel_id: str, thread_ts: str) -> None:
        self.discussion.clear_custom_flow(channel_id, thread_ts)

    def is_open_debate(self, channel_id: str, thread_ts: str) -> bool:
        return self.discussion.is_open_debate(channel_id, thread_ts)

    def set_open_debate(self, channel_id: str, thread_ts: str, is_open: bool = True) -> None:
        self._touch(channel_id, thread_ts)
        self.discussion.set_open_debate(channel_id, thread_ts, is_open)

    def clear_open_debate(self, channel_id: str, thread_ts: str) -> None:
        self.discussion.clear_open_debate(channel_id, thread_ts)

    def is_stopped(self, channel_id: str, thread_ts: str) -> bool:
        return self.discussion.is_stopped(channel_id, thread_ts)

    def clear_stop(self, channel_id: str, thread_ts: str) -> None:
        self.discussion.clear_stop(channel_id, thread_ts)

    def get_rounds(self, channel_id: str, thread_ts: str) -> int:
        return self.discussion.get_rounds(channel_id, thread_ts)

    def set_rounds(self, channel_id: str, thread_ts: str, rounds: int) -> None:
        self._touch(channel_id, thread_ts)
        self.discussion.set_rounds(channel_id, thread_ts, rounds)

    def decrement_rounds(self, channel_id: str, thread_ts: str) -> int:
        self._touch(channel_id, thread_ts)
        return self.discussion.decrement_rounds(channel_id, thread_ts)

    def clear_rounds(self, channel_id: str, thread_ts: str) -> None:
        self.discussion.clear_rounds(channel_id, thread_ts)

    def get_round_sequence(self, channel_id: str, thread_ts: str) -> list[str]:
        return self.discussion.get_round_sequence(channel_id, thread_ts)

    def set_round_sequence(self, channel_id: str, thread_ts: str, sequence: list[str]) -> None:
        self._touch(channel_id, thread_ts)
        self.discussion.set_round_sequence(channel_id, thread_ts, sequence)

    def clear_round_sequence(self, channel_id: str, thread_ts: str) -> None:
        self.discussion.clear_round_sequence(channel_id, thread_ts)

    def get_initial_prompt(self, channel_id: str, thread_ts: str) -> str | None:
        return self.discussion.get_initial_prompt(channel_id, thread_ts)

    def set_initial_prompt(self, channel_id: str, thread_ts: str, prompt: str) -> None:
        self._touch(channel_id, thread_ts)
        self.discussion.set_initial_prompt(channel_id, thread_ts, prompt)

    def clear_initial_prompt(self, channel_id: str, thread_ts: str) -> None:
        self.discussion.clear_initial_prompt(channel_id, thread_ts)

    def get_active_prompt(self, channel_id: str, thread_ts: str) -> str | None:
        return self.discussion.get_active_prompt(channel_id, thread_ts)

    def set_active_prompt(self, channel_id: str, thread_ts: str, prompt: str) -> None:
        self._touch(channel_id, thread_ts)
        self.discussion.set_active_prompt(channel_id, thread_ts, prompt)

    def clear_active_prompt(self, channel_id: str, thread_ts: str) -> None:
        self.discussion.clear_active_prompt(channel_id, thread_ts)

    def get_is_pure_skill_task(self, channel_id: str, thread_ts: str) -> bool:
        return self.discussion.get_is_pure_skill_task(channel_id, thread_ts)

    def set_is_pure_skill_task(self, channel_id: str, thread_ts: str, is_pure: bool) -> None:
        self._touch(channel_id, thread_ts)
        self.discussion.set_is_pure_skill_task(channel_id, thread_ts, is_pure)

    def clear_is_pure_skill_task(self, channel_id: str, thread_ts: str) -> None:
        self.discussion.clear_is_pure_skill_task(channel_id, thread_ts)

    def get_workflow_snapshot(self, channel_id: str, thread_ts: str) -> dict[str, Any] | None:
        return self.discussion.get_workflow_snapshot(channel_id, thread_ts)

    def set_workflow_snapshot(self, channel_id: str, thread_ts: str, snapshot: dict[str, Any]) -> None:
        self._touch(channel_id, thread_ts)
        self.discussion.set_workflow_snapshot(channel_id, thread_ts, snapshot)

    def clear_workflow_snapshot(self, channel_id: str, thread_ts: str) -> None:
        self.discussion.clear_workflow_snapshot(channel_id, thread_ts)

    # --- Cross-Store Coordination & Lifecycle ---
    def is_discussion_active(self, channel_id: str, thread_ts: str) -> bool:
        return self.discussion.is_active(channel_id, thread_ts) or self.retry.has_pending_retries(channel_id, thread_ts)

    def suspend_thread(self, channel_id: str, thread_ts: str) -> None:
        """Suspends thread execution (e.g., for circuit breakers), keeping context intact for resumption."""
        self._touch(channel_id, thread_ts)
        self.discussion.cancel_active_task(channel_id, thread_ts)
        logger.info(f"[THREAD STATE] Thread {(channel_id, thread_ts)} suspended. Routing queue preserved.")

    def interrupt_discussion(self, channel_id: str, thread_ts: str) -> None:
        self._touch(channel_id, thread_ts)
        new_gen = self.retry.increment_generation(channel_id, thread_ts)
        logger.info(f"[THREAD STATE] Interrupting discussion for thread {(channel_id, thread_ts)}. New generation: {new_gen}")
        self.discussion.cancel_active_task(channel_id, thread_ts)
        self.discussion._update_fields(
            channel_id,
            thread_ts,
            remaining_agents=None,
            custom_flow=0,
            open_debate=0,
            stop_requested=0,
            rounds=0,
            workflow_snapshot=None,
            is_pure_skill_task=0,
            consensus_reached=0,
            convergence_speakers="[]",
        )
        self.context._update_fields(
            channel_id,
            thread_ts,
            history_limit_config=None,
            file_context=None,
        )
        self.retry.clear_retry_queue(channel_id, thread_ts)
        self.retry.clear_already_retried(channel_id, thread_ts)
        self.retry.clear_503_errored_agents(channel_id, thread_ts)
        self.evidence.clear_thread_evidence(channel_id, thread_ts)

    def request_stop(self, channel_id: str, thread_ts: str) -> None:
        self._touch(channel_id, thread_ts)
        thread_key = (channel_id, thread_ts)
        self.discussion.cancel_active_task(channel_id, thread_ts)
        self.discussion._update_fields(
            channel_id,
            thread_ts,
            stop_requested=1,
            remaining_agents=None,
            custom_flow=0,
            open_debate=0,
            rounds=0,
            workflow_snapshot=None,
            is_pure_skill_task=0,
            consensus_reached=0,
            convergence_speakers="[]",
        )
        self.context._update_fields(channel_id, thread_ts, history_limit_config=None)
        self.retry.clear_retry_queue(channel_id, thread_ts)
        self.retry.clear_already_retried(channel_id, thread_ts)
        self.retry.clear_503_errored_agents(channel_id, thread_ts)
        self.evidence.clear_thread_evidence(channel_id, thread_ts)
        logger.info(f"[THREAD STATE] Stop requested for thread {thread_key}. Queues and limits cleared.")
        try:
            from agents import trigger_thread_notes_update, trigger_usage_update

            trigger_thread_notes_update(channel_id=channel_id, thread_ts=thread_ts, is_completed=True)
            trigger_usage_update(channel_id=channel_id, thread_ts=thread_ts, is_completed=True)
        except Exception as e:  # noqa: BLE001 - Error boundary ensures notification errors do not abort stop request
            logger.debug(f"[THREAD STATE] Could not finalize thread note or usage on stop: {e}")

    def register_cleanup_hook(self, hook: Any) -> None:
        """Registers a callback (channel_id: str, thread_ts: str) -> None for satellite state cleanup."""
        if hook not in self._cleanup_hooks:
            self._cleanup_hooks.append(hook)

    def register_reset_hook(self, hook: Any) -> None:
        """Registers a callback () -> None for global satellite state reset."""
        if hook not in self._reset_hooks:
            self._reset_hooks.append(hook)

    def clear_round_execution_state(self, channel_id: str, thread_ts: str) -> None:
        """Clears ephemeral execution queue and tasks when a round or discussion concludes, preserving persistent context."""
        self._touch(channel_id, thread_ts)
        self.discussion._update_fields(
            channel_id,
            thread_ts,
            remaining_agents=None,
            custom_flow=0,
            open_debate=0,
            stop_requested=0,
            rounds=0,
            workflow_snapshot=None,
            is_pure_skill_task=0,
            consensus_reached=0,
            convergence_speakers="[]",
        )
        self.retry.clear_retry_queue(channel_id, thread_ts)
        self.retry.clear_already_retried(channel_id, thread_ts)
        self.retry.clear_503_errored_agents(channel_id, thread_ts)
        self.context._update_fields(channel_id, thread_ts, file_context=None)
        self.evidence.clear_thread_evidence(channel_id, thread_ts)
        # Persistent context (language, max_words, min_words, scribe_max_words, scribe_min_words,
        # excluded_agents, included_agents, format_modifiers, round_sequences, initial_prompts)
        # remains intact for subsequent rounds.

    def cleanup_thread(self, channel_id: str, thread_ts: str) -> None:
        self.discussion.clear_thread(channel_id, thread_ts)
        self.retry.clear_thread(channel_id, thread_ts)
        self.context.clear_thread(channel_id, thread_ts)
        self.evidence.clear_thread_evidence(channel_id, thread_ts)
        self.thread_last_activity.pop((channel_id, thread_ts), None)
        for hook in list(self._cleanup_hooks):
            try:
                hook(channel_id, thread_ts)
            except Exception as e:  # noqa: BLE001 - Error boundary isolates third-party hook failure from thread cleanup
                logger.debug(f"[THREAD STATE] Error executing cleanup hook for {(channel_id, thread_ts)}: {e}")

    def clear_state(self, channel_id: str, thread_ts: str) -> None:
        self.cleanup_thread(channel_id, thread_ts)

    def reset_all(self) -> None:
        self.discussion.reset()
        self.retry.reset()
        self.context.reset()
        self.evidence.reset_all()
        self.thread_last_activity.clear()
        for hook in list(self._reset_hooks):
            try:
                hook()
            except Exception as e:  # noqa: BLE001 - Error boundary isolates individual hook failure from global reset
                logger.debug(f"[THREAD STATE] Error executing reset hook: {e}")

    def set_consensus_reached(self, channel_id: str, thread_ts: str, reached: bool = True) -> None:
        self._touch(channel_id, thread_ts)
        self.discussion.set_consensus_reached(channel_id, thread_ts, reached)

    def is_consensus_reached(self, channel_id: str, thread_ts: str) -> bool:
        return self.discussion.is_consensus_reached(channel_id, thread_ts)

    def record_convergence_speaker(self, channel_id: str, thread_ts: str, agent_key: str) -> None:
        self._touch(channel_id, thread_ts)
        self.discussion.record_convergence_speaker(channel_id, thread_ts, agent_key)

    def get_convergence_speakers(self, channel_id: str, thread_ts: str) -> list[str]:
        return self.discussion.get_convergence_speakers(channel_id, thread_ts)

    def clear_convergence_speakers(self, channel_id: str, thread_ts: str) -> None:
        self.discussion.clear_convergence_speakers(channel_id, thread_ts)

    def get_round_evidence(self, channel_id: str, thread_ts: str) -> list[Any]:
        """Returns all shared evidence items recorded for this thread."""
        return self.evidence.get_round_evidence(channel_id, thread_ts)

    def format_round_evidence(
        self, channel_id: str, thread_ts: str, max_items: int = 5, max_chars: int | None = None
    ) -> str | None:
        """Formats the latest verified round evidence into a structured string block."""
        return self.evidence.format_evidence_block(channel_id, thread_ts, max_items=max_items, max_chars=max_chars)

    def add_round_evidence(
        self,
        channel_id: str,
        thread_ts: str,
        tool_name: str,
        arguments: Any,
        result: str,
        agent_key: str,
    ) -> Any:
        """Adds verified tool execution result to the thread's round evidence store."""
        rounds = self.get_rounds(channel_id, thread_ts)
        return self.evidence.add_evidence(
            channel_id=channel_id,
            thread_ts=thread_ts,
            tool_name=tool_name,
            arguments=arguments,
            result=result,
            agent_key=agent_key,
            round_index=rounds,
        )


    def cleanup_expired_threads(self, ttl_seconds: float | None = None) -> int:
        if ttl_seconds is None:
            ttl_seconds = self.ttl_seconds
        now = time.time()
        evicted_count = 0

        # Reap completed tasks from active_thread_tasks
        done_keys = [k for k, task in list(self.discussion.active_thread_tasks.items()) if task.done()]
        for k in done_keys:
            self.discussion.active_thread_tasks.pop(k, None)

        # Prune inactive threads exceeding TTL (only skip if task is currently running)
        candidates = [key for key, last_time in list(self.thread_last_activity.items()) if (now - last_time) > ttl_seconds]
        for channel_id, thread_ts in candidates:
            task = self.get_active_task(channel_id, thread_ts)
            if task and not task.done():
                continue
            self.cleanup_thread(channel_id, thread_ts)
            evicted_count += 1

        # Purge from SQLite thread_sessions & thread_context for any inactive threads exceeding TTL
        # Only delete threads that do not have an active in-memory task running
        cutoff = now - ttl_seconds
        active_running_keys = {
            (c_id, t_ts)
            for (c_id, t_ts), task in list(self.discussion.active_thread_tasks.items())
            if task and not task.done()
        }
        try:
            with get_db_transaction(immediate=True) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT channel_id, thread_ts FROM thread_sessions WHERE last_activity < ?", (cutoff,))
                expired_rows = cursor.fetchall()
                to_delete = [
                    (row[0], row[1])
                    for row in expired_rows
                    if (row[0], row[1]) not in active_running_keys
                ]
                if to_delete:
                    cursor.executemany(
                        "DELETE FROM thread_sessions WHERE channel_id = ? AND thread_ts = ?",
                        to_delete,
                    )
                    cursor.executemany(
                        "DELETE FROM thread_context WHERE channel_id = ? AND thread_ts = ?",
                        to_delete,
                    )
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[THREAD STATE] SQLite cleanup error: {e}")

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

