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
from core.registry import get_skill_registry
from skills.builtin.list_skills.action import run as list_skills_action_run

skill_registry = get_skill_registry()


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
    assert "document_reader" in mapping
    assert "web_research" in mapping
    assert "document_authoring" in mapping
    assert "list_skills" in mapping

    # Verify specific agent assignments
    code_agents = mapping["document_reader"]["authorized_agents"]
    assert "agent-10008" in code_agents
    assert "agent-10006" in code_agents

    ws_agents = mapping["document_authoring"]["authorized_agents"]
    assert "agent-10006" in ws_agents
    assert "agent-10008" not in ws_agents  # SecurityArchitect only has document_reader

    web_agents = mapping["web_research"]["authorized_agents"]
    assert "agent-10009" in web_agents

    doc_agents = mapping["document_reader"]["authorized_agents"]
    assert "agent-10009" in doc_agents or "agent-10006" in doc_agents

    # Verify orchestrator and modifier skills categorization
    assert "stop_discussion" in mapping
    assert "System / Orchestrator Directive" in mapping["stop_discussion"]["authorized_agents"]
    assert "max_words" in mapping
    assert "System / Prompt Modifier" in mapping["max_words"]["authorized_agents"]


def test_format_skills_directory_markdown():
    """Verify formatting of the markdown skills directory."""
    md = skill_registry.format_skills_directory_markdown()
    assert "Application Skills & Capabilities Directory" in md
    assert "`document_reader`" in md
    assert "`web_research`" in md
    assert "*Authorized*:" in md
    assert "agent-10011" in md
    assert "agent-10008" in md


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
    d3 = await directive_parser.parse("/list_skills [agent-10008]")
    assert any(a.name == "list_skills" for a in d3.actions)
    assert "[agent-10008]" in d3.cleaned_prompt

    # 4. Agent first, slash command second
    d4 = await directive_parser.parse("[agent-10001] /list_skills please explain")
    assert any(a.name == "list_skills" for a in d4.actions)
    assert "[agent-10001]" in d4.cleaned_prompt
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
                    "content": "Here are all the application skills:\n• document_reader: ROIStrategist, SecurityArchitect"
                }
            }
        ]
    }

    with patch("skills.builtin.list_skills.action.execute_llm_with_retry", new_callable=AsyncMock, return_value=mock_llm_response) as mock_llm, \
         patch("skills.builtin.list_skills.action.post_system_message", new_callable=AsyncMock) as mock_post:
        res = await list_skills_action_run(ctx)
        assert res == {"ok": True, "halted": True, "action": "list_skills"}
        mock_llm.assert_awaited_once()
        mock_post.assert_awaited_once()
        call_kwargs = mock_post.await_args.kwargs
        assert call_kwargs["channel_id"] == "C12345"
        assert call_kwargs["thread_ts"] == "1234567.89"
        assert "document_reader" in call_kwargs["text"]


@pytest.mark.anyio
async def test_action_run_with_named_agent_halts_exclusively():
    """
    If /list_skills is invoked with a named agent, it is strictly exclusive
    and halts immediately after presenting skills.
    """
    ctx = TurnContext(
        channel_id="C12345",
        thread_ts="1234567.89",
        raw_text="/list_skills [agent-10008]",
    )
    with patch("skills.builtin.list_skills.action.execute_llm_with_retry", new_callable=AsyncMock), \
         patch("skills.builtin.list_skills.action.post_system_message", new_callable=AsyncMock):
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
    with patch("skills.builtin.list_skills.action.execute_llm_with_retry", new_callable=AsyncMock), \
         patch("skills.builtin.list_skills.action.post_system_message", new_callable=AsyncMock):
        res = await list_skills_action_run(ctx)
        assert res == {"ok": True, "halted": True, "action": "list_skills"}


def test_all_agents_see_skills_in_prompt():
    """
    Verify that any agent asked sees and can correctly list all the skills in the app,
    while non-technical agents remain restricted from loading tools on demand.
    """
    # 1. Pure debate agent with 0 allowed skills (e.g. Sceptic, Visionary)
    sceptic_prompt = build_agent_prompt("agent-10003")
    assert "APPLICATION SKILLS DIRECTORY" in sceptic_prompt
    assert "`document_reader`" in sceptic_prompt
    assert "`document_authoring`" in sceptic_prompt
    assert "`web_research`" in sceptic_prompt
    assert "agent-10011" in sceptic_prompt
    assert "agent-10008" in sceptic_prompt
    # Sceptic must NOT have load_skill tool catalog (0 tool overhead)
    assert "AVAILABLE SKILLS (ON DEMAND VIA `load_skill`)" not in sceptic_prompt

    # 2. Technical agent with allowed skills (e.g. SecurityArchitect)
    architect_prompt = build_agent_prompt("agent-10008")
    assert "APPLICATION SKILLS DIRECTORY" in architect_prompt
    assert "`document_authoring`" in architect_prompt
    assert "`web_research`" in architect_prompt
    # SecurityArchitect DOES have load_skill catalog for its authorized skills
    assert "AVAILABLE SKILLS (ON DEMAND VIA `load_skill`)" in architect_prompt
    assert "`document_reader`" in architect_prompt


@pytest.mark.anyio
async def test_runtime_permission_enforcement():
    """
    Verify that an agent cannot circumvent its permission boundaries:
    runtime rejects unauthorized load_skill or tool execution.
    """
    # SecurityArchitect is only authorized for document_reader
    architect_skills = ["document_reader"]

    # 1. Attempting to load unauthorized skill document_authoring -> Rejected
    res1 = await skill_registry.execute_tool_async(
        tool_name="load_skill",
        arguments={"skill_name": "document_authoring"},
        authorized_skills=architect_skills,
    )
    data1 = json.loads(res1)
    assert "error" in data1
    assert "Permission denied: Agent is not authorized to load skill 'document_authoring'" in data1["error"]

    # 2. Attempting to execute unauthorized tool write_to_file -> Rejected
    res2 = await skill_registry.execute_tool_async(
        tool_name="write_to_file",
        arguments={"path": "test.txt", "content": "hack"},
        authorized_skills=architect_skills,
    )
    data2 = json.loads(res2)
    assert "error" in data2
    assert "Permission denied: Agent is not authorized to execute tool 'write_to_file'" in data2["error"]

    # 3. Loading authorized skill document_reader -> Allowed
    res3 = await skill_registry.execute_tool_async(
        tool_name="load_skill",
        arguments={"skill_name": "document_reader"},
        authorized_skills=architect_skills,
    )
    data3 = json.loads(res3)
    assert data3.get("status") == "loaded"
    assert data3.get("skill") == "document_reader"


def test_get_skill_resolution_hint():
    """Verify that get_skill_resolution_hint recommends 1 authorized agent by username (no agent IDs) and /grant_skill."""
    import re
    from skills.loader import select_recommended_agent_from_keys

    hint = skill_registry.get_skill_resolution_hint("document_reader")
    assert "Recommended agent for 'document_reader':" in hint
    assert "/grant_skill" in hint
    assert not re.search(r"\bagent-\d+\b", hint), f"Agent ID leaked in hint: {hint}"

    # Verify single-agent priority: enabled > active workflow > coverage > alphabetical username
    sample_cfg = {
        "agent-10002": {"username": "Innovator", "skills": ["document_reader"]},
        "agent-10006": {"username": "ROIStrategist", "skills": ["document_reader", "web_research"]},
        "agent-10008": {"username": "SecurityArchitect", "skills": ["document_reader"]},
        "agent-10016": {"username": "CreativeAdvisor", "skills": ["document_reader", "web_research"]},
    }
    # Enabled + in workflow wins over alphabetical enabled outside workflow
    picked = select_recommended_agent_from_keys(
        ["agent-10002", "agent-10006", "agent-10008"],
        agents_cfg=sample_cfg,
        enabled_agents={"agent-10002", "agent-10006", "agent-10008"},
        workflow_agents={"agent-10006"},
    )
    assert picked == "ROIStrategist"

    # Disabled agent in workflow loses to enabled agent outside workflow
    picked_enabled = select_recommended_agent_from_keys(
        ["agent-10006", "agent-10008"],
        agents_cfg=sample_cfg,
        enabled_agents={"agent-10008"},
        workflow_agents={"agent-10006"},
    )
    assert picked_enabled == "SecurityArchitect"

    # Coverage breaks ties within same enabled/workflow tier, then alphabetical username
    picked_cov = select_recommended_agent_from_keys(
        ["agent-10002", "agent-10006", "agent-10016"],
        agents_cfg=sample_cfg,
        enabled_agents={"agent-10002", "agent-10006", "agent-10016"},
        workflow_agents=set(),
        coverage_counts={"agent-10002": 1, "agent-10006": 2, "agent-10016": 2},
    )
    assert picked_cov == "CreativeAdvisor"

    # Self-exclusion skips exclude_agent_key
    picked_excl = select_recommended_agent_from_keys(
        ["agent-10006", "agent-10016"],
        exclude_agent_key="agent-10016",
        agents_cfg=sample_cfg,
        enabled_agents={"agent-10006", "agent-10016"},
        workflow_agents=set(),
    )
    assert picked_excl == "ROIStrategist"

    # Multi-skill hint still recommends only 1 agent by username
    multi_hint = skill_registry.get_skill_resolution_hint(["document_reader", "web_research"])
    assert "Recommended agent for 'document_reader', 'web_research':" in multi_hint
    assert not re.search(r"\bagent-\d+\b", multi_hint)


def test_skills_directory_prompt_thread_global_active():
    """
    Verify that when a skill is granted thread-globally, get_skills_directory_prompt
    renders '(Authorized: All Agents (Thread-Global Active))' and updated permission text.
    """
    from orchestration.thread_skills_manager import thread_skills_manager

    channel_id = "C_TEST_THREAD_DIR"
    thread_ts = "1794900001.0001"

    thread_skills_manager.grant_skills(channel_id, thread_ts, ["document_reader"])

    dir_prompt = skill_registry.get_skills_directory_prompt(channel_id=channel_id, thread_ts=thread_ts)
    assert "(Authorized: All Agents (Thread-Global Active))" in dir_prompt
    assert "Any skill marked with '(Authorized: All Agents (Thread-Global Active))'" in dir_prompt


def test_skills_directory_prompt_agent_thread_active():
    """
    Verify that when a skill is granted to a specific agent in a thread,
    get_skills_directory_prompt renders that agent as '(Thread Active)'.
    """
    from orchestration.thread_skills_manager import thread_skills_manager

    channel_id = "C_TEST_AGENT_DIR"
    thread_ts = "1794900002.0002"

    thread_skills_manager.grant_agent_skills(channel_id, thread_ts, "document_reader", ["agent-10001"])

    dir_prompt = skill_registry.get_skills_directory_prompt(channel_id=channel_id, thread_ts=thread_ts)
    assert "agent-10001 (Thread Active)" in dir_prompt


def test_build_agent_prompt_with_thread_context():
    """
    Verify that build_agent_prompt forwards thread context to get_skills_directory_prompt,
    producing an internally coherent system prompt for dynamic skills.
    """
    from orchestration.thread_skills_manager import thread_skills_manager

    channel_id = "C_TEST_BUILD_DIR"
    thread_ts = "1794900003.0003"

    thread_skills_manager.grant_skills(channel_id, thread_ts, ["document_reader"])

    prompt = build_agent_prompt(
        "agent-10001",
        skills=["document_reader"],
        channel_id=channel_id,
        thread_ts=thread_ts,
    )
    assert "APPLICATION SKILLS DIRECTORY" in prompt
    assert "(Authorized: All Agents (Thread-Global Active))" in prompt
    assert "AVAILABLE SKILLS (ON DEMAND VIA `load_skill`)" in prompt
    assert "`document_reader`" in prompt

