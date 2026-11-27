from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import litellm

from core.llm_utils import configure_llm_completion_fn, reset_llm_completion_fn
from orchestration.revisor_service import run_internal_revisor
from orchestration.thread_state import thread_state_manager
from orchestration.turn_pipeline import TurnContext
from services.slack import configure_post_agent_response, reset_post_agent_response
from tests.evals.isolation import PROJECT_ROOT, eval_isolation
from tests.evals.models import EvalCaseResult, SpendTracker, SuiteSummary
from tests.evals.scorers import compute_classification_metrics

DEFAULT_REVISOR_DATASET = PROJECT_ROOT / "tests" / "evals" / "datasets" / "revisor.jsonl"


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


def _extract_tokens_from_resp(resp: Any) -> tuple[int, int]:
    usage = getattr(resp, "usage", None)
    if usage is None:
        return 0, 0
    if isinstance(usage, dict):
        return int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
    return int(getattr(usage, "prompt_tokens", 0) or 0), int(getattr(usage, "completion_tokens", 0) or 0)


async def run_revisor_suite(
    *,
    spend_tracker: SpendTracker,
    trials: int = 1,
    case_ids: set[str] | None = None,
    dataset_path: Path = DEFAULT_REVISOR_DATASET,
) -> SuiteSummary:
    """Evaluates InternalRevisor precision, recall, F1, FPR, and FNR across faithful vs. hallucinated turns."""
    cases = _load_cases(dataset_path, case_ids=case_ids)
    results: list[EvalCaseResult] = []
    pairs: list[tuple[bool, bool]] = []
    suite_start = time.time()
    suite_cost = 0.0

    for trial_num in range(1, max(1, int(trials)) + 1):
        for case in cases:
            spend_tracker.check_budget()
            case_id = str(case["id"])
            title = str(case.get("title", case_id))
            expected_flag = bool(case["expected_flag"])
            case_start = time.time()
            case_prompt_tokens = 0
            case_completion_tokens = 0
            case_cost = 0.0
            error_msg: str | None = None
            predicted_flag = False
            warning_text = ""

            try:
                with eval_isolation():
                    from agents.usage_agent import usage_tracker

                    thread_state_manager.reset_all()
                    usage_tracker.clear_all()

                    channel_id = "C_EVAL_REVISOR"
                    thread_ts = f"170000{trial_num:02d}.{case_id}"
                    initial_prompt = str(case["initial_prompt"])
                    agent_key = str(case.get("agent_key") or "agent-10008")
                    speaker = str(case.get("speaker") or "Security Architect")

                    thread_state_manager.set_initial_prompt(channel_id, thread_ts, initial_prompt)
                    thread_state_manager.set_active_prompt(channel_id, thread_ts, initial_prompt)

                    for ev in case.get("evidence") or []:
                        thread_state_manager.add_round_evidence(
                            channel_id,
                            thread_ts,
                            tool_name=str(ev.get("tool_name") or "read_document"),
                            arguments=ev.get("arguments") or {},
                            result=str(ev.get("content") or ""),
                            agent_key=agent_key,
                        )

                    prior_msgs = list(case.get("prior_assistant_messages") or [])
                    skills_used: list[str] = []
                    if len(prior_msgs) == 0:
                        full_history: list[dict[str, Any]] = []
                        skills_used = ["document_reader"]
                    else:
                        full_history = list(prior_msgs)
                        # Ensure turn_count = len(assistant_msgs) + 1 is odd so run_internal_revisor
                        # verifies ONLY the target candidate message while preserving prior speaker citations.
                        if (len(full_history) + 1) % 2 == 0:
                            full_history.insert(
                                0,
                                {
                                    "role": "assistant",
                                    "content": "[Facilitator]: Evaluate the technical tradeoffs carefully.",
                                },
                            )

                    ctx = TurnContext(
                        channel_id=channel_id,
                        thread_ts=thread_ts,
                        raw_text=initial_prompt,
                        my_generation=thread_state_manager.get_generation(channel_id, thread_ts),
                    )
                    ctx.agent_key = agent_key
                    ctx.agent_cfg = {"username": speaker}
                    ctx.file_context = None
                    ctx.messages = []
                    ctx.full_history = full_history
                    ctx.force_termination = True
                    ctx.output.is_completed = True
                    ctx.output.skills_used = skills_used
                    ctx.output.final_text = str(case["candidate_message"])

                    posted_warnings: list[str] = []

                    async def _capture_revisor_post(
                        channel_id: str,
                        thread_ts: str,
                        text: str,
                        username: str | None = None,
                        icon_url: str | None = None,
                        blocks: list[dict[str, Any]] | None = None,
                        **kwargs: Any,
                    ) -> dict[str, Any]:
                        posted_warnings.append(str(text))
                        return {"ok": True, "ts": "1700000001.0001"}

                    real_acompletion = litellm.acompletion

                    async def _metered_acompletion(*args: Any, **kwargs: Any) -> Any:
                        nonlocal case_prompt_tokens, case_completion_tokens, case_cost
                        resp = await real_acompletion(*args, **kwargs)
                        p_toks, c_toks = _extract_tokens_from_resp(resp)
                        model_name = str(kwargs.get("model") or "unknown")
                        case_prompt_tokens += p_toks
                        case_completion_tokens += c_toks
                        case_cost += spend_tracker.record_tokens(model_name, p_toks, c_toks)
                        return resp

                    configure_post_agent_response(_capture_revisor_post)
                    configure_llm_completion_fn(_metered_acompletion)
                    try:
                        ret_warning = await run_internal_revisor(ctx)
                    finally:
                        reset_llm_completion_fn()
                        reset_post_agent_response()

                    warning_text = str(ret_warning or (posted_warnings[0] if posted_warnings else "")).strip()
                    predicted_flag = bool(warning_text)
            except Exception as exc:
                error_msg = str(exc)

            passed = (error_msg is None) and (predicted_flag == expected_flag)
            if error_msg is None:
                pairs.append((expected_flag, predicted_flag))

            score = 1.0 if passed else 0.0
            rating_5 = 5.0 if passed else 1.0
            suite_cost += case_cost
            results.append(
                EvalCaseResult(
                    case_id=case_id,
                    suite="revisor",
                    title=title,
                    trial=trial_num,
                    score=score,
                    rating_5=rating_5,
                    passed=passed,
                    metrics={
                        "expected_flag": expected_flag,
                        "predicted_flag": predicted_flag,
                        "category": str(case.get("category", "")),
                    },
                    deterministic_checks={"verdict_matches_label": passed},
                    details=warning_text[:240] if warning_text else (error_msg or "NO_HALLUCINATION"),
                    duration_seconds=round(time.time() - case_start, 3),
                    prompt_tokens=case_prompt_tokens,
                    completion_tokens=case_completion_tokens,
                    cost_usd=round(case_cost, 6),
                    error=error_msg,
                )
            )

    agg = compute_classification_metrics(pairs)
    has_positives = any(exp for exp, _ in pairs)
    mean_score = float(agg["f1"]) if has_positives else float(agg["accuracy"])
    return SuiteSummary(
        suite="revisor",
        total_cases=len(cases),
        trials_per_case=max(1, int(trials)),
        mean_score=round(mean_score, 4),
        mean_rating_5=round(mean_score * 5.0, 2),
        aggregate_metrics=agg,
        total_cost_usd=round(suite_cost, 6),
        total_duration_seconds=round(time.time() - suite_start, 3),
        cases=results,
    )

