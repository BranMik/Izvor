import asyncio
import copy
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

import litellm

from core.config import (
    get_orchestrator_fast_model,
    get_pricing_agent_prompt,
    get_system_agent_model,
)
from core.llm_utils import execute_llm_with_retry
from core.paths import BASELINE_PRICES_FILE, PRICING_CACHE_FILE

from .pricing_feeds import (
    MAX_VALID_PRICE_PER_1M as MAX_VALID_PRICE_PER_1M,
    MIN_VALID_PRICE_PER_1M as MIN_VALID_PRICE_PER_1M,
    clear_feed_cache,
    fetch_pricing_from_feeds,
    is_valid_price_pair as _is_valid_price_pair,
    is_within_sanity_bounds as _is_within_sanity_bounds,
    normalize_model_name as _normalize_model_name,
)
from .web_tools import fetch_web_page, web_search

logger = logging.getLogger(__name__)


def get_pricing_cache_ttl_seconds() -> int:
    try:
        from config_store.system_settings import get_pricing_cache_ttl_seconds

        return get_pricing_cache_ttl_seconds()
    except Exception:  # noqa: BLE001
        return 7 * 24 * 3600


def get_pricing_benchmark_url() -> str:
    try:
        from config_store.system_settings import get_pricing_benchmark_url

        return get_pricing_benchmark_url()
    except Exception:  # noqa: BLE001
        return "https://artificialanalysis.ai/models"


# Cache TTL default in seconds
CACHE_TTL_SECONDS = 7 * 24 * 3600
MAX_PRICING_AGENT_TOOL_ROUNDS = 3
POISONED_PRICING_SOURCES: frozenset[str] = frozenset({"Web Search Fallback"})

# LiteLLM tool schemas for the pricing research agent
PRICING_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the live web for official documentation, AI model pricing, cost benchmarks, and provider rate tables. Returns a list of results with title, URL, and snippet.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query string to look up on the web.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of search results to return (1 to 10). Defaults to 5.",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_web_page",
            "description": "Fetch and extract clean readable text content from a web page URL (e.g. provider pricing documentation, AI benchmarks).",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL of the webpage to scrape and read.",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum number of characters of text content to extract. Defaults to 12000.",
                        "default": 12000,
                    },
                },
                "required": ["url"],
            },
        },
    },
]


def load_baseline_pricing(config_path: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """
    Loads baseline model pricing dictionary from JSON configuration file.
    Defaults to data/pricing/baseline_prices.json with safe fallback.
    """
    target = Path(config_path) if config_path else BASELINE_PRICES_FILE
    if target.exists():
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {_normalize_model_name(k): v for k, v in data.items()}
            logger.error(f"[PRICING] Expected dict in {target.name}, got {type(data)}")
        except (OSError, json.JSONDecodeError, UnicodeError, ValueError) as e:
            logger.error(f"[PRICING] Error loading baseline prices from {target}: {e}")
    return {}


# Industry baseline fallback pricing (USD per 1,000,000 tokens) loaded from JSON config
BASELINE_MODEL_PRICING: dict[str, dict[str, Any]] = load_baseline_pricing()

# Conservative high-tier safety fallback (fail expensive) for unknown models
DEFAULT_FALLBACK_PRICING: dict[str, Any] = {
    "input_price_per_1m": 15.00,
    "output_price_per_1m": 75.00,
    "currency": "USD",
    "source": "High-Tier Safety Fallback",
    "source_url": get_pricing_benchmark_url(),
}


def _get_baseline_signature(model_name: str) -> str | None:
    """
    Returns a stable signature for a model's explicit entry in baseline_prices.json,
    or None if the model is not directly listed in baseline_prices.json.
    """
    norm = _normalize_model_name(model_name)
    norm_bare = norm.rsplit("/", 1)[-1] if "/" in norm else norm

    matched: dict[str, Any] | None = None
    if norm in BASELINE_MODEL_PRICING:
        matched = BASELINE_MODEL_PRICING[norm]
    else:
        for key, p in BASELINE_MODEL_PRICING.items():
            key_bare = key.rsplit("/", 1)[-1] if "/" in key else key
            if key_bare == norm_bare:
                matched = p
                break

    if not isinstance(matched, dict):
        return None
    in_p = matched.get("input_price_per_1m")
    out_p = matched.get("output_price_per_1m")
    if not _is_valid_price_pair(in_p, out_p):
        return None
    return f"{float(in_p):.6f}:{float(out_p):.6f}"


def _is_cache_entry_valid(model_name: str, entry: Any) -> bool:
    """
    Validates that a cached pricing entry is well-formed, not produced by the legacy
    blind regex fallback, and not invalidated by a newer baseline_prices.json entry.
    """
    if not isinstance(entry, dict):
        return False
    if entry.get("source") in POISONED_PRICING_SOURCES:
        return False
    if not _is_valid_price_pair(entry.get("input_price_per_1m"), entry.get("output_price_per_1m")):
        return False
    cached_sig = entry.get("baseline_signature")
    if cached_sig is not None:
        current_sig = _get_baseline_signature(model_name)
        if current_sig is not None and str(cached_sig) != current_sig:
            return False
    return True


def get_baseline_for_model(
    model_name: str,
    model_cost_registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Returns baseline fallback pricing for a given model identifier.
    Priority order:
    1. Exact or bare-model exact match in data/pricing/baseline_prices.json (validated)
    2. litellm.model_cost community registry (exact / normalized / bare)
    3. Longest-prefix key-in-norm match in data/pricing/baseline_prices.json (validated)
    4. Fallback norm-in-key match in data/pricing/baseline_prices.json (validated)
    5. DEFAULT_FALLBACK_PRICING ($15.00/$75.00 per 1M)
    """
    norm = _normalize_model_name(model_name)
    norm_bare = norm.rsplit("/", 1)[-1] if "/" in norm else norm

    if norm in BASELINE_MODEL_PRICING:
        base = BASELINE_MODEL_PRICING[norm]
        if _is_valid_price_pair(base.get("input_price_per_1m"), base.get("output_price_per_1m")):
            return dict(base)

    for key, p in BASELINE_MODEL_PRICING.items():
        key_bare = key.rsplit("/", 1)[-1] if "/" in key else key
        if key_bare == norm_bare and _is_valid_price_pair(p.get("input_price_per_1m"), p.get("output_price_per_1m")):
            return dict(p)

    # Primary registry lookup: litellm.model_cost or injected model_cost_registry
    try:
        model_cost = model_cost_registry if model_cost_registry is not None else (getattr(litellm, "model_cost", {}) or {})
        provider_stripped = norm.split("/", 1)[-1] if "/" in norm else norm
        matched_info = (
            model_cost.get(model_name)
            or model_cost.get(norm)
            or model_cost.get(provider_stripped)
            or model_cost.get(norm_bare)
        )
        if matched_info and isinstance(matched_info, dict):
            in_cpt = matched_info.get("input_cost_per_token")
            out_cpt = matched_info.get("output_cost_per_token")
            if in_cpt is not None and out_cpt is not None:
                in_1m = round(float(in_cpt) * 1_000_000, 4)
                out_1m = round(float(out_cpt) * 1_000_000, 4)
                if _is_valid_price_pair(in_1m, out_1m):
                    return {
                        "input_price_per_1m": in_1m,
                        "output_price_per_1m": out_1m,
                        "currency": "USD",
                        "source": "LiteLLM Community Registry",
                        "source_url": "https://github.com/BerriAI/litellm",
                    }
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[PRICING] litellm.model_cost lookup failed for {model_name}: {e}")

    sorted_baseline = sorted(BASELINE_MODEL_PRICING.items(), key=lambda kv: len(kv[0]), reverse=True)
    for key, p in sorted_baseline:
        if key and key in norm:
            if _is_valid_price_pair(p.get("input_price_per_1m"), p.get("output_price_per_1m")):
                return dict(p)

    for key, p in sorted_baseline:
        if norm and norm in key:
            if _is_valid_price_pair(p.get("input_price_per_1m"), p.get("output_price_per_1m")):
                return dict(p)

    return dict(DEFAULT_FALLBACK_PRICING)


class PricingService:
    """Encapsulates pricing cache, structured feeds, live web research agent, and cost lookup per model."""

    def __init__(self, cache_file: Path | None = None):
        self._custom_cache_file: bool = cache_file is not None
        self.default_cache_file = cache_file or PRICING_CACHE_FILE
        self._mem_cache: dict[str, dict[str, Any]] | None = None
        self._mem_cache_key: tuple[str, float] | None = None
        self._refresh_lock: asyncio.Lock | None = None
        self._refresh_lock_loop: asyncio.AbstractEventLoop | None = None

    @property
    def refresh_lock(self) -> asyncio.Lock:
        """Lazily instantiates event-loop-bound asyncio.Lock inside running event loop."""
        try:
            current_loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if self._refresh_lock is None or (current_loop is not None and self._refresh_lock_loop is not current_loop):
            self._refresh_lock = asyncio.Lock()
            self._refresh_lock_loop = current_loop
        return self._refresh_lock

    def configure_paths(self, cache_file: Path | str | None = None) -> None:
        """Configures isolated cache file for tests and runtime overrides."""
        if cache_file is not None:
            self.default_cache_file = Path(cache_file)
            self._custom_cache_file = True
        else:
            self._custom_cache_file = False
        self._mem_cache = None
        self._mem_cache_key = None

    def reset_paths(self) -> None:
        """Resets configured paths back to canonical baseline cache."""
        self._custom_cache_file = False
        from core.paths import PRICING_CACHE_FILE

        self.default_cache_file = PRICING_CACHE_FILE
        self._mem_cache = None
        self._mem_cache_key = None

    @property
    def active_cache_file(self) -> Path:
        if self._custom_cache_file:
            return self.default_cache_file
        from core.paths import get_pricing_cache_file

        return get_pricing_cache_file()

    def load_cache(
        self,
        read_text_fn: Callable[[Path], str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Loads cached pricing data from JSON file with mtime-based memory cache."""
        target = self.active_cache_file
        if not target.exists():
            self._mem_cache = None
            self._mem_cache_key = None
            return {}
        try:
            mtime = target.stat().st_mtime
            current_key = (str(target), mtime)
            if self._mem_cache is not None and self._mem_cache_key == current_key:
                return dict(self._mem_cache)

            if read_text_fn is not None:
                raw_text = read_text_fn(target)
            else:
                raw_text = target.read_text(encoding="utf-8")
            data = json.loads(raw_text)
            if isinstance(data, dict):
                self._mem_cache = data
                self._mem_cache_key = current_key
                return dict(data)
            return {}
        except (OSError, json.JSONDecodeError, UnicodeError, ValueError) as e:
            logger.warning(f"[PRICING] Failed to read {target.name}: {e}")
            return {}

    def save_cache(self, data: dict[str, dict[str, Any]]) -> None:
        """Saves pricing data dictionary to JSON cache file atomically."""
        target = self.active_cache_file
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=str(target.parent),
                prefix=f"{target.stem}_",
                suffix=".tmp",
            )
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(data, indent=2))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, target)
            self._mem_cache = data
            self._mem_cache_key = (str(target), target.stat().st_mtime)
            logger.info(f"[PRICING] Successfully saved pricing cache to {target.name}")
        except (OSError, TypeError, ValueError) as e:
            logger.error(f"[PRICING] Error saving pricing cache: {e}")

    def prune_cache(self, valid_models: set[str] | None = None) -> int:
        """
        Removes poisoned, malformed, baseline-stale, or retired model entries from cache.
        Returns the number of pruned entries.
        """
        cache = self.load_cache()
        if not cache:
            return 0

        normalized_valid = (
            {_normalize_model_name(m) for m in valid_models if str(m).strip()}
            if valid_models is not None
            else None
        )

        pruned: dict[str, dict[str, Any]] = {}
        removed = 0
        for key, entry in cache.items():
            norm_key = _normalize_model_name(key)
            if not _is_cache_entry_valid(norm_key, entry):
                removed += 1
                continue
            if normalized_valid is not None and norm_key not in normalized_valid:
                removed += 1
                continue
            pruned[key] = entry

        if removed > 0:
            self.save_cache(pruned)
            logger.info(f"[PRICING] Pruned {removed} stale/invalid entries from pricing cache.")
        return removed

    def get_pricing(self, model_name: str) -> dict[str, Any]:
        """
        Synchronous getter for model pricing from cache or baseline.
        Does not block for web search.
        """
        cache = self.load_cache()
        norm = _normalize_model_name(model_name)
        baseline = get_baseline_for_model(model_name)
        if norm in cache and _is_cache_entry_valid(norm, cache[norm]):
            res = dict(cache[norm])
            if not res.get("source_url"):
                res["source_url"] = baseline.get("source_url", get_pricing_benchmark_url())
            return copy.deepcopy(res)
        res = dict(baseline)
        res["updated_at"] = 0
        return copy.deepcopy(res)

    async def fetch_pricing_with_agent(self, model_name: str) -> dict[str, Any]:
        """Researches pricing for a given model using a bounded multi-turn LiteLLM agent with tool calls."""
        logger.info(f"[PRICING] Researching current live pricing for '{model_name}' via agent...")
        baseline = get_baseline_for_model(model_name)

        from core.model_catalog import resolve_model_with_fallback

        configured_model = get_system_agent_model(
            "PricingAgent", fallback=get_orchestrator_fast_model()
        )
        active_model = resolve_model_with_fallback(
            configured_model, log_context="PRICING"
        ).model

        def _fallback_payload() -> dict[str, Any]:
            now = time.time()
            return {
                **baseline,
                "is_fallback": True,
                "updated_at": now,
                "updated_at_str": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(now)),
            }

        def _record_spend(resp_obj: Any) -> None:
            try:
                from pipeline.spend_governance import spend_governance

                usage = getattr(resp_obj, "usage", None)
                if usage:
                    spend_governance.record_usage(
                        session_id=f"system:pricing:{model_name}",
                        model_name=active_model,
                        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                    )
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[PRICING] Failed to record spend ledger: {e}")

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": get_pricing_agent_prompt()},
            {
                "role": "user",
                "content": f"Find official per-token API pricing in USD for model: {model_name}",
            },
        ]

        executed_any_tool = False
        final_text: str | None = None

        for _round in range(MAX_PRICING_AGENT_TOOL_ROUNDS):
            resp = await execute_llm_with_retry(
                func=litellm.acompletion,
                kwargs={
                    "model": active_model,
                    "messages": messages,
                    "tools": PRICING_TOOL_SCHEMAS,
                    "tool_choice": "auto",
                },
                agent_key="pricing_agent",
                thread_key=("system", f"pricing_{model_name}"),
                max_attempts=2,
            )
            if not resp or not getattr(resp, "choices", None):
                return _fallback_payload()

            _record_spend(resp)
            resp_msg = resp.choices[0].message
            tool_calls = getattr(resp_msg, "tool_calls", None)

            if tool_calls:
                if hasattr(resp_msg, "model_dump"):
                    messages.append(resp_msg.model_dump())
                else:
                    messages.append(
                        {
                            "role": getattr(resp_msg, "role", "assistant"),
                            "content": getattr(resp_msg, "content", None),
                            "tool_calls": getattr(resp_msg, "tool_calls", None),
                        }
                    )

                for tc in tool_calls:
                    fn_name = tc.function.name
                    raw_args = tc.function.arguments
                    if isinstance(raw_args, str):
                        try:
                            fn_args = json.loads(raw_args)
                        except json.JSONDecodeError:
                            fn_args = {}
                    else:
                        fn_args = raw_args if isinstance(raw_args, dict) else {}

                    if fn_name == "web_search":
                        res_str = await web_search(**fn_args)
                    elif fn_name == "fetch_web_page":
                        call_args = dict(fn_args)
                        call_args.setdefault("max_chars", 12000)
                        res_str = await fetch_web_page(**call_args)
                    else:
                        res_str = f"Error: unknown tool '{fn_name}'"

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "name": fn_name,
                            "content": res_str,
                        }
                    )
                executed_any_tool = True
                continue

            if not executed_any_tool:
                return _fallback_payload()

            final_text = getattr(resp_msg, "content", None) or ""
            break

        if final_text is None:
            if not executed_any_tool:
                return _fallback_payload()

            final_resp = await execute_llm_with_retry(
                func=litellm.acompletion,
                kwargs={
                    "model": active_model,
                    "messages": messages,
                    "response_format": {"type": "json_object"},
                },
                agent_key="pricing_agent",
                thread_key=("system", f"pricing_{model_name}"),
                max_attempts=2,
            )
            if not final_resp or not getattr(final_resp, "choices", None):
                return _fallback_payload()
            _record_spend(final_resp)
            final_text = getattr(final_resp.choices[0].message, "content", None) or ""

        cleaned = re.sub(r"^```json\s*", "", final_text.strip(), flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned.strip())
        data = json.loads(cleaned)
        if not isinstance(data, dict) or data.get("verified") is False:
            return _fallback_payload()

        raw_in = data.get("input_price_per_1m")
        raw_out = data.get("output_price_per_1m")
        if not _is_valid_price_pair(raw_in, raw_out):
            return _fallback_payload()

        in_price = float(raw_in)
        out_price = float(raw_out)
        if not _is_within_sanity_bounds(in_price, out_price, baseline):
            logger.warning(
                f"[PRICING] Agent returned out-of-bounds pricing for '{model_name}': "
                f"in={in_price}, out={out_price}. Falling back to baseline without caching."
            )
            return _fallback_payload()

        source_url = data.get("source_url") or baseline.get("source_url", get_pricing_benchmark_url())
        now = time.time()
        return {
            "input_price_per_1m": in_price,
            "output_price_per_1m": out_price,
            "currency": "USD",
            "source": data.get("source", baseline["source"]),
            "source_url": source_url,
            "updated_at": now,
            "updated_at_str": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(now)),
        }

    async def fetch_pricing_from_web(
        self,
        model_name: str,
        *,
        feed_fetcher: Any = None,
        agent_fetcher: Any = None,
    ) -> dict[str, Any]:
        """
        Researches pricing for a given model:
        1. Structured machine-readable JSON pricing feeds (LiteLLM Live JSON / OpenRouter Models API).
        2. Multi-turn PricingAgent research using web_search + fetch_web_page.
        3. Clean baseline fallback (flagged is_fallback=True so it is never persisted over baseline).
        """
        baseline = get_baseline_for_model(model_name)

        # Stage 1: Structured JSON feeds
        try:
            if feed_fetcher is not None:
                call_res = feed_fetcher(model_name)
            else:
                call_res = fetch_pricing_from_feeds(model_name, baseline=baseline)
            feed_res = await call_res if asyncio.iscoroutine(call_res) else call_res
            if (
                isinstance(feed_res, dict)
                and not feed_res.get("is_fallback")
                and _is_valid_price_pair(
                    feed_res.get("input_price_per_1m"),
                    feed_res.get("output_price_per_1m"),
                )
                and _is_within_sanity_bounds(
                    float(feed_res["input_price_per_1m"]),
                    float(feed_res["output_price_per_1m"]),
                    baseline,
                )
            ):
                return feed_res
        except Exception as e:  # noqa: BLE001 - Feed lookup failure falls back to agent research
            logger.warning(f"[PRICING] Structured feed lookup failed for '{model_name}': {e}")

        # Stage 2: Multi-turn PricingAgent research
        try:
            if agent_fetcher is not None:
                call_res = agent_fetcher(model_name)
            else:
                call_res = self.fetch_pricing_with_agent(model_name)
            agent_res = await call_res if asyncio.iscoroutine(call_res) else call_res
            if isinstance(agent_res, dict) and not agent_res.get("is_fallback"):
                return agent_res
        except (TimeoutError, asyncio.TimeoutError) as e:
            logger.warning(f"[PRICING] Agent research timed out for '{model_name}': {e}. Using baseline.")
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as e:
            logger.warning(f"[PRICING] Agent research format error for '{model_name}': {e}. Using baseline.")
        except Exception as e:  # noqa: BLE001 - Pricing research error boundary falls back to baseline
            logger.warning(f"[PRICING] Agent research failed for '{model_name}': {e}. Using baseline.")

        # Stage 3: Clean baseline fallback (never persisted to cache)
        now = time.time()
        return {
            **baseline,
            "is_fallback": True,
            "updated_at": now,
            "updated_at_str": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(now)),
        }

    async def refresh_pricing(
        self,
        model_name: str,
        force: bool = False,
        web_fetcher: Any = None,
    ) -> dict[str, Any]:
        """
        Returns pricing dictionary for a given model with 7-day TTL check.
        Performs live lookup only if stale, invalid, or missing, governed by weekly_pricing_update_enabled.
        If weekly update is OFF, models with valid cached pricing are not updated, while models without
        valid pricing still fetch pricing.
        """
        async with self.refresh_lock:
            cache = self.load_cache()
            norm = _normalize_model_name(model_name)
            now = time.time()

            from config_store.app_settings import is_weekly_pricing_update_enabled

            weekly_update_enabled = is_weekly_pricing_update_enabled()

            cached_entry = cache.get(norm)
            entry_valid = _is_cache_entry_valid(norm, cached_entry)

            if cached_entry and not force and entry_valid:
                updated_at = cached_entry.get("updated_at", 0)
                age = now - updated_at

                if not weekly_update_enabled:
                    logger.debug(f"[PRICING] Weekly updates disabled; using existing price for '{norm}'")
                    if not cached_entry.get("source_url"):
                        baseline = get_baseline_for_model(model_name)
                        cached_entry["source_url"] = baseline.get("source_url", get_pricing_benchmark_url())
                    return copy.deepcopy(cached_entry)

                effective_ttl = get_pricing_cache_ttl_seconds()
                if age < effective_ttl:
                    logger.debug(
                        f"[PRICING] Using cached pricing for '{norm}' "
                        f"(age: {age / 86400:.1f} days / {effective_ttl / 86400:.1f}-day TTL)"
                    )
                    if not cached_entry.get("source_url"):
                        baseline = get_baseline_for_model(model_name)
                        cached_entry["source_url"] = baseline.get("source_url", get_pricing_benchmark_url())
                    return copy.deepcopy(cached_entry)

            if web_fetcher is not None:
                call_res = web_fetcher(model_name)
                fresh_pricing = await call_res if asyncio.iscoroutine(call_res) else call_res
            else:
                fresh_pricing = await self.fetch_pricing_from_web(model_name)

            if isinstance(fresh_pricing, dict) and fresh_pricing.get("is_fallback") and web_fetcher is None:
                logger.info(
                    f"[PRICING] Skipping persistent cache write for fallback pricing of '{model_name}'."
                )
                return copy.deepcopy(fresh_pricing)

            to_cache = (
                {k: v for k, v in fresh_pricing.items() if k != "is_fallback"}
                if isinstance(fresh_pricing, dict)
                else fresh_pricing
            )
            if isinstance(to_cache, dict):
                sig = _get_baseline_signature(norm)
                if sig is not None:
                    to_cache["baseline_signature"] = sig
            cache[norm] = to_cache
            self.save_cache(cache)
            return copy.deepcopy(to_cache)

    async def warm_up_cache(self, models: list[str] | None = None) -> None:
        """Warms up pricing cache at server startup for all known models."""
        if models is None:
            from config_store.agent_settings import get_agents_config
            from core.config import (
                SYSTEM_AGENTS_CONFIG,
                get_orchestrator_reasoning_model,
                get_system_agent_model,
            )
            from core.model_catalog import load_provider_catalog

            models_to_check = {cfg["model"] for cfg in get_agents_config().values() if "model" in cfg}
            models_to_check.update(
                cfg["model"]
                for cfg in SYSTEM_AGENTS_CONFIG.values()
                if isinstance(cfg, dict) and cfg.get("model")
            )
            scribe_m = get_system_agent_model("DiscussionScribe", fallback="mistral/mistral-small-latest")
            if scribe_m:
                models_to_check.add(scribe_m)
            reasoning_m = get_orchestrator_reasoning_model()
            if reasoning_m:
                models_to_check.add(reasoning_m)

            known_models: set[str] = {_normalize_model_name(m) for m in models_to_check if m}
            known_models.update(BASELINE_MODEL_PRICING.keys())
            catalog = load_provider_catalog()
            for cfg in catalog.values():
                if isinstance(cfg, dict):
                    for cat_model in cfg.get("models", []):
                        if isinstance(cat_model, str) and cat_model.strip():
                            known_models.add(_normalize_model_name(cat_model))
            self.prune_cache(valid_models=known_models)
        else:
            models_to_check = set(models)

        logger.info(f"[PRICING] Warming up pricing cache for {len(models_to_check)} models...")
        from services.web_tools import reset_turn_web_budget

        try:
            for m in models_to_check:
                reset_turn_web_budget()
                try:
                    await self.refresh_pricing(m, force=False)
                except (TimeoutError, asyncio.TimeoutError) as e:
                    logger.warning(f"[PRICING] Warm-up timed out for model '{m}': {e}")
                except (OSError, ValueError, KeyError) as e:
                    logger.warning(f"[PRICING] Warm-up error for model '{m}': {e}")
                except Exception as e:  # noqa: BLE001 - Pricing warmup error boundary isolates individual model failure
                    logger.warning(f"[PRICING] Warm-up failed for model '{m}': {e}")
        finally:
            clear_feed_cache()


# Default singleton instance
pricing_service = PricingService()


def get_model_pricing(model_name: str) -> dict[str, Any]:
    return pricing_service.get_pricing(model_name)
