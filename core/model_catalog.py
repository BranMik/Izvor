"""
Model catalog defining supported LLM providers, model identifiers,
and dynamic filtering based on API keys defined in the .env file.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

logger = logging.getLogger(__name__)

# Providers catalog with their corresponding environment variables and available models
PROVIDER_CATALOG: dict[str, dict[str, Any]] = {
    "Google Gemini": {
        "env_keys": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
        "models": [
            "gemini/gemini-3.8-flash",
            "gemini/gemini-3.8-flash-cyber",
            "gemini/gemini-3.7-flash",
            "gemini/gemini-3.5-flash",
        ],
    },
    "OpenAI": {
        "env_keys": ["OPENAI_API_KEY"],
        "models": [
            "openai/gpt-5.6-sol",
            "openai/gpt-5.6-terra",
            "openai/gpt-5.6-luna",
            "openai/gpt-5.6",
        ],
    },
    "Anthropic": {
        "env_keys": ["ANTHROPIC_API_KEY"],
        "models": [
            "anthropic/claude-fable-5-1",
            "anthropic/claude-opus-5",
            "anthropic/claude-sonnet-5",
            "anthropic/claude-haiku-4-5",
        ],
    },
    "Mistral AI": {
        "env_keys": ["MISTRAL_API_KEY"],
        "models": [
            "mistral/mistral-large-latest",
            "mistral/mistral-small-latest",
            "mistral/codestral-latest",
            "mistral/devstral-2",
            "mistral/ministral-8b-latest",
        ],
    },
    "Meta AI": {
        "env_keys": ["META_API_KEY"],
        "models": [
            "meta/muse-spark",
            "meta/muse-glimmer",
            "meta/llama-4-scout",
            "meta/llama-4-maverick",
            "meta/llama-3.3-70b",
        ],
    },
    "Groq": {
        "env_keys": ["GROQ_API_KEY"],
        "models": [
            "groq/llama-4-scout",
            "groq/llama-4-maverick",
            "groq/llama-3.3-70b-versatile",
            "groq/llama-3.1-8b-instant",
        ],
    },
    "Together AI": {
        "env_keys": ["TOGETHERAI_API_KEY", "TOGETHER_API_KEY"],
        "models": [
            "together_ai/deepseek-ai/DeepSeek-V4-Pro",
            "together_ai/deepseek-ai/DeepSeek-V4-Flash",
            "together_ai/meta-llama/Llama-4-Scout",
            "together_ai/meta-llama/Llama-3.3-70B-Instruct-Turbo",
        ],
    },
    "DeepSeek": {
        "env_keys": ["DEEPSEEK_API_KEY"],
        "models": [
            "deepseek/deepseek-v4-pro",
            "deepseek/deepseek-v4-flash",
            "deepseek/deepseek-chat",
        ],
    },
    "OpenRouter": {
        "env_keys": ["OPENROUTER_API_KEY"],
        "models": [
            "openrouter/auto",
            "openrouter/openai/gpt-5.6-sol",
            "openrouter/openai/gpt-5.6-terra",
            "openrouter/anthropic/claude-opus-5",
            "openrouter/anthropic/claude-sonnet-5",
        ],
    },
    "Cohere": {
        "env_keys": ["COHERE_API_KEY"],
        "models": [
            "command-r-plus",
            "command-r",
        ],
    },
    "Perplexity": {
        "env_keys": ["PERPLEXITY_API_KEY"],
        "models": [
            "perplexity/sonar-reasoning",
            "perplexity/sonar",
        ],
    },
}


def _get_project_env_path() -> Path:
    """Resolves path to .env file."""
    root_env = Path(__file__).resolve().parent.parent / ".env"
    if root_env.exists():
        return root_env
    core_env = Path(__file__).resolve().parent / ".env"
    return core_env


def _is_valid_key(value: Any) -> bool:
    """Checks if an API key value is valid (non-empty and not a dummy placeholder)."""
    if not value or not isinstance(value, str):
        return False
    clean = value.strip()
    if not clean:
        return False
    lower = clean.lower()
    if lower.startswith("your-") or lower.startswith("xoxb-your") or "your-" in lower:
        return False
    return True


def get_active_providers(env_dict: dict[str, str | None] | None = None) -> list[str]:
    """
    Returns a list of provider names that have active API keys defined in the .env file or environment.
    If env_dict is not provided, reads directly from .env and os.environ.
    """
    merged_env: dict[str, Any] = {}
    if env_dict is not None:
        merged_env.update(env_dict)
    else:
        env_file = _get_project_env_path()
        if env_file.exists():
            try:
                raw_values = dotenv_values(env_file)
                merged_env.update(raw_values)
            except Exception as e:
                logger.warning(f"[MODEL_CATALOG] Failed to read .env file at {env_file}: {e}")
        # Also check current process environment
        for k, v in os.environ.items():
            if k not in merged_env or not merged_env[k]:
                merged_env[k] = v

    active_providers = []
    for provider, cfg in PROVIDER_CATALOG.items():
        keys = cfg.get("env_keys", [])
        if any(_is_valid_key(merged_env.get(k)) for k in keys):
            active_providers.append(provider)

    return active_providers


def get_available_models(
    current_model: str | None = None,
    env_dict: dict[str, str | None] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """
    Constructs Block Kit option_groups for a Slack static_select dropdown
    based on active API keys in .env.

    Returns:
        (option_groups, initial_option)
        where option_groups is a list of Slack OptionGroup dicts
        and initial_option is a Slack Option dict matching current_model.
    """
    active_providers = get_active_providers(env_dict=env_dict)
    option_groups: list[dict[str, Any]] = []
    seen_models: set[str] = set()

    for provider in active_providers:
        cfg = PROVIDER_CATALOG.get(provider, {})
        models = cfg.get("models", [])
        opts = []
        for m in models:
            if m not in seen_models:
                seen_models.add(m)
                opts.append({
                    "text": {
                        "type": "plain_text",
                        "text": m,
                        "emoji": True,
                    },
                    "value": m,
                })
        if opts:
            option_groups.append({
                "label": {
                    "type": "plain_text",
                    "text": provider,
                    "emoji": True,
                },
                "options": opts,
            })

    initial_option: dict[str, Any] | None = None

    if current_model and current_model.strip():
        cur_clean = current_model.strip()
        if cur_clean in seen_models:
            initial_option = {
                "text": {
                    "type": "plain_text",
                    "text": cur_clean,
                    "emoji": True,
                },
                "value": cur_clean,
            }
        else:
            # If current_model is not in active providers (e.g. key missing in .env or custom model),
            # append it in a dedicated group so initial_option is guaranteed to match an option.
            fallback_opt = {
                "text": {
                    "type": "plain_text",
                    "text": cur_clean,
                    "emoji": True,
                },
                "value": cur_clean,
            }
            option_groups.append({
                "label": {
                    "type": "plain_text",
                    "text": "Trenutni model (Current)",
                    "emoji": True,
                },
                "options": [fallback_opt],
            })
            initial_option = fallback_opt

    # If no providers are active and no current_model, provide a placeholder group
    if not option_groups:
        placeholder = {
            "text": {
                "type": "plain_text",
                "text": "Nema konfiguriranih API ključeva",
                "emoji": True,
            },
            "value": "none",
        }
        option_groups.append({
            "label": {
                "type": "plain_text",
                "text": "Upozorenje",
                "emoji": True,
            },
            "options": [placeholder],
        })
        initial_option = placeholder

    return option_groups, initial_option
