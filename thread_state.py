import asyncio
import logging
from typing import Any

from agents import trigger_thread_notes_update, trigger_usage_update

logger = logging.getLogger(__name__)

# Track agents who haven't spoken yet per thread (mapped by (channel_id, thread_ts))
REMAINING_AGENTS_MAP: dict[tuple[str, str], list[str]] = {}

# Track agents deferred due to rate limits waiting for retry at the end of discussion
# Map: (channel_id, thread_ts) -> list[str] (agent keys)
RATE_LIMIT_RETRY_QUEUE: dict[tuple[str, str], list[str]] = {}

# Track agents that have already been retried once for this thread to prevent infinite retry loops
# Map: (channel_id, thread_ts) -> set[str] (agent keys)
RATE_LIMIT_ALREADY_RETRIED: dict[tuple[str, str], set[str]] = {}

# Track running asyncio Task for each active thread discussion
# Map: (channel_id, thread_ts) -> asyncio.Task
ACTIVE_THREAD_TASKS: dict[tuple[str, str], asyncio.Task] = {}

# Track discussion generation counter per thread to invalidate superseded tasks
# Map: (channel_id, thread_ts) -> int
THREAD_GENERATIONS: dict[tuple[str, str], int] = {}

# Track threads that are running a custom tagged agent sequence
CUSTOM_FLOW_THREADS: set[tuple[str, str]] = set()

# Track threads where a stop has been requested
STOP_REQUESTED_THREADS: set[tuple[str, str]] = set()

# Track conversation history limit and baseline index per thread
# Map: (channel_id, thread_ts) -> dict[str, int] (e.g. {"limit": 5, "prior_cutoff_index": 12})
THREAD_HISTORY_LIMITS: dict[tuple[str, str], dict[str, int]] = {}


def get_thread_history_limit(channel_id: str, thread_ts: str) -> dict[str, int] | None:
    """Returns the active history limit dict for this thread, or None."""
    return THREAD_HISTORY_LIMITS.get((channel_id, thread_ts))


def set_thread_history_limit(
    channel_id: str, thread_ts: str, limit: int, prior_cutoff_index: int
) -> None:
    """Sets the history limit and prior cutoff index for this thread."""
    THREAD_HISTORY_LIMITS[(channel_id, thread_ts)] = {
        "limit": limit,
        "prior_cutoff_index": prior_cutoff_index,
    }
    logger.info(
        f"[THREAD STATE] Set history limit for thread {(channel_id, thread_ts)}: limit={limit}, prior_cutoff_index={prior_cutoff_index}"
    )


def clear_thread_history_limit(channel_id: str, thread_ts: str) -> None:
    """Clears any active history limit for this thread."""
    THREAD_HISTORY_LIMITS.pop((channel_id, thread_ts), None)


# Track conversation max words limit per thread
# Map: (channel_id, thread_ts) -> int
THREAD_MAX_WORDS_LIMITS: dict[tuple[str, str], int] = {}


def get_thread_max_words(channel_id: str, thread_ts: str) -> int | None:
    """Returns the active max_words limit for this thread, or None."""
    return THREAD_MAX_WORDS_LIMITS.get((channel_id, thread_ts))


def set_thread_max_words(channel_id: str, thread_ts: str, max_words: int) -> None:
    """Sets the max_words limit for this thread."""
    THREAD_MAX_WORDS_LIMITS[(channel_id, thread_ts)] = max_words
    logger.info(
        f"[THREAD STATE] Set max_words limit for thread {(channel_id, thread_ts)}: max_words={max_words}"
    )


def clear_thread_max_words(channel_id: str, thread_ts: str) -> None:
    """Clears any active max_words limit for this thread."""
    THREAD_MAX_WORDS_LIMITS.pop((channel_id, thread_ts), None)


# Track conversation min words limit per thread
# Map: (channel_id, thread_ts) -> int
THREAD_MIN_WORDS_LIMITS: dict[tuple[str, str], int] = {}


def get_thread_min_words(channel_id: str, thread_ts: str) -> int | None:
    """Returns the active min_words limit for this thread, or None."""
    return THREAD_MIN_WORDS_LIMITS.get((channel_id, thread_ts))


def set_thread_min_words(channel_id: str, thread_ts: str, min_words: int) -> None:
    """Sets the min_words limit for this thread."""
    THREAD_MIN_WORDS_LIMITS[(channel_id, thread_ts)] = min_words
    logger.info(
        f"[THREAD STATE] Set min_words limit for thread {(channel_id, thread_ts)}: min_words={min_words}"
    )


def clear_thread_min_words(channel_id: str, thread_ts: str) -> None:
    """Clears any active min_words limit for this thread."""
    THREAD_MIN_WORDS_LIMITS.pop((channel_id, thread_ts), None)


# Track on-demand file context per thread discussion
# Map: (channel_id, thread_ts) -> dict (e.g. {"file_name": str, "content": str, "size": int, "truncated": bool, "authorized_agents": set[str] | None})
THREAD_ACTIVE_FILE_CONTEXT: dict[tuple[str, str], dict[str, Any]] = {}


def get_thread_file_context(channel_id: str, thread_ts: str) -> dict[str, Any] | None:
    """Returns the active file context for this thread discussion, or None."""
    return THREAD_ACTIVE_FILE_CONTEXT.get((channel_id, thread_ts))


def set_thread_file_context(
    channel_id: str, thread_ts: str, file_context: dict[str, Any]
) -> None:
    """Sets the active file context for this thread discussion."""
    THREAD_ACTIVE_FILE_CONTEXT[(channel_id, thread_ts)] = file_context
    logger.info(
        f"[THREAD STATE] Set active file context for thread {(channel_id, thread_ts)}: file='{file_context.get('file_name')}', authorized_agents={file_context.get('authorized_agents')}"
    )


def clear_thread_file_context(channel_id: str, thread_ts: str) -> None:
    """Clears any active file context for this thread."""
    THREAD_ACTIVE_FILE_CONTEXT.pop((channel_id, thread_ts), None)


def is_discussion_active(channel_id: str, thread_ts: str) -> bool:
    """
    Checks if there is an active (ongoing, not completed) discussion in this thread.
    A discussion is active if:
    - An asyncio.Task is currently executing for this thread and not done, OR
    - Intermediate agents are remaining in REMAINING_AGENTS_MAP for this thread, OR
    - Pending rate-limit retries exist in RATE_LIMIT_RETRY_QUEUE.
    """
    thread_key = (channel_id, thread_ts)
    task = ACTIVE_THREAD_TASKS.get(thread_key)
    if task and not task.done():
        return True
    if REMAINING_AGENTS_MAP.get(thread_key):
        return True
    return bool(RATE_LIMIT_RETRY_QUEUE.get(thread_key))


def interrupt_thread_discussion(channel_id: str, thread_ts: str) -> None:
    """
    Interrupts and cancels the current ongoing discussion in the specified thread:
    1. Increments the thread generation counter so in-flight tasks become superseded and abort.
    2. Cancels any active asyncio.Task currently processing for this thread.
    3. Clears remaining agents, custom flows, retry queues, and history limits.
    4. Clears any stop requested flag for this thread so the new query can run cleanly.
    """
    thread_key = (channel_id, thread_ts)
    THREAD_GENERATIONS[thread_key] = THREAD_GENERATIONS.get(thread_key, 0) + 1
    logger.info(
        f"[THREAD STATE] Interrupting discussion for thread {thread_key}. New generation: {THREAD_GENERATIONS[thread_key]}"
    )

    task = ACTIVE_THREAD_TASKS.pop(thread_key, None)
    if task and not task.done() and task is not asyncio.current_task():
        logger.info(
            f"[THREAD STATE] Cancelling active background task for thread {thread_key}"
        )
        task.cancel()

    REMAINING_AGENTS_MAP.pop(thread_key, None)
    RATE_LIMIT_RETRY_QUEUE.pop(thread_key, None)
    RATE_LIMIT_ALREADY_RETRIED.pop(thread_key, None)
    THREAD_HISTORY_LIMITS.pop(thread_key, None)
    THREAD_ACTIVE_FILE_CONTEXT.pop(thread_key, None)
    CUSTOM_FLOW_THREADS.discard(thread_key)
    STOP_REQUESTED_THREADS.discard(thread_key)


def request_thread_stop(channel_id: str, thread_ts: str) -> None:
    """Marks the thread for stopping and clears remaining queued agents."""
    thread_key = (channel_id, thread_ts)
    STOP_REQUESTED_THREADS.add(thread_key)
    REMAINING_AGENTS_MAP.pop(thread_key, None)
    RATE_LIMIT_RETRY_QUEUE.pop(thread_key, None)
    RATE_LIMIT_ALREADY_RETRIED.pop(thread_key, None)
    THREAD_HISTORY_LIMITS.pop(thread_key, None)
    CUSTOM_FLOW_THREADS.discard(thread_key)
    logger.info(
        f"[THREAD STATE] Stop requested for thread {thread_key}. REMAINING_AGENTS_MAP, retry queues, and history limits cleared."
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
        logger.debug(f"[THREAD STATE] Could not finalize thread note or usage on stop: {e}")


def is_thread_stopped(channel_id: str, thread_ts: str) -> bool:
    """Checks if a stop has been requested for this thread."""
    return (channel_id, thread_ts) in STOP_REQUESTED_THREADS


def clear_thread_stop(channel_id: str, thread_ts: str) -> None:
    """Clears the stop flag for this thread."""
    STOP_REQUESTED_THREADS.discard((channel_id, thread_ts))


def clear_thread_state(channel_id: str, thread_ts: str) -> None:
    """
    Cleans up all thread-level discussion state maps and flags
    when a thread completes, is stopped, or hits the circuit breaker.
    """
    thread_key = (channel_id, thread_ts)
    clear_thread_stop(channel_id, thread_ts)
    clear_thread_history_limit(channel_id, thread_ts)
    clear_thread_max_words(channel_id, thread_ts)
    clear_thread_min_words(channel_id, thread_ts)
    clear_thread_file_context(channel_id, thread_ts)
    CUSTOM_FLOW_THREADS.discard(thread_key)
    REMAINING_AGENTS_MAP.pop(thread_key, None)
    RATE_LIMIT_RETRY_QUEUE.pop(thread_key, None)
    RATE_LIMIT_ALREADY_RETRIED.pop(thread_key, None)
