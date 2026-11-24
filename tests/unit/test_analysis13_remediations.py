"""Unit tests for Analysis 13 Wave 2/3 remediations (TKT-1308, TKT-1309, TKT-1310, TKT-1311)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import BackgroundTasks
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_slack_response import AsyncSlackResponse


def test_singleton_locks_are_event_loop_aware(tmp_path: Path):
    """TKT-1308: SlackRateLimiter, PricingService, and SyncReconciliationEngine recreate locks across event loops."""
    from services.pricing_service import PricingService
    from services.rag.manifest_manager import ManifestManager
    from services.rag.sync_engine import SyncReconciliationEngine
    from services.rag.vector_store import VectorStore
    from services.rate_limiter import SlackRateLimiter

    limiter = SlackRateLimiter(rate=100.0, capacity=100.0)
    pricing = PricingService(cache_file=tmp_path / "pricing.json")
    sync_engine = SyncReconciliationEngine(
        knowledge_dir=tmp_path / "kb",
        manifest_manager=ManifestManager(manifest_path=tmp_path / "manifest.json"),
        store=VectorStore(db_dir=tmp_path / "db"),
    )

    async def _use_all() -> tuple[asyncio.Lock, asyncio.Lock, asyncio.Lock]:
        async with limiter.lock:
            pass
        async with pricing.refresh_lock:
            pass
        async with sync_engine.lock:
            pass
        return limiter.lock, pricing.refresh_lock, sync_engine.lock

    locks_run1 = asyncio.run(_use_all())
    locks_run2 = asyncio.run(_use_all())
    assert locks_run1[0] is not locks_run2[0]
    assert locks_run1[1] is not locks_run2[1]
    assert locks_run1[2] is not locks_run2[2]


@pytest.mark.anyio
async def test_slack_rate_limiter_enforced_and_open_dm_channel_validates_type():
    """TKT-1309 & TKT-1311: fetch_thread_files/history & post_thread_note acquire limiter; open_dm_channel returns str|None."""
    from core.config import configure_token, reset_token
    from services.slack.client_core import (
        configure_slack_async_client,
        open_dm_channel,
        reset_slack_async_client,
    )
    from services.slack.files import fetch_thread_files
    from services.slack.messages import fetch_thread_history, post_thread_note

    mock_client = MagicMock()
    # Unconfigured AsyncMock sub-attribute should NOT be returned as a valid DM ID
    unconfigured = AsyncMock()
    configure_slack_async_client(unconfigured)
    configure_token("xoxb-valid-real-token-for-unit-test")
    try:
        assert await open_dm_channel("U123") is None

        mock_client.conversations_open = AsyncMock(return_value={"channel": {"id": "D999"}})
        mock_client.conversations_replies = AsyncMock(return_value={"messages": [], "has_more": False})
        mock_client.chat_postMessage = AsyncMock(return_value={"ts": "1700.1"})
        mock_client.pins_add = AsyncMock(return_value={"ok": True})
        configure_slack_async_client(mock_client)

        assert await open_dm_channel("U123") == "D999"

        with patch("services.slack.files.slack_rate_limiter.acquire", new_callable=AsyncMock) as acq_files:
            await fetch_thread_files("C1", "1700.1")
            acq_files.assert_awaited_once()

        with patch("services.slack.messages.slack_rate_limiter.acquire", new_callable=AsyncMock) as acq_msg:
            await fetch_thread_history("C1", "1700.1")
            await post_thread_note("C1", "1700.1", "Note body", pin=True)
            # 1 acquire in fetch_thread_history + 2 in post_thread_note (post + pins_add)
            assert acq_msg.await_count == 3
    finally:
        reset_token()
        reset_slack_async_client()


@pytest.mark.anyio
async def test_workflow_presets_modal_error_isolation_and_app_home_dm_message():
    """TKT-1309: _open_workspace_context_modal_safe handles SlackApiError and save_workflow_preset posts DM message."""
    from adapters.slack.actions.workflow_presets import (
        _handle_open_edit_workspace_context_modal,
        _handle_save_workflow_preset_button,
    )

    bg = BackgroundTasks()
    mock_client = MagicMock()
    mock_client.views_open = AsyncMock(
        side_effect=SlackApiError(message="expired_trigger_id", response={"ok": False, "error": "expired_trigger_id"})
    )
    with patch("adapters.slack.actions.workflow_presets.get_slack_async_client", return_value=mock_client):
        await _handle_open_edit_workspace_context_modal({}, {"trigger_id": "T_EXP"}, "U_ADM", bg, {})
        assert len(bg.tasks) == 1
        # Must swallow SlackApiError cleanly inside background task without raising
        await bg.tasks[0]()

    with (
        patch("adapters.slack.actions.workflow_presets.save_workflow", side_effect=ValueError("reserved")),
        patch("adapters.slack.actions.workflow_presets.open_dm_channel", AsyncMock(return_value="D_ADM")),
        patch("adapters.slack.actions.workflow_presets.post_message_to_slack", AsyncMock(return_value={"ok": True})) as mock_dm,
    ):
        await _handle_save_workflow_preset_button(
            {},
            {},
            "U_ADM",
            BackgroundTasks(),
            {"b1": {"save_workflow_preset_name_input": {"value": "default_sandwich"}}},
        )
        mock_dm.assert_awaited_once()
        assert mock_dm.call_args.kwargs["channel_id"] == "D_ADM"


@pytest.mark.anyio
async def test_internal_revisor_handles_candidate_exception_without_losing_sibling_warning():
    """TKT-1310: run_internal_revisor isolates per-candidate exceptions via gather(return_exceptions=True)."""
    from orchestration.revisor_service import run_internal_revisor
    from orchestration.turn_pipeline import TurnContext

    ctx = TurnContext(channel_id="C_REV", thread_ts="1700000999.0001", raw_text="Verify facts", user_id="U1")
    ctx.agent_key = "agent-10002"
    ctx.agent_cfg = {"username": "Innovator", "model": "gpt-4o"}
    ctx.full_history = [{"role": "assistant", "content": "[Visionary]: Claim A"}]
    ctx.output.final_text = "Claim B"

    call_n = {"n": 0}

    async def _mock_verify(*, speaker, **kwargs):
        call_n["n"] += 1
        if speaker == "Visionary":
            raise RuntimeError("transient verifier failure")
        return 'ATTENTION: Agent [Innovator] hallucinated "Claim B".'

    with (
        patch("orchestration.revisor_service._verify_single_candidate", side_effect=_mock_verify),
        patch("orchestration.revisor_service.post_agent_response", AsyncMock(return_value={"ok": True})) as mock_post,
    ):
        warning = await run_internal_revisor(ctx)

    assert warning is not None
    assert "Claim B" in warning
    mock_post.assert_awaited_once()
    # Keep import check quiet if AsyncSlackResponse isn't directly constructed here
    assert AsyncSlackResponse is not None

    # TKT-1415: CancelledError from a candidate verifier must propagate (not be swallowed)
    async def _cancel_verify(*, speaker, **kwargs):
        raise asyncio.CancelledError()

    with patch("orchestration.revisor_service._verify_single_candidate", side_effect=_cancel_verify):
        with pytest.raises(asyncio.CancelledError):
            await run_internal_revisor(ctx)

