import json
import logging
import re

from core.config import PROJECT_ROOT
from services.audit_logger import log_audit_event
from services.security.guardrail import GuardrailService, guardrail_service

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = PROJECT_ROOT / "system_config.json"

# Hardcoded main patterns
MAIN_PATTERNS = {
    "slack_token": r"xox[baprs]-[a-zA-Z0-9\-]+",
    "stripe_sk_live": r"sk_live_[a-zA-Z0-9]+",
    "aws_access_key_id": r"AKIA[0-9A-Z]{16}",
    "internal_system_prompt_leak": r"OUTPUT & LENGTH CONSTRAINTS:",
    "aws_secret_key": r"(?i)aws_secret_access_key\s*=\s*[a-zA-Z0-9/+=]{40}"
}

def load_additional_patterns() -> dict[str, str]:
    if DEFAULT_CONFIG_PATH.exists():
        try:
            with open(DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("security_guardrail_patterns", {})
        except (OSError, json.JSONDecodeError, ValueError) as e:
            logger.error(f"Error reading {DEFAULT_CONFIG_PATH}: {e}")
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
]
