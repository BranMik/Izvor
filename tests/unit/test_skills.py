from unittest.mock import patch

import pytest

from pipeline.directive_parser import DirectiveParser
from pipeline.entity_resolver import EntityResolver
from pipeline.orchestrator import SandwichOrchestrator
from pipeline.spend_governance import SpendGovernance


@pytest.fixture
def mock_agents_config():
    return {
        "agent-10001": {"username": "Visionary", "name": "Visionary", "model": "gemini/gemini-3.8-flash"},
        "agent-10008": {"username": "Security Architect", "name": "SecurityArchitect", "model": "openai/gpt-5.6-terra"},
        "agent-10011": {"username": "Legal Advisor", "name": "LegalAdvisor", "model": "mistral/mistral-large-latest"},
        "agent-10010": {"username": "Sales Advisor", "name": "SalesAdvisor", "model": "gemini/gemini-3.8-flash"},
        "agent-10005": {"username": "Scope Optimizer", "name": "ScopeOptimizer", "model": "gemini/gemini-3.8-flash"},
        "agent-10006": {"username": "ROI Strategist", "name": "ROIStrategist", "model": "gemini/gemini-3.8-flash"},
        "agent-10009": {"username": "Market And Competition Research Expert", "name": "MarketAndCompetitionResearchExpert", "model": "gemini/gemini-3.8-flash"},
    }


# ==============================================================================
# 1. Entity Resolver Skill Tests
# ==============================================================================

def test_entity_resolver_exact_and_partial(mock_agents_config):
    resolver = EntityResolver(agents_config=mock_agents_config)

    # Exact match
    assert resolver.resolve_agent_name("agent-10001") == "agent-10001"
    assert resolver.resolve_agent_name("[agent-10008]") == "agent-10008"

    # Partial / abbreviation match
    assert resolver.resolve_agent_name("Legal") == "agent-10011"
    assert resolver.resolve_agent_name("[Sec]") == "agent-10008"

    # Ambiguity detection — "Advisor" matches both Legal Advisor and Sales Advisor
    candidates = resolver.resolve_candidates("Advisor")
    assert len(candidates) >= 2
    assert "agent-10011" in candidates
    assert "agent-10010" in candidates
    assert resolver.resolve_agent_name("Advisor") is None


def test_entity_resolver_ambiguous_and_unrecognized(mock_agents_config):
    resolver = EntityResolver(agents_config=mock_agents_config)

    text = "Let /[Advisor] review the plan, and ignore /[UnknownGuy] and [150/50]."
    ambiguous = resolver.detect_ambiguous_tags(text)
    assert len(ambiguous) == 1
    assert ambiguous[0][0] == "Advisor"

    unrecognized = resolver.detect_unrecognized_tags(text)
    assert "UnknownGuy" in unrecognized
    # Token counter [150/50] should NOT be flagged as unrecognized agent
    assert "[150/50]" not in unrecognized

    # Ordinary brackets do NOT trigger anything
    plain_text = "Let [Strategist] review the plan, and ignore [UnknownGuy] and [150/50]."
    assert resolver.detect_ambiguous_tags(plain_text) == []
    assert resolver.detect_unrecognized_tags(plain_text) == []


def test_entity_resolver_extract_tags(mock_agents_config):
    resolver = EntityResolver(agents_config=mock_agents_config)
    text = "First /[agent-10001], then /[agent-10011], and finally /[agent-10008]."
    tagged = resolver.extract_tagged_agents(text)
    assert tagged == ["agent-10001", "agent-10011", "agent-10008"]

    # Plain brackets are ignored and not extracted as tagged agents
    plain_text = "First [agent-10001], then [agent-10011], and finally [agent-10008]."
    assert resolver.extract_tagged_agents(plain_text) == []


# ==============================================================================
# 2. Directive Parser Skill Tests
# ==============================================================================

@pytest.mark.anyio
async def test_directive_parser_slash_commands():
    parser = DirectiveParser()
    raw = "/max_words 40 /history 5 /exclude [agent-10011] What is our roadmap?"
    directives = await parser.parse(raw)

    assert directives.max_words == 40
    assert directives.history_limit == 5
    assert "agent-10011" in directives.excluded_agents
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
        start_chain=["agent-10008", "agent-10005"],
        end_chain=["agent-10008"],
        agents_map=mock_agents_config,
    )

    available = ["agent-10008", "agent-10005", "agent-10011", "agent-10001"]
    initial_seq = orchestrator.build_initial_sequence(available)
    assert initial_seq == ["agent-10008", "agent-10005", "agent-10011", "agent-10001", "agent-10008"]

    # Phase 1: Start Chain turn 1 (SecurityArchitect)
    desig1, elig1 = orchestrator.get_phase_info(initial_seq)
    assert desig1 == "agent-10008"
    assert elig1 == ["agent-10008"]

    # Phase 1: Start Chain turn 2 (ScopeOptimizer)
    rem_start_2 = initial_seq[1:]
    desig2, elig2 = orchestrator.get_phase_info(rem_start_2)
    assert desig2 == "agent-10005"
    assert elig2 == ["agent-10005"]

    # Phase 2: Middle pool open debate with multiple agents
    rem_middle = rem_start_2[1:]  # ["agent-10011", "agent-10001", "agent-10008"]
    desig_mid, elig_mid = orchestrator.get_phase_info(rem_middle)
    assert desig_mid is None
    assert elig_mid == ["agent-10011", "agent-10001"]

    # Phase 2: Middle pool with single remaining specialist
    rem_mid_single = ["agent-10001", "agent-10008"]
    desig_single, elig_single = orchestrator.get_phase_info(rem_mid_single)
    assert desig_single == "agent-10001"
    assert elig_single == ["agent-10001"]

    # Phase 3: End Chain
    rem_end = ["agent-10008"]
    desig_end, elig_end = orchestrator.get_phase_info(rem_end)
    assert desig_end == "agent-10008"
    assert elig_end == ["agent-10008"]


def test_sandwich_orchestrator_fixed_mode(mock_agents_config):
    orchestrator = SandwichOrchestrator(
        mode="fixed",
        fixed_chain=["agent-10001", "agent-10011"],
        agents_map=mock_agents_config,
    )

    available = ["agent-10001", "agent-10011"]
    seq = orchestrator.build_initial_sequence(available)
    assert seq == ["agent-10001", "agent-10011"]

    desig, elig = orchestrator.get_phase_info(seq)
    assert desig == "agent-10001"
    assert elig == ["agent-10001"]


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


def test_spend_governance_daily_budget_and_session_clearing():
    gov = SpendGovernance(default_threshold_usd=5.0, default_daily_budget_usd=0.10)
    session_1 = "sess-alpha"
    session_2 = "sess-beta"

    with patch("pipeline.spend_governance.circuit_breaker.get_model_pricing") as mock_pricing:
        mock_pricing.return_value = {
            "input_price_per_1m": 1.0,
            "output_price_per_1m": 2.0,
        }

        # 30,000 prompt, 20,000 completion = $0.07 in session 1
        gov.record_usage(session_1, "mock-model", 30_000, 20_000)
        assert gov.get_daily_spend() == pytest.approx(0.07, rel=1e-3)

        # Neither session nor daily exceeded yet
        tripped, _, _ = gov.is_budget_exceeded(session_1, threshold_usd=5.0, daily_budget_usd=0.10)
        assert not tripped

        # 20,000 prompt, 10,000 completion = $0.04 in session 2 (Daily total = $0.11)
        gov.record_usage(session_2, "mock-model", 20_000, 10_000)
        assert gov.get_daily_spend() == pytest.approx(0.11, rel=1e-3)

        # Session 2 cost is only $0.04, but daily budget ($0.10) is exceeded!
        tripped, cost, limit = gov.is_budget_exceeded(session_2, threshold_usd=5.0, daily_budget_usd=0.10)
        assert tripped
        assert cost == pytest.approx(0.11, rel=1e-3)
        assert limit == 0.10

        # Test session reservation eviction
        gov._active_reservations[session_1] = {"res1": 0.01}
        gov._active_reservations[session_2] = {"res2": 0.02}
        assert session_1 in gov._active_reservations
        assert session_2 in gov._active_reservations
        gov.clear_session(session_1)
        assert session_1 not in gov._active_reservations
        assert session_2 in gov._active_reservations

        # Test clear all sessions
        gov.clear_all_sessions()
        assert len(gov._active_reservations) == 0

