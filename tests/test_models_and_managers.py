import json
from pathlib import Path
import tempfile
from unittest.mock import MagicMock, patch
import pytest

from orchestration.agent_settings import AgentSettingsManager
from agents.usage_agent import ThreadUsageTracker
from orchestration.debate_orchestrator import DebateOrchestrator
from nlp.intent_extractor import IntentExtractor
from core.models import (
    CircuitBreakerConfig,
    ExtractedIntent,
    FileContext,
    HistoryLimitConfig,
    ModelPricing,
)
from services.pricing_service import PricingService
from orchestration.thread_state import ThreadStateManager
from orchestration.agent_settings import settings_manager


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
        authorized_agents={"Gemini_Visionary"},
    )
    d = fc.to_dict()
    assert d["file_name"] == "app.py"
    assert d["authorized_agents"] == {"Gemini_Visionary"}
    fc_restored = FileContext.from_dict(d)
    assert fc_restored.file_name == "app.py"
    assert fc_restored.authorized_agents == {"Gemini_Visionary"}

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
        assert "Gemini_Visionary" in mgr.get_enabled_agents()
        assert mgr.is_circuit_breaker_enabled() is False
        assert mgr.get_orchestration_mode() == "sandwich"

        # Toggle all agents freely (no locked agents)
        assert mgr.set_agent_enabled("Gemini_Visionary", False) is True
        assert "Gemini_Visionary" not in mgr.get_enabled_agents()
        assert mgr.set_agent_enabled("AnthrOpus_Innovator", False) is True
        assert "AnthrOpus_Innovator" not in mgr.get_enabled_agents()

        # Orchestration mode and chain configuration
        mgr.set_orchestration_mode("fixed")
        assert mgr.get_orchestration_mode() == "fixed"
        mgr.set_fixed_config(["MistLar_TechLead", "AnthrOpus_Innovator"])
        assert mgr.get_fixed_config() == {"chain": ["MistLar_TechLead", "AnthrOpus_Innovator"]}

        mgr.set_orchestration_mode("sandwich")
        mgr.set_sandwich_config(["Gemini_Visionary"], ["OpAiTerra_Architect"])
        assert mgr.get_sandwich_config() == {
            "start_chain": ["Gemini_Visionary"],
            "end_chain": ["OpAiTerra_Architect"],
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
        assert "AnthrOpus_Innovator" not in mgr_reloaded.get_enabled_agents()
        assert mgr_reloaded.get_orchestration_mode() == "sandwich"
        assert mgr_reloaded.get_sandwich_config()["start_chain"] == ["Gemini_Visionary"]


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
    assert mgr.has_agent_503_errored(channel_id, thread_ts, "Gemini_Visionary") is False
    mgr.mark_agent_503_errored(channel_id, thread_ts, "Gemini_Visionary")
    assert mgr.has_agent_503_errored(channel_id, thread_ts, "Gemini_Visionary") is True
    assert mgr.get_503_errored_agents(channel_id, thread_ts) == {"Gemini_Visionary"}

    # Stop and clear
    mgr.request_stop(channel_id, thread_ts)
    assert mgr.is_stopped(channel_id, thread_ts) is True

    mgr.clear_state(channel_id, thread_ts)
    assert mgr.get_history_limit(channel_id, thread_ts) is None
    assert mgr.get_max_words(channel_id, thread_ts) is None
    assert mgr.get_file_context(channel_id, thread_ts) is None
    assert mgr.is_stopped(channel_id, thread_ts) is False
    assert mgr.has_agent_503_errored(channel_id, thread_ts, "Gemini_Visionary") is False
    assert mgr.get_503_errored_agents(channel_id, thread_ts) == set()


def test_debate_orchestrator():
    orch = DebateOrchestrator()

    # Initial agents with default settings (all enabled)
    initial = orch.get_initial_agents()
    assert isinstance(initial, list)
    assert len(initial) == 17

    # Tag extraction with exact and partial tags
    tags = orch.extract_tagged_agents("Hello [AnthrOpus_Innovator] and [MistLar_TechLead]")
    assert tags == ["AnthrOpus_Innovator", "MistLar_TechLead"]

    partial_tags = orch.extract_tagged_agents("Hello [Gemini_Vis] and [TechLead] and [AnthrOpus Inn]")
    assert partial_tags == ["Gemini_Visionary", "MistLar_TechLead", "AnthrOpus_Innovator"]

    # Ambiguous tags should not be extracted as single valid agent tags
    ambig_tags = orch.extract_tagged_agents("Hello [Architect] and [Gemini]")
    assert ambig_tags == []

    # Detect ambiguous tags
    detected_ambig = orch.detect_ambiguous_tags("Hello [Architect] and [Gemini_Vis]")
    assert len(detected_ambig) == 1
    assert detected_ambig[0][0] == "Architect"
    assert "OpAiTerra_Architect" in detected_ambig[0][1]
    assert "MistLar_SecurityArchitect" in detected_ambig[0][1]

    # Detect unrecognized tags
    unrecognized = orch.detect_unrecognized_tags("Hello [NonExistentAgent] and [Gemini_Vis] and [Link](http://a.com)")
    assert unrecognized == ["NonExistentAgent"]

    # Parse agent and prompt with partial tag containing spaces
    agent, prompt, explicit = orch.parse_agent_and_prompt("what do you think [AnthrOpus Inn]", "C1", "T1")
    assert agent == "AnthrOpus_Innovator"
    assert "[AnthrOpus Inn]" not in prompt
    assert prompt == "what do you think"
    assert explicit is True

    # Parse agent and prompt with typo bracket [AnthrOpus Inn}
    agent2, prompt2, explicit2 = orch.parse_agent_and_prompt("what do you think [AnthrOpus Inn}", "C1", "T1")
    assert agent2 == "AnthrOpus_Innovator"
    assert "[AnthrOpus Inn}" not in prompt2
    assert prompt2 == "what do you think"
    assert explicit2 is True

    # Nomination enforcement
    res = orch.enforce_valid_nomination(
        text="My thoughts on this architecture.",
        current_agent_key="Gemini_Visionary",
        remaining=["AnthrOpus_Innovator"],
        force_termination=False,
    )
    assert "[AnthrOpus_Innovator]" in res

    # Nomination enforcement recognizing partial nomination
    res_partial_nom = orch.enforce_valid_nomination(
        text="Over to you [Innovator] for your review.",
        current_agent_key="Gemini_Visionary",
        remaining=["AnthrOpus_Innovator"],
        force_termination=False,
    )
    assert "[AnthrOpus_Innovator]" in res_partial_nom or "[Innovator]" in res_partial_nom

    # Target file resolution
    files = [{"name": "notes.txt"}, {"name": "schema.sql"}]
    assert orch.resolve_target_file(files, "schema.sql")["name"] == "schema.sql"
    assert orch.resolve_target_file(files, None)["name"] == "schema.sql"


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

        pricing = ps.get_pricing("gemini/gemini-3.7-flash")
        assert pricing["input_price_per_1m"] == 0.15
        assert pricing["output_price_per_1m"] == 0.60

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
            model_name="gemini/gemini-3.7-flash",
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
