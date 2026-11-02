"""
Log record filters for redacting credentials, secrets, and API tokens
from application log messages and arguments.
"""

from __future__ import annotations

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
    # Bearer HTTP tokens
    (
        re.compile(r"Bearer\s+[a-zA-Z0-9_\-\.]{20,}", re.IGNORECASE),
        "Bearer [REDACTED_TOKEN]",
    ),
    # Common secret assignments: password=..., api_key=..., client_secret=...
    (
        re.compile(
            r"(?i)\b([A-Za-z0-9_]*(?:password|passwd|secret|api_key|access_token|client_secret|auth_token))\s*([:=])\s*['\"]?(?!\[REDACTED_)([^\s'\"]{6,})['\"]?"
        ),
        r"\1\2 [REDACTED_SECRET]",
    ),
]


def redact_text(text: str) -> str:
    """Sanitizes text by replacing sensitive credentials and tokens with redaction tags."""
    if not text or not isinstance(text, str):
        return text

    sanitized = text
    for pattern, replacement in _CREDENTIAL_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)

    # Dynamically redact sensitive environment variable values if configured
    env_secrets = [
        os.getenv("SLACK_BOT_TOKEN"),
        os.getenv("SLACK_SIGNING_SECRET"),
        os.getenv("ADMIN_PASSWORD"),
        os.getenv("OPENAI_API_KEY"),
        os.getenv("ANTHROPIC_API_KEY"),
        os.getenv("GEMINI_API_KEY"),
        os.getenv("MISTRAL_API_KEY"),
        os.getenv("GROQ_API_KEY"),
        os.getenv("META_API_KEY"),
        os.getenv("DEEPSEEK_API_KEY"),
        os.getenv("OPENROUTER_API_KEY"),
        os.getenv("COHERE_API_KEY"),
        os.getenv("PERPLEXITY_API_KEY"),
        os.getenv("TOGETHER_API_KEY"),
        os.getenv("AUDIT_HMAC_SECRET"),
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


def setup_credential_redaction_filters() -> None:
    """
    Attaches CredentialRedactionFilter to the root logger and all existing handlers
    to guarantee no raw credentials or API keys leak into console or log files.
    """
    redaction_filter = CredentialRedactionFilter()
    root_logger = logging.getLogger()

    # Avoid duplicate filters
    if not any(isinstance(f, CredentialRedactionFilter) for f in root_logger.filters):
        root_logger.addFilter(redaction_filter)

    for handler in root_logger.handlers:
        if not any(isinstance(f, CredentialRedactionFilter) for f in handler.filters):
            handler.addFilter(redaction_filter)
