from pathlib import Path
from core.paths import PROMPTS_DIR, SYSTEM_AGENTS_DIR, PERSONAS_DIR
from orchestration.agent_settings import get_agents_config
from orchestration.system_settings import get_output_max_words
from prompts.builder import (
    build_agent_prompt,
    get_short_output_agents,
    is_short_output_agent,
    load_system_prompt,
)

AGENTS_CONFIG = get_agents_config()


def test_all_agents_configured():
    assert len(AGENTS_CONFIG) == 16


def test_visionary_prompt_structure():
    prompt = AGENTS_CONFIG["agent-10001"]["system_prompt"]
    assert '<agent_specification id="agent-10001" role="The Visionary"' in prompt
    assert "<cognitive_objective>" in prompt
    assert "<evaluation_lens>" in prompt
    assert "<jurisdiction_boundaries>" in prompt
    assert "<output_contract>" in prompt
    assert "You are" not in prompt
    assert "OUTPUT & LENGTH CONSTRAINTS:" in prompt


def test_cluster1_dialectical_core_xml_structure():
    cluster1_roles = {
        "agent-10001": "The Visionary",
        "agent-10002": "Innovation Lead",
        "agent-10003": "The Sceptic",
    }
    for agent_key, expected_role in cluster1_roles.items():
        prompt = build_agent_prompt(agent_key)
        assert f'<agent_specification id="{agent_key}" role="{expected_role}"' in prompt
        assert "<cognitive_objective>" in prompt
        assert "<evaluation_lens>" in prompt
        assert "<jurisdiction_boundaries>" in prompt
        assert "<operational_rules>" in prompt
        assert "<output_contract>" in prompt
        assert "You are" not in prompt


def test_security_architect_prompt_structure():
    prompt = AGENTS_CONFIG["agent-10008"]["system_prompt"]
    assert '<agent_specification id="agent-10008" role="Security & Threat Defense Lead"' in prompt
    assert "<cognitive_objective>" in prompt
    assert "<evaluation_lens>" in prompt
    assert "<jurisdiction_boundaries>" in prompt
    assert "<tool_and_evidence_grounding>" in prompt
    assert "<output_contract>" in prompt
    assert "You are" not in prompt
    assert "security" in prompt.lower()


def test_cluster2_governance_security_legal_risk_xml_structure():
    cluster2_roles = {
        "agent-10004": "Compliance, Privacy And Governance Officer",
        "agent-10008": "Security & Threat Defense Lead",
        "agent-10011": "Legal Advisor",
        "agent-10013": "The Risk Auditor",
    }
    for agent_key, expected_role in cluster2_roles.items():
        prompt = build_agent_prompt(agent_key)
        assert f'<agent_specification id="{agent_key}" role="{expected_role}"' in prompt
        assert "<cognitive_objective>" in prompt
        assert "<evaluation_lens>" in prompt
        assert "<jurisdiction_boundaries>" in prompt
        assert "<operational_rules>" in prompt
        assert "<output_contract>" in prompt
        assert "You are" not in prompt


def test_sales_advisor_prompt_structure():
    assert "agent-10010" in AGENTS_CONFIG
    prompt = AGENTS_CONFIG["agent-10010"]["system_prompt"]
    assert '<agent_specification id="agent-10010" role="Sales Advisor"' in prompt
    assert "Sales Advisor" in prompt
    assert "sales" in prompt.lower()
    assert "You are" not in prompt
    short_max = get_output_max_words(is_short=True)
    assert f"Hard Limit: Your entire response must strictly NOT exceed {short_max} words." in prompt


def test_cluster3_economics_market_scope_sales_xml_structure():
    cluster3_roles = {
        "agent-10005": "Scope & Feature Rationalization Lead",
        "agent-10006": "ROI & Business Value Maximizer",
        "agent-10009": "Market and Competition Research Expert",
        "agent-10010": "Sales Advisor",
    }
    for agent_key, expected_role in cluster3_roles.items():
        prompt = build_agent_prompt(agent_key)
        assert f'<agent_specification id="{agent_key}" role="{expected_role}"' in prompt
        assert "<cognitive_objective>" in prompt
        assert "<evaluation_lens>" in prompt
        assert "<jurisdiction_boundaries>" in prompt
        assert "<operational_rules>" in prompt
        assert "<output_contract>" in prompt
        assert "You are" not in prompt


def test_cluster4_product_ux_execution_revisor_creative_xml_structure():
    cluster4_roles = {
        "agent-10007": "Voice of the User",
        "agent-10012": "The Product Strategist",
        "agent-10014": "The Tactician",
        "agent-10015": "The Revisor",
        "agent-10016": "Creative Advisor",
    }
    for agent_key, expected_role in cluster4_roles.items():
        prompt = build_agent_prompt(agent_key)
        assert f'<agent_specification id="{agent_key}" role="{expected_role}"' in prompt
        assert "<cognitive_objective>" in prompt
        assert "<evaluation_lens>" in prompt
        assert "<jurisdiction_boundaries>" in prompt
        assert "<operational_rules>" in prompt
        assert "<output_contract>" in prompt
        assert "You are" not in prompt


def test_all_16_personas_enforce_2026_xml_and_zero_you_are():
    assert len(AGENTS_CONFIG) == 16
    for key in AGENTS_CONFIG:
        persona_path = PERSONAS_DIR / f"{key}.md"
        assert persona_path.exists(), f"Missing persona file: {persona_path}"
        persona_content = persona_path.read_text(encoding="utf-8")
        assert f'<agent_specification id="{key}"' in persona_content
        assert "</agent_specification>" in persona_content
        assert "<cognitive_objective>" in persona_content
        assert "<evaluation_lens>" in persona_content
        assert "<jurisdiction_boundaries>" in persona_content
        assert "IN-SCOPE:" in persona_content
        assert "OUT-OF-SCOPE (DEFER TO SPECIALISTS):" in persona_content
        assert "<operational_rules>" in persona_content
        assert "<output_contract>" in persona_content
        assert "You are" not in persona_content
        assert "you are" not in persona_content.lower()
        assert "YOUR PERSONALITY & TONE" not in persona_content


def test_all_system_agents_enforce_2026_xml_and_zero_you_are():
    system_agents = [
        "InternalRevisor",
        "Discussion_Scribe_Live",
        "Discussion_Scribe_Conclusion",
        "Intent_Extractor",
        "Language_Detector",
        "Pricing_Agent",
        "Challenger",
        "Arbitrator",
    ]
    for agent_name in system_agents:
        prompt = load_system_prompt(agent_name)
        assert f'<system_agent_specification id="{agent_name}"' in prompt, f"Missing system_agent_specification tag in {agent_name}"
        assert "</system_agent_specification>" in prompt, f"Missing closing tag in {agent_name}"
        assert "<task_objective>" in prompt, f"Missing task_objective in {agent_name}"
        assert "<processing_rules>" in prompt, f"Missing processing_rules in {agent_name}"
        assert "<output_contract>" in prompt, f"Missing output_contract in {agent_name}"
        assert "You are" not in prompt, f"Found 'You are' in {agent_name}"
        assert "you are" not in prompt.lower(), f"Found 'you are' in {agent_name}"


def test_all_shared_prompts_enforce_2026_xml_and_zero_you_are():
    shared_files = {
        "fixed_constraints.md": "<shared_platform_constraints>",
        "output_constraints.md": "<output_and_length_constraints>",
        "output_constraints_short.md": "<output_and_length_constraints>",
        "nomination_rules.md": "<speaker_nomination_protocol>",
    }
    shared_dir = PROMPTS_DIR / "shared"
    assert not (shared_dir / "output_constraints_100w.md").exists()
    for filename, expected_xml_tag in shared_files.items():
        file_path = shared_dir / filename
        assert file_path.exists(), f"Missing shared prompt file: {file_path}"
        content = file_path.read_text(encoding="utf-8")
        assert expected_xml_tag in content, f"Missing {expected_xml_tag} in {filename}"
        assert expected_xml_tag.replace("<", "</") in content, f"Missing closing tag for {expected_xml_tag} in {filename}"
        assert "You are" not in content, f"Found 'You are' in {filename}"
        assert "you are" not in content.lower(), f"Found 'you are' in {filename}"
        if filename.startswith("output_constraints"):
            assert "{max_words}" in content, f"Missing {{max_words}} placeholder in {filename}"


def test_system_agents_critical_runtime_contracts():
    revisor_prompt = load_system_prompt("InternalRevisor")
    assert "NO_HALLUCINATION" in revisor_prompt
    assert "ATTENTION:" in revisor_prompt
    assert 'ATTENTION: Agent [AgentName] hallucinated the following fact: "[hallucinated claim]"' in revisor_prompt

    live_scribe_prompt = load_system_prompt("Discussion_Scribe_Live")
    assert "*Core Objective & Problem Statement*" in live_scribe_prompt
    assert "*Key Perspectives*" in live_scribe_prompt
    assert "*Risks & Open Questions*" in live_scribe_prompt
    assert "Skip Empty Sections" in live_scribe_prompt
    assert "300 words" in live_scribe_prompt

    conclusion_scribe_prompt = load_system_prompt("Discussion_Scribe_Conclusion")
    assert "*Problem Statement*" in conclusion_scribe_prompt
    assert "*Consensus Decisions*" in conclusion_scribe_prompt
    assert "*Divergent Positions & Trade-offs*" in conclusion_scribe_prompt
    assert "*Action Items & Next Steps*" in conclusion_scribe_prompt
    assert "Skip Empty Sections" in conclusion_scribe_prompt
    assert "320 words" in conclusion_scribe_prompt


def test_all_agents_contain_shared_skills():
    assert len(AGENTS_CONFIG) == 16
    assert len(get_short_output_agents()) == 4
    default_max = get_output_max_words(is_short=False)
    short_max = get_output_max_words(is_short=True)

    for key in AGENTS_CONFIG:
        prompt = AGENTS_CONFIG[key]["system_prompt"]
        # Output format skill checks
        if is_short_output_agent(key):
            assert f"Hard Limit: Your entire response must strictly NOT exceed {short_max} words." in prompt
        else:
            assert f"Hard Limit: Your entire response must strictly NOT exceed {default_max} words." in prompt

        assert "Aim to answer in the same language as user prompt is made." in prompt
        assert "IDENTIFYING MESSAGE AUTHORS IN DISCUSSION HISTORY:" in prompt
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
    default_max = get_output_max_words(is_short=False)
    short_max = get_output_max_words(is_short=True)

    # Compact mode (max_words < 60): skips 3-part bullets and strips persona <output_contract>
    prompt = build_agent_prompt("agent-10011", max_words=50)
    assert "Hard Limit: Your entire response must strictly NOT exceed 50 words." in prompt
    assert "Compact Override:" in prompt
    assert "<output_contract>" not in prompt
    assert "1 sentence stating your core verdict/concern." not in prompt
    assert "2-3 concise bullet points" not in prompt
    assert "1 concluding sentence with a concrete recommendation." not in prompt
    # Verify nomination rules and fixed language constraints are still present
    assert "nominate the next agent" in prompt
    assert "Aim to answer in the same language as user prompt is made." in prompt

    # Short output agent with tight limit: skips short default limit and 3-part bullets, strips <output_contract>
    prompt_short = build_agent_prompt("agent-10010", max_words=30)
    assert "Hard Limit: Your entire response must strictly NOT exceed 30 words." in prompt_short
    assert f"Hard Limit: Your entire response must strictly NOT exceed {short_max} words." not in prompt_short
    assert "<output_contract>" not in prompt_short
    assert "1 sentence stating your core verdict/concern." not in prompt_short
    assert "Aim to answer in the same language as user prompt is made." in prompt_short

    # Standard/large custom limit (max_words >= 60): keeps <output_contract> and Rules 2-4 with 120w ceiling
    prompt_arch = build_agent_prompt("agent-10008", max_words=120)
    assert "Hard Limit: Your entire response must strictly NOT exceed 120 words." in prompt_arch
    assert f"strictly NOT exceed {default_max} words" not in prompt_arch
    assert "<output_contract>" in prompt_arch
    assert "1 sentence stating your core verdict/concern." in prompt_arch
    assert "2-3 concise bullet points" in prompt_arch

    # Visionary with tight limit (max_words=10 / 45 < 60): strips <output_contract> so section headers aren't forced
    prompt_vis_10 = build_agent_prompt("agent-10001", max_words=10)
    assert "Hard Limit: Your entire response must strictly NOT exceed 10 words." in prompt_vis_10
    assert "<output_contract>" not in prompt_vis_10
    assert "Core Strategic Verdict" not in prompt_vis_10

    prompt_vis = build_agent_prompt("agent-10001", max_words=45)
    assert "strictly NOT exceed 45 words" in prompt_vis
    assert f"strictly NOT exceed {default_max} words" not in prompt_vis
    assert "<output_contract>" not in prompt_vis

    # Dialectical system agent (Challenger) with tight limit also strips <output_contract>
    prompt_challenger_tight = build_agent_prompt("Challenger", max_words=15)
    assert "Hard Limit: Your entire response must strictly NOT exceed 15 words." in prompt_challenger_tight
    assert "<output_contract>" not in prompt_challenger_tight


def test_build_agent_prompt_with_min_words():
    default_max = get_output_max_words(is_short=False)
    short_max = get_output_max_words(is_short=True)

    # min_words without max_words: keeps Rules 2-4 and <output_contract>, replaces hardcoded ceiling with min_words
    prompt = build_agent_prompt("agent-10011", min_words=60)
    assert "Minimum Length: Your entire response must have a minimum length of 60 words." in prompt
    assert f"Hard Limit: Your entire response must strictly NOT exceed {default_max} words." not in prompt
    assert "1 sentence stating your core verdict/concern." in prompt
    assert "2-3 concise bullet points" in prompt
    assert "<output_contract>" in prompt
    assert "nominate the next agent" in prompt

    # Short output agent with min_words only: replaces short default ceiling with min_words
    prompt_short = build_agent_prompt("agent-10010", min_words=40)
    assert "Minimum Length: Your entire response must have a minimum length of 40 words." in prompt_short
    assert f"Hard Limit: Your entire response must strictly NOT exceed {short_max} words." not in prompt_short
    assert "1 sentence stating your core verdict/concern." in prompt_short

    # SecurityArchitect: verify min_words addition
    prompt_arch = build_agent_prompt("agent-10008", min_words=150)
    assert "Minimum Length: Your entire response must have a minimum length of 150 words." in prompt_arch

    # Visionary: verify min_words addition
    prompt_vis = build_agent_prompt("agent-10001", min_words=50)
    assert "Minimum Length: Your entire response must have a minimum length of 50 words." in prompt_vis


def test_build_agent_prompt_with_min_and_max_words():
    # Both limits present with max_words >= 60: keeps Rules 2-4 and <output_contract>
    prompt = build_agent_prompt("agent-10011", min_words=50, max_words=150)
    assert "Minimum Length: Your entire response must have a minimum length of 50 words." in prompt
    assert "Hard Limit: Your entire response must strictly NOT exceed 150 words." in prompt
    assert "1 sentence stating your core verdict/concern." in prompt
    assert "<output_contract>" in prompt

    prompt_arch = build_agent_prompt("agent-10008", min_words=80, max_words=300)
    assert "Minimum Length: Your entire response must have a minimum length of 80 words." in prompt_arch
    assert "strictly NOT exceed 300 words" in prompt_arch
    assert "1 sentence stating your core verdict/concern." in prompt_arch

    # Both limits present with max_words < 60: compact mode strips <output_contract> and 3-part bullets
    prompt_tight = build_agent_prompt("agent-10011", min_words=10, max_words=40)
    assert "Minimum Length: Your entire response must have a minimum length of 10 words." in prompt_tight
    assert "Hard Limit: Your entire response must strictly NOT exceed 40 words." in prompt_tight
    assert "<output_contract>" not in prompt_tight
    assert "1 sentence stating your core verdict/concern." not in prompt_tight


def test_system_agent_detection_and_prompts():
    from prompts.builder import is_system_agent

    # Metadata detection
    assert is_system_agent("Orchestrator") is True
    assert is_system_agent("orchestrator") is True
    assert is_system_agent("agent-10008") is False
    assert is_system_agent("agent-10011") is False

    # Prompts for system agent (e.g. InternalRevisor) must be self-contained (no nomination rules or debate output constraints)
    prompt = build_agent_prompt("InternalRevisor")
    assert "OUTPUT & LENGTH CONSTRAINTS:" not in prompt
    assert "nominate the next agent" not in prompt

    # Dialectical system agents (Challenger and Arbitrator) must have shared debate constraints and nomination rules
    default_max = get_output_max_words(is_short=False)
    prompt_challenger = build_agent_prompt("Challenger")
    assert '<system_agent_specification id="Challenger" role="Dialectical Stress-Test Challenger"' in prompt_challenger
    assert f"Hard Limit: Your entire response must strictly NOT exceed {default_max} words." in prompt_challenger
    assert "nominate the next agent" in prompt_challenger

    prompt_arbitrator = build_agent_prompt("Arbitrator")
    assert '<system_agent_specification id="Arbitrator" role="Arbitrator"' in prompt_arbitrator
    assert f"Hard Limit: Your entire response must strictly NOT exceed {default_max} words." in prompt_arbitrator
    assert "nominate the next agent" in prompt_arbitrator


def test_configurable_output_constraints_from_system_settings():
    from orchestration.system_settings import set_output_constraints

    set_output_constraints(default_max_words=350, short_max_words=75)

    prompt_standard = build_agent_prompt("agent-10008")
    assert "Hard Limit: Your entire response must strictly NOT exceed 350 words." in prompt_standard
    assert "{max_words}" not in prompt_standard
    assert "<output_contract>" in prompt_standard
    assert "1 sentence stating your core verdict/concern." in prompt_standard

    prompt_short = build_agent_prompt("agent-10010")
    assert "Hard Limit: Your entire response must strictly NOT exceed 75 words." in prompt_short
    assert "{max_words}" not in prompt_short
    assert "<output_contract>" in prompt_short
    assert "1 sentence stating your core verdict/concern." in prompt_short

    prompt_challenger = build_agent_prompt("Challenger")
    assert "Hard Limit: Your entire response must strictly NOT exceed 350 words." in prompt_challenger

    # Configured limit < 60 triggers compact mode and strips <output_contract>
    set_output_constraints(default_max_words=45, short_max_words=35)

    compact_standard = build_agent_prompt("agent-10008")
    assert "Hard Limit: Your entire response must strictly NOT exceed 45 words." in compact_standard
    assert "Compact Override:" in compact_standard
    assert "<output_contract>" not in compact_standard
    assert "1 sentence stating your core verdict/concern." not in compact_standard

    compact_short = build_agent_prompt("agent-10010")
    assert "Hard Limit: Your entire response must strictly NOT exceed 35 words." in compact_short
    assert "Compact Override:" in compact_short
    assert "<output_contract>" not in compact_short

    # Explicit max_words / min_words override configured limits
    explicit_override = build_agent_prompt("agent-10010", max_words=150)
    assert "Hard Limit: Your entire response must strictly NOT exceed 150 words." in explicit_override
    assert "Compact Override:" not in explicit_override
    assert "<output_contract>" in explicit_override

    min_only_override = build_agent_prompt("agent-10008", min_words=80)
    assert "Minimum Length: Your entire response must have a minimum length of 80 words." in min_only_override
    assert "Hard Limit:" not in min_only_override
    assert "<output_contract>" in min_only_override
