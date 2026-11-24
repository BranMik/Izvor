from unittest.mock import AsyncMock, patch
import pytest

from orchestration.discussion_loop import DiscussionLoop
from orchestration.thread_state import thread_state_manager


@pytest.fixture(autouse=True)
def clean_thread_state():
    thread_state_manager.reset_all()
    yield
    thread_state_manager.reset_all()


@pytest.mark.anyio
async def test_discussion_loop_fresh_start_and_task_registration():
    channel_id = "C_TEST_LOOP"
    thread_ts = "1000.0001"
    calls = []
    active_task_observed = []

    async def mock_turn_executor(**kwargs):
        calls.append(kwargs)
        # Check active task inside execution
        active = thread_state_manager.get_active_task(channel_id, thread_ts)
        active_task_observed.append(active)
        if len(calls) == 1:
            return {
                "ok": True,
                "agent_key": "agent-10001",
                "final_text": "Initial specialist response",
                "username": "Agent 1",
            }
        return {
            "ok": True,
            "agent_key": "agent-10002",
            "final_text": "Concluding consensus",
            "username": "Agent 2",
            "is_completed": True,
        }

    loop = DiscussionLoop(turn_executor=mock_turn_executor)
    with patch(
        "orchestration.discussion_loop.fetch_thread_history",
        AsyncMock(return_value=[]),
    ), patch("orchestration.discussion_loop.get_orchestration_mode", return_value="sandwich"):
        res = await loop.run(
            channel_id=channel_id,
            thread_ts=thread_ts,
            initial_raw_text="Solve this challenge",
            max_turns=3,
        )

    assert res.get("is_completed") is True
    assert len(calls) == 2
    assert len(active_task_observed) == 2
    assert active_task_observed[0] is not None
    # Verify task was cleaned up in finally block
    assert thread_state_manager.get_active_task(channel_id, thread_ts) is None


@pytest.mark.anyio
async def test_discussion_loop_hitl_resumption():
    channel_id = "C_TEST_RESUME"
    thread_ts = "1000.0002"
    calls = []

    async def mock_turn_executor(**kwargs):
        calls.append(kwargs)
        return {
            "ok": True,
            "agent_key": "agent-10002",
            "final_text": "Follow-up response after approval",
            "username": "Agent 2",
            "is_completed": True,
        }

    initial_turn_res = {
        "ok": True,
        "agent_key": "agent-10001",
        "final_text": "Approved sensitive action result",
        "username": "Agent 1",
    }

    loop = DiscussionLoop(turn_executor=mock_turn_executor)
    with patch(
        "orchestration.discussion_loop.fetch_thread_history",
        AsyncMock(return_value=[]),
    ):
        res = await loop.run(
            channel_id=channel_id,
            thread_ts=thread_ts,
            initial_turn_res=initial_turn_res,
            max_turns=3,
        )

    assert res.get("is_completed") is True
    assert len(calls) == 1
    # Turn 2 received raw_text from initial_turn_res
    assert calls[0]["raw_text"] == "Approved sensitive action result"
    assert thread_state_manager.get_active_task(channel_id, thread_ts) is None


@pytest.mark.anyio
async def test_discussion_loop_handles_exception_and_cleans_up_task():
    channel_id = "C_TEST_ERR"
    thread_ts = "1000.0003"

    async def mock_turn_executor(**kwargs):
        raise RuntimeError("Fatal turn pipeline crash")

    loop = DiscussionLoop(turn_executor=mock_turn_executor)
    with patch(
        "orchestration.discussion_loop.fetch_thread_history",
        AsyncMock(return_value=[]),
    ):
        res = await loop.run(
            channel_id=channel_id,
            thread_ts=thread_ts,
            initial_raw_text="Crashing turn",
        )

    assert res.get("ok") is False
    assert "Fatal turn pipeline crash" in res.get("error", "")
    assert thread_state_manager.get_active_task(channel_id, thread_ts) is None


@pytest.mark.anyio
async def test_discussion_loop_appends_final_turn_and_revisor_warning_before_break(tmp_path):
    """Verifies that when a turn completes discussion (is_completed: True), its final_text and revisor_warning are appended to accumulated_history and written to flat history."""
    from services.flat_history_recorder import configure_paths, reset_paths

    channel_id = "C_TEST_COMPLETED"
    thread_ts = "1000.0004"

    custom_flat_dir = tmp_path / "flat-histories"
    custom_flat_dir.mkdir(parents=True, exist_ok=True)
    configure_paths(custom_flat_dir)

    try:
        async def mock_turn_executor(**kwargs):
            return {
                "ok": True,
                "agent_key": "agent-10002",
                "final_text": "Final concluding statement by Tactician",
                "username": "Tactician",
                "is_completed": True,
                "revisor_warning": "ATTENTION: Agent [Tactician] hallucinated the following fact: ...",
            }

        loop = DiscussionLoop(turn_executor=mock_turn_executor)
        with patch(
            "orchestration.discussion_loop.fetch_thread_history",
            AsyncMock(return_value=[]),
        ), patch("orchestration.discussion_loop.get_orchestration_mode", return_value="sandwich"):
            res = await loop.run(
                channel_id=channel_id,
                thread_ts=thread_ts,
                initial_raw_text="Initial problem query",
                max_turns=3,
            )

        assert res.get("is_completed") is True

        files = list(custom_flat_dir.glob("*.md"))
        assert len(files) == 1
        content = files[0].read_text(encoding="utf-8")
        assert "Final concluding statement by Tactician" in content
        assert "Internal Revisor Warning" in content
        assert "ATTENTION: Agent [Tactician] hallucinated" in content
    finally:
        reset_paths()


@pytest.mark.anyio
async def test_discussion_loop_max_turns_cleanup_and_excludes_error_banners():
    """
    TKT-1409 (CB-10):
    1. When max_turns is reached, force_termination_override is passed on the final turn and SQLite
       remaining_agents / round state is cleared on exit.
    2. Failed turn error banners (ok=False) are never appended to accumulated_history.
    """
    ch = "C_MAX_TURNS"
    ts = "1000.0099"
    thread_state_manager.clear_state(ch, ts)
    thread_state_manager.set_remaining_agents(ch, ts, ["agent-10001", "agent-10002", "agent-10003"])

    seen_histories: list[list[dict]] = []
    seen_force_flags: list[bool] = []
    call_count = 0

    async def _turn_exec(**kwargs):
        nonlocal call_count
        call_count += 1
        seen_histories.append(list(kwargs.get("in_memory_history") or []))
        seen_force_flags.append(bool(kwargs.get("force_termination_override", False)))
        if call_count == 1:
            return {
                "ok": True,
                "agent_key": "agent-10001",
                "username": "Architect",
                "final_text": "Turn 1 healthy",
                "is_completed": False,
            }
        return {
            "ok": True,
            "agent_key": "agent-10002",
            "username": "Tactician",
            "final_text": "Turn 2 at max_turns",
            "is_completed": False,
        }

    loop = DiscussionLoop(turn_executor=_turn_exec)
    with (
        patch("orchestration.discussion_loop.fetch_thread_history", AsyncMock(return_value=[])),
        patch("orchestration.discussion_loop.get_orchestration_mode", return_value="fixed"),
    ):
        await loop.run(channel_id=ch, thread_ts=ts, initial_raw_text="Start", max_turns=2)

    assert call_count == 2
    assert seen_force_flags == [False, True]
    assert thread_state_manager.get_remaining_agents(ch, ts) == []

    # Part 2: ok=False error banner must not enter accumulated_history
    ch_e = "C_ERR_HIST"
    ts_e = "1000.0100"
    thread_state_manager.clear_state(ch_e, ts_e)
    hist_captured: list[dict] = []

    async def _err_exec(**kwargs):
        return {
            "ok": False,
            "agent_key": "agent-10001",
            "username": "Architect",
            "final_text": "RATE LIMITS CROSSED",
            "is_completed": False,
        }

    loop_err = DiscussionLoop(turn_executor=_err_exec)
    with patch("orchestration.discussion_loop.fetch_thread_history", AsyncMock(return_value=[])):
        await loop_err.run(
            channel_id=ch_e,
            thread_ts=ts_e,
            initial_raw_text="Fail",
            accumulated_history=hist_captured,
        )
    assert hist_captured == []

