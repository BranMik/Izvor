import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest

from orchestration.turn_pipeline import TurnContext, TurnPipeline


def test_turn_pipeline_di_priority():
    mock_llm = MagicMock(return_value="mock_llm_result")
    mock_poster = AsyncMock(return_value={"ok": True, "ts": "111.222"})
    mock_history = AsyncMock(return_value=[])
    mock_files = AsyncMock(return_value=[])
    mock_tokens = MagicMock()
    mock_notes = MagicMock()
    mock_usage = MagicMock()

    pipeline = TurnPipeline(
        llm_executor=mock_llm,
        slack_poster=mock_poster,
        history_fetcher=mock_history,
        files_fetcher=mock_files,
        token_recorder=mock_tokens,
        notes_updater=mock_notes,
        usage_updater=mock_usage,
    )

    # Injected dependencies must be returned by _get_hook
    assert pipeline._get_hook("execute_llm_with_retry") == mock_llm
    assert pipeline._get_hook("post_agent_response") == mock_poster
    assert pipeline._get_hook("fetch_thread_history") == mock_history
    assert pipeline._get_hook("fetch_thread_files") == mock_files
    assert pipeline._get_hook("record_thread_tokens") == mock_tokens
    assert pipeline._get_hook("trigger_thread_notes_update") == mock_notes
    assert pipeline._get_hook("trigger_usage_update") == mock_usage


@pytest.mark.anyio
async def test_turn_pipeline_custom_sleep_fn():
    mock_sleep = AsyncMock()
    pipeline = TurnPipeline(sleep_fn=mock_sleep)

    await pipeline._sleep(2.5)
    mock_sleep.assert_awaited_once_with(2.5)


def test_turn_pipeline_fallback_to_defaults():
    pipeline = TurnPipeline()

    # Without router or DI, resolves canonical implementations
    llm_fn = pipeline._get_hook("execute_llm_with_retry")
    assert callable(llm_fn)

    poster_fn = pipeline._get_hook("post_agent_response")
    assert callable(poster_fn)
