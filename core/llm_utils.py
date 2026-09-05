import asyncio
import inspect
import logging
import sys
from typing import Any

from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)

# Pacing and retry configuration
LLM_MAX_RETRIES = 3
LLM_RETRY_BACKOFF_BASE = 0.01 if "pytest" in sys.modules else 1.5


def is_anthropic_model(model: str | None) -> bool:
    """Checks whether the given model name targets Anthropic (Claude)."""
    if not model or not isinstance(model, str):
        return False
    m = model.lower().strip()
    return m.startswith("anthropic/") or "claude" in m


def is_openai_model(model: str | None) -> bool:
    """Checks whether the given model name targets OpenAI."""
    if not model or not isinstance(model, str):
        return False
    m = model.lower().strip()
    return (
        m.startswith("openai/")
        or "gpt" in m
        or "chatgpt" in m
        or m.startswith("o1")
        or m.startswith("o3")
        or "/o1" in m
        or "/o3" in m
    )


def classify_agent_error(e: Exception) -> str:
    """
    Classifies an agent execution exception into one of:
    - 'rate_limit': 429 Too Many Requests, quota exceeded, or rate limit crossed.
    - 'service_unavailable': 503 / 529 / overloaded / server temporarily unavailable / high demand spike.
    - 'api_key_not_found': Missing or invalid API key / authentication error.
    - 'model_not_found': Model does not exist or unknown model identifier.
    - 'general_error': Any other unexpected runtime exception.
    """
    error_str = str(e).lower()
    error_type = type(e).__name__.lower()

    # 1. Rate Limit Checks (429 / Quotas)
    if (
        "ratelimit" in error_type
        or "rate_limit" in error_str
        or "ratelimit" in error_str
        or "rate limit" in error_str
        or "429" in error_str
        or "too many requests" in error_str
        or "resourceexhausted" in error_str
        or "resource_exhausted" in error_str
        or "quota" in error_str
        or "tpm" in error_str
        or "rpm" in error_str
    ):
        return "rate_limit"

    # 2. Service Unavailable / High Demand / 503 Overloaded Checks
    if (
        "serviceunavailable" in error_type
        or "internalservererror" in error_type
        or "serveroverloaded" in error_type
        or "overloadederror" in error_type
        or "capacityerror" in error_type
        or "service_unavailable" in error_str
        or "service unavailable" in error_str
        or "serviceunavailable" in error_str
        or "503" in error_str
        or "529" in error_str
        or "high demand" in error_str
        or "high_demand" in error_str
        or "overloaded" in error_str
        or "capacity exceeded" in error_str
        or "temporarily unavailable" in error_str
        or "temporarily overloaded" in error_str
        or "model is overloaded" in error_str
        or "server is overloaded" in error_str
        or "upstream connect error" in error_str
    ):
        return "service_unavailable"

    # 3. API Key Not Found / Authentication Checks
    if (
        "auth" in error_type
        or "api_key" in error_str
        or "apikey" in error_str
        or "api key" in error_str
        or "authentication" in error_str
        or "unauthorized" in error_str
        or "401" in error_str
        or "forbidden" in error_str
        or "403" in error_str
    ):
        return "api_key_not_found"

    # 4. Model Not Found Checks
    if (
        "notfound" in error_type
        or "model_not_found" in error_str
        or "model not found" in error_str
        or "does not exist" in error_str
        or "is not found" in error_str
        or "invalid model" in error_str
        or "unknown model" in error_str
        or "404" in error_str
    ):
        return "model_not_found"

    return "general_error"


def is_high_demand_503_error(e: Exception) -> bool:
    """Checks whether an exception corresponds to a 503 / high demand / overloaded service error."""
    return classify_agent_error(e) == "service_unavailable"


async def _invoke_llm(func: Any, kwargs: dict[str, Any], runner: Any = None) -> Any:
    """Invokes LLM completion natively if coroutine, via runner if provided, or via threadpool if synchronous."""
    if runner is not None:
        return await runner(func, **kwargs)
    if inspect.iscoroutinefunction(func):
        return await func(**kwargs)
    if getattr(func, "__name__", "") == "completion" and not hasattr(func, "assert_called"):
        return await run_in_threadpool(func, **kwargs)
    res = func(**kwargs)
    if asyncio.iscoroutine(res):
        return await res
    return res


async def execute_llm_with_retry(
    func: Any,
    kwargs: dict[str, Any],
    agent_key: str,
    thread_key: tuple[str, str],
    is_superseded_fn=None,
    max_attempts: int = LLM_MAX_RETRIES,
    run_in_threadpool_fn=None,
    allow_503_immediate_retry: bool = True,
) -> Any:
    """
    Executes an LLM call natively or via runner with automatic retries and exponential backoff.
    Attempts up to `max_attempts` times before raising the last exception.
    If `allow_503_immediate_retry` is False and a 503/high-demand error occurs on attempt 1,
    the exception is raised immediately to allow postponement and multi-agent rotation.
    """
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        if is_superseded_fn and is_superseded_fn():
            logger.info(
                f"[LLM] Thread {thread_key} turn was superseded before LLM attempt {attempt}. Aborting."
            )
            return None

        try:
            if attempt > 1:
                logger.info(
                    f"[LLM] Retrying LLM completion for {agent_key} (attempt {attempt}/{max_attempts})..."
                )
            return await _invoke_llm(func, kwargs, runner=run_in_threadpool_fn)
        except Exception as exc:
            last_error = exc
            logger.warning(
                f"[LLM] Attempt {attempt}/{max_attempts} failed for {agent_key}: {exc}"
            )
            if not allow_503_immediate_retry and is_high_demand_503_error(exc):
                logger.info(
                    f"[LLM] 503 / High demand spike detected on first encounter for {agent_key}. Skipping immediate retry."
                )
                raise exc

            if attempt < max_attempts:
                await asyncio.sleep(LLM_RETRY_BACKOFF_BASE * attempt)
            else:
                raise last_error


