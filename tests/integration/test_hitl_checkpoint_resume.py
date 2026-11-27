import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.database import get_db_transaction
from core.domain import TurnCheckpoint
from orchestration.stages.execution import ExecutionStage
from orchestration.thread_state import thread_state_manager
from orchestration.turn_pipeline import TurnContext, TurnPipeline
from services.turn_checkpoint_store import turn_checkpoint_store


@pytest.fixture(autouse=True)
def setup_teardown():
    with get_db_transaction(immediate=True) as conn:
        conn.execute("DELETE FROM hitl_approvals;")
    yield
    with get_db_transaction(immediate=True) as conn:
        conn.execute("DELETE FROM hitl_approvals;")


@pytest.mark.anyio
async def test_hitl_pause_creates_durable_turn_checkpoint():
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
    ctx.messages = [{"role": "user", "content": ctx.input.raw_text}]

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

    skill_def = MagicMock()
    skill_def.name = "db_admin"
    skill_def.tools = ["execute_db_deletion"]
    skill_def.requires_human_ITL = True

    mock_skill_registry = MagicMock()
    mock_skill_registry.find_skill_for_tool.return_value = "db_admin"
    mock_skill_registry.get.return_value = skill_def
    mock_skill_registry.get_global_skills.return_value = []

    # Mock execution stage dependencies
    pipeline = TurnPipeline(
        llm_executor=AsyncMock(return_value=mock_llm_response),
        skill_registry=mock_skill_registry,
    )
    exec_stage = ExecutionStage(pipeline.deps)

    with patch("services.hitl.post_message_to_slack", AsyncMock(return_value={"ok": True, "ts": "11111"})):
        # Run execution stage
        await exec_stage.stage_execute_llm(ctx)

    assert ctx.output.waiting_hitl is True
    assert ctx.output.halted is True
    assert ctx.output.checkpoint_id is not None

    chk = turn_checkpoint_store.get_checkpoint(ctx.output.checkpoint_id)
    assert chk is not None
    assert chk.pause_reason == "hitl"
    assert chk.status == "ACTIVE"
    assert chk.agent_key == "agent-10001"
    assert chk.hitl_request_id is not None

    assert chk.pending_tool_call["name"] == "execute_db_deletion"
    assert chk.pending_tool_call["args"] == {"table": "all"}


@pytest.mark.anyio
async def test_hitl_approve_restores_checkpoint_and_continues_remaining_agents():
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
async def test_hitl_resume_generation_mismatch_fixed():
    """Verifies TKT-904 / TKT-1401: matching my_generation resumes cleanly, while post-interrupt generation mismatch rejects as superseded."""
    from services.hitl import register_hitl_request, get_pending_request

    channel_id = "C_HITL_GEN"
    thread_ts = "99999.0003"
    thread_state_manager.clear_state(channel_id, thread_ts)

    # Simulate 3 previous turns in the thread so current generation is 3
    thread_state_manager.increment_generation(channel_id, thread_ts)
    thread_state_manager.increment_generation(channel_id, thread_ts)
    thread_state_manager.increment_generation(channel_id, thread_ts)
    assert thread_state_manager.get_generation(channel_id, thread_ts) == 3

    req = register_hitl_request(channel_id, thread_ts, "update_records", {})
    chk = TurnCheckpoint(
        checkpoint_id="chk_test_gen_001",
        session_id=f"{channel_id}:{thread_ts}",
        channel_id=channel_id,
        thread_ts=thread_ts,
        agent_key="agent-10001",
        pause_reason="hitl",
        hitl_request_id=req.request_id,
        my_generation=3,
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

    # Even with empty remaining_agents, active checkpoint makes thread active (CB-02)
    assert thread_state_manager.is_discussion_active(channel_id, thread_ts) is True

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
    assert turn_res.get("reason") != "superseded"
    assert "Production records updated successfully" in turn_res.get("final_text", "")

    # Now verify stale generation rejection + interrupt invalidation (CB-01)
    req_stale = register_hitl_request(channel_id, thread_ts, "update_records", {"id": 2})
    chk_stale = TurnCheckpoint(
        checkpoint_id="chk_test_gen_stale",
        session_id=f"{channel_id}:{thread_ts}",
        channel_id=channel_id,
        thread_ts=thread_ts,
        agent_key="agent-10001",
        pause_reason="hitl",
        hitl_request_id=req_stale.request_id,
        my_generation=3,
        messages=[{"role": "user", "content": "Old topic"}],
        pending_tool_call={"id": "call_p2", "name": "update_records", "args": {"id": 2}},
    )
    turn_checkpoint_store.save_checkpoint(chk_stale)

    thread_state_manager.interrupt_discussion(channel_id, thread_ts)
    assert thread_state_manager.get_generation(channel_id, thread_ts) == 4
    assert get_pending_request(req_stale.request_id) is None
    abandoned_chk = turn_checkpoint_store.get_checkpoint("chk_test_gen_stale")
    assert abandoned_chk is not None and abandoned_chk.status == "ABANDONED"

    # Even if a caller holds an in-memory snapshot of chk_stale, resume_from_checkpoint rejects it as superseded
    # Reset status to ACTIVE via save_checkpoint with old generation=3 to simulate race after increment_generation
    chk_stale.status = "ACTIVE"
    turn_checkpoint_store.save_checkpoint(chk_stale)
    stale_res = await pipeline.resume_from_checkpoint(
        checkpoint=chk_stale,
        tool_result="SUCCESS",
        user_id="U_ADMIN",
    )
    assert stale_res.get("cancelled") is True
    assert stale_res.get("reason") == "superseded"


@pytest.mark.anyio
async def test_hitl_pause_synthesizes_deferred_sibling_responses():
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
    ctx.messages = [{"role": "user", "content": ctx.input.raw_text}]

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

    skill_def = MagicMock()
    skill_def.name = "danger_skill"
    skill_def.tools = ["execute_danger", "read_document"]
    skill_def.requires_human_ITL = True

    mock_skill_registry = MagicMock()
    mock_skill_registry.find_skill_for_tool.return_value = "danger_skill"
    mock_skill_registry.get.return_value = skill_def
    mock_skill_registry.get_global_skills.return_value = []

    pipeline = TurnPipeline(
        llm_executor=AsyncMock(return_value=mock_llm_response),
        skill_registry=mock_skill_registry,
    )
    exec_stage = ExecutionStage(pipeline.deps)

    with patch("services.hitl.post_message_to_slack", AsyncMock(return_value={"ok": True, "ts": "44444"})):
        await exec_stage.stage_execute_llm(ctx)

    assert ctx.output.waiting_hitl is True
    assert ctx.output.halted is True
    assert ctx.output.checkpoint_id is not None

    chk = turn_checkpoint_store.get_checkpoint(ctx.output.checkpoint_id)
    assert chk is not None

    # Check saved messages contain responses for both tool calls
    tool_messages = [m for m in chk.messages if isinstance(m, dict) and m.get("role") == "tool"]
    call_ids = {m.get("tool_call_id") for m in tool_messages}
    assert "call_hitl_1" in call_ids
    assert "call_sibling_2" in call_ids

    sibling_msg = next(m for m in tool_messages if m.get("tool_call_id") == "call_sibling_2")
    assert "TOOL EXECUTION DEFERRED" in sibling_msg.get("content", "")


@pytest.mark.anyio
@pytest.mark.parametrize("initial_outcome", ["approved", "denied"])
async def test_resume_from_checkpoint_enforces_hitl_gate_on_subsequent_mutating_tool(initial_outcome):
    """TKT-1201 (C1): Resumed turns use ExecutionStage.execute_tool_loop() and pause again for unapproved HITL tools."""
    channel_id = f"C_NESTED_{initial_outcome}"
    thread_ts = "99999.0010"
    thread_state_manager.clear_state(channel_id, thread_ts)

    chk = TurnCheckpoint(
        checkpoint_id=f"chk_nested_{initial_outcome}",
        session_id=f"{channel_id}:{thread_ts}",
        channel_id=channel_id,
        thread_ts=thread_ts,
        agent_key="agent-10001",
        pause_reason="hitl",
        hitl_request_id=f"req_{initial_outcome}",
        messages=[
            {"role": "user", "content": "Run two gated actions."},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "name": "write_file", "args": {}}]},
            {"role": "tool", "tool_call_id": "call_1", "name": "write_file", "content": "PAUSED"},
        ],
        pending_tool_call={"id": "call_1", "name": "write_file", "args": {}},
        active_tools=[
            {
                "type": "function",
                "function": {"name": "execute_danger", "parameters": {"type": "object", "properties": {}}},
            }
        ],
    )
    turn_checkpoint_store.save_checkpoint(chk)

    tc2 = MagicMock()
    tc2.id = "call_nested_2"
    tc2.function.name = "execute_danger"
    tc2.function.arguments = json.dumps({"target": "prod"})

    msg1 = MagicMock()
    msg1.content = ""
    msg1.tool_calls = [tc2]
    msg1.model_dump.return_value = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "call_nested_2", "type": "function", "function": {"name": "execute_danger", "arguments": tc2.function.arguments}}],
    }
    resp1 = MagicMock(choices=[MagicMock(message=msg1)], usage=MagicMock(prompt_tokens=20, completion_tokens=10))
    msg2 = MagicMock(content="Waiting on second human approval.", tool_calls=None)
    resp2 = MagicMock(choices=[MagicMock(message=msg2)], usage=MagicMock(prompt_tokens=15, completion_tokens=10))

    skill_def = MagicMock()
    skill_def.name = "danger_skill"
    skill_def.tools = ["execute_danger"]
    skill_def.requires_human_ITL = True
    skill_def.is_global = False
    skill_def.requires_admin = False

    mock_reg = MagicMock()
    mock_reg.find_skill_for_tool.return_value = "danger_skill"
    mock_reg.get.return_value = skill_def
    mock_reg.get_global_skills.return_value = []
    mock_reg.filter_skills_for_caller.return_value = ["danger_skill"]
    mock_reg.execute_tool_async = AsyncMock(return_value="SHOULD_NOT_RUN")

    pipeline = TurnPipeline(
        llm_executor=AsyncMock(side_effect=[resp1, resp2]),
        skill_registry=mock_reg,
        slack_poster=AsyncMock(return_value={"ok": True, "ts": "77777"}),
    )

    with (
        patch("services.local_tools.is_agent_authorized_for_tool", return_value=True),
        patch("services.hitl.post_message_to_slack", AsyncMock(return_value={"ok": True, "ts": "88888"})),
    ):
        tool_payload = "OK" if initial_outcome == "approved" else '{"status": "denied", "error": "Denied"}'
        turn_res = await pipeline.resume_from_checkpoint(checkpoint=chk, tool_result=tool_payload, user_id="U_ADMIN")

    assert turn_res.get("waiting_hitl") is True
    assert turn_res.get("halted") is True
    # execute_danger MUST NOT have executed without approval
    for call in mock_reg.execute_tool_async.call_args_list:
        assert call.args[0] != "execute_danger"
    orig_chk = turn_checkpoint_store.get_checkpoint(chk.checkpoint_id)
    assert orig_chk is not None and orig_chk.status == "RESUMED"


@pytest.mark.anyio
async def test_resume_from_checkpoint_unlocks_skills_control_plane_and_claims_atomically():
    """TKT-1201 (C1): Concurrent resume_from_checkpoint executes once; load_skill + control_plane work identically."""
    from skills.registry import LOAD_SKILL_SCHEMA

    channel_id = "C_RESUME_ATOM"
    thread_ts = "99999.0011"
    thread_state_manager.clear_state(channel_id, thread_ts)
    thread_state_manager.set_remaining_agents(channel_id, thread_ts, ["agent-10002"])

    chk = TurnCheckpoint(
        checkpoint_id="chk_atomic_resume",
        session_id=f"{channel_id}:{thread_ts}",
        channel_id=channel_id,
        thread_ts=thread_ts,
        agent_key="agent-10001",
        pause_reason="hitl",
        messages=[
            {"role": "user", "content": "Analyze and hand off"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "t1", "args": {}}]},
            {"role": "tool", "tool_call_id": "c1", "name": "t1", "content": "PAUSED"},
        ],
        pending_tool_call={"id": "c1", "name": "t1", "args": {}},
        active_tools=[LOAD_SKILL_SCHEMA],
    )
    turn_checkpoint_store.save_checkpoint(chk)

    tc_load = MagicMock()
    tc_load.id = "c_load_cp"
    tc_load.function.name = "load_skill"
    tc_load.function.arguments = json.dumps({"skill_name": "control_plane"})

    msg1 = MagicMock(content="", tool_calls=[tc_load])
    msg1.model_dump.return_value = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "c_load_cp", "type": "function", "function": {"name": "load_skill", "arguments": tc_load.function.arguments}}],
    }

    tc_nom = MagicMock()
    tc_nom.id = "c_nom"
    tc_nom.function.name = "nominate_next_speaker"
    tc_nom.function.arguments = json.dumps({"agent_name": "agent-10002", "rationale": "Architecture handoff"})
    msg2 = MagicMock(content="", tool_calls=[tc_nom])
    msg2.model_dump.return_value = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "c_nom", "type": "function", "function": {"name": "nominate_next_speaker", "arguments": tc_nom.function.arguments}}],
    }

    msg3 = MagicMock(content="Handing off to Architect.", tool_calls=None)
    llm_mock = AsyncMock(
        side_effect=[
            MagicMock(choices=[MagicMock(message=msg1)], usage=None),
            MagicMock(choices=[MagicMock(message=msg2)], usage=None),
            MagicMock(choices=[MagicMock(message=msg3)], usage=None),
        ]
    )

    pipeline = TurnPipeline(
        llm_executor=llm_mock,
        slack_poster=AsyncMock(return_value={"ok": True, "ts": "99999"}),
    )

    import asyncio

    res1, res2 = await asyncio.gather(
        pipeline.resume_from_checkpoint(checkpoint="chk_atomic_resume", tool_result="OK", user_id="U_ADMIN"),
        pipeline.resume_from_checkpoint(checkpoint="chk_atomic_resume", tool_result="OK", user_id="U_ADMIN"),
    )
    reasons = {r.get("reason") for r in (res1, res2)}
    assert "checkpoint_already_claimed" in reasons
    successes = [r for r in (res1, res2) if r.get("reason") != "checkpoint_already_claimed"]
    assert len(successes) == 1
    assert successes[0].get("ok") is True
    assert "[agent-10002]" in successes[0].get("final_text", "")


@pytest.mark.anyio
async def test_dispatch_discussion_task_cancels_previous_task_and_stop_invalidates_pauses():
    """TKT-1401: dispatch_discussion_task cancels superseded active task and request_stop increments generation + clears file_context + abandons checkpoints."""
    import asyncio
    from services.hitl import register_hitl_request, get_pending_request

    channel_id = "C_DISP_CANCEL"
    thread_ts = "99999.0012"
    thread_state_manager.clear_state(channel_id, thread_ts)

    cancelled_flag = False
    started_evt = asyncio.Event()

    async def slow_worker():
        nonlocal cancelled_flag
        started_evt.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled_flag = True
            raise

    done_evt = asyncio.Event()

    async def second_worker():
        done_evt.set()

    thread_state_manager.dispatch_discussion_task(channel_id, thread_ts, slow_worker())
    await started_evt.wait()
    first_task = thread_state_manager.get_active_task(channel_id, thread_ts)
    assert first_task is not None

    thread_state_manager.dispatch_discussion_task(channel_id, thread_ts, second_worker())
    await done_evt.wait()
    await asyncio.sleep(0)
    assert cancelled_flag is True
    assert first_task.cancelled() or first_task.done()

    # Now verify request_stop clears file_context, invalidates checkpoints + HITL, and blocks late checkpoint resume
    gen_before = thread_state_manager.get_generation(channel_id, thread_ts)
    thread_state_manager.set_file_context(channel_id, thread_ts, {"filename": "secret.txt", "content": "content"})
    req = register_hitl_request(channel_id, thread_ts, "write_note", {"text": "hi"})
    stop_chk = TurnCheckpoint(
        checkpoint_id="chk_stop_inv",
        session_id=f"{channel_id}:{thread_ts}",
        channel_id=channel_id,
        thread_ts=thread_ts,
        agent_key="agent-10001",
        pause_reason="hitl",
        hitl_request_id=req.request_id,
        my_generation=gen_before,
        messages=[{"role": "user", "content": "old"}],
    )
    turn_checkpoint_store.save_checkpoint(stop_chk)
    thread_state_manager.request_stop(channel_id, thread_ts)
    assert thread_state_manager.is_stopped(channel_id, thread_ts) is True
    assert thread_state_manager.get_file_context(channel_id, thread_ts) is None
    assert get_pending_request(req.request_id) is None
    stopped_chk = turn_checkpoint_store.get_checkpoint("chk_stop_inv")
    assert stopped_chk is not None and stopped_chk.status == "ABANDONED"

    # Even if re-saved ACTIVE while stop_requested=1, resume_from_checkpoint rejects it
    stop_chk.status = "ACTIVE"
    turn_checkpoint_store.save_checkpoint(stop_chk)
    pipeline = TurnPipeline(
        llm_executor=AsyncMock(),
        slack_poster=AsyncMock(return_value={"ok": True, "ts": "99999"}),
    )
    late_res = await pipeline.resume_from_checkpoint(checkpoint=stop_chk, tool_result="OK", user_id="U_ADMIN")
    assert late_res.get("cancelled") is True
    assert late_res.get("reason") == "stopped"


@pytest.mark.anyio
async def test_graceful_shutdown_cancel_releases_checkpoint_claim_and_superseded_tool_loop_returns_dict():
    """
    TKT-1407 (CB-07):
    1. External asyncio.CancelledError during resume_from_checkpoint (graceful shutdown) calls release_claim()
       reverting status back to ACTIVE with payloads preserved.
    2. Superseding a turn during ToolLoopRunner returns a structured cancellation dict instead of raising
       asyncio.CancelledError into Scatter-Gather TaskGroups.
    """
    import asyncio
    from orchestration.stages.tool_loop_runner import ToolLoopRunner
    from orchestration.protocols import PipelineDeps
    from orchestration.turn_pipeline import TurnContext

    ch = "C_SHUTDOWN_CHK"
    ts = "99999.0020"
    thread_state_manager.clear_state(ch, ts)

    chk = TurnCheckpoint(
        checkpoint_id="chk_shutdown_rel",
        session_id=f"{ch}:{ts}",
        channel_id=ch,
        thread_ts=ts,
        agent_key="agent-10001",
        pause_reason="hitl",
        prompt="Important prompt",
        messages=[{"role": "user", "content": "Do work"}],
        pending_tool_call={"id": "c_sd", "name": "write_file", "args": {}},
    )
    turn_checkpoint_store.save_checkpoint(chk)

    started = asyncio.Event()

    async def hanging_llm(*args, **kwargs):
        started.set()
        await asyncio.sleep(30)

    pipeline = TurnPipeline(
        llm_executor=hanging_llm,
        slack_poster=AsyncMock(return_value={"ok": True, "ts": "123"}),
    )

    task = asyncio.create_task(
        pipeline.resume_from_checkpoint(checkpoint="chk_shutdown_rel", tool_result="SAVED_OUTPUT", user_id="U_ADMIN")
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    restored = turn_checkpoint_store.get_checkpoint("chk_shutdown_rel")
    assert restored is not None
    assert restored.status == "ACTIVE"
    assert restored.prompt == "Important prompt"
    assert any("SAVED_OUTPUT" in str(m.get("content", "")) for m in restored.messages)

    # Part 2: ToolLoopRunner supersession returns dict instead of raising CancelledError
    ch_s = "C_SUPER_TOOL"
    ts_s = "99999.0021"
    thread_state_manager.clear_state(ch_s, ts_s)
    ctx = TurnContext(channel_id=ch_s, thread_ts=ts_s, raw_text="test")
    ctx.agent_key = "agent-10001"
    ctx.agent_cfg = {"model": "test-model", "skills": []}
    ctx.my_generation = thread_state_manager.get_generation(ch_s, ts_s)

    tc = MagicMock()
    tc.id = "c_s1"
    tc.function.name = "unknown_tool"
    tc.function.arguments = "{}"
    first_msg = MagicMock(content="", tool_calls=[tc])
    first_msg.model_dump.return_value = {"role": "assistant", "content": ""}

    async def _superseding_exec(*args, **kwargs):
        thread_state_manager.increment_generation(ch_s, ts_s)
        return None

    runner = ToolLoopRunner(PipelineDeps(llm_executor=_superseding_exec))
    res = await runner.run(
        ctx,
        active_tools=[],
        agent_skills=[],
        expired_thread_skills=[],
        thread_ttl_hours=1.0,
        initial_response_message=first_msg,
    )
    assert isinstance(res, dict)
    assert res.get("cancelled") is True
    assert res.get("reason") == "superseded"


@pytest.mark.anyio
async def test_checkpoint_remasking_preserves_provider_thought_signature_and_word_boundaries():
    """Plan 227: Field-scoped word-boundary remasking preserves Base64 thought_signature and avoids mid-word corruption."""
    from orchestration.protocols import PipelineDeps
    from orchestration.stages.tool_loop_runner import ToolLoopRunner

    ch = "C_CHK_REMASK"
    ts = "99999.0030"
    thread_state_manager.clear_state(ch, ts)

    sig_gemini = "Eu4SCusSAVjVB3tJD99wiT8erCSEao/r6XYMst6/JDjayzA85=="
    sig_anthropic = "Ev4PCvsPAVjVB3tJDxyz123456=="

    ctx = TurnContext(channel_id=ch, thread_ts=ts, raw_text="Lead Generation for JD")
    ctx.agent_key = "agent-10014"
    ctx.agent_cfg = {"model": "gemini/gemini-3.8-flash", "skills": []}
    ctx.turn_token_mapping = {"<LOCATION_0>": "JD", "<PERSON_1>": "Lead Gen"}
    ctx.messages = [
        {"role": "system", "content": "System prompt mentioning Lead Gen and JD"},
        {"role": "user", "content": "Lead Generation review for JD"},
        {
            "role": "assistant",
            "content": None,
            "provider_specific_fields": {"thought_signature": sig_gemini},
            "thinking_blocks": [{"type": "thinking", "thinking": "Lead Gen notes", "signature": sig_anthropic}],
            "tool_calls": [
                {
                    "id": "call_JD_1",
                    "type": "function",
                    "function": {
                        "name": "write_document",
                        "arguments": json.dumps({"rel_path": "out.md", "content": "Lead Generation and JD"}),
                    },
                    "provider_specific_fields": {"thought_signature": sig_gemini},
                }
            ],
        },
    ]

    runner = ToolLoopRunner(PipelineDeps())
    chk_id = runner.save_execution_checkpoint(
        ctx=ctx,
        pause_reason="hitl",
        pending_tool_call={"id": "call_JD_1", "name": "write_document", "args": {"content": "Lead Generation and JD"}},
    )
    saved = turn_checkpoint_store.get_checkpoint(chk_id)
    assert saved is not None

    # System message untouched
    assert saved.messages[0]["content"] == "System prompt mentioning Lead Gen and JD"
    # Word boundaries: 'Lead Generation' preserved, standalone 'JD' remasked
    assert saved.messages[1]["content"] == "Lead Generation review for <LOCATION_0>"
    # Opaque provider signatures & tool call IDs byte-for-byte untouched
    asst_msg = saved.messages[2]
    assert asst_msg["provider_specific_fields"]["thought_signature"] == sig_gemini
    assert asst_msg["thinking_blocks"][0]["signature"] == sig_anthropic
    assert asst_msg["tool_calls"][0]["id"] == "call_JD_1"
    assert asst_msg["tool_calls"][0]["provider_specific_fields"]["thought_signature"] == sig_gemini
    assert "Lead Generation and <LOCATION_0>" in asst_msg["tool_calls"][0]["function"]["arguments"]
    assert "<PERSON_1>eration" not in asst_msg["tool_calls"][0]["function"]["arguments"]
    # Pending tool call remasks args only
    assert saved.pending_tool_call["id"] == "call_JD_1"
    assert saved.pending_tool_call["args"]["content"] == "Lead Generation and <LOCATION_0>"

    turn_checkpoint_store.mark_abandoned(chk_id)
    thread_state_manager.cleanup_thread(ch, ts)


@pytest.mark.anyio
async def test_cancelled_turn_cleans_up_intermediate_hourglass_message():
    """Plan 227: Cancelling an in-flight turn updates any posted intermediate hourglass message in Slack."""
    import asyncio
    from tests.e2e.workspace_adapter import InMemoryWorkspaceAdapter

    ch = "C_CANCEL_HOURGLASS"
    ts = "99999.0031"
    thread_state_manager.clear_state(ch, ts)

    ws = InMemoryWorkspaceAdapter()
    started = asyncio.Event()

    async def _slow_llm(*args, **kwargs):
        ctx_msg = await ws.post_agent_turn(channel_id=ch, thread_ts=ts, text="_⏳ *Tactician* is analyzing data..._", username="Tactician")
        ctx.intermediate_msg_ts = ctx_msg["ts"]
        started.set()
        await asyncio.sleep(30)

    pipeline = TurnPipeline(
        llm_executor=_slow_llm,
        workspace_adapter=ws,
        history_fetcher=AsyncMock(return_value=[]),
        files_fetcher=AsyncMock(return_value=[]),
        notes_updater=lambda **kw: None,
        usage_updater=lambda **kw: None,
        flat_history_recorder=lambda **kw: None,
    )
    ctx = TurnContext(channel_id=ch, thread_ts=ts, raw_text="Analyze", is_bot_trigger=True, target_agent_override="agent-10014")

    task = asyncio.create_task(pipeline.run(ctx))
    await started.wait()
    thread_state_manager.request_stop(ch, ts)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Allow scheduled cleanup task to run
    await asyncio.sleep(0.05)
    assert any(u["text"] == "_⏹️ Stopped._" and u["message_ts"] == ctx.intermediate_msg_ts for u in ws.updated_turns)
    thread_state_manager.cleanup_thread(ch, ts)


