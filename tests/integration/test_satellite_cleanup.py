import asyncio

from agents.thread_notes_agent import (
    _PENDING_COMPLETED,
    _PENDING_UPDATES,
    THREAD_LOCKS,
    THREAD_NOTE_MSG_MAP,
)
from agents.usage_agent import usage_tracker
from orchestration.thread_state import thread_state_manager


def test_satellite_cleanup_on_thread_cleanup():
    channel_id = "C_TEST_SATELLITE"
    thread_ts = "12345.67890"
    thread_key = (channel_id, thread_ts)

    # Populate satellite state
    THREAD_NOTE_MSG_MAP[thread_key] = "12345.67891"
    THREAD_LOCKS[thread_key] = asyncio.Lock()
    _PENDING_UPDATES[thread_key] = True
    _PENDING_COMPLETED[thread_key] = True

    usage_tracker.usage_locks[thread_key] = asyncio.Lock()
    usage_tracker.pending_usage_updates[thread_key] = True
    usage_tracker.pending_usage_completed[thread_key] = True

    # Trigger thread cleanup
    thread_state_manager.cleanup_thread(channel_id, thread_ts)

    # Verify thread_notes_agent satellite state is cleaned
    assert thread_key not in THREAD_NOTE_MSG_MAP
    assert thread_key not in THREAD_LOCKS
    assert thread_key not in _PENDING_UPDATES
    assert thread_key not in _PENDING_COMPLETED

    # Verify usage_agent satellite state is cleaned
    assert thread_key not in usage_tracker.usage_locks
    assert thread_key not in usage_tracker.pending_usage_updates
    assert thread_key not in usage_tracker.pending_usage_completed


def test_satellite_cleanup_on_reset_all():
    k1 = ("C_RESET_1", "100.001")
    k2 = ("C_RESET_2", "200.002")

    THREAD_NOTE_MSG_MAP[k1] = "100.002"
    THREAD_LOCKS[k1] = asyncio.Lock()
    usage_tracker.usage_locks[k2] = asyncio.Lock()

    thread_state_manager.reset_all()

    assert k1 not in THREAD_NOTE_MSG_MAP
    assert k1 not in THREAD_LOCKS
    assert k2 not in usage_tracker.usage_locks
