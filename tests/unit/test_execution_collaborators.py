from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestration.protocols import PipelineDeps
from orchestration.stages.control_plane_handler import ControlPlaneHandler
from orchestration.stages.execution import ExecutionStage
from orchestration.stages.tool_loop_runner import ToolLoopRunner
from orchestration.stages.turn_finalizer import TurnFinalizer, build_completion_kwargs
from orchestration.turn_pipeline import TurnContext


def test_execution_stage_deps_setter_synchronizes_collaborators():
    """TKT-1229: Updating ExecutionStage.deps propagates updated dependencies to all collaborators."""
    deps1 = PipelineDeps(max_tool_iterations=2)
    stage = ExecutionStage(deps1)

    assert stage.control_plane_handler.deps is deps1
    assert stage.turn_finalizer.deps is deps1
    assert stage.tool_loop_runner.deps is deps1

    deps2 = PipelineDeps(max_tool_iterations=5)
    stage.deps = deps2

    assert stage.deps is deps2
    assert stage.control_plane_handler.deps is deps2
    assert stage.turn_finalizer.deps is deps2
    assert stage.tool_loop_runner.deps is deps2
    assert stage.tool_loop_runner.control_plane_handler.deps is deps2
    assert stage.tool_loop_runner.turn_finalizer.deps is deps2


def test_control_plane_handler_applies_nomination_and_clarification():
    """TKT-1229: ControlPlaneHandler ignores self-nomination and sets clarification/halted state."""
    ctx = TurnContext(channel_id="C_CP", thread_ts="100.1", raw_text="Discuss")
    ctx.agent_key = "agent-10001"
    handler = ControlPlaneHandler(PipelineDeps())

    # Valid peer nomination
    handler.apply_tool_effect(
        fn_name="nominate_next_speaker",
        fn_args={"agent_name": "agent-10002", "rationale": "Security domain"},
        ctx=ctx,
    )
    assert ctx.control.nomination == "agent-10002"
    assert ctx.control.nomination_rationale == "Security domain"

    # Self-nomination ignored
    ctx2 = TurnContext(channel_id="C_CP", thread_ts="100.2", raw_text="Discuss")
    ctx2.agent_key = "agent-10001"
    handler.apply_tool_effect(
        fn_name="nominate_next_speaker",
        fn_args={"agent_name": "agent-10001", "rationale": "Me"},
        ctx=ctx2,
    )
    assert ctx2.control.nomination is None

    # Clarification halts turn
    err_none = handler.apply_tool_effect(
        fn_name="request_user_clarification",
        fn_args={"question": "Which region?"},
        ctx=ctx2,
    )
    assert err_none is None
    assert ctx2.control.clarification_requested is True
    assert ctx2.control.clarification_question == "Which region?"
    assert ctx2.output.halted is True

    # Empty clarification & ineligible speaker are rejected (CB-11)
    ctx3 = TurnContext(channel_id="C_CP", thread_ts="100.3", raw_text="Discuss")
    ctx3.agent_key = "agent-10001"
    ctx3.eligible_remaining_agents = ["agent-10002"]
    bad_clar = handler.apply_tool_effect(
        fn_name="request_user_clarification",
        fn_args={"question": "   "},
        ctx=ctx3,
    )
    assert bad_clar is not None and '"isError": true' in bad_clar
    assert ctx3.control.clarification_requested is False
    assert ctx3.output.halted is False

    bad_nom = handler.apply_tool_effect(
        fn_name="nominate_next_speaker",
        fn_args={"agent_name": "agent-99999", "rationale": "Who?"},
        ctx=ctx3,
    )
    assert bad_nom is not None and '"isError": true' in bad_nom
    assert ctx3.control.nomination is None

    # _is_tool_error handles 4-key MCP error JSON, indent=2 JSON, >300 char JSON, and RAGService errors
    from orchestration.stages.tool_loop_runner import _is_tool_error

    assert _is_tool_error('{"server": "db", "tool": "q", "error": "boom", "isError": true}') is True
    assert _is_tool_error('{\n  "error": "' + ("z" * 350) + '"\n}') is True
    assert _is_tool_error("Error executing knowledge base search: broken") is True


@pytest.mark.anyio
async def test_turn_finalizer_runs_tool_free_synthesis_when_timed_out_with_evidence():
    """TKT-1229: TurnFinalizer appends synthesis prompt and extracts text after timeout with evidence."""
    synth_resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="Synthesized answer."))],
        usage=None,
    )
    mock_exec = AsyncMock(return_value=synth_resp)
    finalizer = TurnFinalizer(PipelineDeps(llm_executor=mock_exec))

    ctx = TurnContext(channel_id="C_FIN", thread_ts="200.1", raw_text="Summarize")
    ctx.agent_key = "agent-10001"
    ctx.agent_cfg = {"model": "gemini/gemini-3.8-flash"}
    ctx.messages = [{"role": "user", "content": "Summarize"}]
    ctx.turn_token_mapping = {}

    res = await finalizer.synthesize_if_needed(
        ctx,
        response_message=SimpleNamespace(content=""),
        tool_calls=None,
        timed_out_with_evidence=True,
        iteration=1,
        max_tool_iterations=3,
        allow_503_immediate=False,
    )
    assert res is None
    assert mock_exec.await_count == 1
    assert ctx.output.final_text == "Synthesized answer."

    kwargs = build_completion_kwargs(model="openai/gpt-4o", messages=[{"role": "user", "content": "hi"}])
    assert kwargs["model"] == "openai/gpt-4o"

    # Bounded synthesis timeout fallback (TKT-1411)
    ctx_err = TurnContext(channel_id="C_FIN", thread_ts="200.2", raw_text="Summarize")
    ctx_err.agent_key = "agent-10001"
    ctx_err.agent_cfg = {"model": "gemini/gemini-3.8-flash"}
    ctx_err.messages = [{"role": "user", "content": "Summarize"}]
    ctx_err.turn_token_mapping = {}
    failing_finalizer = TurnFinalizer(PipelineDeps(llm_executor=AsyncMock(side_effect=TimeoutError("synth timeout"))))
    await failing_finalizer.synthesize_if_needed(
        ctx_err,
        response_message=SimpleNamespace(content=""),
        tool_calls=None,
        timed_out_with_evidence=True,
        iteration=1,
        max_tool_iterations=3,
        allow_503_immediate=False,
    )
    assert "unable to complete" in ctx_err.output.final_text.lower() or "timed out" in ctx_err.output.final_text.lower()


def test_execution_stage_exposes_tool_loop_runner_properties():
    """TKT-1229: ExecutionStage delegates skill_registry and spend_governance to ToolLoopRunner."""
    mock_reg = object()
    mock_gov = object()
    stage = ExecutionStage(PipelineDeps(skill_registry=mock_reg, spend_governance=mock_gov))
    assert isinstance(stage.tool_loop_runner, ToolLoopRunner)
    assert stage.skill_registry is mock_reg
    assert stage.spend_governance is mock_gov

