"""
Unit and integration tests for the Unified Skills SecurityArchitecture and Multimodal Data Contracts.
"""

from unittest.mock import AsyncMock, patch

import pytest

from core.models import (
    ActionDirective,
    AttachmentType,
    ExtractedIntent,
    MediaAttachment,
)
from pipeline.directive_parser import directive_parser
from core.registry import get_skill_registry
skill_registry = get_skill_registry()


def test_media_attachment_contract():
    """Validates the multimodal data contract and serialization."""
    img_att = MediaAttachment(
        file_id="F_IMG_123",
        name="architecture_diagram.png",
        mimetype="image/png",
        attachment_type=AttachmentType.IMAGE,
        size=1024,
        b64_data="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
        authorized_agents={"agent-10008", "agent-10011"},
    )
    d = img_att.to_dict()
    assert d["file_id"] == "F_IMG_123"
    assert d["attachment_type"] == "image"
    assert d["b64_data"] is not None
    assert set(d["authorized_agents"]) == {"agent-10008", "agent-10011"}

    restored = MediaAttachment.from_dict(d)
    assert restored.file_id == "F_IMG_123"
    assert restored.attachment_type == AttachmentType.IMAGE
    assert restored.authorized_agents == {"agent-10008", "agent-10011"}


def test_action_directive_contract():
    """Validates ActionDirective serialization and multiple actions without collision."""
    act1 = ActionDirective(name="fetch_pr", params={"pr_id": 42})
    act2 = ActionDirective(name="fetch_pr", params={"pr_id": 99})

    intent = ExtractedIntent(actions=[act1, act2])
    assert intent.has_action("fetch_pr") is True
    assert intent.has_action("unknown_act") is False

    pr_actions = intent.get_actions("fetch_pr")
    assert len(pr_actions) == 2
    assert pr_actions[0].params["pr_id"] == 42
    assert pr_actions[1].params["pr_id"] == 99


def test_extracted_intent_canonical_representation():
    """Validates that ExtractedIntent cleanly serializes and restores canonical modifiers and actions."""
    ei = ExtractedIntent(
        actions=[ActionDirective(name="document_reader", params={"target_file": "data.csv"})],
        modifiers={"max_words": 60, "min_words": 10, "history": 3},
    )
    assert ei.get_modifier("max_words") == 60
    assert ei.get_modifier("min_words") == 10
    assert ei.get_modifier("history") == 3
    assert ei.has_action("document_reader") is True

    # Validate dict round-trip
    d = ei.to_dict()
    assert d["modifiers"]["max_words"] == 60
    assert any(a["name"] == "document_reader" for a in d["actions"])

    ei_restored = ExtractedIntent.from_dict(d)
    assert ei_restored.get_modifier("max_words") == 60
    assert ei_restored.get_modifier("min_words") == 10
    assert ei_restored.has_action("document_reader") is True


def test_skill_registry_scopes_and_agent_catalog_isolation():
    """Ensures agent-level catalog prompt strictly excludes orchestrator and prompt_modifier skills."""
    all_skills = skill_registry.list_skills()
    assert any(s.name == "max_words" and s.scope == "prompt_modifier" for s in all_skills)
    assert any(s.name == "stop_discussion" and s.scope == "orchestrator" for s in all_skills)
    assert any(s.name == "document_reader" and s.scope == "agent" for s in all_skills)

    # Catalog for agents must strictly only contain agent skills
    agent_catalog = skill_registry.get_skills_catalog_prompt()
    assert "document_reader" in agent_catalog
    assert "max_words" not in agent_catalog
    assert "stop_discussion" not in agent_catalog
    assert "thread_history" not in agent_catalog


def test_slash_triggers_mapping():
    """Verifies that slash aliases are correctly indexed."""
    triggers = skill_registry.get_slash_triggers()
    assert "/max_words" in triggers
    assert "/max-words" in triggers
    assert "/history" in triggers
    assert "/stop" in triggers

    skill_def, _trg_cfg = triggers["/max_words"]
    assert skill_def.name == "max_words"
    assert skill_def.scope == "prompt_modifier"


@pytest.mark.anyio
async def test_deterministic_fast_path_slash_commands():
    """Tests 100% fail-proof slash command parsing via DirectiveParser."""
    raw = "/max-words 45 /history 2 [agent-10008] inspect database"
    directives = await directive_parser.parse(raw)

    assert directives.max_words == 45
    assert directives.modifiers.get("max_words") == 45
    assert directives.history_limit == 2
    assert directives.modifiers.get("history") == 2
    assert "[agent-10008] inspect database" in directives.cleaned_prompt


@pytest.mark.anyio
async def test_stop_command_immediate_short_circuit():
    """Tests that /stop generates an action and immediate stop flag."""
    raw = "/stop please terminate now"
    directives = await directive_parser.parse(raw)

    assert directives.is_stop is True
    assert any(a.name == "stop_discussion" for a in directives.actions)


@pytest.mark.anyio
async def test_smart_merge_slash_priority_over_llm():
    """Tests that deterministic slash overrides any conflicting LLM inference."""
    raw = "/max-words 30 write an essay"

    # Mock LLM extractor attempting to infer 100 words
    fake_intent = {
        "modifiers": {"max_words": 100},
        "actions": [],
        "max_words": 100,
    }

    with patch("nlp.intent_extractor.IntentExtractor.extract_intent") as mock_extract:
        mock_extract.return_value = ExtractedIntent.from_dict(fake_intent)
        directives = await directive_parser.parse(raw)

        # Slash command 30 must win over LLM's 100
        assert directives.max_words == 30
        assert directives.modifiers["max_words"] == 30


@pytest.mark.anyio
async def test_global_skill_declarative_frontmatter():
    """Verifies that skills marked global: true are universally accessible to all agents."""
    import json

    from skills.loader import SkillDefinition

    mock_global_skill = SkillDefinition(
        name="universal_tool",
        display_name="Universal Tool",
        description="A globally accessible skill for all agents.",
        tools=["web_search"],
        instructions="Operational instructions for universal tool.",
        scope="agent",
        is_global=True,
    )

    # Register temporarily in skill_registry
    skill_registry._skills["universal_tool"] = mock_global_skill
    try:
        # 1. Listed in get_global_skills
        assert "universal_tool" in skill_registry.get_global_skills()

        # 2. Included even when agent has 0 configured skills
        agent_skills = skill_registry.get_agent_skills(allowed_skills=[])
        assert any(s.name == "universal_tool" for s in agent_skills)

        # 3. Present in prompt catalog even with empty allowed_skills
        catalog = skill_registry.get_skills_catalog_prompt(allowed_skills=[])
        assert "universal_tool" in catalog

        # 4. load_skill succeeds without permission error
        res = await skill_registry.execute_tool_async(
            tool_name="load_skill",
            arguments={"skill_name": "universal_tool"},
            authorized_skills=[],
        )
        loaded = json.loads(res)
        assert loaded.get("status") == "loaded"
        assert loaded.get("skill") == "universal_tool"

        # 5. Local tools permitted for execution
        with patch("services.local_tools.web_search", new_callable=AsyncMock, return_value="mock_search_result"):
            tool_res = await skill_registry.execute_tool_async(
                tool_name="web_search",
                arguments={"query": "test"},
                authorized_skills=[],
                agent_name="agent-10001",
            )
            assert tool_res == "mock_search_result"
    finally:
        skill_registry._skills.pop("universal_tool", None)


@pytest.mark.anyio
async def test_dynamic_slash_parsing_grant_and_revoke_for_all():
    """Verifies dynamic slash command extraction for grant_skill_for_all and revoke_skill_for_all without hardcoding."""
    # Test grant_skill_for_all with multiple skills
    raw1 = "/grant_skill_for_all web_research document_reader Discuss database schema"
    d1 = await directive_parser.parse(raw1)
    grant_acts = [a for a in d1.actions if a.name == "grant_skill_for_all"]
    assert len(grant_acts) == 1
    assert grant_acts[0].params.get("skills") == ["web_research", "document_reader"]
    assert "Discuss database schema" in d1.cleaned_prompt
    assert "/grant_skill_for_all" not in d1.cleaned_prompt

    # Test hyphenated alias /grant-skill-for-all
    raw2 = "/grant-skill-for-all web_research"
    d2 = await directive_parser.parse(raw2)
    assert any(a.name == "grant_skill_for_all" and a.params.get("skills") == ["web_research"] for a in d2.actions)

    # Test revoke_skill_for_all with specific skill
    raw3 = "/revoke_skill_for_all web_research Please continue"
    d3 = await directive_parser.parse(raw3)
    revoke_acts = [a for a in d3.actions if a.name == "revoke_skill_for_all"]
    assert len(revoke_acts) == 1
    assert revoke_acts[0].params.get("skills") == ["web_research"]
    assert "Please continue" in d3.cleaned_prompt

    # Test revoke_skill_for_all all
    raw4 = "/revoke-skill-for-all all"
    d4 = await directive_parser.parse(raw4)
    revoke_acts4 = [a for a in d4.actions if a.name == "revoke_skill_for_all"]
    assert len(revoke_acts4) == 1
    assert revoke_acts4[0].params.get("skills") == ["all"]


def test_declarative_requires_admin_all_skills():
    """Verifies that all 21 skills in the registry define a boolean requires_admin,
    and get_admin_skills() returns exactly the expected administrative directives."""
    all_skills = skill_registry.list_skills()
    assert len(all_skills) >= 21
    for s in all_skills:
        assert isinstance(s.requires_admin, bool), f"Skill {s.name} requires_admin is not bool: {s.requires_admin}"

    admin_skills = skill_registry.get_admin_skills()
    admin_names = {s.name for s in admin_skills}
    expected_admins = {
        "grant_skill",
        "grant_skill_for_all",
        "revoke_skill_for_all",
        "revoke_constraint",
    }
    assert admin_names == expected_admins
    for s in admin_skills:
        assert s.requires_admin is True

    # Check non-admin skills
    non_admin_names = {s.name for s in all_skills if not s.requires_admin}
    assert "web_research" in non_admin_names
    assert "document_reader" in non_admin_names
    assert "max_words" in non_admin_names
    assert "stop_discussion" in non_admin_names


def test_missing_or_invalid_requires_admin_raises_integrity_error(tmp_path):
    """Verifies that missing or non-boolean requires_admin in SKILL.md raises SkillIntegrityError."""
    from skills.loader import SkillIntegrityError, SkillRegistry

    registry = SkillRegistry()

    # 1. Missing requires_admin
    bad_skill1 = tmp_path / "skill1" / "SKILL.md"
    bad_skill1.parent.mkdir(parents=True, exist_ok=True)
    bad_skill1.write_text(
        "---\n"
        "name: test_missing\n"
        "display_name: Test Missing\n"
        "description: Testing missing requires_admin\n"
        "---\n"
        "Instructions.\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillIntegrityError) as exc1:
        registry._parse_skill_file(bad_skill1)
    assert "missing mandatory boolean 'requires_admin'" in str(exc1.value)

    # 2. Non-boolean string requires_admin: 'true'
    bad_skill2 = tmp_path / "skill2" / "SKILL.md"
    bad_skill2.parent.mkdir(parents=True, exist_ok=True)
    bad_skill2.write_text(
        "---\n"
        "name: test_str_bool\n"
        "display_name: Test Str\n"
        "description: Testing string bool\n"
        "requires_admin: 'true'\n"
        "---\n"
        "Instructions.\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillIntegrityError) as exc2:
        registry._parse_skill_file(bad_skill2)
    assert "'requires_admin' must be a boolean" in str(exc2.value)

    # 3. reload() during init must re-raise SkillIntegrityError rather than swallowing it as non-fatal
    with pytest.raises(SkillIntegrityError):
        SkillRegistry(root_dir=tmp_path)


def test_skills_directory_admin_lock_indicator():
    """Verifies format_skills_directory_markdown appends lock icon to requires_admin skills."""
    doc = skill_registry.format_skills_directory_markdown()
    assert "grant_skill" in doc
    assert "🔒 *Admin Only*" in doc
    grant_line = next((line for line in doc.splitlines() if line.startswith("• `grant_skill`")), "")
    assert "🔒 *Admin Only*" in grant_line
    max_words_line = next((line for line in doc.splitlines() if line.startswith("• `max_words`")), "")
    assert "🔒 *Admin Only*" not in max_words_line


def test_resolve_skill_aliases():
    """Verifies resolve_skill resolves canonical names and slash command aliases."""
    # Canonical
    sk1 = skill_registry.resolve_skill("grant_skill")
    assert sk1 is not None and sk1.name == "grant_skill"

    # Alias /grant_for_all
    sk2 = skill_registry.resolve_skill("grant_for_all")
    assert sk2 is not None and sk2.name == "grant_skill_for_all"

    # Alias with leading slash /revoke
    sk3 = skill_registry.resolve_skill("/revoke")
    assert sk3 is not None and sk3.name == "revoke_constraint"

    # Alias with hyphen
    sk4 = skill_registry.resolve_skill("max-words")
    assert sk4 is not None and sk4.name == "max_words"

    # Unknown
    assert skill_registry.resolve_skill("nonexistent_skill_xyz") is None


def test_filter_skills_for_caller():
    """Verifies filter_skills_for_caller correctly restricts admin skills unless caller is admin or thread-granted."""
    from skills.loader import SkillDefinition

    mock_admin_skill = SkillDefinition(
        name="admin_only_skill",
        display_name="Admin Skill",
        description="Exclusive admin skill",
        scope="agent",
        requires_admin=True,
    )
    mock_public_skill = SkillDefinition(
        name="public_skill",
        display_name="Public Skill",
        description="Public skill",
        scope="agent",
        requires_admin=False,
    )

    skill_registry._skills["admin_only_skill"] = mock_admin_skill
    skill_registry._skills["public_skill"] = mock_public_skill
    try:
        candidate_skills = ["admin_only_skill", "public_skill"]

        # Case 1: Non-admin caller -> admin skill filtered out
        filtered_user = skill_registry.filter_skills_for_caller(
            candidate_skills, is_admin=False, thread_granted_skills=set()
        )
        assert filtered_user == ["public_skill"]

        # Case 2: Admin caller -> admin skill preserved
        filtered_admin = skill_registry.filter_skills_for_caller(
            candidate_skills, is_admin=True, thread_granted_skills=set()
        )
        assert filtered_admin == ["admin_only_skill", "public_skill"]

        # Case 3: Non-admin caller, but skill was explicitly thread-granted by an admin
        filtered_granted = skill_registry.filter_skills_for_caller(
            candidate_skills, is_admin=False, thread_granted_skills={"admin_only_skill"}
        )
        assert filtered_granted == ["admin_only_skill", "public_skill"]
    finally:
        skill_registry._skills.pop("admin_only_skill", None)
        skill_registry._skills.pop("public_skill", None)


