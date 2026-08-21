from config import AGENTS_CONFIG
from prompts.builder import build_agent_prompt


def test_all_16_agents_configured():
    assert len(AGENTS_CONFIG) == 16


def test_visionary_prompt_structure():
    prompt = AGENTS_CONFIG["Gemini_Visionary"]["system_prompt"]
    assert 'You are "The Visionary"' in prompt
    assert "AnthrOpus_Sceptic" in prompt
    assert "OUTPUT & LENGTH CONSTRAINTS:" in prompt


def test_architect_prompt_structure():
    prompt = AGENTS_CONFIG["OpAiTerra_Architect"]["system_prompt"]
    assert 'You are the "Chief Systems Architect"' in prompt
    assert "end summary of the discussion" in prompt


def test_intermediate_agents_contain_shared_skills():
    intermediate_keys = [
        k for k in AGENTS_CONFIG if k not in ("Gemini_Visionary", "OpAiTerra_Architect")
    ]
    assert len(intermediate_keys) == 14

    for key in intermediate_keys:
        prompt = AGENTS_CONFIG[key]["system_prompt"]
        # Output format skill checks
        assert "Hard Limit: Your entire response must strictly NOT exceed 200 words." in prompt
        assert "Aim to answer in the same language as user prompt is made." in prompt
        assert "1 sentence stating your core verdict/concern." in prompt
        # Nomination skill checks
        assert "nominate the next agent from the available list below" in prompt
        assert "AVAILABLE AGENTS WHO HAVE NOT SPOKEN YET:" in prompt
