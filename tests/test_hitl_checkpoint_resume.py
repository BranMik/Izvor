import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from core.domain import TurnCheckpoint
from orchestration.stages.execution import ExecutionStage
from orchestration.thread_state import thread_state_manager
from orchestration.turn_pipeline import TurnContext, TurnPipeline
from services.hitl import reset_pending_requests
from services.turn_checkpoint_store import turn_checkpoint_store


@pytest.fixture(autouse=True)
def setup_teardown():
    reset_pending_requests()
    yield
    reset_pending_requests()


@pytest.mark.anyio
async def test_hitl_pause_creates_durable_turn_checkpoint(monkeypatch):
    """Verifies TKT-809: when a destructive tool requires human approval, execution pauses and saves a durable checkpoint."""
    channel_id = "C_HITL_TEST"
    thread_ts = "99999.0001"
    thread_state_manager.clear_state(channel_id, thread_ts)

    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="Please delete all database tables.",
        is_bot_trigger=False,
    )
    ctx.agent_key = "agent-10001"
    ctx.agent_cfg = {"model": "test-model", "username": "Visionary"}
    ctx.messages = [{"role": "user", "content": ctx.raw_text}]

    # Mock tool call that requires human approval
    tool_call = MagicMock()
    tool_call.id = "call_del_123"
    tool_call.function.name = "execute_db_deletion"
    tool_call.function.arguments = json.dumps({"table": "all"})

    mock_llm_response = MagicMock()
    mock_llm_response.choices = [
        MagicMock(
            message=MagicMock(
                content="",
                tool_calls=[tool_call],
            )
        )
    ]
    mock_llm_response.usage = MagicMock(prompt_tokens=100, completion_tokens=50)

    # Mock execution stage dependencies
    pipeline = TurnPipeline(
        llm_executor=AsyncMock(return_value=mock_llm_response)
    )
    exec_stage = ExecutionStage(pipeline)

    skill_def = MagicMock()
    skill_def.name = "db_admin"
    skill_def.requires_human_ITL = True

    mock_skill_registry = MagicMock()
    mock_skill_registry.find_skill_for_tool.return_value = "db_admin"
    mock_skill_registry.get.return_value = skill_def
    mock_skill_registry.is_mcp_tool.return_value = False

    monkeypatch.setattr("orchestration.stages.execution.skill_registry", mock_skill_registry)
    monkeypatch.setattr("services.hitl.slack_async_client.chat_postMessage", AsyncMock(return_value={"ok": True, "ts": "11111"}))

    # Run execution stage
    res = await exec_stage.stage_execute_llm(ctx)

    assert ctx.waiting_hitl is True
    assert ctx.halted is True
    assert ctx.checkpoint_id is not None

    chk = turn_checkpoint_store.get_checkpoint(ctx.checkpoint_id)
    assert chk is not None
    assert chk.pause_reason == "hitl"
    assert chk.status == "ACTIVE"
    assert chk.agent_key == "agent-10001"
    assert chk.hitl_request_id is not None

    assert chk.pending_tool_call["name"] == "execute_db_deletion"
    assert chk.pending_tool_call["args"] == {"table": "all"}


@pytest.mark.anyio
async def test_hitl_approve_restores_checkpoint_and_continues_remaining_agents(monkeypatch):
    """Verifies TKT-810: resume_from_checkpoint restores context, completes execution, and marks checkpoint RESUMED."""
    channel_id = "C_HITL_RESUME"
    thread_ts = "99999.0002"
    thread_state_manager.clear_state(channel_id, thread_ts)

    initial_messages = [
        {"role": "user", "content": "Run the dangerous migration."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_mig_456", "name": "run_migration", "args": {}}]},
        {"role": "tool", "tool_call_id": "call_mig_456", "name": "run_migration", "content": "TOOL EXECUTION PAUSED"},
    ]

    chk = TurnCheckpoint(
        checkpoint_id="chk_test_resume_001",
        session_id=f"{channel_id}:{thread_ts}",
        channel_id=channel_id,
        thread_ts=thread_ts,
        agent_key="agent-10001",
        pause_reason="hitl",
        hitl_request_id="req_hitl_abc",
        messages=initial_messages,
        pending_tool_call={"id": "call_mig_456", "name": "run_migration", "args": {}},
        active_tools=[],
        accumulated_history=[{"role": "user", "content": "Run the dangerous migration."}],
        prompt_tokens=150,
        completion_tokens=40,
    )
    turn_checkpoint_store.save_checkpoint(chk)

    # Mock follow-up LLM completion
    mock_followup = MagicMock()
    mock_followup.choices = [
        MagicMock(message=MagicMock(content="Migration executed successfully without any errors."))
    ]
    mock_followup.usage = MagicMock(prompt_tokens=200, completion_tokens=30)

    posted_messages = []

    async def mock_post(channel_id, thread_ts, text, username, icon_url=None, blocks=None):
        posted_messages.append({"text": text, "username": username})
        return {"ok": True, "ts": "22222"}

    pipeline = TurnPipeline(
        llm_executor=AsyncMock(return_value=mock_followup),
        slack_poster=mock_post,
    )

    # Resume turn from checkpoint
    turn_res = await pipeline.resume_from_checkpoint(
        checkpoint=chk,
        tool_result="MIGRATION SUCCESS: 5 tables updated",
        user_id="U_APPROVER",
    )

    assert turn_res.get("ok") is True
    assert "Migration executed successfully" in turn_res.get("final_text", "")
    assert len(posted_messages) == 1

    # Verify checkpoint is marked RESUMED
    updated_chk = turn_checkpoint_store.get_checkpoint("chk_test_resume_001")
    assert updated_chk is not None
    assert updated_chk.status == "RESUMED"
