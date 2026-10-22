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
    ctx.agent_cfg = {"model": "test-model", "username": "Visionary", "skills": ["db_admin"]}
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
    skill_def.tools = ["execute_db_deletion"]
    skill_def.requires_human_ITL = True

    mock_skill_registry = MagicMock()
    mock_skill_registry.find_skill_for_tool.return_value = "db_admin"
    mock_skill_registry.get.return_value = skill_def
    mock_skill_registry.is_mcp_tool.return_value = False

    monkeypatch.setattr("orchestration.stages.execution.skill_registry", mock_skill_registry)
    monkeypatch.setattr("services.hitl.slack_async_client.chat_postMessage", AsyncMock(return_value={"ok": True, "ts": "11111"}))

    # Run execution stage
    await exec_stage.stage_execute_llm(ctx)

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


@pytest.mark.anyio
async def test_hitl_resume_generation_mismatch_fixed(monkeypatch):
    """Verifies TKT-904: resume_from_checkpoint syncs my_generation so active turns with generation >= 1 are not aborted as superseded."""
    channel_id = "C_HITL_GEN"
    thread_ts = "99999.0003"
    thread_state_manager.clear_state(channel_id, thread_ts)

    # Simulate 3 previous turns in the thread so current generation is 3
    thread_state_manager.increment_generation(channel_id, thread_ts)
    thread_state_manager.increment_generation(channel_id, thread_ts)
    thread_state_manager.increment_generation(channel_id, thread_ts)
    assert thread_state_manager.get_generation(channel_id, thread_ts) == 3

    chk = TurnCheckpoint(
        checkpoint_id="chk_test_gen_001",
        session_id=f"{channel_id}:{thread_ts}",
        channel_id=channel_id,
        thread_ts=thread_ts,
        agent_key="agent-10001",
        pause_reason="hitl",
        hitl_request_id="req_hitl_gen",
        messages=[
            {"role": "user", "content": "Update production records."},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_p1", "name": "update_records", "args": {}}]},
            {"role": "tool", "tool_call_id": "call_p1", "name": "update_records", "content": "TOOL EXECUTION PAUSED"},
        ],
        pending_tool_call={"id": "call_p1", "name": "update_records", "args": {}},
        active_tools=[],
        accumulated_history=[{"role": "user", "content": "Update production records."}],
        prompt_tokens=100,
        completion_tokens=20,
    )
    turn_checkpoint_store.save_checkpoint(chk)

    mock_followup = MagicMock()
    mock_followup.choices = [
        MagicMock(message=MagicMock(content="Production records updated successfully."))
    ]
    mock_followup.usage = MagicMock(prompt_tokens=120, completion_tokens=25)

    pipeline = TurnPipeline(
        llm_executor=AsyncMock(return_value=mock_followup),
        slack_poster=AsyncMock(return_value={"ok": True, "ts": "33333"}),
    )

    turn_res = await pipeline.resume_from_checkpoint(
        checkpoint=chk,
        tool_result="SUCCESS",
        user_id="U_ADMIN",
    )

    assert turn_res.get("ok") is True
    assert turn_res.get("status") != "superseded"
    assert "Production records updated successfully" in turn_res.get("final_text", "")


@pytest.mark.anyio
async def test_hitl_pause_synthesizes_deferred_sibling_responses(monkeypatch):
    """Verifies TKT-904: multi-tool batches pausing on HITL synthesize deferred tool responses for remaining sibling calls."""
    channel_id = "C_HITL_BATCH"
    thread_ts = "99999.0004"
    thread_state_manager.clear_state(channel_id, thread_ts)

    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="Batch dangerous operation with read operation",
        is_bot_trigger=False,
    )
    ctx.agent_key = "agent-10001"
    ctx.agent_cfg = {"model": "test-model", "username": "Visionary", "skills": ["danger_skill"]}
    ctx.messages = [{"role": "user", "content": ctx.raw_text}]

    # 2 tool calls in the same assistant response: first requires HITL, second is a sibling
    tc1 = MagicMock()
    tc1.id = "call_hitl_1"
    tc1.function.name = "execute_danger"
    tc1.function.arguments = json.dumps({"target": "cluster"})

    tc2 = MagicMock()
    tc2.id = "call_sibling_2"
    tc2.function.name = "read_document"
    tc2.function.arguments = json.dumps({"path": "doc.txt"})

    mock_msg = MagicMock()
    mock_msg.content = ""
    mock_msg.tool_calls = [tc1, tc2]
    mock_msg.model_dump.return_value = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "call_hitl_1", "type": "function", "function": {"name": "execute_danger", "arguments": tc1.function.arguments}},
            {"id": "call_sibling_2", "type": "function", "function": {"name": "read_document", "arguments": tc2.function.arguments}},
        ],
    }

    mock_llm_response = MagicMock()
    mock_llm_response.choices = [
        MagicMock(message=mock_msg)
    ]
    mock_llm_response.usage = MagicMock(prompt_tokens=100, completion_tokens=50)

    pipeline = TurnPipeline(
        llm_executor=AsyncMock(return_value=mock_llm_response)
    )
    exec_stage = ExecutionStage(pipeline)

    skill_def = MagicMock()
    skill_def.name = "danger_skill"
    skill_def.tools = ["execute_danger", "read_document"]
    skill_def.requires_human_ITL = True

    mock_skill_registry = MagicMock()
    mock_skill_registry.find_skill_for_tool.return_value = "danger_skill"
    mock_skill_registry.get.return_value = skill_def
    mock_skill_registry.is_mcp_tool.return_value = False

    monkeypatch.setattr("orchestration.stages.execution.skill_registry", mock_skill_registry)
    monkeypatch.setattr("services.hitl.slack_async_client.chat_postMessage", AsyncMock(return_value={"ok": True, "ts": "44444"}))

    await exec_stage.stage_execute_llm(ctx)

    assert ctx.waiting_hitl is True
    assert ctx.halted is True
    assert ctx.checkpoint_id is not None

    chk = turn_checkpoint_store.get_checkpoint(ctx.checkpoint_id)
    assert chk is not None

    # Check saved messages contain responses for both tool calls
    tool_messages = [m for m in chk.messages if isinstance(m, dict) and m.get("role") == "tool"]
    call_ids = {m.get("tool_call_id") for m in tool_messages}
    assert "call_hitl_1" in call_ids
    assert "call_sibling_2" in call_ids

    sibling_msg = next(m for m in tool_messages if m.get("tool_call_id") == "call_sibling_2")
    assert "TOOL EXECUTION DEFERRED" in sibling_msg.get("content", "")


