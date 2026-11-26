"""Unit tests for periodic/threshold telemetry batch flusher and trace filtering (TKT-1241)."""

from __future__ import annotations

import asyncio
import time

import pytest

from services.telemetry import (
    clear_turn_traces,
    drain_turn_traces,
    flush_turn_traces,
    get_telemetry_stats,
    get_turn_traces,
    record_turn_trace,
    start_telemetry_flusher,
    stop_telemetry_flusher,
)


@pytest.mark.anyio
async def test_telemetry_batch_flusher_threshold_and_shutdown_drain() -> None:
    task = start_telemetry_flusher(interval_seconds=60.0, batch_size=5)
    try:
        for idx in range(5):
            record_turn_trace(
                session_id="C_TEL:100.1",
                channel_id="C_TEL",
                thread_ts="100.1",
                agent_key="agent-10001",
                stage_name="execution",
                duration_ms=10.0 + idx,
                status="SUCCESS",
            )

        # Wait briefly for threshold wakeup to flush in a single batch
        deadline = time.monotonic() + 3.0
        while get_telemetry_stats()["total_flushed"] < 5 and time.monotonic() < deadline:
            await asyncio.sleep(0.02)

        stats = get_telemetry_stats()
        assert stats["total_flushed"] == 5
        assert stats["buffer_depth"] == 0

        # Queue one sub-threshold span and verify drain_turn_traces flushes it cleanly
        record_turn_trace(
            session_id="C_TEL:100.1",
            channel_id="C_TEL",
            thread_ts="100.1",
            agent_key="agent-10002",
            stage_name="post_record",
            duration_ms=4.0,
            status="HALTED",
        )
        assert get_telemetry_stats()["buffer_depth"] == 1
        await drain_turn_traces(timeout=2.0)
        assert task.done()
        assert get_telemetry_stats()["total_flushed"] == 6
        assert get_telemetry_stats()["buffer_depth"] == 0

        all_rows = get_turn_traces(channel_id="C_TEL", thread_ts="100.1", limit=20)
        assert len(all_rows) == 6
        halted_rows = get_turn_traces(agent_key="agent-10002", status="HALTED", limit=20)
        assert len(halted_rows) == 1
        assert halted_rows[0]["stage_name"] == "post_record"
    finally:
        await stop_telemetry_flusher(timeout=1.0)
        clear_turn_traces("C_TEL:100.1")


@pytest.mark.anyio
async def test_telemetry_periodic_interval_flush() -> None:
    start_telemetry_flusher(interval_seconds=0.08, batch_size=100)
    try:
        record_turn_trace(
            session_id="C_TEL:200.2",
            channel_id="C_TEL",
            thread_ts="200.2",
            agent_key="agent-10001",
            stage_name="validation",
            duration_ms=2.5,
        )
        deadline = time.monotonic() + 3.0
        while get_telemetry_stats()["total_flushed"] < 1 and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert get_telemetry_stats()["total_flushed"] == 1
        assert get_telemetry_stats()["buffer_depth"] == 0
    finally:
        await stop_telemetry_flusher(timeout=1.0)
        clear_turn_traces("C_TEL:200.2")


def test_telemetry_sync_flush_and_since_filter() -> None:
    t0 = time.time() - 5.0
    record_turn_trace(
        session_id="C_SYNC:1.0",
        channel_id="C_SYNC",
        thread_ts="1.0",
        agent_key="agent-10003",
        stage_name="context_prep",
        duration_ms=7.0,
    )
    flushed = flush_turn_traces()
    assert flushed == 1
    rows = get_turn_traces(agent_key="agent-10003", since_ts=t0, limit=10)
    assert len(rows) == 1
    assert rows[0]["agent_key"] == "agent-10003"
    clear_turn_traces("C_SYNC:1.0")

