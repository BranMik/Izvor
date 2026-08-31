import asyncio
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
    - 'api_key_not_found': Missing or invalid API key / authentication error.
    - 'model_not_found': Model does not exist or unknown model identifier.
    - 'rate_limit': 429 Too Many Requests, quota exceeded, or rate limit crossed.
    - 'general_error': Any other unexpected runtime exception.
    """
    error_str = str(e).lower()
    error_type = type(e).__name__.lower()

    # 1. Rate Limit Checks
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

    # 2. API Key Not Found / Authentication Checks
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

    # 3. Model Not Found Checks
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


async def execute_llm_with_retry(
    func: Any,
    kwargs: dict[str, Any],
    agent_key: str,
    thread_key: tuple[str, str],
    is_superseded_fn=None,
    max_attempts: int = LLM_MAX_RETRIES,
    run_in_threadpool_fn=None,
) -> Any:
    """
    Executes an LLM call in a threadpool with automatic retries and exponential backoff.
    Attempts up to `max_attempts` times before raising the last exception.
    """
    runner = run_in_threadpool_fn or run_in_threadpool
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
            return await runner(func, **kwargs)
        except Exception as exc:
            last_error = exc
            logger.warning(
                f"[LLM] Attempt {attempt}/{max_attempts} failed for {agent_key}: {exc}"
            )
            if attempt < max_attempts:
                await asyncio.sleep(LLM_RETRY_BACKOFF_BASE * attempt)
            else:
                raise last_error


# Backwards compatibility alias
_execute_llm_with_retry = execute_llm_with_retry
