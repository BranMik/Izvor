import pytest
from unittest.mock import AsyncMock, MagicMock, patch

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


@pytest.mark.anyio
async def test_revisor_skips_on_pure_missing_skill_notice():
    """Verifies Way 3: InternalRevisor skips execution on pure <= 65 word missing-skill notice without tools."""
    from orchestration.revisor_service import run_internal_revisor

    channel_id = "C_REVISOR_SKIP_TEST"
    thread_ts = "99999.0001"
    thread_state_manager.clear_state(channel_id, thread_ts)

    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="Read file database.sql",
        is_bot_trigger=True,
    )
    # Turn 2 (assistant_msgs count = 1 -> turn_count = 2)
    ctx.full_history = [
        {"role": "user", "content": "Read file database.sql"},
        {"role": "assistant", "content": "First response."},
    ]
    ctx.agent_key = "agent-10001"
    ctx.agent_cfg = {"model": "test-model", "username": "Visionary", "skills": []}
    ctx.missing_required_skills = ["document_reader"]
    ctx.output.skills_used = []
    # Short <= 50-word notice with next tag and token footer
    ctx.output.final_text = (
        "I do not have the 'document_reader' skill required to read database.sql. "
        "Authorized for 'document_reader': ROIStrategist, SecurityArchitect. "
        "/[agent-10008] next {test-model : 15/25}"
    )

    with patch("orchestration.revisor_service.execute_llm_with_retry") as mock_exec:
        result = await run_internal_revisor(ctx)
        assert result is None
        mock_exec.assert_not_called()


@pytest.mark.anyio
async def test_revisor_executes_on_partial_prompt_continuation():
    """Verifies Way 3: InternalRevisor runs when the agent also evaluates other parts of the prompt (> 65 words)."""
    from orchestration.revisor_service import run_internal_revisor

    channel_id = "C_REVISOR_PARTIAL_TEST"
    thread_ts = "99999.0002"
    thread_state_manager.clear_state(channel_id, thread_ts)

    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="Read database.sql and evaluate our market expansion plan.",
        is_bot_trigger=True,
    )
    ctx.full_history = [
        {"role": "user", "content": "Read database.sql and evaluate our market expansion plan."},
        {"role": "assistant", "content": "First response."},
    ]
    ctx.agent_key = "agent-10001"
    ctx.agent_cfg = {"model": "test-model", "username": "Visionary", "skills": []}
    ctx.missing_required_skills = ["document_reader"]
    ctx.output.skills_used = []
    # Partial prompt: missing skill notice + long evaluation of market expansion (> 55 words)
    ctx.output.final_text = (
        "I lack the document_reader skill to read database.sql. "
        "However, evaluating the market expansion plan directly: "
        "The proposed timeline demonstrates strong strategic viability across emerging markets. "
        "Core growth metrics indicate a 40% target expansion rate, which aligns with baseline industry benchmarks. "
        "Key operational dependencies require cross-regional alignment before proceeding with deployment. "
        "We recommend prioritizing initial pilot rollouts in tier-one territories to minimize friction, "
        "establishing robust feedback loops with localized customer segments, and ensuring continuous metric tracking. "
        "/[agent-10002] next {test-model : 100/120}"
    )

    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock(message=MagicMock(content="NO_HALLUCINATION"))]
    mock_resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5)

    with patch("orchestration.revisor_service.execute_llm_with_retry", AsyncMock(return_value=mock_resp)) as mock_exec:
        result = await run_internal_revisor(ctx)
        assert result is None  # NO_HALLUCINATION returns None
        mock_exec.assert_called_once()
