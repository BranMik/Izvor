import sqlite3
import threading
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
        self._lock = threading.Lock()
        self._active_reservations: dict[str, dict[str, float]] = {}
        self._daily_reset_offset_ts: float | None = None

    def record_usage(
        self,
        session_id: str,
        model_name: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        """Records token usage for a model within a session and writes directly to SQLite spend_ledger."""
        model_name = (model_name or "unknown").strip().lower() or "unknown"
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
        except Exception as e:  # noqa: BLE001
            logger.error(f"[SPEND GOVERNANCE] Failed persisting spend ledger entry: {e}")

    def get_daily_spend(self) -> float:
        """Returns the current accumulated spend for today in USD from SQLite spend_ledger."""
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

        try:
            with get_connection() as conn:
                row = conn.execute(
                    "SELECT COALESCE(SUM(cost_usd), 0.0) AS daily_cost FROM spend_ledger WHERE timestamp >= ?;",
                    (max(today_start, getattr(self, "_daily_reset_offset_ts", 0.0) or 0.0),),
                ).fetchone()
                if row:
                    return round(float(row["daily_cost"]), 6)
        except sqlite3.Error as e:
            logger.error(f"[SPEND GOVERNANCE] Failed querying daily spend from SQLite: {e}")
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"[SPEND GOVERNANCE] Unexpected error querying daily spend from SQLite: {e}")

        return 0.0

    def reset_daily_spend(self) -> None:
        """Manually resets the daily spend ledger."""
        now = datetime.now(timezone.utc)
        with self._lock:
            self._daily_reset_offset_ts = now.timestamp()

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
        except sqlite3.Error as e:
            logger.error(f"[SPEND GOVERNANCE] Failed querying session spend from SQLite: {e}")
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"[SPEND GOVERNANCE] Unexpected error querying session spend from SQLite: {e}")

        return SpendMetrics(
            session_id=session_id,
            total_prompt_tokens=0,
            total_completion_tokens=0,
            total_cost_usd=0.0,
            cost_by_model={},
            tokens_by_model={},
        )

    def reserve_budget(
        self,
        session_id: str,
        reservation_id: str,
        estimated_usd: float,
        threshold_usd: float | None = None,
        daily_budget_usd: float | None = None,
        enabled: bool = True,
    ) -> bool:
        """
        Atomically checks if reserving estimated_usd would breach thread or daily budget.
        If budget allows, stores reservation and returns True; otherwise returns False.
        """
        with self._lock:
            daily_limit = (
                daily_budget_usd
                if daily_budget_usd is not None
                else self.default_daily_budget_usd
            )
            limit = threshold_usd if threshold_usd is not None else self.default_threshold_usd
            try:
                daily_spend = self.get_daily_spend()
                all_active_res = sum(
                    sum(res.values()) for res in self._active_reservations.values()
                )
                if daily_limit > 0 and (daily_spend + all_active_res + estimated_usd) >= daily_limit:
                    logger.warning(
                        f"[SPEND GOVERNANCE] Cannot reserve ${estimated_usd:.4f}; would exceed daily budget (${daily_spend + all_active_res + estimated_usd:.4f} >= ${daily_limit:.4f})"
                    )
                    return False

                if enabled:
                    metrics = self.calculate_spend(session_id)
                    session_cost = metrics.total_cost_usd
                    active_res_for_session = sum(self._active_reservations.get(session_id, {}).values())
                    if limit > 0 and (session_cost + active_res_for_session + estimated_usd) >= limit:
                        logger.warning(
                            f"[SPEND GOVERNANCE] Cannot reserve ${estimated_usd:.4f}; would exceed session threshold (${session_cost + active_res_for_session + estimated_usd:.4f} >= ${limit:.4f})"
                        )
                        return False
            except sqlite3.Error as db_err:
                if daily_limit > 0 or (enabled and limit > 0):
                    logger.warning(
                        f"[SPEND GOVERNANCE] SQLite storage error during reserve_budget; failing closed: {db_err}"
                    )
                    return False

            if session_id not in self._active_reservations:
                self._active_reservations[session_id] = {}
            self._active_reservations[session_id][reservation_id] = estimated_usd
            return True

    def release_reservation(self, session_id: str, reservation_id: str) -> None:
        """Releases an active budget reservation."""
        with self._lock:
            if session_id in self._active_reservations:
                self._active_reservations[session_id].pop(reservation_id, None)
                if not self._active_reservations[session_id]:
                    del self._active_reservations[session_id]

    def get_active_reservations_usd(
        self,
        session_id: str | None = None,
        exclude_reservation_id: str | None = None,
    ) -> float:
        """Returns total active reservations in USD for a session or globally."""
        with self._lock:
            if session_id is not None:
                return sum(
                    val
                    for res_id, val in self._active_reservations.get(session_id, {}).items()
                    if exclude_reservation_id is None or res_id != exclude_reservation_id
                )
            return sum(
                val
                for res in self._active_reservations.values()
                for res_id, val in res.items()
                if exclude_reservation_id is None or res_id != exclude_reservation_id
            )

    def is_budget_exceeded(
        self,
        session_id: str,
        threshold_usd: float | None = None,
        enabled: bool = True,
        current_cost_usd: float | None = None,
        daily_budget_usd: float | None = None,
        exclude_reservation_id: str | None = None,
    ) -> tuple[bool, float, float]:
        """
        Checks if session cost or daily aggregated spend has reached or exceeded the monetary budget threshold.
        Returns: `(is_exceeded, current_cost_usd, threshold_usd)`.
        """
        daily_limit = (
            daily_budget_usd
            if daily_budget_usd is not None
            else self.default_daily_budget_usd
        )
        limit = threshold_usd if threshold_usd is not None else self.default_threshold_usd

        try:
            # 1. Check daily budget threshold (always enforced, independent of per-thread breaker)
            daily_spend = self.get_daily_spend() + self.get_active_reservations_usd(
                exclude_reservation_id=exclude_reservation_id
            )
            if daily_limit > 0 and daily_spend >= daily_limit:
                logger.warning(
                    f"[SPEND GOVERNANCE] Daily budget exceeded: ${daily_spend:.4f} >= ${daily_limit:.4f} USD."
                )
                return True, daily_spend, daily_limit

            if not enabled:
                return False, 0.0, 0.0

            # 2. Check per-session threshold
            active_session_res = self.get_active_reservations_usd(
                session_id,
                exclude_reservation_id=exclude_reservation_id,
            )
            if current_cost_usd is not None:
                cost = current_cost_usd + active_session_res
            else:
                metrics = self.calculate_spend(session_id)
                cost = metrics.total_cost_usd + active_session_res
            is_tripped = limit > 0 and cost >= limit
            return is_tripped, cost, limit
        except sqlite3.Error as db_err:
            if daily_limit > 0 or enabled:
                effective_limit = daily_limit if daily_limit > 0 else limit
                logger.warning(
                    f"[SPEND GOVERNANCE] SQLite storage error during is_budget_exceeded; failing closed: {db_err}"
                )
                return True, float("inf"), effective_limit
            return False, 0.0, 0.0

    def clear_session(self, session_id: str) -> None:
        """Clears active budget reservations for a completed session."""
        with self._lock:
            self._active_reservations.pop(session_id, None)

    def clear_all_sessions(self) -> None:
        """Clears all active budget reservations across all sessions."""
        with self._lock:
            self._active_reservations.clear()


# Global default instance
spend_governance = SpendGovernance()

