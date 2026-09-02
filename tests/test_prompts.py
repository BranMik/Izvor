import orchestration.agent_settings  # Ensure AGENTS_CONFIG is populated
from core.config import AGENTS_CONFIG
from prompts.builder import SHORT_OUTPUT_AGENTS, build_agent_prompt


def test_all_17_agents_configured():
    assert len(AGENTS_CONFIG) == 17


def test_visionary_prompt_structure():
    prompt = AGENTS_CONFIG["Visionary"]["system_prompt"]
    assert 'You are "The Visionary"' in prompt
    assert "OUTPUT & LENGTH CONSTRAINTS:" in prompt


def test_architect_prompt_structure():
    prompt = AGENTS_CONFIG["Architect"]["system_prompt"]
    assert 'You are the "Chief Systems Architect"' in prompt
    assert "architectural trade-offs" in prompt


def test_qa_lead_prompt_structure():
    assert "QALead" in AGENTS_CONFIG
    prompt = AGENTS_CONFIG["QALead"]["system_prompt"]
    assert "Quality Assurance & Test Strategy Lead" in prompt
    assert "testability" in prompt.lower()
    assert "test pyramid" in prompt.lower()
    assert "Hard Limit: Your entire response must strictly NOT exceed 100 words." in prompt


def test_all_agents_contain_shared_skills():
    assert len(AGENTS_CONFIG) == 17
    assert len(SHORT_OUTPUT_AGENTS) == 8

    for key in AGENTS_CONFIG:
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


def test_pricing_agent_prompt_structure():
    prompt = build_agent_prompt("Pricing_Agent")
    assert "Model Pricing Specialist" in prompt
    assert "web_search" in prompt
    assert "input_price_per_1m" in prompt
    assert "output_price_per_1m" in prompt


def test_build_agent_prompt_with_max_words():
    # Intermediate agent: verify standard output constraints are skipped, only max_words is added
    prompt = build_agent_prompt("TechLead", max_words=50)
    assert "Hard Limit: Your entire response must strictly NOT exceed 50 words." in prompt
    # Verify standard output_constraints sections are NOT in the prompt
    assert "1 sentence stating your core verdict/concern." not in prompt
    assert "2-3 concise bullet points" not in prompt
    assert "1 concluding sentence with a concrete recommendation." not in prompt
    # Verify nomination rules and fixed language constraints are still present
    assert "nominate the next agent" in prompt
    assert "Aim to answer in the same language as user prompt is made." in prompt

    # Short output agent: verify standard 100w output constraints are skipped but language is kept
    prompt_short = build_agent_prompt("QALead", max_words=30)
    assert "Hard Limit: Your entire response must strictly NOT exceed 30 words." in prompt_short
    assert "Hard Limit: Your entire response must strictly NOT exceed 100 words." not in prompt_short
    assert "1 sentence stating your core verdict/concern." not in prompt_short
    assert "Aim to answer in the same language as user prompt is made." in prompt_short

    # Architect: verify override
    prompt_arch = build_agent_prompt("Architect", max_words=120)
    assert "strictly NOT exceed 120 words" in prompt_arch
    assert "strictly NOT exceed 200 words" not in prompt_arch

    # Visionary: verify override
    prompt_vis = build_agent_prompt("Visionary", max_words=45)
    assert "strictly NOT exceed 45 words" in prompt_vis
    assert "strictly NOT exceed 200 words" not in prompt_vis


def test_build_agent_prompt_with_min_words():
    # Intermediate agent: verify standard output constraints are skipped, only min_words is added
    prompt = build_agent_prompt("TechLead", min_words=60)
    assert "Minimum Length: Your entire response must have a minimum length of 60 words." in prompt
    assert "1 sentence stating your core verdict/concern." not in prompt
    assert "2-3 concise bullet points" not in prompt
    assert "nominate the next agent" in prompt

    # Short output agent: verify standard 100w output constraints are skipped
    prompt_short = build_agent_prompt("QALead", min_words=40)
    assert "Minimum Length: Your entire response must have a minimum length of 40 words." in prompt_short
    assert "Hard Limit: Your entire response must strictly NOT exceed 100 words." not in prompt_short

    # Architect: verify min_words addition
    prompt_arch = build_agent_prompt("Architect", min_words=150)
    assert "Minimum Length: Your entire response must have a minimum length of 150 words." in prompt_arch

    # Visionary: verify min_words addition
    prompt_vis = build_agent_prompt("Visionary", min_words=50)
    assert "Minimum Length: Your entire response must have a minimum length of 50 words." in prompt_vis


def test_build_agent_prompt_with_min_and_max_words():
    # Both limits present
    prompt = build_agent_prompt("TechLead", min_words=50, max_words=150)
    assert "Minimum Length: Your entire response must have a minimum length of 50 words." in prompt
    assert "Hard Limit: Your entire response must strictly NOT exceed 150 words." in prompt
    assert "1 sentence stating your core verdict/concern." not in prompt

    prompt_arch = build_agent_prompt("Architect", min_words=80, max_words=300)
    assert "Minimum Length: Your entire response must have a minimum length of 80 words." in prompt_arch
    assert "strictly NOT exceed 300 words" in prompt_arch
