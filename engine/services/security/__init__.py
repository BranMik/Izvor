import json
import logging
import re

from core.paths import SYSTEM_SETTINGS_FILE
from services.audit_logger import log_audit_event
from services.security.guardrail import GuardrailService, guardrail_service
from services.security.pii_masker import PIIMasker, extract_skill_exclusion_terms, pii_masker

logger = logging.getLogger(__name__)

# Hardcoded main patterns
MAIN_PATTERNS = {
    "slack_token": r"xox[baprs]-[a-zA-Z0-9\-]+",
    "stripe_sk_live": r"sk_live_[a-zA-Z0-9]+",
    "aws_access_key_id": r"AKIA[0-9A-Z]{16}",
    "internal_system_prompt_leak": r"OUTPUT & LENGTH CONSTRAINTS:",
    "aws_secret_key": r"(?i)aws_secret_access_key\s*=\s*[a-zA-Z0-9/+=]{40}",
    "openai_api_key": r"sk-(?:proj-)?[a-zA-Z0-9_\-]{20,}",
    "anthropic_api_key": r"sk-ant-[a-zA-Z0-9_\-]{20,}",
    "google_api_key": r"AIzaSy[a-zA-Z0-9_\-]{33}",
    "github_token": r"gh[pousr]_[a-zA-Z0-9]{36,}",
}

def load_additional_patterns() -> dict[str, str]:
    if SYSTEM_SETTINGS_FILE.exists():
        try:
            with open(SYSTEM_SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("security_guardrail_patterns", {})
        except (OSError, json.JSONDecodeError, ValueError) as e:
            logger.error(f"Error reading {SYSTEM_SETTINGS_FILE}: {e}")
    return {}

def scan_output_for_secrets(text: str, user_id: str = "agent") -> str | None:
    """
    Scans the text for secrets. If found, logs to audit and returns the matched rule name.
    If no secrets found, returns None.
    """
    if not text:
        return None

    patterns = dict(MAIN_PATTERNS)
    patterns.update(load_additional_patterns())

    for rule_name, pattern in patterns.items():
        if re.search(pattern, text, re.IGNORECASE):
            log_audit_event(
                event_type="SECURITY_GUARDRAIL_BLOCKED",
                actor={"type": "system", "id": user_id},
                action="block_message",
                target="slack_output",
                details={"matched_rule": rule_name},
                severity="CRITICAL",
                status="BLOCKED"
            )
            return rule_name

    return None

__all__ = [
    "GuardrailService",
    "guardrail_service",
    "scan_output_for_secrets",
    "MAIN_PATTERNS",
    "load_additional_patterns",
    "PIIMasker",
    "pii_masker",
    "extract_skill_exclusion_terms",
]
