import pytest
from agent_router import parse_agent_and_prompt


def test_parse_explicit_bracket_tags():
    agent_key, prompt = parse_agent_and_prompt(
        "<@U12345> [Gemini_Visionary] Create a new UI flow", "C12345", "123456789.0001"
    )
    assert agent_key == "Gemini_Visionary"
    assert prompt == "Create a new UI flow"


def test_parse_mistral_techlead_tag():
    agent_key, prompt = parse_agent_and_prompt(
        "[Mistral_TechLead] Check this database query", "C12345", "123456789.0001"
    )
    assert agent_key == "Mistral_TechLead"
    assert prompt == "Check this database query"


def test_parse_at_mention_persona():
    agent_key, prompt = parse_agent_and_prompt(
        "<@U12345> @CaiLogics Innovator generate an alternative algorithm",
        "C12345",
        "123456789.0001",
    )
    assert agent_key == "Mistral_Innovator"
    assert prompt == "generate an alternative algorithm"


def test_parse_fallback_default_agent():
    agent_key, prompt = parse_agent_and_prompt(
        "<@U12345> Tell me how to structure this application",
        "C12345",
        "123456789.0001",
    )
    assert agent_key == "Gemini_Visionary"
    assert prompt == "Tell me how to structure this application"
