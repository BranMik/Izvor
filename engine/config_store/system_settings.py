from __future__ import annotations

from collections.abc import Mapping
import copy
import fnmatch
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from config_store.integrity import ConfigIntegrityValidator
from config_store.settings_store import JsonSettingsStore
from core.paths import PROJECT_ROOT, SYSTEM_BACKUP_FILE, SYSTEM_SETTINGS_FILE

logger = logging.getLogger(__name__)

BACKUP_INTERVAL_SECONDS = 7 * 86400

DEFAULT_PROTECTED_WRITE_PATHS: list[str] = [
    ".env",
    ".git",
    "engine",
    "system_resources",
    "audit",
    "data/audit",
    "config/settings/agent_settings.json",
    "config/settings/system_settings.json",
    "engine/settings/system_agent_settings.json",
    "data/state/app_settings.json",
    "data/state/presidio_skill_exclusions.json",
]


_ENV_VAR_REGEX = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _expand_env_vars_in_str(val: str, env: Mapping[str, str] | None = None) -> str:
    """Replaces ${VAR} or $VAR in a string with os.environ.get(VAR, '') or from injected env."""
    if not isinstance(val, str):
        return val
    active_env = env if env is not None else os.environ
    return _ENV_VAR_REGEX.sub(lambda m: active_env.get(m.group(1) or m.group(2), ""), val)


def _expand_env_vars_in_dict(d: dict[str, Any], env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Recursively expands ${VAR} or $VAR in dict keys and values."""
    res: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, str):
            res[k] = _expand_env_vars_in_str(v, env=env)
        elif isinstance(v, dict):
            res[k] = _expand_env_vars_in_dict(v, env=env)
        elif isinstance(v, list):
            res[k] = [_expand_env_vars_in_str(item, env=env) if isinstance(item, str) else item for item in v]
        else:
            res[k] = v
    return res


def _clamp_output_max_words(val: Any, default: int) -> int:
    """Clamps a configured output_constraints word limit to INT_MODIFIER_BOUNDS['max_words']."""
    from core.models import INT_MODIFIER_BOUNDS

    min_b, max_b = INT_MODIFIER_BOUNDS["max_words"]
    if isinstance(val, bool) or val is None:
        return default
    try:
        parsed = int(val)
    except (TypeError, ValueError):
        return default
    return max(min_b, min(max_b, parsed))


class McpServerConfig(BaseModel):
    enabled: bool = True
    transport: str = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = 30.0
    mutating_tools: list[str] = Field(default_factory=list)
    outbound_pii_masking: bool = True

    model_config = {"extra": "allow"}


class SystemSettingsModel(BaseModel):
    default_fast_model: str = "gemini/gemini-3.8-flash"
    default_reasoning_model: str = "mistral/mistral-large-latest"
    internal_revisor_enabled: bool = True
    shared_evidence_max_chars: int = 40_000
    max_file_size_mb: float = 50.0
    rag: dict[str, Any] = Field(
        default_factory=lambda: {
            "max_chunk_chars": 3500,
            "chunk_overlap_chars": 300,
            "min_chunk_chars": 80,
            "default_top_k": 6,
            "embedding_model": "gemini/gemini-embedding-001",
            "embedding_dim": 3072,
            "embedding_batch_size": 32,
            "pii_masking": "AT_RETRIEVAL",
            "guardrail_threshold": 0.85,
        }
    )
    workspace_write_blacklist_additional: list[str] = Field(default_factory=list)
    strict_privacy_mode_enabled: bool = True
    log_level: str = "INFO"
    debate_turn_pacing_seconds: float = 3.0
    slack_post_pause_seconds: float = 2.0
    tool_call_loop_timeout_seconds: float = 60.0
    guardrail_fail_mode: str = "open"
    max_discussion_turns: int = 20
    max_tool_iterations: int = 3
    tool_call_num_retries: int = 2
    word_limit_grace_buffer: int = 50
    output_constraints: dict[str, Any] = Field(
        default_factory=lambda: {
            "default_max_words": 200,
            "short_max_words": 100,
        }
    )
    system_agent_inference_timeout_seconds: float = 20.0
    system_agent_inference_retry_timeout_multiplier: float = 1.5
    guardrail_embedding_cache_size: int = 500
    guardrail_chunk_chars: int = 800
    guardrail_chunk_overlap_chars: int = 200
    guardrail_max_chunks: int = 8
    guardrail_indirect_embedding_enabled: bool = False
    pii_ner_models: list[str] = Field(default_factory=lambda: ["en_core_web_sm"])
    audit_hmac_key_id: str = "default"
    thread_cleanup_interval_seconds: float = 600.0
    audit_integrity_check_interval_seconds: float = 43200.0
    skill_exclusions_sync_interval_seconds: float = 86400.0
    pricing_cache_ttl_days: int = 7
    slack_file_download_timeout_seconds: float = 30.0
    web_search_timeout_seconds: float = 10.0
    web_fetch_timeout_seconds: float = 12.0
    db_busy_timeout_seconds: float = 5.0
    llm_max_retries: int = 3
    llm_retry_backoff_base: float = 1.5
    slack_rate_limit_per_second: float = 10.0
    slack_rate_limit_burst: float = 15.0
    pricing_benchmark_url: str = "https://artificialanalysis.ai/models"
    web_fetch_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    max_write_bytes: int = 1_048_576
    web_security: dict[str, Any] = Field(
        default_factory=lambda: {
            "max_url_length": 2048,
            "max_query_length": 1024,
            "max_redirects": 5,
            "max_body_bytes": 2097152,
            "allowed_ports": [80, 443],
            "allowed_domains": [],
            "strip_query_on_untrusted_domains": False,
            "max_fetches_per_turn": 5,
        }
    )
    security_burst_limits: dict[str, Any] = Field(
        default_factory=lambda: {
            "auth_failure_window_seconds": 60.0,
            "auth_failure_burst_limit": 5,
            "global_auth_failure_burst_limit": 25,
            "max_tracker_entries": 1000,
        }
    )
    dialectical_stress_test: dict[str, Any] = Field(
        default_factory=lambda: {
            "enabled": True,
            "consecutive_threshold": 3,
        }
    )
    retention_checkpoints_days: int = 7
    retention_traces_days: int = 30
    retention_event_dedup_days: int = 1
    mcp_servers: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "allow"}


class SystemSettingsManager:
    """
    Single Source of Truth (SSOT) for system-level settings that are changeable ONLY in file (not through UI):
    - max_file_size_mb (float)
    - rag (dict): semantic chunking and LanceDB embedding settings
    - workspace_write_blacklist_additional (list[str])
    """

    def __init__(
        self,
        settings_file: Path | None = None,
        backup_file: Path | None = None,
        auto_load: bool = True,
    ):
        self._custom_settings_file: bool = settings_file is not None
        self.default_settings_file = settings_file or SYSTEM_SETTINGS_FILE
        self.default_backup_file = backup_file or SYSTEM_BACKUP_FILE
        self._store: JsonSettingsStore[SystemSettingsModel] = JsonSettingsStore(
            self.default_settings_file,
            SystemSettingsModel,
            backup_file=self.default_backup_file,
        )

        self.default_fast_model: str = "gemini/gemini-3.8-flash"
        self.default_reasoning_model: str = "mistral/mistral-large-latest"
        self.internal_revisor_enabled: bool = True
        self.shared_evidence_max_chars: int = 40_000
        self._shared_evidence_max_chars_manually_set: bool = False
        self.max_file_size_mb: float = 50.0
        self.workspace_write_blacklist_additional: list[str] = []
        self.strict_privacy_mode_enabled: bool = True
        self.log_level: str = "INFO"
        self.debate_turn_pacing_seconds: float = 3.0
        self.slack_post_pause_seconds: float = 2.0
        self._debate_turn_pacing_manually_set: bool = False
        self._slack_post_pause_manually_set: bool = False
        self.tool_call_loop_timeout_seconds: float = 60.0
        self.guardrail_fail_mode: str = "open"
        self.max_discussion_turns: int = 20
        self.max_tool_iterations: int = 3
        self.tool_call_num_retries: int = 2
        self.word_limit_grace_buffer: int = 50
        self.output_constraints: dict[str, int] = {
            "default_max_words": 200,
            "short_max_words": 100,
        }
        self.system_agent_inference_timeout_seconds: float = 20.0
        self.system_agent_inference_retry_timeout_multiplier: float = 1.5
        self.guardrail_embedding_cache_size: int = 500
        self.guardrail_chunk_chars: int = 800
        self.guardrail_chunk_overlap_chars: int = 200
        self.guardrail_max_chunks: int = 8
        self.guardrail_indirect_embedding_enabled: bool = False
        self.pii_ner_models: list[str] = ["en_core_web_sm"]
        self.audit_hmac_key_id: str = "default"
        self.thread_cleanup_interval_seconds: float = 600.0
        self.audit_integrity_check_interval_seconds: float = 43200.0
        self.skill_exclusions_sync_interval_seconds: float = 86400.0
        self.pricing_cache_ttl_days: int = 7
        self.slack_file_download_timeout_seconds: float = 30.0
        self.web_search_timeout_seconds: float = 10.0
        self.web_fetch_timeout_seconds: float = 12.0
        self.db_busy_timeout_seconds: float = 5.0
        self.llm_max_retries: int = 3
        self.llm_retry_backoff_base: float = 1.5
        self.slack_rate_limit_per_second: float = 10.0
        self.slack_rate_limit_burst: float = 15.0
        self.pricing_benchmark_url: str = "https://artificialanalysis.ai/models"
        self.web_fetch_user_agent: str = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        self.max_write_bytes: int = 1_048_576
        self.web_security: dict[str, Any] = {
            "max_url_length": 2048,
            "max_query_length": 1024,
            "max_redirects": 5,
            "max_body_bytes": 2097152,
            "allowed_ports": [80, 443],
            "allowed_domains": [],
            "strip_query_on_untrusted_domains": False,
            "max_fetches_per_turn": 5,
        }
        self.security_burst_limits: dict[str, Any] = {
            "auth_failure_window_seconds": 60.0,
            "auth_failure_burst_limit": 5,
            "global_auth_failure_burst_limit": 25,
            "max_tracker_entries": 1000,
        }
        self.dialectical_stress_test: dict[str, Any] = {
            "enabled": True,
            "consecutive_threshold": 3,
        }
        self.retention_checkpoints_days: int = 7
        self.retention_traces_days: int = 30
        self.retention_event_dedup_days: int = 1
        self.mcp_servers: dict[str, dict[str, Any]] = {}
        self._raw_data: dict[str, Any] = {}

        self.rag_config: dict[str, Any] = {
            "max_chunk_chars": 3500,
            "chunk_overlap_chars": 300,
            "min_chunk_chars": 80,
            "default_top_k": 6,
            "embedding_model": "gemini/gemini-embedding-001",
            "embedding_dim": 3072,
            "embedding_batch_size": 32,
            "pii_masking": "AT_RETRIEVAL",
            "guardrail_threshold": 0.85,
        }

        self._last_loaded_mtime: float = 0.0

        if auto_load:
            self.load()
        self._register_core_providers()

    @property
    def active_settings_file(self) -> Path:
        return self.default_settings_file

    def configure_paths(
        self,
        settings_file: Path | str | None = None,
        backup_file: Path | str | None = None,
    ) -> None:
        """Configures isolated backing files for tests and runtime overrides."""
        if settings_file is not None:
            self.default_settings_file = Path(settings_file)
            self._custom_settings_file = True
        if backup_file is not None:
            self.default_backup_file = Path(backup_file)
        self._store = JsonSettingsStore(
            self.default_settings_file,
            SystemSettingsModel,
            backup_file=self.default_backup_file,
        )
        self._last_loaded_mtime = 0.0
        self._store.invalidate_cache()
        if self.default_settings_file.exists():
            self.load(force=True)
        else:
            self._apply_model_to_fields(SystemSettingsModel())
        self._register_core_providers()

    def reset_paths(self) -> None:
        """Resets configured paths back to canonical system defaults."""
        self._custom_settings_file = False
        self.default_settings_file = SYSTEM_SETTINGS_FILE
        self.default_backup_file = SYSTEM_BACKUP_FILE
        self._store = JsonSettingsStore(
            self.default_settings_file,
            SystemSettingsModel,
            backup_file=self.default_backup_file,
        )
        self._last_loaded_mtime = 0.0
        self._store.invalidate_cache()
        if self.default_settings_file.exists():
            try:
                self.load(force=True)
            except (OSError, ValueError, TypeError, ValidationError) as err:
                logger.debug(
                    "[SYSTEM_SETTINGS] Deferred initial load on %s: %s",
                    self.default_settings_file,
                    err,
                )
        else:
            self._apply_model_to_fields(SystemSettingsModel())
        self._register_core_providers()

    def _parse_rag_section(self, rag_data: Any) -> None:
        if isinstance(rag_data, dict):
            def _get(key: str, default: Any) -> Any:
                val = rag_data.get(key)
                return default if val is None else val

            self.rag_config = {
                "max_chunk_chars": int(_get("max_chunk_chars", 3500)),
                "chunk_overlap_chars": int(_get("chunk_overlap_chars", 300)),
                "min_chunk_chars": int(_get("min_chunk_chars", 80)),
                "default_top_k": int(_get("default_top_k", 6)),
                "embedding_model": str(_get("embedding_model", "gemini/gemini-embedding-001")),
                "embedding_dim": int(_get("embedding_dim", 3072)),
                "embedding_batch_size": int(_get("embedding_batch_size", 32)),
                "pii_masking": str(_get("pii_masking", "AT_RETRIEVAL")),
                "guardrail_threshold": float(_get("guardrail_threshold", 0.85)),
            }

    def _parse_output_constraints(self, raw_oc: Any) -> None:
        if isinstance(raw_oc, dict):
            self.output_constraints = {
                "default_max_words": _clamp_output_max_words(raw_oc.get("default_max_words"), 200),
                "short_max_words": _clamp_output_max_words(raw_oc.get("short_max_words"), 100),
            }
        else:
            self.output_constraints = {
                "default_max_words": 200,
                "short_max_words": 100,
            }

    def _apply_model_to_fields(self, model: SystemSettingsModel) -> None:
        self.default_fast_model = (
            str(model.default_fast_model).strip() or "gemini/gemini-3.8-flash"
        )
        self.default_reasoning_model = (
            str(model.default_reasoning_model).strip() or "mistral/mistral-large-latest"
        )
        self.internal_revisor_enabled = bool(getattr(model, "internal_revisor_enabled", True))
        self.shared_evidence_max_chars = int(getattr(model, "shared_evidence_max_chars", 40_000))
        self.max_file_size_mb = float(model.max_file_size_mb)
        self._parse_rag_section(model.rag)
        raw_bl = model.workspace_write_blacklist_additional
        self.workspace_write_blacklist_additional = (
            [str(p).strip() for p in raw_bl if str(p).strip()]
            if isinstance(raw_bl, list)
            else []
        )
        self.strict_privacy_mode_enabled = bool(model.strict_privacy_mode_enabled)
        self.log_level = str(model.log_level).strip().upper() or "INFO"
        self.debate_turn_pacing_seconds = float(model.debate_turn_pacing_seconds)
        self.slack_post_pause_seconds = float(model.slack_post_pause_seconds)
        self.tool_call_loop_timeout_seconds = float(model.tool_call_loop_timeout_seconds)
        raw_gfm = str(model.guardrail_fail_mode).strip().lower()
        self.guardrail_fail_mode = raw_gfm if raw_gfm in ("open", "closed") else "open"
        self.max_discussion_turns = int(model.max_discussion_turns)
        self.max_tool_iterations = int(model.max_tool_iterations)
        self.tool_call_num_retries = int(model.tool_call_num_retries)
        self.word_limit_grace_buffer = int(model.word_limit_grace_buffer)
        self._parse_output_constraints(getattr(model, "output_constraints", None))
        self.system_agent_inference_timeout_seconds = float(model.system_agent_inference_timeout_seconds)
        self.system_agent_inference_retry_timeout_multiplier = float(model.system_agent_inference_retry_timeout_multiplier)
        self.guardrail_embedding_cache_size = int(model.guardrail_embedding_cache_size)
        chunk_chars = max(100, int(getattr(model, "guardrail_chunk_chars", 800)))
        self.guardrail_chunk_chars = chunk_chars
        overlap = max(0, int(getattr(model, "guardrail_chunk_overlap_chars", 200)))
        self.guardrail_chunk_overlap_chars = min(overlap, max(0, chunk_chars - 1))
        self.guardrail_max_chunks = max(1, int(getattr(model, "guardrail_max_chunks", 8)))
        self.guardrail_indirect_embedding_enabled = bool(
            getattr(model, "guardrail_indirect_embedding_enabled", False)
        )
        raw_ner = getattr(model, "pii_ner_models", ["en_core_web_sm"])
        cleaned_ner = [str(m).strip() for m in raw_ner if isinstance(m, str) and str(m).strip()] if isinstance(raw_ner, list) else []
        self.pii_ner_models = cleaned_ner or ["en_core_web_sm"]
        self.audit_hmac_key_id = str(getattr(model, "audit_hmac_key_id", "default") or "default").strip() or "default"
        self.thread_cleanup_interval_seconds = float(model.thread_cleanup_interval_seconds)
        self.audit_integrity_check_interval_seconds = float(model.audit_integrity_check_interval_seconds)
        self.skill_exclusions_sync_interval_seconds = float(
            getattr(model, "skill_exclusions_sync_interval_seconds", 86400.0)
        )
        self.pricing_cache_ttl_days = int(model.pricing_cache_ttl_days)
        self.slack_file_download_timeout_seconds = float(model.slack_file_download_timeout_seconds)
        self.web_search_timeout_seconds = float(model.web_search_timeout_seconds)
        self.web_fetch_timeout_seconds = float(model.web_fetch_timeout_seconds)
        self.db_busy_timeout_seconds = float(model.db_busy_timeout_seconds)
        self.llm_max_retries = int(model.llm_max_retries)
        self.llm_retry_backoff_base = float(model.llm_retry_backoff_base)
        self.slack_rate_limit_per_second = float(model.slack_rate_limit_per_second)
        self.slack_rate_limit_burst = float(model.slack_rate_limit_burst)
        self.pricing_benchmark_url = str(model.pricing_benchmark_url).strip() or "https://artificialanalysis.ai/models"
        self.web_fetch_user_agent = str(model.web_fetch_user_agent).strip()
        self.max_write_bytes = int(model.max_write_bytes)
        raw_ws = model.web_security
        if isinstance(raw_ws, dict):
            self.web_security = {
                "max_url_length": int(raw_ws.get("max_url_length", 2048)),
                "max_query_length": int(raw_ws.get("max_query_length", 1024)),
                "max_redirects": int(raw_ws.get("max_redirects", 5)),
                "max_body_bytes": int(raw_ws.get("max_body_bytes", 2097152)),
                "allowed_ports": list(raw_ws.get("allowed_ports", [80, 443])),
                "allowed_domains": [str(d).lower().lstrip(".") for d in raw_ws.get("allowed_domains", []) if str(d).strip()],
                "strip_query_on_untrusted_domains": bool(raw_ws.get("strip_query_on_untrusted_domains", False)),
                "max_fetches_per_turn": int(raw_ws.get("max_fetches_per_turn", 5)),
            }
        raw_sbl = model.security_burst_limits
        if isinstance(raw_sbl, dict):
            self.security_burst_limits = {
                "auth_failure_window_seconds": float(raw_sbl.get("auth_failure_window_seconds", 60.0)),
                "auth_failure_burst_limit": int(raw_sbl.get("auth_failure_burst_limit", 5)),
                "global_auth_failure_burst_limit": int(raw_sbl.get("global_auth_failure_burst_limit", 25)),
                "max_tracker_entries": int(raw_sbl.get("max_tracker_entries", 1000)),
            }
        raw_dst = model.dialectical_stress_test
        if isinstance(raw_dst, dict):
            self.dialectical_stress_test = {
                "enabled": bool(raw_dst.get("enabled", True)),
                "consecutive_threshold": int(raw_dst.get("consecutive_threshold", 3)),
            }
        self.retention_checkpoints_days = int(getattr(model, "retention_checkpoints_days", 7))
        self.retention_traces_days = int(getattr(model, "retention_traces_days", 30))
        self.retention_event_dedup_days = int(getattr(model, "retention_event_dedup_days", 1))
        raw_mcp = getattr(model, "mcp_servers", {})
        self.mcp_servers = copy.deepcopy(raw_mcp) if isinstance(raw_mcp, dict) else {}

    def _to_model(self) -> SystemSettingsModel:
        return SystemSettingsModel(
            default_fast_model=self.default_fast_model,
            default_reasoning_model=self.default_reasoning_model,
            internal_revisor_enabled=self.internal_revisor_enabled,
            shared_evidence_max_chars=self.shared_evidence_max_chars,
            max_file_size_mb=self.max_file_size_mb,
            rag=copy.deepcopy(self.rag_config),
            workspace_write_blacklist_additional=list(self.workspace_write_blacklist_additional),
            strict_privacy_mode_enabled=self.strict_privacy_mode_enabled,
            log_level=self.log_level,
            debate_turn_pacing_seconds=self.debate_turn_pacing_seconds,
            slack_post_pause_seconds=self.slack_post_pause_seconds,
            tool_call_loop_timeout_seconds=self.tool_call_loop_timeout_seconds,
            guardrail_fail_mode=self.guardrail_fail_mode,
            max_discussion_turns=self.max_discussion_turns,
            max_tool_iterations=self.max_tool_iterations,
            tool_call_num_retries=self.tool_call_num_retries,
            word_limit_grace_buffer=self.word_limit_grace_buffer,
            output_constraints=copy.deepcopy(self.output_constraints),
            system_agent_inference_timeout_seconds=self.system_agent_inference_timeout_seconds,
            system_agent_inference_retry_timeout_multiplier=self.system_agent_inference_retry_timeout_multiplier,
            guardrail_embedding_cache_size=self.guardrail_embedding_cache_size,
            guardrail_chunk_chars=self.guardrail_chunk_chars,
            guardrail_chunk_overlap_chars=self.guardrail_chunk_overlap_chars,
            guardrail_max_chunks=self.guardrail_max_chunks,
            guardrail_indirect_embedding_enabled=self.guardrail_indirect_embedding_enabled,
            pii_ner_models=list(self.pii_ner_models),
            audit_hmac_key_id=self.audit_hmac_key_id,
            thread_cleanup_interval_seconds=self.thread_cleanup_interval_seconds,
            audit_integrity_check_interval_seconds=self.audit_integrity_check_interval_seconds,
            skill_exclusions_sync_interval_seconds=self.skill_exclusions_sync_interval_seconds,
            pricing_cache_ttl_days=self.pricing_cache_ttl_days,
            slack_file_download_timeout_seconds=self.slack_file_download_timeout_seconds,
            web_search_timeout_seconds=self.web_search_timeout_seconds,
            web_fetch_timeout_seconds=self.web_fetch_timeout_seconds,
            db_busy_timeout_seconds=self.db_busy_timeout_seconds,
            llm_max_retries=self.llm_max_retries,
            llm_retry_backoff_base=self.llm_retry_backoff_base,
            slack_rate_limit_per_second=self.slack_rate_limit_per_second,
            slack_rate_limit_burst=self.slack_rate_limit_burst,
            pricing_benchmark_url=self.pricing_benchmark_url,
            web_fetch_user_agent=self.web_fetch_user_agent,
            max_write_bytes=self.max_write_bytes,
            web_security=copy.deepcopy(self.web_security),
            security_burst_limits=copy.deepcopy(self.security_burst_limits),
            dialectical_stress_test=copy.deepcopy(self.dialectical_stress_test),
            retention_checkpoints_days=self.retention_checkpoints_days,
            retention_traces_days=self.retention_traces_days,
            retention_event_dedup_days=self.retention_event_dedup_days,
            mcp_servers=copy.deepcopy(self.mcp_servers),
        )

    def get_snapshot(self) -> SystemSettingsModel:
        """Returns an isolated deep-copied Pydantic snapshot of system settings."""
        self.load()
        return self._to_model().model_copy(deep=True)

    def load(self, force: bool = False) -> None:
        target_file = self.active_settings_file
        if not target_file.exists():
            return

        try:
            mtime = target_file.stat().st_mtime
            if not force and mtime == self._last_loaded_mtime and self._last_loaded_mtime > 0:
                return

            model = self._store.load(force=True)
            data = self._store.get_raw_data()

            ConfigIntegrityValidator.check_system_settings_integrity(data)

            self._raw_data = copy.deepcopy(data)
            self._apply_model_to_fields(model)
            self._last_loaded_mtime = mtime
            logger.debug(f"[SYSTEM_SETTINGS] Loaded settings from {target_file}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[SYSTEM_SETTINGS] Failed to load {target_file}: {e}")

    def save(self) -> None:
        target_file = self.active_settings_file
        target_file.parent.mkdir(parents=True, exist_ok=True)

        payload = copy.deepcopy(self._raw_data) if hasattr(self, "_raw_data") and isinstance(self._raw_data, dict) else {}
        payload.update({
            "default_fast_model": self.default_fast_model,
            "default_reasoning_model": self.default_reasoning_model,
            "internal_revisor_enabled": self.internal_revisor_enabled,
            "shared_evidence_max_chars": self.shared_evidence_max_chars,
            "max_file_size_mb": self.max_file_size_mb,
            "rag": copy.deepcopy(self.rag_config),
            "_comment_workspace_write_blacklist_additional": (
                "Automatic hardcoded protection blocks writes to .env, .git, config/settings/agent_settings.json, "
                "data/state/app_settings.json, and config/settings/system_settings.json. Add any additional relative workspace "
                "paths or file patterns here to prevent unauthorized modifications."
            ),
            "workspace_write_blacklist_additional": list(self.workspace_write_blacklist_additional),
            "strict_privacy_mode_enabled": self.strict_privacy_mode_enabled,
            "log_level": self.log_level,
            "debate_turn_pacing_seconds": self.debate_turn_pacing_seconds,
            "tool_call_loop_timeout_seconds": self.tool_call_loop_timeout_seconds,
            "slack_post_pause_seconds": self.slack_post_pause_seconds,
            "guardrail_fail_mode": self.guardrail_fail_mode,
            "max_discussion_turns": self.max_discussion_turns,
            "max_tool_iterations": self.max_tool_iterations,
            "tool_call_num_retries": self.tool_call_num_retries,
            "word_limit_grace_buffer": self.word_limit_grace_buffer,
            "output_constraints": dict(self.output_constraints),
            "system_agent_inference_timeout_seconds": self.system_agent_inference_timeout_seconds,
            "system_agent_inference_retry_timeout_multiplier": self.system_agent_inference_retry_timeout_multiplier,
            "guardrail_embedding_cache_size": self.guardrail_embedding_cache_size,
            "guardrail_chunk_chars": self.guardrail_chunk_chars,
            "guardrail_chunk_overlap_chars": self.guardrail_chunk_overlap_chars,
            "guardrail_max_chunks": self.guardrail_max_chunks,
            "guardrail_indirect_embedding_enabled": self.guardrail_indirect_embedding_enabled,
            "pii_ner_models": list(self.pii_ner_models),
            "audit_hmac_key_id": self.audit_hmac_key_id,
            "thread_cleanup_interval_seconds": self.thread_cleanup_interval_seconds,
            "audit_integrity_check_interval_seconds": self.audit_integrity_check_interval_seconds,
            "skill_exclusions_sync_interval_seconds": self.skill_exclusions_sync_interval_seconds,
            "pricing_cache_ttl_days": self.pricing_cache_ttl_days,
            "slack_file_download_timeout_seconds": self.slack_file_download_timeout_seconds,
            "web_search_timeout_seconds": self.web_search_timeout_seconds,
            "web_fetch_timeout_seconds": self.web_fetch_timeout_seconds,
            "db_busy_timeout_seconds": self.db_busy_timeout_seconds,
            "llm_max_retries": self.llm_max_retries,
            "llm_retry_backoff_base": self.llm_retry_backoff_base,
            "slack_rate_limit_per_second": self.slack_rate_limit_per_second,
            "slack_rate_limit_burst": self.slack_rate_limit_burst,
            "pricing_benchmark_url": self.pricing_benchmark_url,
            "web_fetch_user_agent": self.web_fetch_user_agent,
            "max_write_bytes": self.max_write_bytes,
            "web_security": copy.deepcopy(self.web_security),
            "security_burst_limits": copy.deepcopy(self.security_burst_limits),
            "dialectical_stress_test": copy.deepcopy(self.dialectical_stress_test),
            "retention_checkpoints_days": self.retention_checkpoints_days,
            "retention_traces_days": self.retention_traces_days,
            "retention_event_dedup_days": self.retention_event_dedup_days,
            "mcp_servers": copy.deepcopy(self.mcp_servers),
        })

        try:
            self._store.save(payload)
            self._last_loaded_mtime = target_file.stat().st_mtime
            self._raw_data = copy.deepcopy(payload)
            self._maybe_update_backup(payload)
        except (OSError, TypeError, ValueError) as e:
            logger.error(f"[SYSTEM_SETTINGS] Error saving settings to {target_file}: {e}")

    def _maybe_update_backup(self, payload: dict[str, Any]) -> None:
        try:
            now = time.time()
            backup = self.default_backup_file
            if not backup.exists() or (now - backup.stat().st_mtime > BACKUP_INTERVAL_SECONDS):
                backup_store = JsonSettingsStore(backup, SystemSettingsModel)
                backup_store.save(payload)
                logger.info("[SYSTEM_SETTINGS] Updated emergency backup.")
        except (OSError, TypeError, ValueError) as e:
            logger.warning(f"[SYSTEM_SETTINGS] Backup update failed: {e}")

    # --- Default Model Settings ---
    def get_default_fast_model(self) -> str:
        self.load()
        return str(self.default_fast_model)

    def get_default_reasoning_model(self) -> str:
        self.load()
        return str(self.default_reasoning_model)

    def set_default_fast_model(self, model: str, save: bool = True) -> None:
        clean = str(model).strip()
        if clean:
            self.default_fast_model = clean
            if save:
                self.save()
            logger.info(f"[SYSTEM_SETTINGS] Set default_fast_model to '{self.default_fast_model}'.")

    def set_default_reasoning_model(self, model: str, save: bool = True) -> None:
        clean = str(model).strip()
        if clean:
            self.default_reasoning_model = clean
            if save:
                self.save()
            logger.info(f"[SYSTEM_SETTINGS] Set default_reasoning_model to '{self.default_reasoning_model}'.")

    # --- Internal Revisor Settings ---
    def get_internal_revisor_enabled(self) -> bool:
        self.load()
        return bool(getattr(self, "internal_revisor_enabled", True))

    def set_internal_revisor_enabled(self, enabled: bool, save: bool = True) -> None:
        self.internal_revisor_enabled = bool(enabled)
        if save:
            self.save()
        logger.info(f"[SYSTEM_SETTINGS] Set internal_revisor_enabled to {self.internal_revisor_enabled}.")

    def _register_core_providers(self) -> None:
        try:
            from core.config import set_default_model_providers, set_max_discussion_turns_provider

            set_default_model_providers(
                fast_provider=lambda: self.get_default_fast_model(),
                reasoning_provider=lambda: self.get_default_reasoning_model(),
            )
            set_max_discussion_turns_provider(lambda: self.get_max_discussion_turns())
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[SYSTEM_SETTINGS] Failed to register core model providers: {e}")

        try:
            from core.llm_utils import set_llm_retry_config_provider, set_privacy_mode_provider

            set_llm_retry_config_provider(lambda: (self.get_llm_max_retries(), self.get_llm_retry_backoff_base()))
            set_privacy_mode_provider(lambda: self.is_strict_privacy_mode_enabled())
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[SYSTEM_SETTINGS] Failed to register core LLM providers: {e}")

        try:
            from core.database import set_db_busy_timeout_provider

            set_db_busy_timeout_provider(lambda: self.get_db_busy_timeout_seconds())
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[SYSTEM_SETTINGS] Failed to register core DB provider: {e}")

        try:
            from core.slack_security import set_burst_limits_provider

            set_burst_limits_provider(lambda: self.get_security_burst_limits())
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[SYSTEM_SETTINGS] Failed to register core burst limits provider: {e}")

    # --- File Size Limit ---
    def get_max_file_size_mb(self) -> float:
        self.load()
        return float(self.max_file_size_mb)

    def get_max_file_size_bytes(self) -> int:
        return int(self.get_max_file_size_mb() * 1024 * 1024)

    def set_max_file_size_mb(self, val: float, save: bool = True) -> None:
        try:
            self.max_file_size_mb = max(0.1, float(val))
            if save:
                self.save()
            logger.info(f"[SYSTEM_SETTINGS] Set max_file_size_mb to {self.max_file_size_mb} MB.")
        except (ValueError, TypeError) as e:
            logger.error(f"[SYSTEM_SETTINGS] Invalid max_file_size_mb '{val}': {e}")

    # --- Write Document Size Limit ---
    def get_max_write_bytes(self) -> int:
        self.load()
        return int(self.max_write_bytes)

    def set_max_write_bytes(self, val: int, save: bool = True) -> None:
        try:
            self.max_write_bytes = max(1, int(val))
            if save:
                self.save()
            logger.info(f"[SYSTEM_SETTINGS] Set max_write_bytes to {self.max_write_bytes} bytes.")
        except (ValueError, TypeError) as e:
            logger.error(f"[SYSTEM_SETTINGS] Invalid max_write_bytes '{val}': {e}")

    # --- RAG Settings ---
    def get_rag_config(self) -> dict[str, Any]:
        self.load()
        return copy.deepcopy(self.rag_config)

    def get_rag_setting(self, key: str, default: Any = None) -> Any:
        self.load()
        return copy.deepcopy(self.rag_config.get(key, default))

    def set_rag_setting(self, key: str, value: Any, save: bool = True) -> None:
        self.load()
        self.rag_config[key] = value
        if save:
            self.save()
        logger.info(f"[SYSTEM_SETTINGS] Set RAG setting '{key}' to {value}.")

    # --- Workspace Write Blacklist Additional & Protections ---
    def get_workspace_write_blacklist_additional(self) -> list[str]:
        self.load()
        return list(self.workspace_write_blacklist_additional)

    def set_workspace_write_blacklist_additional(self, paths: list[str], save: bool = True) -> None:
        self.workspace_write_blacklist_additional = [str(p).strip() for p in paths if str(p).strip()]
        if save:
            self.save()
        logger.info(
            f"[SYSTEM_SETTINGS] Set workspace_write_blacklist_additional to {self.workspace_write_blacklist_additional}."
        )

    # --- Strict Privacy Mode ---
    def is_strict_privacy_mode_enabled(self) -> bool:
        self.load()
        return bool(self.strict_privacy_mode_enabled)

    def set_strict_privacy_mode_enabled(self, enabled: bool, save: bool = True) -> None:
        self.strict_privacy_mode_enabled = bool(enabled)
        if save:
            self.save()
        logger.info(f"[SYSTEM_SETTINGS] Set strict_privacy_mode_enabled to {self.strict_privacy_mode_enabled}.")

    # --- Log Level ---
    def get_log_level(self) -> str:
        self.load()
        return self.log_level

    def set_log_level(self, level: str, save: bool = True) -> None:
        clean = str(level).strip().upper()
        if clean:
            self.log_level = clean
            if save:
                self.save()
            logger.info(f"[SYSTEM_SETTINGS] Set log_level to '{self.log_level}'.")

    # --- Pacing & Execution Timeout Settings ---
    def get_debate_turn_pacing_seconds(self) -> float:
        self.load()
        if not getattr(self, "_debate_turn_pacing_manually_set", False):
            env_val = os.getenv("IZVOR_DEBATE_TURN_PACING_SECONDS")
            if env_val is not None:
                try:
                    return max(0.0, float(env_val))
                except ValueError:
                    pass
        return self.debate_turn_pacing_seconds

    def set_debate_turn_pacing_seconds(self, seconds: float, save: bool = True) -> None:
        self.debate_turn_pacing_seconds = max(0.0, float(seconds))
        self._debate_turn_pacing_manually_set = True
        if save:
            self.save()
        logger.info(f"[SYSTEM_SETTINGS] Set debate_turn_pacing_seconds to {self.debate_turn_pacing_seconds}.")

    def get_slack_post_pause_seconds(self) -> float:
        self.load()
        if not getattr(self, "_slack_post_pause_manually_set", False):
            env_val = os.getenv("IZVOR_SLACK_POST_PAUSE_SECONDS")
            if env_val is not None:
                try:
                    return max(0.0, float(env_val))
                except ValueError:
                    pass
        return self.slack_post_pause_seconds

    def set_slack_post_pause_seconds(self, seconds: float, save: bool = True) -> None:
        self.slack_post_pause_seconds = max(0.0, float(seconds))
        self._slack_post_pause_manually_set = True
        if save:
            self.save()
        logger.info(f"[SYSTEM_SETTINGS] Set slack_post_pause_seconds to {self.slack_post_pause_seconds}.")

    def get_tool_call_loop_timeout_seconds(self) -> float:
        self.load()
        return self.tool_call_loop_timeout_seconds

    def set_tool_call_loop_timeout_seconds(self, seconds: float, save: bool = True) -> None:
        self.tool_call_loop_timeout_seconds = max(0.1, float(seconds))
        if save:
            self.save()
        logger.info(f"[SYSTEM_SETTINGS] Set tool_call_loop_timeout_seconds to {self.tool_call_loop_timeout_seconds}.")

    def get_max_discussion_turns(self) -> int:
        self.load()
        return self.max_discussion_turns

    def set_max_discussion_turns(self, val: int, save: bool = True) -> None:
        self.max_discussion_turns = max(1, int(val))
        if save:
            self.save()
        logger.info(f"[SYSTEM_SETTINGS] Set max_discussion_turns to {self.max_discussion_turns}.")

    def get_shared_evidence_max_chars(self) -> int:
        self.load()
        if not getattr(self, "_shared_evidence_max_chars_manually_set", False):
            env_val = os.getenv("IZVOR_SHARED_EVIDENCE_MAX_CHARS")
            if env_val is not None:
                try:
                    return max(100, int(env_val))
                except ValueError:
                    pass
        return int(self.shared_evidence_max_chars)

    def set_shared_evidence_max_chars(self, max_chars: int, save: bool = True) -> None:
        self.shared_evidence_max_chars = max(100, int(max_chars))
        self._shared_evidence_max_chars_manually_set = True
        if save:
            self.save()
        logger.info(f"[SYSTEM_SETTINGS] Set shared_evidence_max_chars to {self.shared_evidence_max_chars}.")

    def is_path_write_blocked(
        self,
        target_path: str | Path,
        base_dir: Path | None = None,
        custom_blacklist: list[str] | None = None,
    ) -> bool:
        """
        Determines whether writing to target_path is blocked by default protections
        (.env, .git, data/agent_settings.json, data/app_settings.json, data/system_settings.json) or
        the configured workspace_write_blacklist_additional.
        """
        if base_dir is None:
            base_dir = PROJECT_ROOT
        else:
            base_dir = Path(base_dir).resolve()

        if isinstance(target_path, str):
            clean_str = target_path.strip().replace("\\", "/")
            p_obj = Path(clean_str)
            target = p_obj if p_obj.is_absolute() else (base_dir / clean_str).resolve()
        else:
            target = target_path.resolve()

        try:
            rel_posix = target.relative_to(base_dir).as_posix()
        except ValueError:
            rel_posix = target.as_posix()

        target_name = target.name.lower()

        if target_name == ".env" or target_name.startswith(".env."):
            return True

        parts_lower = [part.lower() for part in target.parts]
        if ".git" in parts_lower:
            return True

        if target_name in ("agent_settings.json", "app_settings.json", "system_settings.json", "system_agent_settings.json"):
            return True

        if target.suffix.lower() == ".py":
            return True

        for d_path in DEFAULT_PROTECTED_WRITE_PATHS:
            try:
                d_resolved = (base_dir / d_path).resolve()
                if target == d_resolved or target.is_relative_to(d_resolved):
                    return True
            except (ValueError, OSError):
                pass

        blacklist = (
            custom_blacklist
            if custom_blacklist is not None
            else self.get_workspace_write_blacklist_additional()
        )
        for item in blacklist:
            clean_item = str(item).strip()
            if not clean_item:
                continue
            norm_item = clean_item.replace("\\", "/")
            if fnmatch.fnmatch(rel_posix, norm_item) or fnmatch.fnmatch(
                rel_posix, f"*/{norm_item}"
            ):
                return True
            if fnmatch.fnmatch(target_name, norm_item.lower()):
                return True
            try:
                item_resolved = (base_dir / clean_item).resolve()
                if target == item_resolved or target.is_relative_to(item_resolved):
                    return True
            except (ValueError, OSError):
                pass

        return False

    def get_guardrail_fail_mode(self) -> str:
        self.load()
        return self.guardrail_fail_mode

    def set_guardrail_fail_mode(self, mode: str, save: bool = True) -> None:
        clean_mode = str(mode).strip().lower()
        if clean_mode not in ("open", "closed"):
            raise ValueError(f"Invalid guardrail fail mode '{mode}'. Must be 'open' or 'closed'.")
        self.guardrail_fail_mode = clean_mode
        if save:
            self.save()

    def get_guardrail_chunk_chars(self) -> int:
        self.load()
        return int(self.guardrail_chunk_chars)

    def set_guardrail_chunk_chars(self, val: int, save: bool = True) -> None:
        self.guardrail_chunk_chars = max(100, int(val))
        if self.guardrail_chunk_overlap_chars >= self.guardrail_chunk_chars:
            self.guardrail_chunk_overlap_chars = max(0, self.guardrail_chunk_chars - 1)
        if save:
            self.save()

    def get_guardrail_chunk_overlap_chars(self) -> int:
        self.load()
        return int(self.guardrail_chunk_overlap_chars)

    def set_guardrail_chunk_overlap_chars(self, val: int, save: bool = True) -> None:
        overlap = max(0, int(val))
        self.guardrail_chunk_overlap_chars = min(overlap, max(0, self.guardrail_chunk_chars - 1))
        if save:
            self.save()

    def get_guardrail_max_chunks(self) -> int:
        self.load()
        return int(self.guardrail_max_chunks)

    def set_guardrail_max_chunks(self, val: int, save: bool = True) -> None:
        self.guardrail_max_chunks = max(1, int(val))
        if save:
            self.save()

    def get_guardrail_indirect_embedding_enabled(self) -> bool:
        self.load()
        return bool(self.guardrail_indirect_embedding_enabled)

    def set_guardrail_indirect_embedding_enabled(self, enabled: bool, save: bool = True) -> None:
        self.guardrail_indirect_embedding_enabled = bool(enabled)
        if save:
            self.save()

    def get_pii_ner_models(self) -> list[str]:
        self.load()
        return list(self.pii_ner_models)

    def set_pii_ner_models(self, models: list[str], save: bool = True) -> None:
        cleaned = [str(m).strip() for m in models if isinstance(m, str) and str(m).strip()]
        self.pii_ner_models = cleaned or ["en_core_web_sm"]
        if save:
            self.save()

    def get_audit_hmac_key_id(self) -> str:
        self.load()
        return str(self.audit_hmac_key_id or "default")

    def set_audit_hmac_key_id(self, key_id: str, save: bool = True) -> None:
        clean = str(key_id or "").strip() or "default"
        self.audit_hmac_key_id = clean
        if save:
            self.save()

    def get_max_tool_iterations(self) -> int:
        self.load()
        return int(self.max_tool_iterations)

    def get_tool_call_num_retries(self) -> int:
        self.load()
        return int(self.tool_call_num_retries)

    def get_word_limit_grace_buffer(self) -> int:
        self.load()
        return int(self.word_limit_grace_buffer)

    def get_output_constraints(self) -> dict[str, int]:
        self.load()
        return dict(self.output_constraints)

    def get_output_max_words(self, is_short: bool = False) -> int:
        self.load()
        key = "short_max_words" if is_short else "default_max_words"
        default = 100 if is_short else 200
        return _clamp_output_max_words(self.output_constraints.get(key), default)

    def set_output_constraints(
        self,
        default_max_words: int | None = None,
        short_max_words: int | None = None,
        save: bool = True,
    ) -> None:
        self.load()
        updated = dict(self.output_constraints)
        if default_max_words is not None:
            updated["default_max_words"] = _clamp_output_max_words(default_max_words, 200)
        if short_max_words is not None:
            updated["short_max_words"] = _clamp_output_max_words(short_max_words, 100)
        self.output_constraints = updated
        if save:
            self.save()
        logger.info(f"[SYSTEM_SETTINGS] Set output_constraints to {self.output_constraints}.")

    def get_system_agent_inference_timeout_seconds(self, env: Mapping[str, str] | None = None) -> float:
        self.load()
        active_env = env if env is not None else os.environ
        env_val = active_env.get("IZVOR_SYSTEM_AGENT_INFERENCE_TIMEOUT_SECONDS")
        if env_val is not None:
            try:
                return max(1.0, float(env_val))
            except ValueError:
                pass
        return float(self.system_agent_inference_timeout_seconds)

    def set_system_agent_inference_timeout_seconds(self, seconds: float, save: bool = True) -> None:
        self.system_agent_inference_timeout_seconds = max(1.0, float(seconds))
        if save:
            self.save()
        logger.info(f"[SYSTEM_SETTINGS] Set system_agent_inference_timeout_seconds to {self.system_agent_inference_timeout_seconds}.")

    def get_system_agent_inference_retry_timeout_multiplier(self, env: Mapping[str, str] | None = None) -> float:
        self.load()
        active_env = env if env is not None else os.environ
        env_val = active_env.get("IZVOR_SYSTEM_AGENT_INFERENCE_RETRY_MULTIPLIER")
        if env_val is not None:
            try:
                return max(1.0, float(env_val))
            except ValueError:
                pass
        return float(self.system_agent_inference_retry_timeout_multiplier)

    def set_system_agent_inference_retry_timeout_multiplier(self, multiplier: float, save: bool = True) -> None:
        self.system_agent_inference_retry_timeout_multiplier = max(1.0, float(multiplier))
        if save:
            self.save()
        logger.info(f"[SYSTEM_SETTINGS] Set system_agent_inference_retry_timeout_multiplier to {self.system_agent_inference_retry_timeout_multiplier}.")

    def get_guardrail_embedding_cache_size(self) -> int:
        self.load()
        return int(self.guardrail_embedding_cache_size)

    def get_thread_cleanup_interval_seconds(self) -> float:
        self.load()
        return float(self.thread_cleanup_interval_seconds)

    def get_audit_integrity_check_interval_seconds(self) -> float:
        self.load()
        return float(self.audit_integrity_check_interval_seconds)

    def get_skill_exclusions_sync_interval_seconds(self) -> float:
        self.load()
        return float(self.skill_exclusions_sync_interval_seconds)

    def set_skill_exclusions_sync_interval_seconds(self, seconds: float, save: bool = True) -> None:
        self.skill_exclusions_sync_interval_seconds = max(60.0, float(seconds))
        if save:
            self.save()
        logger.info(f"[SYSTEM_SETTINGS] Set skill_exclusions_sync_interval_seconds to {self.skill_exclusions_sync_interval_seconds}.")

    def get_pricing_cache_ttl_days(self) -> int:
        self.load()
        return int(self.pricing_cache_ttl_days)

    def get_pricing_cache_ttl_seconds(self) -> int:
        return self.get_pricing_cache_ttl_days() * 86400

    def get_slack_file_download_timeout_seconds(self) -> float:
        self.load()
        return float(self.slack_file_download_timeout_seconds)

    def get_web_search_timeout_seconds(self) -> float:
        self.load()
        return float(self.web_search_timeout_seconds)

    def get_web_fetch_timeout_seconds(self) -> float:
        self.load()
        return float(self.web_fetch_timeout_seconds)

    def get_db_busy_timeout_seconds(self) -> float:
        self.load()
        return float(self.db_busy_timeout_seconds)

    def get_llm_max_retries(self) -> int:
        self.load()
        return int(self.llm_max_retries)

    def get_llm_retry_backoff_base(self) -> float:
        self.load()
        return float(self.llm_retry_backoff_base)

    def get_slack_rate_limit_per_second(self) -> float:
        self.load()
        return float(self.slack_rate_limit_per_second)

    def get_slack_rate_limit_burst(self) -> float:
        self.load()
        return float(self.slack_rate_limit_burst)

    def get_pricing_benchmark_url(self) -> str:
        self.load()
        return str(self.pricing_benchmark_url)

    def get_web_fetch_user_agent(self) -> str:
        self.load()
        return str(self.web_fetch_user_agent)

    def get_web_security(self) -> dict[str, Any]:
        self.load()
        return copy.deepcopy(self.web_security)

    def get_security_burst_limits(self) -> dict[str, Any]:
        self.load()
        return copy.deepcopy(self.security_burst_limits)

    def get_dialectical_stress_test(self) -> dict[str, Any]:
        self.load()
        return copy.deepcopy(self.dialectical_stress_test)

    def get_convergence_consecutive_threshold(self) -> int:
        self.load()
        return int(self.dialectical_stress_test.get("consecutive_threshold", 3))

    def set_convergence_consecutive_threshold(self, val: int, save: bool = True) -> None:
        self.dialectical_stress_test["consecutive_threshold"] = max(1, int(val))
        if save:
            self.save()
        logger.info(f"[SYSTEM_SETTINGS] Set dialectical_stress_test consecutive_threshold to {self.dialectical_stress_test['consecutive_threshold']}.")

    def get_retention_checkpoints_days(self) -> int:
        self.load()
        return int(self.retention_checkpoints_days)

    def set_retention_checkpoints_days(self, days: int, save: bool = True) -> None:
        self.retention_checkpoints_days = max(1, int(days))
        if save:
            self.save()

    def get_retention_traces_days(self) -> int:
        self.load()
        return int(self.retention_traces_days)

    def set_retention_traces_days(self, days: int, save: bool = True) -> None:
        self.retention_traces_days = max(1, int(days))
        if save:
            self.save()

    def get_retention_event_dedup_days(self) -> int:
        self.load()
        return int(self.retention_event_dedup_days)

    def set_retention_event_dedup_days(self, days: int, save: bool = True) -> None:
        self.retention_event_dedup_days = max(1, int(days))
        if save:
            self.save()

    # --- MCP Servers Settings ---
    def get_mcp_servers(self, env: Mapping[str, str] | None = None) -> dict[str, McpServerConfig]:
        """Returns validated McpServerConfig objects with env vars expanded."""
        self.load()
        result: dict[str, McpServerConfig] = {}
        for name, raw_cfg in self.mcp_servers.items():
            if not isinstance(raw_cfg, dict):
                continue
            cfg_dict = _expand_env_vars_in_dict(raw_cfg, env=env)
            if "command" in cfg_dict and isinstance(cfg_dict["command"], str):
                cfg_dict["command"] = _expand_env_vars_in_str(cfg_dict["command"], env=env)
            if "url" in cfg_dict and isinstance(cfg_dict["url"], str):
                cfg_dict["url"] = _expand_env_vars_in_str(cfg_dict["url"], env=env)
            if "cwd" in cfg_dict and isinstance(cfg_dict["cwd"], str):
                cfg_dict["cwd"] = _expand_env_vars_in_str(cfg_dict["cwd"], env=env)
            if "args" in cfg_dict and isinstance(cfg_dict["args"], list):
                cfg_dict["args"] = [_expand_env_vars_in_str(a, env=env) if isinstance(a, str) else str(a) for a in cfg_dict["args"]]
            try:
                result[name] = McpServerConfig(**cfg_dict)
            except (ValidationError, ValueError, TypeError) as e:
                logger.warning(f"[SYSTEM_SETTINGS] Invalid MCP server config for '{name}': {e}")
        return result

    def get_mcp_server_config(self, server_name: str, env: Mapping[str, str] | None = None) -> McpServerConfig | None:
        """Returns the McpServerConfig for server_name or None if not found."""
        return self.get_mcp_servers(env=env).get(server_name)

    def set_mcp_servers(self, servers: dict[str, Any], save: bool = True) -> None:
        """Sets MCP servers dictionary and optionally persists to disk."""
        self.load()
        cleaned: dict[str, Any] = {}
        for k, v in servers.items():
            if isinstance(v, McpServerConfig):
                cleaned[k] = v.model_dump()
            elif isinstance(v, dict):
                cleaned[k] = copy.deepcopy(v)
        self.mcp_servers = cleaned
        if save:
            self.save()


system_settings_manager = SystemSettingsManager()


def get_system_settings_snapshot() -> SystemSettingsModel:
    return system_settings_manager.get_snapshot()


def get_max_file_size_mb() -> float:
    return system_settings_manager.get_max_file_size_mb()


def get_max_file_size_bytes() -> int:
    return system_settings_manager.get_max_file_size_bytes()


def get_max_write_bytes() -> int:
    return system_settings_manager.get_max_write_bytes()


def set_max_write_bytes(val: int, save: bool = True) -> None:
    system_settings_manager.set_max_write_bytes(val, save=save)


def set_max_file_size_mb(val: float, save: bool = True) -> None:
    system_settings_manager.set_max_file_size_mb(val, save=save)


def get_rag_config() -> dict[str, Any]:
    return system_settings_manager.get_rag_config()


def get_rag_setting(key: str, default: Any = None) -> Any:
    return system_settings_manager.get_rag_setting(key, default)


def set_rag_setting(key: str, value: Any, save: bool = True) -> None:
    system_settings_manager.set_rag_setting(key, value, save=save)


def get_workspace_write_blacklist_additional() -> list[str]:
    return system_settings_manager.get_workspace_write_blacklist_additional()


def set_workspace_write_blacklist_additional(paths: list[str], save: bool = True) -> None:
    system_settings_manager.set_workspace_write_blacklist_additional(paths, save=save)


def is_path_write_blocked(
    target_path: str | Path,
    base_dir: Path | None = None,
    custom_blacklist: list[str] | None = None,
) -> bool:
    return system_settings_manager.is_path_write_blocked(
        target_path, base_dir=base_dir, custom_blacklist=custom_blacklist
    )


def get_default_fast_model() -> str:
    return system_settings_manager.get_default_fast_model()


def set_default_fast_model(model: str, save: bool = True) -> None:
    system_settings_manager.set_default_fast_model(model, save=save)


def get_default_reasoning_model() -> str:
    return system_settings_manager.get_default_reasoning_model()


def set_default_reasoning_model(model: str, save: bool = True) -> None:
    system_settings_manager.set_default_reasoning_model(model, save=save)


def is_internal_revisor_enabled() -> bool:
    return system_settings_manager.get_internal_revisor_enabled()


def get_debate_turn_pacing_seconds() -> float:
    return system_settings_manager.get_debate_turn_pacing_seconds()


def get_slack_post_pause_seconds() -> float:
    return system_settings_manager.get_slack_post_pause_seconds()


def get_tool_call_loop_timeout_seconds() -> float:
    return system_settings_manager.get_tool_call_loop_timeout_seconds()


def get_guardrail_fail_mode() -> str:
    return system_settings_manager.get_guardrail_fail_mode()


def set_guardrail_fail_mode(mode: str, save: bool = True) -> None:
    system_settings_manager.set_guardrail_fail_mode(mode, save=save)


def get_guardrail_chunk_chars() -> int:
    return system_settings_manager.get_guardrail_chunk_chars()


def get_guardrail_chunk_overlap_chars() -> int:
    return system_settings_manager.get_guardrail_chunk_overlap_chars()


def get_guardrail_max_chunks() -> int:
    return system_settings_manager.get_guardrail_max_chunks()


def get_guardrail_indirect_embedding_enabled() -> bool:
    return system_settings_manager.get_guardrail_indirect_embedding_enabled()


def set_guardrail_indirect_embedding_enabled(enabled: bool, save: bool = True) -> None:
    system_settings_manager.set_guardrail_indirect_embedding_enabled(enabled, save=save)


def get_pii_ner_models() -> list[str]:
    return system_settings_manager.get_pii_ner_models()


def set_pii_ner_models(models: list[str], save: bool = True) -> None:
    system_settings_manager.set_pii_ner_models(models, save=save)


def get_audit_hmac_key_id() -> str:
    return system_settings_manager.get_audit_hmac_key_id()


def set_audit_hmac_key_id(key_id: str, save: bool = True) -> None:
    system_settings_manager.set_audit_hmac_key_id(key_id, save=save)


def get_max_discussion_turns() -> int:
    return system_settings_manager.get_max_discussion_turns()


def get_shared_evidence_max_chars() -> int:
    return system_settings_manager.get_shared_evidence_max_chars()


def set_shared_evidence_max_chars(max_chars: int, save: bool = True) -> None:
    system_settings_manager.set_shared_evidence_max_chars(max_chars, save=save)


def get_max_tool_iterations() -> int:
    return system_settings_manager.get_max_tool_iterations()


def get_tool_call_num_retries() -> int:
    return system_settings_manager.get_tool_call_num_retries()


def get_word_limit_grace_buffer() -> int:
    return system_settings_manager.get_word_limit_grace_buffer()


def get_output_constraints() -> dict[str, int]:
    return system_settings_manager.get_output_constraints()


def get_output_max_words(is_short: bool = False) -> int:
    return system_settings_manager.get_output_max_words(is_short=is_short)


def set_output_constraints(
    default_max_words: int | None = None,
    short_max_words: int | None = None,
    save: bool = True,
) -> None:
    system_settings_manager.set_output_constraints(
        default_max_words=default_max_words,
        short_max_words=short_max_words,
        save=save,
    )


def get_system_agent_inference_timeout_seconds(env: Mapping[str, str] | None = None) -> float:
    return system_settings_manager.get_system_agent_inference_timeout_seconds(env=env)


def get_system_agent_inference_retry_timeout_multiplier(env: Mapping[str, str] | None = None) -> float:
    return system_settings_manager.get_system_agent_inference_retry_timeout_multiplier(env=env)


def get_guardrail_embedding_cache_size() -> int:
    return system_settings_manager.get_guardrail_embedding_cache_size()


def get_thread_cleanup_interval_seconds() -> float:
    return system_settings_manager.get_thread_cleanup_interval_seconds()


def get_audit_integrity_check_interval_seconds() -> float:
    return system_settings_manager.get_audit_integrity_check_interval_seconds()


def get_skill_exclusions_sync_interval_seconds() -> float:
    return system_settings_manager.get_skill_exclusions_sync_interval_seconds()


def set_skill_exclusions_sync_interval_seconds(seconds: float, save: bool = True) -> None:
    system_settings_manager.set_skill_exclusions_sync_interval_seconds(seconds, save=save)


def get_pricing_cache_ttl_days() -> int:
    return system_settings_manager.get_pricing_cache_ttl_days()


def get_pricing_cache_ttl_seconds() -> int:
    return system_settings_manager.get_pricing_cache_ttl_seconds()


def get_slack_file_download_timeout_seconds() -> float:
    return system_settings_manager.get_slack_file_download_timeout_seconds()


def get_web_search_timeout_seconds() -> float:
    return system_settings_manager.get_web_search_timeout_seconds()


def get_web_fetch_timeout_seconds() -> float:
    return system_settings_manager.get_web_fetch_timeout_seconds()


def get_db_busy_timeout_seconds() -> float:
    return system_settings_manager.get_db_busy_timeout_seconds()


def get_llm_max_retries() -> int:
    return system_settings_manager.get_llm_max_retries()


def get_slack_rate_limit_per_second() -> float:
    return system_settings_manager.get_slack_rate_limit_per_second()


def get_slack_rate_limit_burst() -> float:
    return system_settings_manager.get_slack_rate_limit_burst()


def get_pricing_benchmark_url() -> str:
    return system_settings_manager.get_pricing_benchmark_url()


def get_web_fetch_user_agent() -> str:
    return system_settings_manager.get_web_fetch_user_agent()


def get_web_security() -> dict[str, Any]:
    return system_settings_manager.get_web_security()


def get_security_burst_limits() -> dict[str, Any]:
    return system_settings_manager.get_security_burst_limits()


def is_dialectical_stress_test_enabled() -> bool:
    system_settings_manager.load()
    return bool(system_settings_manager.dialectical_stress_test.get("enabled", True))


def get_convergence_consecutive_threshold() -> int:
    return system_settings_manager.get_convergence_consecutive_threshold()


def set_convergence_consecutive_threshold(val: int, save: bool = True) -> None:
    system_settings_manager.set_convergence_consecutive_threshold(val, save=save)


def get_retention_checkpoints_days() -> int:
    return system_settings_manager.get_retention_checkpoints_days()


def get_retention_traces_days() -> int:
    return system_settings_manager.get_retention_traces_days()


def get_retention_event_dedup_days() -> int:
    return system_settings_manager.get_retention_event_dedup_days()


def get_mcp_servers(env: Mapping[str, str] | None = None) -> dict[str, McpServerConfig]:
    return system_settings_manager.get_mcp_servers(env=env)


def get_mcp_server_config(server_name: str, env: Mapping[str, str] | None = None) -> McpServerConfig | None:
    return system_settings_manager.get_mcp_server_config(server_name, env=env)


def set_mcp_servers(servers: dict[str, Any], save: bool = True) -> None:
    system_settings_manager.set_mcp_servers(servers, save=save)
