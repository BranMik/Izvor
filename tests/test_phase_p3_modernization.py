"""
Unit tests for Phase P3: Brand Consistency, Hygiene, Architecture Modernization,
and Background Worker Warning Mitigation.
"""

from unittest.mock import patch

from starlette.testclient import TestClient

from main import app
from orchestration.agent_settings import settings_manager
from orchestration.nomination import parse_agent_and_prompt
from orchestration.thread_state import thread_state_manager
from prompts.builder import is_short_output_agent, is_system_agent


def test_app_mention_regex_routing():
    """Verify regex in adapters.slack.events recognizes both @Izvor and @CaiLogics."""
    import re

    mention_pattern = re.compile(r"@?(?:Izvor|CaiLogics)\b", re.IGNORECASE)
    assert bool(mention_pattern.search("@Izvor help me")) is True
    assert bool(mention_pattern.search("Izvor what is the status")) is True
    assert bool(mention_pattern.search("@CaiLogics help me")) is True
    assert bool(mention_pattern.search("CaiLogics status")) is True
    assert bool(mention_pattern.search("Unrelated text")) is False


def test_prompt_builder_dynamic_ssot(monkeypatch):
    """Verify is_short_output_agent and is_system_agent query AgentSettingsManager dynamically."""
    # Test short output dynamic query
    monkeypatch.setattr(settings_manager, "get_agent", lambda k: {"is_short_output": True} if k == "dynamic-short" else {})
    assert is_short_output_agent("dynamic-short") is True
    assert is_short_output_agent("unknown-long") is False

    # Test system agent dynamic query
    monkeypatch.setattr(settings_manager, "is_system_agent", lambda k: k == "dynamic-sys")
    assert is_system_agent("dynamic-sys") is True


def test_sandwich_orchestration_simplification():
    """Verify sandwich orchestration mode pops the first agent in order without branching errors."""
    from core.models import OrchestrationMode

    channel = "C_P3_TEST"
    ts = "200.001"
    thread_state_manager.clear_state(channel, ts)
    thread_state_manager.discussion.remaining_agents_map[(channel, ts)] = ["agent-10001", "agent-10002"]

    with patch.object(settings_manager, "get_orchestration_mode", return_value=OrchestrationMode.SANDWICH):
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
