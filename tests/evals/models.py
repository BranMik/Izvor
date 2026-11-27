from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class BudgetExceededError(RuntimeError):
    """Raised when an evaluation run reaches or exceeds its configured USD budget cap."""


class RubricJudgment(BaseModel):
    """Structured LLM-as-judge evaluation over 1-5 rubric dimensions."""

    dimensions: dict[str, int]
    overall_score: float = 0.0
    summary: str = ""
    fatal_flaws: list[str] = Field(default_factory=list)

    @field_validator("dimensions")
    @classmethod
    def _clamp_dimensions(cls, value: dict[str, int]) -> dict[str, int]:
        clamped: dict[str, int] = {}
        for key, raw_score in value.items():
            try:
                numeric = int(round(float(raw_score)))
            except (TypeError, ValueError):
                numeric = 1
            clamped[str(key)] = max(1, min(5, numeric))
        return clamped

    @field_validator("fatal_flaws", mode="before")
    @classmethod
    def _normalize_fatal_flaws(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            cleaned = value.strip()
            return [cleaned] if cleaned else []
        return [str(item).strip() for item in value if str(item).strip()]

    @model_validator(mode="after")
    def _compute_overall_score(self) -> RubricJudgment:
        if self.dimensions:
            mean_rating = sum(self.dimensions.values()) / len(self.dimensions)
            self.overall_score = round(mean_rating / 5.0, 4)
        else:
            self.overall_score = round(max(0.0, min(1.0, float(self.overall_score))), 4)
        return self


class EvalCaseResult(BaseModel):
    """Single evaluated case/trial outcome."""

    case_id: str
    suite: str
    title: str
    trial: int = 1
    score: float = 0.0
    rating_5: float = 0.0
    passed: bool = False
    metrics: dict[str, Any] = Field(default_factory=dict)
    deterministic_checks: dict[str, bool] = Field(default_factory=dict)
    rubric: RubricJudgment | None = None
    details: str = ""
    duration_seconds: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    error: str | None = None


class SuiteSummary(BaseModel):
    """Aggregated evaluation summary for a single suite."""

    suite: str
    total_cases: int
    trials_per_case: int
    mean_score: float
    mean_rating_5: float
    aggregate_metrics: dict[str, Any] = Field(default_factory=dict)
    total_cost_usd: float = 0.0
    total_duration_seconds: float = 0.0
    cases: list[EvalCaseResult] = Field(default_factory=list)


class EvalRunReport(BaseModel):
    """Top-level report across all executed evaluation suites."""

    run_id: str
    timestamp_utc: str
    judge_model: str
    max_cost_usd: float
    budget_halted: bool = False
    total_cost_usd: float = 0.0
    total_duration_seconds: float = 0.0
    overall_score: float = 0.0
    suites: dict[str, SuiteSummary] = Field(default_factory=dict)


class SuiteDelta(BaseModel):
    """Per-suite comparison against a saved baseline report."""

    suite: str
    baseline_score: float
    current_score: float
    score_delta: float
    baseline_cost_usd: float
    current_cost_usd: float
    cost_delta_usd: float
    baseline_duration_seconds: float
    current_duration_seconds: float
    duration_delta_seconds: float
    metric_deltas: dict[str, float] = Field(default_factory=dict)


class BaselineDiff(BaseModel):
    """Overall and per-suite delta report comparing current run to baseline."""

    baseline_run_id: str
    current_run_id: str
    overall_score_delta: float
    total_cost_delta_usd: float
    total_duration_delta_seconds: float
    suite_deltas: dict[str, SuiteDelta] = Field(default_factory=dict)
    regressions: list[str] = Field(default_factory=list)


def _default_pricing_lookup(model_name: str) -> dict[str, Any]:
    from services.pricing_service import get_model_pricing

    return get_model_pricing(model_name)


class SpendTracker:
    """Tracks token usage and USD spend across live evals with hard budget enforcement."""

    def __init__(
        self,
        max_cost_usd: float = 1.0,
        pricing_fn: Callable[[str], Any] | None = None,
    ) -> None:
        self.max_cost_usd = float(max_cost_usd)
        self._pricing_fn = pricing_fn or _default_pricing_lookup
        self.total_cost_usd: float = 0.0
        self.total_prompt_tokens: int = 0
        self.total_completion_tokens: int = 0
        self.by_model: dict[str, dict[str, float | int]] = {}

    def _resolve_rates(self, model: str) -> tuple[float, float]:
        try:
            pricing = self._pricing_fn(model)
        except Exception:
            return 1.0, 3.0

        if hasattr(pricing, "input_price_per_1m"):
            in_rate = float(getattr(pricing, "input_price_per_1m", 1.0) or 1.0)
            out_rate = float(getattr(pricing, "output_price_per_1m", 3.0) or 3.0)
            return in_rate, out_rate
        if isinstance(pricing, dict):
            in_rate = float(pricing.get("input_price_per_1m", 1.0) or 1.0)
            out_rate = float(pricing.get("output_price_per_1m", 3.0) or 3.0)
            return in_rate, out_rate
        return 1.0, 3.0

    def record_tokens(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        p_toks = max(0, int(prompt_tokens or 0))
        c_toks = max(0, int(completion_tokens or 0))
        in_rate_1m, out_rate_1m = self._resolve_rates(model)
        cost = (p_toks / 1_000_000.0) * in_rate_1m + (c_toks / 1_000_000.0) * out_rate_1m

        self.total_prompt_tokens += p_toks
        self.total_completion_tokens += c_toks
        self.total_cost_usd += cost

        entry = self.by_model.setdefault(
            model or "unknown",
            {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0},
        )
        entry["prompt_tokens"] = int(entry["prompt_tokens"]) + p_toks
        entry["completion_tokens"] = int(entry["completion_tokens"]) + c_toks
        entry["cost_usd"] = float(entry["cost_usd"]) + cost
        return cost

    def record_usd(
        self,
        amount: float,
        model: str = "external",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> float:
        delta = max(0.0, float(amount or 0.0))
        p_toks = max(0, int(prompt_tokens or 0))
        c_toks = max(0, int(completion_tokens or 0))

        self.total_cost_usd += delta
        self.total_prompt_tokens += p_toks
        self.total_completion_tokens += c_toks

        entry = self.by_model.setdefault(
            model or "external",
            {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0},
        )
        entry["prompt_tokens"] = int(entry["prompt_tokens"]) + p_toks
        entry["completion_tokens"] = int(entry["completion_tokens"]) + c_toks
        entry["cost_usd"] = float(entry["cost_usd"]) + delta
        return delta

    def check_budget(self) -> None:
        if self.max_cost_usd > 0 and self.total_cost_usd >= self.max_cost_usd:
            raise BudgetExceededError(
                f"Evaluation spend limit reached: ${self.total_cost_usd:.4f} >= ${self.max_cost_usd:.4f}"
            )

    @property
    def remaining_usd(self) -> float:
        if self.max_cost_usd <= 0:
            return float("inf")
        return max(0.0, self.max_cost_usd - self.total_cost_usd)

