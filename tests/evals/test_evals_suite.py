from __future__ import annotations

import pytest

from tests.evals.reporting import save_report
from tests.evals.runner import DEFAULT_RESULTS_DIR, EvalRunner
from tests.evals.suites import ALL_SUITES


@pytest.mark.anyio
@pytest.mark.parametrize("suite_name", ALL_SUITES)
async def test_eval_suite(suite_name: str, request):
    """Executes each live AI evaluation suite via pytest tests/evals/test_evals_suite.py."""
    raw_case = request.config.getoption("eval_case") if hasattr(request.config, "getoption") else None
    max_cost = (
        float(request.config.getoption("eval_max_cost_usd"))
        if hasattr(request.config, "getoption")
        else 1.00
    )
    judge_model = (
        request.config.getoption("eval_judge_model")
        if hasattr(request.config, "getoption")
        else None
    )
    case_ids = {c.strip().upper() for c in str(raw_case).split(",") if c.strip()} if raw_case else None

    runner = EvalRunner(
        suites=[suite_name],
        case_ids=case_ids,
        judge_model=judge_model,
        max_cost_usd=max_cost,
        results_dir=DEFAULT_RESULTS_DIR,
    )
    report = await runner.run()
    save_report(report, DEFAULT_RESULTS_DIR)

    assert not report.budget_halted, (
        f"Eval suite '{suite_name}' halted by budget cap (${max_cost:.2f})"
    )
    summary = report.suites.get(suite_name)
    assert summary is not None
    if summary.cases:
        assert summary.mean_score >= 0.5, (
            f"Suite '{suite_name}' mean_score={summary.mean_score:.4f} below minimum floor 0.50"
        )

