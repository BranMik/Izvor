from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestration.turn_pipeline import TurnPipeline


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

    # Injected dependencies must be stored on pipeline.deps and passed to stages
    assert pipeline.deps.llm_executor == mock_llm
    assert pipeline.deps.slack_poster == mock_poster
    assert pipeline.deps.history_fetcher == mock_history
    assert pipeline.deps.files_fetcher == mock_files
    assert pipeline.deps.token_recorder == mock_tokens
    assert pipeline.deps.notes_updater == mock_notes
    assert pipeline.deps.usage_updater == mock_usage


@pytest.mark.anyio
async def test_turn_pipeline_custom_sleep_fn():
    mock_sleep = AsyncMock()
    pipeline = TurnPipeline(sleep_fn=mock_sleep)

    await pipeline._sleep(2.5)
    mock_sleep.assert_awaited_once_with(2.5)


def test_turn_pipeline_fallback_to_defaults():
    pipeline = TurnPipeline()

    # Fallbacks resolve canonical implementations on pipeline.deps
    assert callable(pipeline.deps.llm_executor)
    assert callable(pipeline.deps.slack_poster)
    assert callable(pipeline.deps.history_fetcher)
