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


async def _run_unmocked_llm_call():
    import litellm
    from core.llm_utils import execute_llm_with_retry

    await execute_llm_with_retry(
        func=litellm.acompletion,
        kwargs={"model": "gemini/gemini-3.8-flash", "messages": [{"role": "user", "content": "ping"}]},
        agent_key="isolation_probe",
        thread_key=("C_ISO", "1"),
    )


def test_guard_blocks_unmocked_llm_calls(guard_unmocked_llm_calls_for_tests) -> None:
    """Confirms fail-closed LLM guard blocks unmocked litellm calls immediately."""
    import asyncio
    import pytest

    with pytest.raises(RuntimeError, match="Real LLM API call blocked"):
        asyncio.run(_run_unmocked_llm_call())

    assert len(guard_unmocked_llm_calls_for_tests) == 1
    assert "isolation_probe" in guard_unmocked_llm_calls_for_tests[0]
    guard_unmocked_llm_calls_for_tests.clear()


def test_make_mock_acompletion_intercepts_discussion_scribe() -> None:
    """Confirms make_mock_acompletion intercepts Discussion Scribe without calling agent_turn_handler."""
    import asyncio
    from unittest.mock import MagicMock

    from conftest import make_mock_acompletion
    from prompts.builder import load_system_prompt

    handler = MagicMock(
        return_value=MagicMock(
            choices=[MagicMock(message=MagicMock(content="Persona reply", tool_calls=None))],
            usage=None,
        )
    )
    mock_fn = make_mock_acompletion(handler)

    async def _run() -> None:
        live_prompt = load_system_prompt("Discussion_Scribe_Live")
        conc_prompt = load_system_prompt("Discussion_Scribe_Conclusion")

        live_resp = await mock_fn(
            messages=[{"role": "system", "content": live_prompt}, {"role": "user", "content": "synth"}]
        )
        conc_resp = await mock_fn(
            messages=[{"role": "system", "content": conc_prompt}, {"role": "user", "content": "conclude"}]
        )
        assert "Mock synthesis" in live_resp.choices[0].message.content
        assert "Mock synthesis" in conc_resp.choices[0].message.content
        handler.assert_not_called()

        persona_resp = await mock_fn(
            messages=[
                {"role": "system", "content": '<agent_specification id="agent-10001">...</agent_specification>'},
                {"role": "user", "content": "hello"},
            ]
        )
        assert persona_resp.choices[0].message.content == "Persona reply"
        handler.assert_called_once()

    asyncio.run(_run())



