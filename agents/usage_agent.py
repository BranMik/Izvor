import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.pricing_service import get_model_pricing
from services.slack_client import (
    get_channel_name,
    get_or_create_usage_channel,
    post_usage_message,
    update_usage_message,
)

logger = logging.getLogger(__name__)

# Persistence storage file for thread token/cost metrics
USAGE_STORE_FILE = Path(__file__).resolve().parent.parent / "data" / "thread" / "thread_usage_data.json"


def parse_thread_creation_time(thread_ts: str) -> str:
    """Parses Slack thread_ts epoch string into a human-readable datetime string."""
    try:
        ts_float = float(thread_ts)
        dt = datetime.fromtimestamp(ts_float, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError, OSError):
        return "Unknown"


class ThreadUsageTracker:
    """Tracks token consumption, monetary expenses, and live summary card updates in #cailogics-usage."""

    def __init__(self, store_file: Path | None = None):
        self.store_file = store_file or USAGE_STORE_FILE
        self.thread_usage_data: dict[tuple[str, str], dict[str, Any]] = {}
        self.usage_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self.running_usage_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self.pending_usage_updates: dict[tuple[str, str], bool] = {}
        self.pending_usage_completed: dict[tuple[str, str], bool] = {}
        self.load()

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

    def load(self) -> None:
        """Loads persistent thread usage data from JSON store file."""
        self.thread_usage_data.clear()
        if not self.store_file.exists():
            return
        try:
            raw = json.loads(self.store_file.read_text(encoding="utf-8"))
            for k_str, data in raw.items():
                if ":" in k_str:
                    parts = k_str.split(":", 1)
                    self.thread_usage_data[(parts[0], parts[1])] = data
            logger.info(
                f"[USAGE AGENT] Loaded usage tracking for {len(self.thread_usage_data)} threads from {self.store_file.name}"
            )
        except (OSError, json.JSONDecodeError, UnicodeError, ValueError) as e:
            logger.error(f"[USAGE AGENT] Error loading thread usage store: {e}")

    def save(self) -> None:
        """Saves thread usage data dictionary to JSON store file atomically."""
        try:
            self.store_file.parent.mkdir(parents=True, exist_ok=True)
            serializable = {
                f"{k[0]}:{k[1]}": v for k, v in self.thread_usage_data.items()
            }
            content = json.dumps(serializable, indent=2)

            import os
            import tempfile

            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=str(self.store_file.parent),
                prefix=f"{self.store_file.stem}_",
                suffix=".tmp",
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, self.store_file)
            except OSError:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                raise
        except (OSError, TypeError, ValueError) as e:
            logger.error(f"[USAGE AGENT] Error saving thread usage data: {e}")

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
        Thread-safe and persists to thread_usage_data.json.
        """
        thread_key = (channel_id, thread_ts)
        if thread_key not in self.thread_usage_data:
            created_at_str = parse_thread_creation_time(thread_ts)
            self.thread_usage_data[thread_key] = {
                "channel_id": channel_id,
                "channel_name": channel_name or f"#{channel_id}",
                "thread_ts": thread_ts,
                "created_at_str": created_at_str,
                "usage_msg_ts": None,
                "models": {},
                "is_completed": False,
                "last_updated": time.time(),
            }

        entry = self.thread_usage_data[thread_key]
        if channel_name:
            entry["channel_name"] = channel_name

        models_map = entry.setdefault("models", {})
        if model_name not in models_map:
            models_map[model_name] = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }

        m_stats = models_map[model_name]
        m_stats["prompt_tokens"] += int(prompt_tokens)
        m_stats["completion_tokens"] += int(completion_tokens)
        m_stats["total_tokens"] = m_stats["prompt_tokens"] + m_stats["completion_tokens"]

        entry["last_updated"] = time.time()
        self.save()

        logger.info(
            f"[USAGE AGENT] Recorded tokens for thread {thread_ts} | Model: {model_name} | "
            f"New Prompt={prompt_tokens}, Completion={completion_tokens} | "
            f"Model Cumulative: Prompt={m_stats['prompt_tokens']}, Completion={m_stats['completion_tokens']}, Total={m_stats['total_tokens']}"
        )
        return entry

    def calculate_thread_costs(self, channel_id: str, thread_ts: str) -> dict[str, Any]:
        """
        Computes exact monetary costs per model and grand totals for a given thread.
        """
        thread_key = (channel_id, thread_ts)
        entry = self.thread_usage_data.get(thread_key, {})
        models_data = entry.get("models", {})

        models_breakdown = {}
        grand_prompt_tokens = 0
        grand_completion_tokens = 0
        grand_total_tokens = 0
        grand_total_cost_usd = 0.0

        for model_name, stats in models_data.items():
            p_tokens = stats.get("prompt_tokens", 0)
            c_tokens = stats.get("completion_tokens", 0)
            t_tokens = stats.get("total_tokens", p_tokens + c_tokens)

            pricing = get_model_pricing(model_name)
            in_rate_1m = pricing.get("input_price_per_1m", 1.0)
            out_rate_1m = pricing.get("output_price_per_1m", 3.0)

            in_cost = (p_tokens / 1_000_000.0) * in_rate_1m
            out_cost = (c_tokens / 1_000_000.0) * out_rate_1m
            model_total_cost = in_cost + out_cost

            models_breakdown[model_name] = {
                "prompt_tokens": p_tokens,
                "completion_tokens": c_tokens,
                "total_tokens": t_tokens,
                "input_price_per_1m": in_rate_1m,
                "output_price_per_1m": out_rate_1m,
                "input_cost_usd": in_cost,
                "output_cost_usd": out_cost,
                "total_cost_usd": model_total_cost,
                "source": pricing.get("source", "Pricing Cache"),
                "source_url": pricing.get("source_url", ""),
            }

            grand_prompt_tokens += p_tokens
            grand_completion_tokens += c_tokens
            grand_total_tokens += t_tokens
            grand_total_cost_usd += model_total_cost

        return {
            "channel_id": channel_id,
            "channel_name": entry.get("channel_name", f"#{channel_id}"),
            "thread_ts": thread_ts,
            "created_at_str": entry.get("created_at_str", parse_thread_creation_time(thread_ts)),
            "is_completed": entry.get("is_completed", False),
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
        thread_key = (channel_id, thread_ts)
        existing_entry = self.thread_usage_data.get(thread_key, {})
        existing_name = existing_entry.get("channel_name")

        if existing_name and not existing_name.startswith(("#C", "#D")):
            ch_name = existing_name
        else:
            ch_name = await get_channel_name(channel_id)
            if thread_key in self.thread_usage_data:
                self.thread_usage_data[thread_key]["channel_name"] = ch_name

        if thread_key in self.thread_usage_data and is_completed:
            self.thread_usage_data[thread_key]["is_completed"] = True

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
        inside the 'cailogics-usage' channel for the specified thread.
        """
        thread_key = (channel_id, thread_ts)
        lock = self.get_lock(channel_id, thread_ts)

        async with lock:
            try:
                logger.info(
                    f"[USAGE AGENT] Updating usage report for thread {thread_ts} (channel {channel_id}, completed={is_completed})..."
                )

                usage_ch_id = await get_or_create_usage_channel()
                if not usage_ch_id:
                    logger.error(
                        "[USAGE AGENT] Could not resolve or create 'cailogics-usage' channel. Aborting usage update."
                    )
                    return {"ok": False, "error": "Usage channel not available"}

                formatted_text = await self.format_usage_slack_message(
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    is_completed=is_completed,
                )

                entry = self.thread_usage_data.get(thread_key, {})
                usage_msg_ts = entry.get("usage_msg_ts")

                if not usage_msg_ts:
                    post_res = await post_usage_message(
                        channel_id=usage_ch_id,
                        text=formatted_text,
                        username="CaiLogics Usage Analyst",
                        icon_emoji=":bar_chart:",
                    )
                    new_msg_ts = post_res.get("ts")
                    if new_msg_ts:
                        entry["usage_msg_ts"] = new_msg_ts
                        self.save()
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
        current_completed = is_completed
        try:
            while True:
                self.pending_usage_updates[thread_key] = False
                current_completed = current_completed or self.pending_usage_completed.get(
                    thread_key, False
                )
                self.pending_usage_completed[thread_key] = False

                await self.update_usage_for_thread(
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    is_completed=current_completed,
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

        try:
            task = asyncio.create_task(
                self._usage_worker(
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    is_completed=is_completed,
                )
            )
            self.running_usage_tasks[thread_key] = task
            self.pending_usage_updates[thread_key] = False
            return task
        except RuntimeError as e:
            logger.error(
                f"[USAGE AGENT] Runtime error dispatching background usage update task: {e}"
            )
            return None
        except Exception as e:  # noqa: BLE001 - Background task dispatcher error boundary
            logger.error(
                f"[USAGE AGENT] Failed to dispatch background usage update task: {e}"
            )
            return None


# Default singleton instance
usage_tracker = ThreadUsageTracker()

# Module-level variable mapping for backward compatibility
THREAD_USAGE_DATA = usage_tracker.thread_usage_data
USAGE_LOCKS = usage_tracker.usage_locks
_RUNNING_USAGE_TASKS = usage_tracker.running_usage_tasks
_PENDING_USAGE_UPDATES = usage_tracker.pending_usage_updates
_PENDING_USAGE_COMPLETED = usage_tracker.pending_usage_completed


# Backward-compatible function aliases
def _get_usage_lock(channel_id: str, thread_ts: str) -> asyncio.Lock:
    return usage_tracker.get_lock(channel_id, thread_ts)


def load_thread_usage_data() -> None:
    usage_tracker.load()


def save_thread_usage_data() -> None:
    usage_tracker.save()


def record_thread_tokens(
    channel_id: str,
    thread_ts: str,
    model_name: str,
    prompt_tokens: int,
    completion_tokens: int,
    channel_name: str | None = None,
) -> dict[str, Any]:
    return usage_tracker.record_tokens(
        channel_id=channel_id,
        thread_ts=thread_ts,
        model_name=model_name,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        channel_name=channel_name,
    )


def calculate_thread_costs(channel_id: str, thread_ts: str) -> dict[str, Any]:
    return usage_tracker.calculate_thread_costs(channel_id, thread_ts)


def get_thread_total_cost_usd(channel_id: str, thread_ts: str) -> float:
    return usage_tracker.get_thread_total_cost_usd(channel_id, thread_ts)


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

