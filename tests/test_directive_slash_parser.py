import pytest

from pipeline.directive_parser import DirectiveParser, is_stop_command
from pipeline.directive_parser.slash_parser import (
    parse_agent_list,
    parse_file_target,
    parse_int_or_off,
    parse_token,
)
from pipeline.entity_resolver import entity_resolver

# ==============================================================================
# 1. Stop Command Detection
# ==============================================================================


def test_stop_command_detection():
    assert is_stop_command("/stop") is True
    assert is_stop_command("Please /stop the discussion") is True
    assert is_stop_command("Don't stop now") is False
    assert is_stop_command("") is False


# ==============================================================================
# 2. Entity Resolver Agent Name Matching
# ==============================================================================


def test_resolve_agent_name():
    assert entity_resolver.resolve_agent_name("[agent-10011]") == "agent-10011"
    assert entity_resolver.resolve_agent_name("legaladvisor") == "agent-10011"
    assert entity_resolver.resolve_agent_name("Legal Advisor") == "agent-10011"
    assert entity_resolver.resolve_agent_name("[agent-10002]") == "agent-10002"
    assert entity_resolver.resolve_agent_name("agent-10002") == "agent-10002"
    assert entity_resolver.resolve_agent_name("agent-10003") == "agent-10003"
    assert entity_resolver.resolve_agent_name("agent-10001") == "agent-10001"
    assert entity_resolver.resolve_agent_name("[Vis]") == "agent-10001"
    assert entity_resolver.resolve_agent_name("Vis") == "agent-10001"
    assert entity_resolver.resolve_agent_name("[agent-10008]") == "agent-10008"
    assert entity_resolver.resolve_agent_name("agent-10008") == "agent-10008"
    assert entity_resolver.resolve_agent_name("[Inn]") == "agent-10002"
    assert entity_resolver.resolve_agent_name("[Inn}") == "agent-10002"
    assert entity_resolver.resolve_agent_name("*[Market]*") == "agent-10009"
    assert entity_resolver.resolve_agent_name("[Market]") == "agent-10009"
    # Ambiguous check: [Arch] matches SecurityArchitect & SecurityArchitect -> None
    assert entity_resolver.resolve_agent_name("[Arch]") == "agent-10008"
    assert entity_resolver.resolve_agent_name("NonExistentAgent") is None
    assert entity_resolver.resolve_agent_name("") is None

    # Test resolve_candidates
    assert entity_resolver.resolve_candidates("[Vis]") == ["agent-10001"]
    assert "agent-10008" in entity_resolver.resolve_candidates("[agent-10008]")
    assert len(entity_resolver.resolve_candidates("[Arch]")) == 1
    assert "agent-10008" in entity_resolver.resolve_candidates("[Arch]")
    assert len(entity_resolver.resolve_candidates("[Strategist]")) == 1
    assert entity_resolver.resolve_candidates("[NonExistent]") == []
    assert entity_resolver.resolve_candidates("[TODO]") == []


# ==============================================================================
# 3. Deterministic Integer & Off Slash Parsing
# ==============================================================================


def test_word_limits_and_off():
    matched, val, text = parse_int_or_off("/max_words", "/max_words 5 Discuss this")
    assert matched is True
    assert val == 5
    assert text == "Discuss this"

    matched, val, text = parse_int_or_off("/max_words", "/max_words off Discuss this")
    assert matched is True
    assert val == "off"
    assert text == "Discuss this"

    # None or invalid int should not match int/off
    matched, val, text = parse_int_or_off("/max_words", "/max_words none Discuss this")
    assert val is None

    matched, val, text = parse_int_or_off("/min_words", "/min_words 20 Elaborate")
    assert matched is True
    assert val == 20
    assert text == "Elaborate"

    matched, val, text = parse_int_or_off("/min_words", "/min_words off Elaborate")
    assert matched is True
    assert val == "off"
    assert text == "Elaborate"


def test_scribe_word_limits():
    matched, val, text = parse_int_or_off("/scribe_max_words", "/scribe_max_words 50 Wrap up summary")
    assert matched is True
    assert val == 50
    assert text == "Wrap up summary"

    matched, val, text = parse_int_or_off("/scribe-max-words", "/scribe-max-words off Wrap up summary")
    assert matched is True
    assert val == "off"
    assert text == "Wrap up summary"

    matched, val, text = parse_int_or_off("/scribe_min_words", "/scribe_min_words 10 Wrap up summary")
    assert matched is True
    assert val == 10
    assert text == "Wrap up summary"

    matched, val, text = parse_int_or_off("/scribe_min_words", "/scribe_min_words off Wrap up summary")
    assert matched is True
    assert val == "off"
    assert text == "Wrap up summary"


# ==============================================================================
# 4. Agent List Slash Parsing (/exclude, /include)
# ==============================================================================


def test_exclude_slash_command_single_and_multi():
    # Single agent slash command
    matched, exc, is_off, text = parse_agent_list("/exclude", "/exclude [agent-10011] Debate this feature")
    assert matched is True
    assert exc == ["agent-10011"]
    assert is_off is False
    assert text == "Debate this feature"

    # Multi-agent syntax: /exclude [Agent1] [Agent2] [Agent3]
    matched, exc, is_off, text = parse_agent_list(
        "/exclude", "/exclude [agent-10011] [agent-10002] [agent-10005] Plan the sprint"
    )
    assert exc == ["agent-10011", "agent-10002", "agent-10005"]
    assert is_off is False
    assert text == "Plan the sprint"

    # Comma-separated syntax
    matched, exc, is_off, text = parse_agent_list(
        "/exclude", "/exclude [agent-10011], [agent-10002] Plan the sprint"
    )
    assert exc == ["agent-10011", "agent-10002"]
    assert is_off is False
    assert text == "Plan the sprint"

    # Colon syntax
    matched, exc, is_off, text = parse_agent_list("/exclude", "/exclude: [agent-10011] test")
    assert exc == ["agent-10011"]
    assert is_off is False
    assert text == "test"

    # Unbracketed names
    matched, exc, is_off, text = parse_agent_list("/exclude", "/exclude LegalAdvisor Innovator test")
    assert exc == ["agent-10011", "agent-10002"]
    assert is_off is False
    assert text == "test"

    # Multiple separate /exclude commands
    matched, exc, is_off, text = parse_agent_list(
        "/exclude", "/exclude [agent-10011] Some text /exclude [agent-10002]"
    )
    assert exc == ["agent-10011", "agent-10002"]
    assert is_off is False
    assert text == "Some text"


def test_exclude_off():
    _matched, exc, is_off, text = parse_agent_list("/exclude", "/exclude off Continue the debate")
    assert exc == []
    assert is_off is True
    assert text == "Continue the debate"

    # Verify none is NOT treated as off
    _matched, exc, is_off, text = parse_agent_list("/exclude", "/exclude none Continue the debate")
    assert is_off is False


def test_include_slash_command_and_off():
    _matched, inc, is_off, text = parse_agent_list("/include", "/include [agent-10011] Debate this feature")
    assert inc == ["agent-10011"]
    assert is_off is False
    assert text == "Debate this feature"

    _matched, inc, is_off, text = parse_agent_list("/include", "/include [agent-10002] [agent-10008] Let's discuss")
    assert inc == ["agent-10002", "agent-10008"]
    assert is_off is False
    assert text == "Let's discuss"

    _matched, inc, is_off, text = parse_agent_list("/include", "/include off Reset inclusions")
    assert inc == []
    assert is_off is True
    assert text == "Reset inclusions"


# ==============================================================================
# 5. File Slash Parsing & Token Revoke
# ==============================================================================


def test_file_slash_command_extraction():
    matched, target, text = parse_file_target("/file", "/file: schema.sql Provjeri indekse")
    assert matched is True
    assert target == "schema.sql"
    assert text == "Provjeri indekse"


def test_revoke_instruction():
    matched, target, text = parse_token("/revoke_constraint", "/revoke_constraint all Let's continue")
    assert matched is True
    assert target == "all"
    assert text == "Let's continue"

    matched, target, text = parse_token("/revoke_constraint", "/revoke_constraint max_words Continue")
    assert target == "max_words"
    assert text == "Continue"

    matched, target, text = parse_token("/revoke_constraint", "/revoke_constraint [exclude] Go ahead")
    assert target == "exclude"
    assert text == "Go ahead"


# ==============================================================================
# 6. End-to-End DirectiveParser with Dynamic Skill Sourcing
# ==============================================================================


@pytest.mark.anyio
async def test_directive_parser_dynamic_skills_sourcing():
    parser = DirectiveParser()
    raw = "/max_words 40 /history 5 /exclude [agent-10011] What is our roadmap?"
    directives = await parser.parse(raw)

    assert directives.max_words == 40
    assert directives.history_limit == 5
    assert "agent-10011" in directives.excluded_agents
    assert "What is our roadmap?" in directives.cleaned_prompt
    assert not directives.is_stop


@pytest.mark.anyio
async def test_directive_parser_file_and_revoke():
    parser = DirectiveParser()
    raw = "/file schema.sql /revoke_constraint max_words Check this query"
    directives = await parser.parse(raw)

    assert directives.is_file_req is True
    assert directives.target_filename == "schema.sql"
    assert directives.revoke_target == "max_words"
    assert "Check this query" in directives.cleaned_prompt


def test_strip_all_slash_commands():
    from pipeline.directive_parser.slash_parser import strip_all_slash_commands

    raw = "[agent-10011] /history 3 /max_words 50 /exclude [agent-10002] What should we refactor?"
    cleaned = strip_all_slash_commands(raw)
    assert "/history" not in cleaned
    assert "/max_words" not in cleaned
    assert "/exclude" not in cleaned
    assert "[agent-10002]" not in cleaned
    assert "[agent-10011]" in cleaned
    assert "What should we refactor?" in cleaned
