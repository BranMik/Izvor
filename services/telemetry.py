"""
Telemetry and execution span tracing service for TurnPipeline stages.
Records stage-by-stage latencies, tokens, tools, and outcomes to SQLite WAL turn_traces table.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from core.database import get_connection, get_db_transaction

logger = logging.getLogger(__name__)


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
    """Inserts a turn execution span into the turn_traces table."""
    trace_id = str(uuid.uuid4())
    now = time.time()
    tools_json = json.dumps(tools_called or [])
    try:
        with get_db_transaction(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO turn_traces (
                    trace_id, session_id, channel_id, thread_ts, agent_key,
                    stage_name, duration_ms, prompt_tokens, completion_tokens,
                    tools_called, status, error_message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
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
                ),
            )
    except Exception as e:
        logger.debug(f"[TELEMETRY] Failed to record turn trace: {e}")
    return trace_id


def get_turn_traces(
    session_id: str | None = None,
    channel_id: str | None = None,
    thread_ts: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Retrieves recorded turn traces ordered by creation time."""
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
                except Exception:
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
    except Exception as e:
        logger.debug(f"[TELEMETRY] Failed to fetch turn traces: {e}")
    return traces
