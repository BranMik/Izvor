from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestration.agent_router import (
    orchestrate_discussion,
    process_and_respond,
)
from orchestration.agent_settings import reset_enabled_agents
from orchestration.thread_state import thread_state_manager


@pytest.fixture(autouse=True)
def reset_state():
    reset_enabled_agents()
    thread_state_manager.reset_all()


@pytest.mark.anyio
async def test_internal_loop_multi_agent_custom_flow(monkeypatch):
    """
    Verify that tagging multiple agents ([agent-10002] [agent-10011]) executes both
    agents in-memory sequentially in a single orchestrator call, posting each to Slack,
    accumulating history, and cleanly terminating when the chain finishes.
    """
    channel_id = "C_LOOP_TEST"
    thread_ts = "1000.0001"
    thread_key = (channel_id, thread_ts)

    posted_messages = []
    async def mock_post(channel_id, thread_ts, text, username, icon_url=None):
        posted_messages.append({"username": username, "text": text})
        return {"ok": True, "ts": f"1000.{len(posted_messages)}"}

    monkeypatch.setattr("orchestration.agent_router.post_agent_response", mock_post)
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history",
        AsyncMock(return_value=[]),
    )

    turn_counter = 0
    captured_contexts = []

    async def mock_llm_run(func, *args, **kwargs):
        nonlocal turn_counter
        turn_counter += 1
        messages = kwargs.get("messages") or (args[2] if len(args) > 2 else [])
        captured_contexts.append(list(messages))

        mock_choice = MagicMock()
        if turn_counter == 1:
            mock_choice.message.content = "Innovator response proposing a new idea. /[agent-10011]"
        else:
            mock_choice.message.content = "LegalAdvisor evaluation and conclusion. /[agent-10008]"
        mock_choice.message.tool_calls = None
        mock_response = MagicMock(choices=[mock_choice], usage=None)
        return mock_response

    monkeypatch.setattr("orchestration.agent_router.run_in_threadpool", mock_llm_run)

    # Run internal discussion loop
    await orchestrate_discussion(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="/[agent-10002] /[agent-10011] What is the best architecture for our caching layer?",
    )

    # 1. Verify two turns executed
    assert turn_counter == 2
    assert len(posted_messages) == 2

    # 2. Verify turn 1 was Innovator, turn 2 was LegalAdvisor
    assert posted_messages[0]["username"] == "Innovator"
    assert "/[agent-10011]" in posted_messages[0]["text"]

    assert posted_messages[1]["username"] == "LegalAdvisor"
    # LegalAdvisor was the last agent in custom flow, so premature SecurityArchitect tag is stripped
    assert "/[agent-10008]" not in posted_messages[1]["text"]

    # 3. Verify in-memory history: turn 2 messages contained turn 1 Innovator post
    turn_2_messages = captured_contexts[1]
    assistant_posts_in_turn_2 = [
        m["content"] for m in turn_2_messages if m.get("role") == "assistant"
    ]
    assert any("Innovator response proposing a new idea" in c for c in assistant_posts_in_turn_2)

    # 4. Verify thread state cleanup
    assert thread_key not in thread_state_manager.discussion.remaining_agents_map
    assert thread_key not in thread_state_manager.discussion.custom_flow_threads


@pytest.mark.anyio
async def test_internal_loop_user_stop_interruption(monkeypatch):
    """
    Verify that if a user requests stop (request_thread_stop),
    the internal loop halts after the current turn and does NOT execute the next agent.
    """
    channel_id = "C_STOP_TEST"
    thread_ts = "2000.0001"

    posted_messages = []
    async def mock_post(channel_id, thread_ts, text, username, icon_url=None):
        posted_messages.append({"username": username, "text": text})
        return {"ok": True, "ts": f"2000.{len(posted_messages)}"}

    monkeypatch.setattr("orchestration.agent_router.post_agent_response", mock_post)
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history",
        AsyncMock(return_value=[]),
    )

    turn_counter = 0

    async def mock_llm_run(func, *args, **kwargs):
        nonlocal turn_counter
        turn_counter += 1
        mock_choice = MagicMock()
        mock_choice.message.content = f"Turn {turn_counter} completed. /[agent-10011]"
        mock_choice.message.tool_calls = None
        # Simulate user typing 'stop' right after Turn 1 LLM finishes
        if turn_counter == 1:
            thread_state_manager.request_stop(channel_id, thread_ts)
        return MagicMock(choices=[mock_choice], usage=None)

    monkeypatch.setattr("orchestration.agent_router.run_in_threadpool", mock_llm_run)

    await orchestrate_discussion(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="/[agent-10002] /[agent-10011] Discuss caching",
    )

    # Verify only Turn 1 ran, Turn 2 was stopped before execution
    assert turn_counter == 1
    assert len(posted_messages) == 1


@pytest.mark.anyio
async def test_process_and_respond_with_run_internal_loop(monkeypatch):
    """
    Verify process_and_respond(run_internal_loop=True) delegates directly to
    orchestrate_discussion.
    """
    mock_orchestrate = AsyncMock(return_value={"ok": True, "loop_ran": True})
    monkeypatch.setattr("orchestration.agent_router.orchestrate_discussion", mock_orchestrate)

    res = await process_and_respond(
        channel_id="C_DISPATCH",
        thread_ts="3000.0001",
        raw_text="Hello [agent-10001]",
        run_internal_loop=True,
    )

    assert res.get("loop_ran") is True
    mock_orchestrate.assert_awaited_once_with(
        channel_id="C_DISPATCH",
        thread_ts="3000.0001",
        raw_text="Hello [agent-10001]",
        bot_user_id=None,
        icon_url=None,
        user_id=None,
    )
