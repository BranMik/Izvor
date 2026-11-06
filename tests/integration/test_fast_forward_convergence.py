"""
Tests for Consensus Convergence Fast-Forward, Conditional Rebuttal Verdict Gate,
and Slack Admin Toggle (Tickets TKT-823 and TKT-824).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from orchestration.agent_router import _apply_convergence_stress_test_or_fast_forward
from orchestration.app_settings import (
    app_settings_manager,
    is_convergence_fast_forward_enabled,
    set_convergence_fast_forward_enabled,
)
from orchestration.agent_settings import (
    get_challenger_agent,
    get_rebuttal_agent,
    set_challenger_agent,
    set_rebuttal_agent,
)
from orchestration.system_settings import (
    get_convergence_consecutive_threshold,
    set_convergence_consecutive_threshold,
)
from orchestration.thread_state import thread_state_manager
from skills.builtin.control_plane.tools import build_control_plane_tool_schemas


@pytest.fixture(autouse=True)
def reset_settings_and_thread_state():
    # Save original settings
    orig_ff_enabled = is_convergence_fast_forward_enabled()
    orig_threshold = get_convergence_consecutive_threshold()
    orig_challenger = get_challenger_agent()
    orig_rebuttal = get_rebuttal_agent()

    # Reset default test values
    set_convergence_fast_forward_enabled(True, save=False)
    set_convergence_consecutive_threshold(2, save=False)
    set_challenger_agent("Challenger", save=False)
    set_rebuttal_agent("RebuttalAdvocate", save=False)

    yield

    # Restore originals
    set_convergence_fast_forward_enabled(orig_ff_enabled, save=False)
    set_convergence_consecutive_threshold(orig_threshold, save=False)
    set_challenger_agent(orig_challenger, save=False)
    set_rebuttal_agent(orig_rebuttal, save=False)


def test_consecutive_convergence_triggers_challenger_rebuttal_stress_test():
    channel_id = "C_TEST_CONV"
    thread_ts = "100.1"

    thread_state_manager.set_remaining_agents(
        channel_id,
        thread_ts,
        ["agent-10004", "agent-10005", "agent-10006"],
    )

    spoken_agents = {"agent-10001"}
    consecutive_count = 1  # 1 previous turn converged

    with patch(
        "orchestration.agent_settings.get_sandwich_config",
        return_value={"end_chain": ["agent-10006"]},
    ):
        # Turn 2 also converges -> threshold (2) reached
        new_queue, count = _apply_convergence_stress_test_or_fast_forward(
            channel_id=channel_id,
            thread_ts=thread_ts,
            spoken_agents=spoken_agents,
            consecutive_convergence_count=consecutive_count,
            last_speaker="agent-10004",
            last_turn_converged=True,
        )

        assert new_queue[0] == get_challenger_agent()  # Challenger
        assert new_queue[1] == get_rebuttal_agent()  # RebuttalAdvocate
        assert "agent-10005" in new_queue
        assert "agent-10006" in new_queue


def test_rebuttal_advocate_verdict_a_fast_forwards_to_end_chain():
    channel_id = "C_TEST_VERDICT_A"
    thread_ts = "100.2"

    thread_state_manager.set_remaining_agents(
        channel_id,
        thread_ts,
        ["agent-10004", "agent-10005", "agent-10006"],
    )

    spoken_agents = {"agent-10001", "Challenger", "RebuttalAdvocate"}

    with patch(
        "orchestration.agent_settings.get_sandwich_config",
        return_value={"end_chain": ["agent-10006"]},
    ):
        # Rebuttal advocate signals convergence (Verdict A)
        new_queue, count = _apply_convergence_stress_test_or_fast_forward(
            channel_id=channel_id,
            thread_ts=thread_ts,
            spoken_agents=spoken_agents,
            consecutive_convergence_count=1,
            last_speaker="RebuttalAdvocate",
            last_turn_converged=True,
        )

        # Truncated directly to end_chain
        assert new_queue == ["agent-10006"]
        assert thread_state_manager.get_remaining_agents(channel_id, thread_ts) == ["agent-10006"]


def test_rebuttal_advocate_verdict_b_cancels_fast_forward_and_resumes_middle_pool():
    channel_id = "C_TEST_VERDICT_B"
    thread_ts = "100.3"

    initial_queue = ["agent-10004", "agent-10005", "agent-10006"]
    thread_state_manager.set_remaining_agents(channel_id, thread_ts, list(initial_queue))

    spoken_agents = {"agent-10001", "Challenger", "RebuttalAdvocate"}

    with patch(
        "orchestration.agent_settings.get_sandwich_config",
        return_value={"end_chain": ["agent-10006"]},
    ):
        # Rebuttal advocate rejects consensus (Verdict B: last_turn_converged=False)
        new_queue, count = _apply_convergence_stress_test_or_fast_forward(
            channel_id=channel_id,
            thread_ts=thread_ts,
            spoken_agents=spoken_agents,
            consecutive_convergence_count=2,
            last_speaker="RebuttalAdvocate",
            last_turn_converged=False,
        )

        # Fast forward cancelled: remaining middle queue preserved, count reset to 0
        assert new_queue == initial_queue
        assert count == 0
        assert thread_state_manager.get_remaining_agents(channel_id, thread_ts) == initial_queue


def test_convergence_fast_forward_disabled_toggle():
    channel_id = "C_TEST_TOGGLE_OFF"
    thread_ts = "100.4"

    set_convergence_fast_forward_enabled(False, save=False)

    initial_queue = ["agent-10004", "agent-10005", "agent-10006"]
    thread_state_manager.set_remaining_agents(channel_id, thread_ts, list(initial_queue))

    # Even with threshold reached, toggle OFF disables fast forward
    new_queue, count = _apply_convergence_stress_test_or_fast_forward(
        channel_id=channel_id,
        thread_ts=thread_ts,
        spoken_agents={"agent-10001"},
        consecutive_convergence_count=5,
        last_speaker="agent-10004",
        last_turn_converged=True,
    )

    assert new_queue == initial_queue
    assert count == 0


def test_control_plane_tool_schemas_dynamic_convergence_filtering():
    # When allow_convergence=True
    schemas_on = build_control_plane_tool_schemas(
        allow_clarification=True,
        allow_convergence=True,
        eligible_agents=["agent-10002"],
    )
    names_on = [s["function"]["name"] for s in schemas_on]
    assert "signal_consensus_convergence" in names_on

    # When allow_convergence=False
    schemas_off = build_control_plane_tool_schemas(
        allow_clarification=True,
        allow_convergence=False,
        eligible_agents=["agent-10002"],
    )
    names_off = [s["function"]["name"] for s in schemas_off]
    assert "signal_consensus_convergence" not in names_off
    assert "nominate_next_speaker" in names_off


def test_slack_app_home_toggle_interactivity_handler():
    import asyncio
    import json
    from adapters.slack.interactivity import handle_slack_interactivity
    from fastapi import BackgroundTasks, Request
    from core.config import set_admin_slack_user_ids_override

    set_admin_slack_user_ids_override("U_ADMIN_123")

    payload_off = {
        "type": "block_actions",
        "user": {"id": "U_ADMIN_123", "username": "admin_tester"},
        "actions": [
            {
                "action_id": "toggle_convergence_fast_forward_radio",
                "selected_option": {"value": "convergence_ff:off"},
            }
        ],
    }

    req_off = MagicMock(spec=Request)
    req_off.body = AsyncMock(return_value=json.dumps(payload_off).encode("utf-8"))
    req_off.headers = {}
    bg = BackgroundTasks()

    async def _run():
        with (
            patch("adapters.slack.interactivity.verify_slack_signature", return_value=True),
            patch("services.slack.views.publish_app_home", new_callable=AsyncMock),
            patch.object(app_settings_manager, "save"),
        ):
            res = await handle_slack_interactivity(req_off, bg)
            assert res.status_code == 200

            assert is_convergence_fast_forward_enabled() is False

            # Toggle back to ON
            payload_on = {
                "type": "block_actions",
                "user": {"id": "U_ADMIN_123", "username": "admin_tester"},
                "actions": [
                    {
                        "action_id": "toggle_convergence_fast_forward_radio",
                        "selected_option": {"value": "convergence_ff:on"},
                    }
                ],
            }
            req_on = MagicMock(spec=Request)
            req_on.body = AsyncMock(return_value=json.dumps(payload_on).encode("utf-8"))
            req_on.headers = {}

            res = await handle_slack_interactivity(req_on, bg)
            assert res.status_code == 200

            assert is_convergence_fast_forward_enabled() is True

    try:
        asyncio.run(_run())
    finally:
        set_admin_slack_user_ids_override(None)


def test_consecutive_threshold_three_and_sceptic_early_kickoff_speech():
    """Verify T=3 threshold and that an early kickoff speech by Sceptic does not disqualify them from challenging later consensus."""
    channel_id = "C_TEST_T3_KICKOFF"
    thread_ts = "200.1"
    thread_state_manager.cleanup_thread(channel_id, thread_ts)

    # Threshold set to 3
    set_convergence_consecutive_threshold(3, save=False)
    assert get_convergence_consecutive_threshold() == 3

    thread_state_manager.set_remaining_agents(
        channel_id,
        thread_ts,
        ["agent-10004", "agent-10005", "agent-10006"],
    )

    # Sceptic (agent-10003) already spoke early in turn 1 (kickoff)
    spoken_agents = {"agent-10003"}

    with patch(
        "orchestration.agent_settings.get_sandwich_config",
        return_value={"end_chain": ["agent-10006"]},
    ):
        # Turn 2: Agent 4 converges (count 1)
        queue, count = _apply_convergence_stress_test_or_fast_forward(
            channel_id=channel_id,
            thread_ts=thread_ts,
            spoken_agents=spoken_agents | {"agent-10004"},
            consecutive_convergence_count=0,
            last_speaker="agent-10004",
            last_turn_converged=True,
        )
        assert count == 1
        assert queue == ["agent-10004", "agent-10005", "agent-10006"]

        # Turn 3: Agent 5 converges (count 2 - still below T=3)
        queue, count = _apply_convergence_stress_test_or_fast_forward(
            channel_id=channel_id,
            thread_ts=thread_ts,
            spoken_agents=spoken_agents | {"agent-10004", "agent-10005"},
            consecutive_convergence_count=count,
            last_speaker="agent-10005",
            last_turn_converged=True,
        )
        assert count == 2
        assert queue == ["agent-10004", "agent-10005", "agent-10006"]

        # Turn 4: Agent 6 converges (count 3 - threshold reached!)
        queue, count = _apply_convergence_stress_test_or_fast_forward(
            channel_id=channel_id,
            thread_ts=thread_ts,
            spoken_agents=spoken_agents | {"agent-10004", "agent-10005", "agent-10006"},
            consecutive_convergence_count=count,
            last_speaker="agent-10006",
            last_turn_converged=True,
        )
        assert count == 0
        assert queue[0] == "Challenger"
        assert queue[1] == "RebuttalAdvocate"


def test_case_b_direct_rebuttal_when_challenger_converged():
    """Verify that when Challenger was part of the convergence streak, RebuttalAdvocate is injected directly (Case B)."""
    channel_id = "C_TEST_CHALLENGER_CONV"
    thread_ts = "200.2"
    thread_state_manager.cleanup_thread(channel_id, thread_ts)

    set_convergence_consecutive_threshold(2, save=False)

    thread_state_manager.set_remaining_agents(
        channel_id,
        thread_ts,
        ["agent-10005", "agent-10006", "agent-10007"],
    )

    with patch(
        "orchestration.agent_settings.get_sandwich_config",
        return_value={"end_chain": ["agent-10006"]},
    ):
        # Turn 1: Challenger speaks and converges!
        queue, count = _apply_convergence_stress_test_or_fast_forward(
            channel_id=channel_id,
            thread_ts=thread_ts,
            spoken_agents={"Challenger"},
            consecutive_convergence_count=0,
            last_speaker="Challenger",
            last_turn_converged=True,
        )
        assert count == 1

        # Turn 2: Agent 4 speaks and also converges (threshold 2 reached!)
        queue, count = _apply_convergence_stress_test_or_fast_forward(
            channel_id=channel_id,
            thread_ts=thread_ts,
            spoken_agents={"Challenger", "agent-10004"},
            consecutive_convergence_count=count,
            last_speaker="agent-10004",
            last_turn_converged=True,
        )
        assert count == 0
        # Challenger participated in the convergence streak, so RebuttalAdvocate is injected directly!
        assert queue[0] == "RebuttalAdvocate"
        assert "agent-10005" in queue


def test_fixed_chain_guard_bypasses_fast_forward_on_small_remaining_pool():
    """Verify that in FIXED mode when len(remaining) <= 2, fast forward and stress testing do not truncate the chain."""
    channel_id = "C_TEST_FIXED_GUARD"
    thread_ts = "200.3"
    thread_state_manager.cleanup_thread(channel_id, thread_ts)

    set_convergence_consecutive_threshold(2, save=False)

    initial_remaining = ["agent-10005", "agent-10006"]
    thread_state_manager.set_remaining_agents(
        channel_id,
        thread_ts,
        list(initial_remaining),
    )

    with patch(
        "orchestration.thread_state.thread_state_manager.get_workflow_snapshot",
        return_value={"mode": "fixed"},
    ):
        # Turn with 2 consecutive convergences:
        queue, count = _apply_convergence_stress_test_or_fast_forward(
            channel_id=channel_id,
            thread_ts=thread_ts,
            spoken_agents={"agent-10001", "agent-10002"},
            consecutive_convergence_count=1,
            last_speaker="agent-10002",
            last_turn_converged=True,
        )
        # Small pool guard in fixed mode returns remaining unchanged!
        assert queue == initial_remaining
        assert count == 2
