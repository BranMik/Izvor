"""
Unit tests for Phase P3: Brand Consistency, Hygiene, Architecture Modernization,
and Background Worker Warning Mitigation.
"""

from fastapi.testclient import TestClient

from main import app
from config_store.agent_settings import settings_manager
from orchestration.nomination import parse_agent_and_prompt
from orchestration.thread_state import thread_state_manager
from prompts.builder import is_short_output_agent, is_system_agent


def test_app_mention_regex_routing():
    """Verify is_explicit_app_mention in adapters.slack.events recognizes @Izvor and excludes plain common nouns."""
    from adapters.slack.events import is_explicit_app_mention

    assert is_explicit_app_mention("@Izvor help me") is True
    assert is_explicit_app_mention("Izvor what is the status") is False
    assert is_explicit_app_mention("Ovo je glavni izvor prihoda") is False
    assert is_explicit_app_mention("Unrelated text") is False


def test_prompt_builder_dynamic_ssot():
    """Verify is_short_output_agent and is_system_agent query AgentSettingsManager dynamically."""
    orig_agents = dict(settings_manager.agents_config)
    orig_system = dict(settings_manager.system_agents_config)
    try:
        # Test short output dynamic query via config injection
        settings_manager.agents_config = {
            **orig_agents,
            "dynamic-short": {"is_short_output": True},
            "unknown-long": {"is_short_output": False},
        }
        assert is_short_output_agent("dynamic-short") is True
        assert is_short_output_agent("unknown-long") is False

        # Test system agent dynamic query via config injection
        settings_manager.system_agents_config = {
            **orig_system,
            "dynamic-sys": {"agent_key": "dynamic-sys"},
        }
        assert is_system_agent("dynamic-sys") is True
    finally:
        settings_manager.agents_config = orig_agents
        settings_manager.system_agents_config = orig_system


def test_sandwich_orchestration_simplification():
    """Verify sandwich orchestration mode pops the first agent in order without branching errors."""
    from core.models import OrchestrationMode

    channel = "C_P3_TEST"
    ts = "200.001"
    thread_state_manager.clear_state(channel, ts)
    thread_state_manager.discussion.set_remaining_agents(channel, ts, ["agent-10001", "agent-10002"])

    settings_manager.set_orchestration_mode(OrchestrationMode.SANDWICH, save=False)
    agent, cleaned, was_explicit = parse_agent_and_prompt("hello team", channel, ts)
    assert agent == "agent-10001"
    assert was_explicit is False

    thread_state_manager.clear_state(channel, ts)


def test_background_worker_coroutine_closure():
    """Verify trigger_thread_notes_update and trigger_usage_update cleanly close coroutines on RuntimeError."""
    from agents.thread_notes_agent import trigger_thread_notes_update
    from agents.usage_agent import trigger_usage_update

    # In a sync context without an event loop, asyncio.create_task raises RuntimeError
    # The updated dispatchers must catch it, close the coroutine, and return None without warning
    res_notes = trigger_thread_notes_update("C_WARN_TEST", "999.001", is_completed=True)
    assert res_notes is None

    res_usage = trigger_usage_update("C_WARN_TEST", "999.001", is_completed=True)
    assert res_usage is None


def test_health_endpoints_hardening():
    """Verify public health endpoint returns only safe service status, while /api/health requires auth."""
    client = TestClient(app)

    # Public endpoint
    r_pub = client.get("/health")
    assert r_pub.status_code == 200
    data_pub = r_pub.json()
    assert data_pub["status"] == "healthy"
    assert data_pub["service"] == "Izvor Multi-Agent Hub"
    assert "agents" not in data_pub

    # Protected endpoint without auth
    r_prot_unauth = client.get("/api/health")
    assert r_prot_unauth.status_code == 401
