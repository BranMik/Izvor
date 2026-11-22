import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from services.pricing_service import get_model_pricing
from services.slack import (
    get_channel_name,
    get_or_create_usage_channel,
    post_usage_message,
    update_usage_message,
)

from core.task_utils import safe_create_task
from core.config import USAGE_CHANNEL_NAME

logger = logging.getLogger(__name__)


def parse_thread_creation_time(thread_ts: str) -> str:
    """Parses Slack thread_ts epoch string into a human-readable datetime string."""
    try:
        ts_float = float(thread_ts)
        dt = datetime.fromtimestamp(ts_float, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError, OSError):
        return "Unknown"


class ThreadUsageTracker:
    """Tracks token consumption, monetary expenses, and live summary card updates in #izvor-usage using pure SQLite state."""

    def __init__(self) -> None:
        self.usage_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self.running_usage_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self.pending_usage_updates: dict[tuple[str, str], bool] = {}
        self.pending_usage_completed: dict[tuple[str, str], bool] = {}

    def get_lock(self, channel_id: str, thread_ts: str) -> asyncio.Lock:
        """Returns or creates an asyncio.Lock for a specific thread."""
        thread_key = (channel_id, thread_ts)
        if thread_key not in self.usage_locks:
            self.usage_locks[thread_key] = asyncio.Lock()
        return self.usage_locks[thread_key]

    def cleanup_thread(self, channel_id: str, thread_ts: str) -> None:
        """Cleans up in-memory runtime locks and active tasks for an evicted thread."""
        thread_key = (channel_id, thread_ts)
        self.usage_locks.pop(thread_key, None)
        task = self.running_usage_tasks.pop(thread_key, None)
        if task and not task.done():
            task.cancel()
        self.pending_usage_updates.pop(thread_key, None)
        self.pending_usage_completed.pop(thread_key, None)

    def reset_runtime_state(self) -> None:
        """Cleans up all in-memory runtime locks and tasks across all threads."""
        for task in list(self.running_usage_tasks.values()):
            if task and not task.done():
                task.cancel()
        self.usage_locks.clear()
        self.running_usage_tasks.clear()
        self.pending_usage_updates.clear()
        self.pending_usage_completed.clear()

    def get_thread_meta(self, channel_id: str, thread_ts: str) -> dict[str, Any] | None:
        """Fetches thread usage metadata directly from SQLite thread_usage_meta."""
        try:
            from core.database import get_connection

            with get_connection() as conn:
                row = conn.execute(
                    """
                    SELECT channel_id, thread_ts, channel_name, created_at_str, usage_msg_ts, is_completed, last_updated
                    FROM thread_usage_meta
                    WHERE channel_id = ? AND thread_ts = ?;
                    """,
                    (channel_id, thread_ts),
                ).fetchone()
                if row:
                    return {
                        "channel_id": row["channel_id"],
                        "channel_name": row["channel_name"],
                        "thread_ts": row["thread_ts"],
                        "created_at_str": row["created_at_str"],
                        "usage_msg_ts": row["usage_msg_ts"],
                        "is_completed": bool(row["is_completed"]),
                        "last_updated": float(row["last_updated"]),
                    }
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[USAGE AGENT] Failed fetching thread meta from SQLite: {e}")
        return None

    def upsert_thread_meta(self, channel_id: str, thread_ts: str, **updates: Any) -> dict[str, Any]:
        """Upserts metadata fields into SQLite thread_usage_meta atomically."""
        now = time.time()
        try:
            from core.database import get_db_transaction

            with get_db_transaction(immediate=True) as conn:
                row = conn.execute(
                    """
                    SELECT channel_id, thread_ts, channel_name, created_at_str, usage_msg_ts, is_completed, last_updated
                    FROM thread_usage_meta
                    WHERE channel_id = ? AND thread_ts = ?;
                    """,
                    (channel_id, thread_ts),
                ).fetchone()

                current = dict(row) if row else {
                    "channel_id": channel_id,
                    "thread_ts": thread_ts,
                    "channel_name": f"#{channel_id}",
                    "created_at_str": parse_thread_creation_time(thread_ts),
                    "usage_msg_ts": None,
                    "is_completed": 0,
                    "last_updated": now,
                }
                for k, v in updates.items():
                    if k in current:
                        if k == "is_completed":
                            current[k] = int(bool(v))
                        elif k == "last_updated":
                            current[k] = float(v)
                        else:
                            current[k] = str(v) if v is not None else None

                current["last_updated"] = now
                conn.execute(
                    """
                    INSERT INTO thread_usage_meta (channel_id, thread_ts, channel_name, created_at_str, usage_msg_ts, is_completed, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(channel_id, thread_ts) DO UPDATE SET
                        channel_name = excluded.channel_name,
                        created_at_str = excluded.created_at_str,
                        usage_msg_ts = excluded.usage_msg_ts,
                        is_completed = excluded.is_completed,
                        last_updated = excluded.last_updated;
                    """,
                    (
                        channel_id,
                        thread_ts,
                        current["channel_name"],
                        current["created_at_str"],
                        current["usage_msg_ts"],
                        current["is_completed"],
                        current["last_updated"],
                    ),
                )
                return {
                    "channel_id": current["channel_id"],
                    "thread_ts": current["thread_ts"],
                    "channel_name": current["channel_name"],
                    "created_at_str": current["created_at_str"],
                    "usage_msg_ts": current["usage_msg_ts"],
                    "is_completed": bool(current["is_completed"]),
                    "last_updated": current["last_updated"],
                }
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[USAGE AGENT] Failed upserting thread meta to SQLite: {e}")
            return {
                "channel_id": channel_id,
                "thread_ts": thread_ts,
                "channel_name": str(updates.get("channel_name", f"#{channel_id}")),
                "created_at_str": parse_thread_creation_time(thread_ts),
                "usage_msg_ts": updates.get("usage_msg_ts"),
                "is_completed": bool(updates.get("is_completed", False)),
                "last_updated": now,
            }

    def clear_all(self) -> None:
        """Resets runtime state and clears all rows from SQLite thread_usage_meta."""
        self.reset_runtime_state()
        try:
            from core.database import get_db_transaction

            with get_db_transaction(immediate=True) as conn:
                conn.execute("DELETE FROM thread_usage_meta;")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[USAGE AGENT] Failed clearing thread_usage_meta: {e}")

    def record_tokens(
        self,
        channel_id: str,
        thread_ts: str,
        model_name: str,
        prompt_tokens: int,
        completion_tokens: int,
        channel_name: str | None = None,
    ) -> dict[str, Any]:
        """
        Accumulates input and output tokens for a specific model within a thread.
        Persists directly to SQLite spend_ledger via spend_governance and SQLite thread_usage_meta.
        """
        # Wire into SQLite spend_ledger via spend_governance
        try:
            from pipeline.spend_governance import spend_governance

            session_id = f"{channel_id}:{thread_ts}"
            spend_governance.record_usage(
                session_id=session_id,
                model_name=model_name or "unknown",
                prompt_tokens=int(prompt_tokens),
                completion_tokens=int(completion_tokens),
            )
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[USAGE AGENT] Spend governance recording failed: {e}")

        updates: dict[str, Any] = {}
        if channel_name:
            updates["channel_name"] = channel_name
        meta = self.upsert_thread_meta(channel_id, thread_ts, **updates)

        logger.info(
            f"[USAGE AGENT] Recorded tokens for thread {thread_ts} | Model: {model_name} | "
            f"New Prompt={prompt_tokens}, Completion={completion_tokens}"
        )
        return meta

    def calculate_thread_costs(self, channel_id: str, thread_ts: str) -> dict[str, Any]:
        """
        Computes exact monetary costs per model and grand totals for a given thread
        by querying SQLite spend_ledger via spend_governance and SQLite thread_usage_meta.
        """
        meta = self.get_thread_meta(channel_id, thread_ts) or {}
        session_id = f"{channel_id}:{thread_ts}"
        models_tokens: dict[str, dict[str, int]] = {}

        try:
            from pipeline.spend_governance import spend_governance

            metrics = spend_governance.calculate_spend(session_id)
            if metrics.tokens_by_model:
                models_tokens = {
                    m: {
                        "prompt_tokens": tok.get("prompt", 0),
                        "completion_tokens": tok.get("completion", 0),
                        "total_tokens": tok.get("prompt", 0) + tok.get("completion", 0),
                    }
                    for m, tok in metrics.tokens_by_model.items()
                }
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[USAGE AGENT] Failed querying spend metrics: {e}")

        models_breakdown = {}
        grand_prompt_tokens = 0
        grand_completion_tokens = 0
        grand_total_tokens = 0
        grand_total_cost_usd = 0.0

        for model_name, stats in models_tokens.items():
            p_tokens = stats.get("prompt_tokens", 0)
            c_tokens = stats.get("completion_tokens", 0)
            t_tokens = stats.get("total_tokens", p_tokens + c_tokens)

            pricing = get_model_pricing(model_name)
            in_rate_1m = (
                pricing.input_price_per_1m
                if hasattr(pricing, "input_price_per_1m")
                else pricing.get("input_price_per_1m", 1.0)
            )
            out_rate_1m = (
                pricing.output_price_per_1m
                if hasattr(pricing, "output_price_per_1m")
                else pricing.get("output_price_per_1m", 3.0)
            )

            in_cost = (p_tokens / 1_000_000.0) * in_rate_1m
            out_cost = (c_tokens / 1_000_000.0) * out_rate_1m
            model_total_cost = in_cost + out_cost

            source = (
                pricing.source
                if hasattr(pricing, "source")
                else pricing.get("source", "Pricing Cache")
            )
            source_url = (
                pricing.source_url
                if hasattr(pricing, "source_url")
                else pricing.get("source_url", "")
            )

            models_breakdown[model_name] = {
                "prompt_tokens": p_tokens,
                "completion_tokens": c_tokens,
                "total_tokens": t_tokens,
                "input_price_per_1m": in_rate_1m,
                "output_price_per_1m": out_rate_1m,
                "input_cost_usd": in_cost,
                "output_cost_usd": out_cost,
                "total_cost_usd": model_total_cost,
                "source": source,
                "source_url": source_url,
            }

            grand_prompt_tokens += p_tokens
            grand_completion_tokens += c_tokens
            grand_total_tokens += t_tokens
            grand_total_cost_usd += model_total_cost

        return {
            "channel_id": channel_id,
            "channel_name": meta.get("channel_name", f"#{channel_id}"),
            "thread_ts": thread_ts,
            "created_at_str": meta.get("created_at_str", parse_thread_creation_time(thread_ts)),
            "is_completed": meta.get("is_completed", False),
            "models": models_breakdown,
            "grand_prompt_tokens": grand_prompt_tokens,
            "grand_completion_tokens": grand_completion_tokens,
            "grand_total_tokens": grand_total_tokens,
            "grand_total_cost_usd": grand_total_cost_usd,
        }

    def get_thread_total_cost_usd(self, channel_id: str, thread_ts: str) -> float:
        """Returns the current grand total monetary cost in USD for a given thread."""
        costs = self.calculate_thread_costs(channel_id, thread_ts)
        return float(costs.get("grand_total_cost_usd", 0.0))

    async def format_usage_slack_message(
        self, channel_id: str, thread_ts: str, is_completed: bool = False
    ) -> str:
        """
        Generates the formatted Slack Mrkdwn message representing the current
        token and monetary consumption for the thread.
        """
        existing_entry = self.get_thread_meta(channel_id, thread_ts) or {}
        existing_name = existing_entry.get("channel_name")

        if existing_name and not existing_name.startswith(("#C", "#D")):
            ch_name = existing_name
        else:
            ch_name = await get_channel_name(channel_id)
            self.upsert_thread_meta(channel_id, thread_ts, channel_name=ch_name)

        if is_completed:
            self.upsert_thread_meta(channel_id, thread_ts, is_completed=True)

        cost_data = self.calculate_thread_costs(channel_id, thread_ts)
        created_at = cost_data["created_at_str"]
        completed_status = is_completed or cost_data["is_completed"]

        status_str = "✅ *Completed*" if completed_status else "🟢 *In Progress / Active*"

        lines = [
            "📊 *THREAD USAGE REPORT (TOKENS & COSTS)*",
            "━" * 38,
            f"📍 *Channel:* `{ch_name}`",
            f"📅 *Thread Created:* `{created_at}`",
            f"🧵 *Thread TS:* `{thread_ts}`",
            f"🔄 *Discussion Status:* {status_str}",
            "━" * 38,
            "*BREAKDOWN BY MODEL:*",
        ]

        models = cost_data["models"]
        if not models:
            lines.append("_(No tokens recorded yet for this thread)_")
        else:
            for model_name, m_info in models.items():
                clean_name = model_name
                p_tok = m_info["prompt_tokens"]
                c_tok = m_info["completion_tokens"]
                tot_tok = m_info["total_tokens"]
                in_cost = m_info["input_cost_usd"]
                out_cost = m_info["output_cost_usd"]
                m_cost = m_info["total_cost_usd"]
                in_rate = m_info["input_price_per_1m"]
                out_rate = m_info["output_price_per_1m"]

                lines.append(f"• *`{clean_name}`*")
                lines.append(
                    f"   ├ 📥 *Prompt (Input):* `{p_tok:,}` tok.  |  *${in_cost:.5f}*  _(${in_rate:.2f}/1M)_"
                )
                lines.append(
                    f"   ├ 📤 *Completion (Output):* `{c_tok:,}` tok.  |  *${out_cost:.5f}*  _(${out_rate:.2f}/1M)_"
                )
                lines.append(
                    f"   └ 🔹 *Model Total:* `{tot_tok:,}` tokens  |  *${m_cost:.5f} USD*"
                )

        lines.append("━" * 38)
        lines.append("*📈 GRAND TOTAL THREAD USAGE:*")
        lines.append(f"📥 Total Input Tokens: *{cost_data['grand_prompt_tokens']:,}*")
        lines.append(f"📤 Total Output Tokens: *{cost_data['grand_completion_tokens']:,}*")
        lines.append(f"🔢 Total Tokens: *{cost_data['grand_total_tokens']:,}*")
        lines.append(f"💰 *TOTAL COST:* *${cost_data['grand_total_cost_usd']:.5f} USD*")

        if models:
            lines.append("━" * 38)
            lines.append("*🌐 PRICING SOURCES & WEB REFERENCES:*")
            for model_name, m_info in models.items():
                s_name = m_info.get("source", "Official Docs / Web")
                s_url = m_info.get("source_url", "")
                if s_url:
                    lines.append(f"• *`{model_name}`*: <{s_url}|{s_url}>")
                else:
                    lines.append(f"• *`{model_name}`*: _{s_name}_")

        lines.append("━" * 38)
        lines.append(
            "💡 *METRICS NOTICE:* The primary metric is *token consumption*. "
            "Displayed prices are orientational estimates only and may be inaccurate, so it is recommended to verify them on the provider's official pricing pages."
        )

        curr_time = time.strftime("%H:%M:%S")
        lines.append(f"\n_🕒 Last updated: {curr_time} | Rates refreshed every 7 days_")

        return "\n".join(lines)

    async def update_usage_for_thread(
        self,
        channel_id: str,
        thread_ts: str,
        is_completed: bool = False,
    ) -> dict[str, Any]:
        """
        Asynchronously creates or updates the single dedicated usage report message
        inside the usage channel for the specified thread.
        """
        lock = self.get_lock(channel_id, thread_ts)

        async with lock:
            try:
                logger.info(
                    f"[USAGE AGENT] Updating usage report for thread {thread_ts} (channel {channel_id}, completed={is_completed})..."
                )

                usage_ch_id = await get_or_create_usage_channel()
                if not usage_ch_id:
                    logger.error(
                        f"[USAGE AGENT] Could not resolve or create '{USAGE_CHANNEL_NAME}' channel. Aborting usage update."
                    )
                    return {"ok": False, "error": "Usage channel not available"}

                formatted_text = await self.format_usage_slack_message(
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    is_completed=is_completed,
                )

                entry = self.get_thread_meta(channel_id, thread_ts) or {}
                usage_msg_ts = entry.get("usage_msg_ts")

                if not usage_msg_ts:
                    post_res = await post_usage_message(
                        channel_id=usage_ch_id,
                        text=formatted_text,
                        username="Izvor Usage Analyst",
                        icon_emoji=":bar_chart:",
                    )
                    new_msg_ts = post_res.get("ts")
                    if new_msg_ts:
                        self.upsert_thread_meta(channel_id, thread_ts, usage_msg_ts=new_msg_ts)
                        logger.info(
                            f"[USAGE AGENT] Created new usage report message {new_msg_ts} in channel {usage_ch_id} for thread {thread_ts}"
                        )
                    return post_res
                else:
                    update_res = await update_usage_message(
                        channel_id=usage_ch_id,
                        message_ts=usage_msg_ts,
                        text=formatted_text,
                    )
                    logger.info(
                        f"[USAGE AGENT] Updated existing usage report message {usage_msg_ts} in channel {usage_ch_id} for thread {thread_ts}"
                    )
                    return update_res

            except asyncio.CancelledError:
                logger.info(f"[USAGE AGENT] Usage update task cancelled for thread {thread_ts}")
                raise
            except (TimeoutError, asyncio.TimeoutError) as e:
                logger.warning(f"[USAGE AGENT] Timeout updating usage for thread {thread_ts}: {e}")
                return {"ok": False, "error": str(e)}
            except (KeyError, ValueError, TypeError) as e:
                logger.error(f"[USAGE AGENT] Data error updating usage for thread {thread_ts}: {e}")
                return {"ok": False, "error": str(e)}
            except Exception as e:
                logger.exception(f"[USAGE AGENT] Error updating usage for thread {thread_ts}")
                return {"ok": False, "error": str(e)}

    async def _usage_worker(
        self,
        channel_id: str,
        thread_ts: str,
        is_completed: bool = False,
    ) -> None:
        """
        Background worker loop that processes usage report updates and automatically
        picks up any pending updates queued while the worker was busy.
        """
        thread_key = (channel_id, thread_ts)
        pending_initial_completed = is_completed
        try:
            while True:
                self.pending_usage_updates[thread_key] = False
                iter_completed = pending_initial_completed or self.pending_usage_completed.pop(
                    thread_key, False
                )
                pending_initial_completed = False

                await self.update_usage_for_thread(
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    is_completed=iter_completed,
                )
                if not self.pending_usage_updates.get(thread_key, False):
                    break
        except asyncio.CancelledError:
            logger.info(f"[USAGE AGENT] Worker task cancelled for thread {thread_ts}")
        finally:
            self.running_usage_tasks.pop(thread_key, None)
            self.pending_usage_updates.pop(thread_key, None)
            self.pending_usage_completed.pop(thread_key, None)

    def trigger_update(
        self,
        channel_id: str,
        thread_ts: str,
        is_completed: bool = False,
    ) -> asyncio.Task | None:
        """
        Dispatches live thread usage calculation/update as a non-blocking background task.
        Coalesces rapid successive triggers so only one worker runs with pending follow-up.
        """
        thread_key = (channel_id, thread_ts)
        if is_completed:
            self.pending_usage_completed[thread_key] = True

        existing_task = self.running_usage_tasks.get(thread_key)
        if existing_task and not existing_task.done():
            self.pending_usage_updates[thread_key] = True
            logger.debug(
                f"[USAGE AGENT] Usage update already active for thread {thread_ts}. Queued pending sync."
            )
            return existing_task

        coro = self._usage_worker(
            channel_id=channel_id,
            thread_ts=thread_ts,
            is_completed=is_completed,
        )
        try:
            task = safe_create_task(coro, name=f"usage_update_{thread_ts}")
            self.running_usage_tasks[thread_key] = task
            self.pending_usage_updates[thread_key] = False
            return task
        except RuntimeError as e:
            coro.close()
            logger.error(
                f"[USAGE AGENT] Runtime error dispatching background usage update task: {e}"
            )
            return None
        except Exception as e:  # noqa: BLE001 - Background task dispatcher error boundary
            coro.close()
            logger.error(
                f"[USAGE AGENT] Failed to dispatch background usage update task: {e}"
            )
            return None


# Default singleton instance
usage_tracker = ThreadUsageTracker()

_record_thread_tokens_override: Any = None
_calculate_thread_costs_override: Any = None
_trigger_usage_update_override: Any = None


def configure_record_thread_tokens(fn: Any) -> None:
    """Configures an in-memory record_thread_tokens override for tests and DI."""
    global _record_thread_tokens_override
    _record_thread_tokens_override = fn


def reset_record_thread_tokens() -> None:
    """Resets the in-memory record_thread_tokens override."""
    global _record_thread_tokens_override
    _record_thread_tokens_override = None


def configure_calculate_thread_costs(fn: Any) -> None:
    """Configures an in-memory calculate_thread_costs override for tests and DI."""
    global _calculate_thread_costs_override
    _calculate_thread_costs_override = fn


def reset_calculate_thread_costs() -> None:
    """Resets the in-memory calculate_thread_costs override."""
    global _calculate_thread_costs_override
    _calculate_thread_costs_override = None


def configure_trigger_usage_update(fn: Any) -> None:
    """Configures an in-memory trigger_usage_update override for tests and DI."""
    global _trigger_usage_update_override
    _trigger_usage_update_override = fn


def reset_trigger_usage_update() -> None:
    """Resets the in-memory trigger_usage_update override."""
    global _trigger_usage_update_override
    _trigger_usage_update_override = None


def record_thread_tokens(
    channel_id: str,
    thread_ts: str,
    model_name: str,
    prompt_tokens: int,
    completion_tokens: int,
    channel_name: str | None = None,
) -> dict[str, Any]:
    if _record_thread_tokens_override is not None:
        return _record_thread_tokens_override(
            channel_id=channel_id,
            thread_ts=thread_ts,
            model_name=model_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            channel_name=channel_name,
        )
    return usage_tracker.record_tokens(
        channel_id=channel_id,
        thread_ts=thread_ts,
        model_name=model_name,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        channel_name=channel_name,
    )


def calculate_thread_costs(channel_id: str, thread_ts: str) -> dict[str, Any]:
    if _calculate_thread_costs_override is not None:
        return _calculate_thread_costs_override(channel_id, thread_ts)
    return usage_tracker.calculate_thread_costs(channel_id, thread_ts)


async def format_usage_slack_message(
    channel_id: str, thread_ts: str, is_completed: bool = False
) -> str:
    return await usage_tracker.format_usage_slack_message(
        channel_id, thread_ts, is_completed=is_completed
    )


async def update_usage_for_thread(
    channel_id: str,
    thread_ts: str,
    is_completed: bool = False,
) -> dict[str, Any]:
    return await usage_tracker.update_usage_for_thread(
        channel_id, thread_ts, is_completed=is_completed
    )


def trigger_usage_update(
    channel_id: str,
    thread_ts: str,
    is_completed: bool = False,
) -> asyncio.Task | None:
    if _trigger_usage_update_override is not None:
        return _trigger_usage_update_override(channel_id, thread_ts, is_completed=is_completed)
    return usage_tracker.trigger_update(
        channel_id, thread_ts, is_completed=is_completed
    )


def cleanup_thread_usage(channel_id: str, thread_ts: str) -> None:
    usage_tracker.cleanup_thread(channel_id, thread_ts)


def reset_thread_usage() -> None:
    usage_tracker.reset_runtime_state()


try:
    from orchestration.thread_state import thread_state_manager

    thread_state_manager.register_cleanup_hook(cleanup_thread_usage)
    thread_state_manager.register_reset_hook(reset_thread_usage)
except (ImportError, AttributeError, TypeError) as _e:
    logger.debug(f"[USAGE AGENT] Could not register thread_state hooks: {_e}")

