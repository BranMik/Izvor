"""Async utilities for task tracking and background execution."""

from core.task_utils import (
    _ACTIVE_TASKS,
    _BACKGROUND_TASKS,
    get_active_tasks_count,
    safe_create_task,
    spawn_background_task,
)

__all__ = [
    "_ACTIVE_TASKS",
    "_BACKGROUND_TASKS",
    "get_active_tasks_count",
    "safe_create_task",
    "spawn_background_task",
]
