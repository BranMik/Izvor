"""
Unit tests for Parallel Scatter-Gather Dialectical Topology (PrePublish 8 Phase 5 / Epic C4 & tickets TKT-811, 812, 813).
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch


from core.models import (
    REQUIRED_DEFAULT_WORKFLOW_IDS,
    OrchestrationMode,
    WorkflowPreset,
    build_dynamic_default_workflows,
)
from orchestration.agent_settings import (
    get_orchestration_mode,
    get_sandwich_config,
    set_orchestration_mode,
    set_sandwich_config,
)
from pipeline.orchestrator.sandwich import SandwichOrchestrator
from services.slack_home import build_app_home_view


def test_orchestration_mode_scatter_gather_and_parallel_dialectic():
    assert OrchestrationMode.SCATTER_GATHER.value == "scatter_gather"
    assert OrchestrationMode.from_value("scatter_gather") == OrchestrationMode.SCATTER_GATHER
    assert OrchestrationMode.from_value("unknown_mode") == OrchestrationMode.SANDWICH

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


def test_agent_settings_supports_scatter_gather():
    orig_mode = get_orchestration_mode()
    try:
        set_orchestration_mode("scatter_gather", save=False)
        assert get_orchestration_mode() == "scatter_gather"

        set_orchestration_mode(OrchestrationMode.SCATTER_GATHER, save=False)
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


def test_orchestrate_discussion_parallel_scatter_gather():
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

    orig_mode = get_orchestration_mode()
    orig_sandwich = get_sandwich_config()

    try:
        # Set mode to scatter_gather with end_chain=["agent-10006"]
        set_orchestration_mode("scatter_gather", save=False)
        set_sandwich_config(["agent-10001"], ["agent-10006"])

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

        async def fake_fetch_history(*args, **kwargs):
            return []

        with (
            patch("orchestration.agent_router.process_and_respond", fake_process_and_respond),
            patch("orchestration.agent_router.fetch_thread_history", fake_fetch_history),
        ):
            async def _run():
                return await orchestrate_discussion(
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    raw_text="Investigate caching layer",
                )

            res = asyncio.run(_run())
    finally:
        set_orchestration_mode(orig_mode, save=False)
        if orig_sandwich:
            set_sandwich_config(orig_sandwich.get("start_chain", []), orig_sandwich.get("end_chain", []))

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


def test_scatter_gather_context_prep_preserves_initial_prompt_and_synthesis_instruction():
    """TKT-1218: Gather turn keeps user's initial prompt alongside explicit synthesis directive."""
    from orchestration.stages.context_prep import ContextPrepStage
    from orchestration.thread_state import thread_state_manager
    from orchestration.turn_pipeline import TurnContext, TurnPipeline

    ch = "C_TKT1218_GATHER"
    ts = "1700005001.0001"
    orig_mode = get_orchestration_mode()
    orig_sandwich = get_sandwich_config()
    thread_state_manager.cleanup_thread(ch, ts)

    async def _run():
        set_orchestration_mode("scatter_gather", save=False)
        set_sandwich_config(["agent-10001"], ["agent-10006"], save=False)
        thread_state_manager.set_initial_prompt(ch, ts, "Compare Redis vs Memcached")

        pipeline = TurnPipeline(history_fetcher=lambda *a, **k: [])
        stage = ContextPrepStage(pipeline.deps)
        ctx = TurnContext(
            channel_id=ch,
            thread_ts=ts,
            raw_text="Synthesize the parallel specialist perspectives into a reconciled conclusion.",
            is_bot_trigger=True,
            target_agent_override="agent-10006",
            in_memory_history=[{"role": "user", "content": "Compare Redis vs Memcached"}],
        )
        ctx.agent_key = "agent-10006"
        ctx.agent_cfg = {"model": "test-model", "username": "Arbitrator", "system_prompt": "You are Arbitrator."}
        ctx.prompt = "Synthesize the parallel specialist perspectives into a reconciled conclusion."
        await stage.stage_prepare_context(ctx)
        user_msg = ctx.messages[-1]["content"]
        assert "Compare Redis vs Memcached" in user_msg
        assert "[Synthesis Instruction]: Synthesize the parallel specialist perspectives" in user_msg

    try:
        asyncio.run(_run())
    finally:
        set_orchestration_mode(orig_mode, save=False)
        if orig_sandwich:
            set_sandwich_config(orig_sandwich.get("start_chain", []), orig_sandwich.get("end_chain", []), save=False)
        thread_state_manager.cleanup_thread(ch, ts)


def test_isolated_branch_skips_revisor_and_preserves_queue_and_multiround_history():
    """TKT-1218: isolated_branch skips InternalRevisor and SQLite removal, and 2-round scatter-gather preserves Round 1 history."""
    from unittest.mock import AsyncMock
    from orchestration.discussion_loop import DiscussionLoop
    from orchestration.stages.validation import ValidationStage
    from orchestration.thread_state import thread_state_manager
    from orchestration.turn_pipeline import TurnContext, TurnPipeline

    ch = "C_TKT1218_ISOLATED"
    ts = "1700005002.0001"
    orig_mode = get_orchestration_mode()
    orig_sandwich = get_sandwich_config()
    thread_state_manager.cleanup_thread(ch, ts)

    async def _run():
        # 1. ValidationStage with isolated_branch=True preserves remaining_agents in SQLite
        thread_state_manager.set_remaining_agents(ch, ts, ["agent-10002", "agent-10003", "agent-10006"])
        val_stage = ValidationStage(TurnPipeline().deps)
        vctx = TurnContext(
            channel_id=ch,
            thread_ts=ts,
            raw_text="Analyze caching",
            is_bot_trigger=True,
            target_agent_override="agent-10002",
            isolated_branch=True,
        )
        await val_stage.stage_validate_and_parse(vctx)
        assert vctx.agent_key == "agent-10002"
        assert thread_state_manager.get_remaining_agents(ch, ts) == ["agent-10002", "agent-10003", "agent-10006"]

        # 2. _execute_revisor_barrier returns early without invoking run_internal_revisor when isolated_branch=True
        pipeline = TurnPipeline()
        with patch("orchestration.revisor_service.run_internal_revisor", new_callable=AsyncMock) as mock_rev:
            await pipeline._execute_revisor_barrier(vctx, {"final_text": "branch"})
            mock_rev.assert_not_awaited()

        # 3. 2-round scatter-gather preserves Round 1 history into Round 2 without re-fetching Slack history
        set_orchestration_mode("scatter_gather", save=False)
        set_sandwich_config(["agent-10001"], ["agent-10006"], save=False)
        thread_state_manager.set_rounds(ch, ts, 2)
        thread_state_manager.set_round_sequence(ch, ts, ["agent-10002", "agent-10003", "agent-10006"])
        thread_state_manager.set_remaining_agents(ch, ts, ["agent-10002", "agent-10003", "agent-10006"])

        round2_seen_histories: list[list[dict]] = []

        async def fake_exec(**kwargs):
            agent_t = kwargs.get("target_agent_override") or "agent-10001"
            hist = list(kwargs.get("in_memory_history") or [])
            if thread_state_manager.get_rounds(ch, ts) == 1 and agent_t == "agent-10002":
                round2_seen_histories.append(hist)
            return {
                "ok": True,
                "agent_key": agent_t,
                "username": agent_t,
                "displayname": agent_t,
                "final_text": f"ROUND1_TURN1 from {agent_t}" if thread_state_manager.get_rounds(ch, ts) == 2 else f"ROUND2 from {agent_t}",
            }

        loop = DiscussionLoop(turn_executor=fake_exec)
        with patch("orchestration.discussion_loop.fetch_thread_history", AsyncMock(return_value=[])) as mock_fetch:
            await loop.run(channel_id=ch, thread_ts=ts, initial_raw_text="Topic")
            mock_fetch.assert_awaited_once()
        assert len(round2_seen_histories) == 1
        assert any("ROUND1_TURN1 from agent-10006" in str(m.get("content", "")) for m in round2_seen_histories[0])

    try:
        asyncio.run(_run())
    finally:
        set_orchestration_mode(orig_mode, save=False)
        if orig_sandwich:
            set_sandwich_config(orig_sandwich.get("start_chain", []), orig_sandwich.get("end_chain", []), save=False)
        thread_state_manager.cleanup_thread(ch, ts)
