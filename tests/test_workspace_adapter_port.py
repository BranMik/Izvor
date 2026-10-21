import pytest
from adapters.slack.workspace_adapter import InMemoryWorkspaceAdapter, SlackWorkspaceAdapter
from orchestration.turn_pipeline import TurnContext, TurnPipeline
from orchestration.protocols import WorkspaceAdapter


def test_workspace_adapter_protocols_runtime_checkable():
    in_memory = InMemoryWorkspaceAdapter()
    slack_adapter = SlackWorkspaceAdapter()
    assert isinstance(in_memory, WorkspaceAdapter)
    assert isinstance(slack_adapter, WorkspaceAdapter)


def test_in_memory_workspace_adapter_format_and_blocks():
    adapter = InMemoryWorkspaceAdapter()
    text = "**Bold** and _italic_"
    assert adapter.format_outbound_text(text) == text

    blocks = adapter.build_budget_halt_blocks("Halt", "ch1:t1")
    assert isinstance(blocks, list)
    assert blocks[0]["session_id"] == "ch1:t1"


@pytest.mark.anyio
async def test_turn_pipeline_with_in_memory_workspace_adapter():
    adapter = InMemoryWorkspaceAdapter()
    pipeline = TurnPipeline(workspace_adapter=adapter)

    # Post turn via adapter directly
    res = await adapter.post_agent_turn(
        channel_id="C_TEST",
        thread_ts="1234.5678",
        text="Hello from agent",
        username="TestAgent",
    )
    assert res["ok"] is True
    assert len(adapter.posted_turns) == 1
    assert adapter.posted_turns[0]["username"] == "TestAgent"

    # Update turn via adapter
    up_res = await adapter.update_agent_turn(
        channel_id="C_TEST",
        message_ts=res["ts"],
        text="Updated agent response",
    )
    assert up_res["ok"] is True
    assert len(adapter.updated_turns) == 1
    assert adapter.updated_turns[0]["text"] == "Updated agent response"
