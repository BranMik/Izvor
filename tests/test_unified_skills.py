"""
Unit and integration tests for the Unified Skills Architecture and Multimodal Data Contracts.
"""

from pathlib import Path
from unittest.mock import patch
import pytest

from core.models import (
    ActionDirective,
    AttachmentType,
    ExtractedIntent,
    MediaAttachment,
)
from pipeline.directive_parser import directive_parser
from skills import skill_registry


def test_media_attachment_contract():
    """Validates the multimodal data contract and serialization."""
    img_att = MediaAttachment(
        file_id="F_IMG_123",
        name="architecture_diagram.png",
        mimetype="image/png",
        attachment_type=AttachmentType.IMAGE,
        size=1024,
        b64_data="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
        authorized_agents={"Architect", "TechLead"},
    )
    d = img_att.to_dict()
    assert d["file_id"] == "F_IMG_123"
    assert d["attachment_type"] == "image"
    assert d["b64_data"] is not None
    assert set(d["authorized_agents"]) == {"Architect", "TechLead"}

    restored = MediaAttachment.from_dict(d)
    assert restored.file_id == "F_IMG_123"
    assert restored.attachment_type == AttachmentType.IMAGE
    assert restored.authorized_agents == {"Architect", "TechLead"}


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


def test_extracted_intent_backward_and_unified_compatibility():
    """Validates that ExtractedIntent seamlessly maps legacy properties to modifiers and actions."""
    ei = ExtractedIntent(max_words=60, min_words=10, history=3, file_use=True, target_file="data.csv")
    assert ei.max_words == 60
    assert ei.min_words == 10
    assert ei.history == 3
    assert ei.file_use is True
    assert ei.target_file == "data.csv"
    assert ei.has_action("document_reader") is True

    # Validate dict round-trip
    d = ei.to_dict()
    assert d["modifiers"]["max_words"] == 60
    assert d["max_words"] == 60
    assert any(a["name"] == "document_reader" for a in d["actions"])

    ei_restored = ExtractedIntent.from_dict(d)
    assert ei_restored.max_words == 60
    assert ei_restored.min_words == 10
    assert ei_restored.has_action("document_reader") is True


def test_skill_registry_scopes_and_agent_catalog_isolation():
    """Ensures agent-level catalog prompt strictly excludes orchestrator and prompt_modifier skills."""
    all_skills = skill_registry.list_skills()
    assert any(s.name == "max_words" and s.scope == "prompt_modifier" for s in all_skills)
    assert any(s.name == "stop_discussion" and s.scope == "orchestrator" for s in all_skills)
    assert any(s.name == "codebase_inspection" and s.scope == "agent" for s in all_skills)

    # Catalog for agents must strictly only contain agent skills
    agent_catalog = skill_registry.get_skills_catalog_prompt()
    assert "codebase_inspection" in agent_catalog
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

    skill_def, trg_cfg = triggers["/max_words"]
    assert skill_def.name == "max_words"
    assert skill_def.scope == "prompt_modifier"


@pytest.mark.anyio
async def test_deterministic_fast_path_slash_commands():
    """Tests 100% fail-proof slash command parsing via DirectiveParser."""
    raw = "/max-words 45 /history 2 [Architect] inspect database"
    directives = await directive_parser.parse(raw)

    assert directives.max_words == 45
    assert directives.modifiers.get("max_words") == 45
    assert directives.history_limit == 2
    assert directives.modifiers.get("history") == 2
    assert "[Architect] inspect database" in directives.cleaned_prompt


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
