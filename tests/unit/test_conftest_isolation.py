"""Tests verifying conftest.py state-reset isolation across consecutive tests (TKT-1239)."""

from __future__ import annotations

from adapters.slack import events as slack_events
from config_store.app_settings import app_settings_manager
from core import slack_security
from services.readiness import readiness_registry
from services.telemetry import get_telemetry_stats, record_turn_trace


_OBSERVED_ORIG_FLAGS: dict[str, bool] = {}


def test_conftest_isolation_seed_mutations() -> None:
    """Mutates app flags, auth failure tracker, slash cache, readiness, and telemetry."""
    orig_ff = app_settings_manager.convergence_fast_forward_enabled
    orig_fu = app_settings_manager.filter_unauthorized_skill_agents_enabled
    _OBSERVED_ORIG_FLAGS["convergence_fast_forward_enabled"] = orig_ff
    _OBSERVED_ORIG_FLAGS["filter_unauthorized_skill_agents_enabled"] = orig_fu

    app_settings_manager.convergence_fast_forward_enabled = not orig_ff
    app_settings_manager.filter_unauthorized_skill_agents_enabled = not orig_fu

    slack_security._auth_failure_tracker["10.0.0.99"] = {
        "window_start": 9999999999.0,
        "logged_count": 3,
        "suppressed_count": 2,
    }
    slack_events._get_skills_slash_regex()
    readiness_registry.mark_component("pricing_cache", "failed", detail="injected", critical=False)
    readiness_registry.mark_startup_complete(True)
    record_turn_trace("C:1", "C", "1", "agent-1", "validation", 12.5)


def test_conftest_isolation_verifies_clean_slate() -> None:
    """Confirms every mutated global state was cleanly reset before the next test."""
    assert (
        app_settings_manager.convergence_fast_forward_enabled
        == _OBSERVED_ORIG_FLAGS["convergence_fast_forward_enabled"]
    )
    assert (
        app_settings_manager.filter_unauthorized_skill_agents_enabled
        == _OBSERVED_ORIG_FLAGS["filter_unauthorized_skill_agents_enabled"]
    )
    assert slack_security._auth_failure_tracker == {}
    assert slack_events._SKILLS_SLASH_CACHE is None
    summary = readiness_registry.get_summary()
    assert summary["startup_complete"] is False
    assert summary["components"] == {}
    assert get_telemetry_stats()["buffer_depth"] == 0

