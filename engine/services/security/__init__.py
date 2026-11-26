import json
import logging
from pathlib import Path
import re
import threading
from typing import TYPE_CHECKING, Any

import core.paths as core_paths
from services.audit_logger import log_audit_event
from services.security.pii_masker import PIIMasker, extract_skill_exclusion_terms, pii_masker

if TYPE_CHECKING:
    from services.security.guardrail import GuardrailService, guardrail_service

logger = logging.getLogger(__name__)

# Hardcoded main patterns (case-sensitive prefixes with explicit word boundaries; inline (?i) only where needed)
MAIN_PATTERNS: dict[str, str] = {
    "slack_token": r"\bxox[baprs]-[a-zA-Z0-9\-]{10,}\b",
    "stripe_sk_live": r"\bsk_live_[a-zA-Z0-9]{16,}\b",
    "aws_access_key_id": r"\bAKIA[0-9A-Z]{16}\b",
    "internal_system_prompt_leak": r"OUTPUT & LENGTH CONSTRAINTS:",
    "aws_secret_key": r"(?i)aws_secret_access_key\s*=\s*[a-zA-Z0-9/+=]{40}",
    "anthropic_api_key": r"\bsk-ant-[a-zA-Z0-9_\-]{20,}\b",
    "openai_api_key": r"\bsk-(?!ant-)(?:proj-|svcacct-)?[a-zA-Z0-9_\-]{20,}\b",
    "google_api_key": r"\bAIzaSy[a-zA-Z0-9_\-]{33}\b",
    "github_token": r"\bgh[pousr]_[a-zA-Z0-9]{36,}\b",
    "private_key_block": r"-----BEGIN [A-Z ]+PRIVATE KEY-----[\s\S]*?-----END [A-Z ]+PRIVATE KEY-----",
    "groq_api_key": r"\bgsk_[a-zA-Z0-9_\-]{20,}\b",
    "bearer_token": r"(?i)\bBearer\s+[a-zA-Z0-9_\-\.]{20,}\b",
    "basic_auth_header": r"(?i)\bAuthorization:\s*Basic\s+[A-Za-z0-9+/=]{12,}",
}

_COMPILED_MAIN_PATTERNS: dict[str, re.Pattern[str]] = {
    rule_name: re.compile(pattern) for rule_name, pattern in MAIN_PATTERNS.items()
}

_cache_lock = threading.Lock()
_cached_settings_key: tuple[str, int, int] | None = None
_cached_raw_additional: dict[str, str] = {}
_cached_compiled_additional: dict[str, re.Pattern[str]] = {}
_settings_file_override: Path | None = None


def configure_security_settings_file(path: Path | None) -> None:
    """Configures an explicit system_settings.json path override for security scanners (testing / DI)."""
    global _settings_file_override, _cached_settings_key
    with _cache_lock:
        _settings_file_override = path
        _cached_settings_key = None


def _resolve_settings_file(settings_file: Path | None = None) -> Path:
    if settings_file is not None:
        return settings_file
    if _settings_file_override is not None:
        return _settings_file_override
    return core_paths.SYSTEM_SETTINGS_FILE


def _refresh_additional_patterns_cache(settings_file: Path | None = None) -> tuple[dict[str, str], dict[str, re.Pattern[str]]]:
    global _cached_settings_key, _cached_raw_additional, _cached_compiled_additional

    target_file = _resolve_settings_file(settings_file)
    with _cache_lock:
        try:
            st = target_file.stat()
            current_key = (str(target_file), st.st_mtime_ns, st.st_size)
        except OSError:
            current_key = (str(target_file), -1, -1)

        if _cached_settings_key == current_key:
            return dict(_cached_raw_additional), dict(_cached_compiled_additional)

        raw_patterns: dict[str, str] = {}
        compiled_patterns: dict[str, re.Pattern[str]] = {}

        if current_key[1] != -1:
            try:
                with open(target_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                loaded = data.get("security_guardrail_patterns", {})
                if isinstance(loaded, dict):
                    for rule_name, pat in loaded.items():
                        if not isinstance(pat, str):
                            continue
                        raw_patterns[rule_name] = pat
                        try:
                            compiled_patterns[rule_name] = re.compile(pat)
                        except re.error as regex_err:
                            logger.error(
                                f"Skipping invalid regex pattern for security_guardrail_patterns['{rule_name}']: {regex_err}"
                            )
            except (OSError, json.JSONDecodeError, ValueError) as e:
                logger.error(f"Error reading {target_file}: {e}")

        _cached_settings_key = current_key
        _cached_raw_additional = raw_patterns
        _cached_compiled_additional = compiled_patterns
        return dict(raw_patterns), dict(compiled_patterns)


def load_additional_patterns(settings_file: Path | None = None) -> dict[str, str]:
    """Returns custom security guardrail patterns from system settings (mtime-cached)."""
    raw, _ = _refresh_additional_patterns_cache(settings_file=settings_file)
    return raw


def scan_output_for_secrets(text: str, user_id: str = "agent", settings_file: Path | None = None) -> str | None:
    """
    Scans the text for secrets using pre-compiled boundary/case-aware patterns.
    If found, logs to audit and returns the matched rule name.
    If no secrets found, returns None.
    """
    if not text:
        return None

    _, compiled_additional = _refresh_additional_patterns_cache(settings_file=settings_file)
    compiled_patterns = dict(_COMPILED_MAIN_PATTERNS)
    compiled_patterns.update(compiled_additional)

    for rule_name, compiled in compiled_patterns.items():
        if compiled.search(text):
            log_audit_event(
                event_type="SECURITY_GUARDRAIL_BLOCKED",
                actor={"type": "system", "id": user_id},
                action="block_message",
                target="slack_output",
                details={"matched_rule": rule_name},
                severity="CRITICAL",
                status="BLOCKED",
            )
            return rule_name

    return None

def __getattr__(name: str) -> Any:
    """Lazily resolves guardrail exports so security regex/PII imports avoid loading lancedb/pandas."""
    if name in ("GuardrailService", "guardrail_service"):
        from services.security import guardrail as _guardrail_mod

        return getattr(_guardrail_mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "GuardrailService",
    "guardrail_service",
    "scan_output_for_secrets",
    "MAIN_PATTERNS",
    "load_additional_patterns",
    "configure_security_settings_file",
    "PIIMasker",
    "pii_masker",
    "extract_skill_exclusion_terms",
]
