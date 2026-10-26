import copy
import fnmatch
import logging
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from core.config import is_test_environment
from core.paths import SYSTEM_BACKUP_FILE, SYSTEM_SETTINGS_FILE
from orchestration.settings_store import JsonSettingsStore

logger = logging.getLogger(__name__)

BACKUP_INTERVAL_SECONDS = 7 * 86400

DEFAULT_PROTECTED_WRITE_PATHS: list[str] = [
    ".env",
    ".git",
    "audit",
    "system_resources",
    "data/settings/agent_settings.json",
    "data/settings/app_settings.json",
    "data/settings/system_settings.json",
]


class SystemSettingsModel(BaseModel):
    default_fast_model: str = "gemini/gemini-3.8-flash"
    default_reasoning_model: str = "mistral/mistral-large-latest"
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
    guardrail_embedding_cache_size: int = 500
    thread_cleanup_interval_seconds: float = 600.0
    audit_integrity_check_interval_seconds: float = 43200.0
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
    web_search_endpoint: str = "https://html.duckduckgo.com/html/"
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
            "max_fetches_per_turn": 10,
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
    convergence_consecutive_threshold: int = 2
    dialectical_stress_test: dict[str, Any] = Field(
        default_factory=lambda: {
            "enabled": True,
            "consecutive_threshold": 2,
            "max_stress_rounds": 1,
        }
    )
    retention_checkpoints_days: int = 7
    retention_traces_days: int = 30
    retention_event_dedup_days: int = 1

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
        self.max_file_size_mb: float = 50.0
        self.workspace_write_blacklist_additional: list[str] = []
        self.strict_privacy_mode_enabled: bool = True
        self.log_level: str = "INFO"
        self.debate_turn_pacing_seconds: float = 3.0
        self.slack_post_pause_seconds: float = 2.0
        self.tool_call_loop_timeout_seconds: float = 60.0
        self.guardrail_fail_mode: str = "open"
        self.max_discussion_turns: int = 20
        self.max_tool_iterations: int = 3
        self.tool_call_num_retries: int = 2
        self.word_limit_grace_buffer: int = 50
        self.guardrail_embedding_cache_size: int = 500
        self.thread_cleanup_interval_seconds: float = 600.0
        self.audit_integrity_check_interval_seconds: float = 43200.0
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
        self.web_search_endpoint: str = "https://html.duckduckgo.com/html/"
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
            "max_fetches_per_turn": 10,
        }
        self.security_burst_limits: dict[str, Any] = {
            "auth_failure_window_seconds": 60.0,
            "auth_failure_burst_limit": 5,
            "global_auth_failure_burst_limit": 25,
            "max_tracker_entries": 1000,
        }
        self.convergence_consecutive_threshold: int = 2
        self.dialectical_stress_test: dict[str, Any] = {
            "enabled": True,
            "consecutive_threshold": 2,
            "max_stress_rounds": 1,
        }
        self.retention_checkpoints_days: int = 7
        self.retention_traces_days: int = 30
        self.retention_event_dedup_days: int = 1
        self._debate_turn_pacing_manually_set: bool = False
        self._slack_post_pause_manually_set: bool = False
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

    @property
    def active_settings_file(self) -> Path:
        return self.default_settings_file

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

    def _apply_model_to_fields(self, model: SystemSettingsModel) -> None:
        self.default_fast_model = (
            str(model.default_fast_model).strip() or "gemini/gemini-3.8-flash"
        )
        self.default_reasoning_model = (
            str(model.default_reasoning_model).strip() or "mistral/mistral-large-latest"
        )
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
        self.guardrail_embedding_cache_size = int(model.guardrail_embedding_cache_size)
        self.thread_cleanup_interval_seconds = float(model.thread_cleanup_interval_seconds)
        self.audit_integrity_check_interval_seconds = float(model.audit_integrity_check_interval_seconds)
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
        self.web_search_endpoint = str(model.web_search_endpoint).strip() or "https://html.duckduckgo.com/html/"
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
                "max_fetches_per_turn": int(raw_ws.get("max_fetches_per_turn", 10)),
            }
        raw_sbl = model.security_burst_limits
        if isinstance(raw_sbl, dict):
            self.security_burst_limits = {
                "auth_failure_window_seconds": float(raw_sbl.get("auth_failure_window_seconds", 60.0)),
                "auth_failure_burst_limit": int(raw_sbl.get("auth_failure_burst_limit", 5)),
                "global_auth_failure_burst_limit": int(raw_sbl.get("global_auth_failure_burst_limit", 25)),
                "max_tracker_entries": int(raw_sbl.get("max_tracker_entries", 1000)),
            }
        self.convergence_consecutive_threshold = int(model.convergence_consecutive_threshold)
        raw_dst = model.dialectical_stress_test
        if isinstance(raw_dst, dict):
            self.dialectical_stress_test = {
                "enabled": bool(raw_dst.get("enabled", True)),
                "consecutive_threshold": int(raw_dst.get("consecutive_threshold", self.convergence_consecutive_threshold)),
                "max_stress_rounds": int(raw_dst.get("max_stress_rounds", 1)),
            }
        self.retention_checkpoints_days = int(getattr(model, "retention_checkpoints_days", 7))
        self.retention_traces_days = int(getattr(model, "retention_traces_days", 30))
        self.retention_event_dedup_days = int(getattr(model, "retention_event_dedup_days", 1))

    def load(self) -> None:
        target_file = self.active_settings_file
        if not target_file.exists():
            return

        try:
            mtime = target_file.stat().st_mtime
            if mtime == self._last_loaded_mtime and self._last_loaded_mtime > 0:
                return

            model = self._store.load(force=True)
            data = self._store.get_raw_data()

            from orchestration.integrity_checks import AppIntegrityValidator
            AppIntegrityValidator.check_system_settings_integrity(data)

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
            "max_file_size_mb": self.max_file_size_mb,
            "rag": self.rag_config,
            "_comment_workspace_write_blacklist_additional": (
                "Automatic hardcoded protection blocks writes to .env, .git, data/settings/agent_settings.json, "
                "data/settings/app_settings.json, and data/settings/system_settings.json. Add any additional relative workspace "
                "paths or file patterns here to prevent unauthorized modifications."
            ),
            "workspace_write_blacklist_additional": self.workspace_write_blacklist_additional,
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
            "guardrail_embedding_cache_size": self.guardrail_embedding_cache_size,
            "thread_cleanup_interval_seconds": self.thread_cleanup_interval_seconds,
            "audit_integrity_check_interval_seconds": self.audit_integrity_check_interval_seconds,
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
            "web_search_endpoint": self.web_search_endpoint,
            "web_fetch_user_agent": self.web_fetch_user_agent,
            "max_write_bytes": self.max_write_bytes,
            "web_security": self.web_security,
            "security_burst_limits": self.security_burst_limits,
            "convergence_consecutive_threshold": self.convergence_consecutive_threshold,
            "dialectical_stress_test": self.dialectical_stress_test,
            "retention_checkpoints_days": self.retention_checkpoints_days,
            "retention_traces_days": self.retention_traces_days,
            "retention_event_dedup_days": self.retention_event_dedup_days,
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
        return dict(self.rag_config)

    def get_rag_setting(self, key: str, default: Any = None) -> Any:
        self.load()
        return self.rag_config.get(key, default)

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
        if is_test_environment() and not self._debate_turn_pacing_manually_set:
            return 0.01
        return self.debate_turn_pacing_seconds

    def set_debate_turn_pacing_seconds(self, seconds: float, save: bool = True) -> None:
        self.debate_turn_pacing_seconds = max(0.0, float(seconds))
        self._debate_turn_pacing_manually_set = True
        if save:
            self.save()
        logger.info(f"[SYSTEM_SETTINGS] Set debate_turn_pacing_seconds to {self.debate_turn_pacing_seconds}.")

    def get_slack_post_pause_seconds(self) -> float:
        self.load()
        if is_test_environment() and not self._slack_post_pause_manually_set:
            return 0.01
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
            base_dir = Path(__file__).resolve().parent.parent
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

        # 1. Hardcoded default protections:
        # - .env or any .env.* file (e.g. .env, .env.local)
        if target_name == ".env" or target_name.startswith(".env."):
            return True

        # - .git directory or files inside .git
        parts_lower = [part.lower() for part in target.parts]
        if ".git" in parts_lower:
            return True

        # - agent_settings.json, app_settings.json, and system_settings.json
        if target_name in ("agent_settings.json", "app_settings.json", "system_settings.json"):
            return True

        # - Python source files (.py)
        if target.suffix.lower() == ".py":
            return True


        for d_path in DEFAULT_PROTECTED_WRITE_PATHS:
            try:
                d_resolved = (base_dir / d_path).resolve()
                if target == d_resolved or target.is_relative_to(d_resolved):
                    return True
            except (ValueError, OSError):
                pass

        # 2. Configurable workspace_write_blacklist_additional
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

    def get_max_tool_iterations(self) -> int:
        self.load()
        return int(self.max_tool_iterations)

    def get_tool_call_num_retries(self) -> int:
        self.load()
        return int(self.tool_call_num_retries)

    def get_word_limit_grace_buffer(self) -> int:
        self.load()
        return int(self.word_limit_grace_buffer)

    def get_guardrail_embedding_cache_size(self) -> int:
        self.load()
        return int(self.guardrail_embedding_cache_size)

    def get_thread_cleanup_interval_seconds(self) -> float:
        self.load()
        return float(self.thread_cleanup_interval_seconds)

    def get_audit_integrity_check_interval_seconds(self) -> float:
        self.load()
        return float(self.audit_integrity_check_interval_seconds)

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

    def get_web_search_endpoint(self) -> str:
        self.load()
        return str(self.web_search_endpoint)

    def get_web_fetch_user_agent(self) -> str:
        self.load()
        return str(self.web_fetch_user_agent)

    def get_web_security(self) -> dict[str, Any]:
        self.load()
        return dict(self.web_security)

    def get_security_burst_limits(self) -> dict[str, Any]:
        self.load()
        return dict(self.security_burst_limits)

    def get_convergence_consecutive_threshold(self) -> int:
        self.load()
        return int(self.convergence_consecutive_threshold)

    def set_convergence_consecutive_threshold(self, val: int, save: bool = True) -> None:
        self.convergence_consecutive_threshold = max(1, int(val))
        if save:
            self.save()
        logger.info(f"[SYSTEM_SETTINGS] Set convergence_consecutive_threshold to {self.convergence_consecutive_threshold}.")

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

    def get_challenger_agents(self) -> list[str]:
        from orchestration.agent_settings import get_challenger_agents as _get
        return _get()

    def set_challenger_agents(self, agents: list[str], save: bool = True) -> None:
        from orchestration.agent_settings import set_challenger_agents as _set
        _set(agents, save=save)

    def get_rebuttal_agent(self) -> str:
        from orchestration.agent_settings import get_rebuttal_agent as _get
        return _get()

    def set_rebuttal_agent(self, agent: str, save: bool = True) -> None:
        from orchestration.agent_settings import set_rebuttal_agent as _set
        _set(agent, save=save)


system_settings_manager = SystemSettingsManager()


# Module-level convenience functions
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


def is_strict_privacy_mode_enabled() -> bool:
    return system_settings_manager.is_strict_privacy_mode_enabled()


def set_strict_privacy_mode_enabled(enabled: bool, save: bool = True) -> None:
    system_settings_manager.set_strict_privacy_mode_enabled(enabled, save=save)


def get_log_level() -> str:
    return system_settings_manager.get_log_level()


def set_log_level(level: str, save: bool = True) -> None:
    system_settings_manager.set_log_level(level, save=save)


def get_debate_turn_pacing_seconds() -> float:
    return system_settings_manager.get_debate_turn_pacing_seconds()


def set_debate_turn_pacing_seconds(seconds: float, save: bool = True) -> None:
    system_settings_manager.set_debate_turn_pacing_seconds(seconds, save=save)


def get_slack_post_pause_seconds() -> float:
    return system_settings_manager.get_slack_post_pause_seconds()


def set_slack_post_pause_seconds(seconds: float, save: bool = True) -> None:
    system_settings_manager.set_slack_post_pause_seconds(seconds, save=save)


def get_tool_call_loop_timeout_seconds() -> float:
    return system_settings_manager.get_tool_call_loop_timeout_seconds()


def set_tool_call_loop_timeout_seconds(seconds: float, save: bool = True) -> None:
    system_settings_manager.set_tool_call_loop_timeout_seconds(seconds, save=save)


def get_guardrail_fail_mode() -> str:
    return system_settings_manager.get_guardrail_fail_mode()


def set_guardrail_fail_mode(mode: str, save: bool = True) -> None:
    system_settings_manager.set_guardrail_fail_mode(mode, save=save)


def get_max_discussion_turns() -> int:
    return system_settings_manager.get_max_discussion_turns()


def set_max_discussion_turns(val: int, save: bool = True) -> None:
    system_settings_manager.set_max_discussion_turns(val, save=save)


def get_max_tool_iterations() -> int:
    return system_settings_manager.get_max_tool_iterations()


def get_tool_call_num_retries() -> int:
    return system_settings_manager.get_tool_call_num_retries()


def get_word_limit_grace_buffer() -> int:
    return system_settings_manager.get_word_limit_grace_buffer()


def get_guardrail_embedding_cache_size() -> int:
    return system_settings_manager.get_guardrail_embedding_cache_size()


def get_thread_cleanup_interval_seconds() -> float:
    return system_settings_manager.get_thread_cleanup_interval_seconds()


def get_audit_integrity_check_interval_seconds() -> float:
    return system_settings_manager.get_audit_integrity_check_interval_seconds()


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


def get_llm_retry_backoff_base() -> float:
    return system_settings_manager.get_llm_retry_backoff_base()


def get_slack_rate_limit_per_second() -> float:
    return system_settings_manager.get_slack_rate_limit_per_second()


def get_slack_rate_limit_burst() -> float:
    return system_settings_manager.get_slack_rate_limit_burst()


def get_pricing_benchmark_url() -> str:
    return system_settings_manager.get_pricing_benchmark_url()


def get_web_search_endpoint() -> str:
    return system_settings_manager.get_web_search_endpoint()


def get_web_fetch_user_agent() -> str:
    return system_settings_manager.get_web_fetch_user_agent()


def get_web_security() -> dict[str, Any]:
    return system_settings_manager.get_web_security()


def get_security_burst_limits() -> dict[str, Any]:
    return system_settings_manager.get_security_burst_limits()


def get_dialectical_stress_test_config() -> dict[str, Any]:
    from orchestration.agent_settings import get_challenger_agents as _get_challengers, get_rebuttal_agent as _get_rebuttal
    system_settings_manager.load()
    cfg = dict(system_settings_manager.dialectical_stress_test)
    challengers = _get_challengers()
    rebuttal = _get_rebuttal()
    cfg["challenger_agents"] = challengers
    cfg["challenger_agent"] = challengers[0] if challengers else ""
    cfg["rebuttal_agent"] = rebuttal
    return cfg


def is_dialectical_stress_test_enabled() -> bool:
    system_settings_manager.load()
    return bool(system_settings_manager.dialectical_stress_test.get("enabled", True))


def get_convergence_consecutive_threshold() -> int:
    return system_settings_manager.get_convergence_consecutive_threshold()


def set_convergence_consecutive_threshold(val: int, save: bool = True) -> None:
    system_settings_manager.set_convergence_consecutive_threshold(val, save=save)


def get_challenger_agents() -> list[str]:
    from orchestration.agent_settings import get_challenger_agents as _get
    return _get()


def set_challenger_agents(agents: list[str], save: bool = True) -> None:
    from orchestration.agent_settings import set_challenger_agents as _set
    _set(agents, save=save)


def get_rebuttal_agent() -> str:
    from orchestration.agent_settings import get_rebuttal_agent as _get
    return _get()


def set_rebuttal_agent(agent: str, save: bool = True) -> None:
    from orchestration.agent_settings import set_rebuttal_agent as _set
    _set(agent, save=save)


def get_retention_checkpoints_days() -> int:
    return system_settings_manager.get_retention_checkpoints_days()


def get_retention_traces_days() -> int:
    return system_settings_manager.get_retention_traces_days()


def get_retention_event_dedup_days() -> int:
    return system_settings_manager.get_retention_event_dedup_days()





