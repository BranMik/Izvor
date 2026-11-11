import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from adapters.slack.actions.workflow_actions import _handle_toggle_skill_filter
from core.models import ActionDirective, ExtractedIntent
from pipeline.directive_parser.parser import ParsedDirectives
from orchestration.app_settings import (
    is_filter_unauthorized_skill_agents_enabled,
    set_filter_unauthorized_skill_agents_enabled,
)
from orchestration.stages.validation import ValidationStage
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
    with patch("adapters.slack.actions.workflow_actions.log_audit_event") as mock_audit:
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
    with patch("adapters.slack.actions.workflow_actions.log_audit_event") as mock_audit_on:
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
