import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)

_ACTIVE_TASKS: set[asyncio.Task] = set()


def safe_create_task(
    coro: Coroutine[Any, Any, Any],
    name: str | None = None,
) -> asyncio.Task:
    """
    Creates an asyncio.Task while retaining a strong module-level reference
    until completion to prevent premature garbage collection.
    Hooks a done callback to log unhandled exceptions.
    """
    task = asyncio.create_task(coro, name=name)
    _ACTIVE_TASKS.add(task)

    def _done_callback(t: asyncio.Task) -> None:
        _ACTIVE_TASKS.discard(t)
        if not t.cancelled():
            exc = t.exception()
            if exc:
                task_name = t.get_name() if hasattr(t, "get_name") else name or "unnamed_task"
                logger.error(
                    f"[TASK SAFEGUARD] Background task '{task_name}' raised an unhandled exception: {exc}",
                    exc_info=exc,
                )

    task.add_done_callback(_done_callback)
    return task


def get_active_tasks_count() -> int:
    """Returns the current number of actively tracked background tasks."""
    return len(_ACTIVE_TASKS)
