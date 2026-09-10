from unittest.mock import patch

import pytest

from pipeline.directive_parser import DirectiveParser
from pipeline.entity_resolver import EntityResolver
from pipeline.orchestrator import SandwichOrchestrator
from pipeline.spend_governance import SpendGovernance


@pytest.fixture
def mock_agents_config():
    return {
        "Visionary": {"username": "Visionary", "model": "gemini/gemini-3.8-flash"},
        "Architect": {"username": "Architect", "model": "openai/gpt-5.6-terra"},
        "TechLead": {"username": "TechLead", "model": "mistral/mistral-large-latest"},
        "SecurityArchitect": {"username": "Security Architect", "model": "mistral/mistral-large-latest"},
        "ScopeOptimizer": {"username": "Scope Optimizer", "model": "gemini/gemini-3.8-flash"},
        "ROIStrategist": {"username": "ROI Strategist", "model": "gemini/gemini-3.8-flash"},
        "DataAndAnalyticsStrategist": {"username": "Data And Analytics Strategist", "model": "gemini/gemini-3.8-flash"},
    }


# ==============================================================================
# 1. Entity Resolver Skill Tests
# ==============================================================================

def test_entity_resolver_exact_and_partial(mock_agents_config):
    resolver = EntityResolver(agents_config=mock_agents_config)

    # Exact match
    assert resolver.resolve_agent_name("Visionary") == "Visionary"
    assert resolver.resolve_agent_name("[Architect]") == "Architect"

    # Partial / abbreviation match
    assert resolver.resolve_agent_name("Tech") == "TechLead"
    assert resolver.resolve_agent_name("[Sec]") == "SecurityArchitect"

    # Ambiguity detection
    candidates = resolver.resolve_candidates("Strategist")
    assert len(candidates) >= 2
    assert "ROIStrategist" in candidates
    assert "DataAndAnalyticsStrategist" in candidates
    assert resolver.resolve_agent_name("Strategist") is None


def test_entity_resolver_ambiguous_and_unrecognized(mock_agents_config):
    resolver = EntityResolver(agents_config=mock_agents_config)

    text = "Let [Strategist] review the plan, and ignore [UnknownGuy] and [150/50]."
    ambiguous = resolver.detect_ambiguous_tags(text)
    assert len(ambiguous) == 1
    assert ambiguous[0][0] == "Strategist"

    unrecognized = resolver.detect_unrecognized_tags(text)
    assert "UnknownGuy" in unrecognized
    # Token counter [150/50] should NOT be flagged as unrecognized agent
    assert "[150/50]" not in unrecognized


def test_entity_resolver_extract_tags(mock_agents_config):
    resolver = EntityResolver(agents_config=mock_agents_config)
    text = "First [Visionary], then [Tech], and finally [Architect]."
    tagged = resolver.extract_tagged_agents(text)
    assert tagged == ["Visionary", "TechLead", "Architect"]


# ==============================================================================
# 2. Directive Parser Skill Tests
# ==============================================================================

@pytest.mark.anyio
async def test_directive_parser_slash_commands():
    parser = DirectiveParser()
    raw = "/max_words 40 /history 5 /exclude [TechLead] What is our roadmap?"
    directives = await parser.parse(raw)

    assert directives.max_words == 40
    assert directives.history_limit == 5
    assert "TechLead" in directives.excluded_agents
    assert "What is our roadmap?" in directives.cleaned_prompt
    assert not directives.is_stop


@pytest.mark.anyio
async def test_directive_parser_stop_command():
    parser = DirectiveParser()
    directives = await parser.parse("/stop", is_mention_or_dm=True)
    assert directives.is_stop is True


# ==============================================================================
# 3. Sandwich Orchestrator Skill Tests
# ==============================================================================

def test_sandwich_orchestrator_deterministic_phases(mock_agents_config):
    orchestrator = SandwichOrchestrator(
        mode="sandwich",
        start_chain=["Architect", "ScopeOptimizer"],
        end_chain=["Architect"],
        agents_map=mock_agents_config,
    )

    available = ["Architect", "ScopeOptimizer", "TechLead", "Visionary"]
    initial_seq = orchestrator.build_initial_sequence(available)
    assert initial_seq == ["Architect", "ScopeOptimizer", "TechLead", "Visionary", "Architect"]

    # Phase 1: Start Chain turn 1 (Architect)
    desig1, elig1 = orchestrator.get_phase_info(initial_seq)
    assert desig1 == "Architect"
    assert elig1 == ["Architect"]

    # Phase 1: Start Chain turn 2 (ScopeOptimizer)
    rem_start_2 = initial_seq[1:]
    desig2, elig2 = orchestrator.get_phase_info(rem_start_2)
    assert desig2 == "ScopeOptimizer"
    assert elig2 == ["ScopeOptimizer"]

    # Phase 2: Middle pool open debate with multiple agents
    rem_middle = rem_start_2[1:]  # ["TechLead", "Visionary", "Architect"]
    desig_mid, elig_mid = orchestrator.get_phase_info(rem_middle)
    assert desig_mid is None
    assert elig_mid == ["TechLead", "Visionary"]

    # Phase 2: Middle pool with single remaining specialist
    rem_mid_single = ["Visionary", "Architect"]
    desig_single, elig_single = orchestrator.get_phase_info(rem_mid_single)
    assert desig_single == "Visionary"
    assert elig_single == ["Visionary"]

    # Phase 3: End Chain
    rem_end = ["Architect"]
    desig_end, elig_end = orchestrator.get_phase_info(rem_end)
    assert desig_end == "Architect"
    assert elig_end == ["Architect"]


def test_sandwich_orchestrator_fixed_mode(mock_agents_config):
    orchestrator = SandwichOrchestrator(
        mode="fixed",
        fixed_chain=["Visionary", "TechLead"],
        agents_map=mock_agents_config,
    )

    available = ["Visionary", "TechLead"]
    seq = orchestrator.build_initial_sequence(available)
    assert seq == ["Visionary", "TechLead"]

    desig, elig = orchestrator.get_phase_info(seq)
    assert desig == "Visionary"
    assert elig == ["Visionary"]


# ==============================================================================
# 4. Spend Governance Skill Tests
# ==============================================================================

def test_spend_governance_ledger_and_circuit_breaker():
    gov = SpendGovernance(default_threshold_usd=0.05)
    session_id = "test-sess-123"

    with patch("pipeline.spend_governance.circuit_breaker.get_model_pricing") as mock_pricing:
        mock_pricing.return_value = {
            "input_price_per_1m": 1.0,
            "output_price_per_1m": 2.0,
        }

        # 10,000 prompt, 5,000 completion = 0.01 + 0.01 = $0.02
        gov.record_usage(session_id, "mock-model", 10_000, 5_000)
        metrics = gov.calculate_spend(session_id)
        assert metrics.total_cost_usd == pytest.approx(0.02, rel=1e-3)

        tripped, cost, _ = gov.is_budget_exceeded(session_id, threshold_usd=0.05)
        assert not tripped

        # Add more usage: 30,000 prompt, 20,000 completion = 0.03 + 0.04 = $0.07 (Total = $0.09)
        gov.record_usage(session_id, "mock-model", 30_000, 20_000)
        tripped, cost, _ = gov.is_budget_exceeded(session_id, threshold_usd=0.05)
        assert tripped
        assert cost == pytest.approx(0.09, rel=1e-3)
