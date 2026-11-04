from unittest.mock import AsyncMock, patch

import pytest

from agents import usage_agent
from agents.usage_agent import (
    calculate_thread_costs,
    format_usage_slack_message,
    record_thread_tokens,
    update_usage_for_thread,
    usage_tracker,
)


@pytest.fixture(autouse=True)
def clean_usage_state(tmp_path):
    temp_store = tmp_path / "test_thread_usage_data.json"
    usage_tracker.configure_paths(store_file=temp_store)
    usage_tracker.thread_usage_data.clear()
    usage_tracker.reset_runtime_state()
    yield
    usage_tracker.reset_paths()
    usage_tracker.thread_usage_data.clear()
    usage_tracker.reset_runtime_state()
    if temp_store.exists():
        temp_store.unlink()


def test_record_thread_tokens_accumulation():
    channel_id = "C123"
    thread_ts = "1700000000.123456"

    # Turn 1: Gemini Visionary
    record_thread_tokens(
        channel_id=channel_id,
        thread_ts=thread_ts,
        model_name="gemini/gemini-3.8-flash",
        prompt_tokens=1000,
        completion_tokens=200,
        channel_name="#general",
    )

    # Turn 2: Mistral LegalAdvisor
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
    assert "gemini/gemini-3.8-flash" in models
    assert models["gemini/gemini-3.8-flash"]["prompt_tokens"] == 1000
    assert models["gemini/gemini-3.8-flash"]["completion_tokens"] == 200

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
        model_name="gemini/gemini-3.8-flash",
        prompt_tokens=2000,
        completion_tokens=500,
        channel_name="#dev-discussions",
    )

    msg = await format_usage_slack_message(channel_id, thread_ts, is_completed=False)

    # Must contain header items: Channel name, creation time, thread ts, status
    assert "#dev-discussions" in msg
    assert "Thread TS:" in msg
    assert "Discussion Status:" in msg
    assert "In Progress / Active" in msg

    # Must contain model breakdown
    assert "gemini/gemini-3.8-flash" in msg
    assert "Prompt (Input):" in msg
    assert "Completion (Output):" in msg
    assert "GRAND TOTAL THREAD USAGE:" in msg
    assert "TOTAL COST:" in msg

    # Must contain exact URL references section
    assert "PRICING SOURCES & WEB REFERENCES" in msg
    assert "https://" in msg

    # Must contain orientational pricing / token consumption notice
    assert "METRICS NOTICE" in msg
    assert "token consumption" in msg


@pytest.mark.anyio
async def test_update_usage_for_thread_creates_and_updates_in_slack():
    channel_id = "C_TEST"
    thread_ts = "1700000000.111111"
    usage_channel_id = "C_USAGE"

    record_thread_tokens(
        channel_id=channel_id,
        thread_ts=thread_ts,
        model_name="gemini/gemini-3.8-flash",
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

        assert usage_tracker.thread_usage_data[(channel_id, thread_ts)]["usage_msg_ts"] == "usage_msg_ts_123"

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
        assert "Completed" in call_kwargs["text"]


@pytest.mark.anyio
async def test_no_cache_info_in_usage_message():
    channel_id = "C_CACHE_TEST"
    thread_ts = "1700000000.777777"

    # Record tokens for model
    record_thread_tokens(
        channel_id=channel_id,
        thread_ts=thread_ts,
        model_name="openai/gpt-5.6-terra",
        prompt_tokens=2048,
        completion_tokens=500,
        channel_name="#cache-test",
    )

    costs = calculate_thread_costs(channel_id, thread_ts)
    model_stats = costs["models"]["openai/gpt-5.6-terra"]

    # Verify cached_tokens is NOT tracked in model stats or grand totals
    assert "cached_tokens" not in model_stats
    assert "grand_cached_tokens" not in costs

    # Verify cost calculation
    in_rate = model_stats["input_price_per_1m"]
    out_rate = model_stats["output_price_per_1m"]
    expected_cost = (2048 / 1_000_000.0) * in_rate + (500 / 1_000_000.0) * out_rate
    assert model_stats["total_cost_usd"] == pytest.approx(expected_cost)
    assert costs["grand_total_cost_usd"] == pytest.approx(expected_cost)

    # Verify format_usage_slack_message contains NO cache info
    msg = await format_usage_slack_message(channel_id, thread_ts, is_completed=False)
    assert "• *`openai/gpt-5.6-terra`*" in msg
    assert "Predmemorirani" not in msg
    assert "Cached" not in msg
    assert "predmemoriranih" not in msg


def test_sqlite_persistence_and_legacy_migration(tmp_path):
    import json
    from core.database import get_connection
    from agents.usage_agent import ThreadUsageTracker

    # 1. Test SQLite persistence
    record_thread_tokens(
        channel_id="C_SQLITE",
        thread_ts="1700000000.888888",
        model_name="gemini/gemini-3.8-flash",
        prompt_tokens=3000,
        completion_tokens=600,
        channel_name="#sqlite-test",
    )

    with get_connection() as conn:
        meta_row = conn.execute(
            "SELECT * FROM thread_usage_meta WHERE channel_id = ? AND thread_ts = ?;",
            ("C_SQLITE", "1700000000.888888"),
        ).fetchone()
        assert meta_row is not None
        assert meta_row["channel_name"] == "#sqlite-test"

        ledger_row = conn.execute(
            "SELECT SUM(prompt_tokens) as p, SUM(completion_tokens) as c FROM spend_ledger WHERE channel_id = ? AND thread_ts = ?;",
            ("C_SQLITE", "1700000000.888888"),
        ).fetchone()
        assert ledger_row["p"] == 3000
        assert ledger_row["c"] == 600

    # 2. Test SQLite persistence and metadata synchronization
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO thread_usage_meta (channel_id, thread_ts, channel_name, created_at_str, usage_msg_ts, is_completed, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            ("C_LEGACY", "1700000000.999999", "#legacy-channel", "2026-01-01 10:00:00", "legacy_msg_ts_1", 1, 1700000000.0),
        )

    tracker = ThreadUsageTracker()
    assert ("C_LEGACY", "1700000000.999999") in tracker.thread_usage_data
    assert tracker.thread_usage_data[("C_LEGACY", "1700000000.999999")]["is_completed"] is True

    # Mutate and sync to DB
    tracker.thread_usage_data[("C_LEGACY", "1700000000.999999")]["channel_name"] = "#updated-channel"
    tracker._sync_meta_to_db("C_LEGACY", "1700000000.999999")

    with get_connection() as conn:
        migrated_row = conn.execute(
            "SELECT * FROM thread_usage_meta WHERE channel_id = ? AND thread_ts = ?;",
            ("C_LEGACY", "1700000000.999999"),
        ).fetchone()
        assert migrated_row is not None
        assert migrated_row["channel_name"] == "#updated-channel"
        assert migrated_row["is_completed"] == 1

