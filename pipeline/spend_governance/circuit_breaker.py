import time
from datetime import datetime, timezone
import logging

from core.database import get_connection, get_db_transaction
from core.domain import SpendMetrics
from services.pricing_service import get_model_pricing

logger = logging.getLogger(__name__)


def _parse_session_id(session_id: str) -> tuple[str, str]:
    if ":" in session_id:
        c, t = session_id.split(":", 1)
        return c, t
    return session_id, ""


class SpendGovernance:
    """
    Service for real-time token accounting, monetary cost calculation,
    daily spend tracking, and circuit-breaker budget enforcement.
    Persists all token usage and costs to SQLite spend_ledger.
    """

    def __init__(self, default_threshold_usd: float = 1.0, default_daily_budget_usd: float = 10.0):
        self.default_threshold_usd = default_threshold_usd
        self.default_daily_budget_usd = default_daily_budget_usd
        # In-memory working cache for active execution turns
        self._session_tokens: dict[str, dict[str, dict[str, int]]] = {}
        self._daily_spend_usd: float = 0.0
        self._daily_spend_date: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _check_daily_rollover(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._daily_spend_date != today:
            logger.info(
                f"[SPEND GOVERNANCE] New day rollover ({today} vs {self._daily_spend_date}). Resetting daily spend cache."
            )
            self._daily_spend_date = today
            self._daily_spend_usd = 0.0

    def record_usage(
        self,
        session_id: str,
        model_name: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        model: str | None = None,
    ) -> None:
        """Records token usage for a model within a session, accumulates daily spend, and writes to SQLite ledger."""
        if not model_name and model:
            model_name = model
        if session_id not in self._session_tokens:
            self._session_tokens[session_id] = {}
        if model_name not in self._session_tokens[session_id]:
            self._session_tokens[session_id][model_name] = {"prompt": 0, "completion": 0}

        self._session_tokens[session_id][model_name]["prompt"] += prompt_tokens
        self._session_tokens[session_id][model_name]["completion"] += completion_tokens

        # Accumulate incremental daily spend
        self._check_daily_rollover()
        pricing = get_model_pricing(model_name)
        p_rate = (
            pricing.input_price_per_1m
            if hasattr(pricing, "input_price_per_1m")
            else pricing.get("input_price_per_1m", 0.0)
        )
        c_rate = (
            pricing.output_price_per_1m
            if hasattr(pricing, "output_price_per_1m")
            else pricing.get("output_price_per_1m", 0.0)
        )
        delta_cost = (prompt_tokens * p_rate + completion_tokens * c_rate) / 1_000_000.0
        self._daily_spend_usd += delta_cost

        # Persist to SQLite spend_ledger
        channel_id, thread_ts = _parse_session_id(session_id)
        now_ts = time.time()
        try:
            with get_db_transaction(immediate=True) as conn:
                conn.execute(
                    """
                    INSERT INTO spend_ledger (channel_id, thread_ts, model_name, prompt_tokens, completion_tokens, cost_usd, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?);
                    """,
                    (channel_id, thread_ts, model_name, prompt_tokens, completion_tokens, delta_cost, now_ts),
                )
        except Exception as e:
            logger.error(f"[SPEND GOVERNANCE] Failed persisting spend ledger entry: {e}")

    def get_daily_spend(self) -> float:
        """Returns the current accumulated spend for today in USD from SQLite spend_ledger."""
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

        try:
            with get_connection() as conn:
                row = conn.execute(
                    "SELECT COALESCE(SUM(cost_usd), 0.0) AS daily_cost FROM spend_ledger WHERE timestamp >= ?;",
                    (today_start,),
                ).fetchone()
                if row:
                    return round(float(row["daily_cost"]), 6)
        except Exception as e:
            logger.error(f"[SPEND GOVERNANCE] Failed querying daily spend from SQLite: {e}")

        # Fallback to in-memory tracker
        self._check_daily_rollover()
        return round(self._daily_spend_usd, 6)

    def reset_daily_spend(self) -> None:
        """Manually resets the daily spend ledger."""
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

        try:
            with get_db_transaction(immediate=True) as conn:
                conn.execute("DELETE FROM spend_ledger WHERE timestamp >= ?;", (today_start,))
        except Exception as e:
            logger.error(f"[SPEND GOVERNANCE] Failed resetting daily spend in SQLite: {e}")

        self._daily_spend_usd = 0.0
        self._daily_spend_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def calculate_spend(self, session_id: str) -> SpendMetrics:
        """
        Calculates total tokens and monetary cost in USD for a given session from SQLite spend_ledger.
        """
        channel_id, thread_ts = _parse_session_id(session_id)

        try:
            with get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT model_name, SUM(prompt_tokens) AS p, SUM(completion_tokens) AS c, SUM(cost_usd) AS cost
                    FROM spend_ledger
                    WHERE channel_id = ? AND thread_ts = ?
                    GROUP BY model_name;
                    """,
                    (channel_id, thread_ts),
                ).fetchall()

                if rows:
                    total_prompt = 0
                    total_completion = 0
                    total_cost = 0.0
                    cost_by_model: dict[str, float] = {}
                    tokens_by_model: dict[str, dict[str, int]] = {}

                    for r in rows:
                        m_name = r["model_name"]
                        p_toks = int(r["p"])
                        c_toks = int(r["c"])
                        c_cost = float(r["cost"])

                        total_prompt += p_toks
                        total_completion += c_toks
                        total_cost += c_cost
                        cost_by_model[m_name] = c_cost
                        tokens_by_model[m_name] = {"prompt": p_toks, "completion": c_toks}

                    return SpendMetrics(
                        session_id=session_id,
                        total_prompt_tokens=total_prompt,
                        total_completion_tokens=total_completion,
                        total_cost_usd=round(total_cost, 6),
                        cost_by_model=cost_by_model,
                        tokens_by_model=tokens_by_model,
                    )
        except Exception as e:
            logger.error(f"[SPEND GOVERNANCE] Failed querying session spend from SQLite: {e}")

        # In-memory fallback
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
            p_rate = (
                pricing.input_price_per_1m
                if hasattr(pricing, "input_price_per_1m")
                else pricing.get("input_price_per_1m", 0.0)
            )
            c_rate = (
                pricing.output_price_per_1m
                if hasattr(pricing, "output_price_per_1m")
                else pricing.get("output_price_per_1m", 0.0)
            )

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
        daily_budget_usd: float | None = None,
    ) -> tuple[bool, float, float]:
        """
        Checks if session cost or daily aggregated spend has reached or exceeded the monetary budget threshold.
        Returns: `(is_exceeded, current_cost_usd, threshold_usd)`.
        """
        # 1. Check daily budget threshold (always enforced, independent of per-thread breaker)
        daily_spend = self.get_daily_spend()
        daily_limit = (
            daily_budget_usd
            if daily_budget_usd is not None
            else self.default_daily_budget_usd
        )
        if daily_limit > 0 and daily_spend >= daily_limit:
            logger.warning(
                f"[SPEND GOVERNANCE] Daily budget exceeded: ${daily_spend:.4f} >= ${daily_limit:.4f} USD."
            )
            return True, daily_spend, daily_limit

        if not enabled:
            return False, 0.0, 0.0

        # 2. Check per-session threshold
        limit = threshold_usd if threshold_usd is not None else self.default_threshold_usd
        if current_cost_usd is not None:
            cost = current_cost_usd
        else:
            metrics = self.calculate_spend(session_id)
            cost = metrics.total_cost_usd
        is_tripped = cost >= limit
        return is_tripped, cost, limit

    def clear_session(self, session_id: str) -> None:
        """Clears in-memory active session cache for a completed session, preserving historical spend_ledger."""
        self._session_tokens.pop(session_id, None)

    def clear_all_sessions(self) -> None:
        """Clears in-memory session caches for all sessions, preserving historical spend_ledger."""
        self._session_tokens.clear()


# Global default instance
spend_governance = SpendGovernance()

