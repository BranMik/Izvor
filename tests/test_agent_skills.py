"""
Comprehensive test suite for the Declarative SKILL.md Framework and LLM Progressive Disclosure.
"""

import json
from unittest.mock import patch

from orchestration.agent_settings import settings_manager
from prompts import build_agent_prompt
from skills import (
    LOAD_SKILL_SCHEMA,
    skill_registry,
)

# ==============================================================================
# 1. Declarative SKILL.md Parsing & Discovery Tests
# ==============================================================================

def test_skill_md_discovery_and_frontmatter_parsing():
    """Verifies that all SKILL.md files are correctly discovered and parsed."""
    skills = skill_registry.list_skills()
    skill_names = {s.name for s in skills}

    assert "web_research" in skill_names
    assert "codebase_inspection" in skill_names
    assert "workspace_authoring" in skill_names
    assert "document_reader" in skill_names
    assert "document_authoring" in skill_names


def test_web_research_skill_definition():
    skill = skill_registry.get("web_research")
    assert skill is not None
    assert skill.name == "web_research"
    assert skill.display_name == "Web Research"
    assert "web" in skill.description.lower()
    assert set(skill.tools) == {"web_search", "fetch_web_page"}

    # Verify markdown instructions body
    assert "# Web Research Skill" in skill.instructions
    assert "Operational Guidelines" in skill.instructions

    # Verify LiteLLM tool schemas
    schemas = skill.get_tool_schemas()
    assert len(schemas) == 2
    schema_names = {s["function"]["name"] for s in schemas}
    assert schema_names == {"web_search", "fetch_web_page"}


def test_codebase_inspection_skill_definition():
    skill = skill_registry.get("codebase_inspection")
    assert skill is not None
    assert skill.name == "codebase_inspection"
    assert set(skill.tools) == {"list_directory", "read_file"}
    assert "# Codebase Inspection Skill" in skill.instructions

    schemas = skill.get_tool_schemas()
    assert len(schemas) == 2
    schema_names = {s["function"]["name"] for s in schemas}
    assert schema_names == {"list_directory", "read_file"}


def test_workspace_authoring_skill_definition():
    skill = skill_registry.get("workspace_authoring")
    assert skill is not None
    assert skill.name == "workspace_authoring"
    assert skill.tools == ["write_file"]
    assert "# Workspace Authoring Skill" in skill.instructions

    schemas = skill.get_tool_schemas()
    assert len(schemas) == 1
    assert schemas[0]["function"]["name"] == "write_file"


def test_document_reader_skill_definition():
    skill = skill_registry.get("document_reader")
    assert skill is not None
    assert skill.name == "document_reader"
    assert skill.display_name == "Document Reader"
    assert "resources" in skill.description.lower()
    assert set(skill.tools) == {"list_documents", "read_document"}
    assert "# Document Reader Skill" in skill.instructions

    schemas = skill.get_tool_schemas()
    assert len(schemas) == 2
    schema_names = {s["function"]["name"] for s in schemas}
    assert schema_names == {"list_documents", "read_document"}


def test_document_authoring_skill_definition():
    skill = skill_registry.get("document_authoring")
    assert skill is not None
    assert skill.name == "document_authoring"
    assert skill.display_name == "Document Authoring"
    assert "resources" in skill.description.lower()
    assert skill.tools == ["write_document"]
    assert "# Document Authoring Skill" in skill.instructions

    schemas = skill.get_tool_schemas()
    assert len(schemas) == 1
    assert schemas[0]["function"]["name"] == "write_document"



# ==============================================================================
# 2. Progressive Disclosure & load_skill Execution Tests
# ==============================================================================

def test_load_skill_schema():
    assert LOAD_SKILL_SCHEMA["type"] == "function"
    assert LOAD_SKILL_SCHEMA["function"]["name"] == "load_skill"
    assert "skill_name" in LOAD_SKILL_SCHEMA["function"]["parameters"]["properties"]


def test_execute_load_skill_valid():
    raw_res = skill_registry.execute_tool("load_skill", {"skill_name": "web_research"})
    data = json.loads(raw_res)

    assert data["status"] == "loaded"
    assert data["skill"] == "web_research"
    assert "web_search" in data["unlocked_tools"]
    assert "operational_guidelines" in data
    assert "# Web Research Skill" in data["operational_guidelines"]


def test_execute_load_skill_authorization():
    # Authorized case
    raw_res = skill_registry.execute_tool(
        "load_skill", {"skill_name": "web_research"}, authorized_skills=["web_research"]
    )
    data = json.loads(raw_res)
    assert data["status"] == "loaded"
    assert data["skill"] == "web_research"

    # Unauthorized case: Sceptic has [], tries to load web_research
    denied_res = skill_registry.execute_tool(
        "load_skill", {"skill_name": "web_research"}, authorized_skills=[]
    )
    denied_data = json.loads(denied_res)
    assert "error" in denied_data
    assert "Permission denied" in denied_data["error"]

    # Unauthorized case: TechLead has ['codebase_inspection'], tries to load web_research
    tl_denied = skill_registry.execute_tool(
        "load_skill", {"skill_name": "web_research"}, authorized_skills=["codebase_inspection"]
    )
    tl_data = json.loads(tl_denied)
    assert "Permission denied" in tl_data["error"]


def test_execute_load_skill_invalid():
    raw_res = skill_registry.execute_tool("load_skill", {"skill_name": "teleportation"}, authorized_skills=["teleportation"])
    data = json.loads(raw_res)

    assert "error" in data
    assert "not found" in data["error"]


def test_execute_delegated_mcp_tool():
    with patch("skills.loader.execute_mcp_tool") as mock_exec:
        mock_exec.return_value = json.dumps({"contents": []})
        res = skill_registry.execute_tool("list_directory", {"rel_path": "."})
        assert "contents" in res
        mock_exec.assert_called_once_with("list_directory", {"rel_path": "."})


def test_execute_tool_authorization_enforcement():
    # Authorized: web_research allows web_search
    with patch("skills.loader.execute_mcp_tool") as mock_exec:
        mock_exec.return_value = json.dumps({"results": ["ok"]})
        res = skill_registry.execute_tool(
            "web_search", {"query": "AI"}, authorized_skills=["web_research"]
        )
        assert json.loads(res) == {"results": ["ok"]}
        mock_exec.assert_called_once()

    # Unauthorized tool: web_research does NOT allow read_file
    denied_res = skill_registry.execute_tool(
        "read_file", {"file_path": "secret.txt"}, authorized_skills=["web_research"]
    )
    denied_data = json.loads(denied_res)
    assert "error" in denied_data
    assert "Permission denied: Agent is not authorized to execute tool 'read_file'" in denied_data["error"]
    assert "'web_search'" in denied_data["error"]

    # Zero skills agent: no tools authorized at all
    zero_res = skill_registry.execute_tool(
        "read_file", {"file_path": "test.txt"}, authorized_skills=[]
    )
    zero_data = json.loads(zero_res)
    assert "Permission denied: Agent is not authorized to execute tool 'read_file'" in zero_data["error"]
    assert "Permitted tools: none" in zero_data["error"]



# ==============================================================================
# 3. Prompt Builder Skills Catalog Authorization Tests
# ==============================================================================

def test_skills_catalog_prompt_filtering():
    # When no allowed skills specified, all skills shown
    full_catalog = skill_registry.get_skills_catalog_prompt()
    assert "web_research" in full_catalog
    assert "codebase_inspection" in full_catalog

    # When restricted to web_research only
    web_only = skill_registry.get_skills_catalog_prompt(allowed_skills=["web_research"])
    assert "web_research" in web_only
    assert "codebase_inspection" not in web_only

    # When zero skills allowed (pure debate persona), empty string returned
    empty_catalog = skill_registry.get_skills_catalog_prompt(allowed_skills=[])
    assert empty_catalog == ""


def test_prompt_builder_strict_role_authorization():
    # Pure debate agent (Sceptic) has [] skills in agent_settings.json:
    # Must NOT have any skills catalog injected (zero tool overhead)
    sceptic_prompt = build_agent_prompt("Sceptic")
    assert "AVAILABLE SKILLS (ON DEMAND VIA `load_skill`)" not in sceptic_prompt

    # Research expert has ['web_research']:
    # Should contain web_research, but NOT codebase_inspection or workspace_authoring
    market_prompt = build_agent_prompt("MarketAndCompetitionResearchExpert")
    assert "AVAILABLE SKILLS (ON DEMAND VIA `load_skill`)" in market_prompt
    assert "`web_research`" in market_prompt
    assert "`workspace_authoring`" not in market_prompt

    # TechLead has ['codebase_inspection', 'workspace_authoring']:
    # Should contain codebase_inspection and workspace_authoring, but NOT web_research
    tech_prompt = build_agent_prompt("TechLead")
    assert "AVAILABLE SKILLS (ON DEMAND VIA `load_skill`)" in tech_prompt
    assert "`codebase_inspection`" in tech_prompt
    assert "`workspace_authoring`" in tech_prompt
    assert "`web_research`" not in tech_prompt


# ==============================================================================
# 4. Turn Pipeline Progressive Tool Unlocking Simulation
# ==============================================================================

def test_skill_loader_tool_aggregation():
    # Request schemas for web_research
    web_tools = skill_registry.get_tools_for_skills(["web_research"])
    assert len(web_tools) == 2

    # Request schemas for codebase_inspection and workspace_authoring
    author_tools = skill_registry.get_tools_for_skills(["codebase_inspection", "workspace_authoring"])
    assert len(author_tools) == 3

    # Unknown skill ignored gracefully
    assert skill_registry.get_tools_for_skills(["unknown_skill"]) == []
    assert skill_registry.get_tools_for_skills(None) == []


def test_pipeline_tools_initialization_and_progressive_unlocking():
    # 1. Zero-skills persona (Sceptic)
    sceptic_skills = settings_manager.get_agent_skills("Sceptic")
    assert sceptic_skills == []
    sceptic_active_tools = []
    if sceptic_skills:
        sceptic_active_tools = [LOAD_SKILL_SCHEMA]
    assert sceptic_active_tools == []

    # 2. Skilled agent (MarketAndCompetitionResearchExpert)
    market_skills = settings_manager.get_agent_skills("MarketAndCompetitionResearchExpert")
    assert market_skills == ["web_research"]

    # Initial turn: only LOAD_SKILL_SCHEMA is provided
    market_active_tools = []
    if market_skills:
        market_active_tools = [LOAD_SKILL_SCHEMA]
    assert market_active_tools == [LOAD_SKILL_SCHEMA]

    # Authorized load_skill call unlocks only permitted tools
    load_res = skill_registry.execute_tool(
        "load_skill", {"skill_name": "web_research"}, authorized_skills=market_skills
    )
    load_data = json.loads(load_res)
    assert load_data["status"] == "loaded"
    skill_def = skill_registry.get("web_research")
    for s_schema in skill_def.get_tool_schemas():
        if s_schema not in market_active_tools:
            market_active_tools.append(s_schema)

    unlocked_names = {t["function"]["name"] for t in market_active_tools}
    assert unlocked_names == {"load_skill", "web_search", "fetch_web_page"}

    # Unauthorized load_skill call rejects and does NOT unlock forbidden tools
    unauth_res = skill_registry.execute_tool(
        "load_skill", {"skill_name": "codebase_inspection"}, authorized_skills=market_skills
    )
    unauth_data = json.loads(unauth_res)
    assert "Permission denied" in unauth_data["error"]
    if "codebase_inspection" in market_skills:
        cb_def = skill_registry.get("codebase_inspection")
        for s_schema in cb_def.get_tool_schemas():
            market_active_tools.append(s_schema)

    # list_directory and read_file should NOT be in market_active_tools
    final_names = {t["function"]["name"] for t in market_active_tools}
    assert "list_directory" not in final_names
    assert "read_file" not in final_names

