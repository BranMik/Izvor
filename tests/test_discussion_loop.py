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
