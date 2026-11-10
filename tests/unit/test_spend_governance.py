import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.database import get_connection, get_db_transaction
from core.llm_utils import execute_llm_with_retry
from pipeline.spend_governance.circuit_breaker import SpendGovernance


def test_daily_budget_enforced_when_circuit_breaker_disabled():
    now_ts = time.time()
    with get_db_transaction(immediate=True) as conn:
        conn.execute(
            "INSERT INTO spend_ledger (channel_id, thread_ts, model_name, prompt_tokens, completion_tokens, cost_usd, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("C_DAILY_TEST", "T_DAILY_TEST", "mock-model", 1000, 1000, 15.00, now_ts),
        )

    sg = SpendGovernance()
    # Even with enabled=False, daily_budget_usd=10.0 should trip
    tripped, spend, limit = sg.is_budget_exceeded(
        session_id="test_sess",
        threshold_usd=50.0,
        enabled=False,
        daily_budget_usd=10.0,
    )
    assert tripped is True
    assert spend >= 15.00
    assert limit == 10.00

    # With daily_budget_usd=20.0, daily spend ($15) does not exceed limit
    # And because enabled=False, per-thread check returns False
    tripped2, spend2, limit2 = sg.is_budget_exceeded(
        session_id="test_sess",
        threshold_usd=5.0,
        current_cost_usd=8.0,
        enabled=False,
        daily_budget_usd=20.0,
    )
    assert tripped2 is False
    assert spend2 == 0.0
    assert limit2 == 0.0


@pytest.mark.anyio
async def test_execute_llm_with_retry_defaults_num_retries_zero():
    captured_kwargs = {}

    async def mock_fn(**kwargs):
        captured_kwargs.update(kwargs)
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content="ok"))]
        return mock_resp

    kwargs_passed = {"model": "test-model", "messages": []}
    await execute_llm_with_retry(
        mock_fn,
        kwargs_passed,
        agent_key="test_agent",
        thread_key=("channel_1", "thread_1"),
    )
    assert captured_kwargs.get("num_retries") == 0


@pytest.mark.anyio
async def test_embeddings_records_usage_to_spend_governance():
    from services.rag.embeddings import EmbeddingService

    mock_resp = MagicMock()
    mock_resp.data = [{"embedding": [0.1, 0.2]}]
    mock_resp.usage = MagicMock(prompt_tokens=55)

    with patch("services.rag.embeddings.litellm.aembedding", AsyncMock(return_value=mock_resp)):
        svc = EmbeddingService()
        await svc._embed_batch(["text1", "text2"])

    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM spend_ledger WHERE channel_id = ? AND thread_ts = ? ORDER BY id DESC LIMIT 1",
            ("system", "embeddings"),
        ).fetchone()
        assert row is not None
        assert row["prompt_tokens"] == 55
        assert row["completion_tokens"] == 0
        assert row["model_name"] == svc.model


@pytest.mark.anyio
async def test_pricing_service_records_spend_to_spend_governance():
    from services.pricing_service import PricingService, get_pricing_agent_model

    mock_resp = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.tool_calls = None
    mock_choice.message.content = '{"input_price_per_1m": 1.5, "output_price_per_1m": 3.0}'
    mock_resp.choices = [mock_choice]
    mock_resp.usage = MagicMock(prompt_tokens=120, completion_tokens=40)

    pricing_svc = PricingService()
    expected_model = get_pricing_agent_model()
    with patch(
        "services.pricing_service.execute_llm_with_retry",
        AsyncMock(return_value=mock_resp),
    ):
        await pricing_svc.fetch_pricing_with_agent("test-model")

    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM spend_ledger WHERE channel_id = ? AND thread_ts = ? ORDER BY id DESC LIMIT 1",
            ("system", "pricing:test-model"),
        ).fetchone()
        assert row is not None
        assert row["prompt_tokens"] == 120
        assert row["completion_tokens"] == 40
        assert row["model_name"] == expected_model
