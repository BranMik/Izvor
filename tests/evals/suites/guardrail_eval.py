from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from services.rag.embeddings import EmbeddingService
from services.security.guardrail import GuardrailService
from tests.evals.isolation import PROJECT_ROOT, eval_isolation
from tests.evals.models import EvalCaseResult, SpendTracker, SuiteSummary
from tests.evals.scorers import compute_classification_metrics

logger = logging.getLogger("tests.evals.guardrail")

DEFAULT_GUARDRAIL_DATASET = PROJECT_ROOT / "tests" / "evals" / "datasets" / "guardrail.jsonl"


def _load_cases(dataset_path: Path, case_ids: set[str] | None = None) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for raw_line in dataset_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        item = json.loads(line)
        if case_ids and str(item.get("id", "")).upper() not in case_ids:
            continue
        cases.append(item)
    return cases


def _read_embedding_spend() -> tuple[int, float]:
    try:
        from pipeline.spend_governance import spend_governance

        metrics = spend_governance.calculate_spend("system:embeddings")
        return int(metrics.total_prompt_tokens), float(metrics.total_cost_usd)
    except Exception:
        return 0, 0.0


async def run_guardrail_suite(
    *,
    spend_tracker: SpendTracker,
    trials: int = 1,
    case_ids: set[str] | None = None,
    dataset_path: Path = DEFAULT_GUARDRAIL_DATASET,
) -> SuiteSummary:
    """Evaluates GuardrailService direct and indirect injection accuracy over an uncontaminated holdout."""
    cases = _load_cases(dataset_path, case_ids=case_ids)
    results: list[EvalCaseResult] = []
    pairs: list[tuple[bool, bool]] = []
    heuristic_blocked_count = 0
    semantic_blocked_count = 0
    guardrail_seeded = False
    suite_start = time.time()
    suite_cost = 0.0

    embed_model = EmbeddingService().model

    for trial_num in range(1, max(1, int(trials)) + 1):
        spend_tracker.check_budget()
        with eval_isolation(copy_guardrail_signatures=True) as iso:
            guardrail_seeded = iso.guardrail_seeded
            if not guardrail_seeded:
                logger.warning(
                    "[EVAL GUARDRAIL] data/.rag_cache/lancedb/guardrail_signatures.lance not found; "
                    "semantic embedding guardrail checks will degrade to heuristic-only."
                )

            svc = GuardrailService(db_dir=iso.lancedb_dir)

            for case in cases:
                spend_tracker.check_budget()
                case_id = str(case["id"])
                title = str(case.get("title", case_id))
                expected_blocked = bool(case["expected_blocked"])
                mode = str(case.get("mode", "direct"))
                text = str(case["text"])

                case_start = time.time()
                case_toks = 0
                case_cost = 0.0
                error_msg: str | None = None
                predicted_blocked = False
                detection_method = "allowed"

                try:
                    before_toks, before_usd = _read_embedding_spend()
                    heuristic_hit = svc.check_heuristic_injection(text)
                    if mode == "indirect":
                        tool_name = str(case.get("tool_name") or "web_fetch")
                        res = await svc.screen_untrusted_tool_output(tool_name, text)
                        predicted_blocked = bool(res.detected)
                        detection_method = res.method or "allowed"
                    else:
                        predicted_blocked = await svc.is_prompt_injection(text)
                        if predicted_blocked:
                            detection_method = "heuristic" if heuristic_hit else "semantic"

                    after_toks, after_usd = _read_embedding_spend()
                    case_toks = max(0, after_toks - before_toks)
                    case_cost = max(0.0, after_usd - before_usd)
                    if case_cost > 0 or case_toks > 0:
                        spend_tracker.record_usd(
                            case_cost,
                            model=embed_model,
                            prompt_tokens=case_toks,
                        )
                        suite_cost += case_cost

                    if predicted_blocked:
                        if detection_method == "heuristic":
                            heuristic_blocked_count += 1
                        else:
                            semantic_blocked_count += 1
                except Exception as exc:
                    error_msg = str(exc)

                passed = (error_msg is None) and (predicted_blocked == expected_blocked)
                if error_msg is None:
                    pairs.append((expected_blocked, predicted_blocked))

                results.append(
                    EvalCaseResult(
                        case_id=case_id,
                        suite="guardrail",
                        title=title,
                        trial=trial_num,
                        score=1.0 if passed else 0.0,
                        rating_5=5.0 if passed else 1.0,
                        passed=passed,
                        metrics={
                            "mode": mode,
                            "category": str(case.get("category", "")),
                            "expected_blocked": expected_blocked,
                            "predicted_blocked": predicted_blocked,
                            "detection_method": detection_method,
                        },
                        deterministic_checks={"decision_matches_label": passed},
                        details=(
                            error_msg
                            if error_msg
                            else f"expected={expected_blocked} predicted={predicted_blocked} ({detection_method})"
                        ),
                        duration_seconds=round(time.time() - case_start, 3),
                        prompt_tokens=case_toks,
                        completion_tokens=0,
                        cost_usd=round(case_cost, 6),
                        error=error_msg,
                    )
                )

    agg = compute_classification_metrics(pairs)
    agg["guardrail_seeded"] = guardrail_seeded
    agg["heuristic_blocked_count"] = heuristic_blocked_count
    agg["semantic_blocked_count"] = semantic_blocked_count
    has_positives = any(exp for exp, _ in pairs)
    mean_score = float(agg["f1"]) if has_positives else float(agg["accuracy"])

    return SuiteSummary(
        suite="guardrail",
        total_cases=len(cases),
        trials_per_case=max(1, int(trials)),
        mean_score=round(mean_score, 4),
        mean_rating_5=round(mean_score * 5.0, 2),
        aggregate_metrics=agg,
        total_cost_usd=round(suite_cost, 6),
        total_duration_seconds=round(time.time() - suite_start, 3),
        cases=results,
    )

