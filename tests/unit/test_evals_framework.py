from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.evals.models import (
    BudgetExceededError,
    EvalCaseResult,
    EvalRunReport,
    RubricJudgment,
    SpendTracker,
    SuiteSummary,
)
from tests.evals.reporting import (
    compare_against_baseline,
    load_report,
    render_console_summary,
    render_markdown_report,
    save_baseline,
    save_report,
)
from tests.evals.scorers import (
    RubricJudge,
    compute_classification_metrics,
    compute_retrieval_metrics,
    extract_json_object,
    resolve_judge_model,
)

ROOT_DIR = Path(__file__).resolve().parents[2]
DATASETS_DIR = ROOT_DIR / "tests" / "evals" / "datasets"
CANONICAL_RAG_DIR = ROOT_DIR / "config" / "rag_knowledge"


def _load_jsonl(path: Path) -> list[dict]:
    assert path.is_file(), f"Missing dataset file: {path}"
    items = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw.strip()
        if not stripped:
            continue
        parsed = json.loads(stripped)
        assert isinstance(parsed, dict), f"{path.name}:{line_no} must be a JSON object"
        items.append(parsed)
    return items


def test_golden_datasets_schemas_and_balance():
    debate = _load_jsonl(DATASETS_DIR / "debate.jsonl")
    assert len(debate) == 5
    debate_ids = [c["id"] for c in debate]
    assert len(set(debate_ids)) == len(debate_ids)
    assert {"sandwich", "scatter_gather", "fixed"}.issubset({c["topology"] for c in debate})
    for c in debate:
        assert c["prompt"]
        assert isinstance(c["rubric_dimensions"], list) and len(c["rubric_dimensions"]) >= 3

    revisor = _load_jsonl(DATASETS_DIR / "revisor.jsonl")
    assert len(revisor) == 22
    assert len({c["id"] for c in revisor}) == 22
    assert sum(1 for c in revisor if c["expected_flag"] is True) == 11
    assert sum(1 for c in revisor if c["expected_flag"] is False) == 11
    en_cases = [c for c in revisor if c.get("language") == "en"]
    hr_cases = [c for c in revisor if c.get("language") == "hr"]
    assert len(en_cases) == 12
    assert sum(1 for c in en_cases if c["expected_flag"] is True) == 6
    assert sum(1 for c in en_cases if c["expected_flag"] is False) == 6
    assert len(hr_cases) == 10
    assert sum(1 for c in hr_cases if c["expected_flag"] is True) == 5
    assert sum(1 for c in hr_cases if c["expected_flag"] is False) == 5
    for c in revisor:
        assert c["language"] in ("en", "hr")
        assert c["initial_prompt"]
        assert c["candidate_message"]
        assert isinstance(c["expected_flag"], bool)

    rag = _load_jsonl(DATASETS_DIR / "rag.jsonl")
    assert len(rag) == 10
    assert len({c["id"] for c in rag}) == 10
    canonical_docs = {
        p.relative_to(CANONICAL_RAG_DIR).as_posix()
        for p in CANONICAL_RAG_DIR.rglob("*")
        if p.is_file()
    }
    assert canonical_docs == {
        "shared/system_overview.md",
        "shared/rag_instructions.md",
        "agent-10008/security_policy.md",
    }
    for c in rag:
        assert c["query"]
        assert c["agent_name"]
        assert c["scope"] in ("all", "self")
        for doc_id in c["expected_doc_ids"] + c["forbidden_doc_ids"]:
            assert doc_id in canonical_docs

    guardrail = _load_jsonl(DATASETS_DIR / "guardrail.jsonl")
    assert len(guardrail) == 20
    assert len({c["id"] for c in guardrail}) == 20
    assert sum(1 for c in guardrail if c["expected_blocked"] is True) == 10
    assert sum(1 for c in guardrail if c["expected_blocked"] is False) == 10
    for c in guardrail:
        assert c["text"]
        assert c["mode"] in ("direct", "indirect")
        assert isinstance(c["expected_blocked"], bool)


def test_classification_metrics_math():
    perfect = compute_classification_metrics([(True, True), (False, False), (True, True)])
    assert perfect == {
        "tp": 2,
        "fp": 0,
        "tn": 1,
        "fn": 0,
        "precision": 1.0,
        "recall": 1.0,
        "f1": 1.0,
        "fpr": 0.0,
        "fnr": 0.0,
        "accuracy": 1.0,
    }

    mixed = compute_classification_metrics([(True, True), (True, False), (False, True), (False, False)])
    assert mixed["tp"] == 1
    assert mixed["fp"] == 1
    assert mixed["tn"] == 1
    assert mixed["fn"] == 1
    assert mixed["precision"] == 0.5
    assert mixed["recall"] == 0.5
    assert mixed["f1"] == 0.5
    assert mixed["fpr"] == 0.5
    assert mixed["fnr"] == 0.5
    assert mixed["accuracy"] == 0.5

    empty = compute_classification_metrics([])
    assert empty["f1"] == 0.0
    assert empty["accuracy"] == 0.0


def test_retrieval_metrics_and_scope_isolation():
    hit_top1 = compute_retrieval_metrics(
        retrieved_doc_ids=["shared/system_overview.md", "shared/rag_instructions.md"],
        expected_doc_ids=["shared/system_overview.md"],
        forbidden_doc_ids=["agent-10008/security_policy.md"],
        k=3,
    )
    assert hit_top1["recall_at_k"] == 1.0
    assert hit_top1["mrr"] == 1.0
    assert hit_top1["scope_isolation_passed"] is True
    assert hit_top1["score"] == 1.0

    rank2 = compute_retrieval_metrics(
        retrieved_doc_ids=["shared/system_overview.md", "shared/rag_instructions.md"],
        expected_doc_ids=["shared/rag_instructions.md"],
        k=3,
    )
    assert rank2["recall_at_k"] == 1.0
    assert rank2["mrr"] == 0.5
    assert rank2["score"] == 0.8

    leaked = compute_retrieval_metrics(
        retrieved_doc_ids=["shared/system_overview.md", "agent-10008/security_policy.md"],
        expected_doc_ids=["shared/system_overview.md"],
        forbidden_doc_ids=["agent-10008/security_policy.md"],
        k=3,
    )
    assert leaked["scope_isolation_passed"] is False
    assert leaked["score"] == 0.0

    neg_ok = compute_retrieval_metrics(
        retrieved_doc_ids=["shared/system_overview.md"],
        expected_doc_ids=[],
        forbidden_doc_ids=["agent-10008/security_policy.md"],
        k=3,
    )
    assert neg_ok["scope_isolation_passed"] is True
    assert neg_ok["score"] == 1.0

    neg_fail = compute_retrieval_metrics(
        retrieved_doc_ids=["agent-10008/security_policy.md"],
        expected_doc_ids=[],
        forbidden_doc_ids=["agent-10008/security_policy.md"],
        k=3,
    )
    assert neg_fail["scope_isolation_passed"] is False
    assert neg_fail["score"] == 0.0


def test_spend_tracker_and_budget_guard():
    tracker = SpendTracker(
        max_cost_usd=0.05,
        pricing_fn=lambda _model: {"input_price_per_1m": 10.0, "output_price_per_1m": 20.0},
    )
    cost1 = tracker.record_tokens("test-model", prompt_tokens=1_000, completion_tokens=500)
    assert pytest.approx(cost1, rel=1e-6) == 0.02
    assert pytest.approx(tracker.remaining_usd, rel=1e-6) == 0.03
    tracker.check_budget()

    tracker.record_usd(0.03, model="external")
    assert tracker.remaining_usd == 0.0
    with pytest.raises(BudgetExceededError):
        tracker.check_budget()


def test_resolve_judge_model_precedence():
    env = {
        "IZVOR_EVAL_JUDGE_MODEL": "gemini/gemini-4-hydrogen",
        "GEMINI_API_KEY": "real-key-1234567890",
        "MISTRAL_API_KEY": "real-key-0987654321",
    }
    assert resolve_judge_model(explicit_model="mistral/mistral-large-latest", env=env) == "mistral/mistral-large-latest"
    assert resolve_judge_model(explicit_model=None, env=env) == "gemini/gemini-4-hydrogen"


def test_extract_json_object_variants():
    assert extract_json_object('{"a": 1}') == {"a": 1}
    assert extract_json_object('```json\n{"a": 2}\n```') == {"a": 2}
    assert extract_json_object('Sure, here is the verdict:\n{"a": 3}\nDone.') == {"a": 3}
    with pytest.raises(ValueError):
        extract_json_object("not valid json at all")


@pytest.mark.anyio
async def test_rubric_judge_parses_clamps_and_enforces_budget():
    tracker = SpendTracker(
        max_cost_usd=1.0,
        pricing_fn=lambda _m: {"input_price_per_1m": 2.0, "output_price_per_1m": 4.0},
    )

    async def fake_completion(**_kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='```json\n{"dimensions": {"rigor": 9, "clarity": 0, "actionability": 4}, "summary": "Solid synthesis.", "fatal_flaws": []}\n```'
                    )
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=250),
        )

    judge = RubricJudge(
        model="gemini/gemini-4-hydrogen",
        spend_tracker=tracker,
        completion_fn=fake_completion,
    )
    judgment = await judge.evaluate(
        prompt="Compare Postgres vs Redis",
        transcript="[Visionary]: Use Postgres for ACID.",
        rubric_dimensions=["rigor", "clarity", "actionability"],
    )
    assert isinstance(judgment, RubricJudgment)
    assert judgment.dimensions == {"rigor": 5, "clarity": 1, "actionability": 4}
    assert judgment.overall_score == round((5 + 1 + 4) / 15.0, 4)
    assert judgment.summary == "Solid synthesis."
    assert tracker.total_prompt_tokens == 1000
    assert tracker.total_completion_tokens == 250
    assert tracker.total_cost_usd > 0.0

    called = False

    async def should_not_be_called(**_kwargs):
        nonlocal called
        called = True
        return None

    exhausted_tracker = SpendTracker(max_cost_usd=0.01)
    exhausted_tracker.record_usd(0.02)
    blocked_judge = RubricJudge(
        model="gemini/gemini-4-hydrogen",
        spend_tracker=exhausted_tracker,
        completion_fn=should_not_be_called,
    )
    with pytest.raises(BudgetExceededError):
        await blocked_judge.evaluate(
            prompt="Test",
            transcript="Test",
            rubric_dimensions=["rigor"],
        )
    assert called is False


def test_reporting_save_baseline_and_regression_diff(tmp_path: Path):
    base_report = EvalRunReport(
        run_id="20261127_100000",
        timestamp_utc="2026-11-27T10:00:00+00:00",
        judge_model="mistral/mistral-large-latest",
        max_cost_usd=1.0,
        total_cost_usd=0.05,
        total_duration_seconds=12.0,
        overall_score=0.9,
        suites={
            "rag": SuiteSummary(
                suite="rag",
                total_cases=1,
                trials_per_case=1,
                mean_score=0.9,
                mean_rating_5=4.5,
                aggregate_metrics={"mean_mrr": 0.9},
                total_cost_usd=0.05,
                total_duration_seconds=12.0,
                cases=[
                    EvalCaseResult(
                        case_id="RAG-01",
                        suite="rag",
                        title="Overview",
                        score=0.9,
                        rating_5=4.5,
                        passed=True,
                    )
                ],
            )
        },
    )
    cur_report = EvalRunReport(
        run_id="20261127_110000",
        timestamp_utc="2026-11-27T11:00:00+00:00",
        judge_model="mistral/mistral-large-latest",
        max_cost_usd=1.0,
        total_cost_usd=0.06,
        total_duration_seconds=14.0,
        overall_score=0.72,
        suites={
            "rag": SuiteSummary(
                suite="rag",
                total_cases=1,
                trials_per_case=1,
                mean_score=0.72,
                mean_rating_5=3.6,
                aggregate_metrics={"mean_mrr": 0.7},
                total_cost_usd=0.06,
                total_duration_seconds=14.0,
                cases=[
                    EvalCaseResult(
                        case_id="RAG-01",
                        suite="rag",
                        title="Overview",
                        score=0.72,
                        rating_5=3.6,
                        passed=True,
                    )
                ],
            )
        },
    )

    baseline_path = save_baseline(base_report, tmp_path, name="baseline")
    loaded_base = load_report(baseline_path)
    assert loaded_base.run_id == base_report.run_id

    diff = compare_against_baseline(cur_report, loaded_base, regression_tolerance=0.05)
    assert pytest.approx(diff.overall_score_delta, abs=1e-4) == -0.18
    assert len(diff.regressions) == 2
    assert diff.suite_deltas["rag"].metric_deltas["mean_mrr"] == pytest.approx(-0.2, abs=1e-4)

    no_reg_diff = compare_against_baseline(cur_report, loaded_base, regression_tolerance=0.25)
    assert no_reg_diff.regressions == []

    json_path, md_path = save_report(cur_report, tmp_path, diff=diff)
    assert json_path.is_file()
    assert md_path.is_file()
    md_text = render_markdown_report(cur_report, diff=diff)
    assert "Regressions Detected" in md_text
    console_text = render_console_summary(cur_report, diff=diff)
    assert "REGRESSION" in console_text

