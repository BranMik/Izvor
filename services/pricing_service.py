import asyncio
import inspect
import json
import logging
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import litellm

from core.config import get_pricing_agent_model, get_pricing_agent_prompt
from core.llm_utils import execute_llm_with_retry

from .web_tools import fetch_web_page, web_search

from core.paths import BASELINE_PRICES_FILE, PRICING_CACHE_FILE

logger = logging.getLogger(__name__)

# Baseline model pricing alias
BASELINE_CONFIG_FILE = BASELINE_PRICES_FILE

def get_pricing_cache_ttl_seconds() -> int:
    try:
        from orchestration.system_settings import get_pricing_cache_ttl_seconds
        return get_pricing_cache_ttl_seconds()
    except Exception:
        return 7 * 24 * 3600


def get_pricing_benchmark_url() -> str:
    try:
        from orchestration.system_settings import get_pricing_benchmark_url
        return get_pricing_benchmark_url()
    except Exception:
        return "https://artificialanalysis.ai/models"


# Cache TTL: 7 days default in seconds (from system_settings SSOT)
CACHE_TTL_SECONDS = get_pricing_cache_ttl_seconds()

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
                        "description": "Maximum number of characters of text content to extract. Defaults to 4000.",
                        "default": 4000,
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
    Defaults to data/baseline_prices.json with safe fallback.
    """
    target = Path(config_path) if config_path else BASELINE_PRICES_FILE
    if target.exists():
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
            logger.error(f"[PRICING] Expected dict in {target.name}, got {type(data)}")
        except (OSError, json.JSONDecodeError, UnicodeError, ValueError) as e:
            logger.error(f"[PRICING] Error loading baseline prices from {target}: {e}")
    return {}


MIN_VALID_PRICE_PER_1M: float = 0.005
MAX_VALID_PRICE_PER_1M: float = 500.0


def _is_valid_price_pair(in_price: Any, out_price: Any) -> bool:
    """Validates that both input and output prices per 1M tokens fall within acceptable sanity bounds."""
    try:
        if in_price is None or out_price is None:
            return False
        inp = float(in_price)
        outp = float(out_price)
        return (
            MIN_VALID_PRICE_PER_1M <= inp <= MAX_VALID_PRICE_PER_1M
            and MIN_VALID_PRICE_PER_1M <= outp <= MAX_VALID_PRICE_PER_1M
        )
    except (TypeError, ValueError):
        return False


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


def _normalize_model_name(model_name: str) -> str:
    """Normalizes model string (e.g. 'gemini/gemini-3.8-flash')."""
    return model_name.strip().lower()


def _extract_pricing_from_search_text(
    text: str, model_name: str
) -> tuple[float | None, float | None]:
    """
    Attempts to extract input and output token pricing per 1M tokens from web search snippets.
    Supports both 'keyword before price' and 'price before keyword' conventions, as well as '$X / $Y per 1M'.
    """
    input_price = None
    output_price = None

    # Strategy 1: Combined slash format like "$0.15 / $0.60 per 1M" or "$2.00 / $6.00 per million"
    slash_match = re.search(
        r"\$\s*([0-9]+(?:\.[0-9]+)?)\s*/\s*\$\s*([0-9]+(?:\.[0-9]+)?)\s*(?:(?:/|per)\s*(?:1m|1\s*million|m|million|1000k))?",
        text,
        re.IGNORECASE,
    )
    if slash_match:
        try:
            v_in = float(slash_match.group(1))
            v_out = float(slash_match.group(2))
            if _is_valid_price_pair(v_in, v_out):
                return v_in, v_out
        except (ValueError, IndexError):
            pass

    # Strategy 2: Keyword immediately before price (e.g. "Input: $2.00 / 1M", "Prompt tokens: $0.15/M")
    in_after_kw = re.search(
        r"(?:input|prompt)(?:\s+tokens?)?[:\s\-\–]{1,15}\$\s*([0-9]+(?:\.[0-9]+)?)",
        text,
        re.IGNORECASE,
    )
    if in_after_kw:
        try:
            val = float(in_after_kw.group(1))
            if MIN_VALID_PRICE_PER_1M <= val <= MAX_VALID_PRICE_PER_1M:
                input_price = val
        except (ValueError, IndexError):
            pass

    out_after_kw = re.search(
        r"(?:output|completion)(?:\s+tokens?)?[:\s\-\–]{1,15}\$\s*([0-9]+(?:\.[0-9]+)?)",
        text,
        re.IGNORECASE,
    )
    if out_after_kw:
        try:
            val = float(out_after_kw.group(1))
            if MIN_VALID_PRICE_PER_1M <= val <= MAX_VALID_PRICE_PER_1M:
                output_price = val
        except (ValueError, IndexError):
            pass

    # Strategy 3: Price immediately before keyword (e.g. "$0.15 per 1M input" / "$0.60 per 1M output tokens")
    if input_price is None:
        in_before_kw = re.search(
            r"\$\s*([0-9]+(?:\.[0-9]+)?)\s*(?:(?:/|per)\s*(?:1m|1\s*million|m|million|1000k))?\s*(?:for\s+)?(?:input|prompt)\b",
            text,
            re.IGNORECASE,
        )
        if in_before_kw:
            try:
                val = float(in_before_kw.group(1))
                if MIN_VALID_PRICE_PER_1M <= val <= MAX_VALID_PRICE_PER_1M:
                    input_price = val
            except (ValueError, IndexError):
                pass

    if output_price is None:
        out_before_kw = re.search(
            r"\$\s*([0-9]+(?:\.[0-9]+)?)\s*(?:(?:/|per)\s*(?:1m|1\s*million|m|million|1000k))?\s*(?:for\s+)?(?:output|completion)\b",
            text,
            re.IGNORECASE,
        )
        if out_before_kw:
            try:
                val = float(out_before_kw.group(1))
                if MIN_VALID_PRICE_PER_1M <= val <= MAX_VALID_PRICE_PER_1M:
                    output_price = val
            except (ValueError, IndexError):
                pass

    return input_price, output_price


def get_baseline_for_model(model_name: str) -> dict[str, Any]:
    """
    Returns baseline fallback pricing for a given model identifier.
    Priority order:
    1. Exact / substring match in data/baseline_prices.json (validated)
    2. litellm.model_cost community registry
    3. DEFAULT_FALLBACK_PRICING ($15.00/$75.00 per 1M)
    """
    norm = _normalize_model_name(model_name)
    if norm in BASELINE_MODEL_PRICING:
        base = BASELINE_MODEL_PRICING[norm]
        if _is_valid_price_pair(base.get("input_price_per_1m"), base.get("output_price_per_1m")):
            return dict(base)

    for key, p in BASELINE_MODEL_PRICING.items():
        if key in norm or norm in key:
            if _is_valid_price_pair(p.get("input_price_per_1m"), p.get("output_price_per_1m")):
                return dict(p)

    # Primary registry lookup: litellm.model_cost
    try:
        model_cost = getattr(litellm, "model_cost", {}) or {}
        provider_stripped = norm.split("/", 1)[-1] if "/" in norm else norm
        matched_info = (
            model_cost.get(model_name)
            or model_cost.get(norm)
            or model_cost.get(provider_stripped)
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
    except Exception as e:
        logger.debug(f"[PRICING] litellm.model_cost lookup failed for {model_name}: {e}")

    return dict(DEFAULT_FALLBACK_PRICING)



class PricingService:
    """Encapsulates pricing cache, live web research agent, and cost calculation per model."""

    def __init__(self, cache_file: Path | None = None):
        self._custom_cache_file: bool = cache_file is not None
        self.default_cache_file = cache_file or PRICING_CACHE_FILE
        self.baseline_pricing = dict(BASELINE_MODEL_PRICING)
        self.default_fallback = dict(DEFAULT_FALLBACK_PRICING)
        self._mem_cache: dict[str, dict[str, Any]] | None = None
        self._mem_cache_key: tuple[str, float] | None = None

    @property
    def active_cache_file(self) -> Path:
        if self._custom_cache_file:
            return self.default_cache_file
        ps_mod = sys.modules.get("pricing_service") or sys.modules.get("services.pricing_service")
        if ps_mod and hasattr(ps_mod, "PRICING_CACHE_FILE"):
            return Path(ps_mod.PRICING_CACHE_FILE)
        return self.default_cache_file

    def load_cache(self) -> dict[str, dict[str, Any]]:
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

            data = json.loads(target.read_text(encoding="utf-8"))
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
            logger.info(
                f"[PRICING] Successfully saved pricing cache to {target.name}"
            )
        except (OSError, TypeError, ValueError) as e:
            logger.error(f"[PRICING] Error saving pricing cache: {e}")

    def get_pricing(self, model_name: str) -> dict[str, Any]:
        """
        Synchronous getter for model pricing from cache or baseline.
        Does not block for web search.
        """
        cache = self.load_cache()
        norm = _normalize_model_name(model_name)
        baseline = get_baseline_for_model(model_name)
        if norm in cache:
            res = dict(cache[norm])
            if not res.get("source_url"):
                res["source_url"] = baseline.get(
                    "source_url", get_pricing_benchmark_url()
                )
            return res
        res = dict(baseline)
        res["updated_at"] = 0
        return res

    async def fetch_pricing_with_agent(self, model_name: str) -> dict[str, Any]:
        """Researches pricing for a given model using LiteLLM agent with tool calls."""
        logger.info(f"[PRICING] Researching current live pricing for '{model_name}' via agent...")
        baseline = get_baseline_for_model(model_name)

        messages = [
            {"role": "system", "content": get_pricing_agent_prompt()},
            {
                "role": "user",
                "content": f"Find official per-token API pricing in USD for model: {model_name}",
            },
        ]

        resp = await execute_llm_with_retry(
            func=litellm.acompletion,
            kwargs={
                "model": get_pricing_agent_model(),
                "messages": messages,
                "tools": PRICING_TOOL_SCHEMAS,
                "tool_choice": "auto",
            },
            agent_key="pricing_agent",
            thread_key=("system", f"pricing_{model_name}"),
            max_attempts=2,
        )
        if not resp or not getattr(resp, "choices", None):
            return {
                **baseline,
                "updated_at": time.time(),
                "updated_at_str": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            }

        try:
            from pipeline.spend_governance import spend_governance

            usage = getattr(resp, "usage", None)
            if usage:
                spend_governance.record_usage(
                    session_id=f"system:pricing:{model_name}",
                    model_name=get_pricing_agent_model(),
                    prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                    completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                )
        except Exception as e:
            logger.debug(f"[PRICING] Failed to record spend ledger: {e}")

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
                fn_args = (
                    json.loads(tc.function.arguments)
                    if isinstance(tc.function.arguments, str)
                    else tc.function.arguments
                )
                if fn_name == "web_search":
                    search_fn = getattr(sys.modules.get("pricing_service") or sys.modules.get("services.pricing_service"), "web_search", web_search)
                    if inspect.iscoroutinefunction(search_fn):
                        res_str = await search_fn(**fn_args)
                    else:
                        res_str = await asyncio.to_thread(search_fn, **fn_args)
                elif fn_name == "fetch_web_page":
                    fetch_page_fn = getattr(sys.modules.get("pricing_service") or sys.modules.get("services.pricing_service"), "fetch_web_page", fetch_web_page)
                    if inspect.iscoroutinefunction(fetch_page_fn):
                        res_str = await fetch_page_fn(**fn_args)
                    else:
                        res_str = await asyncio.to_thread(fetch_page_fn, **fn_args)
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

            final_resp = await execute_llm_with_retry(
                func=litellm.acompletion,
                kwargs={
                    "model": get_pricing_agent_model(),
                    "messages": messages,
                    "response_format": {"type": "json_object"},
                },
                agent_key="pricing_agent",
                thread_key=("system", f"pricing_{model_name}"),
                max_attempts=2,
            )
            if not final_resp or not getattr(final_resp, "choices", None):
                return {
                    **baseline,
                    "updated_at": time.time(),
                    "updated_at_str": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
                }

            try:
                from pipeline.spend_governance import spend_governance

                usage_final = getattr(final_resp, "usage", None)
                if usage_final:
                    spend_governance.record_usage(
                        session_id=f"system:pricing:{model_name}",
                        model_name=get_pricing_agent_model(),
                        prompt_tokens=getattr(usage_final, "prompt_tokens", 0) or 0,
                        completion_tokens=getattr(usage_final, "completion_tokens", 0) or 0,
                    )
            except Exception as e:
                logger.debug(f"[PRICING] Failed to record spend ledger: {e}")

            final_text = final_resp.choices[0].message.content

            cleaned = re.sub(r"^```json\s*", "", final_text.strip(), flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned.strip())
            data = json.loads(cleaned)

            in_price = float(data.get("input_price_per_1m", baseline["input_price_per_1m"]))
            out_price = float(data.get("output_price_per_1m", baseline["output_price_per_1m"]))
            if not _is_valid_price_pair(in_price, out_price):
                logger.warning(
                    f"[PRICING] Agent returned out-of-bounds pricing for '{model_name}': "
                    f"in={in_price}, out={out_price}. Falling back to baseline."
                )
                in_price = float(baseline["input_price_per_1m"])
                out_price = float(baseline["output_price_per_1m"])

            source_url = data.get("source_url") or baseline.get("source_url", get_pricing_benchmark_url())

            return {
                "input_price_per_1m": in_price,
                "output_price_per_1m": out_price,
                "currency": "USD",
                "source": data.get("source", baseline["source"]),
                "source_url": source_url,
                "updated_at": time.time(),
                "updated_at_str": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            }


        # If no tool calls, return baseline
        return {
            **baseline,
            "updated_at": time.time(),
            "updated_at_str": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        }

    async def fetch_pricing_from_web(self, model_name: str) -> dict[str, Any]:
        """Researches pricing for a given model via agent with fallback to direct search."""
        baseline = get_baseline_for_model(model_name)
        try:
            # Delegate to module-level fetch_pricing_with_agent to intercept test mocks
            call_res = fetch_pricing_with_agent(model_name)
            return await call_res if asyncio.iscoroutine(call_res) else call_res
        except (TimeoutError, asyncio.TimeoutError) as e:
            logger.warning(f"[PRICING] Agent research timed out for '{model_name}': {e}. Trying direct search.")
        except (KeyError, ValueError, TypeError) as e:
            logger.warning(f"[PRICING] Agent research format error for '{model_name}': {e}. Trying direct search.")
        except Exception as e:  # noqa: BLE001 - Pricing research error boundary falls back to direct search
            logger.warning(f"[PRICING] Agent research failed for '{model_name}': {e}. Trying direct search.")

        # Fallback to direct web search extraction
        try:
            clean_query = re.sub(r"^[a-zA-Z0-9_-]+/", "", model_name)
            search_fn = getattr(sys.modules.get("pricing_service") or sys.modules.get("services.pricing_service"), "web_search", web_search)
            if inspect.iscoroutinefunction(search_fn):
                search_raw = await search_fn(query=f"{clean_query} api pricing tokens million official", max_results=5)
            else:
                search_raw = await asyncio.to_thread(search_fn, query=f"{clean_query} api pricing tokens million official", max_results=5)
            in_val, out_val = _extract_pricing_from_search_text(search_raw, model_name)
            input_price = in_val if in_val is not None else baseline["input_price_per_1m"]
            output_price = out_val if out_val is not None else baseline["output_price_per_1m"]
            source = "Web Search Fallback" if (in_val or out_val) else baseline["source"]
            source_url = baseline.get("source_url", get_pricing_benchmark_url())

            url_match = re.search(r"https?://[^\s)\]\"'>]+", search_raw)
            if url_match:
                source_url = url_match.group(0)

            return {
                "input_price_per_1m": float(input_price),
                "output_price_per_1m": float(output_price),
                "currency": "USD",
                "source": source,
                "source_url": source_url,
                "updated_at": time.time(),
                "updated_at_str": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            }
        except (TimeoutError, asyncio.TimeoutError) as e:
            logger.warning(f"[PRICING] Direct search timed out for '{model_name}': {e}. Using baseline.")
        except (ValueError, TypeError, KeyError) as e:
            logger.warning(f"[PRICING] Direct search parsing error for '{model_name}': {e}. Using baseline.")
        except Exception as e:  # noqa: BLE001 - Pricing direct search error boundary falls back to baseline pricing
            logger.warning(f"[PRICING] Direct search failed for '{model_name}': {e}. Using baseline.")

        return {
            **baseline,
            "updated_at": time.time(),
            "updated_at_str": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        }

    async def refresh_pricing(self, model_name: str, force: bool = False) -> dict[str, Any]:
        """
        Returns pricing dictionary for a given model with 7-day TTL check.
        Performs web search only if stale or missing, governed by weekly_pricing_update_enabled setting.
        If weekly update is OFF, models with existing pricing are not updated, but models without set
        pricing will still fetch pricing.
        """
        cache = self.load_cache()
        norm = _normalize_model_name(model_name)
        now = time.time()

        from orchestration.app_settings import is_weekly_pricing_update_enabled
        weekly_update_enabled = is_weekly_pricing_update_enabled()

        cached_entry = cache.get(norm)
        has_price = (
            cached_entry is not None
            and cached_entry.get("input_price_per_1m") is not None
            and cached_entry.get("output_price_per_1m") is not None
        )

        if cached_entry and not force:
            updated_at = cached_entry.get("updated_at", 0)
            age = now - updated_at

            # If weekly updates are turned OFF and pricing is already set for this model,
            # do not update and return cached pricing.
            if not weekly_update_enabled and has_price:
                logger.debug(
                    f"[PRICING] Weekly updates disabled; using existing price for '{norm}'"
                )
                if not cached_entry.get("source_url"):
                    baseline = get_baseline_for_model(model_name)
                    cached_entry["source_url"] = baseline.get(
                        "source_url", get_pricing_benchmark_url()
                    )
                return cached_entry

            # If weekly updates are turned ON, enforce the dynamic TTL check
            effective_ttl = get_pricing_cache_ttl_seconds()
            if weekly_update_enabled and has_price and age < effective_ttl:
                logger.debug(
                    f"[PRICING] Using cached pricing for '{norm}' (age: {age / 86400:.1f} days / {effective_ttl / 86400:.1f}-day TTL)"
                )
                if not cached_entry.get("source_url"):
                    baseline = get_baseline_for_model(model_name)
                    cached_entry["source_url"] = baseline.get(
                        "source_url", get_pricing_benchmark_url()
                    )
                return cached_entry

        call_res = fetch_pricing_from_web(model_name)
        fresh_pricing = await call_res if asyncio.iscoroutine(call_res) else call_res

        cache[norm] = fresh_pricing
        self.save_cache(cache)
        return fresh_pricing

    async def warm_up_cache(self, models: list[str] | None = None) -> None:
        """Warms up pricing cache at server startup for all known models."""
        if models is None:
            from core.config import get_thread_notes_model
            from orchestration.agent_settings import get_agents_config

            models_to_check = {cfg["model"] for cfg in get_agents_config().values() if "model" in cfg}
            thread_notes_m = get_thread_notes_model()
            if thread_notes_m:
                models_to_check.add(thread_notes_m)
        else:
            models_to_check = set(models)

        logger.info(
            f"[PRICING] Warming up pricing cache for {len(models_to_check)} models..."
        )
        for m in models_to_check:
            try:
                await self.refresh_pricing(m, force=False)
            except (TimeoutError, asyncio.TimeoutError) as e:
                logger.warning(f"[PRICING] Warm-up timed out for model '{m}': {e}")
            except (OSError, ValueError, KeyError) as e:
                logger.warning(f"[PRICING] Warm-up error for model '{m}': {e}")
            except Exception as e:  # noqa: BLE001 - Pricing warmup error boundary isolates individual model failure
                logger.warning(f"[PRICING] Warm-up failed for model '{m}': {e}")


# Default singleton instance
pricing_service = PricingService()


# Backward-compatible function aliases
def load_pricing_cache() -> dict[str, dict[str, Any]]:
    return pricing_service.load_cache()


def save_pricing_cache(data: dict[str, dict[str, Any]]) -> None:
    pricing_service.save_cache(data)


def get_model_pricing(model_name: str) -> dict[str, Any]:
    return pricing_service.get_pricing(model_name)


async def fetch_pricing_from_web(model_name: str) -> dict[str, Any]:
    return await pricing_service.fetch_pricing_from_web(model_name)


async def fetch_pricing_with_agent(model_name: str) -> dict[str, Any]:
    return await pricing_service.fetch_pricing_with_agent(model_name)


async def refresh_model_pricing(
    model_name: str, force: bool = False
) -> dict[str, Any]:
    return await pricing_service.refresh_pricing(model_name, force=force)


async def warm_up_pricing_cache(models: list[str] | None = None) -> None:
    await pricing_service.warm_up_cache(models=models)
