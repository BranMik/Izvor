import pytest
from unittest.mock import AsyncMock, patch

from core.config import set_internal_revisor_enabled_override
from orchestration.agent_router import continue_discussion_from_turn_result
from orchestration.thread_state import thread_state_manager
from orchestration.turn_pipeline import TurnContext, TurnPipeline


@pytest.mark.anyio
async def test_revisor_warning_injected_into_accumulated_history_before_next_speaker():
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
    ctx.output.final_text = "The project generated $100M last year."
    ctx.output.skills_used = ["knowledge_base_rag"]

    # 1. Enable revisor and mock it returning a hallucination warning
    set_internal_revisor_enabled_override(True)
    revisor_warning = "ATTENTION: The claim '$100M revenue' contradicts internal financial docs."

    async def mock_run_revisor(c):
        return revisor_warning

    pipeline = TurnPipeline()
    # Mock stages 1-4 returning None (no halt) and stage 5 returning base response
    pipeline.validation_stage.stage_validate_and_parse = AsyncMock(return_value=None)
    pipeline.resolution_stage.stage_resolve_agent_and_governance = AsyncMock(return_value=None)
    pipeline.context_prep_stage.stage_prepare_context = AsyncMock(return_value=None)
    pipeline.execution_stage.stage_execute_llm = AsyncMock(return_value=None)
    pipeline.post_record_stage.stage_post_and_record = AsyncMock(
        return_value={"ok": True, "final_text": ctx.output.final_text, "username": "Visionary"}
    )

    with patch("orchestration.revisor_service.run_internal_revisor", side_effect=mock_run_revisor):
        slack_res = await pipeline.run(ctx)
    assert slack_res.get("revisor_warning") == revisor_warning
    assert ctx.output.revisor_warning == revisor_warning

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
