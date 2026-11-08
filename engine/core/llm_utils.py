import asyncio
from collections.abc import Callable
import inspect
import json
import logging
from typing import Any

import litellm

from core.paths import SYSTEM_SETTINGS_FILE

logger = logging.getLogger(__name__)

# Pacing and retry configuration
_llm_retry_config_provider: Callable[[], tuple[int, float]] | None = None


def set_llm_retry_config_provider(provider: Callable[[], tuple[int, float]] | None) -> None:
    """Injectable provider for LLM retries and backoff base, decoupling core from orchestration."""
    global _llm_retry_config_provider
    _llm_retry_config_provider = provider


def get_llm_max_retries() -> int:
    if _llm_retry_config_provider is not None:
        try:
            return int(_llm_retry_config_provider()[0])
        except Exception:
            pass
    try:
        if SYSTEM_SETTINGS_FILE.exists():
            with open(SYSTEM_SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return int(data.get("llm_max_retries", 3))
    except Exception:  # noqa: BLE001
        pass
    return 3


def get_llm_retry_backoff_base() -> float:
    if _llm_retry_config_provider is not None:
        try:
            return float(_llm_retry_config_provider()[1])
        except Exception:
            pass
    try:
        if SYSTEM_SETTINGS_FILE.exists():
            with open(SYSTEM_SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return float(data.get("llm_retry_backoff_base", 1.5))
    except Exception:  # noqa: BLE001
        pass
    return 1.5


LLM_MAX_RETRIES = get_llm_max_retries()


_privacy_mode_provider: Callable[[], bool] | None = None
_pii_masker_instance: Any = None
_llm_completion_fn: Any = None
_llm_sleep_fn: Any = None


def configure_llm_completion_fn(fn: Any) -> None:
    """Configures an in-memory LLM completion function override for tests and dependency injection."""
    global _llm_completion_fn
    _llm_completion_fn = fn


def reset_llm_completion_fn() -> None:
    """Resets the in-memory LLM completion override."""
    global _llm_completion_fn
    _llm_completion_fn = None


def get_llm_completion_fn() -> Any:
    """Returns the currently configured LLM completion function override, if any."""
    return _llm_completion_fn


def configure_llm_sleep_fn(fn: Any) -> None:
    """Configures an in-memory sleep function for LLM backoff retries."""
    global _llm_sleep_fn
    _llm_sleep_fn = fn


def reset_llm_sleep_fn() -> None:
    """Resets the in-memory sleep function for LLM retries."""
    global _llm_sleep_fn
    _llm_sleep_fn = None


def set_privacy_mode_provider(provider: Callable[[], bool] | None) -> None:
    """Injectable provider for privacy mode, decoupling core from orchestration."""
    global _privacy_mode_provider
    _privacy_mode_provider = provider


def set_pii_masker_instance(masker: Any) -> None:
    """Injectable PII masker, decoupling core from services."""
    global _pii_masker_instance
    _pii_masker_instance = masker


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
        m.startswith(("openai/", "o1", "o3"))
        or "gpt" in m
        or "chatgpt" in m
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
    # 1. Type inspection against litellm.exceptions
    try:
        if isinstance(e, litellm.exceptions.RateLimitError):
            return "rate_limit"
        if isinstance(
            e,
            (
                litellm.exceptions.ServiceUnavailableError,
                litellm.exceptions.Timeout,
                litellm.exceptions.InternalServerError,
                litellm.exceptions.APIConnectionError,
            ),
        ):
            return "service_unavailable"
        if isinstance(
            e,
            (
                litellm.exceptions.AuthenticationError,
                litellm.exceptions.PermissionDeniedError,
            ),
        ):
            return "api_key_not_found"
        if isinstance(e, litellm.exceptions.NotFoundError):
            return "model_not_found"
    except (AttributeError, TypeError):
        pass

    # 2. HTTP status code inspection
    status_code = (
        getattr(e, "status_code", None)
        or getattr(e, "http_status", None)
        or getattr(getattr(e, "response", None), "status_code", None)
    )
    if isinstance(status_code, int):
        if status_code == 429:
            return "rate_limit"
        if status_code in (500, 502, 503, 504, 529):
            return "service_unavailable"
        if status_code in (401, 403):
            return "api_key_not_found"
        if status_code == 404:
            return "model_not_found"

    error_str = str(e).lower()
    error_type = type(e).__name__.lower()

    # 3. Fallback string matching for wrapped exceptions
    if (
        "rate_limit" in error_str
        or "ratelimit" in error_str
        or "rate limit" in error_str
        or "429" in error_str
        or "too many requests" in error_str
        or "resourceexhausted" in error_str
        or "resource_exhausted" in error_str
        or "quota" in error_str
    ):
        return "rate_limit"

    if (
        "serviceunavailable" in error_type
        or "internalservererror" in error_type
        or "serveroverloaded" in error_type
        or "overloadederror" in error_type
        or "capacityerror" in error_type
        or "service_unavailable" in error_str
        or "service unavailable" in error_str
        or "503" in error_str
        or "529" in error_str
        or "high demand" in error_str
        or "high_demand" in error_str
        or "overloaded" in error_str
        or "capacity exceeded" in error_str
        or "temporarily unavailable" in error_str
    ):
        return "service_unavailable"

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


async def _invoke_llm(func: Any, kwargs: dict[str, Any]) -> Any:
    """Invokes async LLM completion natively."""
    res = func(**kwargs)
    if inspect.isawaitable(res):
        return await res
    return res



async def execute_llm_with_retry(
    func: Any,
    kwargs: dict[str, Any],
    agent_key: str,
    thread_key: tuple[str, str],
    is_superseded_fn=None,
    max_attempts: int = LLM_MAX_RETRIES,
    allow_503_immediate_retry: bool = True,
    turn_token_mapping: dict[str, str] | None = None,
) -> Any:
    """
    Executes an LLM call natively or via runner with automatic retries and exponential backoff.
    Attempts up to `max_attempts` times before raising the last exception.
    If `allow_503_immediate_retry` is False and a 503/high-demand error occurs on attempt 1,
    the exception is raised immediately to allow postponement and multi-agent rotation.
    """
    import copy

    if _privacy_mode_provider is not None:
        try:
            is_strict = _privacy_mode_provider()
        except Exception:
            is_strict = False
    else:
        try:
            if SYSTEM_SETTINGS_FILE.exists():
                with open(SYSTEM_SETTINGS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                is_strict = bool(data.get("strict_privacy_mode_enabled", True))
            else:
                is_strict = True
        except Exception:
            is_strict = True

    masker = _pii_masker_instance
    active_mapping: dict[str, str] = turn_token_mapping if turn_token_mapping is not None else {}

    # 1. Masking Phase
    if is_strict and masker and "messages" in kwargs:
        # Create a deep copy to avoid mutating the original message array referenced elsewhere
        kwargs = copy.deepcopy(kwargs)
        for msg in kwargs.get("messages", []):
            if isinstance(msg, dict) and msg.get("role") == "system":
                continue
            if "content" in msg and isinstance(msg["content"], str):
                masked_content, mapping = await masker.async_mask(msg["content"], existing_mapping=active_mapping)
                msg["content"] = masked_content
                active_mapping.update(mapping)
            elif "content" in msg and isinstance(msg["content"], list):
                for part in msg["content"]:
                    if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                        masked_part, mapping = await masker.async_mask(part["text"], existing_mapping=active_mapping)
                        part["text"] = masked_part
                        active_mapping.update(mapping)
            if "tool_calls" in msg and isinstance(msg["tool_calls"], list):
                for tc in msg["tool_calls"]:
                    fn = tc.get("function") if isinstance(tc, dict) else getattr(tc, "function", None)
                    if fn:
                        args = fn.get("arguments") if isinstance(fn, dict) else getattr(fn, "arguments", None)
                        if isinstance(args, str):
                            masked_args, mapping = await masker.async_mask(args, existing_mapping=active_mapping)
                            if isinstance(fn, dict):
                                fn["arguments"] = masked_args
                            else:
                                fn.arguments = masked_args
                            active_mapping.update(mapping)

    logger.debug(f"[LLM] Prepared prompt for {agent_key}: {kwargs.get('messages', [])}")

    # Enforce single retry layer: prevent underlying LiteLLM from retrying internally
    kwargs.setdefault("num_retries", 0)

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
            active_func = _llm_completion_fn or func or litellm.acompletion
            res = await _invoke_llm(active_func, kwargs)

            # 2. Unmasking Phase
            if is_strict and active_mapping and hasattr(res, "choices") and res.choices and masker:
                first_choice = res.choices[0]
                if hasattr(first_choice, "message"):
                    if hasattr(first_choice.message, "content") and isinstance(first_choice.message.content, str):
                        unmasked_content = masker.unmask(first_choice.message.content, active_mapping)
                        first_choice.message.content = unmasked_content
                    if hasattr(first_choice.message, "tool_calls") and first_choice.message.tool_calls:
                        for tc in first_choice.message.tool_calls:
                            if hasattr(tc, "function") and hasattr(tc.function, "arguments") and isinstance(tc.function.arguments, str):
                                tc.function.arguments = masker.unmask(tc.function.arguments, active_mapping)

            if turn_token_mapping is not None:
                turn_token_mapping.update(active_mapping)

            return res

        except asyncio.CancelledError:
            raise
        except (TimeoutError, asyncio.TimeoutError) as exc:
            last_error = exc
            logger.warning(
                f"[LLM] Attempt {attempt}/{max_attempts} timed out for {agent_key}: {exc}"
            )
            sleep_call = _llm_sleep_fn or asyncio.sleep
            if attempt < max_attempts:
                await sleep_call(get_llm_retry_backoff_base() * attempt)
            else:
                raise last_error
        except Exception as exc:
            last_error = exc
            logger.warning(
                f"[LLM] Attempt {attempt}/{max_attempts} failed for {agent_key}: {exc}"
            )
            if not allow_503_immediate_retry and is_high_demand_503_error(exc):
                logger.info(
                    f"[LLM] 503 / High demand spike detected on first encounter for {agent_key}. Skipping immediate retry."
                )
                raise

            sleep_call = _llm_sleep_fn or asyncio.sleep
            if attempt < max_attempts:
                await sleep_call(get_llm_retry_backoff_base() * attempt)
            else:
                raise last_error


