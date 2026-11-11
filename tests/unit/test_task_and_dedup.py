import asyncio
import time
from unittest.mock import patch
import pytest

from adapters.slack.events import EventDeduplicationCache
from core.task_utils import safe_create_task, _ACTIVE_TASKS


def test_dedup_cache_partial_eviction_when_maxsize_exceeded():
    # Cache with maxsize=10
    cache = EventDeduplicationCache(ttl_seconds=3600.0, maxsize=10)

    # Insert 10 events with increasing timestamps
    base_time = time.time()
    for i in range(10):
        cache[f"event_{i}"] = base_time + i

    assert len(cache) == 10

    # Insert 11th item: this exceeds maxsize (11 > 10)
    # Old behavior would wipe entire dict (len -> 1).
    # New behavior evicts oldest 20% (max(1, int(11 * 0.2)) = 2 items).
    cache["event_10"] = base_time + 10

    # 11 - 2 = 9 items remain
    assert len(cache) == 9
    # Oldest items (event_0, event_1) were evicted
    assert "event_0" not in cache
    assert "event_1" not in cache
    # Recent items are safely retained
    assert "event_2" in cache
    assert "event_9" in cache
    assert "event_10" in cache


@pytest.mark.anyio
async def test_safe_create_task_lifecycle_normal():
    initial_count = len(_ACTIVE_TASKS)

    async def normal_task():
        await asyncio.sleep(0.01)
        return "done"

    task = safe_create_task(normal_task(), name="test_normal")
    assert task in _ACTIVE_TASKS
    assert len(_ACTIVE_TASKS) == initial_count + 1

    await task
    # Allow callback to run
    await asyncio.sleep(0.01)

    assert task not in _ACTIVE_TASKS
    assert len(_ACTIVE_TASKS) == initial_count


@pytest.mark.anyio
async def test_safe_create_task_exception_logged():
    initial_count = len(_ACTIVE_TASKS)

    async def failing_task():
        await asyncio.sleep(0.01)
        raise RuntimeError("Controlled test exception in background task")

    with patch("core.task_utils.logger.error") as mock_logger_error:
        task = safe_create_task(failing_task(), name="test_failing")
        assert task in _ACTIVE_TASKS

        # Await with gather/suppress or try/except
        try:
            await task
        except RuntimeError:
            pass

        await asyncio.sleep(0.01)

        # Verified strong reference cleaned up
        assert task not in _ACTIVE_TASKS
        assert len(_ACTIVE_TASKS) == initial_count

        # Verified logger intercepted and logged the error
        mock_logger_error.assert_called_once()
        assert "Controlled test exception" in str(mock_logger_error.call_args)
        assert "test_failing" in str(mock_logger_error.call_args)
