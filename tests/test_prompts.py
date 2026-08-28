from config import AGENTS_CONFIG
from prompts.builder import SHORT_OUTPUT_AGENTS, build_agent_prompt


def test_all_17_agents_configured():
    assert len(AGENTS_CONFIG) == 17


def test_visionary_prompt_structure():
    prompt = AGENTS_CONFIG["Gemini_Visionary"]["system_prompt"]
    assert 'You are "The Visionary"' in prompt
    assert "AnthrOpus_Sceptic" in prompt
    assert "OUTPUT & LENGTH CONSTRAINTS:" in prompt


def test_architect_prompt_structure():
    prompt = AGENTS_CONFIG["OpAiTerra_Architect"]["system_prompt"]
    assert 'You are the "Chief Systems Architect"' in prompt
    assert "end summary of the discussion" in prompt


def test_qa_lead_prompt_structure():
    assert "MistLar_QALead" in AGENTS_CONFIG
    prompt = AGENTS_CONFIG["MistLar_QALead"]["system_prompt"]
    assert "Quality Assurance & Test Strategy Lead" in prompt
    assert "testability" in prompt.lower()
    assert "test pyramid" in prompt.lower()
    assert "Hard Limit: Your entire response must strictly NOT exceed 100 words." in prompt


def test_intermediate_agents_contain_shared_skills():
    intermediate_keys = [
        k for k in AGENTS_CONFIG if k not in ("Gemini_Visionary", "OpAiTerra_Architect")
    ]
    assert len(intermediate_keys) == 15
    assert len(SHORT_OUTPUT_AGENTS) == 8

    for key in intermediate_keys:
        prompt = AGENTS_CONFIG[key]["system_prompt"]
        # Output format skill checks
        if key in SHORT_OUTPUT_AGENTS:
            assert "Hard Limit: Your entire response must strictly NOT exceed 100 words." in prompt
        else:
            assert "Hard Limit: Your entire response must strictly NOT exceed 200 words." in prompt

        assert "Aim to answer in the same language as user prompt is made." in prompt
        assert "1 sentence stating your core verdict/concern." in prompt
        # Nomination skill checks
        assert "nominate the next agent from the available list provided in the prompt" in prompt
        assert "OpAiTerra_Architect" in prompt


def test_pricing_agent_prompt_structure():
    prompt = build_agent_prompt("Pricing_Agent")
    assert "Model Pricing Specialist" in prompt
    assert "web_search" in prompt
    assert "input_price_per_1m" in prompt
    assert "output_price_per_1m" in prompt


def test_build_agent_prompt_with_max_words():
    # Intermediate agent: verify standard output constraints are skipped, only max_words is added
    prompt = build_agent_prompt("MistLar_TechLead", max_words=50)
    assert "Hard Limit: Your entire response must strictly NOT exceed 50 words." in prompt
    # Verify standard output_constraints sections are NOT in the prompt
    assert "1 sentence stating your core verdict/concern." not in prompt
    assert "2-3 concise bullet points" not in prompt
    assert "1 concluding sentence with a concrete recommendation." not in prompt
    # Verify nomination rules are still present
    assert "nominate the next agent" in prompt

    # Short output agent: verify standard 100w output constraints are skipped
    prompt_short = build_agent_prompt("MistLar_QALead", max_words=30)
    assert "Hard Limit: Your entire response must strictly NOT exceed 30 words." in prompt_short
    assert "Hard Limit: Your entire response must strictly NOT exceed 100 words." not in prompt_short
    assert "1 sentence stating your core verdict/concern." not in prompt_short

    # OpAiTerra_Architect: verify override
    prompt_arch = build_agent_prompt("OpAiTerra_Architect", max_words=120)
    assert "strictly NOT exceed 120 words" in prompt_arch
    assert "strictly NOT exceed 700 words" not in prompt_arch

    # Gemini_Visionary: verify override
    prompt_vis = build_agent_prompt("Gemini_Visionary", max_words=45)
    assert "strictly NOT exceed 45 words" in prompt_vis



