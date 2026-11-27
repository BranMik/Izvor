from __future__ import annotations

import asyncio
import importlib
import json
import os
import time
from pathlib import Path
from typing import Any

from tests.evals.isolation import PROJECT_ROOT, eval_isolation
from tests.evals.models import EvalCaseResult, RubricJudgment, SpendTracker, SuiteSummary
from tests.evals.scorers import RubricJudge

DEFAULT_DEBATE_DATASET = PROJECT_ROOT / "tests" / "evals" / "datasets" / "debate.jsonl"


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


async def _noop_sleep(*args: Any, **kwargs: Any) -> None:
    return None


def _configure_case_workflow(case: dict[str, Any]) -> None:
    from config_store.agent_settings import settings_manager
    from config_store.system_settings import system_settings_manager

    if case.get("enabled_agents"):
        settings_manager.enabled_agents.clear()
        settings_manager.enabled_agents.update(str(a) for a in case["enabled_agents"])

    topology = str(case.get("topology") or "sandwich").strip().lower()
    wf_payload: dict[str, Any] = {
        "id": "eval_case_flow",
        "name": f"Eval Case Flow ({case.get('id', 'case')})",
        "mode": topology,
        "description": "Isolated evaluation workflow preset",
    }
    if topology == "fixed":
        wf_payload["fixed_config"] = {"chain": list(case.get("fixed_chain") or [])}
    else:
        wf_payload["sandwich_config"] = {
            "start_chain": list(case.get("start_chain") or []),
            "end_chain": list(case.get("end_chain") or []),
        }

    wf_dir = settings_manager.workflow_manager.workflows_dir
    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / "eval_case_flow.json").write_text(json.dumps(wf_payload, indent=2), encoding="utf-8")
    settings_manager.workflow_manager.reload()
    settings_manager.apply_workflow("eval_case_flow", save=True)

    if case.get("stress_test_enabled") is not None:
        system_settings_manager.dialectical_stress_test["enabled"] = bool(case["stress_test_enabled"])
    if case.get("convergence_consecutive_threshold") is not None:
        system_settings_manager.set_convergence_consecutive_threshold(
            int(case["convergence_consecutive_threshold"]),
            save=True,
        )


def _evaluate_deterministic_checks(
    case: dict[str, Any],
    agent_messages: list[dict[str, Any]],
    all_texts: list[str],
) -> dict[str, bool]:
    checks: dict[str, bool] = {}
    min_messages = int(case.get("min_messages") or 1)
    checks["min_messages_met"] = len(agent_messages) >= min_messages
    checks["non_empty_turns"] = bool(agent_messages) and all(
        bool(str(m.get("text") or "").strip()) for m in agent_messages
    )

    expected_speakers = [str(s) for s in (case.get("expected_speakers") or [])]
    if expected_speakers:
        seen_speakers = {str(m.get("username") or "").strip().lower() for m in agent_messages}
        checks["expected_speakers_present"] = all(
            any(exp.lower() in sp for sp in seen_speakers) for exp in expected_speakers
        )

    max_words_limit = case.get("max_words_limit")
    if max_words_limit is not None:
        from config_store.system_settings import system_settings_manager

        grace = int(getattr(system_settings_manager, "word_limit_grace_buffer", 50) or 50)
        ceiling = int(max_words_limit) + grace
        checks["word_limit_respected"] = all(
            len(str(m.get("text") or "").split()) <= ceiling for m in agent_messages
        )

    reference_facts = [str(f) for f in (case.get("reference_facts") or [])]
    if reference_facts:
        joined_lower = "\n".join(str(m.get("text") or "") for m in agent_messages).lower()
        checks["reference_facts_cited"] = any(fact.lower() in joined_lower for fact in reference_facts)

    if case.get("stress_test_enabled"):
        combined_lower = "\n".join(all_texts).lower()
        checks["stress_test_or_convergence_observed"] = any(
            marker in combined_lower
            for marker in ("challenger", "arbitrator", "verdict", "convergence", "stress-test")
        )

    return checks


async def run_debate_suite(
    *,
    spend_tracker: SpendTracker,
    judge: RubricJudge,
    trials: int = 1,
    case_ids: set[str] | None = None,
    dataset_path: Path = DEFAULT_DEBATE_DATASET,
) -> SuiteSummary:
    """Evaluates live multi-turn dialectical topologies, document grounding, and convergence via RubricJudge."""
    cases = _load_cases(dataset_path, case_ids=case_ids)
    results: list[EvalCaseResult] = []
    suite_start = time.time()
    suite_cost = 0.0

    for trial_num in range(1, max(1, int(trials)) + 1):
        for case in cases:
            spend_tracker.check_budget()
            case_id = str(case["id"])
            title = str(case.get("title", case_id))
            case_start = time.time()
            case_prompt_tokens = 0
            case_completion_tokens = 0
            case_cost = 0.0
            error_msg: str | None = None
            det_checks: dict[str, bool] = {}
            rubric: RubricJudgment | None = None
            details = ""

            try:
                with eval_isolation() as iso:
                    importlib.import_module("main")
                    from adapters.slack.events import PROCESSED_EVENTS
                    from agents.usage_agent import usage_tracker
                    from config_store.app_settings import set_circuit_breaker_enabled
                    from config_store.system_settings import system_settings_manager
                    from core.config import configure_token, reset_token, set_admin_slack_user_ids_override
                    from orchestration.agent_router import (
                        configure_router_sleep_fn,
                        reset_router_deps,
                        reset_router_sleep_fn,
                    )
                    from orchestration.thread_state import thread_state_manager
                    from services.mcp.client_manager import mcp_client_manager
                    from services.slack import (
                        configure_fetch_thread_files,
                        configure_fetch_thread_history,
                        configure_post_agent_response,
                        configure_post_system_message,
                        configure_slack_async_client,
                        reset_fetch_thread_files,
                        reset_fetch_thread_history,
                        reset_post_agent_response,
                        reset_post_system_message,
                        reset_slack_async_client,
                    )
                    from services.thread_skills_store import thread_skills_manager
                    from tests.e2e.protocol_client import TEST_ADMIN_USER_ID, SlackProtocolClient

                    for rel_name, content in (case.get("staged_files") or {}).items():
                        target = iso.agents_resources_dir / rel_name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(str(content), encoding="utf-8")

                    PROCESSED_EVENTS.clear()
                    thread_state_manager.reset_all()
                    thread_skills_manager.clear_all()
                    usage_tracker.clear_all()
                    mcp_client_manager.reset_transport_factory()
                    reset_router_deps()

                    _configure_case_workflow(case)
                    system_settings_manager.set_debate_turn_pacing_seconds(0.0, save=False)
                    system_settings_manager.set_slack_post_pause_seconds(0.0, save=False)
                    set_circuit_breaker_enabled(False, save=False)

                    client = SlackProtocolClient(live_mode=False)
                    client.set_active_scenario(f"EVAL-[{case_id}]", title)
                    orig_signing_secret = os.environ.get("SLACK_SIGNING_SECRET")
                    if not orig_signing_secret or orig_signing_secret == "your-slack-signing-secret":
                        os.environ["SLACK_SIGNING_SECRET"] = client.signing_secret

                    try:
                        set_admin_slack_user_ids_override({TEST_ADMIN_USER_ID})
                        configure_post_agent_response(client.mock_post_agent_response)
                        configure_post_system_message(client.mock_post_system_message)
                        configure_fetch_thread_history(client.mock_fetch_thread_history)
                        configure_fetch_thread_files(client.mock_fetch_thread_files)
                        configure_slack_async_client(client.slack_client_double)
                        configure_token("xoxb-eval-simulated-token")
                        configure_router_sleep_fn(_noop_sleep)

                        channel_id = "C_EVAL_DEBATE"
                        await client.send_mention(
                            str(case["prompt"]),
                            channel_id=channel_id,
                            wait_for_task=True,
                        )
                        thread_ts = client.current_thread_ts or ""
                        if thread_ts:
                            await client.await_active_task(channel_id, thread_ts, timeout=180.0)
                        await asyncio.sleep(0.05)

                        agent_msgs = client.bus.get_messages_for_thread(channel_id, thread_ts)
                        all_texts = client.bus.get_all_texts_for_thread(channel_id, thread_ts)
                        det_checks = _evaluate_deterministic_checks(case, agent_msgs, all_texts)

                        thread_costs = usage_tracker.calculate_thread_costs(channel_id, thread_ts)
                        debate_usd = float(thread_costs.get("grand_total_cost_usd") or 0.0)
                        debate_p = int(thread_costs.get("grand_prompt_tokens") or 0)
                        debate_c = int(thread_costs.get("grand_completion_tokens") or 0)
                        if debate_usd > 0 or debate_p > 0 or debate_c > 0:
                            spend_tracker.record_usd(
                                debate_usd,
                                model="debate_pipeline",
                                prompt_tokens=debate_p,
                                completion_tokens=debate_c,
                            )
                            case_prompt_tokens += debate_p
                            case_completion_tokens += debate_c
                            case_cost += debate_usd

                        transcript_lines = [
                            f"[{m.get('username', 'Agent')}]: {str(m.get('text') or '').strip()}"
                            for m in agent_msgs
                        ]
                        transcript = "\n\n".join(transcript_lines) or "<NO AGENT MESSAGES PRODUCED>"

                        rubric = await judge.evaluate(
                            prompt=str(case["prompt"]),
                            transcript=transcript,
                            rubric_dimensions=list(case.get("rubric_dimensions") or ["dialectical_rigor"]),
                            evaluation_notes=str(case.get("evaluation_notes") or ""),
                            reference_facts=list(case.get("reference_facts") or []) or None,
                        )
                        case_prompt_tokens += int(judge.last_usage.get("prompt_tokens", 0))
                        case_completion_tokens += int(judge.last_usage.get("completion_tokens", 0))
                        case_cost += float(judge.last_usage.get("cost_usd", 0.0))
                        details = rubric.summary or f"turns={len(agent_msgs)}"
                    finally:
                        await client.aclose()
                        if orig_signing_secret is None:
                            os.environ.pop("SLACK_SIGNING_SECRET", None)
                        else:
                            os.environ["SLACK_SIGNING_SECRET"] = orig_signing_secret
                        set_admin_slack_user_ids_override(None)
                        reset_post_agent_response()
                        reset_post_system_message()
                        reset_fetch_thread_history()
                        reset_fetch_thread_files()
                        reset_slack_async_client()
                        reset_token()
                        reset_router_sleep_fn()
                        reset_router_deps()
                        mcp_client_manager.reset_transport_factory()
            except Exception as exc:
                error_msg = str(exc)
                details = error_msg

            score = float(rubric.overall_score) if rubric is not None else 0.0
            rating_5 = round(score * 5.0, 2)
            passed = (
                error_msg is None
                and rubric is not None
                and score >= 0.65
                and not rubric.fatal_flaws
                and bool(det_checks.get("min_messages_met", False))
            )
            suite_cost += case_cost

            results.append(
                EvalCaseResult(
                    case_id=case_id,
                    suite="debate",
                    title=title,
                    trial=trial_num,
                    score=round(score, 4),
                    rating_5=rating_5,
                    passed=passed,
                    metrics={
                        "topology": str(case.get("topology", "")),
                        "dimensions": rubric.dimensions if rubric else {},
                        "fatal_flaws": rubric.fatal_flaws if rubric else [],
                    },
                    deterministic_checks=det_checks,
                    rubric=rubric,
                    details=details,
                    duration_seconds=round(time.time() - case_start, 3),
                    prompt_tokens=case_prompt_tokens,
                    completion_tokens=case_completion_tokens,
                    cost_usd=round(case_cost, 6),
                    error=error_msg,
                )
            )

    n = len(results)
    mean_score = sum(r.score for r in results) / n if n > 0 else 0.0
    pass_rate = sum(1 for r in results if r.passed) / n if n > 0 else 0.0
    det_checks_all = [val for r in results for val in r.deterministic_checks.values()]
    det_rate = (sum(1 for v in det_checks_all if v) / len(det_checks_all)) if det_checks_all else 1.0

    return SuiteSummary(
        suite="debate",
        total_cases=len(cases),
        trials_per_case=max(1, int(trials)),
        mean_score=round(mean_score, 4),
        mean_rating_5=round(mean_score * 5.0, 2),
        aggregate_metrics={
            "pass_rate": round(pass_rate, 4),
            "deterministic_pass_rate": round(det_rate, 4),
        },
        total_cost_usd=round(suite_cost, 6),
        total_duration_seconds=round(time.time() - suite_start, 3),
        cases=results,
    )

