import asyncio
from datetime import datetime
import json
import logging
from pathlib import Path
import time
from typing import Any

from pricing_service import get_model_pricing, refresh_model_pricing
from slack_client import (
    get_channel_name,
    get_or_create_usage_channel,
    post_usage_message,
    update_usage_message,
)

logger = logging.getLogger(__name__)

# Persistence storage file for thread token/cost metrics (stored in workspace root)
USAGE_STORE_FILE = Path(__file__).resolve().parent.parent / "thread_usage_data.json"

# In-memory storage for thread usage: (channel_id, thread_ts) -> thread_data_dict
THREAD_USAGE_DATA: dict[tuple[str, str], dict[str, Any]] = {}

# Locks to ensure atomic sequential updates per thread
USAGE_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}

# Background worker queue management
_RUNNING_USAGE_TASKS: dict[tuple[str, str], asyncio.Task] = {}
_PENDING_USAGE_UPDATES: dict[tuple[str, str], bool] = {}
_PENDING_USAGE_COMPLETED: dict[tuple[str, str], bool] = {}


def _get_usage_lock(channel_id: str, thread_ts: str) -> asyncio.Lock:
    """Returns or creates an asyncio.Lock for a specific thread."""
    thread_key = (channel_id, thread_ts)
    if thread_key not in USAGE_LOCKS:
        USAGE_LOCKS[thread_key] = asyncio.Lock()
    return USAGE_LOCKS[thread_key]


def load_thread_usage_data() -> None:
    """Loads persistent thread usage data from JSON store file."""
    THREAD_USAGE_DATA.clear()
    if not USAGE_STORE_FILE.exists():
        return
    try:
        raw = json.loads(USAGE_STORE_FILE.read_text(encoding="utf-8"))
        for k_str, data in raw.items():
            if ":" in k_str:
                parts = k_str.split(":", 1)
                THREAD_USAGE_DATA[(parts[0], parts[1])] = data
        logger.info(
            f"[USAGE AGENT] Loaded usage tracking for {len(THREAD_USAGE_DATA)} threads from {USAGE_STORE_FILE.name}"
        )
    except Exception as e:
        logger.error(f"[USAGE AGENT] Error loading thread usage store: {e}")


def save_thread_usage_data() -> None:
    """Saves thread usage data dictionary to JSON store file."""
    try:
        serializable = {
            f"{k[0]}:{k[1]}": v for k, v in THREAD_USAGE_DATA.items()
        }
        USAGE_STORE_FILE.write_text(json.dumps(serializable, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error(f"[USAGE AGENT] Error saving thread usage data: {e}")


# Initialize in-memory cache from disk
load_thread_usage_data()


def parse_thread_creation_time(thread_ts: str) -> str:
    """Parses Slack thread_ts epoch string into a human-readable datetime string."""
    try:
        ts_float = float(thread_ts)
        dt = datetime.fromtimestamp(ts_float)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "Unknown"


def record_thread_tokens(
    channel_id: str,
    thread_ts: str,
    model_name: str,
    prompt_tokens: int,
    completion_tokens: int,
    channel_name: str | None = None,
    cached_tokens: int = 0,
) -> dict[str, Any]:
    """
    Accumulates input, output, and cached tokens for a specific model within a thread.
    Thread-safe and persists to thread_usage_data.json.
    """
    thread_key = (channel_id, thread_ts)
    if thread_key not in THREAD_USAGE_DATA:
        created_at_str = parse_thread_creation_time(thread_ts)
        THREAD_USAGE_DATA[thread_key] = {
            "channel_id": channel_id,
            "channel_name": channel_name or f"#{channel_id}",
            "thread_ts": thread_ts,
            "created_at_str": created_at_str,
            "usage_msg_ts": None,
            "models": {},
            "is_completed": False,
            "last_updated": time.time(),
        }

    entry = THREAD_USAGE_DATA[thread_key]
    if channel_name:
        entry["channel_name"] = channel_name

    models_map = entry.setdefault("models", {})
    if model_name not in models_map:
        models_map[model_name] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cached_tokens": 0,
            "total_tokens": 0,
        }

    m_stats = models_map[model_name]
    m_stats["prompt_tokens"] += int(prompt_tokens)
    m_stats["completion_tokens"] += int(completion_tokens)
    m_stats["cached_tokens"] = m_stats.get("cached_tokens", 0) + int(cached_tokens)
    m_stats["total_tokens"] = m_stats["prompt_tokens"] + m_stats["completion_tokens"]

    entry["last_updated"] = time.time()
    save_thread_usage_data()

    logger.info(
        f"[USAGE AGENT] Recorded tokens for thread {thread_ts} | Model: {model_name} | "
        f"New Prompt={prompt_tokens}, Completion={completion_tokens}, Cached={cached_tokens} | "
        f"Model Cumulative: Prompt={m_stats['prompt_tokens']}, Completion={m_stats['completion_tokens']}, Cached={m_stats['cached_tokens']}, Total={m_stats['total_tokens']}"
    )
    return entry


def calculate_thread_costs(channel_id: str, thread_ts: str) -> dict[str, Any]:
    """
    Computes exact monetary costs per model and grand totals for a given thread.
    """
    thread_key = (channel_id, thread_ts)
    entry = THREAD_USAGE_DATA.get(thread_key, {})
    models_data = entry.get("models", {})

    models_breakdown = {}
    grand_prompt_tokens = 0
    grand_completion_tokens = 0
    grand_cached_tokens = 0
    grand_total_tokens = 0
    grand_total_cost_usd = 0.0

    for model_name, stats in models_data.items():
        p_tokens = stats.get("prompt_tokens", 0)
        c_tokens = stats.get("completion_tokens", 0)
        cached_tok = stats.get("cached_tokens", 0)
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
            "cached_tokens": cached_tok,
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
        grand_cached_tokens += cached_tok
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
        "grand_cached_tokens": grand_cached_tokens,
        "grand_total_tokens": grand_total_tokens,
        "grand_total_cost_usd": grand_total_cost_usd,
    }


async def format_usage_slack_message(
    channel_id: str, thread_ts: str, is_completed: bool = False
) -> str:
    """
    Generates the formatted Slack Mrkdwn message representing the current
    token and monetary consumption for the thread.
    """
    thread_key = (channel_id, thread_ts)
    existing_entry = THREAD_USAGE_DATA.get(thread_key, {})
    existing_name = existing_entry.get("channel_name")

    if existing_name and not (existing_name.startswith("#C") or existing_name.startswith("#D")):
        ch_name = existing_name
    else:
        ch_name = await get_channel_name(channel_id)
        if thread_key in THREAD_USAGE_DATA:
            THREAD_USAGE_DATA[thread_key]["channel_name"] = ch_name

    if thread_key in THREAD_USAGE_DATA and is_completed:
        THREAD_USAGE_DATA[thread_key]["is_completed"] = True

    cost_data = calculate_thread_costs(channel_id, thread_ts)
    created_at = cost_data["created_at_str"]
    completed_status = is_completed or cost_data["is_completed"]

    status_str = "✅ *Završeno / Completed*" if completed_status else "🟢 *U tijeku / Active*"

    lines = [
        "📊 *IZVJEŠTAJ POTROŠNJE THREADA (TOKENI & TROŠKOVI)*",
        "━" * 38,
        f"📍 *Kanal:* `{ch_name}`",
        f"📅 *Kreiran thread:* `{created_at}`",
        f"🧵 *Thread TS:* `{thread_ts}`",
        f"🔄 *Status rasprave:* {status_str}",
        "━" * 38,
        "*POTROŠNJA PO MODELIMA:*",
    ]

    models = cost_data["models"]
    if not models:
        lines.append("_(Još nema zabilježenih tokena za ovaj thread)_")
    else:
        for model_name, m_info in models.items():
            clean_name = model_name
            p_tok = m_info["prompt_tokens"]
            c_tok = m_info["completion_tokens"]
            cached_tok = m_info.get("cached_tokens", 0)
            tot_tok = m_info["total_tokens"]
            in_cost = m_info["input_cost_usd"]
            out_cost = m_info["output_cost_usd"]
            m_cost = m_info["total_cost_usd"]
            in_rate = m_info["input_price_per_1m"]
            out_rate = m_info["output_price_per_1m"]

            lines.append(f"• *`{clean_name}`*")
            lines.append(
                f"   ├ 📥 *Ulazni (Prompt):* `{p_tok:,}` tok.  |  *${in_cost:.5f}*  _(${in_rate:.2f}/1M)_"
            )
            lines.append(
                f"   ├ 📤 *Izlazni (Output):* `{c_tok:,}` tok.  |  *${out_cost:.5f}*  _(${out_rate:.2f}/1M)_"
            )
            lines.append(
                f"   ├ 💾 *Predmemorirani (Cached):* `{cached_tok:,}` tok.  _(nije uračunato u trošak)_"
            )
            lines.append(
                f"   └ 🔹 *Ukupno model:* `{tot_tok:,}` tokena  |  *${m_cost:.5f} USD*"
            )

    lines.append("━" * 38)
    lines.append("*📈 UKUPNA POTROŠNJA THREADA:*")
    lines.append(f"📥 Ukupno ulaznih tokena: *{cost_data['grand_prompt_tokens']:,}*")
    lines.append(f"📤 Ukupno izlaznih tokena: *{cost_data['grand_completion_tokens']:,}*")
    lines.append(f"💾 Ukupno predmemoriranih tokena: *{cost_data['grand_cached_tokens']:,}* _(nije u trošku)_")
    lines.append(f"🔢 Ukupno tokena: *{cost_data['grand_total_tokens']:,}*")
    lines.append(f"💰 *UKUPAN TROŠAK:* *${cost_data['grand_total_cost_usd']:.5f} USD*")

    if models:
        lines.append("━" * 38)
        lines.append("*🌐 IZVORI TARIFA I WEB REFERENCE:*")
        for model_name, m_info in models.items():
            s_name = m_info.get("source", "Official Docs / Web")
            s_url = m_info.get("source_url", "")
            if s_url:
                lines.append(f"• *`{model_name}`*: <{s_url}|{s_url}>")
            else:
                lines.append(f"• *`{model_name}`*: _{s_name}_")

    curr_time = time.strftime("%H:%M:%S")
    lines.append(f"\n_🕒 Zadnje ažuriranje: {curr_time} | Osvježavanje tarifa svakih 7 dana_")

    return "\n".join(lines)


async def update_usage_for_thread(
    channel_id: str,
    thread_ts: str,
    is_completed: bool = False,
) -> dict[str, Any]:
    """
    Asynchronously creates or updates the single dedicated usage report message
    inside the 'cailogics-usage' channel for the specified thread.
    """
    thread_key = (channel_id, thread_ts)
    lock = _get_usage_lock(channel_id, thread_ts)

    async with lock:
        try:
            logger.info(
                f"[USAGE AGENT] Updating usage report for thread {thread_ts} (channel {channel_id}, completed={is_completed})..."
            )

            # 1. Resolve or create the 'cailogics-usage' channel
            usage_ch_id = await get_or_create_usage_channel()
            if not usage_ch_id:
                logger.error(
                    "[USAGE AGENT] Could not resolve or create 'cailogics-usage' channel. Aborting usage update."
                )
                return {"ok": False, "error": "Usage channel not available"}

            # 2. Format the updated usage message
            formatted_text = await format_usage_slack_message(
                channel_id=channel_id,
                thread_ts=thread_ts,
                is_completed=is_completed,
            )

            # 3. Post new message or update existing in-place
            entry = THREAD_USAGE_DATA.get(thread_key, {})
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
                    save_thread_usage_data()
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
        except Exception as e:
            logger.exception(f"[USAGE AGENT] Error updating usage for thread {thread_ts}: {e}")
            return {"ok": False, "error": str(e)}


async def _usage_worker(
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
            _PENDING_USAGE_UPDATES[thread_key] = False
            current_completed = current_completed or _PENDING_USAGE_COMPLETED.get(
                thread_key, False
            )
            _PENDING_USAGE_COMPLETED[thread_key] = False

            await update_usage_for_thread(
                channel_id=channel_id,
                thread_ts=thread_ts,
                is_completed=current_completed,
            )
            if not _PENDING_USAGE_UPDATES.get(thread_key, False):
                break
    except asyncio.CancelledError:
        logger.info(f"[USAGE AGENT] Worker task cancelled for thread {thread_ts}")
    finally:
        _RUNNING_USAGE_TASKS.pop(thread_key, None)
        _PENDING_USAGE_UPDATES.pop(thread_key, None)
        _PENDING_USAGE_COMPLETED.pop(thread_key, None)


def trigger_usage_update(
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
        _PENDING_USAGE_COMPLETED[thread_key] = True

    existing_task = _RUNNING_USAGE_TASKS.get(thread_key)
    if existing_task and not existing_task.done():
        _PENDING_USAGE_UPDATES[thread_key] = True
        logger.debug(
            f"[USAGE AGENT] Usage update already active for thread {thread_ts}. Queued pending sync."
        )
        return existing_task

    try:
        task = asyncio.create_task(
            _usage_worker(
                channel_id=channel_id,
                thread_ts=thread_ts,
                is_completed=is_completed,
            )
        )
        _RUNNING_USAGE_TASKS[thread_key] = task
        _PENDING_USAGE_UPDATES[thread_key] = False
        return task
    except Exception as e:
        logger.error(
            f"[USAGE AGENT] Failed to dispatch background usage update task: {e}"
        )
        return None
