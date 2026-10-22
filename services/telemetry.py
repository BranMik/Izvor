"""
Telemetry and execution span tracing service for TurnPipeline stages.
Records stage-by-stage latencies, tokens, tools, and outcomes to SQLite WAL turn_traces table
using a non-blocking write-behind queue.
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

logger = logging.getLogger(__name__)

_TRACE_BUFFER: collections.deque[tuple[Any, ...]] = collections.deque()
_TRACE_BUFFER_LOCK = threading.Lock()


def flush_turn_traces() -> int:
    """Flushes all queued turn traces to SQLite in a single transaction using executemany."""
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
        return len(items)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[TELEMETRY] Failed to batch flush turn traces: {e}")
        return 0


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
    """Queues a turn execution span into the write-behind buffer and triggers background flush."""
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
        _TRACE_BUFFER.append(item)

    try:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, flush_turn_traces)
    except RuntimeError:
        # No running event loop (synchronous context / test), flush immediately
        flush_turn_traces()

    return trace_id


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
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Retrieves recorded turn traces ordered by creation time after flushing pending queue."""
    flush_turn_traces()
    traces: list[dict[str, Any]] = []
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            if session_id:
                cursor.execute(
                    "SELECT * FROM turn_traces WHERE session_id = ? ORDER BY created_at ASC LIMIT ?",
                    (session_id, limit),
                )
            elif channel_id and thread_ts:
                cursor.execute(
                    "SELECT * FROM turn_traces WHERE channel_id = ? AND thread_ts = ? ORDER BY created_at ASC LIMIT ?",
                    (channel_id, thread_ts, limit),
                )
            else:
                cursor.execute(
                    "SELECT * FROM turn_traces ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
            rows = cursor.fetchall()
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
