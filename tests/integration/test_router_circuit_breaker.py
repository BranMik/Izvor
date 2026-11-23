from unittest.mock import AsyncMock, MagicMock

import pytest

from conftest import async_noop_sleep, make_mock_acompletion
from core.llm_utils import configure_llm_completion_fn
from agents import (
    configure_calculate_thread_costs,
    configure_trigger_thread_notes_update,
    configure_trigger_usage_update,
)
from orchestration.agent_router import (
    configure_router_sleep_fn,
    process_and_respond,
)
from config_store.agent_settings import (
    reset_enabled_agents,
    set_fixed_config,
    set_orchestration_mode,
    set_sandwich_config,
)
from config_store.app_settings import (
    get_circuit_breaker_config,
    get_circuit_breaker_threshold,
    is_circuit_breaker_enabled,
    set_circuit_breaker_enabled,
    set_circuit_breaker_threshold,
)
from orchestration.thread_state import thread_state_manager
from services.slack import configure_fetch_thread_history, configure_post_agent_response


@pytest.fixture(autouse=True)
def setup_test_environment():
    reset_enabled_agents()
    set_orchestration_mode("sandwich")
    set_sandwich_config([], [])
    set_fixed_config([])
    thread_state_manager.reset_all()
    configure_router_sleep_fn(async_noop_sleep)


def test_circuit_breaker_configuration_methods():
    reset_enabled_agents()
    set_circuit_breaker_enabled(True)
    assert is_circuit_breaker_enabled() is True

    set_circuit_breaker_threshold(2.75)
    assert get_circuit_breaker_threshold() == 2.75

    cfg = get_circuit_breaker_config()
    assert cfg.enabled is True
    assert cfg.threshold_usd == 2.75

    set_circuit_breaker_enabled(False)
    assert is_circuit_breaker_enabled() is False

    # Negative threshold should clamp to 0.0
    set_circuit_breaker_threshold(-1.0)
    assert get_circuit_breaker_threshold() == 0.0


@pytest.mark.anyio
async def test_circuit_breaker_triggers_when_threshold_exceeded():
    thread_state_manager.discussion.reset()
    channel_id = "C_CB_TRIGGER"
    thread_ts = "123456789.9001"
    thread_state_manager.discussion.set_remaining_agents(channel_id, thread_ts, ["agent-10011", "agent-10008"])

    set_circuit_breaker_enabled(True)
    set_circuit_breaker_threshold(0.50)

    # Mock calculate_thread_costs to return $0.75 (> $0.50)
    configure_calculate_thread_costs(lambda ch, ts: {"grand_total_cost_usd": 0.75})

    posted_messages = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None, **kwargs
    ):
        posted_messages.append({"text": text, "username": username})
        return {"ok": True, "ts": "12345.9999"}

    llm_called = False

    def mock_agent_turn(*args, **kwargs):
        nonlocal llm_called
        llm_called = True
        raise RuntimeError("LLM should not be called when circuit-breaker is triggered")

    mock_execute_llm = make_mock_acompletion(mock_agent_turn)

    notes_updated = False

    def mock_trigger_notes(channel_id, thread_ts, is_completed=False, **kwargs):
        nonlocal notes_updated
        if is_completed:
            notes_updated = True

    usage_updated = False

    def mock_trigger_usage(channel_id, thread_ts, is_completed=False, **kwargs):
        nonlocal usage_updated
        if is_completed:
            usage_updated = True

    configure_post_agent_response(mock_post_agent_response)
    configure_llm_completion_fn(mock_execute_llm)
    configure_trigger_thread_notes_update(mock_trigger_notes)
    configure_trigger_usage_update(mock_trigger_usage)

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="Hello agents, continue analysis",
        is_bot_trigger=False,
    )

    assert res.get("circuit_breaker_triggered") is True
    assert llm_called is False
    assert len(posted_messages) == 1
    assert (
        posted_messages[0]["text"]
        == "EXPENSE LIMIT REACHED FOR THE THREAD. ENDING CALLS"
    )
    assert notes_updated is False  # Suppressed to avoid burning LLM tokens after breaker trip
    assert usage_updated is True
    assert thread_state_manager.discussion.has_remaining_agents_key(channel_id, thread_ts)
    assert thread_state_manager.discussion.get_remaining_agents(channel_id, thread_ts) == ["agent-10008"]


@pytest.mark.anyio
async def test_circuit_breaker_allows_normal_response_when_below_threshold():
    thread_state_manager.discussion.reset()
    channel_id = "C_CB_BELOW"
    thread_ts = "123456789.9002"

    set_circuit_breaker_enabled(True)
    set_circuit_breaker_threshold(1.00)

    # Cost is $0.20 (< $1.00)
    configure_calculate_thread_costs(lambda ch, ts: {"grand_total_cost_usd": 0.20})

    mock_choice = MagicMock()
    mock_choice.message.content = "Normal agent response [agent-10008]"
    mock_choice.message.tool_calls = None

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=10, completion_tokens=10)

    mock_execute_llm = make_mock_acompletion(lambda *a, **k: mock_response)

    posted_messages = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted_messages.append({"text": text, "username": username})
        return {"ok": True, "ts": "12345.9999"}

    configure_post_agent_response(mock_post_agent_response)
    configure_llm_completion_fn(mock_execute_llm)
    configure_fetch_thread_history(AsyncMock(return_value=[]))

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="[agent-10001] Normal query",
        is_bot_trigger=False,
    )

    assert res.get("ok") is True
    assert res.get("circuit_breaker_triggered") is not True
    assert len(posted_messages) == 1
    assert "Normal agent response" in posted_messages[0]["text"]


@pytest.mark.anyio
async def test_circuit_breaker_allows_normal_response_when_disabled():
    thread_state_manager.discussion.reset()
    channel_id = "C_CB_DISABLED"
    thread_ts = "123456789.9003"

    set_circuit_breaker_enabled(False)
    set_circuit_breaker_threshold(0.10)

    # Cost is $5.00 (> $0.10), but circuit breaker is disabled
    configure_calculate_thread_costs(lambda ch, ts: {"grand_total_cost_usd": 5.00})

    mock_choice = MagicMock()
    mock_choice.message.content = "Normal response despite high cost"
    mock_choice.message.tool_calls = None

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=10, completion_tokens=10)

    mock_execute_llm = make_mock_acompletion(lambda *a, **k: mock_response)

    posted_messages = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted_messages.append({"text": text, "username": username})
        return {"ok": True, "ts": "12345.9999"}

    configure_post_agent_response(mock_post_agent_response)
    configure_llm_completion_fn(mock_execute_llm)
    configure_fetch_thread_history(AsyncMock(return_value=[]))

    try:
        res = await process_and_respond(
            channel_id=channel_id,
            thread_ts=thread_ts,
            raw_text="[agent-10001] Normal query",
            is_bot_trigger=False,
        )

        assert res.get("ok") is True
        assert res.get("circuit_breaker_triggered") is not True
        assert len(posted_messages) == 1
        assert "Normal response despite high cost" in posted_messages[0]["text"]
    finally:
        set_circuit_breaker_enabled(True)


@pytest.mark.anyio
async def test_circuit_breaker_resume_sequence_stage2_and_stage4_checkpoints(tmp_path):
    """TKT-1210 (C7): Resume Sequence action handles both Stage-2 (empty messages) and Stage-4 (mid-turn) CB checkpoints."""
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from core.config import set_admin_slack_user_ids_override
    from core.domain import TurnCheckpoint
    from main import app
    from services.turn_checkpoint_store import turn_checkpoint_store

    client = TestClient(app)
    set_admin_slack_user_ids_override("U_CB_ADMIN")
    dispatched_coros = []
    thread_state_manager.configure_task_dispatcher(lambda ch, ts, coro: dispatched_coros.append((ch, ts, coro)))

    try:
        # Case B: Stage-2 pre-turn checkpoint (empty messages, populated prompt)
        ch2 = "C_CB_STG2"
        ts2 = "1700000100.0001"
        chk2 = TurnCheckpoint(
            checkpoint_id="chk_cb_stage2",
            session_id=f"{ch2}:{ts2}",
            channel_id=ch2,
            thread_ts=ts2,
            agent_key="agent-10001",
            pause_reason="circuit_breaker",
            prompt="Analyze system reliability",
            messages=[],
        )
        turn_checkpoint_store.save_checkpoint(chk2)

        observed_prompts = []

        async def _fake_orchestrate(**kwargs):
            observed_prompts.append(kwargs)

        with (
            patch("adapters.slack.interactivity.verify_slack_signature", return_value=True),
            patch("orchestration.agent_router.orchestrate_discussion", side_effect=_fake_orchestrate),
        ):
            payload2 = {
                "type": "block_actions",
                "user": {"id": "U_CB_ADMIN"},
                "actions": [{"action_id": "resume_sequence_action"}],
                "channel": {"id": ch2},
                "container": {"thread_ts": ts2},
                "message": {"ts": "1700000100.0002"},
            }
            resp2 = client.post("/slack/actions", json=payload2)
            assert resp2.status_code == 200
            assert len(dispatched_coros) == 1
            await dispatched_coros[0][2]
            assert observed_prompts[0]["raw_text"] == "Analyze system reliability"
            assert observed_prompts[0]["target_agent_override"] == "agent-10001"
            updated2 = turn_checkpoint_store.get_checkpoint("chk_cb_stage2")
            assert updated2 is not None and updated2.status == "RESUMED"

        # Case A: Stage-4 mid-turn checkpoint (non-empty messages) -> calls resume_from_checkpoint
        dispatched_coros.clear()
        ch4 = "C_CB_STG4"
        ts4 = "1700000200.0001"
        chk4 = TurnCheckpoint(
            checkpoint_id="chk_cb_stage4",
            session_id=f"{ch4}:{ts4}",
            channel_id=ch4,
            thread_ts=ts4,
            agent_key="agent-10002",
            pause_reason="circuit_breaker",
            prompt="Mid-turn tool resumption",
            messages=[{"role": "user", "content": "Continue reading file"}],
        )
        turn_checkpoint_store.save_checkpoint(chk4)

        mock_followup = MagicMock()
        mock_followup.choices = [MagicMock(message=MagicMock(content="Finished post-breaker analysis.", tool_calls=None))]
        mock_followup.usage = MagicMock(prompt_tokens=10, completion_tokens=10)
        configure_llm_completion_fn(make_mock_acompletion(lambda *a, **k: mock_followup))
        configure_post_agent_response(AsyncMock(return_value={"ok": True, "ts": "99.99"}))

        with (
            patch("adapters.slack.interactivity.verify_slack_signature", return_value=True),
            patch("orchestration.agent_router.continue_discussion_from_turn_result", new_callable=AsyncMock) as mock_cont,
        ):
            payload4 = {
                "type": "block_actions",
                "user": {"id": "U_CB_ADMIN"},
                "actions": [{"action_id": "resume_sequence_action"}],
                "channel": {"id": ch4},
                "container": {"thread_ts": ts4},
                "message": {"ts": "1700000200.0002"},
            }
            resp4 = client.post("/slack/actions", json=payload4)
            assert resp4.status_code == 200
            assert len(dispatched_coros) == 1
            await dispatched_coros[0][2]
            mock_cont.assert_awaited_once()
            updated4 = turn_checkpoint_store.get_checkpoint("chk_cb_stage4")
            assert updated4 is not None and updated4.status == "RESUMED"
    finally:
        thread_state_manager.reset_task_dispatcher()
        set_admin_slack_user_ids_override(None)


@pytest.mark.anyio
async def test_mid_turn_budget_does_not_double_count_persisted_tokens():
    """TKT-1214: _is_mid_turn_budget_exceeded subtracts already-persisted tokens and excludes own reservation."""
    from orchestration.stages.execution import ExecutionStage
    from orchestration.turn_pipeline import TurnContext, TurnPipeline
    from pipeline.spend_governance.circuit_breaker import spend_governance
    from services.pricing_service import pricing_service

    channel_id = "C_CB_MIDTURN_NODUP"
    thread_ts = "1700000300.0001"
    session_id = f"{channel_id}:{thread_ts}"
    model = "anthropic/claude-opus-5"
    pricing = pricing_service.get_pricing(model)
    in_rate = float(pricing["input_price_per_1m"])

    # Choose token counts cleanly corresponding to ~$0.06 and ~$0.045 based on active model rates
    prompt_toks_1 = int(round((0.06 / max(in_rate, 0.001)) * 1_000_000))
    prompt_toks_2 = int(round((0.045 / max(in_rate, 0.001)) * 1_000_000))

    set_circuit_breaker_enabled(True)
    set_circuit_breaker_threshold(0.10)
    spend_governance.clear_all_sessions()

    pipeline = TurnPipeline()
    stage = ExecutionStage(pipeline.deps)
    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="Analyze budget",
        agent_cfg={"model": model},
    )
    ctx.agent_key = "agent-10002"

    # Simulate persisted spend from step 1 (~$0.06 < $0.10), plus an active own scatter reservation ($0.05)
    spend_governance.reserve_budget(
        session_id=session_id,
        reservation_id=f"{session_id}:{ctx.agent_key}",
        estimated_usd=0.05,
        threshold_usd=0.50,
        enabled=True,
    )
    try:
        ctx.add_usage(MagicMock(model=model, usage={"prompt_tokens": prompt_toks_1, "completion_tokens": 0}))
        assert ctx.output.persisted_prompt_tokens == prompt_toks_1
        assert stage._is_mid_turn_budget_exceeded(ctx) is False

        # Second tool iteration pushes real persisted spend to ~$0.105 >= $0.10
        ctx.add_usage(MagicMock(model=model, usage={"prompt_tokens": prompt_toks_2, "completion_tokens": 0}))
        assert stage._is_mid_turn_budget_exceeded(ctx) is True
    finally:
        spend_governance.clear_all_sessions()


@pytest.mark.anyio
async def test_scatter_gather_enforces_reserve_budget_with_tight_threshold():
    """TKT-1214: scatter-gather branches check reserve_budget() against thread threshold and halt excess branches."""
    import asyncio
    from orchestration.discussion_loop import DiscussionLoop
    from pipeline.spend_governance.circuit_breaker import spend_governance

    channel_id = "C_SCATTER_BUDGET"
    thread_ts = "1700000400.0001"
    thread_state_manager.cleanup_thread(channel_id, thread_ts)
    spend_governance.clear_all_sessions()
    configure_fetch_thread_history(AsyncMock(return_value=[]))

    set_circuit_breaker_enabled(True)
    set_circuit_breaker_threshold(0.11)  # Allows 2 x $0.05 reservations, denies the 3rd
    set_orchestration_mode("scatter_gather", save=False)
    set_sandwich_config(["agent-10001"], ["agent-10006"])
    thread_state_manager.set_remaining_agents(
        channel_id,
        thread_ts,
        ["agent-10002", "agent-10003", "agent-10004", "agent-10006"],
    )

    executed_scatter: list[str] = []

    async def fake_executor(**kwargs):
        agent_target = kwargs.get("target_agent_override") or "agent-10001"
        if agent_target in ("agent-10002", "agent-10003", "agent-10004"):
            executed_scatter.append(agent_target)
            await asyncio.sleep(0)
        return {
            "ok": True,
            "agent_key": agent_target,
            "final_text": f"Output from {agent_target}",
            "username": agent_target,
            "channel_id": channel_id,
            "thread_ts": thread_ts,
        }

    loop = DiscussionLoop(turn_executor=fake_executor, sleep_fn=async_noop_sleep)
    try:
        res = await loop.run(
            channel_id=channel_id,
            thread_ts=thread_ts,
            initial_raw_text="Topic",
        )
        assert isinstance(res, dict)
        assert res.get("circuit_breaker_triggered") is True
        assert res.get("halted") is True
        assert len(executed_scatter) == 2
    finally:
        spend_governance.clear_all_sessions()
        thread_state_manager.cleanup_thread(channel_id, thread_ts)


@pytest.mark.anyio
async def test_mid_turn_tool_loop_circuit_breaker_renders_blocks_suspends_and_skips_scribe_and_revisor():
    """TKT-1303: Mid-turn CB trip posts Continue Sequence blocks, strips nominations, suspends thread, and skips Scribe & Revisor."""
    from unittest.mock import patch

    from config_store.agent_settings import reset_enabled_agents
    from core.config import set_internal_revisor_enabled_override
    from core.domain import TurnCheckpoint
    from orchestration.turn_pipeline import TurnContext, TurnPipeline
    from pipeline.spend_governance.circuit_breaker import spend_governance
    from services.pricing_service import pricing_service
    from services.turn_checkpoint_store import turn_checkpoint_store

    reset_enabled_agents()
    channel_id = "C_CB_MIDTURN_POST"
    thread_ts = "1700000499.0001"
    thread_state_manager.cleanup_thread(channel_id, thread_ts)
    spend_governance.clear_all_sessions()

    model = "anthropic/claude-opus-5"
    pricing = pricing_service.get_pricing(model)
    in_rate = float(pricing["input_price_per_1m"])
    tokens_to_exceed = int(round((0.15 / max(in_rate, 0.001)) * 1_000_000))

    set_circuit_breaker_enabled(True)
    set_circuit_breaker_threshold(0.10)
    set_internal_revisor_enabled_override(True)
    thread_state_manager.set_remaining_agents(channel_id, thread_ts, ["agent-10002", "agent-10003"])
    import asyncio
    dummy_task = asyncio.create_task(asyncio.sleep(10))
    thread_state_manager.set_active_task(channel_id, thread_ts, dummy_task)

    posted: list[dict] = []
    notes_called: list[bool] = []
    usage_called: list[bool] = []
    revisor_called: list[bool] = []

    async def fake_post(channel_id, thread_ts, text, username, icon_url=None, **kwargs):
        posted.append({"text": text, "username": username, **kwargs})
        return {"ok": True, "ts": "1700000499.0002"}

    async def fake_tool(fn_name, fn_args, **kwargs):
        return "web search evidence"

    tc = MagicMock()
    tc.id = "call_1"
    tc.function.name = "web_search"
    tc.function.arguments = '{"query": "cost"}'
    tc2 = MagicMock()
    tc2.id = "call_2"
    tc2.function.name = "web_search"
    tc2.function.arguments = '{"query": "more"}'

    follow_up_resp = MagicMock()
    follow_up_resp.model = model
    follow_up_resp.choices = [MagicMock(message=MagicMock(content="Partial findings before pause.", tool_calls=[tc2]))]
    follow_up_resp.usage = MagicMock(prompt_tokens=tokens_to_exceed, completion_tokens=10)

    pipeline = TurnPipeline(
        llm_executor=make_mock_acompletion(lambda *a, **k: follow_up_resp),
        tool_executor=fake_tool,
        slack_poster=fake_post,
        notes_updater=lambda **kw: notes_called.append(True),
        usage_updater=lambda **kw: usage_called.append(bool(kw.get("is_completed"))),
        sleep_fn=async_noop_sleep,
        debate_turn_pacing_seconds=0.0,
        slack_post_pause_seconds=0.0,
    )

    ctx = TurnContext(channel_id=channel_id, thread_ts=thread_ts, raw_text="Analyze", user_id="U1")
    ctx.agent_key = "agent-10001"
    ctx.agent_cfg = {"model": model, "username": "Visionary"}
    ctx.remaining = ["agent-10002", "agent-10003"]
    ctx.eligible_remaining_agents = ["agent-10002", "agent-10003"]
    ctx.next_designated_agent = "agent-10002"
    ctx.messages = [{"role": "user", "content": "Analyze"}]

    init_msg = MagicMock(content="", tool_calls=[tc])
    try:
        with (
            patch("services.local_tools.is_agent_authorized_for_tool", return_value=True),
            patch("orchestration.revisor_service.run_internal_revisor", side_effect=lambda *a, **k: revisor_called.append(True)),
        ):
            halt = await pipeline.execution_stage.execute_tool_loop(
                ctx,
                active_tools=[],
                agent_skills=[],
                expired_thread_skills=[],
                thread_ttl_hours=24.0,
                initial_response_message=init_msg,
            )
            assert halt is None
            res = await pipeline.post_record_stage.stage_post_and_record(ctx)
            await pipeline._execute_revisor_barrier(ctx, res)

        assert res["circuit_breaker_triggered"] is True
        assert res["halted"] is True
        assert res["is_completed"] is False
        assert len(posted) == 1
        assert "/[agent-10002]" not in posted[0]["text"]
        assert "blocks" in posted[0]
        btn_actions = [
            el.get("action_id")
            for b in posted[0]["blocks"]
            for el in b.get("elements", [])
        ]
        assert "resume_sequence_action" in btn_actions
        assert thread_state_manager.get_active_task(channel_id, thread_ts) is None
        assert thread_state_manager.get_remaining_agents(channel_id, thread_ts) == ["agent-10002", "agent-10003"]
        assert notes_called == []
        assert usage_called == [True]
        assert revisor_called == []
        assert ctx.output.checkpoint_id
        chk = turn_checkpoint_store.get_checkpoint(ctx.output.checkpoint_id)
        assert isinstance(chk, TurnCheckpoint)
        assert chk.pause_reason == "circuit_breaker"
    finally:
        if not dummy_task.done():
            dummy_task.cancel()
        set_internal_revisor_enabled_override(None)
        spend_governance.clear_all_sessions()
        thread_state_manager.cleanup_thread(channel_id, thread_ts)
