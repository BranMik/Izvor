"""
Structured JSON Pricing Feeds for Izvor.

Queries machine-readable provider/aggregator pricing registries (LiteLLM live JSON
and OpenRouter Models API) with SSRF protection, strict canonical model matching,
and short-lived in-memory batch caching before falling back to LLM research.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
import urllib.parse
from collections.abc import Callable
from typing import Any

import httpx

from .web_tools import (
    MAX_REDIRECTS,
    PinnedIPTransport,
    get_web_fetch_timeout,
    get_web_security_limits,
    get_web_user_agent,
    sanitize_outbound_url,
    validate_url_security,
)

logger = logging.getLogger(__name__)

LITELLM_PRICING_JSON_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)
LITELLM_PRICING_SOURCE_URL = (
    "https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json"
)
OPENROUTER_MODELS_API_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_PRICING_SOURCE_URL = "https://openrouter.ai/models"

FEED_CACHE_TTL_SECONDS: float = 300.0
FEED_MAX_BODY_BYTES: int = 10 * 1024 * 1024  # 10 MB cap for full LiteLLM registry JSON

MIN_VALID_PRICE_PER_1M: float = 0.005
MAX_VALID_PRICE_PER_1M: float = 500.0
DEFAULT_FALLBACK_SOURCE: str = "High-Tier Safety Fallback"

_DIRECT_SINGLE_SLASH_PROVIDERS: frozenset[str] = frozenset(
    {"gemini", "openai", "anthropic", "mistral", "deepseek", "perplexity", "cohere"}
)

_feed_fetcher_override: Callable[[str], Any] | None = None
_feed_cache: dict[str, tuple[float, Any]] = {}


def normalize_model_name(model_name: str) -> str:
    """Normalizes model string (e.g. 'gemini/gemini-4-hydrogen')."""
    if not isinstance(model_name, str):
        model_name = str(model_name) if model_name is not None else ""
    return model_name.strip().lower()


def is_valid_price_pair(in_price: Any, out_price: Any) -> bool:
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


def is_within_sanity_bounds(in_price: float, out_price: float, baseline: dict[str, Any]) -> bool:
    """
    Validates that candidate prices do not deviate wildly (< 10% or > 1000%)
    from an established model baseline (from catalog or community registry).
    """
    try:
        if baseline.get("source") == DEFAULT_FALLBACK_SOURCE:
            return True
        base_in = float(baseline.get("input_price_per_1m") or 0.0)
        base_out = float(baseline.get("output_price_per_1m") or 0.0)
        if base_in > 0:
            if in_price < 0.1 * base_in or in_price > 10.0 * base_in:
                return False
        if base_out > 0:
            if out_price < 0.1 * base_out or out_price > 10.0 * base_out:
                return False
        return True
    except (TypeError, ValueError):
        return False


def configure_feed_fetcher(fn: Callable[[str], Any] | None) -> None:
    """Configures an in-memory JSON feed fetcher override for tests and dependency injection."""
    global _feed_fetcher_override
    _feed_fetcher_override = fn


def reset_feed_fetcher() -> None:
    """Resets the in-memory JSON feed fetcher override."""
    global _feed_fetcher_override
    _feed_fetcher_override = None


def clear_feed_cache() -> None:
    """Clears the short-lived in-memory batch feed cache."""
    _feed_cache.clear()


def _toggle_version_separator(name: str) -> list[str]:
    """Generates `-` <-> `.` digit-version variants for Anthropic model IDs."""
    variants = [name]
    dot_ver = re.sub(r"(\d)-(\d)", r"\1.\2", name)
    dash_ver = re.sub(r"(\d)\.(\d)", r"\1-\2", name)
    for variant in (dot_ver, dash_ver):
        if variant not in variants:
            variants.append(variant)
    return variants


async def _download_json_feed(url: str) -> Any | None:
    """Downloads and parses a JSON pricing feed using SSRF and IP-pinning guards."""
    is_safe, cleaned_url, reason, resolved_ip = await asyncio.to_thread(validate_url_security, url)
    if not is_safe:
        logger.warning(f"[PRICING_FEEDS] Blocked unsafe feed URL '{url}': {reason}")
        return None

    cleaned_url = sanitize_outbound_url(cleaned_url)
    sec_limits = get_web_security_limits()
    max_redirects = sec_limits.get("max_redirects", MAX_REDIRECTS)
    fetch_timeout = max(float(get_web_fetch_timeout()), 15.0)
    headers = {
        "User-Agent": get_web_user_agent(),
        "Accept": "application/json, text/plain;q=0.9, */*;q=0.5",
    }

    current_url = cleaned_url
    current_ip = resolved_ip
    redirect_count = 0
    deadline = time.monotonic() + fetch_timeout

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning(f"[PRICING_FEEDS] Feed download timed out for '{cleaned_url}'.")
                return None

            hop_timeout = httpx.Timeout(remaining, connect=min(remaining, 10.0), read=remaining)
            transport = PinnedIPTransport(current_ip) if current_ip else None
            async with httpx.AsyncClient(
                transport=transport, timeout=hop_timeout, follow_redirects=False
            ) as client:
                async with client.stream("GET", current_url, headers=headers) as response:
                    if (300 <= response.status_code < 400) and response.headers.get("location"):
                        redirect_count += 1
                        if redirect_count > max_redirects:
                            logger.warning(
                                f"[PRICING_FEEDS] Exceeded max redirects ({max_redirects}) for '{cleaned_url}'."
                            )
                            return None
                        loc = response.headers.get("location", "")
                        next_url = urllib.parse.urljoin(current_url, loc)
                        ok, validated_next, redir_reason, next_ip = await asyncio.to_thread(
                            validate_url_security, next_url
                        )
                        if not ok:
                            logger.warning(
                                f"[PRICING_FEEDS] Blocked unsafe feed redirect '{next_url}': {redir_reason}"
                            )
                            return None
                        current_url = sanitize_outbound_url(validated_next)
                        current_ip = next_ip
                        continue

                    if response.status_code != 200:
                        logger.warning(
                            f"[PRICING_FEEDS] Feed '{current_url}' returned HTTP {response.status_code}."
                        )
                        return None

                    content_type = response.headers.get("content-type", "").lower()
                    mime_type = content_type.split(";")[0].strip() if content_type else ""
                    is_json_or_text = bool(mime_type) and (
                        mime_type in {"application/json", "text/plain", "text/json"}
                        or mime_type.endswith("+json")
                    )
                    if not is_json_or_text:
                        logger.warning(
                            f"[PRICING_FEEDS] Disallowed Content-Type '{mime_type or 'missing'}' for '{current_url}'."
                        )
                        return None

                    chunks: list[bytes] = []
                    total_bytes = 0
                    aiter = response.aiter_bytes(chunk_size=65536).__aiter__()
                    while True:
                        chunk_remaining = deadline - time.monotonic()
                        if chunk_remaining <= 0:
                            raise httpx.ReadTimeout("Feed download wall-clock deadline exceeded.")
                        try:
                            chunk = await asyncio.wait_for(aiter.__anext__(), timeout=chunk_remaining)
                        except StopAsyncIteration:
                            break
                        except TimeoutError as te:
                            raise httpx.ReadTimeout("Feed download wall-clock deadline exceeded.") from te
                        total_bytes += len(chunk)
                        if total_bytes > FEED_MAX_BODY_BYTES:
                            logger.warning(
                                f"[PRICING_FEEDS] Feed '{current_url}' exceeded {FEED_MAX_BODY_BYTES} bytes."
                            )
                            return None
                        chunks.append(chunk)

                    encoding = response.encoding or "utf-8"
                    raw_text = b"".join(chunks).decode(encoding, errors="replace")
                    return json.loads(raw_text)
    except (httpx.HTTPError, OSError, json.JSONDecodeError, ValueError, TimeoutError) as e:
        logger.warning(f"[PRICING_FEEDS] Failed loading JSON feed '{url}': {e}")
        return None
    except Exception as e:  # noqa: BLE001 - Feed download error boundary returns None
        logger.warning(f"[PRICING_FEEDS] Unexpected error loading JSON feed '{url}': {e}")
        return None


async def fetch_json_feed(
    url: str,
    *,
    http_fetcher: Callable[[str], Any] | None = None,
    ttl_seconds: float = FEED_CACHE_TTL_SECONDS,
) -> Any | None:
    """
    Fetches a JSON pricing feed with short-lived in-memory batch caching.
    Supports injected `http_fetcher` or global `configure_feed_fetcher()` override for tests.
    """
    now_mono = time.monotonic()
    cached = _feed_cache.get(url)
    if cached is not None:
        cached_at, cached_payload = cached
        if (now_mono - cached_at) < ttl_seconds:
            return cached_payload

    payload: Any = None
    if http_fetcher is not None:
        res = http_fetcher(url)
        payload = await res if inspect.isawaitable(res) else res
    elif _feed_fetcher_override is not None:
        res = _feed_fetcher_override(url)
        payload = await res if inspect.isawaitable(res) else res
    else:
        payload = await _download_json_feed(url)

    if payload is not None:
        _feed_cache[url] = (time.monotonic(), payload)
    return payload


def _build_litellm_candidates(norm: str) -> list[str]:
    """Builds exact canonical lookup keys for LiteLLM's model_prices_and_context_window.json."""
    candidates: list[str] = []

    def _add(item: str) -> None:
        if item and item not in candidates:
            candidates.append(item)

    _add(norm)

    if norm.startswith("openrouter/"):
        _add(norm[len("openrouter/"):])

    if "/" in norm:
        provider, rest = norm.split("/", 1)
        if provider in _DIRECT_SINGLE_SLASH_PROVIDERS and "/" not in rest:
            _add(rest)

    if norm.startswith("anthropic/") or norm.startswith("claude-"):
        for base in list(candidates):
            for variant in _toggle_version_separator(base):
                _add(variant)

    return candidates


def _lookup_litellm_pricing(model_name: str, payload: Any) -> dict[str, Any] | None:
    """Looks up exact canonical pricing from LiteLLM's live pricing JSON dictionary."""
    if not isinstance(payload, dict):
        return None

    norm = normalize_model_name(model_name)
    if not norm:
        return None

    index: dict[str, dict[str, Any]] = {
        str(k).strip().lower(): v for k, v in payload.items() if isinstance(v, dict)
    }

    for key in _build_litellm_candidates(norm):
        entry = index.get(key)
        if not entry:
            continue
        in_cpt = entry.get("input_cost_per_token")
        out_cpt = entry.get("output_cost_per_token")
        if in_cpt is None or out_cpt is None:
            continue
        try:
            in_1m = round(float(in_cpt) * 1_000_000, 4)
            out_1m = round(float(out_cpt) * 1_000_000, 4)
        except (TypeError, ValueError):
            continue
        if is_valid_price_pair(in_1m, out_1m):
            now = time.time()
            return {
                "input_price_per_1m": in_1m,
                "output_price_per_1m": out_1m,
                "currency": "USD",
                "source": "LiteLLM Live Pricing Feed",
                "source_url": LITELLM_PRICING_SOURCE_URL,
                "updated_at": now,
                "updated_at_str": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(now)),
            }

    return None


def _build_openrouter_candidates(norm: str) -> list[str]:
    """Maps canonical Izvor model identifiers to OpenRouter catalog IDs."""
    if not norm or norm == "openrouter/auto" or norm.startswith(("groq/", "together_ai/")):
        return []

    if norm.startswith("openrouter/"):
        rest = norm[len("openrouter/"):]
        if rest.startswith("anthropic/"):
            return _toggle_version_separator(rest)
        return [rest] if rest else []

    if norm.startswith("gemini/"):
        return [f"google/{norm.split('/', 1)[1]}"]
    if norm.startswith("mistral/"):
        return [f"mistralai/{norm.split('/', 1)[1]}"]
    if norm.startswith("openai/"):
        return [norm]
    if norm.startswith("anthropic/"):
        return _toggle_version_separator(norm)
    if norm.startswith("deepseek/"):
        return [norm]
    if norm.startswith("perplexity/"):
        return [norm]
    if norm.startswith("meta/"):
        rest = norm.split("/", 1)[1]
        return [f"meta-llama/{rest}", f"meta/{rest}"]
    if norm.startswith("command-"):
        return [f"cohere/{norm}"]
    if "/" in norm:
        return [norm]
    return []


def _lookup_openrouter_pricing(model_name: str, payload: Any) -> dict[str, Any] | None:
    """Looks up exact canonical pricing from OpenRouter's /api/v1/models JSON payload."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return None

    norm = normalize_model_name(model_name)
    candidates = _build_openrouter_candidates(norm)
    if not candidates:
        return None

    index: dict[str, dict[str, Any]] = {}
    for item in payload["data"]:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id", "")).strip().lower()
        if not item_id or item_id.endswith(":free"):
            continue
        index[item_id] = item

    for cand_id in candidates:
        item = index.get(cand_id)
        if not item:
            continue
        pricing = item.get("pricing")
        if not isinstance(pricing, dict):
            continue
        raw_prompt = pricing.get("prompt")
        raw_completion = pricing.get("completion")
        if raw_prompt is None or raw_completion is None:
            continue
        try:
            prompt_cpt = float(raw_prompt)
            completion_cpt = float(raw_completion)
        except (TypeError, ValueError):
            continue
        if prompt_cpt <= 0 or completion_cpt <= 0:
            continue
        in_1m = round(prompt_cpt * 1_000_000, 4)
        out_1m = round(completion_cpt * 1_000_000, 4)
        if is_valid_price_pair(in_1m, out_1m):
            now = time.time()
            return {
                "input_price_per_1m": in_1m,
                "output_price_per_1m": out_1m,
                "currency": "USD",
                "source": "OpenRouter Live Models API",
                "source_url": OPENROUTER_PRICING_SOURCE_URL,
                "updated_at": now,
                "updated_at_str": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(now)),
            }

    return None


async def fetch_pricing_from_feeds(
    model_name: str,
    *,
    baseline: dict[str, Any] | None = None,
    http_fetcher: Callable[[str], Any] | None = None,
) -> dict[str, Any] | None:
    """
    Queries live structured JSON pricing feeds in priority order and returns the first
    sanity-validated pricing entry, or None if unavailable.
    """
    norm = normalize_model_name(model_name)
    if norm.startswith("openrouter/"):
        feed_plan = (
            ("openrouter", OPENROUTER_MODELS_API_URL, _lookup_openrouter_pricing),
            ("litellm", LITELLM_PRICING_JSON_URL, _lookup_litellm_pricing),
        )
    else:
        feed_plan = (
            ("litellm", LITELLM_PRICING_JSON_URL, _lookup_litellm_pricing),
            ("openrouter", OPENROUTER_MODELS_API_URL, _lookup_openrouter_pricing),
        )

    for feed_name, url, lookup_fn in feed_plan:
        payload = await fetch_json_feed(url, http_fetcher=http_fetcher)
        if payload is None:
            continue
        candidate = lookup_fn(model_name, payload)
        if candidate is None:
            continue
        if baseline is not None and not is_within_sanity_bounds(
            candidate["input_price_per_1m"],
            candidate["output_price_per_1m"],
            baseline,
        ):
            logger.warning(
                f"[PRICING_FEEDS] Rejected out-of-bounds pricing from {feed_name} for '{model_name}': "
                f"in={candidate['input_price_per_1m']}, out={candidate['output_price_per_1m']}"
            )
            continue
        return candidate

    return None

