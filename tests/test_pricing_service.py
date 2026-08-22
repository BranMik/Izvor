import json
import time
from unittest.mock import MagicMock, patch

import pytest

import pricing_service
from pricing_service import (
    CACHE_TTL_SECONDS,
    _extract_pricing_from_search_text,
    fetch_pricing_from_web,
    get_baseline_for_model,
    get_model_pricing,
    refresh_model_pricing,
    save_pricing_cache,
)


@pytest.fixture(autouse=True)
def clean_pricing_cache(monkeypatch, tmp_path):
    temp_cache = tmp_path / "test_model_pricing_cache.json"
    monkeypatch.setattr(pricing_service, "PRICING_CACHE_FILE", temp_cache)
    yield
    if temp_cache.exists():
        temp_cache.unlink()


def test_get_baseline_for_model():
    gemini_base = get_baseline_for_model("gemini/gemini-3.7-flash")
    assert gemini_base["input_price_per_1m"] == 0.15
    assert gemini_base["output_price_per_1m"] == 0.60
    assert "https://ai.google.dev/pricing" in gemini_base["source_url"]

    mistral_base = get_baseline_for_model("mistral/mistral-large-latest")
    assert mistral_base["input_price_per_1m"] == 2.00
    assert mistral_base["output_price_per_1m"] == 6.00
    assert "https://mistral.ai" in mistral_base["source_url"]

    unknown_base = get_baseline_for_model("custom/unknown-model-xyz")
    assert unknown_base["input_price_per_1m"] > 0
    assert unknown_base["output_price_per_1m"] > 0
    assert "source_url" in unknown_base


def test_extract_pricing_from_search_text():
    snippet_1 = "Google Gemini 3.7 Flash pricing: $0.15 per 1M input tokens and $0.60 per 1M output tokens."
    in_p, out_p = _extract_pricing_from_search_text(snippet_1, "gemini-3.7-flash")
    assert in_p == 0.15
    assert out_p == 0.60

    snippet_2 = "Mistral Large: Input prompt tokens: $2.00/1M, Output completion tokens: $6.00/1M"
    in_p2, out_p2 = _extract_pricing_from_search_text(snippet_2, "mistral-large")
    assert in_p2 == 2.00
    assert out_p2 == 6.00


@pytest.mark.anyio
async def test_fetch_pricing_from_web_success():
    mock_search_json = json.dumps(
        {
            "query": "gemini-3.7-flash API pricing cost per 1M input output tokens",
            "results": [
                {
                    "title": "Gemini Developer Pricing",
                    "snippet": "Gemini 3.7 Flash is priced at $0.15 per 1M input tokens and $0.60 per 1M output tokens for prompts under 128k.",
                    "url": "https://ai.google.dev/pricing",
                }
            ],
        }
    )

    with patch("pricing_service.web_search", return_value=mock_search_json):
        res = await fetch_pricing_from_web("gemini/gemini-3.7-flash")
        assert res["input_price_per_1m"] == 0.15
        assert res["output_price_per_1m"] == 0.60
        assert "Web Search" in res["source"]
        assert res["source_url"] == "https://ai.google.dev/pricing"
        assert "updated_at" in res


@pytest.mark.anyio
async def test_7_day_cache_ttl(monkeypatch):
    model = "gemini/gemini-3.7-flash"
    now = time.time()

    # Pre-populate cache with an entry from 2 days ago (< 7 days)
    cached_data = {
        model: {
            "input_price_per_1m": 0.15,
            "output_price_per_1m": 0.60,
            "currency": "USD",
            "source": "Initial Test Cache",
            "updated_at": now - (2 * 24 * 3600),
        }
    }
    save_pricing_cache(cached_data)

    with patch("pricing_service.fetch_pricing_from_web") as mock_fetch:
        pricing = await refresh_model_pricing(model, force=False)
        assert pricing["source"] == "Initial Test Cache"
        mock_fetch.assert_not_called()

    # If cache is 8 days old (> 7 days), it must trigger fresh web fetch
    stale_data = {
        model: {
            "input_price_per_1m": 0.10,
            "output_price_per_1m": 0.40,
            "currency": "USD",
            "source": "Stale 8-Day Cache",
            "updated_at": now - (8 * 24 * 3600),
        }
    }
    save_pricing_cache(stale_data)

    mock_fresh = {
        "input_price_per_1m": 0.15,
        "output_price_per_1m": 0.60,
        "currency": "USD",
        "source": "Refreshed Web Search",
        "updated_at": now,
    }
    with patch("pricing_service.fetch_pricing_from_web", return_value=mock_fresh) as mock_fetch:
        refreshed = await refresh_model_pricing(model, force=False)
        assert refreshed["source"] == "Refreshed Web Search"
        mock_fetch.assert_called_once()
