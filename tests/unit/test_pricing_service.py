import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.config import get_orchestrator_fast_model, get_system_agent_model
from services.pricing_feeds import (
    LITELLM_PRICING_JSON_URL,
    OPENROUTER_MODELS_API_URL,
    _lookup_litellm_pricing,
    _lookup_openrouter_pricing,
    clear_feed_cache,
    configure_feed_fetcher,
    fetch_pricing_from_feeds,
    reset_feed_fetcher,
)
from services.pricing_service import (
    MAX_VALID_PRICE_PER_1M,
    MIN_VALID_PRICE_PER_1M,
    PRICING_TOOL_SCHEMAS,
    PricingService,
    _is_valid_price_pair,
    get_baseline_for_model,
    load_baseline_pricing,
    pricing_service,
)


@pytest.fixture(autouse=True)
def clean_pricing_cache(tmp_path):
    temp_cache = tmp_path / "test_model_pricing_cache.json"
    pricing_service.configure_paths(cache_file=temp_cache)
    clear_feed_cache()
    yield
    clear_feed_cache()
    reset_feed_fetcher()
    pricing_service.reset_paths()
    if temp_cache.exists():
        temp_cache.unlink()


def test_load_baseline_pricing_from_json():
    """Verify load_baseline_pricing loads data/pricing/baseline_prices.json correctly."""
    catalog = load_baseline_pricing()
    assert isinstance(catalog, dict)
    assert "gemini/gemini-4-hydrogen" in catalog
    assert "openai/gpt-6.2-astra" in catalog
    assert "anthropic/claude-opus-5-5" in catalog
    assert "mistral/mistral-large-latest" in catalog

    for cfg in catalog.values():
        assert "input_price_per_1m" in cfg
        assert "output_price_per_1m" in cfg
        assert cfg["currency"] == "USD"
        assert "source" in cfg
        assert "source_url" in cfg


def test_load_baseline_pricing_missing_file(tmp_path):
    """Verify missing baseline config file returns empty dict."""
    missing = tmp_path / "nonexistent_baseline.json"
    catalog = load_baseline_pricing(missing)
    assert catalog == {}


def test_get_baseline_for_model():
    gemini_base = get_baseline_for_model("gemini/gemini-4-hydrogen")
    assert gemini_base["input_price_per_1m"] == 1.50
    assert gemini_base["output_price_per_1m"] == 7.50
    assert "https://ai.google.dev/pricing" in gemini_base["source_url"]

    mistral_base = get_baseline_for_model("mistral/mistral-large-latest")
    assert mistral_base["input_price_per_1m"] == 0.70
    assert mistral_base["output_price_per_1m"] == 2.10
    assert "https://mistral.ai" in mistral_base["source_url"]

    # TKT-907: Unknown models fail-expensive with 15.00 / 75.00
    unknown_base = get_baseline_for_model("custom/unknown-model-xyz")
    assert unknown_base["input_price_per_1m"] == 15.00
    assert unknown_base["output_price_per_1m"] == 75.00
    assert "source_url" in unknown_base


def test_get_baseline_from_litellm_model_cost():
    """TKT-907: If not in baseline JSON, query litellm.model_cost community registry."""
    mock_cost = {
        "special-unlisted-model": {
            "input_cost_per_token": 0.000003,  # $3.00 / 1M
            "output_cost_per_token": 0.000015,  # $15.00 / 1M
        }
    }
    res = get_baseline_for_model("special-unlisted-model", model_cost_registry=mock_cost)
    assert res["input_price_per_1m"] == 3.00
    assert res["output_price_per_1m"] == 15.00
    assert "LiteLLM Community Registry" in res["source"]


def test_pricing_bounds_validation():
    """TKT-907: Verify bounds validation between MIN and MAX valid prices."""
    assert _is_valid_price_pair(0.75, 3.75) is True
    assert _is_valid_price_pair(MIN_VALID_PRICE_PER_1M, MAX_VALID_PRICE_PER_1M) is True
    # Below min or above max
    assert _is_valid_price_pair(0.001, 3.75) is False
    assert _is_valid_price_pair(0.75, 600.0) is False
    assert _is_valid_price_pair(None, 3.75) is False
    assert _is_valid_price_pair("invalid", 3.75) is False


def test_pricing_tool_schemas():
    tool_names = [tool["function"]["name"] for tool in PRICING_TOOL_SCHEMAS]
    assert "web_search" in tool_names
    assert "fetch_web_page" in tool_names
    fetch_tool = next(t for t in PRICING_TOOL_SCHEMAS if t["function"]["name"] == "fetch_web_page")
    assert fetch_tool["function"]["parameters"]["properties"]["max_chars"]["default"] == 12000


def test_litellm_live_feed_lookup():
    """Verify strict canonical lookup in LiteLLM JSON without cross-provider leaks."""
    payload = {
        "gemini/gemini-4-hydrogen": {
            "input_cost_per_token": 0.0000015,
            "output_cost_per_token": 0.0000075,
        },
        "together_ai/deepseek-ai/DeepSeek-V4-Pro-0813": {
            "input_cost_per_token": 0.00000132,
            "output_cost_per_token": 0.00000396,
        },
        "openai/gpt-oss-120b": {
            "input_cost_per_token": 0.000009,
            "output_cost_per_token": 0.000027,
        },
        "anthropic/claude-opus-5.5": {
            "input_cost_per_token": 0.000004,
            "output_cost_per_token": 0.000020,
        },
    }

    gem = _lookup_litellm_pricing("gemini/gemini-4-hydrogen", payload)
    assert gem is not None
    assert gem["input_price_per_1m"] == 1.50
    assert gem["output_price_per_1m"] == 7.50
    assert gem["source"] == "LiteLLM Live Pricing Feed"

    tog = _lookup_litellm_pricing("together_ai/deepseek-ai/DeepSeek-V4-Pro-0813", payload)
    assert tog is not None
    assert tog["input_price_per_1m"] == 1.32
    assert tog["output_price_per_1m"] == 3.96

    # Never strip groq/ prefix onto openai/gpt-oss-120b
    assert _lookup_litellm_pricing("groq/openai/gpt-oss-120b", payload) is None

    # Anthropic dash/dot version separator normalization
    anth = _lookup_litellm_pricing("anthropic/claude-opus-5-5", payload)
    assert anth is not None
    assert anth["input_price_per_1m"] == 4.00
    assert anth["output_price_per_1m"] == 20.00


def test_openrouter_live_feed_lookup():
    """Verify OpenRouter /api/v1/models lookup maps canonical provider IDs and skips invalid entries."""
    payload = {
        "data": [
            {
                "id": "openai/gpt-6.2-astra",
                "pricing": {"prompt": "0.000008", "completion": "0.00004"},
            },
            {
                "id": "google/gemini-4-hydrogen",
                "pricing": {"prompt": "0.0000015", "completion": "0.0000075"},
            },
            {
                "id": "openrouter/auto",
                "pricing": {"prompt": "-1", "completion": "-1"},
            },
            {
                "id": "google/gemini-4-hydrogen:free",
                "pricing": {"prompt": "0", "completion": "0"},
            },
        ]
    }

    or_res = _lookup_openrouter_pricing("openrouter/openai/gpt-6.2-astra", payload)
    assert or_res is not None
    assert or_res["input_price_per_1m"] == 8.00
    assert or_res["output_price_per_1m"] == 40.00
    assert or_res["source"] == "OpenRouter Live Models API"

    gem_res = _lookup_openrouter_pricing("gemini/gemini-4-hydrogen", payload)
    assert gem_res is not None
    assert gem_res["input_price_per_1m"] == 1.50
    assert gem_res["output_price_per_1m"] == 7.50

    assert _lookup_openrouter_pricing("openrouter/auto", payload) is None
    assert _lookup_openrouter_pricing("groq/openai/gpt-oss-120b", payload) is None
    assert _lookup_openrouter_pricing("together_ai/deepseek-ai/DeepSeek-V4-Pro-0813", payload) is None


@pytest.mark.anyio
async def test_pricing_feeds_batch_cache_reuses_downloaded_payload():
    """Verify fetch_pricing_from_feeds downloads each feed once per batch until cleared."""
    calls: list[str] = []

    async def fake_feed_fetcher(url: str):
        calls.append(url)
        if url == LITELLM_PRICING_JSON_URL:
            return {
                "gemini/gemini-4-hydrogen": {
                    "input_cost_per_token": 0.0000015,
                    "output_cost_per_token": 0.0000075,
                },
                "openai/gpt-6.1-terra": {
                    "input_cost_per_token": 0.000001,
                    "output_cost_per_token": 0.000005,
                },
            }
        if url == OPENROUTER_MODELS_API_URL:
            return {"data": []}
        return None

    configure_feed_fetcher(fake_feed_fetcher)
    r1 = await fetch_pricing_from_feeds("gemini/gemini-4-hydrogen")
    r2 = await fetch_pricing_from_feeds("openai/gpt-6.1-terra")

    assert r1 is not None and r1["input_price_per_1m"] == 1.50
    assert r2 is not None and r2["input_price_per_1m"] == 1.00
    assert calls == [LITELLM_PRICING_JSON_URL]

    clear_feed_cache()
    await fetch_pricing_from_feeds("gemini/gemini-4-hydrogen")
    assert calls == [LITELLM_PRICING_JSON_URL, LITELLM_PRICING_JSON_URL]


@pytest.mark.anyio
async def test_fetch_pricing_with_agent_success():
    """Verify multi-turn tool loop: web_search -> fetch_web_page (default max_chars=12000) -> JSON."""
    search_tool_call = SimpleNamespace(
        id="call_web_search_1",
        function=SimpleNamespace(
            name="web_search",
            arguments=json.dumps({"query": "gemini-4-hydrogen API pricing per 1M tokens", "max_results": 3}),
        ),
    )
    round_1_response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    role="assistant",
                    content=None,
                    tool_calls=[search_tool_call],
                )
            )
        ]
    )

    fetch_tool_call = SimpleNamespace(
        id="call_fetch_web_page_2",
        function=SimpleNamespace(
            name="fetch_web_page",
            arguments=json.dumps({"url": "https://ai.google.dev/pricing"}),
        ),
    )
    round_2_response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    role="assistant",
                    content=None,
                    tool_calls=[fetch_tool_call],
                )
            )
        ]
    )

    agent_json_output = json.dumps(
        {
            "model_name": "gemini/gemini-4-hydrogen",
            "verified": True,
            "input_price_per_1m": 1.50,
            "output_price_per_1m": 7.50,
            "currency": "USD",
            "source": "Google AI Pricing Docs",
            "source_url": "https://ai.google.dev/pricing",
        }
    )
    round_3_response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    role="assistant",
                    content=f"```json\n{agent_json_output}\n```",
                    tool_calls=None,
                )
            )
        ]
    )

    mock_search_json = json.dumps(
        {
            "results": [
                {
                    "title": "Gemini API Pricing",
                    "snippet": "See official pricing table.",
                    "url": "https://ai.google.dev/pricing",
                }
            ]
        }
    )
    mock_page_json = json.dumps(
        {
            "url": "https://ai.google.dev/pricing",
            "content": "Gemini 4 Hydrogen: $1.50 per 1M input tokens, $7.50 per 1M output tokens.",
        }
    )

    with patch(
        "litellm.acompletion",
        new_callable=AsyncMock,
        side_effect=[round_1_response, round_2_response, round_3_response],
    ) as mock_completion, patch(
        "services.pricing_service.web_search", new_callable=AsyncMock, return_value=mock_search_json
    ) as mock_search, patch(
        "services.pricing_service.fetch_web_page", new_callable=AsyncMock, return_value=mock_page_json
    ) as mock_fetch_page:
        res = await pricing_service.fetch_pricing_with_agent("gemini/gemini-4-hydrogen")

        assert res["input_price_per_1m"] == 1.50
        assert res["output_price_per_1m"] == 7.50
        assert res["currency"] == "USD"
        assert res["source"] == "Google AI Pricing Docs"
        assert res["source_url"] == "https://ai.google.dev/pricing"
        assert "is_fallback" not in res
        assert "updated_at" in res

        assert mock_completion.call_count == 3
        first_call_kwargs = mock_completion.call_args_list[0].kwargs
        expected_model = get_system_agent_model("PricingAgent", fallback=get_orchestrator_fast_model())
        assert first_call_kwargs["model"] == expected_model
        assert first_call_kwargs["tools"] == PRICING_TOOL_SCHEMAS

        mock_search.assert_called_once_with(
            query="gemini-4-hydrogen API pricing per 1M tokens",
            max_results=3,
        )
        mock_fetch_page.assert_called_once_with(
            url="https://ai.google.dev/pricing",
            max_chars=12000,
        )


@pytest.mark.anyio
async def test_fetch_pricing_from_web_feed_priority_and_clean_fallback():
    """Structured feeds short-circuit agent; feed miss calls agent; agent failure returns clean baseline fallback."""
    feed_hit = {
        "input_price_per_1m": 1.50,
        "output_price_per_1m": 7.50,
        "currency": "USD",
        "source": "LiteLLM Live Pricing Feed",
        "source_url": LITELLM_PRICING_JSON_URL,
        "updated_at": time.time(),
    }
    agent_mock = AsyncMock()
    res_feed = await pricing_service.fetch_pricing_from_web(
        "gemini/gemini-4-hydrogen",
        feed_fetcher=AsyncMock(return_value=feed_hit),
        agent_fetcher=agent_mock,
    )
    assert res_feed["source"] == "LiteLLM Live Pricing Feed"
    agent_mock.assert_not_called()

    # Feed miss delegates to agent
    agent_hit = {
        "input_price_per_1m": 1.50,
        "output_price_per_1m": 7.50,
        "currency": "USD",
        "source": "Google AI Pricing Docs",
        "source_url": "https://ai.google.dev/pricing",
        "updated_at": time.time(),
    }
    res_agent = await pricing_service.fetch_pricing_from_web(
        "gemini/gemini-4-hydrogen",
        feed_fetcher=AsyncMock(return_value=None),
        agent_fetcher=AsyncMock(return_value=agent_hit),
    )
    assert res_agent["source"] == "Google AI Pricing Docs"
    assert "is_fallback" not in res_agent

    # Agent error returns clean baseline fallback (never regex scraping)
    res_err = await pricing_service.fetch_pricing_from_web(
        "gemini/gemini-4-hydrogen",
        feed_fetcher=AsyncMock(return_value=None),
        agent_fetcher=AsyncMock(side_effect=Exception("LiteLLM connection error")),
    )
    assert res_err["is_fallback"] is True
    assert res_err["input_price_per_1m"] == 1.50
    assert res_err["output_price_per_1m"] == 7.50
    assert res_err["source"] == "Google AI Documentation"


@pytest.mark.anyio
async def test_fetch_pricing_with_agent_unverified_and_out_of_bounds_return_fallback(tmp_path):
    """Unverified or out-of-bounds agent outputs must return is_fallback=True and NOT poison cache."""
    tool_call = SimpleNamespace(
        id="call_ws",
        function=SimpleNamespace(name="web_search", arguments=json.dumps({"query": "gemini-4-hydrogen pricing"})),
    )
    r1 = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(role="assistant", content=None, tool_calls=[tool_call]))]
    )
    r2_unverified = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    role="assistant",
                    content=json.dumps(
                        {
                            "model_name": "gemini/gemini-4-hydrogen",
                            "verified": False,
                            "input_price_per_1m": None,
                            "output_price_per_1m": None,
                            "currency": "USD",
                            "source": "Unverified",
                            "source_url": "https://ai.google.dev/pricing",
                        }
                    ),
                    tool_calls=None,
                )
            )
        ]
    )
    r2_oob = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    role="assistant",
                    content=json.dumps(
                        {
                            "model_name": "gemini/gemini-4-hydrogen",
                            "verified": True,
                            "input_price_per_1m": 50.0,  # > 10x baseline 1.50
                            "output_price_per_1m": 7.50,
                            "currency": "USD",
                            "source": "Bad Scrape",
                            "source_url": "https://example.com",
                        }
                    ),
                    tool_calls=None,
                )
            )
        ]
    )

    with patch("litellm.acompletion", new_callable=AsyncMock, side_effect=[r1, r2_unverified]), patch(
        "services.pricing_service.web_search", new_callable=AsyncMock, return_value="{}"
    ):
        res_unverified = await pricing_service.fetch_pricing_with_agent("gemini/gemini-4-hydrogen")
    assert res_unverified["is_fallback"] is True
    assert res_unverified["input_price_per_1m"] == 1.50

    cache_path = tmp_path / "no_poison_cache.json"
    svc = PricingService(cache_file=cache_path)
    with patch("litellm.acompletion", new_callable=AsyncMock, side_effect=[r1, r2_oob]), patch(
        "services.pricing_service.web_search", new_callable=AsyncMock, return_value="{}"
    ):
        res_oob = await svc.refresh_pricing("gemini/gemini-4-hydrogen")
    assert res_oob["is_fallback"] is True
    assert res_oob["input_price_per_1m"] == 1.50
    assert not cache_path.exists()


@pytest.mark.anyio
async def test_7_day_cache_ttl():
    model = "gemini/gemini-4-hydrogen"
    now = time.time()

    # Pre-populate cache with an entry from 2 days ago (< 7 days)
    cached_data = {
        model: {
            "input_price_per_1m": 1.50,
            "output_price_per_1m": 7.50,
            "currency": "USD",
            "source": "Initial Test Cache",
            "updated_at": now - (2 * 24 * 3600),
        }
    }
    pricing_service.save_cache(cached_data)

    mock_fetch = AsyncMock()
    pricing = await pricing_service.refresh_pricing(model, force=False, web_fetcher=mock_fetch)
    assert pricing["source"] == "Initial Test Cache"
    mock_fetch.assert_not_called()

    # If cache is 8 days old (> 7 days), it must trigger fresh web fetch
    stale_data = {
        model: {
            "input_price_per_1m": 1.00,
            "output_price_per_1m": 5.00,
            "currency": "USD",
            "source": "Stale 8-Day Cache",
            "updated_at": now - (8 * 24 * 3600),
        }
    }
    pricing_service.save_cache(stale_data)

    mock_fresh = {
        "input_price_per_1m": 1.50,
        "output_price_per_1m": 7.50,
        "currency": "USD",
        "source": "Refreshed Web Search",
        "updated_at": now,
    }
    mock_fetch = AsyncMock(return_value=mock_fresh)
    refreshed = await pricing_service.refresh_pricing(model, force=False, web_fetcher=mock_fetch)
    assert refreshed["source"] == "Refreshed Web Search"
    mock_fetch.assert_called_once()


@pytest.mark.anyio
async def test_refresh_pricing_when_weekly_update_disabled_and_price_already_set():
    """
    When weekly pricing update is OFF, if price is already set per model,
    it must NOT update from web even if cache is older than 7 days.
    """
    from config_store.app_settings import app_settings_manager

    app_settings_manager.set_weekly_pricing_update_enabled(False, save=False)
    model = "openai/gpt-6.1-terra"
    now = time.time()

    cached_data = {
        model: {
            "input_price_per_1m": 2.0,
            "output_price_per_1m": 12.0,
            "currency": "USD",
            "source": "Cached Terra Pricing",
            "updated_at": now - (10 * 24 * 3600),  # 10 days old (> 7 days)
        }
    }
    pricing_service.save_cache(cached_data)

    mock_fetch = AsyncMock()
    pricing = await pricing_service.refresh_pricing(model, force=False, web_fetcher=mock_fetch)
    assert pricing["source"] == "Cached Terra Pricing"
    assert pricing["input_price_per_1m"] == 2.0
    assert pricing["output_price_per_1m"] == 12.0
    mock_fetch.assert_not_called()


@pytest.mark.anyio
async def test_refresh_pricing_when_weekly_update_disabled_but_no_price_set():
    """
    When weekly pricing update is OFF, but there is NO price per token set
    for the model (missing from cache or null price), it MUST still update from web.
    """
    from config_store.app_settings import app_settings_manager

    app_settings_manager.set_weekly_pricing_update_enabled(False, save=False)
    model = "custom/new-unpriced-model"
    now = time.time()

    # Model not in cache at all
    mock_fresh = {
        "input_price_per_1m": 1.50,
        "output_price_per_1m": 4.50,
        "currency": "USD",
        "source": "Web Search Fetched",
        "updated_at": now,
    }

    mock_fetch = AsyncMock(return_value=mock_fresh)
    pricing = await pricing_service.refresh_pricing(model, force=False, web_fetcher=mock_fetch)
    assert pricing["source"] == "Web Search Fetched"
    assert pricing["input_price_per_1m"] == 1.50
    assert pricing["output_price_per_1m"] == 4.50
    mock_fetch.assert_called_once()

    # Model in cache but price is None
    cached_data = {
        model: {
            "input_price_per_1m": None,
            "output_price_per_1m": None,
            "currency": "USD",
            "source": "Incomplete Cache",
            "updated_at": now - (1 * 24 * 3600),
        }
    }
    pricing_service.save_cache(cached_data)

    mock_fresh2 = {
        "input_price_per_1m": 1.75,
        "output_price_per_1m": 5.00,
        "currency": "USD",
        "source": "Web Search Second Fetch",
        "updated_at": now,
    }

    mock_fetch2 = AsyncMock(return_value=mock_fresh2)
    pricing = await pricing_service.refresh_pricing(model, force=False, web_fetcher=mock_fetch2)
    assert pricing["source"] == "Web Search Second Fetch"
    assert pricing["input_price_per_1m"] == 1.75
    mock_fetch2.assert_called_once()


@pytest.mark.anyio
async def test_refresh_pricing_force_overrides_disabled_weekly_update():
    """
    Even when weekly pricing update is OFF, calling with force=True must fetch fresh pricing.
    """
    from config_store.app_settings import app_settings_manager

    app_settings_manager.set_weekly_pricing_update_enabled(False, save=False)
    model = "anthropic/claude-opus-5-5"
    now = time.time()

    cached_data = {
        model: {
            "input_price_per_1m": 15.0,
            "output_price_per_1m": 75.0,
            "currency": "USD",
            "source": "Old Cached Opus",
            "updated_at": now - (1 * 24 * 3600),
        }
    }
    pricing_service.save_cache(cached_data)

    mock_forced = {
        "input_price_per_1m": 10.0,
        "output_price_per_1m": 50.0,
        "currency": "USD",
        "source": "Forced Refresh",
        "updated_at": now,
    }

    mock_fetch = AsyncMock(return_value=mock_forced)
    pricing = await pricing_service.refresh_pricing(model, force=True, web_fetcher=mock_fetch)
    assert pricing["source"] == "Forced Refresh"
    assert pricing["input_price_per_1m"] == 10.0
    mock_fetch.assert_called_once()


@pytest.mark.anyio
async def test_cache_hygiene_rejects_poisoned_and_stale_baseline_signature():
    """Verify legacy 'Web Search Fallback', baseline_signature mismatch, and prune_cache()."""
    now = time.time()
    pricing_service.save_cache(
        {
            "mistral/mistral-small-latest": {
                "input_price_per_1m": 0.50,
                "output_price_per_1m": 1.50,
                "currency": "USD",
                "source": "Web Search Fallback",
                "updated_at": now,
            },
            "gemini/gemini-4-hydrogen": {
                "input_price_per_1m": 9.99,
                "output_price_per_1m": 19.99,
                "currency": "USD",
                "source": "Old Live Feed",
                "baseline_signature": "0.990000:1.990000",  # Mismatches 1.50:7.50 baseline
                "updated_at": now,
            },
            "retired/old-model": {
                "input_price_per_1m": 1.0,
                "output_price_per_1m": 3.0,
                "currency": "USD",
                "source": "Old Cache",
                "updated_at": now,
            },
            "openai/gpt-6.1-terra": {
                "input_price_per_1m": 1.00,
                "output_price_per_1m": 5.00,
                "currency": "USD",
                "source": "LiteLLM Live Pricing Feed",
                "baseline_signature": "1.000000:5.000000",
                "updated_at": now,
            },
        }
    )

    # get_pricing ignores poisoned and baseline-mismatched entries, falling back to baseline
    mistral = pricing_service.get_pricing("mistral/mistral-small-latest")
    assert mistral["input_price_per_1m"] == 0.15
    assert mistral["output_price_per_1m"] == 0.60

    gem = pricing_service.get_pricing("gemini/gemini-4-hydrogen")
    assert gem["input_price_per_1m"] == 1.50
    assert gem["output_price_per_1m"] == 7.50

    # Valid entry with matching baseline_signature is preserved
    terra = pricing_service.get_pricing("openai/gpt-6.1-terra")
    assert terra["source"] == "LiteLLM Live Pricing Feed"

    # refresh_pricing refreshes poisoned entry even within TTL
    mock_fetch = AsyncMock(
        return_value={
            "input_price_per_1m": 0.15,
            "output_price_per_1m": 0.60,
            "currency": "USD",
            "source": "LiteLLM Live Pricing Feed",
            "updated_at": now,
        }
    )
    refreshed = await pricing_service.refresh_pricing("mistral/mistral-small-latest", web_fetcher=mock_fetch)
    mock_fetch.assert_called_once()
    assert refreshed["baseline_signature"] == "0.150000:0.600000"

    # prune_cache removes retired and baseline-stale entries while keeping valid known models
    removed = pricing_service.prune_cache(valid_models={"openai/gpt-6.1-terra", "mistral/mistral-small-latest"})
    assert removed == 2
    remaining = pricing_service.load_cache()
    assert set(remaining.keys()) == {"openai/gpt-6.1-terra", "mistral/mistral-small-latest"}


@pytest.mark.anyio
async def test_warm_up_cache_includes_discussion_scribe_and_pricing_agent_models(tmp_path):
    """Verify startup warm_up_cache includes dedicated DiscussionScribe and PricingAgent SSOT models."""
    class RecordingPricingService(PricingService):
        def __init__(self, cache_file):
            super().__init__(cache_file=cache_file)
            self.checked_models: list[str] = []

        async def refresh_pricing(self, model_name: str, force: bool = False, web_fetcher=None):
            self.checked_models.append(model_name)
            return {}

    svc = RecordingPricingService(cache_file=tmp_path / "warmup_pricing_cache.json")
    await svc.warm_up_cache()

    expected_scribe_model = get_system_agent_model("DiscussionScribe", fallback="mistral/mistral-small-latest")
    expected_pricing_model = get_system_agent_model("PricingAgent", fallback=get_orchestrator_fast_model())
    assert expected_scribe_model in svc.checked_models
    assert expected_pricing_model in svc.checked_models


@pytest.mark.anyio
async def test_fetch_pricing_with_agent_falls_back_when_pricing_agent_key_missing():
    """Verify PricingService resolves PricingAgent fallback when its API key is missing."""
    from core.model_catalog import configure_env_provider

    configure_env_provider(lambda: {"MISTRAL_API_KEY": "valid-mistral-key"})
    resp = MagicMock()
    resp.choices = [
        MagicMock(
            message=MagicMock(
                content='{"model_name": "test-model", "verified": true, "input_price_per_1m": 1.0, "output_price_per_1m": 2.0, "currency": "USD", "source": "Web", "source_url": "https://test.com"}',
                tool_calls=None,
            )
        )
    ]
    resp.usage = MagicMock(prompt_tokens=20, completion_tokens=10)

    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=resp) as mock_comp:
        await pricing_service.fetch_pricing_with_agent("test-model")

    assert mock_comp.call_args.kwargs["model"] == "mistral/mistral-large-latest"


def test_get_baseline_for_model_lowercase_together_and_longest_prefix():
    """TKT-1219: mixed-case Together AI keys resolve accurately and exact registry beats shorter baseline prefix."""
    res = get_baseline_for_model("together_ai/deepseek-ai/DeepSeek-V4-Pro-0813")
    assert res["input_price_per_1m"] == 1.32
    assert res["output_price_per_1m"] == 3.96

    res_mini = get_baseline_for_model(
        "openai/gpt-6-mini",
        model_cost_registry={
            "openai/gpt-6-mini": {
                "input_cost_per_token": 0.0000001,
                "output_cost_per_token": 0.0000004,
            }
        },
    )
    assert res_mini["input_price_per_1m"] == 0.1
    assert res_mini["output_price_per_1m"] == 0.4
    assert res_mini["source"] == "LiteLLM Community Registry"


@pytest.mark.anyio
async def test_warm_up_cache_resets_web_budget_and_skips_fallback_cache_write(tmp_path):
    """TKT-1219: warm_up_cache resets web budget per model and unverified lookups do not poison cache JSON."""
    from services.web_tools import check_and_increment_web_budget, get_turn_web_fetch_count

    cache_path = tmp_path / "pricing_cache.json"
    svc = PricingService(cache_file=cache_path)

    seen_counts: list[int] = []
    tool_call = SimpleNamespace(
        id="call_ws",
        function=SimpleNamespace(name="web_search", arguments=json.dumps({"query": "probe pricing"})),
    )
    round_1 = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(role="assistant", content=None, tool_calls=[tool_call]))]
    )
    round_2 = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    role="assistant",
                    content=json.dumps({"verified": False, "input_price_per_1m": None, "output_price_per_1m": None}),
                    tool_calls=None,
                )
            )
        ]
    )

    async def fake_web_search(**kwargs):
        seen_counts.append(get_turn_web_fetch_count())
        check_and_increment_web_budget()
        return json.dumps({"results": []})

    # Pre-exhaust turn web budget before warm_up_cache runs
    for _ in range(5):
        try:
            check_and_increment_web_budget()
        except PermissionError:
            break

    with (
        patch("litellm.acompletion", new_callable=AsyncMock, side_effect=[round_1, round_2, round_1, round_2]),
        patch("services.pricing_service.web_search", side_effect=fake_web_search),
    ):
        await svc.warm_up_cache(models=["unknown/probe-one", "unknown/probe-two"])

    assert seen_counts == [0, 0]
    assert not cache_path.exists()
