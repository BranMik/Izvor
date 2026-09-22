"""
Unit and integration tests for list_skills skill, /list_skills slash command,
system model execution on empty query, dynamic skill-agent mapping,
and permission boundary enforcement.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from core.models import ActionDirective
from orchestration.turn_pipeline import TurnContext
from pipeline.directive_parser import directive_parser
from prompts.builder import build_agent_prompt
from skills import skill_registry
from skills.list_skills.action import run as list_skills_action_run


@pytest.fixture(autouse=True)
def reload_skills():
    skill_registry.reload()


def test_list_skills_registered():
    """Verify that list_skills is properly discovered and registered in SkillRegistry."""
    skill_def = skill_registry.get("list_skills")
    assert skill_def is not None
    assert skill_def.name == "list_skills"
    assert skill_def.display_name == "List Skills"
    assert "/list_skills" in skill_def.triggers.get("slash", [])
    assert "/list-skills" in skill_def.triggers.get("slash", [])
    assert skill_def.triggers.get("auto_detect") is False
    assert skill_def.action_handler is not None


def test_skill_agent_mapping():
    """Verify that every skill is mapped to its authorized agents."""
    mapping = skill_registry.get_skill_agent_mapping()
    assert "codebase_inspection" in mapping
    assert "workspace_authoring" in mapping
    assert "web_research" in mapping
    assert "document_reader" in mapping
    assert "document_authoring" in mapping
    assert "list_skills" in mapping

    # Verify specific agent assignments
    code_agents = mapping["codebase_inspection"]["authorized_agents"]
    assert "Architect" in code_agents
    assert "TechLead" in code_agents

    ws_agents = mapping["workspace_authoring"]["authorized_agents"]
    assert "TechLead" in ws_agents
    assert "Architect" not in ws_agents  # Architect only has codebase_inspection

    web_agents = mapping["web_research"]["authorized_agents"]
    assert "MarketAndCompetitionResearchExpert" in web_agents

    doc_agents = mapping["document_reader"]["authorized_agents"]
    assert "DataAndAnalyticsStrategist" in doc_agents or "ROIStrategist" in doc_agents

    # Verify orchestrator and modifier skills categorization
    assert "stop_discussion" in mapping
    assert "System / Orchestrator Directive" in mapping["stop_discussion"]["authorized_agents"]
    assert "max_words" in mapping
    assert "System / Prompt Modifier" in mapping["max_words"]["authorized_agents"]


def test_format_skills_directory_markdown():
    """Verify formatting of the markdown skills directory."""
    md = skill_registry.format_skills_directory_markdown()
    assert "Application Skills & Capabilities Directory" in md
    assert "`codebase_inspection`" in md
    assert "`workspace_authoring`" in md
    assert "`web_research`" in md
    assert "*Authorized*:" in md
    assert "TechLead" in md
    assert "Architect" in md


@pytest.mark.anyio
async def test_directive_parser_slash_list_skills():
    """Verify directive parser handles /list_skills variants without consuming tags or prompts."""
    # 1. Plain /list_skills
    d1 = await directive_parser.parse("/list_skills")
    assert any(a.name == "list_skills" for a in d1.actions)
    assert d1.cleaned_prompt == ""

    # 2. Alias /list-skills
    d2 = await directive_parser.parse("/list-skills")
    assert any(a.name == "list_skills" for a in d2.actions)
    assert d2.cleaned_prompt == ""

    # 3. With tagged agent
    d3 = await directive_parser.parse("/list_skills [Architect]")
    assert any(a.name == "list_skills" for a in d3.actions)
    assert "[Architect]" in d3.cleaned_prompt

    # 4. Agent first, slash command second
    d4 = await directive_parser.parse("[Visionary] /list_skills please explain")
    assert any(a.name == "list_skills" for a in d4.actions)
    assert "[Visionary]" in d4.cleaned_prompt
    assert "please explain" in d4.cleaned_prompt


@pytest.mark.anyio
async def test_action_run_no_agent_no_text_halts():
    """
    If /list_skills is invoked without an agent and without additional text,
    it must use the system model to list all skills, post a system message, and halt the turn.
    """
    ctx = TurnContext(
        channel_id="C12345",
        thread_ts="1234567.89",
        raw_text="/list_skills",
    )
    ctx.raw_text_cleaned = ""
    ctx.actions = [ActionDirective(name="list_skills")]

    mock_llm_response = {
        "choices": [
            {
                "message": {
                    "content": "Here are all the application skills:\n• codebase_inspection: TechLead, Architect"
                }
            }
        ]
    }

    with patch("skills.list_skills.action.execute_llm_with_retry", new_callable=AsyncMock, return_value=mock_llm_response) as mock_llm, \
         patch("skills.list_skills.action.post_system_message", new_callable=AsyncMock) as mock_post:
        res = await list_skills_action_run(ctx)
        assert res == {"ok": True, "halted": True, "action": "list_skills"}
        mock_llm.assert_awaited_once()
        mock_post.assert_awaited_once()
        call_kwargs = mock_post.await_args.kwargs
        assert call_kwargs["channel_id"] == "C12345"
        assert call_kwargs["thread_ts"] == "1234567.89"
        assert "codebase_inspection" in call_kwargs["text"]


@pytest.mark.anyio
async def test_action_run_with_named_agent_halts_exclusively():
    """
    If /list_skills is invoked with a named agent, it is strictly exclusive
    and halts immediately after presenting skills.
    """
    ctx = TurnContext(
        channel_id="C12345",
        thread_ts="1234567.89",
        raw_text="/list_skills [Architect]",
    )
    with patch("skills.list_skills.action.execute_llm_with_retry", new_callable=AsyncMock), \
         patch("skills.list_skills.action.post_system_message", new_callable=AsyncMock):
        res = await list_skills_action_run(ctx)
        assert res == {"ok": True, "halted": True, "action": "list_skills"}


@pytest.mark.anyio
async def test_action_run_with_additional_text_halts_exclusively():
    """
    If /list_skills is invoked with additional prompt text, it is strictly exclusive
    and halts immediately after presenting skills.
    """
    ctx = TurnContext(
        channel_id="C12345",
        thread_ts="1234567.89",
        raw_text="/list_skills which agent should I use for code reviews?",
    )
    with patch("skills.list_skills.action.execute_llm_with_retry", new_callable=AsyncMock), \
         patch("skills.list_skills.action.post_system_message", new_callable=AsyncMock):
        res = await list_skills_action_run(ctx)
        assert res == {"ok": True, "halted": True, "action": "list_skills"}


def test_all_agents_see_skills_in_prompt():
    """
    Verify that any agent asked sees and can correctly list all the skills in the app,
    while non-technical agents remain restricted from loading tools on demand.
    """
    # 1. Pure debate agent with 0 allowed skills (e.g. Sceptic, Visionary)
    sceptic_prompt = build_agent_prompt("Sceptic")
    assert "APPLICATION SKILLS DIRECTORY" in sceptic_prompt
    assert "`codebase_inspection`" in sceptic_prompt
    assert "`workspace_authoring`" in sceptic_prompt
    assert "`web_research`" in sceptic_prompt
    assert "TechLead" in sceptic_prompt
    assert "Architect" in sceptic_prompt
    # Sceptic must NOT have load_skill tool catalog (0 tool overhead)
    assert "AVAILABLE SKILLS (ON DEMAND VIA `load_skill`)" not in sceptic_prompt

    # 2. Technical agent with allowed skills (e.g. Architect)
    architect_prompt = build_agent_prompt("Architect")
    assert "APPLICATION SKILLS DIRECTORY" in architect_prompt
    assert "`workspace_authoring`" in architect_prompt
    assert "`web_research`" in architect_prompt
    # Architect DOES have load_skill catalog for its authorized skills
    assert "AVAILABLE SKILLS (ON DEMAND VIA `load_skill`)" in architect_prompt
    assert "`codebase_inspection`" in architect_prompt


def test_runtime_permission_enforcement():
    """
    Verify that an agent cannot circumvent its permission boundaries:
    runtime rejects unauthorized load_skill or tool execution.
    """
    # Architect is only authorized for codebase_inspection
    architect_skills = ["codebase_inspection"]

    # 1. Attempting to load unauthorized skill workspace_authoring -> Rejected
    res1 = skill_registry.execute_tool(
        tool_name="load_skill",
        arguments={"skill_name": "workspace_authoring"},
        authorized_skills=architect_skills,
    )
    data1 = json.loads(res1)
    assert "error" in data1
    assert "Permission denied: Agent is not authorized to load skill 'workspace_authoring'" in data1["error"]

    # 2. Attempting to execute unauthorized tool write_to_file -> Rejected
    res2 = skill_registry.execute_tool(
        tool_name="write_to_file",
        arguments={"path": "test.txt", "content": "hack"},
        authorized_skills=architect_skills,
    )
    data2 = json.loads(res2)
    assert "error" in data2
    assert "Permission denied: Agent is not authorized to execute tool 'write_to_file'" in data2["error"]

    # 3. Loading authorized skill codebase_inspection -> Allowed
    res3 = skill_registry.execute_tool(
        tool_name="load_skill",
        arguments={"skill_name": "codebase_inspection"},
        authorized_skills=architect_skills,
    )
    data3 = json.loads(res3)
    assert data3.get("status") == "loaded"
    assert data3.get("skill") == "codebase_inspection"
