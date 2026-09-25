import logging
import re
from typing import Any

from .config import get_pii_masking_mode

logger = logging.getLogger(__name__)

# Precompiled regex patterns for deterministic secret and PII detection
_SECRET_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Private Key blocks
    (
        re.compile(
            r"-----BEGIN [A-Z ]+PRIVATE KEY-----[\s\S]*?-----END [A-Z ]+PRIVATE KEY-----",
            re.MULTILINE,
        ),
        "<PRIVATE_KEY>",
    ),
    # Slack Webhooks
    (
        re.compile(
            r"https://hooks\.slack\.com/services/T[a-zA-Z0-9_]+/B[a-zA-Z0-9_]+/[a-zA-Z0-9_]+"
        ),
        "<SLACK_WEBHOOK>",
    ),
    # Slack Bot / User / App tokens
    (
        re.compile(
            r"\bxox[baprs]-[0-9]{10,13}-[0-9]{10,13}-[a-zA-Z0-9]{24,34}\b"
        ),
        "<SLACK_TOKEN>",
    ),
    # OpenAI & generic sk- tokens
    (
        re.compile(r"\bsk-(?:proj-|live-)?[a-zA-Z0-9_-]{20,}\b"),
        "<API_KEY>",
    ),
    # Anthropic API Keys
    (
        re.compile(r"\bsk-ant-[a-zA-Z0-9_-]{20,}\b"),
        "<API_KEY>",
    ),
    # Groq API Keys
    (
        re.compile(r"\bgsk_[a-zA-Z0-9_-]{20,}\b"),
        "<API_KEY>",
    ),
    # Google API Keys
    (
        re.compile(r"\bAIza[0-9A-Za-z-_]{35}\b"),
        "<API_KEY>",
    ),
    # AWS Access Key ID
    (
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "<AWS_ACCESS_KEY>",
    ),
    # JWT tokens
    (
        re.compile(
            r"\beyJ[a-zA-Z0-9_-]{10,}\.eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\b"
        ),
        "<JWT_TOKEN>",
    ),
    # Broad key-value secret configurations (e.g. MISTRAL_API_KEY=..., GROQ_API_KEY=..., password: xyz, api_secret="abc")
    (
        re.compile(
            r"(?i)\b([A-Za-z0-9_]*(?:password|passwd|secret|api_key|access_token|client_secret|auth_token))\s*([:=])\s*['\"]?([^\s'\"]{4,})['\"]?"
        ),
        r"\1\2 <REDACTED_SECRET>",
    ),
    # Standalone 32-64 char hex hashes/tokens (MD5/SHA/API keys)
    (
        re.compile(r"\b[a-f0-9]{32,64}\b", re.IGNORECASE),
        "<TOKEN_HASH>",
    ),
]

_PII_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Email addresses
    (
        re.compile(r"\b[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+\b"),
        "<EMAIL>",
    ),
    # US Social Security Numbers
    (
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        "<SSN>",
    ),
    # Credit Card Numbers (13-19 digits, optionally spaced or hyphenated)
    (
        re.compile(r"\b(?:\d{4}[-\s]?){3}\d{4}\b"),
        "<CREDIT_CARD>",
    ),
    # Phone numbers (US and international formats)
    (
        re.compile(
            r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"
        ),
        "<PHONE>",
    ),
    # IPv4 addresses
    (
        re.compile(
            r"\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b"
        ),
        "<IP_ADDRESS>",
    ),
]


class PIIMasker:
    """
    Sanitizes documents and search results to prevent PII and secrets
    from reaching LLMs or vector indices.
    """

    def __init__(self) -> None:
        self._presidio_analyzer: Any = None
        self._presidio_anonymizer: Any = None
        self._presidio_initialized = False

    def _init_presidio_if_available(self) -> None:
        if self._presidio_initialized:
            return
        self._presidio_initialized = True
        try:
            from presidio_analyzer import AnalyzerEngine
            from presidio_analyzer.nlp_engine import NlpEngineProvider
            from presidio_anonymizer import AnonymizerEngine

            # Explicitly load the lightweight en_core_web_sm model
            provider = NlpEngineProvider(
                nlp_configuration={
                    "nlp_engine_name": "spacy",
                    "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
                }
            )
            nlp_engine = provider.create_engine()
            self._presidio_analyzer = AnalyzerEngine(nlp_engine=nlp_engine)
            self._presidio_anonymizer = AnonymizerEngine()
            logger.info("[PII Masker] Microsoft Presidio (en_core_web_sm) successfully initialized.")
        except ImportError:
            logger.debug(
                "[PII Masker] Presidio not installed; using built-in high-performance regex engine."
            )
        except Exception as e:
            logger.debug(f"[PII Masker] Could not initialize Presidio: {e}")

    @property
    def mode(self) -> str:
        """Returns the current masking mode ('AT_INGESTION', 'AT_RETRIEVAL', 'NONE')."""
        return get_pii_masking_mode()

    def should_mask_at_ingestion(self) -> bool:
        """True if PII masking should happen when documents are parsed and chunked."""
        return self.mode == "AT_INGESTION"

    def should_mask_at_retrieval(self) -> bool:
        """True if PII masking should happen on search results before LLM prompt formatting."""
        return self.mode == "AT_RETRIEVAL"

    def is_enabled(self) -> bool:
        """True if any masking is configured."""
        return self.mode in {"AT_INGESTION", "AT_RETRIEVAL"}

    def mask(self, text: str) -> str:
        """
        Masks secrets, credentials, and PII in the given text.
        Applies deterministic pattern scrubbers first, followed by Presidio NER (if installed).
        """
        if not text or not isinstance(text, str):
            return text or ""

        masked_text = text

        # 1. Mask deterministic secrets and credentials
        for pattern, replacement in _SECRET_PATTERNS:
            masked_text = pattern.sub(replacement, masked_text)

        # 2. Mask deterministic PII (emails, phones, SSNs, credit cards, IPs)
        for pattern, replacement in _PII_PATTERNS:
            masked_text = pattern.sub(replacement, masked_text)

        # 3. Presidio enhancement (for NER: PERSON, LOCATION, ORGANIZATION)
        self._init_presidio_if_available()
        if self._presidio_analyzer and self._presidio_anonymizer:
            try:
                from presidio_anonymizer.entities import OperatorConfig

                results = self._presidio_analyzer.analyze(
                    text=masked_text,
                    entities=[
                        "PERSON",
                        "LOCATION",
                        "ORGANIZATION",
                        "PHONE_NUMBER",
                        "EMAIL_ADDRESS",
                        "IP_ADDRESS",
                        "US_SSN",
                        "CREDIT_CARD",
                        "IBAN_CODE",
                    ],
                    language="en",
                )
                if results:
                    anonymized = self._presidio_anonymizer.anonymize(
                        text=masked_text,
                        analyzer_results=results,
                        operators={
                            "DEFAULT": OperatorConfig("replace", {"new_value": "<REDACTED>"}),
                            "PERSON": OperatorConfig("replace", {"new_value": "<PERSON>"}),
                            "LOCATION": OperatorConfig("replace", {"new_value": "<LOCATION>"}),
                            "ORGANIZATION": OperatorConfig(
                                "replace", {"new_value": "<ORGANIZATION>"}
                            ),
                            "EMAIL_ADDRESS": OperatorConfig("replace", {"new_value": "<EMAIL>"}),
                            "PHONE_NUMBER": OperatorConfig("replace", {"new_value": "<PHONE>"}),
                            "CREDIT_CARD": OperatorConfig("replace", {"new_value": "<CREDIT_CARD>"}),
                            "US_SSN": OperatorConfig("replace", {"new_value": "<SSN>"}),
                            "IP_ADDRESS": OperatorConfig("replace", {"new_value": "<IP_ADDRESS>"}),
                            "IBAN_CODE": OperatorConfig("replace", {"new_value": "<IBAN>"}),
                        },
                    )
                    masked_text = anonymized.text
            except Exception as e:
                logger.debug(f"[PII Masker] Presidio NER processing error: {e}")

        return masked_text


# Global singleton instance
pii_masker = PIIMasker()
