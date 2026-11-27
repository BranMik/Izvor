from __future__ import annotations

from pathlib import Path

from tests.evals.models import BaselineDiff, EvalRunReport, SuiteDelta


def load_report(path: Path) -> EvalRunReport:
    """Loads an EvalRunReport JSON file from disk."""
    raw = path.read_text(encoding="utf-8")
    return EvalRunReport.model_validate_json(raw)


def save_baseline(
    report: EvalRunReport,
    results_dir: Path,
    name: str = "baseline",
) -> Path:
    """Persists a report snapshot as a named baseline JSON file in results_dir."""
    results_dir.mkdir(parents=True, exist_ok=True)
    clean_name = name[:-5] if name.endswith(".json") else name
    target = results_dir / f"{clean_name}.json"
    target.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return target


def compare_against_baseline(
    current: EvalRunReport,
    baseline: EvalRunReport,
    regression_tolerance: float = 0.05,
) -> BaselineDiff:
    """Computes overall and per-suite score/cost/latency deltas and identifies regressions."""
    tol = max(0.0, float(regression_tolerance))
    overall_delta = round(current.overall_score - baseline.overall_score, 4)
    cost_delta = round(current.total_cost_usd - baseline.total_cost_usd, 6)
    duration_delta = round(current.total_duration_seconds - baseline.total_duration_seconds, 3)

    suite_deltas: dict[str, SuiteDelta] = {}
    regressions: list[str] = []

    if overall_delta < -tol:
        regressions.append(
            f"overall_score regressed by {overall_delta:+.4f} "
            f"({baseline.overall_score:.4f} -> {current.overall_score:.4f}, tol={tol:.4f})"
        )

    for suite_name, cur_suite in current.suites.items():
        base_suite = baseline.suites.get(suite_name)
        if base_suite is None:
            continue
        score_delta = round(cur_suite.mean_score - base_suite.mean_score, 4)
        s_cost_delta = round(cur_suite.total_cost_usd - base_suite.total_cost_usd, 6)
        s_dur_delta = round(cur_suite.total_duration_seconds - base_suite.total_duration_seconds, 3)

        metric_deltas: dict[str, float] = {}
        for m_key, cur_val in cur_suite.aggregate_metrics.items():
            base_val = base_suite.aggregate_metrics.get(m_key)
            if (
                isinstance(cur_val, (int, float))
                and not isinstance(cur_val, bool)
                and isinstance(base_val, (int, float))
                and not isinstance(base_val, bool)
            ):
                metric_deltas[m_key] = round(float(cur_val) - float(base_val), 4)

        suite_deltas[suite_name] = SuiteDelta(
            suite=suite_name,
            baseline_score=base_suite.mean_score,
            current_score=cur_suite.mean_score,
            score_delta=score_delta,
            baseline_cost_usd=base_suite.total_cost_usd,
            current_cost_usd=cur_suite.total_cost_usd,
            cost_delta_usd=s_cost_delta,
            baseline_duration_seconds=base_suite.total_duration_seconds,
            current_duration_seconds=cur_suite.total_duration_seconds,
            duration_delta_seconds=s_dur_delta,
            metric_deltas=metric_deltas,
        )

        if score_delta < -tol:
            regressions.append(
                f"suite '{suite_name}' regressed by {score_delta:+.4f} "
                f"({base_suite.mean_score:.4f} -> {cur_suite.mean_score:.4f}, tol={tol:.4f})"
            )

    return BaselineDiff(
        baseline_run_id=baseline.run_id,
        current_run_id=current.run_id,
        overall_score_delta=overall_delta,
        total_cost_delta_usd=cost_delta,
        total_duration_delta_seconds=duration_delta,
        suite_deltas=suite_deltas,
        regressions=regressions,
    )


def render_markdown_report(report: EvalRunReport, diff: BaselineDiff | None = None) -> str:
    """Renders an EvalRunReport (and optional baseline diff) as structured Markdown."""
    lines = [
        f"# Izvor AI Evaluation Report — `{report.run_id}`",
        "",
        f"- **Timestamp (UTC):** {report.timestamp_utc}",
        f"- **Judge Model:** `{report.judge_model}`",
        f"- **Overall Score:** `{report.overall_score:.4f}` (`{report.overall_score * 5.0:.2f} / 5.0`)",
        f"- **Total Spend:** `${report.total_cost_usd:.6f}` (cap: `${report.max_cost_usd:.2f}`, halted: `{report.budget_halted}`)",
        f"- **Total Duration:** `{report.total_duration_seconds:.2f}s`",
        "",
        "## Suite Summary",
        "",
        "| Suite | Cases | Trials | Mean Score | Rating (/5) | Cost (USD) | Duration (s) | Key Metrics |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]

    for suite_name, summary in report.suites.items():
        metrics_str = ", ".join(f"{k}={v}" for k, v in summary.aggregate_metrics.items()) or "-"
        lines.append(
            f"| `{suite_name}` | {summary.total_cases} | {summary.trials_per_case} | "
            f"{summary.mean_score:.4f} | {summary.mean_rating_5:.2f} | "
            f"${summary.total_cost_usd:.6f} | {summary.total_duration_seconds:.2f} | {metrics_str} |"
        )

    if diff is not None:
        lines.extend([
            "",
            f"## Baseline Comparison (`{diff.baseline_run_id}` -> `{diff.current_run_id}`)",
            "",
            f"- **Overall Score Delta:** `{diff.overall_score_delta:+.4f}`",
            f"- **Total Cost Delta:** `${diff.total_cost_delta_usd:+.6f}`",
            f"- **Total Duration Delta:** `{diff.total_duration_delta_seconds:+.2f}s`",
            "",
        ])
        if diff.regressions:
            lines.append("**Regressions Detected:**")
            for reg in diff.regressions:
                lines.append(f"- {reg}")
            lines.append("")

    for suite_name, summary in report.suites.items():
        lines.extend([
            "",
            f"## Suite Details: `{suite_name}`",
            "",
            "| Case ID | Trial | Passed | Score | Rating (/5) | Cost (USD) | Duration (s) | Details |",
            "|---|---:|:---:|---:|---:|---:|---:|---|",
        ])
        for case in summary.cases:
            safe_details = case.details.replace("\n", " ").replace("|", "/")[:140]
            lines.append(
                f"| `{case.case_id}` | {case.trial} | {'PASS' if case.passed else 'FAIL'} | "
                f"{case.score:.4f} | {case.rating_5:.2f} | ${case.cost_usd:.6f} | "
                f"{case.duration_seconds:.2f} | {safe_details} |"
            )

    lines.append("")
    return "\n".join(lines)


def save_report(
    report: EvalRunReport,
    results_dir: Path,
    diff: BaselineDiff | None = None,
) -> tuple[Path, Path]:
    """Saves timestamped JSON and Markdown evaluation reports in results_dir."""
    results_dir.mkdir(parents=True, exist_ok=True)
    json_path = results_dir / f"eval_{report.run_id}.json"
    md_path = results_dir / f"eval_{report.run_id}.md"
    json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    md_path.write_text(render_markdown_report(report, diff=diff), encoding="utf-8")
    return json_path, md_path


def render_console_summary(report: EvalRunReport, diff: BaselineDiff | None = None) -> str:
    """Formats a concise human-readable terminal rating summary."""
    lines = [
        "",
        "=" * 78,
        f"  Izvor AI Evaluation & Rating Summary ({report.run_id})",
        "=" * 78,
        f"  Judge Model:   {report.judge_model}",
        f"  Overall Score: {report.overall_score:.4f}  ({report.overall_score * 5.0:.2f} / 5.0)",
        f"  Total Spend:   ${report.total_cost_usd:.6f} / ${report.max_cost_usd:.2f}"
        + (" [BUDGET HALTED]" if report.budget_halted else ""),
        f"  Duration:      {report.total_duration_seconds:.2f}s",
        "-" * 78,
    ]
    for suite_name, summary in report.suites.items():
        passed_cnt = sum(1 for c in summary.cases if c.passed)
        total_cnt = len(summary.cases)
        lines.append(
            f"  [{suite_name:9s}] score={summary.mean_score:.4f} ({summary.mean_rating_5:.2f}/5.0) "
            f"| pass={passed_cnt}/{total_cnt} | cost=${summary.total_cost_usd:.6f} "
            f"| {summary.total_duration_seconds:.2f}s"
        )
        if summary.aggregate_metrics:
            metrics_str = ", ".join(f"{k}={v}" for k, v in summary.aggregate_metrics.items())
            lines.append(f"              metrics: {metrics_str}")

    if diff is not None:
        lines.append("-" * 78)
        lines.append(
            f"  Baseline Diff ({diff.baseline_run_id}): "
            f"overall={diff.overall_score_delta:+.4f}, cost=${diff.total_cost_delta_usd:+.6f}"
        )
        for reg in diff.regressions:
            lines.append(f"  [REGRESSION] {reg}")

    lines.append("=" * 78 + "\n")
    return "\n".join(lines)

