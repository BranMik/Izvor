import pytest
from unittest.mock import AsyncMock, MagicMock

from orchestration.agent_router import continue_discussion_from_turn_result
from orchestration.thread_state import thread_state_manager
from orchestration.turn_pipeline import TurnContext, TurnPipeline


@pytest.mark.anyio
async def test_revisor_warning_injected_into_accumulated_history_before_next_speaker(monkeypatch):
    """Verifies TKT-820: InternalRevisor verdict is attached to slack_res and injected into accumulated_history before next speaker."""
    channel_id = "C_REVISOR_TEST"
    thread_ts = "88888.0001"
    thread_state_manager.clear_state(channel_id, thread_ts)

    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="Tell me about project revenue.",
        is_bot_trigger=False,
    )
    ctx.agent_key = "agent-10001"
    ctx.agent_cfg = {"model": "test-model", "username": "Visionary"}
    ctx.final_text = "The project generated $100M last year."
    ctx.skills_used = ["knowledge_base_rag"]

    # 1. Enable revisor and mock it returning a hallucination warning
    monkeypatch.setattr("core.config.is_internal_revisor_enabled", lambda: True)
    revisor_warning = "ATTENTION: The claim '$100M revenue' contradicts internal financial docs."

    async def mock_run_revisor(c):
        return revisor_warning

    monkeypatch.setattr("orchestration.revisor_service.run_internal_revisor", mock_run_revisor)

    pipeline = TurnPipeline()
    # Mock stages 1-4 returning None (no halt) and stage 5 returning base response
    monkeypatch.setattr(pipeline.validation_stage, "stage_validate_and_parse", AsyncMock(return_value=None))
    monkeypatch.setattr(pipeline.resolution_stage, "stage_resolve_agent_and_governance", AsyncMock(return_value=None))
    monkeypatch.setattr(pipeline.context_prep_stage, "stage_prepare_context", AsyncMock(return_value=None))
    monkeypatch.setattr(pipeline.execution_stage, "stage_execute_llm", AsyncMock(return_value=None))
    monkeypatch.setattr(
        pipeline.post_record_stage,
        "stage_post_and_record",
        AsyncMock(return_value={"ok": True, "final_text": ctx.final_text, "username": "Visionary"}),
    )

    slack_res = await pipeline.run(ctx)
    assert slack_res.get("revisor_warning") == revisor_warning
    assert ctx.revisor_warning == revisor_warning

    # 2. Verify that continue_discussion_from_turn_result injects revisor warning into accumulated_history
    history = [{"role": "user", "content": "Tell me about project revenue."}]
    # Mark stop so it doesn't run full turn loop
    thread_state_manager.request_stop(channel_id, thread_ts)

    await continue_discussion_from_turn_result(
        channel_id=channel_id,
        thread_ts=thread_ts,
        turn_res=slack_res,
        accumulated_history=history,
    )

    system_msgs = [m for m in history if m.get("role") == "system" and "[Internal Revisor Warning]:" in m.get("content", "")]
    assert len(system_msgs) == 1
    assert revisor_warning in system_msgs[0]["content"]
