"""
Unit tests for Parallel Scatter-Gather Dialectical Topology (PrePublish 8 Phase 5 / Epic C4 & tickets TKT-811, 812, 813).
"""

from __future__ import annotations

import asyncio


from core.models import (
    REQUIRED_DEFAULT_WORKFLOW_IDS,
    OrchestrationMode,
    WorkflowPreset,
    build_dynamic_default_workflows,
)
from orchestration.agent_settings import (
    get_orchestration_mode,
    set_orchestration_mode,
)
from pipeline.orchestrator.sandwich import DialecticExecutionPlan, SandwichOrchestrator
from services.slack_home import build_app_home_view


def test_orchestration_mode_scatter_gather_and_parallel_dialectic():
    assert OrchestrationMode.SCATTER_GATHER.value == "scatter_gather"
    assert OrchestrationMode.PARALLEL_DIALECTIC.value == "scatter_gather"
    assert OrchestrationMode.SCATTER_GATHER == OrchestrationMode.PARALLEL_DIALECTIC

    # Verify parallel dialectic flow preset exists in default workflows
    assert "parallel_dialectic_flow" in REQUIRED_DEFAULT_WORKFLOW_IDS
    defaults = build_dynamic_default_workflows([
        "agent-10001", "agent-10002", "agent-10003", "agent-10004", "agent-10005", "agent-10006"
    ])
    flow_presets = [wf for wf in defaults if wf.id == "parallel_dialectic_flow"]
    assert len(flow_presets) == 1
    preset = flow_presets[0]
    assert preset.mode == "scatter_gather"
    assert "agent-10001" in preset.sandwich_config.get("start_chain", [])
    assert "agent-10006" in preset.sandwich_config.get("end_chain", [])

    # Test serialization / deserialization round-trip
    p_dict = preset.to_dict()
    assert p_dict["mode"] == "scatter_gather"
    reconstructed = WorkflowPreset.from_dict(p_dict)
    assert reconstructed.id == "parallel_dialectic_flow"
    assert reconstructed.mode == "scatter_gather"


def test_build_dialectic_plan_partitions_framing_parallel_and_closing():
    orchestrator = SandwichOrchestrator(
        mode=OrchestrationMode.SCATTER_GATHER,
        start_chain=["agent-10001"],
        end_chain=["agent-10006"],
    )
    available = ["agent-10001", "agent-10002", "agent-10003", "agent-10017", "agent-10006"]
    plan: DialecticExecutionPlan = orchestrator.build_dialectic_plan(available)

    assert plan.framing_chain == ["agent-10001"]
    assert plan.closing_chain == ["agent-10006"]
    # agent-10017 should be classified into rebuttal turns
    assert "agent-10017" in plan.rebuttal_turns
    # remaining agents (10002, 10003) should be parallel specialists
    assert plan.parallel_specialists == ["agent-10002", "agent-10003"]


def test_agent_settings_supports_scatter_gather():
    orig_mode = get_orchestration_mode()
    try:
        set_orchestration_mode("scatter_gather", save=False)
        assert get_orchestration_mode() == "scatter_gather"

        set_orchestration_mode(OrchestrationMode.PARALLEL_DIALECTIC, save=False)
        assert get_orchestration_mode() == "scatter_gather"
    finally:
        set_orchestration_mode(orig_mode, save=False)


def test_slack_app_home_view_includes_parallel_dialectic():
    orig_mode = get_orchestration_mode()
    try:
        set_orchestration_mode("scatter_gather", save=False)
        view = build_app_home_view()
        blocks = view.get("blocks", [])

        # Check section text for parallel dialectic mode
        mode_sections = [
            b for b in blocks
            if b.get("type") == "section" and "Parallel Dialectic" in str(b.get("text", {}).get("text", ""))
        ]
        assert len(mode_sections) >= 1

        # Check that context element explains parallel dialectic flow
        context_elements = [
            b for b in blocks
            if b.get("type") == "context" and any("Parallel Dialectic" in el.get("text", "") for el in b.get("elements", []))
        ]
        assert len(context_elements) >= 1
    finally:
        set_orchestration_mode(orig_mode, save=False)


def test_orchestrate_discussion_parallel_scatter_gather(monkeypatch):
    from orchestration.agent_router import orchestrate_discussion
    from orchestration.thread_state import thread_state_manager

    channel_id = "C_TEST_PHASE5"
    thread_ts = "1700000005.000000"

    # Setup initial state
    thread_state_manager.cleanup_thread(channel_id, thread_ts)
    thread_state_manager.set_initial_prompt(channel_id, thread_ts, "Investigate caching layer")
    thread_state_manager.set_remaining_agents(
        channel_id, thread_ts, ["agent-10002", "agent-10003", "agent-10006"]
    )

    # Set mode to scatter_gather with end_chain=["agent-10006"]
    monkeypatch.setattr("orchestration.agent_settings.get_orchestration_mode", lambda: "scatter_gather")
    monkeypatch.setattr(
        "orchestration.agent_settings.get_sandwich_config",
        lambda: {"start_chain": ["agent-10001"], "end_chain": ["agent-10006"]},
    )

    calls = []

    async def fake_process_and_respond(**kwargs):
        agent_k = kwargs.get("target_agent_override", "unknown")
        hist = kwargs.get("in_memory_history", [])
        calls.append({
            "agent": agent_k,
            "raw_text": kwargs.get("raw_text"),
            "history_snapshot_len": len(hist),
        })
        # Simulate slight async delay to verify gather concurrency
        await asyncio.sleep(0.01)
        return {
            "final_text": f"Response from {agent_k}",
            "username": agent_k,
            "displayname": f"Agent {agent_k}",
            "channel_id": kwargs.get("channel_id"),
            "thread_ts": kwargs.get("thread_ts"),
        }

    monkeypatch.setattr("orchestration.agent_router.process_and_respond", fake_process_and_respond)

    async def fake_fetch_history(*args, **kwargs):
        return []

    monkeypatch.setattr("orchestration.agent_router.fetch_thread_history", fake_fetch_history)

    async def _run():
        return await orchestrate_discussion(
            channel_id=channel_id,
            thread_ts=thread_ts,
            raw_text="Investigate caching layer",
        )

    res = asyncio.run(_run())

    assert res is not None
    # Calls should include turn 1 framing, then scatter agents (10002 and 10003), then gather agent (10006)
    called_agents = [c["agent"] for c in calls]
    assert "agent-10002" in called_agents
    assert "agent-10003" in called_agents
    assert called_agents[-1] == "agent-10006"

    # Both parallel scatter agents should have received identical isolated history snapshots (user prompt + turn 1 response)
    scatter_calls = [c for c in calls if c["agent"] in ("agent-10002", "agent-10003")]
    assert len(scatter_calls) == 2
    assert scatter_calls[0]["history_snapshot_len"] == 2
    assert scatter_calls[1]["history_snapshot_len"] == 2

    # Synthesis agent receives all gathered findings (user prompt + turn 1 response + 2 specialist outputs = 4)
    gather_call = [c for c in calls if c["agent"] == "agent-10006"][0]
    assert gather_call["history_snapshot_len"] == 4
    assert "Synthesize" in gather_call["raw_text"]
