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


@pytest.mark.anyio
async def test_tool_loop_runner_skips_followup_on_final_iteration_even_with_preamble():
    """Plan 220: On max_tool_iterations, ToolLoopRunner skips redundant tool-enabled follow-up and runs synthesis even if preamble text was emitted."""
    captured_kwargs: list[dict] = []

    async def mock_exec(func, kwargs, **rest):
        captured_kwargs.append(dict(kwargs))
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="Final synthesized answer.", tool_calls=None))],
            usage=None,
        )

    async def mock_tool(fn_name: str, fn_args: dict) -> str:
        return '{"results": ["Pinecone", "Qdrant"]}'

    mock_reg = SimpleNamespace(
        find_skill_for_tool=lambda fn: "web_research",
        get=lambda name: None,
        execute_tool_async=AsyncMock(return_value="ok"),
    )

    tc = SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(name="web_search", arguments='{"query": "vector db"}'),
    )
    initial_msg = SimpleNamespace(
        content="Let me search for vector databases first.",
        tool_calls=[tc],
        model_dump=lambda: {"role": "assistant", "content": "Let me search for vector databases first."},
    )

    ctx = TurnContext(channel_id="C_FIN", thread_ts="200.3", raw_text="Compare vector DBs")
    ctx.agent_key = "agent-10009"
    ctx.agent_cfg = {"model": "mistral/mistral-large-latest", "skills": ["web_research"]}
    ctx.messages = [{"role": "user", "content": "Compare vector DBs"}]
    ctx.turn_token_mapping = {}

    runner = ToolLoopRunner(
        PipelineDeps(
            llm_executor=mock_exec,
            tool_executor=mock_tool,
            skill_registry=mock_reg,
            max_tool_iterations=1,
        )
    )
    from unittest.mock import patch

    with patch("services.local_tools.is_agent_authorized_for_tool", return_value=True):
        res = await runner.run(
            ctx,
            active_tools=[{"type": "function", "function": {"name": "web_search"}}],
            agent_skills=["web_research"],
            expired_thread_skills=[],
            thread_ttl_hours=1.0,
            initial_response_message=initial_msg,
        )

    assert res is None
    assert len(captured_kwargs) == 1
    assert "tools" not in captured_kwargs[0]
    assert ctx.output.final_text == "Final synthesized answer."


def test_turn_finalizer_resolve_synthesis_timeout_honors_system_settings():
    """Plan 220: TurnFinalizer honors tool_call_loop_timeout_seconds without a hardcoded 30s ceiling."""
    from config_store.system_settings import system_settings_manager

    orig_timeout = system_settings_manager.get_tool_call_loop_timeout_seconds()
    try:
        system_settings_manager.set_tool_call_loop_timeout_seconds(60.0, save=False)
        finalizer_default = TurnFinalizer(PipelineDeps())
        assert finalizer_default.resolve_synthesis_timeout() == 60.0

        finalizer_override = TurnFinalizer(PipelineDeps(timeout_secs=45.0))
        assert finalizer_override.resolve_synthesis_timeout() == 45.0
    finally:
        system_settings_manager.set_tool_call_loop_timeout_seconds(orig_timeout, save=False)


@pytest.mark.anyio
async def test_turn_finalizer_logs_exception_type_and_sets_fallback_on_force_synthesis_failure(caplog):
    """Plan 220: Empty-message TimeoutError and RuntimeError during forced synthesis log type name and populate fallback text."""
    import logging

    # Case 1: Empty TimeoutError() with tool evidence
    ctx_to = TurnContext(channel_id="C_FIN", thread_ts="200.4", raw_text="Compare")
    ctx_to.agent_key = "agent-10009"
    ctx_to.agent_cfg = {"model": "mistral/mistral-large-latest"}
    ctx_to.messages = [
        {"role": "user", "content": "Compare"},
        {"role": "tool", "tool_call_id": "c1", "name": "web_search", "content": "Pinecone data"},
    ]
    ctx_to.turn_token_mapping = {}

    finalizer_to = TurnFinalizer(PipelineDeps(llm_executor=AsyncMock(side_effect=TimeoutError())))
    with caplog.at_level(logging.WARNING):
        await finalizer_to.synthesize_if_needed(
            ctx_to,
            response_message=SimpleNamespace(content="", tool_calls=[]),
            tool_calls=None,
            timed_out_with_evidence=False,
            iteration=3,
            max_tool_iterations=3,
            allow_503_immediate=False,
            force_synthesis=True,
        )
    assert "TimeoutError" in caplog.text
    assert "timed out" in ctx_to.output.final_text.lower()

    # Case 2: RuntimeError with tool evidence
    ctx_err = TurnContext(channel_id="C_FIN", thread_ts="200.5", raw_text="Compare")
    ctx_err.agent_key = "agent-10009"
    ctx_err.agent_cfg = {"model": "mistral/mistral-large-latest"}
    ctx_err.messages = [
        {"role": "user", "content": "Compare"},
        {"role": "tool", "tool_call_id": "c1", "name": "web_search", "content": "Qdrant data"},
    ]
    ctx_err.turn_token_mapping = {}

    finalizer_err = TurnFinalizer(PipelineDeps(llm_executor=AsyncMock(side_effect=RuntimeError("provider 502"))))
    with caplog.at_level(logging.WARNING):
        await finalizer_err.synthesize_if_needed(
            ctx_err,
            response_message=SimpleNamespace(content="", tool_calls=[]),
            tool_calls=None,
            timed_out_with_evidence=False,
            iteration=3,
            max_tool_iterations=3,
            allow_503_immediate=False,
            force_synthesis=True,
        )
    assert "RuntimeError: provider 502" in caplog.text
    assert "unable to complete tool synthesis" in ctx_err.output.final_text.lower()

