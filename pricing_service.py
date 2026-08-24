import asyncio
import json
import logging
from pathlib import Path
import re
import time
from typing import Any

import litellm
from starlette.concurrency import run_in_threadpool

from config import PRICING_AGENT_MODEL, PRICING_AGENT_PROMPT
from web_tools import fetch_web_page, web_search

logger = logging.getLogger(__name__)

PRICING_CACHE_FILE = Path(__file__).parent / "model_pricing_cache.json"

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
# Source: Official provider documentation
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


def load_pricing_cache() -> dict[str, dict[str, Any]]:
    """Loads cached pricing data from JSON file."""
    if not PRICING_CACHE_FILE.exists():
        return {}
    try:
        data = json.loads(PRICING_CACHE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"[PRICING] Failed to read {PRICING_CACHE_FILE.name}: {e}")
        return {}


def save_pricing_cache(data: dict[str, dict[str, Any]]) -> None:
    """Saves pricing data dictionary to JSON cache file."""
    try:
        PRICING_CACHE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        logger.info(f"[PRICING] Successfully saved pricing cache to {PRICING_CACHE_FILE.name}")
    except Exception as e:
        logger.error(f"[PRICING] Error saving pricing cache: {e}")


def _normalize_model_name(model_name: str) -> str:
    """Normalizes model string (e.g. 'gemini/gemini-3.7-flash' -> 'gemini/gemini-3.7-flash')."""
    return model_name.strip().lower()


def _extract_pricing_from_search_text(text: str, model_name: str) -> tuple[float | None, float | None]:
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
    """Finds best matching baseline pricing for a given model."""
    norm = _normalize_model_name(model_name)
    for k, v in BASELINE_MODEL_PRICING.items():
        if _normalize_model_name(k) in norm or norm in _normalize_model_name(k):
            return dict(v)
    return dict(DEFAULT_FALLBACK_PRICING)


async def fetch_pricing_with_agent(model_name: str) -> dict[str, Any]:
    """
    Invokes the Model Pricing Research Agent (powered by mistral-small-latest) equipped with
    the web_search tool to find, verify, and return accurate token pricing rates for a given model.
    """
    clean_model_display = model_name.split("/")[-1] if "/" in model_name else model_name
    logger.info(
        f"[PRICING AGENT] Launching pricing research agent ({PRICING_AGENT_MODEL}) with web_search tool for '{model_name}'..."
    )

    baseline = get_baseline_for_model(model_name)
    user_prompt = (
        f"Determine the current official API pricing for the model '{model_name}' (also known as '{clean_model_display}'). "
        f"Use the web_search tool to find the exact cost per 1M input tokens and cost per 1M output tokens in USD ($). "
        f"As baseline guidance, standard rates for this family are approximately ${baseline['input_price_per_1m']:.2f}/1M input "
        f"and ${baseline['output_price_per_1m']:.2f}/1M output. "
        f"Search the web to verify the latest pricing and return ONLY the JSON object with fields: "
        f"model_name, input_price_per_1m, output_price_per_1m, currency ('USD'), source, source_url."
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": PRICING_AGENT_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    response = await run_in_threadpool(
        litellm.completion,
        model=PRICING_AGENT_MODEL,
        messages=messages,
        tools=PRICING_TOOL_SCHEMAS,
        tool_choice="auto",
    )

    response_message = response.choices[0].message
    tool_calls = getattr(response_message, "tool_calls", None)

    # Tool calling loop (up to 3 iterations for multi-step search/fetch)
    iterations = 0
    max_iterations = 3
    while tool_calls and iterations < max_iterations:
        iterations += 1
        logger.info(
            f"[PRICING AGENT] Iteration {iterations}: Agent requested {len(tool_calls)} tool call(s)."
        )

        if hasattr(response_message, "model_dump"):
            messages.append(response_message.model_dump())
        elif isinstance(response_message, dict):
            messages.append(response_message)
        else:
            messages.append(
                {
                    "role": "assistant",
                    "content": response_message.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in tool_calls
                    ],
                }
            )

        for tc in tool_calls:
            fn_name = tc.function.name
            fn_args = tc.function.arguments
            if isinstance(fn_args, str):
                try:
                    fn_args = json.loads(fn_args)
                except Exception:
                    fn_args = {}
            if not isinstance(fn_args, dict):
                fn_args = {}

            logger.info(f"[PRICING AGENT] Executing tool '{fn_name}' with args: {fn_args}")

            if fn_name == "web_search":
                q = fn_args.get("query", f"{clean_model_display} API pricing per 1M tokens")
                max_res = fn_args.get("max_results", 5)
                tool_result = await run_in_threadpool(web_search, query=q, max_results=max_res)
            elif fn_name in ("fetch_web_page", "web_scrape"):
                url = fn_args.get("url", "")
                max_ch = fn_args.get("max_chars", 4000)
                tool_result = await run_in_threadpool(fetch_web_page, url=url, max_chars=max_ch)
            else:
                tool_result = json.dumps({"error": f"Unknown tool: {fn_name}"})

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": fn_name,
                    "content": tool_result,
                }
            )

        follow_up = await run_in_threadpool(
            litellm.completion,
            model=PRICING_AGENT_MODEL,
            messages=messages,
            tools=PRICING_TOOL_SCHEMAS,
            tool_choice="auto",
        )
        response_message = follow_up.choices[0].message
        tool_calls = getattr(response_message, "tool_calls", None)

    raw_content = response_message.content or ""
    logger.debug(f"[PRICING AGENT] Raw response content: {raw_content}")

    # Parse JSON from model response
    cleaned = raw_content.strip()
    if "```json" in cleaned:
        cleaned = cleaned.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in cleaned:
        cleaned = cleaned.split("```", 1)[1].split("```", 1)[0].strip()

    json_match = re.search(r"\{[\s\S]*\}", cleaned)
    if not json_match:
        raise ValueError(f"Agent did not return valid JSON: {raw_content[:200]}")

    parsed = json.loads(json_match.group(0))
    in_price = float(parsed.get("input_price_per_1m", 0.0))
    out_price = float(parsed.get("output_price_per_1m", 0.0))
    source_name = parsed.get("source") or f"Mistral Web Search Agent ({PRICING_AGENT_MODEL})"
    source_url = parsed.get("source_url") or baseline.get("source_url", "")

    # Sanity check rates (0.001 USD to 500 USD per 1M tokens)
    if not (0.001 <= in_price <= 500.0 and 0.001 <= out_price <= 500.0):
        raise ValueError(
            f"Extracted prices out of acceptable range: input={in_price}, output={out_price}"
        )

    logger.info(
        f"[PRICING AGENT] Successfully resolved pricing for '{model_name}' via agent: "
        f"Input=${in_price}/1M, Output=${out_price}/1M (Source: {source_name}, URL: {source_url})"
    )

    return {
        "input_price_per_1m": in_price,
        "output_price_per_1m": out_price,
        "currency": "USD",
        "source": source_name,
        "source_url": source_url,
        "updated_at": time.time(),
        "updated_at_str": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
    }


async def fetch_pricing_from_web(model_name: str) -> dict[str, Any]:
    """
    Fetches the latest pricing for a model by invoking the mistral-small-latest
    pricing research agent with web_search tool. Falls back gracefully to direct
    web search extraction or baseline values if the agent call encounters an error.
    """
    baseline = get_baseline_for_model(model_name)
    clean_model_display = model_name.split("/")[-1] if "/" in model_name else model_name

    # 1. Primary path: mistral-small-latest agent equipped with web_search tool
    try:
        agent_result = await fetch_pricing_with_agent(model_name)
        if agent_result and agent_result.get("input_price_per_1m") is not None:
            return agent_result
    except Exception as e:
        logger.warning(
            f"[PRICING] Agent pricing lookup failed for '{model_name}': {e}. Attempting direct web search fallback..."
        )

    # 2. Secondary fallback: Direct web search with regex snippet extraction
    query = f"{clean_model_display} API pricing cost per 1M input output tokens"
    input_price = baseline["input_price_per_1m"]
    output_price = baseline["output_price_per_1m"]
    source = baseline.get("source", "Baseline")
    source_url = baseline.get("source_url", "https://artificialanalysis.ai/models")

    try:
        search_res_json = await run_in_threadpool(web_search, query=query, max_results=5)
        parsed_search = json.loads(search_res_json)
        results = parsed_search.get("results", [])

        combined_text = ""
        top_url = None
        for item in results:
            title = item.get("title", "")
            snippet = item.get("snippet", "")
            item_url = item.get("url", "")
            if item_url and not top_url:
                top_url = item_url
            combined_text += f"{title}\n{snippet}\n"

        extracted_in, extracted_out = _extract_pricing_from_search_text(
            combined_text, clean_model_display
        )
        if extracted_in is not None:
            input_price = extracted_in
            source = f"Web Search ({query})"
            if top_url:
                source_url = top_url
        if extracted_out is not None:
            output_price = extracted_out
            source = f"Web Search ({query})"
            if top_url:
                source_url = top_url

        logger.info(
            f"[PRICING] Direct web search resolved pricing for '{model_name}': "
            f"Input=${input_price}/1M, Output=${output_price}/1M (Source: {source}, URL: {source_url})"
        )
    except Exception as e:
        logger.warning(
            f"[PRICING] Direct web search fallback failed for '{model_name}': {e}. Using baseline."
        )

    return {
        "input_price_per_1m": float(input_price),
        "output_price_per_1m": float(output_price),
        "currency": "USD",
        "source": source,
        "source_url": source_url,
        "updated_at": time.time(),
        "updated_at_str": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
    }


async def refresh_model_pricing(model_name: str, force: bool = False) -> dict[str, Any]:
    """
    Returns the pricing dictionary for a given model.
    Checks the 7-day cache TTL and performs a web search only if stale or missing.
    """
    cache = load_pricing_cache()
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
            # Ensure source_url exists on returned cache entry
            if not cached_entry.get("source_url"):
                baseline = get_baseline_for_model(model_name)
                cached_entry["source_url"] = baseline.get("source_url", "https://artificialanalysis.ai/models")
            return cached_entry

    # Cache is missing or older than 7 days -> fetch fresh via web search
    fresh_pricing = await fetch_pricing_from_web(model_name)
    cache[norm] = fresh_pricing
    save_pricing_cache(cache)
    return fresh_pricing


def get_model_pricing(model_name: str) -> dict[str, Any]:
    """
    Synchronous getter for model pricing from cache or baseline.
    Does not block for web search.
    """
    cache = load_pricing_cache()
    norm = _normalize_model_name(model_name)
    baseline = get_baseline_for_model(model_name)
    if norm in cache:
        res = dict(cache[norm])
        if not res.get("source_url"):
            res["source_url"] = baseline.get("source_url", "https://artificialanalysis.ai/models")
        return res
    # Check baseline if not in cache
    res = dict(baseline)
    res["updated_at"] = 0
    return res


async def warm_up_pricing_cache(models: list[str] | None = None) -> None:
    """
    Warms up pricing cache at server startup for all known models.
    Refreshes only models that are missing from cache or older than 7 days.
    """
    if models is None:
        from config import AGENTS_CONFIG, THREAD_NOTES_MODEL
        models_to_check = {cfg["model"] for cfg in AGENTS_CONFIG.values()}
        if THREAD_NOTES_MODEL:
            models_to_check.add(THREAD_NOTES_MODEL)
    else:
        models_to_check = set(models)

    logger.info(f"[PRICING] Warming up pricing cache for {len(models_to_check)} models (7-day refresh check)...")
    for m in models_to_check:
        try:
            await refresh_model_pricing(m, force=False)
        except Exception as e:
            logger.warning(f"[PRICING] Warm-up failed for model '{m}': {e}")
