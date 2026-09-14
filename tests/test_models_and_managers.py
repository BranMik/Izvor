import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agents.usage_agent import ThreadUsageTracker
from core.models import (
    CircuitBreakerConfig,
    ExtractedIntent,
    FileContext,
    HistoryLimitConfig,
    ModelPricing,
    OrchestrationMode,
)
from nlp.intent_extractor import IntentExtractor
from orchestration.agent_settings import AgentSettingsManager, settings_manager
from orchestration.nomination import (
    enforce_valid_nomination,
    get_initial_agents,
    parse_agent_and_prompt,
    resolve_target_file,
)
from orchestration.thread_state import ThreadStateManager
from pipeline.entity_resolver import entity_resolver
from services.pricing_service import PricingService


@pytest.fixture(autouse=True)
def setup_models_and_managers_env():
    settings_manager.reset()
    yield
    settings_manager.reset()


def test_models_serialization():
    # 1. FileContext
    fc = FileContext(
        file_name="app.py",
        content="print('hello')",
        size=14,
        truncated=False,
        authorized_agents={"Visionary"},
    )
    d = fc.to_dict()
    assert d["file_name"] == "app.py"
    assert d["authorized_agents"] == {"Visionary"}
    fc_restored = FileContext.from_dict(d)
    assert fc_restored.file_name == "app.py"
    assert fc_restored.authorized_agents == {"Visionary"}

    # 2. ExtractedIntent
    ei = ExtractedIntent(max_words=50, history=3, file_use=True, target_file="data.csv")
    d_ei = ei.to_dict()
    assert d_ei["max_words"] == 50
    assert d_ei["file_use"] is True
    ei_restored = ExtractedIntent.from_dict(d_ei)
    assert ei_restored.max_words == 50
    assert ei_restored.target_file == "data.csv"

    # 3. CircuitBreakerConfig
    cb = CircuitBreakerConfig(enabled=True, threshold_usd=2.5)
    d_cb = cb.to_dict()
    assert d_cb["enabled"] is True
    assert d_cb["threshold_usd"] == 2.5

    # 4. HistoryLimitConfig
    hl = HistoryLimitConfig(limit=5, prior_cutoff_index=10)
    d_hl = hl.to_dict()
    assert d_hl["limit"] == 5
    assert d_hl["prior_cutoff_index"] == 10

    # 5. ModelPricing
    mp = ModelPricing(
        input_price_per_1m=0.15,
        output_price_per_1m=0.60,
        currency="USD",
        source="Test Source",
        source_url="https://example.com",
    )
    d_mp = mp.to_dict()
    assert d_mp["input_price_per_1m"] == 0.15
    mp_restored = ModelPricing.from_dict(d_mp)
    assert mp_restored.source == "Test Source"


def test_agent_settings_manager():
    with tempfile.TemporaryDirectory() as tmp_dir:
        settings_file = Path(tmp_dir) / "test_settings.json"
        mgr = AgentSettingsManager(settings_file=settings_file)

        # Defaults
        assert "Visionary" in mgr.get_enabled_agents()
        assert mgr.is_circuit_breaker_enabled() is False
        assert mgr.get_orchestration_mode() == "fixed"

        # Toggle all agents freely (no locked agents)
        assert mgr.set_agent_enabled("Visionary", False) is True
        assert "Visionary" not in mgr.get_enabled_agents()
        assert mgr.set_agent_enabled("Innovator", False) is True
        assert "Innovator" not in mgr.get_enabled_agents()

        # Orchestration mode and chain configuration
        mgr.set_orchestration_mode(OrchestrationMode.FIXED)
        assert mgr.get_orchestration_mode() == "fixed"
        assert mgr.get_orchestration_mode() == OrchestrationMode.FIXED
        mgr.set_fixed_config(["TechLead", "Innovator"])
        assert mgr.get_fixed_config() == {"chain": ["TechLead", "Innovator"]}

        mgr.set_orchestration_mode(OrchestrationMode.SANDWICH)
        assert mgr.get_orchestration_mode() == "sandwich"
        assert mgr.get_orchestration_mode() == OrchestrationMode.SANDWICH
        mgr.set_sandwich_config(["Visionary"], ["Architect"])
        assert mgr.get_sandwich_config() == {
            "start_chain": ["Visionary"],
            "end_chain": ["Architect"],
        }

        # Circuit breaker settings
        mgr.set_circuit_breaker_enabled(True)
        mgr.set_circuit_breaker_threshold(3.75)
        assert mgr.is_circuit_breaker_enabled() is True
        assert mgr.get_circuit_breaker_threshold() == 3.75

        # Persistence reload
        mgr_reloaded = AgentSettingsManager(settings_file=settings_file)
        mgr_reloaded.load()
        assert mgr_reloaded.is_circuit_breaker_enabled() is True
        assert mgr_reloaded.get_circuit_breaker_threshold() == 3.75
        assert "Innovator" not in mgr_reloaded.get_enabled_agents()
        assert mgr_reloaded.get_orchestration_mode() == "fixed"
        assert mgr_reloaded.get_workflow_config() == "idea_architecture_feasibility_review"


def test_thread_state_manager():
    mgr = ThreadStateManager()
    channel_id = "C123"
    thread_ts = "1700000000.000100"

    # History limits
    mgr.set_history_limit(channel_id, thread_ts, limit=4, prior_cutoff_index=8)
    assert mgr.get_history_limit(channel_id, thread_ts) == {
        "limit": 4,
        "prior_cutoff_index": 8,
    }

    # Max / Min words
    mgr.set_max_words(channel_id, thread_ts, 100)
    mgr.set_min_words(channel_id, thread_ts, 50)
    assert mgr.get_max_words(channel_id, thread_ts) == 100
    assert mgr.get_min_words(channel_id, thread_ts) == 50

    # File context with FileContext dataclass
    fc = FileContext(file_name="schema.sql", content="CREATE TABLE ...", size=50)
    mgr.set_file_context(channel_id, thread_ts, fc)
    assert mgr.get_file_context(channel_id, thread_ts)["file_name"] == "schema.sql"

    # 503 tracking
    assert mgr.has_agent_503_errored(channel_id, thread_ts, "Visionary") is False
    mgr.mark_agent_503_errored(channel_id, thread_ts, "Visionary")
    assert mgr.has_agent_503_errored(channel_id, thread_ts, "Visionary") is True
    assert mgr.get_503_errored_agents(channel_id, thread_ts) == {"Visionary"}

    # Stop and clear
    mgr.request_stop(channel_id, thread_ts)
    assert mgr.is_stopped(channel_id, thread_ts) is True

    mgr.clear_state(channel_id, thread_ts)
    assert mgr.get_history_limit(channel_id, thread_ts) is None
    assert mgr.get_max_words(channel_id, thread_ts) is None
    assert mgr.get_file_context(channel_id, thread_ts) is None
    assert mgr.is_stopped(channel_id, thread_ts) is False
    assert mgr.has_agent_503_errored(channel_id, thread_ts, "Visionary") is False
    assert mgr.get_503_errored_agents(channel_id, thread_ts) == set()

    # Remaining agents operations
    assert mgr.get_remaining_agents(channel_id, thread_ts) == []
    mgr.set_remaining_agents(channel_id, thread_ts, ["Visionary", "Architect", "Critic"])
    assert mgr.get_remaining_agents(channel_id, thread_ts) == ["Visionary", "Architect", "Critic"]
    popped = mgr.pop_remaining_agent(channel_id, thread_ts)
    assert popped == "Visionary"
    assert mgr.get_remaining_agents(channel_id, thread_ts) == ["Architect", "Critic"]
    popped_specific = mgr.pop_remaining_agent(channel_id, thread_ts, "Critic")
    assert popped_specific == "Critic"
    assert mgr.get_remaining_agents(channel_id, thread_ts) == ["Architect"]
    mgr.clear_remaining_agents(channel_id, thread_ts)
    assert mgr.get_remaining_agents(channel_id, thread_ts) == []

    # Retry queue operations
    assert mgr.has_pending_retries(channel_id, thread_ts) is False
    mgr.add_to_retry_queue(channel_id, thread_ts, "TechLead")
    mgr.add_to_retry_queue(channel_id, thread_ts, "Innovator")
    assert mgr.has_pending_retries(channel_id, thread_ts) is True
    assert mgr.get_retry_queue(channel_id, thread_ts) == ["TechLead", "Innovator"]
    popped_retry = mgr.pop_retry_agent(channel_id, thread_ts)
    assert popped_retry == "TechLead"
    assert mgr.get_retry_queue(channel_id, thread_ts) == ["Innovator"]
    popped_retry_spec = mgr.pop_retry_agent(channel_id, thread_ts, "Innovator")
    assert popped_retry_spec == "Innovator"
    assert mgr.has_pending_retries(channel_id, thread_ts) is False

    # Already retried operations
    assert mgr.is_already_retried(channel_id, thread_ts, "TechLead") is False
    mgr.mark_already_retried(channel_id, thread_ts, "TechLead")
    assert mgr.is_already_retried(channel_id, thread_ts, "TechLead") is True
    assert mgr.get_already_retried(channel_id, thread_ts) == {"TechLead"}
    mgr.clear_already_retried(channel_id, thread_ts)
    assert mgr.is_already_retried(channel_id, thread_ts, "TechLead") is False

    # Active task operations
    fake_task = object()
    mgr.set_active_task(channel_id, thread_ts, fake_task)
    assert mgr.get_active_task(channel_id, thread_ts) is fake_task
    other_task = object()
    mgr.clear_active_task(channel_id, thread_ts, other_task)
    assert mgr.get_active_task(channel_id, thread_ts) is fake_task
    mgr.clear_active_task(channel_id, thread_ts, fake_task)
    assert mgr.get_active_task(channel_id, thread_ts) is None

    # Generations operations
    assert mgr.get_generation(channel_id, thread_ts) == 0
    gen1 = mgr.increment_generation(channel_id, thread_ts)
    assert gen1 == 1
    assert mgr.get_generation(channel_id, thread_ts) == 1
    gen2 = mgr.increment_generation(channel_id, thread_ts)
    assert gen2 == 2

    # Custom flow operations
    assert mgr.is_custom_flow(channel_id, thread_ts) is False
    mgr.set_custom_flow(channel_id, thread_ts, True)
    assert mgr.is_custom_flow(channel_id, thread_ts) is True
    mgr.clear_custom_flow(channel_id, thread_ts)
    assert mgr.is_custom_flow(channel_id, thread_ts) is False

    # Reset all
    mgr.set_remaining_agents(channel_id, thread_ts, ["Visionary"])
    mgr.add_to_retry_queue(channel_id, thread_ts, "Architect")
    mgr.increment_generation(channel_id, thread_ts)
    mgr.reset_all()
    assert mgr.get_remaining_agents(channel_id, thread_ts) == []
    assert mgr.get_retry_queue(channel_id, thread_ts) == []
    assert mgr.get_generation(channel_id, thread_ts) == 0


def test_thread_state_manager_eviction():
    mgr = ThreadStateManager()
    channel = "C_TEST_EVICT"
    ts_inactive = "100.001"
    ts_active = "100.002"

    # Set state on inactive thread
    mgr.set_max_words(channel, ts_inactive, 150)
    mgr.set_history_limit(channel, ts_inactive, 5, 2)
    assert mgr.get_last_activity(channel, ts_inactive) is not None

    # Set state on active thread
    mgr.set_remaining_agents(channel, ts_active, ["Visionary"])
    assert mgr.is_discussion_active(channel, ts_active) is True

    # Simulate time passing by artificially backdating timestamps
    mgr.thread_last_activity[(channel, ts_inactive)] = 1000.0
    mgr.thread_last_activity[(channel, ts_active)] = 1000.0

    # Prune with TTL=60s
    pruned = mgr.cleanup_expired_threads(ttl_seconds=60.0)

    # Inactive thread should be evicted
    assert pruned == 1
    assert mgr.get_max_words(channel, ts_inactive) is None
    assert mgr.get_history_limit(channel, ts_inactive) is None
    assert (channel, ts_inactive) not in mgr.thread_last_activity

    # Active thread must be preserved
    assert mgr.get_remaining_agents(channel, ts_active) == ["Visionary"]
    assert (channel, ts_active) in mgr.thread_last_activity

    # Clean active thread and ensure it can be evicted once inactive
    mgr.clear_remaining_agents(channel, ts_active)
    assert mgr.is_discussion_active(channel, ts_active) is False
    pruned_2 = mgr.cleanup_expired_threads(ttl_seconds=60.0)
    assert pruned_2 == 1
    assert (channel, ts_active) not in mgr.thread_last_activity

    # Reset all cleans last activity
    mgr.set_max_words(channel, "999.001", 50)
    assert (channel, "999.001") in mgr.thread_last_activity
    mgr.reset_all()
    assert (channel, "999.001") not in mgr.thread_last_activity


def test_nomination_and_entity_resolver():
    # Initial agents with default settings (all enabled)
    initial = get_initial_agents()
    assert isinstance(initial, list)
    assert len(initial) == 17

    # Tag extraction with exact and partial tags
    tags = entity_resolver.extract_tagged_agents("Hello [Innovator] and [TechLead]")
    assert tags == ["Innovator", "TechLead"]

    partial_tags = entity_resolver.extract_tagged_agents("Hello [Vis] and [TechLead] and [Inn]")
    assert partial_tags == ["Visionary", "TechLead", "Innovator"]

    # Ambiguous tags should not be extracted as single valid agent tags
    ambig_tags = entity_resolver.extract_tagged_agents("Hello [Arch] and [Strategist]")
    assert ambig_tags == []

    # Detect ambiguous tags
    detected_ambig = entity_resolver.detect_ambiguous_tags("Hello [Arch] and [Vis]")
    assert len(detected_ambig) == 1
    assert detected_ambig[0][0] == "Arch"
    assert "Architect" in detected_ambig[0][1]
    assert "SecurityArchitect" in detected_ambig[0][1]

    # Detect unrecognized tags
    unrecognized = entity_resolver.detect_unrecognized_tags("Hello [NonExistentAgent] and [Vis] and [Link](http://a.com)")
    assert unrecognized == ["NonExistentAgent"]

    # Parse agent and prompt with partial tag containing spaces
    agent, prompt, explicit = parse_agent_and_prompt("what do you think [Inn]", "C1", "T1")
    assert agent == "Innovator"
    assert "[Inn]" not in prompt
    assert prompt == "what do you think"
    assert explicit is True

    # Parse agent and prompt with typo bracket [Inn}
    agent2, prompt2, explicit2 = parse_agent_and_prompt("what do you think [Inn}", "C1", "T1")
    assert agent2 == "Innovator"
    assert "[Inn}" not in prompt2
    assert prompt2 == "what do you think"
    assert explicit2 is True

    # Nomination enforcement
    res = enforce_valid_nomination(
        text="My thoughts on this architecture.",
        current_agent_key="Visionary",
        remaining=["Innovator"],
        force_termination=False,
    )
    assert "[Innovator]" in res

    # Nomination enforcement recognizing partial nomination
    res_partial_nom = enforce_valid_nomination(
        text="Over to you [Innovator] for your review.",
        current_agent_key="Visionary",
        remaining=["Innovator"],
        force_termination=False,
    )
    assert "[Innovator]" in res_partial_nom

    # Target file resolution
    files = [{"name": "notes.txt"}, {"name": "schema.sql"}]
    assert resolve_target_file(files, "schema.sql")["name"] == "schema.sql"
    assert resolve_target_file(files, None)["name"] == "schema.sql"


@pytest.mark.anyio
async def test_intent_extractor_class():
    extractor = IntentExtractor()

    # Skip logic
    assert extractor.should_skip("/stop") is True
    assert extractor.should_skip("/file: code.py", is_file_cmd=True) is False

    # Extract intent
    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(
            message=MagicMock(
                content=json.dumps(
                    {
                        "max_words": 75,
                        "history": 2,
                        "file_use": False,
                        "target_file": None,
                    }
                )
            )
        )
    ]

    with patch("nlp.intent_extractor.litellm.completion", return_value=mock_resp):
        intent = await extractor.extract_intent("keep it to 75 words using last 2 messages")
        assert isinstance(intent, ExtractedIntent)
        assert intent.max_words == 75
        assert intent.history == 2
        assert intent.file_use is False


def test_pricing_service_class():
    with tempfile.TemporaryDirectory() as tmp_dir:
        cache_file = Path(tmp_dir) / "test_pricing_cache.json"
        ps = PricingService(cache_file=cache_file)

        pricing = ps.get_pricing("gemini/gemini-3.8-flash")
        assert pricing["input_price_per_1m"] == 0.75
        assert pricing["output_price_per_1m"] == 3.75

        # Save cache
        ps.save_cache({"test-model": {"input_price_per_1m": 5.0, "output_price_per_1m": 15.0}})
        loaded = ps.load_cache()
        assert "test-model" in loaded
        assert loaded["test-model"]["input_price_per_1m"] == 5.0


def test_thread_usage_tracker_class():
    with tempfile.TemporaryDirectory() as tmp_dir:
        store_file = Path(tmp_dir) / "test_usage.json"
        tracker = ThreadUsageTracker(store_file=store_file)

        channel_id = "C999"
        thread_ts = "1700000000.123456"

        tracker.record_tokens(
            channel_id=channel_id,
            thread_ts=thread_ts,
            model_name="gemini/gemini-3.8-flash",
            prompt_tokens=1000,
            completion_tokens=500,
            channel_name="#general",
        )

        costs = tracker.calculate_thread_costs(channel_id, thread_ts)
        assert costs["grand_prompt_tokens"] == 1000
        assert costs["grand_completion_tokens"] == 500
        assert costs["grand_total_tokens"] == 1500
        assert costs["grand_total_cost_usd"] > 0.0

        total_usd = tracker.get_thread_total_cost_usd(channel_id, thread_ts)
        assert total_usd == costs["grand_total_cost_usd"]
