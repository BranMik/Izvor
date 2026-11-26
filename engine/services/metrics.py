"""
Zero-dependency Prometheus text exposition metrics renderer (Tier L2).
Reads readiness, active task count, telemetry buffer stats, and read-only SQLite aggregates.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from core.database import get_connection
from core.task_utils import _ACTIVE_TASKS
from services.readiness import readiness_registry
from services.telemetry import get_telemetry_stats

logger = logging.getLogger(__name__)


def _escape_label_value(value: Any) -> str:
    """Escapes a Prometheus label value per the text exposition format spec."""
    text = str(value if value is not None else "")
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_labels(labels: dict[str, Any] | None) -> str:
    if not labels:
        return ""
    parts = [f'{k}="{_escape_label_value(v)}"' for k, v in labels.items()]
    return "{" + ",".join(parts) + "}"


def _emit_family(
    lines: list[str],
    name: str,
    metric_type: str,
    help_text: str,
    samples: list[tuple[dict[str, Any] | None, int | float]],
) -> None:
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} {metric_type}")
    for labels, value in samples:
        lines.append(f"{name}{_format_labels(labels)} {value}")


def render_prometheus_metrics() -> str:
    """Renders current application health, telemetry, and spend metrics in Prometheus v0.0.4 text format."""
    lines: list[str] = []

    readiness = readiness_registry.get_summary()
    _emit_family(
        lines,
        "izvor_up",
        "gauge",
        "Whether Izvor is critically ready to serve traffic (1 = ready/degraded, 0 = starting/unhealthy).",
        [(None, 1 if readiness["ready"] else 0)],
    )

    comp_samples: list[tuple[dict[str, Any] | None, int | float]] = []
    for comp_name, comp_info in readiness["components"].items():
        comp_samples.append(
            (
                {
                    "component": comp_name,
                    "status": comp_info["status"],
                    "critical": str(bool(comp_info["critical"])).lower(),
                },
                1 if comp_info["status"] == "ready" else 0,
            )
        )
    if comp_samples:
        _emit_family(
            lines,
            "izvor_readiness_component_status",
            "gauge",
            "Readiness status per subsystem (1 = ready, 0 = pending/degraded/failed).",
            comp_samples,
        )

    active_tasks = sum(1 for t in list(_ACTIVE_TASKS) if not t.done())
    _emit_family(
        lines,
        "izvor_active_background_tasks",
        "gauge",
        "Number of currently active tracked background asyncio tasks.",
        [(None, active_tasks)],
    )

    t_stats = get_telemetry_stats()
    _emit_family(
        lines,
        "izvor_telemetry_buffer_depth",
        "gauge",
        "Current number of buffered turn trace spans waiting to flush.",
        [(None, t_stats["buffer_depth"])],
    )
    _emit_family(
        lines,
        "izvor_telemetry_flushed_total",
        "counter",
        "Total number of turn trace spans flushed to SQLite.",
        [(None, t_stats["total_flushed"])],
    )
    _emit_family(
        lines,
        "izvor_telemetry_dropped_total",
        "counter",
        "Total number of turn trace spans dropped due to buffer saturation.",
        [(None, t_stats["total_dropped"])],
    )
    _emit_family(
        lines,
        "izvor_telemetry_flush_errors_total",
        "counter",
        "Total number of turn trace batch flush errors.",
        [(None, t_stats["total_flush_errors"])],
    )

    session_samples: list[tuple[dict[str, Any] | None, int | float]] = []
    trace_count_samples: list[tuple[dict[str, Any] | None, int | float]] = []
    trace_dur_sum_samples: list[tuple[dict[str, Any] | None, int | float]] = []
    trace_dur_cnt_samples: list[tuple[dict[str, Any] | None, int | float]] = []
    spend_usd_total = 0.0
    prompt_tokens_total = 0
    completion_tokens_total = 0
    checkpoint_samples: list[tuple[dict[str, Any] | None, int | float]] = []

    try:
        with get_connection() as conn:
            for row in conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM thread_sessions GROUP BY status;"
            ).fetchall():
                session_samples.append(({"status": row["status"]}, int(row["cnt"])))

            for row in conn.execute(
                """
                SELECT stage_name, status, COUNT(*) AS cnt, COALESCE(SUM(duration_ms), 0.0) AS dur_sum
                FROM turn_traces
                GROUP BY stage_name, status;
                """
            ).fetchall():
                lbl = {"stage_name": row["stage_name"], "status": row["status"]}
                cnt = int(row["cnt"])
                trace_count_samples.append((lbl, cnt))
                trace_dur_cnt_samples.append((lbl, cnt))
                trace_dur_sum_samples.append((lbl, round(float(row["dur_sum"]), 3)))

            spend_row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(cost_usd), 0.0) AS total_usd,
                    COALESCE(SUM(prompt_tokens), 0) AS total_prompt,
                    COALESCE(SUM(completion_tokens), 0) AS total_completion
                FROM spend_ledger;
                """
            ).fetchone()
            if spend_row is not None:
                spend_usd_total = round(float(spend_row["total_usd"]), 6)
                prompt_tokens_total = int(spend_row["total_prompt"])
                completion_tokens_total = int(spend_row["total_completion"])

            for row in conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM turn_checkpoints GROUP BY status;"
            ).fetchall():
                checkpoint_samples.append(({"status": row["status"]}, int(row["cnt"])))
    except sqlite3.Error as db_err:
        logger.debug("[METRICS] Failed querying SQLite aggregate metrics: %s", db_err)

    _emit_family(
        lines,
        "izvor_thread_sessions_total",
        "gauge",
        "Persisted thread sessions grouped by status.",
        session_samples,
    )
    _emit_family(
        lines,
        "izvor_turn_traces_total",
        "counter",
        "Recorded TurnPipeline stage spans grouped by stage_name and status.",
        trace_count_samples,
    )
    _emit_family(
        lines,
        "izvor_turn_trace_duration_ms_sum",
        "counter",
        "Cumulative TurnPipeline stage duration in milliseconds grouped by stage_name and status.",
        trace_dur_sum_samples,
    )
    _emit_family(
        lines,
        "izvor_turn_trace_duration_ms_count",
        "counter",
        "Count of TurnPipeline stage duration observations grouped by stage_name and status.",
        trace_dur_cnt_samples,
    )
    _emit_family(
        lines,
        "izvor_spend_usd_total",
        "counter",
        "Total recorded LLM spend in USD across the spend_ledger.",
        [(None, spend_usd_total)],
    )
    _emit_family(
        lines,
        "izvor_tokens_total",
        "counter",
        "Total recorded LLM tokens grouped by token_type (prompt or completion).",
        [
            ({"token_type": "prompt"}, prompt_tokens_total),
            ({"token_type": "completion"}, completion_tokens_total),
        ],
    )
    _emit_family(
        lines,
        "izvor_turn_checkpoints_total",
        "gauge",
        "Persisted turn checkpoints grouped by status.",
        checkpoint_samples,
    )

    return "\n".join(lines) + "\n"

