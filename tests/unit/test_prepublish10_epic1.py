import asyncio
import pytest
from unittest.mock import MagicMock, patch

from config_store.app_settings import (
    get_circuit_breaker_threshold,
    get_daily_budget_usd,
    is_circuit_breaker_enabled,
    set_circuit_breaker_enabled,
    set_circuit_breaker_threshold,
)
from orchestration.turn_pipeline import TurnContext
from pipeline.spend_governance.circuit_breaker import SpendGovernance
from services.pricing_service import _is_within_sanity_bounds
from services.slack_home.governance_tab import build_governance_blocks
from services.web_tools import (
    check_and_increment_web_budget,
    get_turn_web_fetch_count,
    reset_turn_web_budget,
)


def test_circuit_breaker_toggle_preserves_threshold():
    """TKT-1002: Disabling circuit breaker should NOT zero out the configured threshold."""
    set_circuit_breaker_threshold(3.5)
    assert get_circuit_breaker_threshold() == 3.5

    set_circuit_breaker_enabled(False)
    assert is_circuit_breaker_enabled() is False
    # Threshold must be preserved and not zeroed out
    assert get_circuit_breaker_threshold() == 3.5

    set_circuit_breaker_enabled(True)
    assert is_circuit_breaker_enabled() is True
    assert get_circuit_breaker_threshold() == 3.5


def test_reset_daily_spend_preserves_ledger_rows(tmp_path):
    """TKT-1002: reset_daily_spend() should use a watermark timestamp instead of deleting rows."""
    from core.database import db_manager

    prev_db_path = db_manager.db_path
    fresh_db = tmp_path / "test_spend.db"
    db_manager.configure_paths(db_path=fresh_db)

    try:
        sg = SpendGovernance()
        sg.record_usage(
            session_id="channel_1:thread_1",
            model_name="gemini/gemini-3.8-flash",
            prompt_tokens=1000,
            completion_tokens=500,
        )

        daily_spend = sg.get_daily_spend()
        assert daily_spend > 0

        with db_manager.get_connection() as conn:
            count_before = conn.execute("SELECT COUNT(*) FROM spend_ledger").fetchone()[0]
        assert count_before == 1

        # Reset daily spend
        sg.reset_daily_spend()

        # Daily spend calculation should now be 0.0
        assert sg.get_daily_spend() == 0.0

        # Ledger rows must still exist for historical audit integrity
        with db_manager.get_connection() as conn:
            count_after = conn.execute("SELECT COUNT(*) FROM spend_ledger").fetchone()[0]
        assert count_after == 1
    finally:
        db_manager.configure_paths(db_path=prev_db_path)


@pytest.mark.anyio
async def test_web_fetch_budget_enforced_across_asyncio_to_thread():
    """TKT-1003: ContextVar mutable container should share web fetch count across threads."""
    reset_turn_web_budget()
    assert get_turn_web_fetch_count() == 0

    with patch("services.web_tools.get_web_security_limits", return_value={"max_fetches_per_turn": 3}):
        def worker():
            check_and_increment_web_budget()

        # Run 3 increments in background threads
        await asyncio.to_thread(worker)
        await asyncio.to_thread(worker)
        await asyncio.to_thread(worker)

        assert get_turn_web_fetch_count() == 3

        # The 4th should raise PermissionError
        with pytest.raises(PermissionError, match="Web fetch budget exceeded"):
            await asyncio.to_thread(worker)


def test_slack_app_home_daily_budget_rendering():
    """TKT-1002: build_governance_blocks should render daily budget input and presets."""
    blocks = build_governance_blocks(
        cb_enabled=True,
        cb_threshold=1.5,
        cb_status_str="ON ($1.50)",
        scribe_on=True,
        scribe_status_str="ON",
        ff_on=True,
        ff_status_str="ON",
        weekly_pricing_on=True,
        weekly_pricing_status_str="ON",
        expiry_hours=24.0,
        daily_budget=25.0,
    )
    action_ids = []
    for b in blocks:
        if b.get("type") == "actions":
            for el in b.get("elements", []):
                action_ids.append(el.get("action_id"))
        elif b.get("type") == "input":
            el = b.get("element", {})
            if el.get("action_id"):
                action_ids.append(el.get("action_id"))

    assert "set_daily_budget_input" in action_ids
    assert "save_daily_budget_button" in action_ids
    assert "set_daily_budget_preset:10.00" in action_ids
    assert "set_daily_budget_preset:20.00" in action_ids
    assert "set_daily_budget_preset:50.00" in action_ids


@pytest.mark.anyio
async def test_slack_app_home_daily_budget_actions():
    """TKT-1002: Slack actions update daily_budget_usd setting."""
    from adapters.slack.actions.governance_actions import (
        _handle_save_daily_budget_button,
        _handle_set_daily_budget_input,
        _handle_set_daily_budget_preset,
    )

    bg_tasks = MagicMock()

    # Test preset action
    action_preset = {"action_id": "set_daily_budget_preset:50"}
    await _handle_set_daily_budget_preset(
        action=action_preset,
        payload={},
        user_id="U12345",
        background_tasks=bg_tasks,
        view_state={},
    )
    assert get_daily_budget_usd() == 50.0

    # Test input change
    action_input = {"action_id": "set_daily_budget_input", "value": "15.5"}
    await _handle_set_daily_budget_input(
        action=action_input,
        payload={},
        user_id="U12345",
        background_tasks=bg_tasks,
        view_state={},
    )
    assert get_daily_budget_usd() == 15.5

    # Test save button reads input from view_state
    view_state = {
        "daily_budget_block": {
            "set_daily_budget_input": {"value": "30.0"}
        }
    }
    await _handle_save_daily_budget_button(
        action={"action_id": "save_daily_budget_button"},
        payload={},
        user_id="U12345",
        background_tasks=bg_tasks,
        view_state=view_state,
    )
    assert get_daily_budget_usd() == 30.0


def test_pricing_sanity_bounds_rejection():
    """TKT-1004: Prices deviating wildly (< 10% or > 1000%) from established baseline must be rejected."""
    baseline = {
        "input_price_per_1m": 1.0,
        "output_price_per_1m": 4.0,
        "source": "Google Official Baseline",
    }

    # Normal variation (acceptable)
    assert _is_within_sanity_bounds(0.8, 3.5, baseline) is True
    assert _is_within_sanity_bounds(1.5, 6.0, baseline) is True

    # Wild drop (< 10% of baseline) - e.g. prompt injection claiming $0.05/1M
    assert _is_within_sanity_bounds(0.05, 4.0, baseline) is False
    assert _is_within_sanity_bounds(1.0, 0.2, baseline) is False

    # Wild spike (> 1000% of baseline)
    assert _is_within_sanity_bounds(15.0, 4.0, baseline) is False
    assert _is_within_sanity_bounds(1.0, 50.0, baseline) is False


def test_turn_context_delta_token_recording():
    """TKT-1001: TurnContext.add_usage immediately records delta tokens via token_recorder."""
    mock_recorder = MagicMock()
    ctx = TurnContext(
        channel_id="channel_1",
        thread_ts="thread_1",
        agent_config={"model": "gemini/gemini-3.8-flash"},
        token_recorder=mock_recorder,
    )

    mock_resp1 = MagicMock()
    mock_resp1.model = "gemini/gemini-3.8-flash"
    mock_resp1.usage = MagicMock(prompt_tokens=100, completion_tokens=50)

    ctx.add_usage(mock_resp1)
    assert ctx.output.total_prompt_tokens == 100
    assert ctx.output.total_completion_tokens == 50
    mock_recorder.assert_called_once_with(
        channel_id="channel_1",
        thread_ts="thread_1",
        model_name="gemini/gemini-3.8-flash",
        prompt_tokens=100,
        completion_tokens=50,
    )

    mock_recorder.reset_mock()

    # Second LLM call in the same turn
    mock_resp2 = MagicMock()
    mock_resp2.model = "gemini/gemini-3.8-flash"
    mock_resp2.usage = MagicMock(prompt_tokens=40, completion_tokens=25)

    ctx.add_usage(mock_resp2)
    assert ctx.output.total_prompt_tokens == 140
    assert ctx.output.total_completion_tokens == 75
    # Must record ONLY the delta tokens (40, 25)
    mock_recorder.assert_called_once_with(
        channel_id="channel_1",
        thread_ts="thread_1",
        model_name="gemini/gemini-3.8-flash",
        prompt_tokens=40,
        completion_tokens=25,
    )


def test_turn_context_and_spend_governance_normalize_model_names(tmp_path):
    """Plan 227: TurnContext.add_usage prefers configured canonical model and SpendGovernance normalizes case."""
    from core.database import db_manager

    mock_recorder = MagicMock()
    ctx = TurnContext(
        channel_id="channel_norm",
        thread_ts="thread_norm",
        agent_cfg={"model": "gemini/gemini-3.8-flash"},
        token_recorder=mock_recorder,
    )
    resp = MagicMock()
    resp.model = "gemini-3.8-flash"  # LiteLLM unprefixed response model
    resp.usage = MagicMock(prompt_tokens=50, completion_tokens=10)
    ctx.add_usage(resp)
    mock_recorder.assert_called_once_with(
        channel_id="channel_norm",
        thread_ts="thread_norm",
        model_name="gemini/gemini-3.8-flash",
        prompt_tokens=50,
        completion_tokens=10,
    )

    prev_db = db_manager.db_path
    db_manager.configure_paths(db_path=tmp_path / "norm_spend.db")
    try:
        sg = SpendGovernance()
        sg.record_usage("channel_norm:thread_norm", "  Gemini/Gemini-3.8-Flash ", prompt_tokens=100, completion_tokens=20)
        sg.record_usage("channel_norm:thread_norm", "gemini/gemini-3.8-flash", prompt_tokens=50, completion_tokens=10)
        metrics = sg.calculate_spend("channel_norm:thread_norm")
        assert list(metrics.tokens_by_model.keys()) == ["gemini/gemini-3.8-flash"]
        assert metrics.total_prompt_tokens == 150
        assert metrics.total_completion_tokens == 30
    finally:
        db_manager.configure_paths(db_path=prev_db)
