import asyncio
import json
import logging
from pathlib import Path
import re
import sys
import time
from typing import Any

import litellm
from starlette.concurrency import run_in_threadpool

from core.config import PRICING_AGENT_MODEL, PRICING_AGENT_PROMPT
from core.models import ModelPricing
from .web_tools import fetch_web_page, web_search

logger = logging.getLogger(__name__)

# Model pricing cache file
PRICING_CACHE_FILE = Path(__file__).parent.parent / "data" / "model_pricing_cache.json"

# Cache TTL: 7 days in seconds
CACHE_TTL_SECONDS = 7 * 24 * 3600

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

# Industry baseline fallback pricing (USD per 1,000,000 tokens)
BASELINE_MODEL_PRICING: dict[str, dict[str, Any]] = {
    "gemini/gemini-3.7-flash": {
        "input_price_per_1m": 0.15,
        "output_price_per_1m": 0.60,
        "currency": "USD",
        "source": "Google AI Documentation",
        "source_url": "https://ai.google.dev/pricing",
    },
    "openai/gpt-5.6-terra": {
        "input_price_per_1m": 2.50,
        "output_price_per_1m": 10.00,
        "currency": "USD",
        "source": "OpenAI Pricing",
        "source_url": "https://openai.com/pricing",
    },
    "anthropic/claude-opus-5": {
        "input_price_per_1m": 15.00,
        "output_price_per_1m": 75.00,
        "currency": "USD",
        "source": "Anthropic Pricing",
        "source_url": "https://www.anthropic.com/pricing",
    },
    "mistral/mistral-large-latest": {
        "input_price_per_1m": 2.00,
        "output_price_per_1m": 6.00,
        "currency": "USD",
        "source": "Mistral AI Pricing",
        "source_url": "https://mistral.ai/technology/#pricing",
    },
    "mistral/mistral-small-latest": {
        "input_price_per_1m": 0.20,
        "output_price_per_1m": 0.60,
        "currency": "USD",
        "source": "Mistral AI Pricing",
        "source_url": "https://mistral.ai/technology/#pricing",
    },
}

# Generic fallback for any unknown model
DEFAULT_FALLBACK_PRICING: dict[str, Any] = {
    "input_price_per_1m": 1.00,
    "output_price_per_1m": 3.00,
    "currency": "USD",
    "source": "Artificial Analysis Benchmarks",
    "source_url": "https://artificialanalysis.ai/models",
}


def _normalize_model_name(model_name: str) -> str:
    """Normalizes model string (e.g. 'gemini/gemini-3.7-flash')."""
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
            if 0.005 <= v_in <= 250.0 and 0.005 <= v_out <= 250.0:
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
            if 0.005 <= val <= 250.0:
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
            if 0.005 <= val <= 250.0:
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
                if 0.005 <= val <= 250.0:
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
                if 0.005 <= val <= 250.0:
                    output_price = val
            except (ValueError, IndexError):
                pass

    return input_price, output_price


def get_baseline_for_model(model_name: str) -> dict[str, Any]:
    """Returns baseline fallback pricing for a given model identifier."""
    norm = _normalize_model_name(model_name)
    if norm in BASELINE_MODEL_PRICING:
        return dict(BASELINE_MODEL_PRICING[norm])
    for key, p in BASELINE_MODEL_PRICING.items():
        if key in norm or norm in key:
            return dict(p)
    return dict(DEFAULT_FALLBACK_PRICING)


class PricingService:
    """Encapsulates pricing cache, live web research agent, and cost calculation per model."""

    def __init__(self, cache_file: Path | None = None):
        self.default_cache_file = cache_file or PRICING_CACHE_FILE
        self.baseline_pricing = dict(BASELINE_MODEL_PRICING)
        self.default_fallback = dict(DEFAULT_FALLBACK_PRICING)

    @property
    def active_cache_file(self) -> Path:
        if self.default_cache_file != PRICING_CACHE_FILE:
            return self.default_cache_file
        ps_mod = sys.modules.get("pricing_service") or sys.modules.get("services.pricing_service")
        if ps_mod and hasattr(ps_mod, "PRICING_CACHE_FILE"):
            return Path(getattr(ps_mod, "PRICING_CACHE_FILE"))
        return self.default_cache_file

    def load_cache(self) -> dict[str, dict[str, Any]]:
        """Loads cached pricing data from JSON file."""
        target = self.active_cache_file
        if not target.exists():
            return {}
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning(f"[PRICING] Failed to read {target.name}: {e}")
            return {}

    def save_cache(self, data: dict[str, dict[str, Any]]) -> None:
        """Saves pricing data dictionary to JSON cache file."""
        target = self.active_cache_file
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(data, indent=2), encoding="utf-8"
            )
            logger.info(
                f"[PRICING] Successfully saved pricing cache to {target.name}"
            )
        except Exception as e:
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
                    "source_url", "https://artificialanalysis.ai/models"
                )
            return res
        res = dict(baseline)
        res["updated_at"] = 0
        return res

    async def fetch_pricing_with_agent(self, model_name: str) -> dict[str, Any]:
        """Researches pricing for a given model using LiteLLM agent with tool calls."""
        logger.info(f"[PRICING] Researching current live pricing for '{model_name}' via agent...")
        baseline = get_baseline_for_model(model_name)
        norm = _normalize_model_name(model_name)

        messages = [
            {"role": "system", "content": PRICING_AGENT_PROMPT},
            {
                "role": "user",
                "content": f"Find official per-token API pricing in USD for model: {model_name}",
            },
        ]

        def _run_completion():
            return litellm.completion(
                model=PRICING_AGENT_MODEL,
                messages=messages,
                tools=PRICING_TOOL_SCHEMAS,
                tool_choice="auto",
            )

        resp = await run_in_threadpool(_run_completion)
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
                    call_res = search_fn(**fn_args)
                    res_str = await call_res if asyncio.iscoroutine(call_res) else call_res
                elif fn_name == "fetch_web_page":
                    fetch_page_fn = getattr(sys.modules.get("pricing_service") or sys.modules.get("services.pricing_service"), "fetch_web_page", fetch_web_page)
                    call_res = fetch_page_fn(**fn_args)
                    res_str = await call_res if asyncio.iscoroutine(call_res) else call_res
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

            def _run_followup():
                return litellm.completion(
                    model=PRICING_AGENT_MODEL,
                    messages=messages,
                    response_format={"type": "json_object"},
                )

            final_resp = await run_in_threadpool(_run_followup)
            final_text = final_resp.choices[0].message.content
            cleaned = re.sub(r"^```json\s*", "", final_text.strip(), flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned.strip())
            data = json.loads(cleaned)

            in_price = float(data.get("input_price_per_1m", baseline["input_price_per_1m"]))
            out_price = float(data.get("output_price_per_1m", baseline["output_price_per_1m"]))
            source_url = data.get("source_url") or baseline.get("source_url", "https://artificialanalysis.ai/models")

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
        except Exception as e:
            logger.warning(f"[PRICING] Agent research failed for '{model_name}': {e}. Trying direct search.")

        # Fallback to direct web search extraction
        try:
            clean_query = re.sub(r"^[a-zA-Z0-9_-]+/", "", model_name)
            search_fn = getattr(sys.modules.get("pricing_service") or sys.modules.get("services.pricing_service"), "web_search", web_search)
            call_res = search_fn(query=f"{clean_query} api pricing tokens million official", max_results=5)
            search_raw = await call_res if asyncio.iscoroutine(call_res) else call_res
            in_val, out_val = _extract_pricing_from_search_text(search_raw, model_name)
            input_price = in_val if in_val is not None else baseline["input_price_per_1m"]
            output_price = out_val if out_val is not None else baseline["output_price_per_1m"]
            source = "Web Search Fallback" if (in_val or out_val) else baseline["source"]
            source_url = baseline.get("source_url", "https://artificialanalysis.ai/models")

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
        except Exception as e:
            logger.warning(f"[PRICING] Direct search failed for '{model_name}': {e}. Using baseline.")

        return {
            **baseline,
            "updated_at": time.time(),
            "updated_at_str": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        }

    async def refresh_pricing(self, model_name: str, force: bool = False) -> dict[str, Any]:
        """
        Returns pricing dictionary for a given model with 7-day TTL check.
        Performs web search only if stale or missing.
        """
        cache = self.load_cache()
        norm = _normalize_model_name(model_name)
        now = time.time()

        cached_entry = cache.get(norm)
        if cached_entry and not force:
            updated_at = cached_entry.get("updated_at", 0)
            age = now - updated_at
            if age < CACHE_TTL_SECONDS:
                logger.debug(
                    f"[PRICING] Using cached pricing for '{norm}' (age: {age / 86400:.1f} days / 7-day TTL)"
                )
                if not cached_entry.get("source_url"):
                    baseline = get_baseline_for_model(model_name)
                    cached_entry["source_url"] = baseline.get(
                        "source_url", "https://artificialanalysis.ai/models"
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
            from core.config import AGENTS_CONFIG, THREAD_NOTES_MODEL

            models_to_check = {cfg["model"] for cfg in AGENTS_CONFIG.values()}
            if THREAD_NOTES_MODEL:
                models_to_check.add(THREAD_NOTES_MODEL)
        else:
            models_to_check = set(models)

        logger.info(
            f"[PRICING] Warming up pricing cache for {len(models_to_check)} models..."
        )
        for m in models_to_check:
            try:
                await self.refresh_pricing(m, force=False)
            except Exception as e:
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
