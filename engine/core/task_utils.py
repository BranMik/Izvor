import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)

_ACTIVE_TASKS: set[asyncio.Task[Any]] = set()


def safe_create_task(
    coro: Coroutine[Any, Any, Any],
    name: str | None = None,
) -> asyncio.Task[Any]:
    """
    Creates an asyncio.Task while retaining a strong module-level reference
    until completion to prevent premature garbage collection.
    Hooks a done callback to log unhandled exceptions.
    """
    task = asyncio.create_task(coro, name=name)
    _ACTIVE_TASKS.add(task)

    def _done_callback(t: asyncio.Task[Any]) -> None:
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


def safe_cancel_task(
    task: asyncio.Task[Any] | None,
    *,
    skip_task: asyncio.Task[Any] | None = None,
) -> bool:
    """
    Thread-safe task cancellation helper.
    Safe to call from asyncio worker threads (e.g., asyncio.to_thread) that have no running event loop.
    Returns True if cancellation was executed or scheduled.
    """
    if task is None or task.done() or task is skip_task:
        return False

    running_loop: asyncio.AbstractEventLoop | None = None
    current_task: asyncio.Task[Any] | None = None
    try:
        running_loop = asyncio.get_running_loop()
        current_task = asyncio.current_task(running_loop)
    except RuntimeError:
        running_loop = None
        current_task = None

    if task is current_task:
        return False

    try:
        task_loop = task.get_loop()
    except AttributeError:
        task.cancel()
        return True

    if running_loop is task_loop:
        task.cancel()
        return True

    if task_loop.is_running():
        task_loop.call_soon_threadsafe(task.cancel)
        return True

    return False


