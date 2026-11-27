from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import litellm

from core.config import get_default_reasoning_model
from core.model_catalog import resolve_model_with_fallback
from tests.evals.models import RubricJudgment, SpendTracker


def compute_classification_metrics(pairs: list[tuple[bool, bool]]) -> dict[str, float | int]:
    """
    Computes binary classification metrics from `(expected_positive, predicted_positive)` pairs.
    """
    tp = sum(1 for expected, predicted in pairs if expected and predicted)
    fp = sum(1 for expected, predicted in pairs if (not expected) and predicted)
    tn = sum(1 for expected, predicted in pairs if (not expected) and (not predicted))
    fn = sum(1 for expected, predicted in pairs if expected and (not predicted))

    total = len(pairs)
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0
    fpr = fp / (fp + tn) if (fp + tn) > 0 else 0.0
    fnr = fn / (fn + tp) if (fn + tp) > 0 else 0.0
    accuracy = (tp + tn) / total if total > 0 else 0.0

    return {
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "fpr": round(fpr, 4),
        "fnr": round(fnr, 4),
        "accuracy": round(accuracy, 4),
    }


def compute_retrieval_metrics(
    retrieved_doc_ids: list[str],
    expected_doc_ids: list[str],
    forbidden_doc_ids: list[str] | None = None,
    k: int = 3,
) -> dict[str, float | bool]:
    """
    Computes Recall@K, Hit@K, MRR, scope isolation, and composite score for RAG queries.
    """
    limit = max(1, int(k))
    top_k_raw = [str(d).replace("\\", "/") for d in retrieved_doc_ids[:limit] if d]
    seen: set[str] = set()
    top_k_unique: list[str] = []
    for doc_id in top_k_raw:
        if doc_id not in seen:
            seen.add(doc_id)
            top_k_unique.append(doc_id)

    forbidden = {str(d).replace("\\", "/") for d in (forbidden_doc_ids or []) if d}
    scope_isolation_passed = not any(doc_id in forbidden for doc_id in top_k_unique)

    expected = [str(d).replace("\\", "/") for d in expected_doc_ids if d]
    if not expected:
        binary = 1.0 if scope_isolation_passed else 0.0
        return {
            "recall_at_k": binary,
            "hit_at_k": binary,
            "mrr": binary,
            "scope_isolation_passed": scope_isolation_passed,
            "score": binary,
        }

    expected_set = set(expected)
    hits = [doc_id for doc_id in expected_set if doc_id in seen]
    recall_at_k = len(hits) / len(expected_set)
    hit_at_k = 1.0 if hits else 0.0

    mrr = 0.0
    for rank, doc_id in enumerate(top_k_unique, start=1):
        if doc_id in expected_set:
            mrr = 1.0 / rank
            break

    composite = 0.0 if not scope_isolation_passed else round(0.6 * recall_at_k + 0.4 * mrr, 4)
    return {
        "recall_at_k": round(recall_at_k, 4),
        "hit_at_k": round(hit_at_k, 4),
        "mrr": round(mrr, 4),
        "scope_isolation_passed": scope_isolation_passed,
        "score": composite,
    }


def resolve_judge_model(
    explicit_model: str | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """
    Resolves the LLM-as-judge model using precedence:
    explicit_model -> IZVOR_EVAL_JUDGE_MODEL -> default_reasoning_model,
    then applies key-availability fallback via model_catalog.
    """
    active_env = env if env is not None else os.environ
    env_model = str(active_env.get("IZVOR_EVAL_JUDGE_MODEL") or "").strip()
    candidate = str(explicit_model or "").strip() or env_model or get_default_reasoning_model()
    return resolve_model_with_fallback(
        candidate,
        env_dict=env,
        log_context="EVAL_JUDGE",
    ).model


def extract_json_object(raw_text: str) -> dict[str, Any]:
    """Parses a JSON object from raw model output, handling markdown fences and surrounding prose."""
    cleaned = str(raw_text or "").strip()
    if not cleaned:
        raise ValueError("Judge returned empty response.")

    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, re.IGNORECASE)
    if fence_match:
        cleaned = fence_match.group(1).strip()

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        candidate = cleaned[start : end + 1]
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed

    raise ValueError(f"Could not parse JSON object from judge output: {cleaned[:200]}")


def _extract_usage_tokens(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return 0, 0
    if isinstance(usage, dict):
        prompt_toks = int(usage.get("prompt_tokens") or 0)
        completion_toks = int(usage.get("completion_tokens") or 0)
        return prompt_toks, completion_toks
    prompt_toks = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion_toks = int(getattr(usage, "completion_tokens", 0) or 0)
    return prompt_toks, completion_toks


class RubricJudge:
    """Provider-agnostic async LLM-as-judge scoring transcripts on a 1-5 rubric."""

    def __init__(
        self,
        model: str | None = None,
        spend_tracker: SpendTracker | None = None,
        completion_fn: Callable[..., Awaitable[Any]] | None = None,
        timeout_seconds: float = 45.0,
    ) -> None:
        self._explicit_model = model
        self._resolved_model: str | None = None
        self.spend_tracker = spend_tracker
        self.completion_fn = completion_fn
        self.timeout_seconds = float(timeout_seconds)
        self.last_usage: dict[str, int | float] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cost_usd": 0.0,
        }

    @property
    def model(self) -> str:
        if self._resolved_model is None:
            self._resolved_model = resolve_judge_model(self._explicit_model)
        return self._resolved_model

    async def evaluate(
        self,
        *,
        prompt: str,
        transcript: str,
        rubric_dimensions: list[str],
        evaluation_notes: str = "",
        reference_facts: list[str] | None = None,
    ) -> RubricJudgment:
        if self.spend_tracker is not None:
            self.spend_tracker.check_budget()

        active_model = self.model
        dim_list = ", ".join(f'"{d}": <integer 1..5>' for d in rubric_dimensions)
        facts_section = ""
        if reference_facts:
            facts_bullets = "\n".join(f"- {fact}" for fact in reference_facts)
            facts_section = f"\nReference ground-truth facts that should be respected:\n{facts_bullets}\n"

        notes_section = f"\nEvaluation notes:\n{evaluation_notes}\n" if evaluation_notes else ""

        system_prompt = (
            "You are an impartial, rigorous AI evaluation judge grading a multi-agent deliberation transcript.\n"
            "Score each requested rubric dimension on an integer scale from 1 (very poor / broken) to 5 (exceptional).\n"
            "List any fatal flaws (hallucinated facts, ignored user instructions, broken role adherence, empty turns) "
            "in `fatal_flaws` (empty list [] if none).\n"
            "Return ONLY a valid JSON object matching this exact schema:\n"
            "{\n"
            f'  "dimensions": {{{dim_list}}},\n'
            '  "summary": "<1-3 sentence concise justification>",\n'
            '  "fatal_flaws": ["<string>"]\n'
            "}"
        )

        user_prompt = (
            f"User Prompt:\n{prompt}\n"
            f"{facts_section}"
            f"{notes_section}\n"
            f"Transcript to Evaluate:\n{transcript}"
        )

        call_fn = self.completion_fn or litellm.acompletion
        response = await asyncio.wait_for(
            call_fn(
                model=active_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.0,
            ),
            timeout=self.timeout_seconds,
        )

        p_toks, c_toks = _extract_usage_tokens(response)
        cost = 0.0
        if self.spend_tracker is not None and (p_toks > 0 or c_toks > 0):
            cost = self.spend_tracker.record_tokens(active_model, p_toks, c_toks)
        self.last_usage = {
            "prompt_tokens": p_toks,
            "completion_tokens": c_toks,
            "cost_usd": cost,
        }

        choices = getattr(response, "choices", None)
        if not choices:
            raise ValueError("Judge returned no choices.")
        raw_content = getattr(choices[0].message, "content", "") or ""
        payload = extract_json_object(raw_content)

        raw_dims = payload.get("dimensions")
        if not isinstance(raw_dims, dict):
            raise ValueError(f"Judge JSON missing 'dimensions' object: {payload}")

        normalized_dims: dict[str, int] = {}
        for dim in rubric_dimensions:
            if dim in raw_dims:
                normalized_dims[dim] = raw_dims[dim]
            else:
                normalized_dims[dim] = 1

        return RubricJudgment(
            dimensions=normalized_dims,
            summary=str(payload.get("summary") or "").strip(),
            fatal_flaws=payload.get("fatal_flaws") or [],
        )

