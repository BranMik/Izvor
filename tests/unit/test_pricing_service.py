import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core.config import get_pricing_agent_model
import services.pricing_service as pricing_module
from services.pricing_service import (
    MAX_VALID_PRICE_PER_1M,
    MIN_VALID_PRICE_PER_1M,
    PRICING_TOOL_SCHEMAS,
    _extract_pricing_from_search_text,
    _is_valid_price_pair,
    get_baseline_for_model,
    load_baseline_pricing,
    pricing_service,
)


@pytest.fixture(autouse=True)
def clean_pricing_cache(tmp_path):
    temp_cache = tmp_path / "test_model_pricing_cache.json"
    pricing_service.configure_paths(cache_file=temp_cache)
    yield
    pricing_service.reset_paths()
    if temp_cache.exists():
        temp_cache.unlink()


def test_load_baseline_pricing_from_json():
    """Verify load_baseline_pricing loads data/pricing/baseline_prices.json correctly."""
    catalog = load_baseline_pricing()
    assert isinstance(catalog, dict)
    assert "gemini/gemini-3.8-flash" in catalog
    assert "openai/gpt-5.6-sol" in catalog
    assert "anthropic/claude-opus-5" in catalog
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
    gemini_base = get_baseline_for_model("gemini/gemini-3.8-flash")
    assert gemini_base["input_price_per_1m"] == 0.75
    assert gemini_base["output_price_per_1m"] == 3.75
    assert "https://ai.google.dev/pricing" in gemini_base["source_url"]

    mistral_base = get_baseline_for_model("mistral/mistral-large-latest")
    assert mistral_base["input_price_per_1m"] == 2.00
    assert mistral_base["output_price_per_1m"] == 6.00
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
            "input_cost_per_token": 0.000003,   # $3.00 / 1M
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


def test_extract_pricing_from_search_text():
    snippet_1 = "Google Gemini 3.8 Flash pricing: $0.75 per 1M input tokens and $3.75 per 1M output tokens."
    in_p, out_p = _extract_pricing_from_search_text(snippet_1, "gemini-3.8-flash")
    assert in_p == 0.75
    assert out_p == 3.75

    snippet_2 = "Mistral Large: Input prompt tokens: $2.00/1M, Output completion tokens: $6.00/1M"
    in_p2, out_p2 = _extract_pricing_from_search_text(snippet_2, "mistral-large")
    assert in_p2 == 2.00
    assert out_p2 == 6.00


@pytest.mark.anyio
async def test_fetch_pricing_with_agent_success():
    # 1. First LiteLLM response triggers tool_call for web_search
    mock_tool_call = SimpleNamespace(
        id="call_web_search_1",
        function=SimpleNamespace(
            name="web_search",
            arguments=json.dumps({"query": "gemini-3.8-flash API pricing per 1M tokens", "max_results": 3}),
        ),
    )
    first_response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    role="assistant",
                    content=None,
                    tool_calls=[mock_tool_call],
                )
            )
        ]
    )

    # 2. Second LiteLLM response returns structured JSON with accurate model pricing
    agent_json_output = json.dumps(
        {
            "model_name": "gemini/gemini-3.8-flash",
            "input_price_per_1m": 0.75,
            "output_price_per_1m": 3.75,
            "currency": "USD",
            "source": "Google AI Pricing Docs",
            "source_url": "https://ai.google.dev/pricing",
        }
    )
    second_response = SimpleNamespace(
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
                    "title": "Gemini 3.8 Flash Pricing",
                    "snippet": "Gemini 3.8 Flash: $0.75/1M input, $3.75/1M output.",
                    "url": "https://ai.google.dev/pricing",
                }
            ]
        }
    )

    with patch("litellm.acompletion", new_callable=AsyncMock, side_effect=[first_response, second_response]) as mock_completion, \
         patch("services.pricing_service.web_search", new_callable=AsyncMock, return_value=mock_search_json) as mock_search:

        res = await pricing_service.fetch_pricing_with_agent("gemini/gemini-3.8-flash")

        assert res["input_price_per_1m"] == 0.75
        assert res["output_price_per_1m"] == 3.75
        assert res["currency"] == "USD"
        assert res["source"] == "Google AI Pricing Docs"
        assert res["source_url"] == "https://ai.google.dev/pricing"
        assert "updated_at" in res

        # Verify litellm called with configured pricing agent model
        assert mock_completion.call_count == 2
        first_call_kwargs = mock_completion.call_args_list[0].kwargs
        assert first_call_kwargs["model"] == get_pricing_agent_model()
        assert first_call_kwargs["tools"] == PRICING_TOOL_SCHEMAS

        # Verify web_search was called with query requested by agent
        mock_search.assert_called_once_with(
            query="gemini-3.8-flash API pricing per 1M tokens",
            max_results=3,
        )



@pytest.mark.anyio
async def test_fetch_pricing_from_web_agent_fallback():
    # If agent raises an exception, fetch_pricing_from_web falls back to direct search
    mock_search_json = json.dumps(
        {
            "query": "gemini-3.8-flash API pricing cost per 1M input output tokens",
            "results": [
                {
                    "title": "Gemini Developer Pricing",
                    "snippet": "Gemini 3.8 Flash is priced at $0.75 per 1M input tokens and $3.75 per 1M output tokens.",
                    "url": "https://ai.google.dev/pricing",
                }
            ],
        }
    )

    res = await pricing_service.fetch_pricing_from_web(
        "gemini/gemini-3.8-flash",
        agent_fetcher=AsyncMock(side_effect=Exception("LiteLLM connection error")),
        search_fn=AsyncMock(return_value=mock_search_json),
    )
    assert res["input_price_per_1m"] == 0.75
    assert res["output_price_per_1m"] == 3.75
    assert "Web Search" in res["source"]
    assert res["source_url"] == "https://ai.google.dev/pricing"
    assert "updated_at" in res


@pytest.mark.anyio
async def test_7_day_cache_ttl():
    model = "gemini/gemini-3.8-flash"
    now = time.time()

    # Pre-populate cache with an entry from 2 days ago (< 7 days)
    cached_data = {
        model: {
            "input_price_per_1m": 0.75,
            "output_price_per_1m": 3.75,
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
            "input_price_per_1m": 0.50,
            "output_price_per_1m": 2.50,
            "currency": "USD",
            "source": "Stale 8-Day Cache",
            "updated_at": now - (8 * 24 * 3600),
        }
    }
    pricing_service.save_cache(stale_data)

    mock_fresh = {
        "input_price_per_1m": 0.75,
        "output_price_per_1m": 3.75,
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
    from orchestration.app_settings import app_settings_manager

    app_settings_manager.set_weekly_pricing_update_enabled(False, save=False)
    model = "openai/gpt-5.6-terra"
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
    from orchestration.app_settings import app_settings_manager

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
    from orchestration.app_settings import app_settings_manager

    app_settings_manager.set_weekly_pricing_update_enabled(False, save=False)
    model = "anthropic/claude-opus-5"
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

