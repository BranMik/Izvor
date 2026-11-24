"""
Carbon-Audit cross-boundary fidelity test suite (TKT-1416 / TF-01..TF-12).

Verifies real cross-module seams without mock oversimplifications or monkeypatch.setattr:
- TF-01: ThreadStateManager real dispatch_discussion_task cancellation & pause liveness
- TF-02: Real TurnPipeline + asyncio.to_thread(forget_thread) + Telemetry buffer purge
- TF-05: Real TurnPipeline inside DiscussionLoop 2-round Scatter-Gather & partial HITL pause
- TF-06: Real post_agent_response DLP pipeline wired into PostRecordStage & interactive buttons
- TF-07: Real ToolLoopRunner HITL masking + RAM unmasking + inbound PII token mapping round-trip
- TF-10: Real error JSON / malformed control-plane effects rejected by ToolLoopRunner & EvidenceStore
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adapters.slack.actions.hitl_actions import _execute_and_resume_hitl_approval
from config_store.agent_settings import (
    get_orchestration_mode,
    get_sandwich_config,
    set_orchestration_mode,
    set_sandwich_config,
)
from core.config import (
    configure_token,
    reset_token,
    set_admin_slack_user_ids_override,
)
from core.database import get_db_transaction
from core.domain import TurnCheckpoint
from core.llm_utils import configure_llm_completion_fn, reset_llm_completion_fn
from core.security_utils import is_tool_error_payload
from orchestration.agent_router import (
    configure_router_deps,
    configure_router_sleep_fn,
    process_and_respond,
    reset_router_deps,
    reset_router_sleep_fn,
)
from orchestration.discussion_loop import DiscussionLoop
from orchestration.protocols import PipelineDeps
from orchestration.stages.control_plane_handler import ControlPlaneHandler
from orchestration.stages.post_record import PostRecordStage
from orchestration.stages.tool_loop_runner import ToolLoopRunner, _is_tool_error
from orchestration.state.evidence_store import RoundEvidenceStore
from orchestration.thread_state import thread_state_manager
from orchestration.turn_pipeline import TurnContext, TurnPipeline
from services import flat_history_recorder, telemetry
from services.hitl import expire_thread_requests, register_hitl_request
from services.mcp.security_bridge import async_sanitize_inbound_mcp_result
from services.slack.messages import (
    _prepare_outbound_payload,
    configure_post_agent_response,
    configure_post_system_message,
    post_agent_response,
    reset_post_agent_response,
    reset_post_system_message,
)
from services.thread_skills_store import thread_skills_manager
from services.turn_checkpoint_store import turn_checkpoint_store
from skills.parser import SkillDefinition
from skills.registry import skill_registry
from tests.e2e.workspace_adapter import InMemoryWorkspaceAdapter


@pytest.mark.anyio
async def test_tf01_real_dispatch_discussion_task_cancels_prior_and_pause_keeps_thread_active():
    """TF-01: Real dispatch_discussion_task cancels older active task, and active checkpoints/HITL keep thread active."""
    ch = "C_TF01"
    ts = "1700001416.0001"
    thread_state_manager.reset_all()
    thread_state_manager.reset_task_dispatcher()
    first_cancelled = False

    async def _first_worker():
        nonlocal first_cancelled
        try:
            await asyncio.sleep(30.0)
        except asyncio.CancelledError:
            first_cancelled = True
            raise

    async def _second_worker():
        await asyncio.sleep(0)
        return "second_done"

    try:
        thread_state_manager.dispatch_discussion_task(ch, ts, _first_worker())
        t1 = thread_state_manager.get_active_task(ch, ts)
        assert t1 is not None
        await asyncio.sleep(0)
        assert thread_state_manager.is_discussion_active(ch, ts) is True

        thread_state_manager.dispatch_discussion_task(ch, ts, _second_worker())
        t2 = thread_state_manager.get_active_task(ch, ts)
        assert t2 is not None and t2 is not t1
        assert await t2 == "second_done"
        assert first_cancelled is True

        chk = TurnCheckpoint(
            checkpoint_id="chk_tf01",
            session_id=f"{ch}:{ts}",
            channel_id=ch,
            thread_ts=ts,
            agent_key="agent-10001",
            pause_reason="hitl",
            status="ACTIVE",
        )
        turn_checkpoint_store.save_checkpoint(chk)
        assert thread_state_manager.is_discussion_active(ch, ts) is True

        thread_state_manager.interrupt_discussion(ch, ts)
        assert thread_state_manager.is_discussion_active(ch, ts) is False

        register_hitl_request(
            channel_id=ch,
            thread_ts=ts,
            fn_name="danger_tool",
            fn_args={"x": 1},
            agent_name="agent-10001",
            skill_name="tf01_skill",
        )
        assert thread_state_manager.is_discussion_active(ch, ts) is True

        thread_state_manager.request_stop(ch, ts)
        assert thread_state_manager.is_discussion_active(ch, ts) is False
    finally:
        expire_thread_requests(ch, ts)
        turn_checkpoint_store.abandon_thread_checkpoints(ch, ts)
        thread_state_manager.reset_all()


@pytest.mark.anyio
async def test_tf02_real_forget_thread_via_to_thread_purges_trace_buffer_without_self_cancel(tmp_path):
    """TF-02: Real TurnPipeline + /forget_thread via asyncio.to_thread purges traces without self-cancelling."""
    ch = "C_TF02"
    ts = "1700001416.0002"
    session_id = f"{ch}:{ts}"
    thread_state_manager.reset_all()
    telemetry.clear_turn_traces(session_id)
    set_admin_slack_user_ids_override("U_ADM_TF02")
    flat_history_recorder.configure_paths(tmp_path / "flat_tf02")

    try:
        telemetry.record_turn_trace(
            session_id=session_id,
            channel_id=ch,
            thread_ts=ts,
            agent_key="agent-10001",
            stage_name="stage4",
            duration_ms=25.0,
            status="SUCCESS",
        )
        await telemetry.drain_turn_traces(timeout=2.0)

        flat_history_recorder.record_flat_history(
            channel_id=ch,
            thread_ts=ts,
            initial_prompt="Sensitive topic",
            messages=[{"role": "user", "content": "Forget me"}],
        )

        deps = PipelineDeps(
            slack_poster=AsyncMock(return_value={"ok": True, "ts": "1.1"}),
            post_system_message_fn=AsyncMock(return_value={"ok": True}),
            history_fetcher=AsyncMock(return_value=[]),
            files_fetcher=AsyncMock(return_value=[]),
            workspace_adapter=InMemoryWorkspaceAdapter(),
            notes_updater=lambda **kw: None,
            usage_updater=lambda **kw: None,
        )
        pipeline = TurnPipeline(deps=deps)

        with patch("services.audit_logger.log_audit_event_async", AsyncMock()):
            res = await pipeline.run(
                TurnContext(
                    channel_id=ch,
                    thread_ts=ts,
                    raw_text="/forget_thread",
                    user_id="U_ADM_TF02",
                )
            )

        assert res["ok"] is True
        assert res["halted"] is True
        assert res["action"] == "forget_thread"
        assert telemetry.get_turn_traces(session_id=session_id) == []
    finally:
        telemetry.clear_turn_traces(session_id)
        flat_history_recorder.reset_paths()
        set_admin_slack_user_ids_override(None)
        thread_state_manager.reset_all()


@pytest.mark.anyio
async def test_tf05_real_pipeline_in_discussion_loop_scatter_gather_and_partial_hitl_pause():
    """TF-05: Real TurnPipeline inside DiscussionLoop preserves single-authority rounds and branch HITL state."""
    ch = "C_TF05"
    ts = "1700001416.0005"
    orig_mode = get_orchestration_mode()
    orig_sandwich = get_sandwich_config()
    thread_state_manager.cleanup_thread(ch, ts)

    async def _noop_sleep(_sec: float) -> None:
        return None

    async def _guarded_exec(arguments: dict, **_kwargs) -> str:
        return f"ok:{arguments.get('target', '')}"

    skill_registry.ensure_loaded()
    skill_inst = SkillDefinition(
        name="tf05_restricted_skill",
        display_name="TF05 Guarded Skill",
        description="Test HITL gate skill",
        tools=["tf05_guarded_op"],
        requires_human_ITL=True,
        custom_tool_schemas=[
            {
                "type": "function",
                "function": {
                    "name": "tf05_guarded_op",
                    "description": "Guarded test op",
                    "parameters": {"type": "object", "properties": {"target": {"type": "string"}}},
                },
            }
        ],
        tool_handlers={"tf05_guarded_op": _guarded_exec},
    )
    skill_registry._skills[skill_inst.name] = skill_inst

    try:
        set_orchestration_mode("scatter_gather", save=False)
        set_sandwich_config(["agent-10001"], ["agent-10006"], save=False)
        thread_state_manager.set_rounds(ch, ts, 2)
        thread_state_manager.set_round_sequence(
            ch, ts, ["agent-10001", "agent-10002", "agent-10003", "agent-10006"]
        )
        thread_state_manager.set_remaining_agents(
            ch, ts, ["agent-10001", "agent-10002", "agent-10003", "agent-10006"]
        )
        thread_skills_manager.grant_skills(ch, ts, [skill_inst.name])

        async def _mock_llm(*args, **kwargs):
            sys_prompt = ""
            msgs = kwargs.get("messages") or []
            if msgs and isinstance(msgs[0], dict):
                sys_prompt = str(msgs[0].get("content", ""))
            if '<agent_specification id="agent-10003"' in sys_prompt:
                tc = MagicMock()
                tc.id = "call_tf05_1"
                tc.function.name = "tf05_guarded_op"
                tc.function.arguments = json.dumps({"target": "prod-db"})
                return MagicMock(
                    choices=[MagicMock(message=MagicMock(content="", tool_calls=[tc]))],
                    usage=None,
                )
            return MagicMock(
                choices=[MagicMock(message=MagicMock(content="Scatter branch complete.", tool_calls=None))],
                usage=None,
            )

        deps = PipelineDeps(
            slack_poster=AsyncMock(return_value={"ok": True, "ts": "500.1"}),
            post_system_message_fn=AsyncMock(return_value={"ok": True}),
            history_fetcher=AsyncMock(return_value=[]),
            files_fetcher=AsyncMock(return_value=[]),
            workspace_adapter=InMemoryWorkspaceAdapter(),
            debate_turn_pacing_seconds=0.0,
            slack_post_pause_seconds=0.0,
            sleep_fn=_noop_sleep,
            token_recorder=lambda **kw: None,
            notes_updater=lambda **kw: None,
            usage_updater=lambda **kw: None,
        )
        configure_llm_completion_fn(_mock_llm)
        configure_post_agent_response(deps.slack_poster)
        configure_post_system_message(deps.post_system_message_fn)
        configure_router_deps(deps)
        configure_router_sleep_fn(_noop_sleep)

        loop = DiscussionLoop(turn_executor=process_and_respond, sleep_fn=_noop_sleep)
        with (
            patch("orchestration.discussion_loop.fetch_thread_history", AsyncMock(return_value=[])),
            patch("services.hitl.post_message_to_slack", AsyncMock(return_value={"ok": True, "ts": "500.9"})),
        ):
            res = await loop.run(channel_id=ch, thread_ts=ts, initial_raw_text="Inspect scatter HITL pause")

        assert res is not None
        assert res.get("waiting_hitl") is True or res.get("halted") is True
        assert thread_state_manager.get_rounds(ch, ts) == 2
        rem = thread_state_manager.get_remaining_agents(ch, ts)
        assert "agent-10002" not in rem
        chk = turn_checkpoint_store.get_latest_checkpoint_for_thread(ch, ts, pause_reason="hitl")
        assert chk is not None
        assert chk.agent_key == "agent-10003"
    finally:
        skill_registry._skills.pop(skill_inst.name, None)
        reset_llm_completion_fn()
        reset_post_agent_response()
        reset_post_system_message()
        reset_router_deps()
        reset_router_sleep_fn()
        expire_thread_requests(ch, ts)
        turn_checkpoint_store.abandon_thread_checkpoints(ch, ts)
        set_orchestration_mode(orig_mode, save=False)
        if orig_sandwich:
            set_sandwich_config(
                orig_sandwich.get("start_chain", []),
                orig_sandwich.get("end_chain", []),
                save=False,
            )
        thread_state_manager.cleanup_thread(ch, ts)


@pytest.mark.anyio
async def test_tf06_real_post_agent_response_dlp_propagates_to_post_record_and_preserves_hitl_buttons():
    """TF-06: Real post_agent_response DLP redacts ctx.output.final_text in PostRecordStage while keeping safe action blocks."""
    ch = "C_TF06"
    ts = "1700001416.0006"
    thread_state_manager.cleanup_thread(ch, ts)
    reset_post_agent_response()
    configure_token("xoxb-your-bot-user-oauth-token")

    try:
        deps = PipelineDeps(
            slack_poster=post_agent_response,
            history_fetcher=AsyncMock(return_value=[]),
            debate_turn_pacing_seconds=0.0,
            slack_post_pause_seconds=0.0,
            token_recorder=lambda **kw: None,
            notes_updater=lambda **kw: None,
            usage_updater=lambda **kw: None,
        )
        stage = PostRecordStage(deps)
        ctx = TurnContext(channel_id=ch, thread_ts=ts, raw_text="Share key")
        ctx.agent_key = "agent-10001"
        ctx.agent_cfg = {"username": "Visionary", "displayname": "Visionary", "icon_url": ""}
        fake_openai_key = "-".join(["sk", "proj", "1234567890abcdefghijklmnopqrstuvwxyzABCD"])
        fake_openai_prefix = "-".join(["sk", "proj", "1234567890"])
        ctx.output.final_text = f"Confidential token {fake_openai_key} should not leak"

        with patch("services.audit_logger.log_audit_event"):
            await stage.stage_post_and_record(ctx)

        assert fake_openai_prefix not in ctx.output.final_text
        assert "blocked by security policy" in ctx.output.final_text.lower()

        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": fake_openai_key}},
            {
                "type": "actions",
                "elements": [
                    {"type": "button", "action_id": "hitl_approve_tool", "value": "req_safe"},
                    {"type": "button", "action_id": "resume_sequence_action", "value": "chk_safe"},
                ],
            },
        ]
        with patch("services.audit_logger.log_audit_event"):
            clean_txt, clean_blocks, rule = _prepare_outbound_payload(
                f"Secret {fake_openai_key}",
                blocks=blocks,
                user_id="Visionary",
            )
        assert rule is not None
        assert fake_openai_prefix not in clean_txt
        action_ids = [
            el.get("action_id")
            for b in (clean_blocks or [])
            if b.get("type") == "actions"
            for el in (b.get("elements") or [])
        ]
        assert "hitl_approve_tool" in action_ids
        assert "resume_sequence_action" in action_ids
    finally:
        reset_token()
        reset_post_agent_response()
        thread_state_manager.cleanup_thread(ch, ts)


@pytest.mark.anyio
async def test_tf07_real_hitl_mask_unmask_and_inbound_pii_token_mapping_roundtrip():
    """TF-07: Masked PII persists to checkpoint & HITL table, unmasks in RAM on approval, and inbound PII masks back safely."""
    ch = "C_TF07"
    ts = "1700001416.0007"
    thread_state_manager.cleanup_thread(ch, ts)
    seen_ram_args: list[dict] = []

    async def _lookup_user_handler(arguments: dict, token_mapping: dict | None = None) -> str:
        seen_ram_args.append(dict(arguments))
        raw_out = "Primary contact alice@secret.corp; secondary contact bob@external.org"
        return await async_sanitize_inbound_mcp_result(
            raw_out,
            token_mapping=token_mapping,
        )

    skill_registry.ensure_loaded()
    skill_inst = SkillDefinition(
        name="tf07_pii_skill",
        display_name="TF07 PII HITL Skill",
        description="HITL skill verifying RAM unmasking and inbound masking",
        tools=["tf07_lookup_user"],
        requires_human_ITL=True,
        custom_tool_schemas=[
            {
                "type": "function",
                "function": {
                    "name": "tf07_lookup_user",
                    "description": "Lookup user",
                    "parameters": {"type": "object", "properties": {"email": {"type": "string"}}},
                },
            }
        ],
        tool_handlers={"tf07_lookup_user": _lookup_user_handler},
    )
    skill_registry._skills[skill_inst.name] = skill_inst

    try:
        thread_skills_manager.grant_skills(ch, ts, [skill_inst.name])
        with get_db_transaction(immediate=True) as conn:
            conn.execute("DELETE FROM hitl_approvals WHERE channel_id = ?", (ch,))

        tc = MagicMock()
        tc.id = "call_tf07"
        tc.function.name = "tf07_lookup_user"
        tc.function.arguments = json.dumps({"email": "alice@secret.corp"})

        mock_resp = MagicMock(choices=[MagicMock(message=MagicMock(content="", tool_calls=[tc]))], usage=None)
        deps = PipelineDeps(
            llm_executor=AsyncMock(return_value=mock_resp),
            slack_poster=AsyncMock(return_value={"ok": True, "ts": "700.1"}),
            post_system_message_fn=AsyncMock(return_value={"ok": True}),
            skill_registry=skill_registry,
            workspace_adapter=InMemoryWorkspaceAdapter(),
            notes_updater=lambda **kw: None,
            usage_updater=lambda **kw: None,
        )
        runner = ToolLoopRunner(deps)
        ctx = TurnContext(channel_id=ch, thread_ts=ts, raw_text="Lookup user")
        ctx.agent_key = "agent-10001"
        ctx.agent_cfg = {"model": "test-model", "username": "Visionary"}
        ctx.authorized_skills = [skill_inst.name]
        ctx.messages = [{"role": "user", "content": "Lookup user"}]
        ctx.turn_token_mapping = {"<EMAIL_0>": "alice@secret.corp"}
        ctx.control.tools = skill_inst.custom_tool_schemas

        with patch("services.hitl.post_message_to_slack", AsyncMock(return_value={"ok": True, "ts": "700.9"})):
            await runner.run(
                ctx,
                active_tools=skill_inst.custom_tool_schemas,
                agent_skills=[skill_inst.name],
                expired_thread_skills=[],
                thread_ttl_hours=24.0,
                initial_response_message=mock_resp.choices[0].message,
            )
        assert ctx.output.waiting_hitl is True
        chk = turn_checkpoint_store.get_latest_checkpoint_for_thread(ch, ts, pause_reason="hitl")
        assert chk is not None

        saved_args_str = json.dumps(chk.pending_tool_call)
        assert "alice@secret.corp" not in saved_args_str
        assert "<EMAIL_0>" in saved_args_str

        from services.hitl import approve_hitl_request

        approved_req = approve_hitl_request(chk.hitl_request_id)
        assert approved_req is not None
        assert "alice@secret.corp" not in json.dumps(approved_req.fn_args)

        resumed_calls: list[dict] = []

        async def _capture_resume(self_pipe, *, checkpoint, tool_result, user_id=None, is_denied=False):
            resumed_calls.append({"checkpoint": checkpoint, "tool_result": tool_result})
            return {"ok": True, "agent_key": checkpoint.agent_key, "final_text": "Done"}

        dispatched = []
        thread_state_manager.configure_task_dispatcher(lambda cid, tts, coro: dispatched.append(coro))
        with (
            patch("adapters.slack.actions.hitl_actions.is_slack_admin", return_value=True),
            patch("adapters.slack.actions.hitl_actions.post_message_to_slack", AsyncMock(return_value={"ok": True})),
            patch("orchestration.turn_pipeline.TurnPipeline.resume_from_checkpoint", _capture_resume),
            patch("orchestration.agent_router.continue_discussion_from_turn_result", AsyncMock()),
        ):
            await _execute_and_resume_hitl_approval(approved_req, user_id="U_ADMIN")
            assert len(dispatched) == 1
            await dispatched[0]

        assert seen_ram_args == [{"email": "alice@secret.corp"}]
        assert len(resumed_calls) == 1
        tool_out = str(resumed_calls[0]["tool_result"])
        assert "bob@external.org" not in tool_out
        assert "<EMAIL_0>" in tool_out
        assert "<EMAIL_1>" in tool_out
        assert resumed_calls[0]["checkpoint"].token_mapping.get("<EMAIL_1>") == "bob@external.org"
    finally:
        thread_state_manager.reset_task_dispatcher()
        skill_registry._skills.pop(skill_inst.name, None)
        expire_thread_requests(ch, ts)
        turn_checkpoint_store.abandon_thread_checkpoints(ch, ts)
        thread_state_manager.cleanup_thread(ch, ts)


def test_tf10_real_error_payloads_rejected_by_tool_loop_and_evidence_store():
    """TF-10: Unified is_tool_error_payload rejects MCP/RAG errors across ToolLoopRunner, EvidenceStore, and ControlPlaneHandler."""
    store = RoundEvidenceStore()

    mcp_err_json = json.dumps({"server": "db", "tool": "sql", "error": "timeout", "isError": True}, indent=2)
    rag_err_json = json.dumps({"ok": False, "isError": True, "error": "Embedding backend unavailable"})
    plain_err = "Error executing knowledge base search: connection refused"

    assert is_tool_error_payload(mcp_err_json) is True
    assert is_tool_error_payload(rag_err_json) is True
    assert is_tool_error_payload(plain_err) is True
    assert _is_tool_error(mcp_err_json) is True
    assert _is_tool_error(rag_err_json) is True

    for bad_payload in (mcp_err_json, rag_err_json, plain_err):
        added = store.add_evidence(
            channel_id="C_TF10",
            thread_ts="1.1",
            tool_name="mcp_tool",
            arguments={},
            result=bad_payload,
            agent_key="agent-10001",
        )
        assert added is None
    assert store.get_round_evidence("C_TF10", "1.1") == []

    handler = ControlPlaneHandler(PipelineDeps())
    ctx = TurnContext(channel_id="C_TF10", thread_ts="1.1", raw_text="Discuss")
    ctx.agent_key = "agent-10001"
    ctx.eligible_remaining_agents = ["agent-10002"]

    nom_err = handler.apply_tool_effect(
        fn_name="nominate_next_speaker",
        fn_args={"agent_name": "nonexistent_agent", "rationale": "x"},
        ctx=ctx,
    )
    assert nom_err is not None and is_tool_error_payload(nom_err) is True
    assert ctx.control.nomination is None

    clar_err = handler.apply_tool_effect(
        fn_name="request_user_clarification",
        fn_args={"question": "   "},
        ctx=ctx,
    )
    assert clar_err is not None and is_tool_error_payload(clar_err) is True
    assert ctx.control.clarification_requested is False
