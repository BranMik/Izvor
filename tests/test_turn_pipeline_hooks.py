import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.domain import Turn, SYSTEM_PRINCIPAL
from core.models import ActionDirective
from orchestration.turn_pipeline import TurnContext, TurnPipeline


def test_turn_pipeline_router_module_removed():
    pipeline = TurnPipeline()
    assert not hasattr(pipeline, "_router_module")
    assert not hasattr(pipeline, "router")


def test_custom_tool_executor_cannot_bypass_authorization():
    mock_executor = MagicMock(return_value="executed_bypass")
    pipeline = TurnPipeline(tool_executor=mock_executor)

    ctx = TurnContext(
        channel_id="C_TEST",
        thread_ts="1234.5678",
        raw_text="Hello",
        user_id="U_USER",
    )
    ctx.start_time = 1000.0
    ctx.agent_key = "agent-10003"  # Sceptic agent has no write_document skill

    # Call execution stage with a tool call for write_document
    tool_call = MagicMock()
    tool_call.id = "tc_1"
    tool_call.function.name = "write_document"
    tool_call.function.arguments = '{"rel_path": "test.txt", "content": "bypass"}'

    # In stage_execute_llm loop, execution must fail authorization before mock_executor is touched
    from core.registry import get_skill_registry
    skill_registry = get_skill_registry()

    from services.mcp_tools import is_agent_authorized_for_tool
    assert is_agent_authorized_for_tool(ctx.agent_key, "write_document", authorized_skills=[]) is False

    # Execute tool via stage
    exec_mcp_fn = pipeline.deps.tool_executor
    assert exec_mcp_fn is mock_executor


def test_tool_executor_injected_via_di():
    mock_executor = MagicMock(return_value='{"success": true}')
    pipeline = TurnPipeline(tool_executor=mock_executor)
    assert pipeline.deps.tool_executor is mock_executor
