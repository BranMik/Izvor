"""
Unit tests for Structured Control Plane, Dialectical Stress-Testing, Scribe Domain Fields,
and Observability Span Traces (PrePublish 8 Phase 4 / Epic C6 & tickets TKT-817, 818, 819, 822).
"""

from __future__ import annotations

import pytest

from orchestration.nomination import enforce_valid_nomination
from pipeline.discussion_scribe.scribe import (
    _extract_brief_fields,
    _extract_conclusion_fields,
)
from pipeline.orchestrator.sandwich import SandwichOrchestrator
from services.telemetry import get_turn_traces, record_turn_trace
from skills.loader import skill_registry


@pytest.mark.anyio
async def test_control_plane_skill_registered_and_executable():
    skill = skill_registry.get("control_plane")
    assert skill is not None
    assert skill.is_global
    tool_names = [s.get("function", {}).get("name") for s in skill.get_tool_schemas()]
    assert "nominate_next_speaker" in tool_names
    assert "request_user_clarification" in tool_names
    assert "signal_consensus_convergence" in tool_names

    # Test executing nominate_next_speaker
    res = await skill_registry.execute_tool_async(
        "nominate_next_speaker",
        {"agent_name": "agent-10002", "rationale": "Needs technical architecture review"},
        agent_name="agent-10001",
    )
    assert "agent-10002" in res
    assert "nominated" in res

    # Test executing signal_consensus_convergence
    res = await skill_registry.execute_tool_async(
        "signal_consensus_convergence",
        {"summary_of_agreement": "Agreed on SQLite WAL architecture."},
        agent_name="agent-10001",
    )
    assert "consensus_signaled" in res


def test_enforce_valid_nomination_with_control_plane_override():
    base_text = "Here are my final thoughts on the migration."
    remaining = ["agent-10002", "agent-10003"]

    # When control plane explicitly nominates agent-10002
    result = enforce_valid_nomination(
        text=base_text,
        current_agent_key="agent-10001",
        remaining=remaining,
        force_termination=False,
        control_plane_nomination="agent-10002",
    )
    assert "/[agent-10002]" in result or "[agent-10002]" in result


def test_arbitrator_persona_and_standby_topology():
    from orchestration.agent_settings import get_challenger_agent, get_arbitrator_agent

    assert get_arbitrator_agent() == "Arbitrator"
    assert get_challenger_agent() == "Challenger"

    from prompts import build_agent_prompt

    prompt = build_agent_prompt("Arbitrator")
    assert "Arbitrator" in prompt
    assert "VERDICT A" in prompt.upper()
    assert "VERDICT B" in prompt.upper()

    orchestrator = SandwichOrchestrator(
        start_chain=["agent-10001"],
        end_chain=["agent-10006"],
        standby_agents={"Arbitrator", "Challenger"},
    )
    all_available = ["agent-10001", "agent-10002", "Arbitrator", "Challenger", "agent-10006"]
    seq = orchestrator.build_initial_sequence(all_available)

    # Dialectical system agents are standby agents, so they are excluded from the initial sequence
    assert "Arbitrator" not in seq
    assert "Challenger" not in seq
    assert seq[0] == "agent-10001"
    assert seq[-1] == "agent-10006"


def test_discussion_scribe_structured_fields_extraction():
    sample_brief = """
# Core Objective & Problem Statement
The team needs to choose between PostgreSQL and SQLite WAL for embedded local storage.

# Key Perspectives & Emerging Consensus
- Architect recommended SQLite WAL for low operational complexity.
- Security verified local file permission boundaries.
Consensus is leaning towards SQLite.

# Risks, Trade-offs & Open Questions
- High write concurrency could encounter busy timeouts.

# Action Items & Next Steps
1. Benchmark sqlite3 with 50 concurrent writers.
2. Verify WAL checkpointing behavior.
"""
    extracted = _extract_brief_fields(sample_brief)
    assert "PostgreSQL and SQLite WAL" in extracted["objective"]
    assert len(extracted["perspectives"]) >= 2
    assert "Consensus is leaning" in extracted["consensus"]
    assert len(extracted["risks"]) >= 1
    assert len(extracted["action_items"]) >= 2

    sample_conclusion = """
*Problem Statement*
Resolve local database persistence strategy for single-node deployments.

*Consensus Decisions*
• Adopt SQLite in WAL mode with immediate transactions.

*Divergent Positions & Trade-offs*
• **PostgreSQL Container** ([Architect]): Distributed scaling — *Trade-off / Counter:* High operational overhead.

*Action Items & Next Steps*
1. Run benchmark suite with WAL mode.
2. Configure checkpoint interval.
"""
    conclusion_extracted = _extract_conclusion_fields(sample_conclusion)
    assert "single-node deployments" in conclusion_extracted["problem_statement"]
    assert "Adopt SQLite in WAL mode" in conclusion_extracted["consensus"]
    assert len(conclusion_extracted["opposing_conclusions"]) >= 1
    assert len(conclusion_extracted["action_items"]) == 2


def test_telemetry_span_recording_and_retrieval():
    session_id = "test-session-telemetry"
    channel_id = "C_TRACE"
    thread_ts = "999.888"

    trace_id = record_turn_trace(
        session_id=session_id,
        channel_id=channel_id,
        thread_ts=thread_ts,
        agent_key="agent-10001",
        stage_name="execution",
        duration_ms=124.5,
        prompt_tokens=350,
        completion_tokens=80,
        tools_called=["nominate_next_speaker"],
        status="SUCCESS",
    )
    assert trace_id is not None

    traces = get_turn_traces(session_id=session_id)
    assert len(traces) >= 1
    t = traces[-1]
    assert t["stage_name"] == "execution"
    assert t["agent_key"] == "agent-10001"
    assert t["duration_ms"] == 124.5
    assert t["tools_called"] == ["nominate_next_speaker"]


@pytest.mark.anyio
async def test_control_plane_rejects_self_nomination_and_enforces_strip():
    """Plan 193: control_plane and enforce_valid_nomination block self-nomination and strip extra inline tags."""
    skill = skill_registry.get("control_plane")
    assert skill is not None

    self_res = await skill_registry.execute_tool_async(
        "nominate_next_speaker",
        {"agent_name": "SecurityArchitect", "rationale": "Loop check"},
        agent_name="agent-10008",
    )
    assert "rejected" in self_res.lower()
    assert "error" in self_res.lower()

    # Inline stale self-tag must not survive alongside valid peer nomination or empty remaining
    enforced_peer = enforce_valid_nomination(
        text="Pass to /[agent-10008] and then /[agent-10002]",
        current_agent_key="agent-10008",
        remaining=["agent-10002", "agent-10008"],
        force_termination=False,
        control_plane_nomination="agent-10008",
    )
    assert "/[agent-10008]" not in enforced_peer
    assert enforced_peer.endswith("/[agent-10002]")

    enforced_self_only = enforce_valid_nomination(
        text="Assessment complete. /[agent-10008]",
        current_agent_key="agent-10008",
        remaining=["agent-10008"],
        force_termination=False,
    )
    assert "/[agent-10008]" not in enforced_self_only


def test_reset_enabled_agents_preserves_default_review_flow():
    """Plan 193: AgentSettingsManager.reset() restores default_review_flow instead of 16-agent fallback."""
    from orchestration.agent_settings import reset_enabled_agents, settings_manager
    from orchestration.nomination import get_initial_agents

    reset_enabled_agents()
    assert settings_manager.get_active_workflow_id() == "default_review_flow"
    assert settings_manager.get_orchestration_mode() == "fixed"
    assert get_initial_agents() == ["agent-10001", "agent-10002", "agent-10003", "agent-10014", "agent-10012"]


def test_scatter_gather_selects_start_chain_deterministically():
    """Plan 193: scatter_gather pops the ordered start_chain head instead of random selection."""
    from orchestration.agent_settings import (
        reset_enabled_agents,
        set_orchestration_mode,
        set_sandwich_config,
    )
    from orchestration.nomination import parse_agent_and_prompt
    from orchestration.thread_state import thread_state_manager

    reset_enabled_agents()
    set_orchestration_mode("scatter_gather", save=False)
    set_sandwich_config(["agent-10001"], ["agent-10008"], save=False)
    thread_state_manager.reset_all()

    for idx in range(5):
        agent_key, _, explicit = parse_agent_and_prompt(
            "Evaluate architecture trade-offs.",
            "C_SG",
            f"1700000000.{idx}",
        )
        assert agent_key == "agent-10001"
        assert explicit is False


@pytest.mark.anyio
async def test_control_plane_errored_or_denied_tools_do_not_mutate_state():
    """TKT-1201 (C6): Failed or denied request_user_clarification and signal_consensus_convergence must not mutate ctx or SQLite."""
    import json
    from unittest.mock import AsyncMock, MagicMock, patch

    from orchestration.stages.execution import ExecutionStage
    from orchestration.thread_state import thread_state_manager
    from orchestration.turn_pipeline import TurnContext, TurnPipeline

    ch = "C_C6_GUARD"
    ts = "1700000900.0001"
    thread_state_manager.clear_state(ch, ts)

    ctx = TurnContext(channel_id=ch, thread_ts=ts, raw_text="Check state", is_bot_trigger=False)
    ctx.agent_key = "agent-10001"
    ctx.agent_cfg = {"model": "test-model", "username": "Visionary", "skills": ["control_plane"]}
    ctx.messages = [{"role": "user", "content": "Check state"}]

    tc_clarify = MagicMock()
    tc_clarify.id = "tc_clarify"
    tc_clarify.function.name = "request_user_clarification"
    tc_clarify.function.arguments = json.dumps({"question": "Clarify scope?"})

    tc_conv = MagicMock()
    tc_conv.id = "tc_conv"
    tc_conv.function.name = "signal_consensus_convergence"
    tc_conv.function.arguments = json.dumps({"summary_of_agreement": "Consensus reached"})

    msg1 = MagicMock(content="", tool_calls=[tc_clarify, tc_conv])
    msg1.model_dump.return_value = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "tc_clarify", "type": "function", "function": {"name": "request_user_clarification", "arguments": tc_clarify.function.arguments}},
            {"id": "tc_conv", "type": "function", "function": {"name": "signal_consensus_convergence", "arguments": tc_conv.function.arguments}},
        ],
    }
    resp1 = MagicMock(choices=[MagicMock(message=msg1)], usage=None)
    msg2 = MagicMock(content="Proceeding without control state changes.", tool_calls=None)
    resp2 = MagicMock(choices=[MagicMock(message=msg2)], usage=None)

    cp_skill = MagicMock(name="control_plane", tools=["request_user_clarification", "signal_consensus_convergence"], requires_human_ITL=False, is_global=True, requires_admin=False)
    mock_reg = MagicMock()
    mock_reg.get_global_skills.return_value = ["control_plane"]
    mock_reg.filter_skills_for_caller.return_value = ["control_plane"]
    mock_reg.find_skill_for_tool.return_value = "control_plane"
    mock_reg.get.return_value = cp_skill
    # Simulate denied / errored execution of BOTH control plane tools
    mock_reg.execute_tool_async = AsyncMock(
        side_effect=[
            "Permission denied: Agent not authorized",
            '{"error": "Tool execution failed"}',
        ]
    )

    pipeline = TurnPipeline(llm_executor=AsyncMock(side_effect=[resp1, resp2]), skill_registry=mock_reg)
    stage = ExecutionStage(pipeline.deps)

    with patch("services.local_tools.is_agent_authorized_for_tool", return_value=True):
        await stage.stage_execute_llm(ctx)

    assert ctx.output.halted is False
    assert ctx.control.clarification_requested is False
    assert ctx.control.clarification_question is None
    assert ctx.control.consensus_signaled is False
    assert thread_state_manager.is_consensus_reached(ch, ts) is False


@pytest.mark.anyio
async def test_arbitrator_verdict_b_revokes_consensus_and_resets_round_rollover():
    """TKT-1216: Arbitrator Verdict B runs in fixed small pools, revokes consensus_reached, and round rollover clears evidence & convergence."""
    from orchestration.agent_router import _apply_convergence_stress_test_or_fast_forward
    from orchestration.agent_settings import (
        get_orchestration_mode,
        reset_enabled_agents,
        set_fixed_config,
        set_orchestration_mode,
    )
    from orchestration.stages.post_record import PostRecordStage
    from orchestration.system_settings import system_settings_manager
    from orchestration.thread_state import thread_state_manager
    from orchestration.turn_pipeline import TurnContext, TurnPipeline

    ch = "C_TKT1216_VERDICT"
    ts = "1700000950.0001"
    thread_state_manager.cleanup_thread(ch, ts)
    reset_enabled_agents()
    orig_mode = get_orchestration_mode()
    system_settings_manager.load()
    orig_dst_enabled = system_settings_manager.dialectical_stress_test.get("enabled", True)
    set_orchestration_mode("fixed", save=False)
    set_fixed_config(["agent-10001", "agent-10002", "agent-10008"], save=False)
    system_settings_manager.dialectical_stress_test["enabled"] = True

    try:
        thread_state_manager.set_remaining_agents(ch, ts, ["agent-10008"])
        thread_state_manager.set_consensus_reached(ch, ts, True)
        thread_state_manager.record_convergence_speaker(ch, ts, "agent-10001")

        # Even in fixed mode with len(remaining) == 1, Arbitrator Verdict B must execute and revoke consensus
        new_queue, new_count = _apply_convergence_stress_test_or_fast_forward(
            channel_id=ch,
            thread_ts=ts,
            spoken_agents={"agent-10001", "Challenger", "Arbitrator"},
            consecutive_convergence_count=2,
            last_speaker="Arbitrator",
            last_turn_converged=False,
        )
        assert new_count == 0
        assert new_queue == ["agent-10008"]
        assert thread_state_manager.is_consensus_reached(ch, ts) is False
        assert thread_state_manager.get_convergence_speakers(ch, ts) == []

        # Round rollover in PostRecordStage (/rounds 2) clears convergence_speakers and RoundEvidenceStore
        thread_state_manager.set_rounds(ch, ts, 2)
        thread_state_manager.set_round_sequence(ch, ts, ["agent-10001", "agent-10008"])
        thread_state_manager.set_remaining_agents(ch, ts, [])
        thread_state_manager.set_consensus_reached(ch, ts, False)
        thread_state_manager.record_convergence_speaker(ch, ts, "agent-10001")
        thread_state_manager.evidence.add_evidence(
            ch, ts, "web_search", {"q": "k8s"}, "Cluster sizing benchmark results", agent_key="agent-10001"
        )

        ctx = TurnContext(channel_id=ch, thread_ts=ts, raw_text="continue", is_bot_trigger=True)
        ctx.agent_key = "agent-10008"
        ctx.agent_cfg = {"model": "test-model", "username": "SecurityArchitect"}
        ctx.output.final_text = "Round 1 summary."

        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        pipeline = TurnPipeline(
            workspace_adapter=SimpleNamespace(
                format_outbound_text=lambda t: t,
                post_agent_turn=AsyncMock(return_value={"ok": True, "ts": "1700000950.0002"}),
            ),
            sleep_fn=AsyncMock(),
            notes_updater=lambda **k: None,
            usage_updater=lambda **k: None,
            flat_history_recorder=lambda **k: None,
        )
        stage = PostRecordStage(pipeline.deps)
        await stage.stage_post_and_record(ctx)

        assert thread_state_manager.get_rounds(ch, ts) == 1
        assert thread_state_manager.is_consensus_reached(ch, ts) is False
        assert thread_state_manager.get_convergence_speakers(ch, ts) == []
        assert thread_state_manager.format_round_evidence(ch, ts) is None
    finally:
        set_orchestration_mode(orig_mode, save=False)
        system_settings_manager.dialectical_stress_test["enabled"] = orig_dst_enabled
        thread_state_manager.cleanup_thread(ch, ts)
