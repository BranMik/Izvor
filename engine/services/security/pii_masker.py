import json
import logging
from pathlib import Path
import re
import threading
from datetime import datetime, timezone
from typing import Any

from core.paths import PRESIDIO_SKILL_EXCLUSIONS_FILE
from orchestration.settings_store import atomic_write_json
from services.rag.config import get_pii_masking_mode

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

_STATIC_GLOBAL_ALLOWLIST_LOWER: frozenset[str] = frozenset({
    # Platform, provider & technical vocabulary
    "izvor",
    "slack",
    "ai",
    "llm",
    "llms",
    "json",
    "yaml",
    "markdown",
    "pdf",
    "api",
    "apis",
    "sql",
    "sqlite",
    "html",
    "css",
    "http",
    "https",
    "url",
    "uri",
    "cli",
    "ui",
    "ux",
    "sdk",
    "rag",
    "mcp",
    "pii",
    "rbac",
    "hitl",
    "jwt",
    "oauth",
    "sso",
    "tls",
    "ssl",
    "dns",
    "tcp",
    "udp",
    "quic",
    "ip",
    "ipv4",
    "ipv6",
    "cpu",
    "gpu",
    "ram",
    "windows",
    "linux",
    "docker",
    "git",
    "github",
    "python",
    "fastapi",
    "pydantic",
    "litellm",
    "presidio",
    "lancedb",
    "gemini",
    "claude",
    "anthropic",
    "openai",
    "mistral",
    "groq",
    "meta",
    "int",
    "int_or_off",
    "target_file",
    "file_use",
})

_STATIC_SENTENCE_START_ALLOWLIST_LOWER: frozenset[str] = frozenset({
    # Non-skill generic syntax and directive vocabulary
    "off",
    "all",
    "require",
    "operations",
})

_SKILL_STOPWORDS: frozenset[str] = frozenset({
    "the",
    "a",
    "an",
    "and",
    "or",
    "for",
    "of",
    "to",
    "in",
    "on",
    "at",
    "by",
    "with",
    "from",
    "into",
    "as",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "have",
    "has",
    "had",
    "do",
    "does",
    "did",
    "can",
    "could",
    "should",
    "would",
    "will",
    "this",
    "that",
    "these",
    "those",
    "if",
    "then",
    "not",
    "no",
    "such",
    "than",
    "please",
    "use",
    "using",
    "used",
    "via",
    "make",
    "take",
    "give",
    "also",
    "just",
})

MAX_SKILL_KEYWORDS: int = 5

_PROTECTED_SPAN_PATTERNS: list[re.Pattern] = [
    # Already-tokenized placeholders (e.g. <EMAIL_0>, <REDACTED_SECRET_1>)
    re.compile(r"<[A-Z][A-Z0-9_]*_\d+>"),
    # Slack user/channel/subteam mentions (<@U...>, <!subteam^...>, <#C...>)
    re.compile(r"<[@#!][A-Za-z0-9_|.-]+>"),
    # Bracketed agent tags and slash directives: /[Visionary], [agent-10001], /max, etc.
    re.compile(r"/?\[[^\]\r\n]+\]"),
    re.compile(r"(?:^|(?<=\s))/[a-zA-Z0-9_-]+"),
    # Quantitative bound expressions where max/min precedes or follows numbers/units
    re.compile(
        r"\b(?:max|min|maximum|minimum)\b(?:\s*[:=]?\s*\d+|\s+(?:words?|chars?|characters?|lines?|sentences?|paragraphs?|tokens?|rounds?|messages?|turns?)\b)",
        re.IGNORECASE,
    ),
]


def extract_skill_exclusion_terms(skill: Any) -> tuple[list[str], list[str]]:
    """
    Extracts (global_terms, sentence_start_keywords) for a SkillDefinition.
    - global_terms: exact skill name, multi-word display name, and all-caps acronyms.
    - sentence_start_keywords: bare slash triggers and natural language keywords (max 5).
    Only extracts terms for auto-detected skills or agent-scoped skills.
    """
    triggers = getattr(skill, "triggers", {}) or {}
    is_auto = bool(triggers.get("auto_detect", False))
    scope = getattr(skill, "scope", "")
    if not (is_auto or scope == "agent"):
        return [], []

    global_terms: list[str] = []
    s_name = getattr(skill, "name", "").strip().lower()
    if s_name and s_name not in global_terms:
        global_terms.append(s_name)

    d_name = getattr(skill, "display_name", "").strip()
    if d_name:
        d_lower = d_name.lower()
        if d_lower not in global_terms:
            global_terms.append(d_lower)
        # Acronyms (e.g. RAG, PDF, API, SQL) in display name
        for word in d_name.split():
            clean_word = word.strip(" \t\r\n`'\".,;:!?()[]{}<>/")
            if clean_word.isupper() and len(clean_word) >= 2:
                w_lower = clean_word.lower()
                if w_lower not in global_terms:
                    global_terms.append(w_lower)

    # Collect sentence-start keyword candidates
    candidates: list[str] = []

    # 1. Explicit triggers.keywords
    triggers = getattr(skill, "triggers", {}) or {}
    explicit_kw = triggers.get("keywords") or []
    if isinstance(explicit_kw, (list, tuple)):
        for kw in explicit_kw:
            if isinstance(kw, str):
                candidates.append(kw.strip().lower())

    # 2. Slash command stems
    slash_list = triggers.get("slash") or []
    if isinstance(slash_list, str):
        slash_list = [slash_list]
    for slash_cmd in slash_list:
        bare = str(slash_cmd).strip().lstrip("/")
        if bare:
            candidates.append(bare.lower())
            for sub in re.split(r"[_\-]+", bare):
                if sub:
                    candidates.append(sub.lower())

    # 3. Meaningful non-stopword tokens from display_name
    if d_name:
        for word in re.split(r"[\s\W_]+", d_name):
            if word:
                candidates.append(word.lower())

    # 4. Name subtokens
    if s_name:
        for sub in re.split(r"[_\-]+", s_name):
            if sub:
                candidates.append(sub.lower())

    # 5. Extraction rule / description words for auto-detect or agent skills
    scope = getattr(skill, "scope", "")
    is_auto = bool(triggers.get("auto_detect", False))
    if is_auto or scope == "agent":
        ext_rule = getattr(skill, "extraction_rule", "") or ""
        quoted = re.findall(r"['\"]([^'\"]+)['\"]", ext_rule)
        for q in quoted:
            for w in re.split(r"[\s\W_]+", q):
                if w:
                    candidates.append(w.lower())
        desc = getattr(skill, "description", "") or ""
        for w in re.split(r"[\s\W_]+", desc)[:15]:
            if w:
                candidates.append(w.lower())

    # Filter & deduplicate
    sentence_start_keywords: list[str] = []
    seen: set[str] = set()

    for cand in candidates:
        token = cand.strip(" \t\r\n`'\".,;:!?()[]{}<>/")
        if (
            len(token) >= 3
            and token.isalpha()
            and token not in seen
            and token not in _SKILL_STOPWORDS
            and token not in _STATIC_GLOBAL_ALLOWLIST_LOWER
            and token not in global_terms
        ):
            seen.add(token)
            sentence_start_keywords.append(token)
            if len(sentence_start_keywords) >= MAX_SKILL_KEYWORDS:
                break

    return global_terms, sentence_start_keywords


def _is_at_sentence_or_clause_start(text: str, span_start: int) -> bool:
    """
    Checks whether span_start in text is at the beginning of the prompt or following
    a sentence or clause boundary, ignoring leading whitespace, Slack mentions,
    bracketed agent tags, and common prompt prefixes.
    """
    prefix = text[:span_start]
    # Strip optional 'User message:' prompt wrapper prefix
    prefix = re.sub(r"^\s*User(?:\s+message)?:\s*", "", prefix, flags=re.IGNORECASE)

    # Iteratively strip trailing Slack mentions and bracketed tags
    while True:
        stripped = prefix.rstrip(" \t")
        m_slack = re.search(r"<[@#!][A-Za-z0-9_|.-]+>\s*$", stripped)
        if m_slack:
            prefix = stripped[:m_slack.start()]
            continue
        m_tag = re.search(r"/?\[[^\]\r\n]+\]\s*$", stripped)
        if m_tag:
            prefix = stripped[:m_tag.start()]
            continue
        break

    clean_prefix = prefix.rstrip(" \t")
    if not clean_prefix:
        return True
    last_char = clean_prefix[-1]
    return last_char in {".", "!", "?", "\n", "\r", ":", ";", "-", "—", "(", "[", '"', "'", "`", "*"}


class PIIMasker:
    """
    Sanitizes documents and search results to prevent PII and secrets
    from reaching LLMs or vector indices. Supports two-way tokenization.
    """

    def __init__(self) -> None:
        self._presidio_analyzer: Any = None
        self._presidio_initialized = False
        self._init_lock = threading.Lock()
        self._custom_exclusions_file: Path | None = None
        self._exclusions_data: dict[str, Any] = {}
        self._global_allowlist: frozenset[str] = _STATIC_GLOBAL_ALLOWLIST_LOWER
        self._sentence_start_allowlist: frozenset[str] = _STATIC_SENTENCE_START_ALLOWLIST_LOWER
        self.refresh_allowlist()

    @property
    def active_exclusions_file(self) -> Path:
        return self._custom_exclusions_file or PRESIDIO_SKILL_EXCLUSIONS_FILE

    def configure_paths(self, exclusions_file: Path | str | None = None) -> None:
        """Configures an in-memory exclusions file override for test isolation."""
        self._custom_exclusions_file = Path(exclusions_file) if exclusions_file else None
        self._exclusions_data = {}
        self.refresh_allowlist()

    def reset_paths(self) -> None:
        """Resets the exclusions file back to the canonical state file."""
        self._custom_exclusions_file = None
        self._exclusions_data = {}
        self.refresh_allowlist()

    def refresh_allowlist(self) -> None:
        """
        Reconstructs in-memory frozenset allowlists by combining:
        1. Static technical & directive vocabularies
        2. Configured agent IDs, usernames, and display names
        3. Persisted skill keywords from presidio_skill_exclusions.json
        4. Any user manual_include / manual_exclude overrides
        """
        global_terms: set[str] = set(_STATIC_GLOBAL_ALLOWLIST_LOWER)
        sentence_terms: set[str] = set(_STATIC_SENTENCE_START_ALLOWLIST_LOWER)
        manual_excludes: set[str] = set()

        # 1. Agent names from config
        try:
            from core.config import AGENTS_CONFIG

            for k, v in AGENTS_CONFIG.items():
                global_terms.add(k.lower())
                if isinstance(v, dict):
                    if v.get("username"):
                        global_terms.add(v["username"].lower())
                    if v.get("displayname"):
                        global_terms.add(v["displayname"].lower())
                        for word in v["displayname"].lower().split():
                            global_terms.add(word)
        except Exception:
            pass

        try:
            from orchestration.agent_settings import settings_manager

            cfg = settings_manager.get_agents_config()
            if cfg:
                for k, v in cfg.items():
                    global_terms.add(k.lower())
                    if isinstance(v, dict):
                        if v.get("username"):
                            global_terms.add(v["username"].lower())
                        if v.get("displayname"):
                            global_terms.add(v["displayname"].lower())
                            for word in v["displayname"].lower().split():
                                global_terms.add(word)
        except Exception:
            pass

        # 2. Skill terms from exclusions registry
        ex_file = self.active_exclusions_file
        if ex_file.exists():
            try:
                with open(ex_file, "r", encoding="utf-8") as f:
                    self._exclusions_data = json.load(f)
            except Exception as e:
                logger.debug(f"[PII Masker] Could not read exclusions file {ex_file}: {e}")

        skills_dict = self._exclusions_data.get("skills", {})
        if isinstance(skills_dict, dict):
            for s_name, s_info in skills_dict.items():
                if isinstance(s_info, dict):
                    for gt in s_info.get("global_terms", []):
                        global_terms.add(gt.lower())
                    for st in s_info.get("sentence_start_keywords", []):
                        sentence_terms.add(st.lower())
                    for mi in s_info.get("manual_include", []):
                        sentence_terms.add(mi.lower())
                    for me in s_info.get("manual_exclude", []):
                        manual_excludes.add(me.lower())

        self._global_allowlist = frozenset(global_terms)
        self._sentence_start_allowlist = frozenset(sentence_terms - manual_excludes)

    def sync_skill_exclusions(self, force: bool = False) -> dict[str, Any]:
        """
        Scans registered skills, updates presidio_skill_exclusions.json atomically
        if new or modified skills are detected, and refreshes in-memory frozenset caches.
        """
        ex_file = self.active_exclusions_file
        existing: dict[str, Any] = {}
        if ex_file.exists():
            try:
                with open(ex_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        existing = data
            except Exception as e:
                logger.warning(f"[PII Masker] Failed to read {ex_file}: {e}")

        skills_data = existing.get("skills", {}) if isinstance(existing.get("skills"), dict) else {}
        stats = {"new": 0, "updated": 0, "deleted": 0, "unchanged": 0}
        has_changes = False

        try:
            from core.registry import get_skill_registry

            skill_registry = get_skill_registry()
            current_skills = skill_registry.list_skills()
        except Exception as e:
            logger.warning(f"[PII Masker] Could not get skill registry for exclusions sync: {e}")
            current_skills = []

        eligible_names = set()

        for skill in current_skills:
            is_auto = bool(skill.triggers.get("auto_detect", False))
            is_agent = getattr(skill, "scope", "") == "agent"
            if not (is_auto or is_agent):
                continue
            eligible_names.add(skill.name)
            rec = skills_data.get(skill.name)
            if rec and not force and rec.get("content_hash") == skill.content_hash:
                stats["unchanged"] += 1
                continue

            # Extract terms
            global_terms, sentence_start_kw = extract_skill_exclusion_terms(skill)
            existing_rec = rec or {}
            skills_data[skill.name] = {
                "content_hash": skill.content_hash,
                "display_name": skill.display_name,
                "scope": skill.scope,
                "auto_detect": is_auto,
                "global_terms": global_terms,
                "sentence_start_keywords": sentence_start_kw,
                "manual_include": existing_rec.get("manual_include", []),
                "manual_exclude": existing_rec.get("manual_exclude", []),
            }
            if rec:
                stats["updated"] += 1
            else:
                stats["new"] += 1
            has_changes = True

        # Check for deleted or ineligible skills
        for old_name in list(skills_data.keys()):
            if old_name not in eligible_names:
                del skills_data[old_name]
                stats["deleted"] += 1
                has_changes = True

        if has_changes or not ex_file.exists():
            payload = {
                "last_synced_at": datetime.now(timezone.utc).isoformat(),
                "skills": skills_data,
            }
            try:
                atomic_write_json(ex_file, payload)
                self._exclusions_data = payload
                logger.info(
                    f"[PII Masker] Synced skill exclusions ({stats['new']} new, {stats['updated']} updated, "
                    f"{stats['deleted']} deleted, {len(skills_data)} total) to {ex_file.name}"
                )
            except Exception as e:
                logger.error(f"[PII Masker] Failed to persist skill exclusions: {e}")
                self._exclusions_data = payload
        else:
            self._exclusions_data = existing

        self.refresh_allowlist()
        stats["total"] = len(skills_data)
        return stats

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
                        "ner_model_configuration": {
                            "labels_to_ignore": [
                                "CARDINAL",
                                "ORDINAL",
                                "MONEY",
                                "QUANTITY",
                                "PERCENT",
                                "WORK_OF_ART",
                                "LANGUAGE",
                                "LAW",
                                "EVENT",
                                "FAC",
                                "PRODUCT",
                                "NORP",
                                "DATE",
                                "TIME",
                                "ORGANIZATION",
                                "ORG",
                            ]
                        },
                    }
                )
                nlp_engine = provider.create_engine()
                self._presidio_analyzer = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["en"])
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

        # Pre-reserve any literal <TOKEN_N> placeholders already present in user/input text
        # so fresh masks never collide with forged placeholder tags (C5 / TKT-1209).
        for lit_match in re.finditer(r"<([A-Z][A-Z0-9_]*)_(\d+)>", text):
            lit_base, lit_idx = lit_match.group(1), lit_match.group(2)
            counts[lit_base] = max(counts.get(lit_base, 0), int(lit_idx) + 1)

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
                    # Pre-compute protected spans to avoid false positives or re-slicing
                    protected_intervals: list[tuple[int, int]] = []
                    for p_pat in _PROTECTED_SPAN_PATTERNS:
                        for m in p_pat.finditer(masked_text):
                            protected_intervals.append((m.start(), m.end()))

                    filtered_results = []
                    for res in results:
                        original_str = masked_text[res.start : res.end]
                        # Reject span if it overlaps any protected pattern interval
                        if any(res.start < end and res.end > start for start, end in protected_intervals):
                            continue

                        # For named entities (PERSON, LOCATION), apply syntactic & semantic guards
                        if res.entity_type in ("PERSON", "LOCATION"):
                            # 1. Require at least one uppercase letter (named entities must not be pure lowercase)
                            if not any(ch.isupper() for ch in original_str):
                                continue

                            # 2. Reject code / syntax identifiers (containing underscores or kebab-case with digits)
                            if "_" in original_str or re.search(r"[a-z0-9]-[a-z0-9]", original_str, re.IGNORECASE):
                                continue

                            # 3. Check against static and dynamic allowlists
                            clean_norm = original_str.lower().strip(" \t\n\r`'\".,;:!?()[]{}<>/")
                            if clean_norm in self._global_allowlist:
                                continue
                            if clean_norm in self._sentence_start_allowlist and _is_at_sentence_or_clause_start(masked_text, res.start):
                                continue

                        filtered_results.append(res)

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
                    sorted_spans = sorted(filtered_results, key=lambda x: (x.start, -(x.end - x.start)))
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

    async def warm_up(self) -> None:
        """
        Eagerly initializes Presidio/spaCy and syncs skill exclusions off the event loop
        at startup so user turn latency is never impacted.
        """
        import asyncio

        if not self._presidio_initialized:
            await asyncio.to_thread(self._init_presidio_if_available)
        await asyncio.to_thread(self.sync_skill_exclusions)
        logger.info("[PII Masker] Warm-up complete (Presidio/spaCy pre-initialized & skill exclusions synced).")

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
