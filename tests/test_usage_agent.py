import asyncio
from unittest.mock import AsyncMock, patch

import pytest

import agents.usage_agent as usage_agent
from agents.usage_agent import (
    THREAD_USAGE_DATA,
    calculate_thread_costs,
    format_usage_slack_message,
    record_thread_tokens,
    trigger_usage_update,
    update_usage_for_thread,
)


@pytest.fixture(autouse=True)
def clean_usage_state(monkeypatch, tmp_path):
    temp_store = tmp_path / "test_thread_usage_data.json"
    monkeypatch.setattr(usage_agent, "USAGE_STORE_FILE", temp_store)
    THREAD_USAGE_DATA.clear()
    usage_agent.USAGE_LOCKS.clear()
    usage_agent._RUNNING_USAGE_TASKS.clear()
    usage_agent._PENDING_USAGE_UPDATES.clear()
    usage_agent._PENDING_USAGE_COMPLETED.clear()
    yield
    THREAD_USAGE_DATA.clear()
    usage_agent.USAGE_LOCKS.clear()
    usage_agent._RUNNING_USAGE_TASKS.clear()
    usage_agent._PENDING_USAGE_UPDATES.clear()
    usage_agent._PENDING_USAGE_COMPLETED.clear()
    if temp_store.exists():
        temp_store.unlink()


def test_record_thread_tokens_accumulation():
    channel_id = "C123"
    thread_ts = "1700000000.123456"

    # Turn 1: Gemini Visionary
    record_thread_tokens(
        channel_id=channel_id,
        thread_ts=thread_ts,
        model_name="gemini/gemini-3.7-flash",
        prompt_tokens=1000,
        completion_tokens=200,
        channel_name="#general",
    )

    # Turn 2: Mistral TechLead
    record_thread_tokens(
        channel_id=channel_id,
        thread_ts=thread_ts,
        model_name="mistral/mistral-large-latest",
        prompt_tokens=1500,
        completion_tokens=400,
    )

    # Turn 3: Scribe
    record_thread_tokens(
        channel_id=channel_id,
        thread_ts=thread_ts,
        model_name="mistral/mistral-small-latest",
        prompt_tokens=500,
        completion_tokens=150,
    )

    costs = calculate_thread_costs(channel_id, thread_ts)

    assert costs["grand_prompt_tokens"] == 3000
    assert costs["grand_completion_tokens"] == 750
    assert costs["grand_total_tokens"] == 3750

    # Model specific verification
    models = costs["models"]
    assert "gemini/gemini-3.7-flash" in models
    assert models["gemini/gemini-3.7-flash"]["prompt_tokens"] == 1000
    assert models["gemini/gemini-3.7-flash"]["completion_tokens"] == 200

    assert "mistral/mistral-large-latest" in models
    assert models["mistral/mistral-large-latest"]["prompt_tokens"] == 1500

    assert "mistral/mistral-small-latest" in models
    assert models["mistral/mistral-small-latest"]["total_tokens"] == 650

    assert costs["grand_total_cost_usd"] > 0.0


@pytest.mark.anyio
async def test_format_usage_slack_message_structure():
    channel_id = "C999"
    thread_ts = "1700000000.555555"

    record_thread_tokens(
        channel_id=channel_id,
        thread_ts=thread_ts,
        model_name="gemini/gemini-3.7-flash",
        prompt_tokens=2000,
        completion_tokens=500,
        channel_name="#dev-discussions",
    )

    msg = await format_usage_slack_message(channel_id, thread_ts, is_completed=False)

    # Must contain header items: Channel name, creation time, thread ts, status
    assert "#dev-discussions" in msg
    assert "Thread TS:" in msg
    assert "Status rasprave:" in msg
    assert "U tijeku" in msg

    # Must contain model breakdown
    assert "gemini/gemini-3.7-flash" in msg
    assert "Ulazni (Prompt):" in msg
    assert "Izlazni (Output):" in msg
    assert "UKUPNA POTROŠNJA THREADA:" in msg
    assert "UKUPAN TROŠAK:" in msg

    # Must contain exact URL references section
    assert "IZVORI TARIFA I WEB REFERENCE" in msg
    assert "https://" in msg


@pytest.mark.anyio
async def test_update_usage_for_thread_creates_and_updates_in_slack():
    channel_id = "C_TEST"
    thread_ts = "1700000000.111111"
    usage_channel_id = "C_USAGE"

    record_thread_tokens(
        channel_id=channel_id,
        thread_ts=thread_ts,
        model_name="gemini/gemini-3.7-flash",
        prompt_tokens=1000,
        completion_tokens=250,
    )

    with patch("agents.usage_agent.get_or_create_usage_channel", new_callable=AsyncMock) as mock_get_ch, \
         patch("agents.usage_agent.post_usage_message", new_callable=AsyncMock) as mock_post, \
         patch("agents.usage_agent.update_usage_message", new_callable=AsyncMock) as mock_update:

        mock_get_ch.return_value = usage_channel_id
        mock_post.return_value = {"ok": True, "ts": "usage_msg_ts_123"}
        mock_update.return_value = {"ok": True, "ts": "usage_msg_ts_123"}

        # 1. First invocation -> creates new message in usage channel
        res1 = await update_usage_for_thread(channel_id, thread_ts, is_completed=False)
        assert res1["ok"] is True
        mock_post.assert_awaited_once()
        mock_update.assert_not_called()

        assert THREAD_USAGE_DATA[(channel_id, thread_ts)]["usage_msg_ts"] == "usage_msg_ts_123"

        # 2. Second turn in same thread -> updates existing message in-place
        record_thread_tokens(
            channel_id=channel_id,
            thread_ts=thread_ts,
            model_name="mistral/mistral-large-latest",
            prompt_tokens=2000,
            completion_tokens=400,
        )

        res2 = await update_usage_for_thread(channel_id, thread_ts, is_completed=True)
        assert res2["ok"] is True
        mock_update.assert_awaited_once()
        call_kwargs = mock_update.call_args.kwargs
        assert call_kwargs["message_ts"] == "usage_msg_ts_123"
        assert "Završeno" in call_kwargs["text"]
