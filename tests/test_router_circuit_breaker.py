from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestration import agent_router
from orchestration.agent_router import process_and_respond
from orchestration.agent_settings import (
    get_circuit_breaker_config,
    get_circuit_breaker_threshold,
    is_circuit_breaker_enabled,
    reset_enabled_agents,
    set_circuit_breaker_enabled,
    set_circuit_breaker_threshold,
    set_fixed_config,
    set_orchestration_mode,
    set_sandwich_config,
)
from orchestration.thread_state import thread_state_manager


@pytest.fixture(autouse=True)
def setup_test_environment(monkeypatch):
    reset_enabled_agents()
    set_orchestration_mode("sandwich")
    set_sandwich_config([], [])
    set_fixed_config([])
    thread_state_manager.reset_all()
    monkeypatch.setattr(agent_router.asyncio, "sleep", AsyncMock(return_value=None))


def test_circuit_breaker_configuration_methods():
    reset_enabled_agents()
    set_circuit_breaker_enabled(True)
    assert is_circuit_breaker_enabled() is True

    set_circuit_breaker_threshold(2.75)
    assert get_circuit_breaker_threshold() == 2.75

    cfg = get_circuit_breaker_config()
    assert cfg["enabled"] is True
    assert cfg["threshold_usd"] == 2.75

    set_circuit_breaker_enabled(False)
    assert is_circuit_breaker_enabled() is False

    # Negative threshold should clamp to 0.0
    set_circuit_breaker_threshold(-1.0)
    assert get_circuit_breaker_threshold() == 0.0


@pytest.mark.anyio
async def test_circuit_breaker_triggers_when_threshold_exceeded(monkeypatch):
    thread_state_manager.remaining_agents_map.clear()
    channel_id = "C_CB_TRIGGER"
    thread_ts = "123456789.9001"
    thread_key = (channel_id, thread_ts)
    thread_state_manager.remaining_agents_map[thread_key] = ["agent-10011", "agent-10008"]

    set_circuit_breaker_enabled(True)
    set_circuit_breaker_threshold(0.50)

    # Mock calculate_thread_costs to return $0.75 (> $0.50)
    monkeypatch.setattr(
        "orchestration.agent_router.calculate_thread_costs",
        lambda ch, ts: {"grand_total_cost_usd": 0.75},
    )

    posted_messages = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None, **kwargs
    ):
        posted_messages.append({"text": text, "username": username})
        return {"ok": True, "ts": "12345.9999"}

    llm_called = False

    async def mock_run_in_threadpool(func, *args, **kwargs):
        nonlocal llm_called
        llm_called = True
        raise RuntimeError("LLM should not be called when circuit-breaker is triggered")

    notes_updated = False

    def mock_trigger_notes(channel_id, thread_ts, is_completed=False, **kwargs):
        nonlocal notes_updated
        if is_completed:
            notes_updated = True

    usage_updated = False

    def mock_trigger_usage(channel_id, thread_ts, is_completed=False, **kwargs):
        nonlocal usage_updated
        if is_completed:
            usage_updated = True

    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent_response
    )
    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_run_in_threadpool
    )
    monkeypatch.setattr(
        "orchestration.agent_router.trigger_thread_notes_update", mock_trigger_notes
    )
    monkeypatch.setattr(
        "orchestration.agent_router.trigger_usage_update", mock_trigger_usage
    )

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="Hello agents, continue analysis",
        is_bot_trigger=False,
    )

    assert res.get("circuit_breaker_triggered") is True
    assert llm_called is False
    assert len(posted_messages) == 1
    assert (
        posted_messages[0]["text"]
        == "EXPENSE LIMIT REACHED FOR THE THREAD. ENDING CALLS"
    )
    assert notes_updated is False  # Suppressed to avoid burning LLM tokens after breaker trip
    assert usage_updated is True
    assert thread_key in thread_state_manager.remaining_agents_map
    assert thread_state_manager.remaining_agents_map[thread_key] == ["agent-10008"]


@pytest.mark.anyio
async def test_circuit_breaker_allows_normal_response_when_below_threshold(
    monkeypatch,
):
    thread_state_manager.remaining_agents_map.clear()
    channel_id = "C_CB_BELOW"
    thread_ts = "123456789.9002"

    set_circuit_breaker_enabled(True)
    set_circuit_breaker_threshold(1.00)

    # Cost is $0.20 (< $1.00)
    monkeypatch.setattr(
        "orchestration.agent_router.calculate_thread_costs",
        lambda ch, ts: {"grand_total_cost_usd": 0.20},
    )

    mock_choice = MagicMock()
    mock_choice.message.content = "Normal agent response [agent-10008]"
    mock_choice.message.tool_calls = None

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=10, completion_tokens=10)

    async def mock_run_in_threadpool(func, *args, **kwargs):
        return mock_response

    posted_messages = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted_messages.append({"text": text, "username": username})
        return {"ok": True, "ts": "12345.9999"}

    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent_response
    )
    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_run_in_threadpool
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="[agent-10001] Normal query",
        is_bot_trigger=False,
    )

    assert res.get("ok") is True
    assert res.get("circuit_breaker_triggered") is not True
    assert len(posted_messages) == 1
    assert "Normal agent response" in posted_messages[0]["text"]


@pytest.mark.anyio
async def test_circuit_breaker_allows_normal_response_when_disabled(monkeypatch):
    thread_state_manager.remaining_agents_map.clear()
    channel_id = "C_CB_DISABLED"
    thread_ts = "123456789.9003"

    set_circuit_breaker_enabled(False)
    set_circuit_breaker_threshold(0.10)

    # Cost is $5.00 (> $0.10), but circuit breaker is disabled
    monkeypatch.setattr(
        "orchestration.agent_router.calculate_thread_costs",
        lambda ch, ts: {"grand_total_cost_usd": 5.00},
    )

    mock_choice = MagicMock()
    mock_choice.message.content = "Normal response despite high cost"
    mock_choice.message.tool_calls = None

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=10, completion_tokens=10)

    async def mock_run_in_threadpool(func, *args, **kwargs):
        return mock_response

    posted_messages = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted_messages.append({"text": text, "username": username})
        return {"ok": True, "ts": "12345.9999"}

    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent_response
    )
    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_run_in_threadpool
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )

    try:
        res = await process_and_respond(
            channel_id=channel_id,
            thread_ts=thread_ts,
            raw_text="[agent-10001] Normal query",
            is_bot_trigger=False,
        )

        assert res.get("ok") is True
        assert res.get("circuit_breaker_triggered") is not True
        assert len(posted_messages) == 1
        assert "Normal response despite high cost" in posted_messages[0]["text"]
    finally:
        set_circuit_breaker_enabled(True)
