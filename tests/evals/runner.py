from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from tests.evals.isolation import PROJECT_ROOT
from tests.evals.models import BudgetExceededError, EvalRunReport, SpendTracker, SuiteSummary
from tests.evals.reporting import (
    compare_against_baseline,
    load_report,
    render_console_summary,
    save_baseline,
    save_report,
)
from tests.evals.scorers import RubricJudge, resolve_judge_model
from tests.evals.suites import (
    ALL_SUITES,
    run_debate_suite,
    run_guardrail_suite,
    run_rag_suite,
    run_revisor_suite,
)

DEFAULT_RESULTS_DIR = PROJECT_ROOT / "tests" / "evals" / "results"


class EvalRunner:
    """Orchestrates live AI evaluation suites with budget governance and baseline reporting."""

    def __init__(
        self,
        *,
        suites: list[str] | tuple[str, ...] = ALL_SUITES,
        case_ids: set[str] | None = None,
        trials: int = 1,
        judge_model: str | None = None,
        max_cost_usd: float = 1.0,
        results_dir: Path = DEFAULT_RESULTS_DIR,
    ) -> None:
        self.suites = [s.strip().lower() for s in suites if s.strip()]
        invalid = [s for s in self.suites if s not in ALL_SUITES]
        if invalid:
            raise ValueError(f"Unknown eval suite(s): {invalid}. Valid suites: {list(ALL_SUITES)}")
        self.case_ids = {c.strip().upper() for c in case_ids if c.strip()} if case_ids else None
        self.trials = max(1, int(trials))
        self.judge_model_explicit = judge_model
        self.max_cost_usd = float(max_cost_usd)
        self.results_dir = Path(results_dir)
        self.spend_tracker = SpendTracker(max_cost_usd=self.max_cost_usd)

    async def run(self) -> EvalRunReport:
        now = datetime.now(timezone.utc)
        run_id = now.strftime("%Y%m%d_%H%M%S")
        timestamp_utc = now.isoformat()
        active_judge_model = resolve_judge_model(self.judge_model_explicit)
        judge = RubricJudge(model=active_judge_model, spend_tracker=self.spend_tracker)

        run_start = time.time()
        suite_summaries: dict[str, SuiteSummary] = {}
        budget_halted = False

        for suite_name in self.suites:
            try:
                if suite_name == "debate":
                    summary = await run_debate_suite(
                        spend_tracker=self.spend_tracker,
                        judge=judge,
                        trials=self.trials,
                        case_ids=self.case_ids,
                    )
                elif suite_name == "revisor":
                    summary = await run_revisor_suite(
                        spend_tracker=self.spend_tracker,
                        trials=self.trials,
                        case_ids=self.case_ids,
                    )
                elif suite_name == "rag":
                    summary = await run_rag_suite(
                        spend_tracker=self.spend_tracker,
                        trials=self.trials,
                        case_ids=self.case_ids,
                    )
                elif suite_name == "guardrail":
                    summary = await run_guardrail_suite(
                        spend_tracker=self.spend_tracker,
                        trials=self.trials,
                        case_ids=self.case_ids,
                    )
                else:
                    raise ValueError(f"Unsupported suite: {suite_name}")
                suite_summaries[suite_name] = summary
            except BudgetExceededError:
                budget_halted = True
                break

        evaluated_suites = [s for s in suite_summaries.values() if s.cases]
        overall_score = (
            round(sum(s.mean_score for s in evaluated_suites) / len(evaluated_suites), 4)
            if evaluated_suites
            else 0.0
        )

        return EvalRunReport(
            run_id=run_id,
            timestamp_utc=timestamp_utc,
            judge_model=active_judge_model,
            max_cost_usd=self.max_cost_usd,
            budget_halted=budget_halted,
            total_cost_usd=round(self.spend_tracker.total_cost_usd, 6),
            total_duration_seconds=round(time.time() - run_start, 3),
            overall_score=overall_score,
            suites=suite_summaries,
        )


def _resolve_baseline_path(spec: str, results_dir: Path) -> Path:
    candidate = Path(spec)
    if candidate.exists():
        return candidate
    clean = spec if spec.endswith(".json") else f"{spec}.json"
    return results_dir / clean


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Izvor Live AI Evaluation & Rating Suite Runner"
    )
    parser.add_argument(
        "--suite",
        type=str,
        default="all",
        help="Comma-separated suites to run (debate, revisor, rag, guardrail) or 'all' (default: all)",
    )
    parser.add_argument(
        "--case",
        type=str,
        default=None,
        help="Optional comma-separated case IDs to execute (e.g. DEB-01,RAG-05,GRD-01)",
    )
    parser.add_argument(
        "--trials",
        type=int,
        default=1,
        help="Number of trials per case (default: 1)",
    )
    parser.add_argument(
        "--judge-model",
        type=str,
        default=None,
        help="Override LLM-as-judge model (defaults to IZVOR_EVAL_JUDGE_MODEL or default_reasoning_model)",
    )
    parser.add_argument(
        "--max-cost-usd",
        type=float,
        default=1.00,
        help="Hard spend cap in USD before halting further eval cases (default: 1.00)",
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=DEFAULT_RESULTS_DIR,
        help="Directory for JSON/Markdown eval reports and baselines (default: tests/evals/results)",
    )
    parser.add_argument(
        "--save-baseline",
        type=str,
        default=None,
        help="Save this run's JSON report as a named baseline in results-dir (e.g. baseline)",
    )
    parser.add_argument(
        "--compare-baseline",
        type=str,
        default=None,
        help="Compare this run against a baseline name or JSON file path",
    )
    parser.add_argument(
        "--regression-tolerance",
        type=float,
        default=0.05,
        help="Allowed score drop before flagging a regression (default: 0.05)",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=None,
        help="Fail with non-zero exit code if overall_score is below this threshold (0.0..1.0)",
    )
    parser.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="Exit non-zero if baseline comparison detects any score regression",
    )

    args = parser.parse_args(argv)

    raw_suite = str(args.suite or "all").strip().lower()
    selected_suites = list(ALL_SUITES) if raw_suite == "all" else [
        s.strip().lower() for s in raw_suite.split(",") if s.strip()
    ]
    selected_cases = (
        {c.strip().upper() for c in str(args.case).split(",") if c.strip()}
        if args.case
        else None
    )

    runner = EvalRunner(
        suites=selected_suites,
        case_ids=selected_cases,
        trials=args.trials,
        judge_model=args.judge_model,
        max_cost_usd=args.max_cost_usd,
        results_dir=args.results_dir,
    )
    report = asyncio.run(runner.run())

    diff = None
    if args.compare_baseline:
        baseline_path = _resolve_baseline_path(args.compare_baseline, args.results_dir)
        baseline_report = load_report(baseline_path)
        diff = compare_against_baseline(
            report,
            baseline_report,
            regression_tolerance=args.regression_tolerance,
        )

    json_path, md_path = save_report(report, args.results_dir, diff=diff)
    if args.save_baseline:
        baseline_saved = save_baseline(report, args.results_dir, name=args.save_baseline)
        print(f"Saved baseline: {baseline_saved}")

    print(render_console_summary(report, diff=diff))
    print(f"JSON report:     {json_path}")
    print(f"Markdown report: {md_path}")

    completed_cases = sum(len(s.cases) for s in report.suites.values())
    if report.budget_halted and completed_cases == 0:
        return 2
    if args.min_score is not None and report.overall_score < float(args.min_score):
        print(
            f"[FAIL] overall_score {report.overall_score:.4f} < --min-score {float(args.min_score):.4f}",
            file=sys.stderr,
        )
        return 1
    if args.fail_on_regression and diff is not None and diff.regressions:
        print("[FAIL] Baseline regression detected.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

