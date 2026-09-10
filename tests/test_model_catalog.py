"""
Unit tests for core.model_catalog and Slack App Home model selection dropdown.
"""

from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

from core.config import AGENTS_CONFIG
from core.model_catalog import (
    get_active_providers,
    get_available_models,
    load_provider_catalog,
)
from main import app
from orchestration.agent_settings import (
    AgentSettingsManager,
    reset_enabled_agents,
    set_agent_model,
)
from services.slack_client import build_app_home_view


def test_get_active_providers_filters_placeholders():
    """Verify dummy placeholder keys are ignored, while valid keys activate providers."""
    mock_env = {
        "GEMINI_API_KEY": "your-gemini-api-key",  # Dummy placeholder
        "SLACK_BOT_TOKEN": "xoxb-your-bot-token",
        "OPENAI_API_KEY": "sk-proj-real-openai-key-value",  # Valid
        "ANTHROPIC_API_KEY": "",  # Empty
        "MISTRAL_API_KEY": "   ",  # Whitespace only
        "GROQ_API_KEY": "gsk_valid_groq_key",  # Valid
    }
    active = get_active_providers(env_dict=mock_env)
    assert "OpenAI" in active
    assert "Groq" in active
    assert "Google Gemini" not in active
    assert "Anthropic" not in active
    assert "Mistral AI" not in active


def test_get_available_models_structure():
    """Verify Block Kit option_groups format and initial_option match."""
    mock_env = {
        "GEMINI_API_KEY": "AIzaSyFakeKeyValidFormat",
        "MISTRAL_API_KEY": "fake_mistral_key",
        "OPENAI_API_KEY": "sk-proj-valid-key",
    }
    option_groups, initial_opt = get_available_models(
        current_model="gemini/gemini-3.8-flash",
        env_dict=mock_env,
    )
    assert len(option_groups) == 3
    group_labels = [g["label"]["text"] for g in option_groups]
    assert "Google Gemini" in group_labels
    assert "Mistral AI" in group_labels
    assert "OpenAI" in group_labels

    # Check initial option matches
    assert initial_opt is not None
    assert initial_opt["value"] == "gemini/gemini-3.8-flash"
    assert initial_opt["text"]["text"] == "gemini/gemini-3.8-flash"

    # Verify initial_opt is physically inside the Google Gemini group options
    gemini_grp = next(g for g in option_groups if g["label"]["text"] == "Google Gemini")
    opt_values = [o["value"] for o in gemini_grp["options"]]
    assert "gemini/gemini-3.8-flash" in opt_values

    # Verify OpenAI models include Sol, Terra, and Luna
    openai_grp = next(g for g in option_groups if g["label"]["text"] == "OpenAI")
    openai_opt_values = [o["value"] for o in openai_grp["options"]]
    assert "openai/gpt-5.6-sol" in openai_opt_values
    assert "openai/gpt-5.6-terra" in openai_opt_values
    assert "openai/gpt-5.6-luna" in openai_opt_values


def test_get_available_models_fallback_when_key_missing():
    """Verify that an agent's current model is always preserved even if provider key is absent in .env."""
    mock_env = {
        "GEMINI_API_KEY": "AIzaSyFakeKeyValidFormat",
    }
    # Current model is OpenAI Sol, but OPENAI_API_KEY is not in mock_env
    option_groups, initial_opt = get_available_models(
        current_model="openai/gpt-5.6-sol",
        env_dict=mock_env,
    )
    group_labels = [g["label"]["text"] for g in option_groups]
    assert "Google Gemini" in group_labels
    assert "Current Model" in group_labels

    assert initial_opt is not None
    assert initial_opt["value"] == "openai/gpt-5.6-sol"
    fallback_grp = next(g for g in option_groups if g["label"]["text"] == "Current Model")
    assert fallback_grp["options"][0]["value"] == "openai/gpt-5.6-sol"


def test_get_available_models_no_keys_placeholder():
    """Verify fallback when absolutely no API keys are present."""
    option_groups, initial_opt = get_available_models(
        current_model=None,
        env_dict={},
    )
    assert len(option_groups) == 1
    assert option_groups[0]["options"][0]["value"] == "none"
    assert initial_opt["value"] == "none"


def test_agent_settings_set_agent_model(tmp_path):
    """Verify set_agent_model updates memory and persists to active JSON."""
    test_settings_file = tmp_path / "test_settings.json"
    mgr = AgentSettingsManager(settings_file=test_settings_file)

    # Invalid agent key returns False
    assert mgr.set_agent_model("NonExistentAgent", "openai/gpt-5.6-sol") is False

    # Valid agent key updates model
    assert mgr.set_agent_model("Visionary", "openai/gpt-5.6-luna") is True
    assert mgr.agents_config["Visionary"]["model"] == "openai/gpt-5.6-luna"

    # Reload from JSON file and verify persistence
    mgr2 = AgentSettingsManager(settings_file=test_settings_file)
    mgr2.load()
    assert mgr2.agents_config["Visionary"]["model"] == "openai/gpt-5.6-luna"


def test_build_app_home_view_renders_model_dropdowns():
    """Verify build_app_home_view renders select_agent_model dropdown for each agent."""
    reset_enabled_agents()
    view = build_app_home_view()
    blocks = view.get("blocks", [])

    action_ids = []
    for b in blocks:
        if "accessory" in b and isinstance(b["accessory"], dict):
            action_ids.append(b["accessory"].get("action_id", ""))
        elif "elements" in b:
            for el in b["elements"]:
                action_ids.append(el.get("action_id", ""))

    # Verify model select dropdown action IDs exist for agents
    for agent_key in AGENTS_CONFIG:
        assert f"select_agent_model:{agent_key}" in action_ids

    # Check that model section has valid option_groups
    model_sections = [
        b for b in blocks
        if b.get("accessory", {}).get("action_id", "").startswith("select_agent_model:")
    ]
    assert len(model_sections) == len(AGENTS_CONFIG)
    first_sel = model_sections[0]["accessory"]
    assert first_sel["type"] == "static_select"
    assert "option_groups" in first_sel
    assert len(first_sel["option_groups"]) > 0
    assert "initial_option" in first_sel


@pytest.mark.anyio
async def test_slack_interactivity_model_change():
    """Test handling of select_agent_model action via /slack/actions webhook."""
    client = TestClient(app)

    payload = {
        "type": "block_actions",
        "user": {"id": "U123TEST", "name": "testuser"},
        "actions": [
            {
                "action_id": "select_agent_model:Visionary",
                "block_id": "agent_model_block_Visionary",
                "type": "static_select",
                "selected_option": {
                    "text": {"type": "plain_text", "text": "openai/gpt-5.6-sol"},
                    "value": "openai/gpt-5.6-sol",
                },
            }
        ],
    }

    orig_model = AGENTS_CONFIG["Visionary"].get("model")
    try:
        with patch("main.verify_slack_signature", return_value=True), \
             patch("main.publish_app_home", new_callable=AsyncMock) as mock_publish:
            resp = client.post("/slack/actions", json=payload)
            assert resp.status_code == 200
            assert mock_publish.called
            assert mock_publish.call_args.kwargs["user_id"] == "U123TEST"
            assert AGENTS_CONFIG["Visionary"]["model"] == "openai/gpt-5.6-sol"
    finally:
        if orig_model:
            set_agent_model("Visionary", orig_model)


def test_load_provider_catalog_from_json():
    """Verify load_provider_catalog reads data/provider_catalog.json correctly."""
    catalog = load_provider_catalog()
    assert isinstance(catalog, dict)
    assert "Google Gemini" in catalog
    assert "OpenAI" in catalog
    assert "Anthropic" in catalog

    for cfg in catalog.values():
        assert "env_keys" in cfg
        assert isinstance(cfg["env_keys"], list)
        assert "models" in cfg
        assert isinstance(cfg["models"], list)
        assert len(cfg["models"]) > 0


def test_load_provider_catalog_missing_file(tmp_path):
    """Verify non-existent catalog file returns empty dict gracefully."""
    missing = tmp_path / "non_existent_catalog.json"
    assert load_provider_catalog(missing) == {}


def test_load_provider_catalog_custom_file(tmp_path):
    """Verify load_provider_catalog accepts a custom Path."""
    import json
    custom_file = tmp_path / "my_catalog.json"
    custom_data = {
        "CustomProvider": {
            "env_keys": ["CUSTOM_KEY"],
            "models": ["custom/model-1"],
        }
    }
    custom_file.write_text(json.dumps(custom_data), encoding="utf-8")
    loaded = load_provider_catalog(custom_file)
    assert loaded == custom_data

    # Test get_available_models with this catalog
    option_groups, _ = get_available_models(
        env_dict={"CUSTOM_KEY": "valid-secret"},
        catalog=loaded,
    )
    assert len(option_groups) == 1
    assert option_groups[0]["label"]["text"] == "CustomProvider"
    assert option_groups[0]["options"][0]["value"] == "custom/model-1"

