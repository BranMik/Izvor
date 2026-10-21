"""
Unit tests for Structured Control Plane, Dialectical Stress-Testing, Scribe Domain Fields,
and Observability Span Traces (PrePublish 8 Phase 4 / Epic C6 & tickets TKT-817, 818, 819, 822).
"""

from __future__ import annotations

import pytest

from core.domain import DiscussionBrief, DiscussionConclusion
from orchestration.nomination import enforce_valid_nomination
from orchestration.system_settings import (
    get_dialectical_stress_test_config,
    is_dialectical_stress_test_enabled,
)
from pipeline.discussion_scribe.scribe import (
    _extract_brief_fields,
    _extract_conclusion_fields,
)
from pipeline.orchestrator.sandwich import SandwichOrchestrator
from services.telemetry import get_turn_traces, record_turn_trace
from skills import skill_registry


def test_control_plane_skill_registered_and_executable():
    skill = skill_registry.get("control_plane")
    assert skill is not None
    assert skill.is_global
    tool_names = [s.get("function", {}).get("name") for s in skill.get_tool_schemas()]
    assert "nominate_next_speaker" in tool_names
    assert "request_user_clarification" in tool_names
    assert "signal_consensus_convergence" in tool_names

    # Test executing nominate_next_speaker
    res = skill_registry.execute_tool(
        "nominate_next_speaker",
        {"agent_name": "agent-10002", "rationale": "Needs technical architecture review"},
        agent_name="agent-10001",
    )
    assert "agent-10002" in res
    assert "nominated" in res

    # Test executing signal_consensus_convergence
    res = skill_registry.execute_tool(
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


def test_rebuttal_advocate_persona_and_standby_topology():
    from orchestration.system_settings import get_challenger_agents, get_rebuttal_agent

    assert get_rebuttal_agent() == "agent-10017"
    assert "agent-10003" in get_challenger_agents()

    from prompts import build_agent_prompt

    prompt = build_agent_prompt("agent-10017")
    assert "Rebuttal & Defense Advocate" in prompt
    assert "VERDICT A" in prompt.upper()
    assert "VERDICT B" in prompt.upper()

    orchestrator = SandwichOrchestrator(
        start_chain=["agent-10001"],
        end_chain=["agent-10006"],
        standby_agents={"agent-10017"},
    )
    all_available = ["agent-10001", "agent-10002", "agent-10017", "agent-10006"]
    seq = orchestrator.build_initial_sequence(all_available)

    # agent-10017 is a standby agent, so it is excluded from the initial sequence
    assert "agent-10017" not in seq
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
Part 1: Problem Statement
Resolve local database persistence strategy for single-node deployments.

Part 2: Conclusion and Argumentation
Consensus Decisions:
Adopt SQLite in WAL mode with immediate transactions.

Opposing Conclusions & Divergent Advice:
- Use PostgreSQL via Docker container for distributed scaling.
"""
    conclusion_extracted = _extract_conclusion_fields(sample_conclusion)
    assert "single-node deployments" in conclusion_extracted["problem_statement"]
    assert "Adopt SQLite in WAL mode" in conclusion_extracted["consensus"]
    assert len(conclusion_extracted["opposing_conclusions"]) >= 1


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
