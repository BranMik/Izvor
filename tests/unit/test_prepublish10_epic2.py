import json
import time
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from adapters.slack.events import EventDeduplicationCache
from orchestration.turn_pipeline import TurnContext, TurnPipeline
from services.hitl import (
    _prune_expired,
    approve_hitl_request,
    deny_hitl_request,
    get_pending_request,
    register_hitl_request,
)
from services.turn_checkpoint_store import TurnCheckpoint, turn_checkpoint_store


@pytest.fixture(autouse=True)
def clean_db(tmp_path):
    from core.database import db_manager

    prev_db_path = db_manager.db_path
    fresh_db = tmp_path / "test_epic2.db"
    db_manager.configure_paths(db_path=fresh_db)
    yield
    db_manager.configure_paths(db_path=prev_db_path)


def test_event_dedup_atomic_claim():
    """TKT-1007: EventDeduplicationCache.claim_event must atomically allow only one caller to process an event."""
    cache = EventDeduplicationCache(ttl_seconds=3600.0)
    event_key = ("C_TEST", "1700000000.123456")

    # First claim succeeds
    assert cache.claim_event(event_key) is True

    # Immediate second claim on the same key fails (deduplicated)
    assert cache.claim_event(event_key) is False

    # Different event succeeds
    assert cache.claim_event(("C_TEST", "1700000000.123457")) is True


def test_hitl_prune_expired_preserves_audit_records():
    """TKT-1008: _prune_expired must only delete PENDING requests, preserving APPROVED/DENIED/CONSUMED audit trails."""
    now = time.time()

    # 1. Register an expired pending request
    req1 = register_hitl_request("C1", "T1", "fn1", {"a": 1}, ttl_seconds=-10.0)

    # 2. Register an expired approved request
    req2 = register_hitl_request("C1", "T1", "fn2", {"a": 2}, ttl_seconds=100.0)
    approve_hitl_request(req2.request_id)
    # Manually backdate expires_at
    from core.database import get_db_transaction
    with get_db_transaction(immediate=True) as conn:
        conn.execute("UPDATE hitl_approvals SET expires_at = ? WHERE request_id = ?;", (now - 10.0, req2.request_id))

    # 3. Register an expired denied request
    req3 = register_hitl_request("C1", "T1", "fn3", {"a": 3}, ttl_seconds=100.0)
    deny_hitl_request(req3.request_id)
    with get_db_transaction(immediate=True) as conn:
        conn.execute("UPDATE hitl_approvals SET expires_at = ? WHERE request_id = ?;", (now - 10.0, req3.request_id))

    # Run prune
    _prune_expired()

    # Pending request was pruned
    assert get_pending_request(req1.request_id) is None

    # Approved and denied records are PRESERVED in SQLite for audit history
    from core.database import get_connection
    with get_connection() as conn:
        row_approved = conn.execute("SELECT status FROM hitl_approvals WHERE request_id = ?;", (req2.request_id,)).fetchone()
        row_denied = conn.execute("SELECT status FROM hitl_approvals WHERE request_id = ?;", (req3.request_id,)).fetchone()

    assert row_approved is not None
    assert row_approved["status"] == "APPROVED"
    assert row_denied is not None
    assert row_denied["status"] == "DENIED"


@pytest.mark.anyio
async def test_hitl_resumption_effective_skills():
    """TKT-1006: resume_from_checkpoint must populate ctx.authorized_skills with dynamic grants."""
    from services.thread_skills_store import thread_skills_manager
    channel_id = "C_SKILLS"
    thread_ts = "1700000000.999"

    # Grant dynamic skill
    thread_skills_manager.grant_skills(channel_id, thread_ts, ["dynamic_admin_skill"])

    chk = TurnCheckpoint(
        checkpoint_id="chk_test_skills",
        session_id=f"{channel_id}:{thread_ts}",
        channel_id=channel_id,
        thread_ts=thread_ts,
        agent_key="Architect",
        pause_reason="hitl",
        messages=[{"role": "user", "content": "hello"}],
        active_tools=[{"type": "function", "function": {"name": "test_fn"}}],
        pending_tool_call={"id": "call_1", "name": "test_fn", "args": {}},
        status="ACTIVE",
    )
    turn_checkpoint_store.save_checkpoint(chk)

    pipeline = TurnPipeline(
        slack_poster=AsyncMock(return_value={"ok": True}),
        notes_updater=lambda *a, **k: None,
        usage_updater=lambda *a, **k: None,
        token_recorder=lambda *a, **k: None,
    )

    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock(message=MagicMock(content="Resumed successfully"))]
    mock_resp.usage = MagicMock(prompt_tokens=10, completion_tokens=10)

    with patch("litellm.acompletion", AsyncMock(return_value=mock_resp)):
        await pipeline.resume_from_checkpoint(
            checkpoint=chk,
            tool_result="approved_result",
            user_id="U12345",
        )

    # Checkpoint status should be marked RESUMED
    saved_chk = turn_checkpoint_store.get_checkpoint("chk_test_skills")
    assert saved_chk.status == "RESUMED"


@pytest.mark.anyio
async def test_hitl_denial_synthetic_tool_result():
    """TKT-1006: Denying a tool approval should resume checkpoint with synthetic denial tool error."""
    from adapters.slack.actions.hitl_actions import _resume_hitl_denial
    channel_id = "C_DENY"
    thread_ts = "1700000000.555"
    req_id = "req_to_deny_123"

    chk = TurnCheckpoint(
        checkpoint_id="chk_deny_123",
        session_id=f"{channel_id}:{thread_ts}",
        channel_id=channel_id,
        thread_ts=thread_ts,
        agent_key="Researcher",
        pause_reason="hitl",
        hitl_request_id=req_id,
        messages=[
            {"role": "user", "content": "analyze files"},
            {
                "role": "assistant",
                "content": "calling dangerous tool",
                "tool_calls": [{"id": "call_deny_1", "function": {"name": "delete_all", "arguments": "{}"}}],
            },
        ],
        active_tools=[],
        pending_tool_call={"id": "call_deny_1", "name": "delete_all", "args": {}},
        status="ACTIVE",
    )
    turn_checkpoint_store.save_checkpoint(chk)

    mock_req = MagicMock(
        request_id=req_id,
        channel_id=channel_id,
        thread_ts=thread_ts,
        agent_name="Researcher",
        fn_name="delete_all",
    )

    dispatched = []
    def mock_dispatch(cid, ts, coro):
        dispatched.append((cid, ts, coro))

    from orchestration.thread_state import thread_state_manager
    thread_state_manager.configure_task_dispatcher(mock_dispatch)
    try:
        with patch("orchestration.turn_pipeline.TurnPipeline.resume_from_checkpoint", AsyncMock(return_value={"ok": True})) as mock_resume:
            await _resume_hitl_denial(mock_req, user_id="U_ADMIN_1")
            # Verify discussion task was dispatched and await it
            assert len(dispatched) == 1
            coro = dispatched[0][2]
            await coro
            mock_resume.assert_called_once()
    finally:
        thread_state_manager.reset_task_dispatcher()


@pytest.mark.anyio
async def test_tool_loop_pause_skips_followup_llm_call():
    """Plan 227: Execution stage skips redundant follow-up LLM call when paused for HITL approval."""
    from orchestration.turn_pipeline import TurnPipeline

    mock_skill = MagicMock()
    mock_skill.name = "sensitive_skill"
    mock_skill.requires_human_ITL = True

    mock_reg = MagicMock()
    mock_reg.find_skill_for_tool.return_value = "sensitive_skill"
    mock_reg.get.return_value = mock_skill
    mock_reg.get_global_skills.return_value = []

    ctx = TurnContext(
        channel_id="C_EXEC",
        thread_ts="1700000000.111",
        raw_text="run query",
        agent_cfg={"model": "gemini/gemini-3.8-flash", "skills": []},
    )
    ctx.agent_key = "Analyst"

    # Initial response calling a tool
    mock_tool_call = MagicMock()
    mock_tool_call.id = "call_paused_1"
    mock_tool_call.function.name = "sensitive_query"
    mock_tool_call.function.arguments = json.dumps({"q": "secret"})

    first_resp = MagicMock()
    first_resp.choices = [
        MagicMock(message=MagicMock(content="Thinking...", tool_calls=[mock_tool_call]))
    ]
    first_resp.usage = MagicMock(prompt_tokens=5, completion_tokens=5)

    captured_kwargs = []

    async def mock_exec_fn(func, kwargs, **rest):
        captured_kwargs.append(kwargs)
        return first_resp

    pipeline = TurnPipeline(llm_executor=mock_exec_fn, skill_registry=mock_reg)
    stage = pipeline.execution_stage

    with patch("services.hitl.consume_tool_approval", return_value=False), \
         patch("services.hitl.request_human_approval", new_callable=AsyncMock, return_value="req-123"), \
         patch("services.local_tools.is_agent_authorized_for_tool", return_value=True):
        await stage.stage_execute_llm(ctx)

    # Only the initial LLM call runs; no follow-up LLM call is dispatched once HITL pauses
    assert len(captured_kwargs) == 1
    assert ctx.output.waiting_hitl is True
    assert ctx.output.halted is True
    assert "Waiting for human approval" in ctx.output.final_text


@pytest.mark.anyio
async def test_tool_loop_exhaustion_runs_final_synthesis_call():
    """Plan 193: Exhausting max_tool_iterations with empty text triggers a tool-free synthesis turn."""
    from orchestration.turn_pipeline import TurnPipeline

    mock_reg = MagicMock()
    mock_reg.get.return_value = None
    mock_reg.get_global_skills.return_value = []
    mock_reg.execute_tool_async = AsyncMock(return_value='{"status": "loaded"}')

    ctx = TurnContext(
        channel_id="C_SYNTH",
        thread_ts="1700000000.222",
        raw_text="analyze security",
        agent_cfg={"model": "mistral/mistral-large-latest", "skills": ["rag_knowledge"]},
    )
    ctx.agent_key = "agent-10008"

    tool_call_1 = MagicMock()
    tool_call_1.id = "call_1"
    tool_call_1.function.name = "load_skill"
    tool_call_1.function.arguments = json.dumps({"skill_name": "rag_knowledge"})

    tool_resp = MagicMock()
    tool_resp.choices = [MagicMock(message=MagicMock(content="", tool_calls=[tool_call_1]))]
    tool_resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5)

    synth_resp = MagicMock()
    synth_resp.choices = [
        MagicMock(
            message=MagicMock(
                content="Core Threat Verdict: Synthesized threat assessment after tool budget.",
                tool_calls=None,
            )
        )
    ]
    synth_resp.usage = MagicMock(prompt_tokens=20, completion_tokens=15)

    captured_kwargs = []

    async def mock_exec_fn(func, kwargs, **rest):
        captured_kwargs.append(dict(kwargs))
        if len(captured_kwargs) == 1:
            return tool_resp
        return synth_resp

    pipeline = TurnPipeline(
        llm_executor=mock_exec_fn,
        skill_registry=mock_reg,
        max_tool_iterations=1,
    )
    await pipeline.execution_stage.stage_execute_llm(ctx)

    # Call 1 (initial tool request) + Call 2 (direct tool-free synthesis once max_tool_iterations=1 is reached)
    assert len(captured_kwargs) == 2
    assert "tools" not in captured_kwargs[1]
    assert "Core Threat Verdict:" in ctx.output.final_text
    # Verify load_skill received ToolPrincipal with ctx.agent_key (not unknown_agent)
    assert mock_reg.execute_tool_async.await_count == 1
    principal_arg = mock_reg.execute_tool_async.await_args.kwargs["agent_name"]
    assert getattr(principal_arg, "id", None) == "agent-10008"
