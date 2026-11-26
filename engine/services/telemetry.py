"""
Telemetry and execution span tracing service for TurnPipeline stages.
Records stage-by-stage latencies, tokens, tools, and outcomes to SQLite WAL turn_traces table
using a periodic / threshold batch flusher.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import threading
import time
import uuid
from typing import Any

from core.database import get_connection, get_db_transaction
from core.task_utils import safe_cancel_task

logger = logging.getLogger(__name__)

MAX_TRACE_BUFFER_SIZE = 10_000
TRACE_FLUSH_BATCH_SIZE = 50
TRACE_FLUSH_INTERVAL_SECONDS = 5.0

_TRACE_BUFFER: collections.deque[tuple[Any, ...]] = collections.deque(maxlen=MAX_TRACE_BUFFER_SIZE)
_TRACE_BUFFER_LOCK = threading.Lock()
_FLUSH_LOCK = threading.Lock()

_TOTAL_FLUSHED: int = 0
_TOTAL_DROPPED: int = 0
_TOTAL_FLUSH_ERRORS: int = 0

_FLUSHER_LOCK = threading.Lock()
_FLUSHER_TASK: asyncio.Task[None] | None = None
_FLUSHER_LOOP: asyncio.AbstractEventLoop | None = None
_FLUSH_EVENT: asyncio.Event | None = None
_STOP_EVENT: asyncio.Event | None = None
_ACTIVE_BATCH_SIZE: int = TRACE_FLUSH_BATCH_SIZE


def get_telemetry_stats() -> dict[str, int]:
    """Returns current telemetry buffer depth and cumulative flush/drop/error counters."""
    with _TRACE_BUFFER_LOCK:
        return {
            "buffer_depth": len(_TRACE_BUFFER),
            "total_flushed": _TOTAL_FLUSHED,
            "total_dropped": _TOTAL_DROPPED,
            "total_flush_errors": _TOTAL_FLUSH_ERRORS,
        }


def flush_turn_traces() -> int:
    """Flushes all queued turn traces to SQLite in a single transaction using executemany."""
    global _TOTAL_FLUSHED, _TOTAL_FLUSH_ERRORS

    with _FLUSH_LOCK:
        items: list[tuple[Any, ...]] = []
        with _TRACE_BUFFER_LOCK:
            while _TRACE_BUFFER:
                items.append(_TRACE_BUFFER.popleft())

        if not items:
            return 0

        try:
            with get_db_transaction(immediate=True) as conn:
                conn.executemany(
                    """
                    INSERT INTO turn_traces (
                        trace_id, session_id, channel_id, thread_ts, agent_key,
                        stage_name, duration_ms, prompt_tokens, completion_tokens,
                        tools_called, status, error_message, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    items,
                )
            with _TRACE_BUFFER_LOCK:
                _TOTAL_FLUSHED += len(items)
            return len(items)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[TELEMETRY] Failed to batch flush turn traces: {e}")
            with _TRACE_BUFFER_LOCK:
                _TOTAL_FLUSH_ERRORS += 1
                for item in reversed(items):
                    if len(_TRACE_BUFFER) < MAX_TRACE_BUFFER_SIZE:
                        _TRACE_BUFFER.appendleft(item)
                    else:
                        break
            return 0


def _signal_flusher_if_active() -> bool:
    """Signals the active background flusher event loop if running; returns True if signaled."""
    with _FLUSHER_LOCK:
        task = _FLUSHER_TASK
        loop = _FLUSHER_LOOP
        flush_event = _FLUSH_EVENT
    if task is not None and not task.done() and loop is not None and loop.is_running() and flush_event is not None:
        try:
            loop.call_soon_threadsafe(flush_event.set)
            return True
        except RuntimeError:
            return False
    return False


def record_turn_trace(
    session_id: str,
    channel_id: str,
    thread_ts: str,
    agent_key: str,
    stage_name: str,
    duration_ms: float,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    tools_called: list[str] | None = None,
    status: str = "SUCCESS",
    error_message: str | None = None,
) -> str:
    """Queues a turn execution span into the write-behind buffer and triggers batch flush at threshold."""
    global _TOTAL_DROPPED

    trace_id = str(uuid.uuid4())
    now = time.time()
    tools_json = json.dumps(tools_called or [])
    item = (
        trace_id,
        session_id,
        channel_id,
        thread_ts,
        agent_key,
        stage_name,
        float(duration_ms),
        int(prompt_tokens),
        int(completion_tokens),
        tools_json,
        status,
        error_message,
        now,
    )
    with _TRACE_BUFFER_LOCK:
        if len(_TRACE_BUFFER) >= MAX_TRACE_BUFFER_SIZE:
            _TOTAL_DROPPED += 1
        _TRACE_BUFFER.append(item)
        buffer_len = len(_TRACE_BUFFER)
        threshold = _ACTIVE_BATCH_SIZE

    if buffer_len >= threshold:
        if not _signal_flusher_if_active():
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                flush_turn_traces()

    return trace_id


async def _telemetry_flusher_loop(
    interval_seconds: float,
    flush_event: asyncio.Event,
    stop_event: asyncio.Event,
) -> None:
    """Single-flight background worker flushing buffered traces periodically or when threshold is reached."""
    effective_interval = max(0.05, float(interval_seconds))
    while not stop_event.is_set():
        flush_task = asyncio.create_task(flush_event.wait())
        stop_task = asyncio.create_task(stop_event.wait())
        try:
            await asyncio.wait(
                {flush_task, stop_task},
                timeout=effective_interval,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for pending_waiter in (flush_task, stop_task):
                if not pending_waiter.done():
                    pending_waiter.cancel()
            await asyncio.gather(flush_task, stop_task, return_exceptions=True)

        flush_event.clear()
        await asyncio.to_thread(flush_turn_traces)


def start_telemetry_flusher(
    interval_seconds: float = TRACE_FLUSH_INTERVAL_SECONDS,
    batch_size: int = TRACE_FLUSH_BATCH_SIZE,
) -> asyncio.Task[None]:
    """Starts the loop-aware background telemetry batch flusher (idempotent per active loop)."""
    global _FLUSHER_TASK, _FLUSHER_LOOP, _FLUSH_EVENT, _STOP_EVENT, _ACTIVE_BATCH_SIZE

    loop = asyncio.get_running_loop()
    with _FLUSHER_LOCK:
        _ACTIVE_BATCH_SIZE = max(1, int(batch_size))
        if (
            _FLUSHER_TASK is not None
            and not _FLUSHER_TASK.done()
            and _FLUSHER_LOOP is loop
        ):
            return _FLUSHER_TASK

        if _FLUSHER_TASK is not None and not _FLUSHER_TASK.done():
            safe_cancel_task(_FLUSHER_TASK)

        flush_event = asyncio.Event()
        stop_event = asyncio.Event()
        task = loop.create_task(
            _telemetry_flusher_loop(interval_seconds, flush_event, stop_event),
            name="telemetry_batch_flusher",
        )
        _FLUSHER_LOOP = loop
        _FLUSH_EVENT = flush_event
        _STOP_EVENT = stop_event
        _FLUSHER_TASK = task
        return task


async def stop_telemetry_flusher(timeout: float = 2.0) -> None:
    """Stops the background telemetry batch flusher gracefully if active."""
    global _FLUSHER_TASK, _FLUSHER_LOOP, _FLUSH_EVENT, _STOP_EVENT

    with _FLUSHER_LOCK:
        task = _FLUSHER_TASK
        loop = _FLUSHER_LOOP
        stop_event = _STOP_EVENT
        _FLUSHER_TASK = None
        _FLUSHER_LOOP = None
        _FLUSH_EVENT = None
        _STOP_EVENT = None

    if task is None:
        return

    current_loop = asyncio.get_running_loop()
    if stop_event is not None and loop is current_loop:
        stop_event.set()
    elif stop_event is not None and loop is not None and loop.is_running():
        loop.call_soon_threadsafe(stop_event.set)
    else:
        safe_cancel_task(task)
        return

    if loop is current_loop and not task.done():
        try:
            await asyncio.wait_for(task, timeout=timeout)
        except TimeoutError:
            safe_cancel_task(task)
            await asyncio.gather(task, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[TELEMETRY] Error stopping telemetry flusher: {e}")


async def drain_turn_traces(timeout: float = 2.0) -> None:
    """Stops the background flusher (if active) and synchronously flushes all remaining buffered traces."""
    await stop_telemetry_flusher(timeout=timeout)
    await asyncio.to_thread(flush_turn_traces)


def reset_telemetry_state() -> None:
    """Stops any lingering flusher task and resets buffers/counters (for test isolation)."""
    global _FLUSHER_TASK, _FLUSHER_LOOP, _FLUSH_EVENT, _STOP_EVENT
    global _ACTIVE_BATCH_SIZE, _TOTAL_FLUSHED, _TOTAL_DROPPED, _TOTAL_FLUSH_ERRORS

    with _FLUSHER_LOCK:
        task = _FLUSHER_TASK
        _FLUSHER_TASK = None
        _FLUSHER_LOOP = None
        _FLUSH_EVENT = None
        _STOP_EVENT = None
        _ACTIVE_BATCH_SIZE = TRACE_FLUSH_BATCH_SIZE

    if task is not None:
        safe_cancel_task(task)

    with _TRACE_BUFFER_LOCK:
        _TRACE_BUFFER.clear()
        _TOTAL_FLUSHED = 0
        _TOTAL_DROPPED = 0
        _TOTAL_FLUSH_ERRORS = 0


def clear_turn_traces(session_id: str | None = None) -> None:
    """Clears turn traces from both the in-memory buffer and SQLite."""
    with _TRACE_BUFFER_LOCK:
        if session_id:
            remaining = [item for item in _TRACE_BUFFER if item[1] != session_id]
            _TRACE_BUFFER.clear()
            _TRACE_BUFFER.extend(remaining)
        else:
            _TRACE_BUFFER.clear()

    try:
        with get_db_transaction(immediate=True) as conn:
            if session_id:
                conn.execute("DELETE FROM turn_traces WHERE session_id = ?", (session_id,))
            else:
                conn.execute("DELETE FROM turn_traces")
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[TELEMETRY] Failed to clear turn traces: {e}")


def get_turn_traces(
    session_id: str | None = None,
    channel_id: str | None = None,
    thread_ts: str | None = None,
    agent_key: str | None = None,
    status: str | None = None,
    since_ts: float | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Retrieves recorded turn traces ordered by creation time after flushing pending queue."""
    flush_turn_traces()
    clamped_limit = max(1, min(500, int(limit)))
    traces: list[dict[str, Any]] = []

    conditions: list[str] = []
    params: list[Any] = []
    if session_id:
        conditions.append("session_id = ?")
        params.append(session_id)
    if channel_id:
        conditions.append("channel_id = ?")
        params.append(channel_id)
    if thread_ts:
        conditions.append("thread_ts = ?")
        params.append(thread_ts)
    if agent_key:
        conditions.append("agent_key = ?")
        params.append(agent_key)
    if status:
        conditions.append("status = ?")
        params.append(status)
    if since_ts is not None:
        conditions.append("created_at >= ?")
        params.append(float(since_ts))

    where_sql = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    order_dir = "ASC" if (session_id or (channel_id and thread_ts)) else "DESC"
    query = f"SELECT * FROM turn_traces {where_sql} ORDER BY created_at {order_dir} LIMIT ?"
    params.append(clamped_limit)

    try:
        with get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            for r in rows:
                tools_raw = r["tools_called"]
                try:
                    tools = json.loads(tools_raw) if tools_raw else []
                except Exception:  # noqa: BLE001
                    tools = []
                traces.append({
                    "trace_id": r["trace_id"],
                    "session_id": r["session_id"],
                    "channel_id": r["channel_id"],
                    "thread_ts": r["thread_ts"],
                    "agent_key": r["agent_key"],
                    "stage_name": r["stage_name"],
                    "duration_ms": float(r["duration_ms"]),
                    "prompt_tokens": int(r["prompt_tokens"]),
                    "completion_tokens": int(r["completion_tokens"]),
                    "tools_called": tools,
                    "status": r["status"],
                    "error_message": r["error_message"],
                    "created_at": float(r["created_at"]),
                })
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[TELEMETRY] Failed to fetch turn traces: {e}")
    return traces
