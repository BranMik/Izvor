import asyncio
from collections.abc import Callable
import copy
import hashlib
import inspect
import json
import logging
import random
import re
import sys
from typing import Any

from core.paths import SYSTEM_SETTINGS_FILE

logger = logging.getLogger(__name__)

MaskedTextCache = dict[str, tuple[str, dict[str, str]]]
_LITERAL_PLACEHOLDER_RE = re.compile(r"<[A-Z][A-Z0-9_]*_\d+>")

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
        except (AttributeError, IndexError, RuntimeError, TypeError, ValueError) as exc:
            logger.debug(f"[LLM] Retry config provider fallback: {exc}")
    try:
        if SYSTEM_SETTINGS_FILE.exists():
            with open(SYSTEM_SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return int(data.get("llm_max_retries", 3))
    except (OSError, ValueError, TypeError) as exc:
        logger.debug(f"[LLM] Failed reading llm_max_retries: {exc}")
    return 3


def get_llm_retry_backoff_base() -> float:
    if _llm_retry_config_provider is not None:
        try:
            return float(_llm_retry_config_provider()[1])
        except (AttributeError, IndexError, RuntimeError, TypeError, ValueError) as exc:
            logger.debug(f"[LLM] Retry backoff provider fallback: {exc}")
    try:
        if SYSTEM_SETTINGS_FILE.exists():
            with open(SYSTEM_SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return float(data.get("llm_retry_backoff_base", 1.5))
    except (OSError, ValueError, TypeError) as exc:
        logger.debug(f"[LLM] Failed reading llm_retry_backoff_base: {exc}")
    return 1.5


LLM_MAX_RETRIES = get_llm_max_retries()


_privacy_mode_provider: Callable[[], bool] | None = None
_pii_masker_instance: Any = None
_llm_completion_fn: Any = None
_default_llm_guard: Callable[..., Any] | None = None
_llm_sleep_fn: Any = None


def _get_litellm() -> Any:
    """Lazily imports and returns the litellm module."""
    import litellm

    return litellm


def _is_unmocked_litellm_callable(func: Any) -> bool:
    """Returns True when `func` is None or an unpatched litellm completion/embedding function."""
    if func is None:
        return True
    mod = getattr(func, "__module__", "") or ""
    name = getattr(func, "__name__", "") or ""
    return isinstance(mod, str) and mod.startswith("litellm.") and name in ("acompletion", "aembedding")


def configure_default_llm_guard(guard: Callable[..., Any] | None) -> None:
    """Configures a fail-closed guard invoked when an unmocked litellm call is attempted in tests."""
    global _default_llm_guard
    _default_llm_guard = guard


def reset_default_llm_guard() -> None:
    """Resets the fail-closed default LLM guard."""
    global _default_llm_guard
    _default_llm_guard = None


def configure_llm_completion_fn(fn: Any) -> None:
    """Configures an in-memory LLM completion function override for tests and dependency injection."""
    global _llm_completion_fn
    _llm_completion_fn = fn


def reset_llm_completion_fn() -> None:
    """Resets the in-memory LLM completion override."""
    global _llm_completion_fn
    _llm_completion_fn = None


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


def _has_status_code(error_str: str, *codes: int) -> bool:
    """Checks for HTTP status code tokens using word boundaries to avoid substring matches on IDs."""
    pattern = r"\b(?:" + "|".join(str(code) for code in codes) + r")\b"
    return re.search(pattern, error_str) is not None


def _is_api_key_error_text(error_str: str, error_type: str) -> bool:
    """Checks whether exception text or type indicates a missing or invalid API key."""
    is_auth_type = (
        error_type in ("authenticationerror", "autherror", "permissiondeniederror", "unauthenticatederror")
        or error_type.startswith("auth")
        or error_type.endswith("autherror")
        or "authentication" in error_type
    )
    return (
        is_auth_type
        or "api_key" in error_str
        or "apikey" in error_str
        or "api key" in error_str
        or "missing credentials" in error_str
        or "authentication" in error_str
        or "unauthorized" in error_str
        or _has_status_code(error_str, 401, 403)
        or "forbidden" in error_str
    )


def classify_agent_error(e: Exception) -> str:
    """
    Classifies an agent execution exception into one of:
    - 'rate_limit': 429 Too Many Requests, quota exceeded, or rate limit crossed.
    - 'service_unavailable': 500 / 502 / 503 / 504 / 529 / overloaded / server temporarily unavailable / high demand spike.
    - 'api_key_not_found': Missing or invalid API key / authentication error.
    - 'model_not_found': Model does not exist or unknown model identifier.
    - 'bad_request': Non-retryable 400 / 413 / 422 invalid request, context window, or content policy error.
    - 'general_error': Any other unexpected runtime exception.
    """
    error_str = str(e).lower()
    error_type = type(e).__name__.lower()

    # 1. Type inspection against litellm.exceptions (only if litellm is already imported;
    #    auth/rate-limit/not-found first; check API-key wording before treating
    #    InternalServerError/APIConnectionError as 503 or BadRequestError as non-auth
    #    bad_request, because LiteLLM wraps missing keys there).
    litellm_mod = sys.modules.get("litellm")
    litellm_exceptions = getattr(litellm_mod, "exceptions", None) if litellm_mod is not None else None
    if litellm_exceptions is not None:
        try:
            if isinstance(e, litellm_exceptions.RateLimitError):
                return "rate_limit"
            if isinstance(
                e,
                (
                    litellm_exceptions.AuthenticationError,
                    litellm_exceptions.PermissionDeniedError,
                ),
            ):
                return "api_key_not_found"
            if isinstance(e, litellm_exceptions.NotFoundError):
                return "model_not_found"
            bad_request_types = tuple(
                cls
                for cls in (
                    getattr(litellm_exceptions, "BadRequestError", None),
                    getattr(litellm_exceptions, "ContextWindowExceededError", None),
                    getattr(litellm_exceptions, "ContentPolicyViolationError", None),
                )
                if cls is not None
            )
            if bad_request_types and isinstance(e, bad_request_types):
                if _is_api_key_error_text(error_str, error_type):
                    return "api_key_not_found"
                return "bad_request"
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
        if status_code in (401, 403):
            return "api_key_not_found"
        if status_code == 404:
            return "model_not_found"
        if status_code in (400, 413, 422):
            if _is_api_key_error_text(error_str, error_type):
                return "api_key_not_found"
            return "bad_request"

    # 3. API key / credential errors before 5xx / APIConnectionError checks
    if _is_api_key_error_text(error_str, error_type):
        return "api_key_not_found"

    try:
        import httpx

        httpx_timeout_cls: tuple[type[BaseException], ...] = (httpx.TimeoutException,)
    except ImportError:
        httpx_timeout_cls = ()

    if isinstance(e, (TimeoutError, asyncio.TimeoutError) + httpx_timeout_cls):
        return "service_unavailable"

    if litellm_exceptions is not None:
        try:
            if isinstance(
                e,
                (
                    litellm_exceptions.ServiceUnavailableError,
                    litellm_exceptions.Timeout,
                    litellm_exceptions.InternalServerError,
                    litellm_exceptions.APIConnectionError,
                ),
            ):
                return "service_unavailable"
        except (AttributeError, TypeError):
            pass

    if isinstance(status_code, int) and status_code in (500, 502, 503, 504, 529):
        return "service_unavailable"

    # 4. Fallback string matching for wrapped exceptions
    if (
        "rate_limit" in error_str
        or "ratelimit" in error_str
        or "rate limit" in error_str
        or _has_status_code(error_str, 429)
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
        or "timeout" in error_type
        or "service_unavailable" in error_str
        or "service unavailable" in error_str
        or "timed out" in error_str
        or "timeout" in error_str
        or re.search(r"\b(?:http|status|code|error)[:\s=]*500\b|\b500\s+(?:internal|error|server)\b", error_str)
        is not None
        or _has_status_code(error_str, 502, 503, 504, 529)
        or "high demand" in error_str
        or "high_demand" in error_str
        or "overloaded" in error_str
        or "capacity exceeded" in error_str
        or "temporarily unavailable" in error_str
    ):
        return "service_unavailable"

    if (
        "notfound" in error_type
        or "model_not_found" in error_str
        or "model not found" in error_str
        or "does not exist" in error_str
        or "is not found" in error_str
        or "invalid model" in error_str
        or "unknown model" in error_str
        or _has_status_code(error_str, 404)
    ):
        return "model_not_found"

    if (
        "badrequest" in error_type
        or "contextwindowexceeded" in error_type
        or "contentpolicyviolation" in error_type
        or "bad_request" in error_str
        or "bad request" in error_str
        or "context_length_exceeded" in error_str
        or "context window" in error_str
        or _has_status_code(error_str, 400, 413, 422)
    ):
        return "bad_request"

    return "general_error"


def _calculate_backoff_delay(attempt: int) -> float:
    """Computes exponential backoff delay (with bounded jitter outside deterministic test sleep injection)."""
    base = float(get_llm_retry_backoff_base())
    delay: float = base * float(2 ** max(0, attempt - 1))
    if _llm_sleep_fn is None:
        delay += random.uniform(0.0, 0.25 * base)
    return float(delay)


def is_high_demand_503_error(e: Exception) -> bool:
    """Checks whether an exception corresponds to a 503 / high demand / overloaded service error."""
    return classify_agent_error(e) == "service_unavailable"


async def _invoke_llm(func: Any, kwargs: dict[str, Any]) -> Any:
    """Invokes async LLM completion natively."""
    res = func(**kwargs)
    if inspect.isawaitable(res):
        return await res
    return res



async def _mask_text_with_cache(
    text: str,
    masker: Any,
    active_mapping: dict[str, str],
    masked_text_cache: MaskedTextCache | None,
) -> str:
    """Masks outbound text while reusing per-turn cached outputs for unchanged messages."""
    if not text:
        return text
    cache_key: str | None = None
    if masked_text_cache is not None:
        has_unmapped_literal_placeholder = any(
            m.group(0) not in active_mapping for m in _LITERAL_PLACEHOLDER_RE.finditer(text)
        )
        if not has_unmapped_literal_placeholder:
            ner_flag = int(bool(getattr(masker, "ner_enabled", False)))
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            cache_key = f"{id(masker)}:{ner_flag}:{digest}"
            cached = masked_text_cache.get(cache_key)
            if cached is not None:
                cached_masked, cached_tokens = cached
                if cached_tokens:
                    active_mapping.update(cached_tokens)
                return cached_masked

    masked_text, mapping = await masker.async_mask(text, existing_mapping=active_mapping)
    active_mapping.update(mapping)
    if cache_key is not None and masked_text_cache is not None:
        referenced_tokens = {k: v for k, v in mapping.items() if k in masked_text}
        masked_text_cache[cache_key] = (masked_text, referenced_tokens)
    return str(masked_text)


async def execute_llm_with_retry(
    func: Any,
    kwargs: dict[str, Any],
    agent_key: str,
    thread_key: tuple[str, str],
    is_superseded_fn: Callable[[], bool] | None = None,
    max_attempts: int | None = None,
    allow_503_immediate_retry: bool = True,
    turn_token_mapping: dict[str, str] | None = None,
    masked_text_cache: MaskedTextCache | None = None,
) -> Any:
    """
    Executes an LLM call natively or via runner with single-layer exponential backoff retries.
    Attempts up to `max_attempts` times (or dynamic `get_llm_max_retries()`) before raising.
    If `allow_503_immediate_retry` is False and a 503/high-demand error occurs on attempt 1,
    the exception is raised immediately to allow postponement and multi-agent rotation.
    """
    effective_max_attempts = max(1, int(max_attempts if max_attempts is not None else get_llm_max_retries()))

    if _privacy_mode_provider is not None:
        try:
            is_strict = bool(_privacy_mode_provider())
        except Exception as e:  # noqa: BLE001 - Fail closed to strict=True if privacy mode provider raises
            logger.warning(f"[LLM] Failed to read privacy mode provider, failing closed (strict=True): {e}")
            is_strict = True
    else:
        try:
            if SYSTEM_SETTINGS_FILE.exists():
                with open(SYSTEM_SETTINGS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                is_strict = bool(data.get("strict_privacy_mode_enabled", True))
            else:
                is_strict = True
        except (OSError, ValueError, TypeError) as exc:
            logger.debug(f"[LLM] Failed reading strict_privacy_mode_enabled; failing closed: {exc}")
            is_strict = True

    masker = _pii_masker_instance
    active_mapping: dict[str, str] = turn_token_mapping if turn_token_mapping is not None else {}

    # 1. Masking Phase (never mutate caller's kwargs dict)
    if is_strict and masker and ("messages" in kwargs or "input" in kwargs):
        call_kwargs = copy.deepcopy(kwargs)
        for msg in call_kwargs.get("messages", []):
            if isinstance(msg, dict) and msg.get("role") == "system":
                continue
            if "content" in msg and isinstance(msg["content"], str):
                msg["content"] = await _mask_text_with_cache(
                    msg["content"], masker, active_mapping, masked_text_cache
                )
            elif "content" in msg and isinstance(msg["content"], list):
                for part in msg["content"]:
                    if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                        part["text"] = await _mask_text_with_cache(
                            part["text"], masker, active_mapping, masked_text_cache
                        )
            if "tool_calls" in msg and isinstance(msg["tool_calls"], list):
                for tc in msg["tool_calls"]:
                    fn = tc.get("function") if isinstance(tc, dict) else getattr(tc, "function", None)
                    if fn:
                        args = fn.get("arguments") if isinstance(fn, dict) else getattr(fn, "arguments", None)
                        if isinstance(args, str):
                            masked_args = await _mask_text_with_cache(
                                args, masker, active_mapping, masked_text_cache
                            )
                            if isinstance(fn, dict):
                                fn["arguments"] = masked_args
                            else:
                                fn.arguments = masked_args

        if "input" in call_kwargs:
            raw_input = call_kwargs["input"]
            if isinstance(raw_input, str):
                call_kwargs["input"] = await _mask_text_with_cache(
                    raw_input, masker, active_mapping, masked_text_cache
                )
            elif isinstance(raw_input, list):
                masked_items: list[Any] = []
                for item in raw_input:
                    if isinstance(item, str):
                        masked_items.append(
                            await _mask_text_with_cache(
                                item, masker, active_mapping, masked_text_cache
                            )
                        )
                    else:
                        masked_items.append(item)
                call_kwargs["input"] = masked_items
    else:
        call_kwargs = dict(kwargs)

    if turn_token_mapping is not None and active_mapping:
        turn_token_mapping.update(active_mapping)

    if "messages" in call_kwargs:
        logger.debug(f"[LLM] Prepared prompt for {agent_key}: {call_kwargs.get('messages', [])}")
    elif "input" in call_kwargs:
        input_val = call_kwargs["input"]
        count = len(input_val) if isinstance(input_val, list) else 1
        logger.debug(f"[LLM] Prepared embedding input ({count} item(s)) for {agent_key}")

    # Enforce single retry layer and bounded per-call timeout
    call_kwargs["num_retries"] = 0
    call_kwargs.setdefault("timeout", 120.0)

    if _llm_completion_fn is None and _default_llm_guard is not None and _is_unmocked_litellm_callable(func):
        guard_res = _default_llm_guard(agent_key=agent_key, kwargs=call_kwargs)
        if inspect.isawaitable(guard_res):
            await guard_res

    last_error: Exception | None = None
    for attempt in range(1, effective_max_attempts + 1):
        if is_superseded_fn and is_superseded_fn():
            logger.info(
                f"[LLM] Thread {thread_key} turn was superseded before LLM attempt {attempt}. Aborting."
            )
            return None

        try:
            if attempt > 1:
                logger.info(
                    f"[LLM] Retrying LLM completion for {agent_key} (attempt {attempt}/{effective_max_attempts})..."
                )
            active_func = _llm_completion_fn or func or _get_litellm().acompletion
            res = await _invoke_llm(active_func, call_kwargs)

            # 2. Unmasking Phase (keep tool_calls arguments and intermediate tool-calling messages
            # masked so ToolLoopRunner enforces per-tool outbound PII policy and checkpoints stay safe)
            if is_strict and active_mapping and hasattr(res, "choices") and res.choices and masker:
                first_choice = res.choices[0]
                if hasattr(first_choice, "message"):
                    has_tool_calls = bool(getattr(first_choice.message, "tool_calls", None))
                    if (
                        not has_tool_calls
                        and hasattr(first_choice.message, "content")
                        and isinstance(first_choice.message.content, str)
                    ):
                        unmasked_content = masker.unmask(first_choice.message.content, active_mapping)
                        first_choice.message.content = unmasked_content

            if turn_token_mapping is not None:
                turn_token_mapping.update(active_mapping)

            return res

        except asyncio.CancelledError:
            raise
        except (TimeoutError, asyncio.TimeoutError) as exc:
            last_error = exc
            logger.warning(
                f"[LLM] Attempt {attempt}/{effective_max_attempts} timed out for {agent_key}: {exc}"
            )
            if not allow_503_immediate_retry:
                logger.info(
                    f"[LLM] Timeout detected on first encounter for {agent_key}. Skipping immediate retry."
                )
                raise
            sleep_call = _llm_sleep_fn or asyncio.sleep
            if attempt < effective_max_attempts:
                await sleep_call(_calculate_backoff_delay(attempt))
            else:
                raise last_error
        except Exception as exc:
            last_error = exc
            logger.warning(
                f"[LLM] Attempt {attempt}/{effective_max_attempts} failed for {agent_key}: {exc}"
            )
            err_class = classify_agent_error(exc)
            if err_class in ("api_key_not_found", "model_not_found", "bad_request"):
                logger.info(
                    f"[LLM] Non-retryable error ({err_class}) detected for {agent_key}. Skipping retries."
                )
                raise
            if not allow_503_immediate_retry and err_class == "service_unavailable":
                logger.info(
                    f"[LLM] 503 / High demand spike detected on first encounter for {agent_key}. Skipping immediate retry."
                )
                raise

            sleep_call = _llm_sleep_fn or asyncio.sleep
            if attempt < effective_max_attempts:
                await sleep_call(_calculate_backoff_delay(attempt))
            else:
                raise last_error


