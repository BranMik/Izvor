import copy
import inspect
import shutil
from unittest.mock import MagicMock

import pytest

from orchestration.agent_settings import (
    BACKUP_FILE as REAL_BACKUP_FILE,
    DEFAULT_SYSTEM_AGENT_SETTINGS_FILE,
    SETTINGS_FILE as REAL_SETTINGS_FILE,
    SYSTEM_AGENT_BACKUP_FILE as REAL_SYSTEM_AGENT_BACKUP_FILE,
    SYSTEM_AGENT_SETTINGS_FILE as REAL_SYSTEM_AGENT_SETTINGS_FILE,
    settings_manager,
)
from services.pricing_service import PRICING_CACHE_FILE as REAL_PRICING_CACHE_FILE
from services.audit_logger import audit_logger, log_audit_event
from services.security.pii_masker import pii_masker
from services.security import scan_output_for_secrets
import skills.loader
from core.llm_utils import set_pii_masker_instance
from core.slack_security import set_audit_logger_hook, set_secret_scanner_hook

# Wire default IoC hooks for test runs
set_audit_logger_hook(log_audit_event)
set_secret_scanner_hook(scan_output_for_secrets)
set_pii_masker_instance(pii_masker)


@pytest.fixture(autouse=True)
def isolate_audit_logger_for_tests(tmp_path):
    """
    Bulletproof isolation of the AuditLogger.
    Forces all audit logging during pytest runs to write to a temporary directory.
    Because this is a pytest fixture, it cannot be triggered in production.
    """
    test_audit_dir = tmp_path / "audit"
    test_audit_dir.mkdir(parents=True, exist_ok=True)

    audit_logger.configure_paths(audit_dir=test_audit_dir)
    audit_logger.invalidate_cache()

    yield

    # Restore (optional, but good practice)
    audit_logger.reset_paths()
    audit_logger.invalidate_cache()


@pytest.fixture(autouse=True)
def isolate_pricing_cache_for_tests(tmp_path):
    """
    Prevents tests from modifying or overwriting production data/pricing/model_pricing_cache.json.
    Each test gets an isolated copy in tmp_path / "_conftest_iso".
    """
    iso_dir = tmp_path / "_conftest_iso"
    iso_dir.mkdir(parents=True, exist_ok=True)
    temp_pricing_cache = iso_dir / "model_pricing_cache.json"
    if REAL_PRICING_CACHE_FILE.exists():
        shutil.copy2(REAL_PRICING_CACHE_FILE, temp_pricing_cache)
    from services.pricing_service import pricing_service

    pricing_service.configure_paths(cache_file=temp_pricing_cache)
    yield
    pricing_service.reset_paths()


@pytest.fixture(autouse=True)
def isolate_agent_settings_for_tests(tmp_path):
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

    if REAL_SETTINGS_FILE.exists():
        shutil.copy2(REAL_SETTINGS_FILE, temp_settings)
    if REAL_BACKUP_FILE.exists():
        shutil.copy2(REAL_BACKUP_FILE, temp_backup)
    if REAL_SYSTEM_AGENT_SETTINGS_FILE.exists():
        shutil.copy2(REAL_SYSTEM_AGENT_SETTINGS_FILE, temp_sys_settings)
    elif DEFAULT_SYSTEM_AGENT_SETTINGS_FILE.exists():
        shutil.copy2(DEFAULT_SYSTEM_AGENT_SETTINGS_FILE, temp_sys_settings)
    if REAL_SYSTEM_AGENT_BACKUP_FILE.exists():
        shutil.copy2(REAL_SYSTEM_AGENT_BACKUP_FILE, temp_sys_backup)
    elif DEFAULT_SYSTEM_AGENT_SETTINGS_FILE.exists():
        shutil.copy2(DEFAULT_SYSTEM_AGENT_SETTINGS_FILE, temp_sys_backup)

    # Isolate workflows directory
    temp_workflows_dir = iso_dir / "workflows"
    temp_workflows_dir.mkdir(parents=True, exist_ok=True)
    real_workflows_dir = settings_manager.workflow_manager.workflows_dir
    if real_workflows_dir.exists():
        for wf_file in real_workflows_dir.glob("*.json"):
            shutil.copy2(wf_file, temp_workflows_dir / wf_file.name)

    orig_active_wf_id = settings_manager.active_workflow_id

    settings_manager.configure_paths(
        settings_file=temp_settings,
        backup_file=temp_backup,
        system_agent_settings_file=temp_sys_settings,
        system_agent_backup_file=temp_sys_backup,
        workflows_dir=temp_workflows_dir,
    )

    # Save original in-memory state
    orig_enabled = set(settings_manager.enabled_agents)
    orig_orchestration_mode = settings_manager.orchestration_mode
    orig_sandwich_start = list(settings_manager.sandwich_start_chain)
    orig_sandwich_end = list(settings_manager.sandwich_end_chain)
    orig_fixed = list(settings_manager.fixed_chain)
    orig_agents_config = copy.deepcopy(settings_manager.agents_config)
    orig_system_agents = copy.deepcopy(settings_manager.system_agents_config)
    orig_orchestrator = copy.deepcopy(settings_manager.orchestrator_config)

    yield

    # Restore in-memory state in-place
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

    settings_manager.reset_paths()
    settings_manager.active_workflow_id = orig_active_wf_id


@pytest.fixture(autouse=True)
def isolate_app_settings_for_tests(tmp_path):
    """
    Prevents tests from modifying or overwriting production data/state/app_settings.json.
    Each test gets an isolated copy in tmp_path / "_conftest_iso".
    """
    from orchestration.app_settings import (
        APP_BACKUP_FILE as REAL_APP_BACKUP_FILE,
        APP_SETTINGS_FILE as REAL_APP_SETTINGS_FILE,
        app_settings_manager,
    )

    iso_dir = tmp_path / "_conftest_iso"
    iso_dir.mkdir(parents=True, exist_ok=True)
    temp_app_settings = iso_dir / "app_settings.json"
    temp_app_backup = iso_dir / "app_settings_backup.json"

    if REAL_APP_SETTINGS_FILE.exists():
        shutil.copy2(REAL_APP_SETTINGS_FILE, temp_app_settings)
    if REAL_APP_BACKUP_FILE.exists():
        shutil.copy2(REAL_APP_BACKUP_FILE, temp_app_backup)

    # Save original in-memory state
    orig_expiration = app_settings_manager.thread_skill_expiration_time
    orig_cb_enabled = app_settings_manager.circuit_breaker_enabled
    orig_cb_threshold = app_settings_manager.circuit_breaker_threshold_usd
    orig_daily_budget = app_settings_manager.daily_budget_usd
    orig_scribe_enabled = app_settings_manager.scribe_enabled
    orig_weekly_pricing = app_settings_manager.weekly_pricing_update_enabled
    orig_workspace_context = app_settings_manager.workspace_context

    app_settings_manager.configure_paths(
        settings_file=temp_app_settings,
        backup_file=temp_app_backup,
    )

    yield

    # Restore in-memory state
    app_settings_manager.thread_skill_expiration_time = orig_expiration
    app_settings_manager.circuit_breaker_enabled = orig_cb_enabled
    app_settings_manager.circuit_breaker_threshold_usd = orig_cb_threshold
    app_settings_manager.daily_budget_usd = orig_daily_budget
    app_settings_manager.scribe_enabled = orig_scribe_enabled
    app_settings_manager.weekly_pricing_update_enabled = orig_weekly_pricing
    app_settings_manager.workspace_context = orig_workspace_context
    app_settings_manager.reset_paths()


@pytest.fixture(autouse=True)
def isolate_database_for_tests(tmp_path):
    """
    Bulletproof SQLite database isolation for tests.
    Each test gets a dedicated temporary SQLite database initialized with all schemas and WAL pragmas.
    """
    from core.database import db_manager

    iso_dir = tmp_path / "_conftest_iso"
    iso_dir.mkdir(parents=True, exist_ok=True)
    test_db_path = iso_dir / "test_izvor.db"
    db_manager.configure_paths(db_path=test_db_path)

    yield

    db_manager.reset_paths()


@pytest.fixture(autouse=True)
def isolate_thread_state_files_for_tests():
    """
    Ensures clean thread skills state and thread usage state in SQLite.
    """
    from agents.usage_agent import usage_tracker
    from orchestration.thread_skills_manager import thread_skills_manager

    thread_skills_manager.clear_all()
    usage_tracker.clear_all()

    yield

    thread_skills_manager.clear_all()
    usage_tracker.clear_all()


@pytest.fixture(autouse=True)
def isolate_mcp_client_for_tests():
    """
    Prevents tests from leaking custom MCP transport factories,
    cached tool discoveries, or custom MCP servers.
    """
    from services.mcp.client_manager import mcp_client_manager
    from orchestration.system_settings import system_settings_manager

    old_mcp_servers = dict(system_settings_manager.mcp_servers)
    mcp_client_manager.reset_transport_factory()

    yield

    mcp_client_manager.reset_transport_factory()
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
    try:
        yield
    finally:
        guardrail_service.reset_paths()


@pytest.fixture(autouse=True)
def isolate_system_settings_for_tests(tmp_path):
    """
    Prevents tests from modifying config/settings/system_settings.json
    and data/backups/system_settings_backup.json.
    """
    from orchestration.system_settings import (
        SYSTEM_SETTINGS_FILE as REAL_SYS_SETTINGS_FILE,
        SYSTEM_BACKUP_FILE as REAL_SYS_BACKUP_FILE,
        system_settings_manager,
    )

    iso_dir = tmp_path / "_conftest_iso"
    iso_dir.mkdir(parents=True, exist_ok=True)
    temp_sys_settings = iso_dir / "system_settings.json"
    temp_sys_backup = iso_dir / "system_settings_backup.json"

    if REAL_SYS_SETTINGS_FILE.exists():
        shutil.copy2(REAL_SYS_SETTINGS_FILE, temp_sys_settings)
    if REAL_SYS_BACKUP_FILE.exists():
        shutil.copy2(REAL_SYS_BACKUP_FILE, temp_sys_backup)

    system_settings_manager.configure_paths(
        settings_file=temp_sys_settings,
        backup_file=temp_sys_backup,
    )

    yield

    system_settings_manager.reset_paths()


@pytest.fixture(autouse=True)
def isolate_pii_masker_exclusions_for_tests(tmp_path):
    """
    Prevents tests from modifying or overwriting production data/state/presidio_skill_exclusions.json.
    Each test gets an isolated copy in tmp_path / "_conftest_iso".
    """
    from core.paths import PRESIDIO_SKILL_EXCLUSIONS_FILE as REAL_EXCLUSIONS_FILE
    from services.security.pii_masker import pii_masker

    iso_dir = tmp_path / "_conftest_iso"
    iso_dir.mkdir(parents=True, exist_ok=True)
    temp_exclusions = iso_dir / "presidio_skill_exclusions.json"

    if REAL_EXCLUSIONS_FILE.exists():
        shutil.copy2(REAL_EXCLUSIONS_FILE, temp_exclusions)

    pii_masker.configure_paths(exclusions_file=temp_exclusions)

    yield

    pii_masker.reset_paths()


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
    from orchestration.system_settings import system_settings_manager
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


@pytest.fixture(autouse=True)
def isolate_workspace_tests_for_tests():
    """
    Cleans up any scratch or temporary test files written into agents_resources or workspace.
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
    system agent calls (Language Detector, Intent Extractor, Internal Revisor)
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


@pytest.fixture(autouse=True)
def reset_router_and_llm_di_for_tests():
    """Guarantees that all in-memory DI overrides for router, LLM, slack messages, and usage tracking are cleanly reset."""
    from core.llm_utils import reset_llm_completion_fn, reset_llm_sleep_fn
    from core.model_catalog import configure_env_provider, reset_env_provider

    # Isolate model catalog key checks from developer .env during unit/integration tests;
    # tests verifying missing-key fallback explicitly inject their own env via configure_env_provider().
    configure_env_provider(lambda: {})
    from core.config import (
        reset_token,
        set_admin_slack_user_ids_override,
        set_internal_revisor_enabled_override,
    )
    from orchestration.agent_router import (
        reset_guardrail_checker,
        reset_router_deps,
        reset_router_sleep_fn,
    )
    from services.web_tools import reset_web_fetch_fn, reset_web_search_fn
    from orchestration.thread_state import thread_state_manager
    from services.slack import (
        reset_fetch_thread_files,
        reset_fetch_thread_history,
        reset_post_agent_response,
        reset_post_system_message,
        reset_slack_async_client,
    )
    from agents import (
        reset_calculate_thread_costs,
        reset_record_thread_tokens,
        reset_trigger_thread_notes_update,
        reset_trigger_usage_update,
    )
    from nlp.intent_extractor import reset_intent_extractor
    from services.rag.embeddings import EmbeddingService
    from services.rate_limiter import ip_rate_limiter

    yield

    set_internal_revisor_enabled_override(None)
    set_admin_slack_user_ids_override(None)
    reset_token()
    reset_slack_async_client()
    reset_llm_completion_fn()
    reset_llm_sleep_fn()
    reset_env_provider()
    reset_router_sleep_fn()
    reset_router_deps()
    reset_guardrail_checker()
    reset_web_search_fn()
    reset_web_fetch_fn()
    thread_state_manager.reset_capacity()
    thread_state_manager.reset_task_dispatcher()
    reset_post_agent_response()
    reset_fetch_thread_history()
    reset_fetch_thread_files()
    reset_post_system_message()
    reset_record_thread_tokens()
    reset_calculate_thread_costs()
    reset_trigger_usage_update()
    reset_trigger_thread_notes_update()
    reset_intent_extractor()
    EmbeddingService.reset_embeddings_fn()
    ip_rate_limiter.reset()


