import logging
from typing import Any

from core.domain import SpendMetrics
from services.pricing_service import get_model_pricing

logger = logging.getLogger(__name__)


class SpendGovernance:
    """
    Service for real-time token accounting, monetary cost calculation,
    and circuit-breaker budget enforcement.
    """

    def __init__(self, default_threshold_usd: float = 1.0):
        self.default_threshold_usd = default_threshold_usd
        # Map session_id -> {model: {"prompt": int, "completion": int}}
        self._session_tokens: dict[str, dict[str, dict[str, int]]] = {}

    def record_usage(
        self,
        session_id: str,
        model_name: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        """Records token usage for a model within a session."""
        if session_id not in self._session_tokens:
            self._session_tokens[session_id] = {}
        if model_name not in self._session_tokens[session_id]:
            self._session_tokens[session_id][model_name] = {"prompt": 0, "completion": 0}

        self._session_tokens[session_id][model_name]["prompt"] += prompt_tokens
        self._session_tokens[session_id][model_name]["completion"] += completion_tokens

    def calculate_spend(self, session_id: str) -> SpendMetrics:
        """
        Calculates total tokens and monetary cost in USD for a given session.
        """
        data = self._session_tokens.get(session_id, {})
        total_prompt = 0
        total_completion = 0
        total_cost = 0.0
        cost_by_model: dict[str, float] = {}

        for model, usage in data.items():
            p_toks = usage["prompt"]
            c_toks = usage["completion"]
            total_prompt += p_toks
            total_completion += c_toks

            pricing = get_model_pricing(model)
            p_rate = pricing.input_price_per_1m if hasattr(pricing, "input_price_per_1m") else pricing.get("input_price_per_1m", 0.0)
            c_rate = pricing.output_price_per_1m if hasattr(pricing, "output_price_per_1m") else pricing.get("output_price_per_1m", 0.0)

            model_cost = (p_toks * p_rate + c_toks * c_rate) / 1_000_000.0
            cost_by_model[model] = model_cost
            total_cost += model_cost

        return SpendMetrics(
            session_id=session_id,
            total_prompt_tokens=total_prompt,
            total_completion_tokens=total_completion,
            total_cost_usd=round(total_cost, 6),
            cost_by_model=cost_by_model,
            tokens_by_model=data,
        )

    def is_budget_exceeded(
        self,
        session_id: str,
        threshold_usd: float | None = None,
        enabled: bool = True,
        current_cost_usd: float | None = None,
    ) -> tuple[bool, float, float]:
        """
        Checks if session cost has reached or exceeded the monetary budget threshold.
        Returns: `(is_exceeded, current_cost_usd, threshold_usd)`.
        """
        if not enabled:
            return False, 0.0, 0.0

        limit = threshold_usd if threshold_usd is not None else self.default_threshold_usd
        if current_cost_usd is not None:
            cost = current_cost_usd
        else:
            metrics = self.calculate_spend(session_id)
            cost = metrics.total_cost_usd
        is_tripped = cost >= limit
        return is_tripped, cost, limit

    def clear_session(self, session_id: str) -> None:
        """Clears memory for a completed session."""
        self._session_tokens.pop(session_id, None)


# Backward compatibility alias
SpendGovernanceSkill = SpendGovernance

# Global default instance
spend_governance = SpendGovernance()
