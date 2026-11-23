"""
Log record filters for redacting credentials, secrets, and API tokens
from application log messages and arguments.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
import os
import re
from typing import Any

# Regex patterns matching high-sensitivity tokens and API keys
_CREDENTIAL_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Private Key blocks
    (
        re.compile(
            r"-----BEGIN [A-Z ]+PRIVATE KEY-----[\s\S]*?-----END [A-Z ]+PRIVATE KEY-----",
            re.MULTILINE,
        ),
        "[REDACTED_PRIVATE_KEY]",
    ),
    # Slack Bot / User / App / Refresh tokens
    (
        re.compile(r"\bxox[baprs]-[0-9a-zA-Z-]{10,}\b"),
        "[REDACTED_SLACK_TOKEN]",
    ),
    # Anthropic API Keys
    (
        re.compile(r"\bsk-ant-[a-zA-Z0-9_-]{20,}\b"),
        "[REDACTED_API_KEY]",
    ),
    # OpenAI & generic sk- tokens
    (
        re.compile(r"\bsk-(?:proj-|live-)?[a-zA-Z0-9_-]{20,}\b"),
        "[REDACTED_API_KEY]",
    ),
    # Groq API Keys
    (
        re.compile(r"\bgsk_[a-zA-Z0-9_-]{20,}\b"),
        "[REDACTED_API_KEY]",
    ),
    # Google API Keys
    (
        re.compile(r"\bAIza[0-9A-Za-z-_]{35}\b"),
        "[REDACTED_API_KEY]",
    ),
    # AWS Access Key ID
    (
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "[REDACTED_AWS_KEY]",
    ),
    # Bearer and Basic HTTP tokens
    (
        re.compile(r"Bearer\s+[a-zA-Z0-9_\-\.]{20,}", re.IGNORECASE),
        "Bearer [REDACTED_TOKEN]",
    ),
    (
        re.compile(r"\bBasic\s+[A-Za-z0-9+/=]{8,}", re.IGNORECASE),
        "Basic [REDACTED_TOKEN]",
    ),
    # Common secret assignments: password=..., api_key=..., client_secret=...
    (
        re.compile(
            r"(?i)\b([A-Za-z0-9_]*(?:password|passwd|secret|api_key|access_token|client_secret|auth_token))\s*([:=])\s*['\"]?(?!\[REDACTED_)([^\s'\"]{6,})['\"]?"
        ),
        r"\1\2 [REDACTED_SECRET]",
    ),
]


def redact_text(text: str, env: Mapping[str, str] | None = None) -> str:
    """Sanitizes text by replacing sensitive credentials and tokens with redaction tags."""
    if not text or not isinstance(text, str):
        return text

    sanitized = text
    for pattern, replacement in _CREDENTIAL_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)

    # Dynamically redact sensitive environment variable values if configured
    active_env = env if env is not None else os.environ
    env_secrets = [
        active_env.get("SLACK_BOT_TOKEN"),
        active_env.get("SLACK_SIGNING_SECRET"),
        active_env.get("ADMIN_PASSWORD"),
        active_env.get("OPENAI_API_KEY"),
        active_env.get("ANTHROPIC_API_KEY"),
        active_env.get("GEMINI_API_KEY"),
        active_env.get("MISTRAL_API_KEY"),
        active_env.get("GROQ_API_KEY"),
        active_env.get("META_API_KEY"),
        active_env.get("DEEPSEEK_API_KEY"),
        active_env.get("OPENROUTER_API_KEY"),
        active_env.get("COHERE_API_KEY"),
        active_env.get("PERPLEXITY_API_KEY"),
        active_env.get("TOGETHER_API_KEY"),
        active_env.get("AUDIT_HMAC_SECRET"),
    ]
    for secret in env_secrets:
        if secret and len(secret) > 5 and secret in sanitized:
            sanitized = sanitized.replace(secret, "[REDACTED_ENV_SECRET]")

    return sanitized


def _sanitize_obj(obj: Any) -> Any:
    """Recursively sanitizes strings within containers (tuple, list, dict)."""
    if isinstance(obj, str):
        return redact_text(obj)
    elif isinstance(obj, tuple):
        return tuple(_sanitize_obj(item) for item in obj)
    elif isinstance(obj, list):
        return [_sanitize_obj(item) for item in obj]
    elif isinstance(obj, dict):
        return {k: _sanitize_obj(v) for k, v in obj.items()}
    return obj


class CredentialRedactionFilter(logging.Filter):
    """
    Logging filter that intercepts log records and scrubs any API tokens,
    Slack secrets, passwords, and private keys from the message, arguments,
    and exception tracebacks.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args and isinstance(record.msg, str):
            try:
                formatted = record.msg % record.args
                record.msg = redact_text(formatted)
                record.args = ()
            except (TypeError, ValueError):
                record.msg = redact_text(record.msg)
                record.args = _sanitize_obj(record.args)
        else:
            if record.msg is not None:
                if isinstance(record.msg, str):
                    record.msg = redact_text(record.msg)
                elif isinstance(record.msg, (dict, list, tuple)):
                    record.msg = _sanitize_obj(record.msg)
                else:
                    record.msg = redact_text(str(record.msg))
            if record.args:
                record.args = _sanitize_obj(record.args)
        if record.exc_info and not record.exc_text:
            try:
                record.exc_text = redact_text(logging.Formatter().formatException(record.exc_info))
            except Exception:  # noqa: BLE001
                record.exc_text = "[Failed to format exception traceback]"
        elif record.exc_text:
            record.exc_text = redact_text(record.exc_text)
        if getattr(record, "stack_text", None):
            record.stack_text = redact_text(record.stack_text)
        return True


def _attach_filter_to_logger(target_logger: logging.Logger, redaction_filter: CredentialRedactionFilter) -> None:
    if not any(isinstance(f, CredentialRedactionFilter) for f in target_logger.filters):
        target_logger.addFilter(redaction_filter)
    for handler in target_logger.handlers:
        if not any(isinstance(f, CredentialRedactionFilter) for f in handler.filters):
            handler.addFilter(redaction_filter)


def setup_credential_redaction_filters() -> None:
    """
    Attaches CredentialRedactionFilter to the root logger, third-party loggers with
    propagate=False (uvicorn*, httpx), and all of their existing handlers.
    """
    redaction_filter = CredentialRedactionFilter()
    _attach_filter_to_logger(logging.getLogger(), redaction_filter)
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx"):
        _attach_filter_to_logger(logging.getLogger(logger_name), redaction_filter)
