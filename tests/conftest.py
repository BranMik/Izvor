import copy
import inspect
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from config_store.agent_settings import (
    BACKUP_FILE as REAL_BACKUP_FILE,
    SETTINGS_FILE as REAL_SETTINGS_FILE,
    SYSTEM_AGENT_BACKUP_FILE as REAL_SYSTEM_AGENT_BACKUP_FILE,
    SYSTEM_AGENT_SETTINGS_FILE as REAL_SYSTEM_AGENT_SETTINGS_FILE,
    register_on_agents_changed,
    set_prompt_builder_hook,
    settings_manager,
)
from core.llm_utils import (
    configure_default_llm_guard,
    reset_default_llm_guard,
    reset_llm_completion_fn,
    reset_llm_sleep_fn,
    set_pii_masker_instance,
)
from core.paths import (
    PRESIDIO_SKILL_EXCLUSIONS_FILE as REAL_EXCLUSIONS_FILE,
    PRICING_CACHE_FILE as REAL_PRICING_CACHE_FILE,
    WORKFLOWS_DIR as REAL_WORKFLOWS_DIR,
)
from core.registry import (
    register_token_recorder,
    set_default_workspace_adapter_factory,
    set_initial_agents_provider,
    set_thread_state_provider,
)
from core.slack_security import set_audit_logger_hook, set_secret_scanner_hook
from prompts.builder import build_agent_prompt
from services.audit_logger import audit_logger, log_audit_event
from services.retention_service import register_thread_purge_hook
from services.security import scan_output_for_secrets
from services.security.pii_masker import pii_masker
from skills.registry import skill_registry


def _lazy_record_thread_tokens(*args, **kwargs):
    from agents.usage_agent import record_thread_tokens

    return record_thread_tokens(*args, **kwargs)


def _lazy_cleanup_thread_usage(*args, **kwargs):
    from agents.usage_agent import cleanup_thread_usage

    return cleanup_thread_usage(*args, **kwargs)


def _lazy_clear_thread_note(*args, **kwargs):
    from agents.thread_notes_agent import clear_thread_note

    return clear_thread_note(*args, **kwargs)


def _lazy_get_initial_agents(*args, **kwargs):
    from orchestration.nomination import get_initial_agents

    return get_initial_agents(*args, **kwargs)


def _lazy_thread_state_provider():
    from orchestration.thread_state import thread_state_manager

    return thread_state_manager


def _lazy_workspace_adapter(*args, **kwargs):
    from adapters.slack.workspace_adapter import SlackWorkspaceAdapter

    return SlackWorkspaceAdapter(*args, **kwargs)


def _lazy_cleanup_thread_state(channel_id: str, thread_ts: str, *, skip_task=None):
    from orchestration.thread_state import thread_state_manager

    return thread_state_manager.cleanup_thread(channel_id, thread_ts, skip_task=skip_task)


def _wire_default_ioc_hooks() -> None:
    """Wires default IoC hooks for tests without eagerly importing heavy runtime modules."""
    set_audit_logger_hook(log_audit_event)
    set_secret_scanner_hook(scan_output_for_secrets)
    set_pii_masker_instance(pii_masker)
    set_prompt_builder_hook(build_agent_prompt)
    register_on_agents_changed(pii_masker.refresh_allowlist)
    set_default_workspace_adapter_factory(_lazy_workspace_adapter)
    register_token_recorder(_lazy_record_thread_tokens)
    set_initial_agents_provider(_lazy_get_initial_agents)
    set_thread_state_provider(_lazy_thread_state_provider)
    register_thread_purge_hook("usage_tracker", _lazy_cleanup_thread_usage)
    register_thread_purge_hook("thread_notes", _lazy_clear_thread_note)
    register_thread_purge_hook("thread_state", _lazy_cleanup_thread_state)
    skill_registry.ensure_loaded()


_wire_default_ioc_hooks()


def _read_optional_template(path: Path) -> tuple[bytes, int, int] | None:
    if not path.exists():
        return None
    st = path.stat()
    return path.read_bytes(), st.st_atime_ns, st.st_mtime_ns


def _write_template(dest: Path, template: tuple[bytes, int, int] | None) -> None:
    if template is None:
        return
    data, atime_ns, mtime_ns = template
    dest.write_bytes(data)
    os.utime(dest, ns=(atime_ns, mtime_ns))


@pytest.fixture(scope="session")
def _session_config_templates():
    """
    Reads immutable baseline config/state JSON files (and timestamps) once per pytest
    worker session and restores canonical manager paths once at session teardown.
    """
    from config_store.app_settings import (
        APP_BACKUP_FILE as REAL_APP_BACKUP_FILE,
        APP_SETTINGS_FILE as REAL_APP_SETTINGS_FILE,
        app_settings_manager,
    )
    from config_store.system_settings import (
        SYSTEM_BACKUP_FILE as REAL_SYS_BACKUP_FILE,
        SYSTEM_SETTINGS_FILE as REAL_SYS_SETTINGS_FILE,
        system_settings_manager,
    )
    from services.pricing_service import pricing_service

    sys_agent_settings_tpl = _read_optional_template(REAL_SYSTEM_AGENT_SETTINGS_FILE)
    sys_agent_backup_tpl = _read_optional_template(REAL_SYSTEM_AGENT_BACKUP_FILE)

    workflow_files: dict[str, tuple[bytes, int, int]] = {}
    wf_dir = REAL_WORKFLOWS_DIR
    if wf_dir.exists():
        for wf_file in wf_dir.glob("*.json"):
            tpl = _read_optional_template(wf_file)
            if tpl is not None:
                workflow_files[wf_file.name] = tpl

    templates = {
        "pricing_cache": _read_optional_template(REAL_PRICING_CACHE_FILE),
        "agent_settings": _read_optional_template(REAL_SETTINGS_FILE),
        "agent_backup": _read_optional_template(REAL_BACKUP_FILE),
        "sys_agent_settings": sys_agent_settings_tpl,
        "sys_agent_backup": sys_agent_backup_tpl,
        "workflows": workflow_files,
        "app_settings": _read_optional_template(REAL_APP_SETTINGS_FILE),
        "app_backup": _read_optional_template(REAL_APP_BACKUP_FILE),
        "system_settings": _read_optional_template(REAL_SYS_SETTINGS_FILE),
        "system_backup": _read_optional_template(REAL_SYS_BACKUP_FILE),
        "exclusions": _read_optional_template(REAL_EXCLUSIONS_FILE),
    }

    yield templates

    audit_logger.reset_paths()
    audit_logger.invalidate_cache()
    pricing_service.reset_paths()
    settings_manager.reset_paths()
    app_settings_manager.reset_paths()
    system_settings_manager.reset_paths()
    pii_masker.reset_paths()
    if "services.security.guardrail" in sys.modules:
        from services.security.guardrail import guardrail_service

        guardrail_service.reset_paths()


@pytest.fixture(autouse=True)
def scope_presidio_ner_for_tests(request):
    """
    Disables heavy spaCy/Presidio NER model loading by default for unit/integration tests,
    enabling it automatically for tests marked with `@pytest.mark.presidio`.
    """
    use_ner = request.node.get_closest_marker("presidio") is not None
    pii_masker.configure_ner_enabled(use_ner)
    try:
        yield
    finally:
        pii_masker.configure_ner_enabled(False)


@pytest.fixture(autouse=True)
def isolate_audit_logger_for_tests(tmp_path):
    """
    Bulletproof isolation of the AuditLogger.
    Forces all audit logging during pytest runs to write to a temporary directory.
    """
    test_audit_dir = tmp_path / "audit"
    test_audit_dir.mkdir(parents=True, exist_ok=True)

    audit_logger.configure_paths(audit_dir=test_audit_dir)
    audit_logger.invalidate_cache()

    yield

    audit_logger.invalidate_cache()


@pytest.fixture(autouse=True)
def isolate_pricing_cache_for_tests(tmp_path, _session_config_templates):
    """
    Prevents tests from modifying or overwriting production data/pricing/model_pricing_cache.json.
    Each test gets an isolated copy in tmp_path / "_conftest_iso".
    """
    from services.pricing_service import pricing_service

    iso_dir = tmp_path / "_conftest_iso"
    iso_dir.mkdir(parents=True, exist_ok=True)
    temp_pricing_cache = iso_dir / "model_pricing_cache.json"
    _write_template(temp_pricing_cache, _session_config_templates["pricing_cache"])

    pricing_service.configure_paths(cache_file=temp_pricing_cache)
    yield


@pytest.fixture(autouse=True)
def isolate_agent_settings_for_tests(tmp_path, _session_config_templates):
    """
    Prevents tests from modifying or overwriting config/settings/agent_settings.json,
    engine/settings/system_agent_settings.json, and config/workflows/*.json.
    Each test gets an isolated copy in tmp_path / "_conftest_iso".
    """
    iso_dir = tmp_path / "_conftest_iso"
    iso_dir.mkdir(parents=True, exist_ok=True)
    temp_settings = iso_dir / "agent_settings.json"
    temp_backup = iso_dir / "agent_settings_backup.json"
    temp_sys_settings = iso_dir / "system_agent_settings.json"
    temp_sys_backup = iso_dir / "system_agent_settings_backup.json"

    _write_template(temp_settings, _session_config_templates["agent_settings"])
    _write_template(temp_backup, _session_config_templates["agent_backup"])
    _write_template(temp_sys_settings, _session_config_templates["sys_agent_settings"])
    _write_template(temp_sys_backup, _session_config_templates["sys_agent_backup"])

    temp_workflows_dir = iso_dir / "workflows"
    temp_workflows_dir.mkdir(parents=True, exist_ok=True)
    for wf_name, wf_tpl in _session_config_templates["workflows"].items():
        _write_template(temp_workflows_dir / wf_name, wf_tpl)

    orig_active_wf_id = settings_manager.active_workflow_id

    settings_manager.configure_paths(
        settings_file=temp_settings,
        backup_file=temp_backup,
        system_agent_settings_file=temp_sys_settings,
        system_agent_backup_file=temp_sys_backup,
        workflows_dir=temp_workflows_dir,
    )

    orig_enabled = set(settings_manager.enabled_agents)
    orig_orchestration_mode = settings_manager.orchestration_mode
    orig_sandwich_start = list(settings_manager.sandwich_start_chain)
    orig_sandwich_end = list(settings_manager.sandwich_end_chain)
    orig_fixed = list(settings_manager.fixed_chain)
    orig_agents_config = copy.deepcopy(settings_manager.agents_config)
    orig_system_agents = copy.deepcopy(settings_manager.system_agents_config)
    orig_orchestrator = copy.deepcopy(settings_manager.orchestrator_config)

    yield

    settings_manager.enabled_agents.clear()
    settings_manager.enabled_agents.update(orig_enabled)
    settings_manager.orchestration_mode = orig_orchestration_mode
    settings_manager.sandwich_start_chain.clear()
    settings_manager.sandwich_start_chain.extend(orig_sandwich_start)
    settings_manager.sandwich_end_chain.clear()
    settings_manager.sandwich_end_chain.extend(orig_sandwich_end)
    settings_manager.fixed_chain.clear()
    settings_manager.fixed_chain.extend(orig_fixed)
    settings_manager.agents_config.clear()
    settings_manager.agents_config.update(orig_agents_config)
    settings_manager.system_agents_config.clear()
    settings_manager.system_agents_config.update(orig_system_agents)
    settings_manager.orchestrator_config.clear()
    settings_manager.orchestrator_config.update(orig_orchestrator)
    settings_manager.active_workflow_id = orig_active_wf_id


@pytest.fixture(autouse=True)
def isolate_app_settings_for_tests(tmp_path, _session_config_templates):
    """
    Prevents tests from modifying or overwriting production data/state/app_settings.json.
    Each test gets an isolated copy in tmp_path / "_conftest_iso".
    """
    from config_store.app_settings import app_settings_manager

    iso_dir = tmp_path / "_conftest_iso"
    iso_dir.mkdir(parents=True, exist_ok=True)
    temp_app_settings = iso_dir / "app_settings.json"
    temp_app_backup = iso_dir / "app_settings_backup.json"

    _write_template(temp_app_settings, _session_config_templates["app_settings"])
    _write_template(temp_app_backup, _session_config_templates["app_backup"])

    orig_expiration = app_settings_manager.thread_skill_expiration_time
    orig_cb_enabled = app_settings_manager.circuit_breaker_enabled
    orig_cb_threshold = app_settings_manager.circuit_breaker_threshold_usd
    orig_daily_budget = app_settings_manager.daily_budget_usd
    orig_scribe_enabled = app_settings_manager.scribe_enabled
    orig_weekly_pricing = app_settings_manager.weekly_pricing_update_enabled
    orig_workspace_context = app_settings_manager.workspace_context
    orig_convergence_ff = app_settings_manager.convergence_fast_forward_enabled
    orig_filter_unauth = app_settings_manager.filter_unauthorized_skill_agents_enabled

    app_settings_manager.configure_paths(
        settings_file=temp_app_settings,
        backup_file=temp_app_backup,
    )

    yield

    app_settings_manager.thread_skill_expiration_time = orig_expiration
    app_settings_manager.circuit_breaker_enabled = orig_cb_enabled
    app_settings_manager.circuit_breaker_threshold_usd = orig_cb_threshold
    app_settings_manager.daily_budget_usd = orig_daily_budget
    app_settings_manager.scribe_enabled = orig_scribe_enabled
    app_settings_manager.weekly_pricing_update_enabled = orig_weekly_pricing
    app_settings_manager.workspace_context = orig_workspace_context
    app_settings_manager.convergence_fast_forward_enabled = orig_convergence_ff
    app_settings_manager.filter_unauthorized_skill_agents_enabled = orig_filter_unauth


class _UnmockedSlackClientGuard:
    """Fail-closed guard preventing unmocked Slack SDK calls during unit/integration pytest runs."""

    def __getattr__(self, name: str):
        raise RuntimeError(
            f"Real Slack API call '{name}' blocked during pytest run: "
            "call configure_slack_async_client(...) with a mock first."
        )


@pytest.fixture(scope="session")
def _session_template_db(tmp_path_factory):
    """
    Creates ONE pre-migrated SQLite WAL template database once per pytest worker session
    so individual tests copy a ready-to-use schema instead of executing all DDL migrations.
    """
    from core.database import db_manager

    template_dir = tmp_path_factory.mktemp("db_template")
    template_path = template_dir / "template_izvor.db"
    db_manager.configure_paths(db_path=template_path)
    with db_manager.get_connection() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    db_manager.close_all_connections()

    yield template_path

    db_manager.reset_paths()


@pytest.fixture(autouse=True)
def isolate_database_for_tests(tmp_path, _session_template_db):
    """
    Bulletproof SQLite database isolation for tests.
    Each test gets a dedicated temporary SQLite database copied from the session template.
    """
    from core.database import db_manager

    iso_dir = tmp_path / "_conftest_iso"
    iso_dir.mkdir(parents=True, exist_ok=True)
    test_db_path = iso_dir / "test_izvor.db"
    shutil.copy2(_session_template_db, test_db_path)
    db_manager.configure_paths(db_path=test_db_path)

    yield test_db_path

    db_manager.close_all_connections()


@pytest.fixture(autouse=True)
def isolate_thread_state_files_for_tests(isolate_database_for_tests):
    """
    Ensures clean thread skills state and thread usage state in SQLite,
    cancels dangling background notes/usage tasks, and guards against wiping production data/izvor.db.
    """
    from core.database import db_manager
    from core.paths import DB_PATH as REAL_DB_PATH
    from core.task_utils import safe_cancel_task

    if "agents.usage_agent" in sys.modules:
        sys.modules["agents.usage_agent"].usage_tracker.clear_all()

    yield

    if "agents.thread_notes_agent" in sys.modules:
        thread_notes_agent = sys.modules["agents.thread_notes_agent"]
        for task in list(thread_notes_agent._RUNNING_TASKS.values()):
            safe_cancel_task(task)
        thread_notes_agent._RUNNING_TASKS.clear()
        thread_notes_agent._PENDING_UPDATES.clear()
        thread_notes_agent._PENDING_COMPLETED.clear()
        thread_notes_agent.THREAD_LOCKS.clear()

    leaked_prod_db = db_manager.db_path.resolve() == REAL_DB_PATH.resolve()
    if leaked_prod_db:
        db_manager.configure_paths(db_path=isolate_database_for_tests)

    if "agents.usage_agent" in sys.modules:
        sys.modules["agents.usage_agent"].usage_tracker.clear_all()

    if leaked_prod_db:
        raise AssertionError(
            "Test reset db_manager back to production data/izvor.db; "
            "restore the isolated test db path instead of calling db_manager.reset_paths()."
        )


@pytest.fixture(autouse=True)
def isolate_mcp_client_for_tests():
    """
    Prevents tests from leaking custom MCP transport factories,
    cached tool discoveries, or custom MCP servers.
    """
    from config_store.system_settings import system_settings_manager

    old_mcp_servers = dict(system_settings_manager.mcp_servers)
    if "services.mcp.client_manager" in sys.modules:
        sys.modules["services.mcp.client_manager"].mcp_client_manager.reset_transport_factory()

    yield

    if "services.mcp.client_manager" in sys.modules:
        sys.modules["services.mcp.client_manager"].mcp_client_manager.reset_transport_factory()
    system_settings_manager.mcp_servers = old_mcp_servers


@pytest.fixture(autouse=True)
def mock_embedding_service_for_tests():
    from services.rag.config import EMBEDDING_DIM
    from services.rag.embeddings import EmbeddingService

    async def dummy_get_embeddings(texts):
        if not texts:
            return []
        return [[0.0] * EMBEDDING_DIM for _ in texts]

    EmbeddingService.configure_embeddings_fn(dummy_get_embeddings)
    try:
        yield
    finally:
        EmbeddingService.reset_embeddings_fn()


@pytest.fixture(autouse=True)
def isolate_guardrail_for_tests(tmp_path):
    """Isolates guardrail LanceDB path per test so tests never touch production RAG cache."""
    from services.security.guardrail import guardrail_service

    iso_dir = tmp_path / "_conftest_iso" / "lancedb"
    iso_dir.mkdir(parents=True, exist_ok=True)
    guardrail_service.configure_paths(db_dir=iso_dir)
    yield
    guardrail_service.clear_cache()


@pytest.fixture(autouse=True)
def isolate_system_settings_for_tests(tmp_path, _session_config_templates):
    """
    Prevents tests from modifying config/settings/system_settings.json
    and data/backups/system_settings_backup.json.
    """
    from config_store.system_settings import system_settings_manager

    iso_dir = tmp_path / "_conftest_iso"
    iso_dir.mkdir(parents=True, exist_ok=True)
    temp_sys_settings = iso_dir / "system_settings.json"
    temp_sys_backup = iso_dir / "system_settings_backup.json"

    _write_template(temp_sys_settings, _session_config_templates["system_settings"])
    _write_template(temp_sys_backup, _session_config_templates["system_backup"])

    system_settings_manager.configure_paths(
        settings_file=temp_sys_settings,
        backup_file=temp_sys_backup,
    )

    yield


@pytest.fixture(autouse=True)
def isolate_pii_masker_exclusions_for_tests(tmp_path, _session_config_templates):
    """
    Prevents tests from modifying or overwriting production data/state/presidio_skill_exclusions.json.
    Each test gets an isolated copy in tmp_path / "_conftest_iso".
    """
    iso_dir = tmp_path / "_conftest_iso"
    iso_dir.mkdir(parents=True, exist_ok=True)
    temp_exclusions = iso_dir / "presidio_skill_exclusions.json"

    _write_template(temp_exclusions, _session_config_templates["exclusions"])

    pii_masker.configure_paths(exclusions_file=temp_exclusions)

    yield


@pytest.fixture(autouse=True)
def isolate_downloads_dir_for_tests(request, tmp_path):
    """
    Prevents tests from leaking downloaded test files into data/downloads.
    """
    if "test_paths" in request.node.nodeid:
        yield
        return
    from core.paths import set_downloads_dir_override

    iso_dir = tmp_path / "_conftest_iso"
    iso_dir.mkdir(parents=True, exist_ok=True)
    temp_downloads = iso_dir / "downloads"
    temp_downloads.mkdir(parents=True, exist_ok=True)

    set_downloads_dir_override(temp_downloads)
    yield
    set_downloads_dir_override(None)


@pytest.fixture(autouse=True)
def isolate_flat_histories_and_notes_for_tests(tmp_path):
    """
    Prevents tests from creating flat history and thread note files in real directories.
    Provides fast pacing for multi-turn execution without production test seams.
    """
    from config_store.system_settings import system_settings_manager
    from services.flat_history_recorder import configure_paths as configure_flat_paths, reset_paths as reset_flat_paths
    from services.thread_notes_writer import configure_paths as configure_note_paths, reset_paths as reset_note_paths

    iso_dir = tmp_path / "_conftest_iso"
    iso_dir.mkdir(parents=True, exist_ok=True)
    temp_flat = iso_dir / "flat-histories"
    temp_flat.mkdir(parents=True, exist_ok=True)
    temp_notes = iso_dir / "thread_notes"
    temp_notes.mkdir(parents=True, exist_ok=True)

    orig_pacing = system_settings_manager.debate_turn_pacing_seconds
    orig_pause = system_settings_manager.slack_post_pause_seconds

    system_settings_manager.set_debate_turn_pacing_seconds(0.01, save=False)
    system_settings_manager.set_slack_post_pause_seconds(0.01, save=False)

    configure_flat_paths(temp_flat)
    configure_note_paths(temp_notes)

    yield

    reset_flat_paths()
    reset_note_paths()
    system_settings_manager.set_debate_turn_pacing_seconds(orig_pacing, save=False)
    system_settings_manager.set_slack_post_pause_seconds(orig_pause, save=False)


@pytest.fixture(scope="session", autouse=True)
def isolate_workspace_tests_for_tests():
    """
    Cleans up any scratch or temporary test files written into agents_resources or workspace
    once per worker session (avoiding cross-worker unlink races under pytest-xdist).
    """
    yield
    from core.paths import AGENTS_RESOURCES_DIR, PROJECT_ROOT

    for base_d in (AGENTS_RESOURCES_DIR, PROJECT_ROOT):
        t_dir = base_d / "tests"
        if t_dir.exists():
            for f in t_dir.glob("*.tmp"):
                try:
                    f.unlink(missing_ok=True)
                except OSError:
                    pass
            for f in t_dir.glob("*scratch*"):
                try:
                    f.unlink(missing_ok=True)
                except OSError:
                    pass


def make_mock_acompletion(agent_turn_handler=None, default_content="Response text"):
    """
    Creates an AsyncMock for litellm.acompletion that automatically handles
    system agent calls (Language Detector, Intent Extractor, Internal Revisor, Discussion Scribe)
    while routing debate agent calls to `agent_turn_handler` or returning a standard mock choice.
    """
    async def _mock_acompletion(*args, **kwargs):
        messages = kwargs.get("messages") or (
            args[0].get("messages")
            if args and isinstance(args[0], dict)
            else (args[2] if len(args) > 2 else [])
        )
        sys_content = ""
        for m in messages:
            if isinstance(m, dict) and m.get("role") == "system":
                sys_content += str(m.get("content", ""))

        if "language_detector" in sys_content.lower() or "language identification" in sys_content.lower():
            m = MagicMock()
            m.message.content = "English"
            m.message.tool_calls = None
            return MagicMock(choices=[m], usage=None)

        if "intent_extractor" in sys_content.lower() or "intent" in sys_content.lower():
            m = MagicMock()
            m.message.content = '{"detected_language": "English", "directives": {}}'
            m.message.tool_calls = None
            return MagicMock(choices=[m], usage=None)

        if "internalrevisor" in sys_content.lower() or "revisor" in sys_content.lower():
            m = MagicMock()
            m.message.content = "VERDICT: APPROVED"
            m.message.tool_calls = None
            return MagicMock(choices=[m], usage=None)

        if (
            "discussion_scribe_live" in sys_content.lower()
            or "discussion_scribe_conclusion" in sys_content.lower()
            or "strategic synthesis scribe" in sys_content.lower()
            or "dialectical conclusion synthesizer" in sys_content.lower()
        ):
            m = MagicMock()
            m.message.content = "*Core Objective & Problem Statement*\nMock synthesis."
            m.message.tool_calls = None
            return MagicMock(choices=[m], usage=None)

        if agent_turn_handler is not None:
            res = agent_turn_handler(*args, **kwargs)
            if inspect.isawaitable(res):
                return await res
            return res

        m = MagicMock()
        m.message.content = default_content
        m.message.tool_calls = None
        return MagicMock(choices=[m], usage=None)

    return _mock_acompletion


async def async_noop_sleep(*args, **kwargs):
    """Fast async no-op sleep helper for tests."""
    return None


async def noop_intent_extractor(*args, **kwargs):
    """Deterministic no-op IntentExtractor stub for slash/directive parser tests."""
    from core.models import ExtractedIntent

    return ExtractedIntent()


@pytest.fixture(autouse=True)
def guard_unmocked_llm_calls_for_tests():
    """
    Fail-closed guard preventing unmocked live LLM API calls during unit/integration tests.
    Records hits so silent exception fallbacks (e.g. IntentExtractor / LanguageDetector)
    cannot mask accidental live API calls.
    """
    unmocked_hits: list[str] = []

    def _fail_closed_llm_guard(*, agent_key: str = "unknown", kwargs: dict | None = None) -> None:
        model = (kwargs or {}).get("model", "<unspecified>")
        unmocked_hits.append(f"{agent_key}(model={model})")
        raise RuntimeError(
            f"Real LLM API call blocked during pytest run (agent={agent_key}, model={model}): "
            "mock litellm.acompletion, configure_llm_completion_fn(...), or configure_intent_extractor(...) first."
        )

    configure_default_llm_guard(_fail_closed_llm_guard)
    try:
        yield unmocked_hits
    finally:
        reset_default_llm_guard()
        assert not unmocked_hits, (
            f"Test attempted {len(unmocked_hits)} unmocked live LLM call(s): {unmocked_hits}"
        )


@pytest.fixture(autouse=True)
def reset_router_and_llm_di_for_tests():
    """Guarantees that all in-memory DI overrides for router, LLM, slack messages, and usage tracking are cleanly reset."""
    from core.config import (
        configure_token,
        reset_report_timezone,
        reset_token,
        set_admin_slack_user_ids_override,
        set_internal_revisor_enabled_override,
    )
    from core.model_catalog import configure_env_provider, reset_env_provider
    from core.slack_security import reset_auth_failure_tracker
    from services.rate_limiter import ip_rate_limiter
    from services.readiness import reset_readiness_state
    from services.slack.client_core import (
        configure_slack_async_client,
        reset_slack_async_client,
        reset_slack_caches,
    )
    from services.telemetry import reset_telemetry_state

    # Isolate model catalog key checks from developer .env during unit/integration tests;
    # tests verifying missing-key fallback explicitly inject their own env via configure_env_provider().
    configure_env_provider(lambda: {})

    # Hard-isolate Slack credentials, async client, caches, and report timezone for every test
    configure_token("")
    configure_slack_async_client(_UnmockedSlackClientGuard())
    reset_slack_caches()
    reset_report_timezone()
    reset_auth_failure_tracker()
    reset_readiness_state()
    reset_telemetry_state()
    if "adapters.slack.events" in sys.modules:
        sys.modules["adapters.slack.events"].reset_skills_slash_cache()

    yield

    set_internal_revisor_enabled_override(None)
    set_admin_slack_user_ids_override(None)
    reset_report_timezone()
    reset_token()
    reset_slack_async_client()
    reset_slack_caches()
    reset_auth_failure_tracker()
    reset_readiness_state()
    reset_telemetry_state()
    reset_llm_completion_fn()
    reset_llm_sleep_fn()
    reset_env_provider()
    ip_rate_limiter.reset()

    if "adapters.slack.events" in sys.modules:
        sys.modules["adapters.slack.events"].reset_skills_slash_cache()
    if "orchestration.agent_router" in sys.modules:
        router_mod = sys.modules["orchestration.agent_router"]
        router_mod.reset_router_sleep_fn()
        router_mod.reset_router_deps()
        router_mod.reset_guardrail_checker()
    if "services.web_tools" in sys.modules:
        web_mod = sys.modules["services.web_tools"]
        web_mod.reset_web_search_fn()
        web_mod.reset_web_fetch_fn()
    if "orchestration.thread_state" in sys.modules:
        tsm = sys.modules["orchestration.thread_state"].thread_state_manager
        tsm.reset_capacity()
        tsm.reset_task_dispatcher()
    if "services.slack.messages" in sys.modules:
        msg_mod = sys.modules["services.slack.messages"]
        msg_mod.reset_post_agent_response()
        msg_mod.reset_fetch_thread_history()
        msg_mod.reset_post_system_message()
    if "services.slack.files" in sys.modules:
        sys.modules["services.slack.files"].reset_fetch_thread_files()
    if "services.slack.views" in sys.modules:
        sys.modules["services.slack.views"].reset_app_home_publish_cache()
    if "agents" in sys.modules:
        agents_mod = sys.modules["agents"]
        agents_mod.reset_record_thread_tokens()
        agents_mod.reset_calculate_thread_costs()
        agents_mod.reset_trigger_usage_update()
        agents_mod.reset_trigger_thread_notes_update()
    if "nlp.intent_extractor" in sys.modules:
        sys.modules["nlp.intent_extractor"].reset_intent_extractor()
    if "services.rag.embeddings" in sys.modules:
        sys.modules["services.rag.embeddings"].EmbeddingService.reset_embeddings_fn()

    from services.audit_logger import reset_audit_hmac_keyring_override, set_audit_hmac_secret_override

    set_audit_hmac_secret_override(None)
    reset_audit_hmac_keyring_override()
    _wire_default_ioc_hooks()
