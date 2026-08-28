import logging
from typing import Any

logger = logging.getLogger(__name__)


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


def extract_openai_cache_usage(response_or_usage: Any) -> dict[str, int]:
    """
    Extracts cached_tokens (read) and cache_write_tokens (write) for an OpenAI model.
    If cache_write_tokens is not explicitly returned by the API/LiteLLM,
    it is calculated from prompt_tokens (OpenAI automatically caches prompts >= 1024 tokens).
    Returns {"cached_tokens": int, "cache_write_tokens": int}.
    """
    if not response_or_usage:
        return {"cached_tokens": 0, "cache_write_tokens": 0}
    if isinstance(response_or_usage, dict) and "usage" in response_or_usage:
        usage = response_or_usage["usage"]
    else:
        usage = getattr(response_or_usage, "usage", response_or_usage)
    if not usage:
        return {"cached_tokens": 0, "cache_write_tokens": 0}

    def parse_val(v: Any) -> int | None:
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str) and v.strip().isdigit():
            return int(v.strip())
        return None

    cached_tokens = 0
    explicit_write = None

    # 1. Check prompt_tokens_details
    details = None
    if isinstance(usage, dict):
        details = usage.get("prompt_tokens_details")
    else:
        details = getattr(usage, "prompt_tokens_details", None)

    if details is not None:
        if isinstance(details, dict):
            c_read = parse_val(details.get("cached_tokens"))
            if c_read is not None:
                cached_tokens = c_read
            c_write = parse_val(
                details.get("cache_write_tokens")
                or details.get("cache_creation_tokens")
            )
            if c_write is not None:
                explicit_write = c_write
        else:
            c_read = parse_val(getattr(details, "cached_tokens", None))
            if c_read is not None:
                cached_tokens = c_read
            c_write = parse_val(
                getattr(details, "cache_write_tokens", None)
                or getattr(details, "cache_creation_tokens", None)
            )
            if c_write is not None:
                explicit_write = c_write

    # 2. Check direct cached_tokens / cache_write_tokens on usage
    if isinstance(usage, dict):
        if not cached_tokens:
            c_read = parse_val(usage.get("cached_tokens"))
            if c_read is not None:
                cached_tokens = c_read
        if explicit_write is None:
            c_write = parse_val(
                usage.get("cache_write_tokens")
                or usage.get("cache_creation_tokens")
                or usage.get("cache_creation_input_tokens")
            )
            if c_write is not None:
                explicit_write = c_write
        p_tokens = parse_val(usage.get("prompt_tokens"))
    else:
        if not cached_tokens:
            c_read = parse_val(getattr(usage, "cached_tokens", None))
            if c_read is not None:
                cached_tokens = c_read
        if explicit_write is None:
            c_write = parse_val(
                getattr(usage, "cache_write_tokens", None)
                or getattr(usage, "cache_creation_tokens", None)
                or getattr(usage, "cache_creation_input_tokens", None)
            )
            if c_write is not None:
                explicit_write = c_write
        p_tokens = parse_val(getattr(usage, "prompt_tokens", None))

    if explicit_write is not None:
        cache_write_tokens = explicit_write
    elif p_tokens is not None and p_tokens >= 1024:
        cache_write_tokens = max(0, p_tokens - cached_tokens)
    else:
        cache_write_tokens = 0

    return {"cached_tokens": cached_tokens, "cache_write_tokens": cache_write_tokens}


def extract_cached_tokens(response_or_usage: Any) -> int:
    """Extracts cached_tokens count from an LLM response or usage object/dict."""
    return extract_openai_cache_usage(response_or_usage)["cached_tokens"]


def log_openai_cached_tokens(
    model: str | None,
    response: Any,
    caller_info: str = "",
    custom_logger: logging.Logger | None = None,
) -> int:
    """
    Logs cached_tokens (read) and cache_write_tokens (write) if the model targets OpenAI.
    Returns the extracted cached token count (0 if not an OpenAI model or none found).
    """
    if not is_openai_model(model):
        return 0
    usage = extract_openai_cache_usage(response)
    cached_tokens = usage["cached_tokens"]
    cache_write_tokens = usage["cache_write_tokens"]
    log = custom_logger or logger
    caller_str = f" for {caller_info}" if caller_info else ""
    log.info(
        f"[OPENAI] cached_tokens: {cached_tokens} | "
        f"cache_write_tokens: {cache_write_tokens} (model: {model}{caller_str})"
    )
    return cached_tokens


def extract_anthropic_cache_usage(response_or_usage: Any) -> dict[str, int]:
    """
    Extracts cache_read and cache_creation token counts from an Anthropic response or usage object/dict.
    Returns {"cache_read": int, "cache_creation": int}.
    """
    if not response_or_usage:
        return {"cache_read": 0, "cache_creation": 0}
    if isinstance(response_or_usage, dict) and "usage" in response_or_usage:
        usage = response_or_usage["usage"]
    else:
        usage = getattr(response_or_usage, "usage", response_or_usage)
    if not usage:
        return {"cache_read": 0, "cache_creation": 0}

    def parse_val(v: Any) -> int | None:
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str) and v.strip().isdigit():
            return int(v.strip())
        return None

    cache_read = 0
    cache_creation = 0

    # 1. Check direct attributes or keys on usage (native Anthropic & LiteLLM usage fields)
    if isinstance(usage, dict):
        r_val = parse_val(usage.get("cache_read_input_tokens"))
        c_val = parse_val(usage.get("cache_creation_input_tokens"))
    else:
        r_val = parse_val(getattr(usage, "cache_read_input_tokens", None))
        c_val = parse_val(getattr(usage, "cache_creation_input_tokens", None))

    if r_val is not None:
        cache_read = r_val
    if c_val is not None:
        cache_creation = c_val

    # 2. Check prompt_tokens_details
    details = None
    if isinstance(usage, dict):
        details = usage.get("prompt_tokens_details")
    else:
        details = getattr(usage, "prompt_tokens_details", None)

    if details is not None:
        if isinstance(details, dict):
            if r_val is None:
                d_read = parse_val(details.get("cached_tokens"))
                if d_read is not None:
                    cache_read = d_read
            if c_val is None:
                d_create = parse_val(
                    details.get("cache_creation_tokens")
                    or details.get("cache_write_tokens")
                )
                if d_create is not None:
                    cache_creation = d_create
        else:
            if r_val is None:
                d_read = parse_val(getattr(details, "cached_tokens", None))
                if d_read is not None:
                    cache_read = d_read
            if c_val is None:
                d_create = parse_val(
                    getattr(details, "cache_creation_tokens", None)
                    or getattr(details, "cache_write_tokens", None)
                )
                if d_create is not None:
                    cache_creation = d_create

    return {"cache_read": cache_read, "cache_creation": cache_creation}


def log_anthropic_cache_usage(
    model: str | None,
    response: Any,
    caller_info: str = "",
    custom_logger: logging.Logger | None = None,
) -> dict[str, int]:
    """
    Logs Anthropic cache read and creation tokens if the model targets Anthropic.
    Returns dict with {"cache_read": int, "cache_creation": int}.
    """
    if not is_anthropic_model(model):
        return {"cache_read": 0, "cache_creation": 0}
    usage = extract_anthropic_cache_usage(response)
    log = custom_logger or logger
    caller_str = f" for {caller_info}" if caller_info else ""
    log.info(
        f"[ANTHROPIC] cache_read: {usage['cache_read']} | "
        f"cache_creation: {usage['cache_creation']} (model: {model}{caller_str})"
    )
    return usage


def log_llm_cache_usage(
    model: str | None,
    response: Any,
    caller_info: str = "",
    custom_logger: logging.Logger | None = None,
) -> None:
    """Logs caching usage for any supported provider (OpenAI, Anthropic)."""
    if is_openai_model(model):
        log_openai_cached_tokens(model, response, caller_info, custom_logger)
    elif is_anthropic_model(model):
        log_anthropic_cache_usage(model, response, caller_info, custom_logger)
