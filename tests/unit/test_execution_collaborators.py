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
    handler.apply_tool_effect(
        fn_name="request_user_clarification",
        fn_args={"question": "Which region?"},
        ctx=ctx2,
    )
    assert ctx2.control.clarification_requested is True
    assert ctx2.control.clarification_question == "Which region?"
    assert ctx2.output.halted is True


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


def test_execution_stage_exposes_tool_loop_runner_properties():
    """TKT-1229: ExecutionStage delegates skill_registry and spend_governance to ToolLoopRunner."""
    mock_reg = object()
    mock_gov = object()
    stage = ExecutionStage(PipelineDeps(skill_registry=mock_reg, spend_governance=mock_gov))
    assert isinstance(stage.tool_loop_runner, ToolLoopRunner)
    assert stage.skill_registry is mock_reg
    assert stage.spend_governance is mock_gov

