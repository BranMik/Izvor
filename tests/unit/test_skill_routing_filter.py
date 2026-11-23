import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from adapters.slack.actions.governance_actions import _handle_toggle_skill_filter
from core.models import ActionDirective
from pipeline.directive_parser.parser import ParsedDirectives
from config_store.app_settings import (
    is_filter_unauthorized_skill_agents_enabled,
    set_filter_unauthorized_skill_agents_enabled,
)
from orchestration.thread_state import thread_state_manager
from orchestration.turn_pipeline import TurnContext, TurnPipeline
from services.slack_home.governance_tab import build_governance_blocks


@pytest.fixture(autouse=True)
def reset_skill_filter_setting():
    original = is_filter_unauthorized_skill_agents_enabled()
    set_filter_unauthorized_skill_agents_enabled(True)
    yield
    set_filter_unauthorized_skill_agents_enabled(original)


@pytest.mark.anyio
async def test_pure_skill_task_filters_unauthorized_agents_when_enabled():
    """Verifies that in pure skill tasks with toggle ON, unauthorized agents are filtered out and orchestrator notice is posted."""
    channel_id = "C_SKILL_ROUTING_1"
    thread_ts = "100000.0001"
    thread_state_manager.clear_state(channel_id, thread_ts)

    # Set up initial pool: agent-10001 (unauthorized for document_reader), agent-10002 (authorized for document_reader)
    thread_state_manager.set_remaining_agents(channel_id, thread_ts, ["agent-10002"])

    posted_sys_messages = []

    async def mock_post_sys_msg(channel_id, thread_ts, text):
        posted_sys_messages.append(text)
        return {"ok": True}

    pipeline = TurnPipeline(
        post_system_message_fn=mock_post_sys_msg,
        files_fetcher=AsyncMock(return_value=[{"id": "F1", "name": "spec.pdf"}]),
    )

    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="Analiziraj privitak spec.pdf",
        is_bot_trigger=False,
    )
    directives = ParsedDirectives(
        cleaned_prompt="Analiziraj privitak spec.pdf",
        actions=[ActionDirective(name="document_reader", params={"target_file": "spec.pdf"})],
        is_pure_skill_task=True,
    )

    with (
        patch("orchestration.stages.validation.directive_parser.parse", AsyncMock(return_value=directives)),
        patch("orchestration.stages.validation.get_initial_agents", return_value=["agent-10001", "agent-10002"]),
        patch("orchestration.stages.validation.parse_agent_and_prompt", return_value=("agent-10001", "Analiziraj privitak spec.pdf", False)),
        patch("orchestration.stages.validation.download_slack_file", AsyncMock(return_value={"ok": True, "content": "PDF data", "size": 100})),
    ):
        halt = await pipeline.validation_stage.stage_validate_and_parse(ctx)

    assert halt is None  # Not halted, continues to authorized agent
    # Authorized agent-10002 was selected!
    assert ctx.agent_key == "agent-10002"
    # Unauthorized agent-10001 filtered out from remaining pool
    assert thread_state_manager.get_remaining_agents(channel_id, thread_ts) == []
    # Verify Orchestrator notice was posted
    assert len(posted_sys_messages) == 1
    assert "Skill Routing Filter" in posted_sys_messages[0]
    assert "Filtering out unauthorized agent(s)" in posted_sys_messages[0]
    assert "document_reader" in posted_sys_messages[0]


@pytest.mark.anyio
async def test_zero_authorized_agents_halts_cleanly_with_notice_and_hint():
    """Verifies that when 0 agents in the chain possess the skill, system halts cleanly without calling LLM."""
    channel_id = "C_SKILL_ROUTING_2"
    thread_ts = "100000.0002"
    thread_state_manager.clear_state(channel_id, thread_ts)

    # agent-10001 and agent-10003 both lack document_reader
    thread_state_manager.set_remaining_agents(channel_id, thread_ts, ["agent-10003"])

    posted_sys_messages = []

    async def mock_post_sys_msg(channel_id, thread_ts, text):
        posted_sys_messages.append(text)
        return {"ok": True}

    pipeline = TurnPipeline(
        post_system_message_fn=mock_post_sys_msg,
        files_fetcher=AsyncMock(return_value=[{"id": "F1", "name": "spec.pdf"}]),
    )

    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="Analiziraj privitak spec.pdf",
        is_bot_trigger=False,
    )
    directives = ParsedDirectives(
        cleaned_prompt="Analiziraj privitak spec.pdf",
        actions=[ActionDirective(name="document_reader", params={"target_file": "spec.pdf"})],
        is_pure_skill_task=True,
    )

    with (
        patch("orchestration.stages.validation.directive_parser.parse", AsyncMock(return_value=directives)),
        patch("orchestration.stages.validation.get_initial_agents", return_value=["agent-10001", "agent-10003"]),
        patch("orchestration.stages.validation.parse_agent_and_prompt", return_value=("agent-10001", "Analiziraj privitak spec.pdf", False)),
    ):
        halt = await pipeline.validation_stage.stage_validate_and_parse(ctx)

    assert halt is not None
    assert halt.get("ok") is True
    assert halt.get("halted") is True
    assert halt.get("reason") == "no_skill_authorized_agents_in_chain"
    assert thread_state_manager.get_remaining_agents(channel_id, thread_ts) == []
    assert thread_state_manager.has_remaining_agents_key(channel_id, thread_ts) is False
    assert len(posted_sys_messages) == 1
    assert "No agents in the active workflow are authorized for this skill" in posted_sys_messages[0]
    assert "/grant_skill" in posted_sys_messages[0]


@pytest.mark.anyio
async def test_toggle_off_prioritizes_authorized_agent_first_and_keeps_unauthorized_for_debate():
    """Verifies that with toggle OFF, authorized agents are reordered to speak first, followed by unauthorized ones."""
    channel_id = "C_SKILL_ROUTING_3"
    thread_ts = "100000.0003"
    thread_state_manager.clear_state(channel_id, thread_ts)

    set_filter_unauthorized_skill_agents_enabled(False)

    # Initial chain starts with unauthorized agent-10001, followed by authorized agent-10002
    thread_state_manager.set_remaining_agents(channel_id, thread_ts, ["agent-10002"])

    posted_sys_messages = []

    async def mock_post_sys_msg(channel_id, thread_ts, text):
        posted_sys_messages.append(text)
        return {"ok": True}

    pipeline = TurnPipeline(
        post_system_message_fn=mock_post_sys_msg,
        files_fetcher=AsyncMock(return_value=[{"id": "F1", "name": "spec.pdf"}]),
    )

    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="Analiziraj privitak spec.pdf",
        is_bot_trigger=False,
    )
    directives = ParsedDirectives(
        cleaned_prompt="Analiziraj privitak spec.pdf",
        actions=[ActionDirective(name="document_reader", params={"target_file": "spec.pdf"})],
        is_pure_skill_task=True,
    )

    with (
        patch("orchestration.stages.validation.directive_parser.parse", AsyncMock(return_value=directives)),
        patch("orchestration.stages.validation.get_initial_agents", return_value=["agent-10001", "agent-10002"]),
        patch("orchestration.stages.validation.parse_agent_and_prompt", return_value=("agent-10001", "Analiziraj privitak spec.pdf", False)),
        patch("orchestration.stages.validation.download_slack_file", AsyncMock(return_value={"ok": True, "content": "PDF data", "size": 100})),
    ):
        halt = await pipeline.validation_stage.stage_validate_and_parse(ctx)

    assert halt is None
    # Authorized agent-10002 was moved to the front!
    assert ctx.agent_key == "agent-10002"
    # Unauthorized agent-10001 is preserved in remaining so it can debate findings downstream!
    assert thread_state_manager.get_remaining_agents(channel_id, thread_ts) == ["agent-10001"]
    # No filtering notice posted since toggle is OFF
    assert len(posted_sys_messages) == 0


@pytest.mark.anyio
async def test_slack_app_home_governance_block_and_action_handler():
    """Verifies Block Kit UI generation and interactivity action dispatch for skill filter toggle."""
    blocks = build_governance_blocks(
        cb_enabled=True,
        cb_threshold=1.0,
        cb_status_str="ON",
        scribe_on=True,
        scribe_status_str="ON",
        ff_on=True,
        ff_status_str="ON",
        weekly_pricing_on=True,
        weekly_pricing_status_str="ON",
        expiry_hours=4.0,
        skill_filter_on=True,
        skill_filter_status_str="🟢 *Enabled (ON)*",
    )
    # Check block existence
    filter_sections = [b for b in blocks if "skill_filter_toggle_section" in b.get("block_id", "")]
    assert len(filter_sections) == 1
    accessory = filter_sections[0].get("accessory", {})
    assert accessory.get("action_id") == "toggle_skill_filter_radio"
    assert accessory.get("initial_option", {}).get("value") == "skill_filter:on"

    # Test toggling OFF via handler (demoted to WARNING since guardrail is disabled)
    action_payload = {"selected_option": {"value": "skill_filter:off"}}
    bg_tasks = MagicMock()
    with patch("adapters.slack.actions.governance_actions.log_audit_event_async", new_callable=AsyncMock) as mock_audit:
        await _handle_toggle_skill_filter(
            action=action_payload,
            payload={},
            user_id="U_ADMIN",
            background_tasks=bg_tasks,
            view_state={},
        )
        assert is_filter_unauthorized_skill_agents_enabled() is False
        mock_audit.assert_called_once()
        _, kwargs = mock_audit.call_args
        assert kwargs["action"] == "toggle_skill_filter"
        assert kwargs["details"] == {"enabled": False}
        assert kwargs["severity"] == "WARNING"

    # Test toggling ON via handler (demoted to INFO since guardrail is restored)
    action_payload_on = {"selected_option": {"value": "skill_filter:on"}}
    with patch("adapters.slack.actions.governance_actions.log_audit_event_async", new_callable=AsyncMock) as mock_audit_on:
        await _handle_toggle_skill_filter(
            action=action_payload_on,
            payload={},
            user_id="U_ADMIN",
            background_tasks=bg_tasks,
            view_state={},
        )
        assert is_filter_unauthorized_skill_agents_enabled() is True
        mock_audit_on.assert_called_once()
        _, kwargs_on = mock_audit_on.call_args
        assert kwargs_on["action"] == "toggle_skill_filter"
        assert kwargs_on["details"] == {"enabled": True}
        assert kwargs_on["severity"] == "INFO"


@pytest.mark.anyio
async def test_human_prompt_reinitializes_exhausted_empty_remaining_agents_queue():
    """Verifies that a new human prompt clears stale remaining_agents=[] and re-initializes from round_sequence."""
    channel_id = "C_EMPTY_REINIT_1"
    thread_ts = "100000.0010"
    thread_state_manager.clear_state(channel_id, thread_ts)

    # Simulate completed/halted prior state where remaining_agents was left as [] while round_sequence persists
    thread_state_manager.set_round_sequence(channel_id, thread_ts, ["agent-10001", "agent-10002", "agent-10003"])
    thread_state_manager.set_remaining_agents(channel_id, thread_ts, [])

    set_filter_unauthorized_skill_agents_enabled(False)
    posted_sys_messages = []

    async def mock_post_sys_msg(channel_id, thread_ts, text):
        posted_sys_messages.append(text)
        return {"ok": True}

    pipeline = TurnPipeline(
        post_system_message_fn=mock_post_sys_msg,
        files_fetcher=AsyncMock(return_value=[{"id": "F1", "name": "spec.pdf"}]),
    )

    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="From file spec.pdf summarize one point",
        is_bot_trigger=False,
    )
    directives = ParsedDirectives(
        cleaned_prompt="From file spec.pdf summarize one point",
        actions=[ActionDirective(name="document_reader", params={"target_file": "spec.pdf"})],
        detected_language="English",
        is_pure_skill_task=True,
    )

    with (
        patch("orchestration.stages.validation.directive_parser.parse", AsyncMock(return_value=directives)),
        patch("orchestration.stages.validation.download_slack_file", AsyncMock(return_value={"ok": True, "content": "PDF data", "size": 100})),
    ):
        halt = await pipeline.validation_stage.stage_validate_and_parse(ctx)

    assert halt is None
    # Toggle OFF prioritizes authorized agent-10002 first, keeping unauthorized agents for downstream discussion
    assert ctx.agent_key == "agent-10002"
    assert thread_state_manager.get_remaining_agents(channel_id, thread_ts) == ["agent-10001", "agent-10003"]
    assert posted_sys_messages == []


@pytest.mark.anyio
async def test_bot_trigger_with_empty_remaining_agents_still_halts_cleanly():
    """Verifies that internal bot-triggered turns still treat remaining_agents=[] as an exhausted round."""
    channel_id = "C_EMPTY_BOT_1"
    thread_ts = "100000.0011"
    thread_state_manager.clear_state(channel_id, thread_ts)
    thread_state_manager.set_round_sequence(channel_id, thread_ts, ["agent-10001", "agent-10002"])
    thread_state_manager.set_remaining_agents(channel_id, thread_ts, [])

    posted_sys_messages = []

    async def mock_post_sys_msg(channel_id, thread_ts, text):
        posted_sys_messages.append(text)
        return {"ok": True}

    pipeline = TurnPipeline(post_system_message_fn=mock_post_sys_msg)
    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="Final summary",
        is_bot_trigger=True,
    )

    halt = await pipeline.validation_stage.stage_validate_and_parse(ctx)

    assert halt is not None
    assert halt["halted"] is True
    assert halt["reason"] == "no_agents_available"
    assert posted_sys_messages == []


@pytest.mark.anyio
async def test_standalone_revoke_directive_does_not_become_initial_prompt():
    """Verifies standalone slash commands (like /revoke-skill-for-all) are not recorded as initial_prompt."""
    channel_id = "C_REVOKE_INIT_1"
    thread_ts = "100000.0012"
    thread_state_manager.clear_state(channel_id, thread_ts)

    posted_sys_messages = []

    async def mock_post_sys_msg(channel_id, thread_ts, text):
        posted_sys_messages.append(text)
        return {"ok": True}

    pipeline = TurnPipeline(
        post_system_message_fn=mock_post_sys_msg,
        files_fetcher=AsyncMock(return_value=[]),
    )
    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="<@U_BOT> /revoke-skill-for-all document_reader",
        user_id="U_ADMIN",
        is_bot_trigger=False,
    )
    directives = ParsedDirectives(
        cleaned_prompt="<@U_BOT>",
        actions=[ActionDirective(name="revoke_skill_for_all", params={"skills": ["document_reader"]}, origin="slash")],
    )

    with (
        patch("orchestration.stages.validation.directive_parser.parse", AsyncMock(return_value=directives)),
        patch("core.config.is_slack_admin", return_value=True),
    ):
        halt = await pipeline.validation_stage.stage_validate_and_parse(ctx)

    assert halt is not None
    assert halt["halted"] is True
    assert halt["action"] == "revoke_skill_for_all"
    assert thread_state_manager.get_initial_prompt(channel_id, thread_ts) is None
    assert len(posted_sys_messages) == 1


@pytest.mark.anyio
async def test_human_turn_no_agents_available_posts_slack_notice():
    """Verifies human prompts with no eligible agents (e.g., all excluded) emit a Slack notice instead of silent halt."""
    channel_id = "C_NO_AGENT_NOTICE_1"
    thread_ts = "100000.0013"
    thread_state_manager.clear_state(channel_id, thread_ts)
    thread_state_manager.add_excluded_agent(channel_id, thread_ts, "agent-10001")

    posted_sys_messages = []

    async def mock_post_sys_msg(channel_id, thread_ts, text):
        posted_sys_messages.append(text)
        return {"ok": True}

    pipeline = TurnPipeline(
        post_system_message_fn=mock_post_sys_msg,
        files_fetcher=AsyncMock(return_value=[]),
    )
    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="Hello team",
        is_bot_trigger=False,
    )
    directives = ParsedDirectives(cleaned_prompt="Hello team", detected_language="English")

    with (
        patch("orchestration.stages.validation.directive_parser.parse", AsyncMock(return_value=directives)),
        patch("orchestration.stages.validation.get_initial_agents", return_value=["agent-10001"]),
        patch("orchestration.stages.validation.parse_agent_and_prompt", return_value=(None, "Hello team", False)),
    ):
        halt = await pipeline.validation_stage.stage_validate_and_parse(ctx)

    assert halt is not None
    assert halt["halted"] is True
    assert halt["reason"] == "no_agents_available"
    assert len(posted_sys_messages) == 1
    assert "No Agent Available" in posted_sys_messages[0]


@pytest.mark.anyio
async def test_round2_updates_active_prompt_and_hydrates_pure_skill_for_bot_turns():
    """Verifies Round 2 updates active_prompt (preserving initial_prompt) and hydrates is_pure_skill_task on bot turns."""
    channel_id = "C_ACTIVE_PROMPT_1"
    thread_ts = "100000.0020"
    thread_state_manager.clear_state(channel_id, thread_ts)

    # Round 1 recorded initial_prompt
    thread_state_manager.set_initial_prompt(channel_id, thread_ts, "From 01_Maturity.pdf /grant-for-all document_reader")
    thread_state_manager.set_active_prompt(channel_id, thread_ts, "From 01_Maturity.pdf")
    thread_state_manager.set_round_sequence(channel_id, thread_ts, ["agent-10002", "agent-10001"])

    pipeline = TurnPipeline(
        post_system_message_fn=AsyncMock(return_value={"ok": True}),
        files_fetcher=AsyncMock(return_value=[]),
    )

    # Round 2 human prompt arrives
    ctx_r2_t1 = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="<@U_BOT> From 02_Foundation.pdf pick one Foundation",
        is_bot_trigger=False,
    )
    directives_r2 = ParsedDirectives(
        cleaned_prompt="<@U_BOT> From 02_Foundation.pdf pick one Foundation",
        actions=[ActionDirective(name="document_reader", params={"target_file": "02_Foundation.pdf"})],
        detected_language="English",
        is_pure_skill_task=True,
    )
    with (
        patch("orchestration.stages.validation.directive_parser.parse", AsyncMock(return_value=directives_r2)),
        patch("orchestration.stages.validation.resolve_target_file", return_value=None),
    ):
        halt1 = await pipeline.validation_stage.stage_validate_and_parse(ctx_r2_t1)

    assert halt1 is None
    assert thread_state_manager.get_initial_prompt(channel_id, thread_ts) == "From 01_Maturity.pdf /grant-for-all document_reader"
    assert thread_state_manager.get_active_prompt(channel_id, thread_ts) == "<@U_BOT> From 02_Foundation.pdf pick one Foundation"
    assert thread_state_manager.get_is_pure_skill_task(channel_id, thread_ts) is True

    # Chained bot-triggered Turn 2 in Round 2 hydrates is_pure_skill_task and uses active_prompt (not Round 1 initial_prompt)
    ctx_r2_t2 = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="Foundation 2 summary /[agent-10001]",
        is_bot_trigger=True,
    )
    halt2 = await pipeline.validation_stage.stage_validate_and_parse(ctx_r2_t2)
    assert halt2 is None
    assert ctx_r2_t2.is_pure_skill_task is True

    await pipeline.resolution_stage.stage_resolve_agent_and_governance(ctx_r2_t2)

    # Verify ContextPrepStage re-anchored to Round 2's active_prompt when building LLM messages
    ctx_prep_check = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="Foundation 2 summary",
        is_bot_trigger=True,
        agent_key="agent-10001",
        agent_cfg={"username": "Visionary", "skills": [], "model": "gemini/gemini-3.8-flash"},
        in_memory_history=[{"role": "assistant", "content": "[Innovator]: Foundation 2"}],
    )
    with patch("orchestration.stages.context_prep.build_agent_prompt", return_value="System prompt"):
        await pipeline.context_prep_stage.stage_prepare_context(ctx_prep_check)
    last_msg = ctx_prep_check.messages[-1]["content"]
    assert "02_Foundation.pdf" in last_msg
    assert "01_Maturity.pdf" not in last_msg

    # Clearing round execution state clears is_pure_skill_task
    thread_state_manager.clear_round_execution_state(channel_id, thread_ts)
    assert thread_state_manager.get_is_pure_skill_task(channel_id, thread_ts) is False


@pytest.mark.anyio
async def test_execution_short_circuits_missing_skill_pure_task_with_zero_llm_calls():
    """Verifies pure-skill turns lacking required skill short-circuit deterministically (0 LLM tokens) and preserve hint."""
    channel_id = "C_SHORT_CIRCUIT_1"
    thread_ts = "100000.0021"
    thread_state_manager.clear_state(channel_id, thread_ts)
    thread_state_manager.set_max_words(channel_id, thread_ts, 10)

    mock_llm = AsyncMock()
    mock_slack = AsyncMock(return_value={"ok": True, "ts": "100000.0022"})
    pipeline = TurnPipeline(
        llm_executor=mock_llm,
        slack_poster=mock_slack,
        sleep_fn=AsyncMock(),
    )

    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="Read 02_Foundation_Blueprint.pdf",
        is_bot_trigger=True,
        agent_key="agent-10001",
        agent_cfg={"username": "Visionary", "displayname": "Visionary", "skills": [], "model": "gemini/gemini-3.8-flash"},
        is_pure_skill_task=True,
        missing_required_skills=["document_reader"],
        file_context={"file_name": "02_Foundation_Blueprint.pdf", "required_skill": "document_reader", "authorized_agents": None},
        effective_history=[{"role": "assistant", "content": "[Innovator]: Foundation 2\n\nSkills used: [document_reader]"}],
        remaining=["agent-10003"],
        eligible_remaining_agents=["agent-10003"],
        next_designated_agent="agent-10003",
    )

    halt = await pipeline.execution_stage.stage_execute_llm(ctx)
    assert halt is None
    mock_llm.assert_not_called()
    assert ctx.output.is_deterministic_missing_skill is True
    assert ctx.output.total_prompt_tokens == 0
    assert ctx.output.total_completion_tokens == 0
    assert "document_reader" in ctx.output.final_text
    assert "/grant_skill" in ctx.output.final_text
    assert "Recommended agent for 'document_reader':" in ctx.output.final_text
    assert "agent-10002" not in ctx.output.final_text
    assert "agent-10006" not in ctx.output.final_text
    assert "agent-10008" not in ctx.output.final_text
    assert "agent-10016" not in ctx.output.final_text

    # Stage 5 preserves the deterministic notice + /grant_skill hint without truncating at max_words=10
    with patch("orchestration.stages.post_record.trigger_thread_notes_update"), \
         patch("orchestration.stages.post_record.trigger_usage_update"), \
         patch("orchestration.stages.post_record.async_record_flat_history", AsyncMock()):
        res = await pipeline.post_record_stage.stage_post_and_record(ctx)

    assert "/grant_skill" in ctx.output.final_text
    assert "Recommended agent for 'document_reader':" in ctx.output.final_text
    assert "/[agent-10003]" in ctx.output.final_text
    assert res["agent_key"] == "agent-10001"


