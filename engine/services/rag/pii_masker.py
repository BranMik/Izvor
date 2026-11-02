import logging
import re
import threading
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
        re.compile(r"https://hooks\.slack\.com/services/T[a-zA-Z0-9_]+/B[a-zA-Z0-9_]+/[a-zA-Z0-9_]+"),
        "<SLACK_WEBHOOK>",
    ),
    # Slack Bot / User / App tokens
    (
        re.compile(r"\bxox[baprs]-[0-9]{10,13}-[0-9]{10,13}-[a-zA-Z0-9]{24,34}\b"),
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
        re.compile(r"\beyJ[a-zA-Z0-9_-]{10,}\.eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\b"),
        "<JWT_TOKEN>",
    ),
    # Broad key-value secret configurations (e.g. MISTRAL_API_KEY=..., GROQ_API_KEY=..., password: xyz, api_secret="abc")
    (
        re.compile(
            r"(?i)\b([A-Za-z0-9_]*(?:password|passwd|secret|api_key|access_token|client_secret|auth_token))\s*([:=])\s*['\"]?([^\s'\"]{4,})['\"]?"
        ),
        r"\1\2 <REDACTED_SECRET>",
    ),
    # Hex hashes/tokens preceded by secret/token/hash context (avoiding bare commit SHAs or digests)
    (
        re.compile(r"(?i)\b([A-Za-z0-9_]*(?:token|secret|hash|salt|seed))\s*([:=])\s*['\"]?([a-f0-9]{32,64})['\"]?"),
        r"\1\2 <TOKEN_HASH>",
    ),
    (
        re.compile(r"(?i)\b(?:sk[_-]|key[_-]|token[_-]|secret[_-])[a-f0-9]{24,64}\b"),
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
        re.compile(r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
        "<PHONE>",
    ),
    # IPv4 addresses
    (
        re.compile(r"\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b"),
        "<IP_ADDRESS>",
    ),
]


class PIIMasker:
    """
    Sanitizes documents and search results to prevent PII and secrets
    from reaching LLMs or vector indices. Supports two-way tokenization.
    """

    def __init__(self) -> None:
        self._presidio_analyzer: Any = None
        self._presidio_initialized = False
        self._init_lock = threading.Lock()

    def _init_presidio_if_available(self) -> None:
        if self._presidio_initialized:
            return
        with self._init_lock:
            if self._presidio_initialized:
                return
            try:
                from presidio_analyzer import AnalyzerEngine
                from presidio_analyzer.nlp_engine import NlpEngineProvider

                # Explicitly load the lightweight en_core_web_sm model
                provider = NlpEngineProvider(
                    nlp_configuration={
                        "nlp_engine_name": "spacy",
                        "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
                    }
                )
                nlp_engine = provider.create_engine()
                self._presidio_analyzer = AnalyzerEngine(nlp_engine=nlp_engine)
                logger.info("[PII Masker] Microsoft Presidio (en_core_web_sm) successfully initialized.")
            except ImportError:
                logger.debug("[PII Masker] Presidio not installed; using built-in high-performance regex engine.")
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[PII Masker] Could not initialize Presidio: {e}")
            finally:
                self._presidio_initialized = True

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

    def mask(
        self,
        text: str,
        existing_mapping: dict[str, str] | None = None,
    ) -> tuple[str, dict[str, str]]:
        """
        Masks secrets, credentials, and PII in the given text.
        Applies deterministic pattern scrubbers first, followed by Presidio NER (if installed).
        Returns a tuple of (masked_text, token_mapping).
        Accepts optional existing_mapping to continue indexing without token collisions.
        """
        if not text or not isinstance(text, str):
            return text or "", dict(existing_mapping or {})

        masked_text = text
        token_mapping: dict[str, str] = dict(existing_mapping or {})
        counts: dict[str, int] = {}

        if existing_mapping:
            for token_key in existing_mapping:
                m = re.match(r"^<([A-Za-z0-9_]+)_(\d+)>$", token_key)
                if m:
                    base_type, idx_str = m.group(1), m.group(2)
                    counts[base_type] = max(counts.get(base_type, 0), int(idx_str) + 1)

        def get_replacer(base_token: str):
            base_token_bare = base_token.strip("<>")

            def replacer(match: re.Match) -> str:
                original = match.group(0)

                if base_token in (r"\1\2 <REDACTED_SECRET>", r"\1\2 <TOKEN_HASH>") and len(match.groups()) == 3:
                    prefix = match.group(1) + match.group(2) + " "
                    secret_val = match.group(3)
                    token_name = "TOKEN_HASH" if "TOKEN_HASH" in base_token else "REDACTED_SECRET"

                    for t, v in token_mapping.items():
                        if v == secret_val and t.startswith(f"<{token_name}_"):
                            return prefix + t

                    idx = counts.get(token_name, 0)
                    counts[token_name] = idx + 1
                    token_key = f"<{token_name}_{idx}>"
                    token_mapping[token_key] = secret_val
                    return prefix + token_key

                # Deduplicate exact matches
                for t, v in token_mapping.items():
                    if v == original and t.startswith(f"<{base_token_bare}_"):
                        return t

                idx = counts.get(base_token_bare, 0)
                counts[base_token_bare] = idx + 1
                token = f"<{base_token_bare}_{idx}>"
                token_mapping[token] = original
                return token

            return replacer

        # 1. Mask deterministic secrets and credentials
        for pattern, replacement in _SECRET_PATTERNS:
            masked_text = pattern.sub(get_replacer(replacement), masked_text)

        # 2. Mask deterministic PII (emails, phones, SSNs, credit cards, IPs)
        for pattern, replacement in _PII_PATTERNS:
            masked_text = pattern.sub(get_replacer(replacement), masked_text)

        # 3. Presidio enhancement (for NER: PERSON, LOCATION, ORGANIZATION)
        self._init_presidio_if_available()
        if self._presidio_analyzer:
            try:
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

                    def custom_anonymize(res) -> str:
                        entity_type = res.entity_type
                        original_str = masked_text[res.start : res.end]
                        for t, v in token_mapping.items():
                            if v == original_str and t.startswith(f"<{entity_type}_"):
                                return t
                        idx = counts.get(entity_type, 0)
                        counts[entity_type] = idx + 1
                        token = f"<{entity_type}_{idx}>"
                        token_mapping[token] = original_str
                        return token

                    # Deduplicate overlapping spans (keep longest/earliest)
                    sorted_spans = sorted(results, key=lambda x: (x.start, -(x.end - x.start)))
                    non_overlapping = []
                    last_end = -1
                    for r in sorted_spans:
                        if r.start >= last_end:
                            non_overlapping.append(r)
                            last_end = r.end

                    # Sort non-overlapping results descending by start index to replace from end to start
                    non_overlapping.sort(key=lambda x: x.start, reverse=True)
                    for res in non_overlapping:
                        replacement = custom_anonymize(res)
                        masked_text = masked_text[: res.start] + replacement + masked_text[res.end :]

            except Exception as e:  # noqa: BLE001
                logger.debug(f"[PII Masker] Presidio NER processing error: {e}")

        return masked_text, token_mapping

    async def async_mask(self, text: str, existing_mapping: dict[str, str] | None = None) -> tuple[str, dict[str, str]]:
        """
        Asynchronously masks PII in text by offloading Presidio CPU-bound NER to a worker thread.
        """
        import asyncio

        self._init_presidio_if_available()
        if self._presidio_analyzer is not None:
            return await asyncio.to_thread(self.mask, text, existing_mapping)
        return self.mask(text, existing_mapping)

    def unmask(self, text: str, token_mapping: dict[str, str]) -> str:
        """
        Restores tokenized PII back to original values in the text using a single-pass compiled regex.
        """
        if not text or not token_mapping:
            return text

        pattern = re.compile("|".join(re.escape(k) for k in sorted(token_mapping.keys(), key=len, reverse=True)))
        return pattern.sub(lambda m: token_mapping[m.group(0)], text)


# Global singleton instance
pii_masker = PIIMasker()
