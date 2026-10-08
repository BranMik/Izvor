"""
Model catalog defining supported LLM providers, model identifiers,
and dynamic filtering based on API keys defined in the .env file.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

logger = logging.getLogger(__name__)

# Path to the provider catalog JSON configuration file in data/
MODEL_PROVIDER_CATALOG_FILE = Path(__file__).resolve().parent.parent / "data" / "pricing" / "model_provider_catalog.json"
PROVIDER_CATALOG_FILE = MODEL_PROVIDER_CATALOG_FILE


def load_provider_catalog(catalog_path: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """
    Loads provider and model definitions from the configuration JSON file in the data folder.
    Defaults to data/model_provider_catalog.json.
    """
    target_path = Path(catalog_path) if catalog_path else PROVIDER_CATALOG_FILE
    if not target_path.exists():
        logger.warning(f"[MODEL_CATALOG] Provider catalog file not found at: {target_path}")
        return {}
    try:
        with open(target_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
            logger.error(f"[MODEL_CATALOG] Expected dict in {target_path}, got {type(data)}")
            return {}
    except (OSError, json.JSONDecodeError, UnicodeError, ValueError) as e:
        logger.error(f"[MODEL_CATALOG] Failed to read or parse provider catalog from {target_path}: {e}")
        return {}


# Providers catalog loaded from configuration JSON file
PROVIDER_CATALOG: dict[str, dict[str, Any]] = load_provider_catalog()


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
    return not (lower.startswith(("your-", "xoxb-your")) or "your-" in lower)


def get_active_providers(
    env_dict: dict[str, str | None] | None = None,
    catalog: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    """
    Returns a list of provider names that have active API keys defined in the .env file or environment.
    If env_dict is not provided, reads directly from .env and os.environ.
    """
    if catalog is None:
        catalog = load_provider_catalog()

    merged_env: dict[str, Any] = {}
    if env_dict is not None:
        merged_env.update(env_dict)
    else:
        env_file = _get_project_env_path()
        if env_file.exists():
            try:
                raw_values = dotenv_values(env_file)
                merged_env.update(raw_values)
            except (OSError, UnicodeDecodeError, ValueError) as e:
                logger.warning(f"[MODEL_CATALOG] Failed to read .env file at {env_file}: {e}")
        # Also check current process environment
        for k, v in os.environ.items():
            if k not in merged_env or not merged_env[k]:
                merged_env[k] = v

    active_providers = []
    for provider, cfg in catalog.items():
        keys = cfg.get("env_keys", [])
        if any(_is_valid_key(merged_env.get(k)) for k in keys):
            active_providers.append(provider)

    return active_providers


def get_available_models(
    current_model: str | None = None,
    env_dict: dict[str, str | None] | None = None,
    catalog: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """
    Constructs Block Kit option_groups for a Slack static_select dropdown
    based on active API keys in .env.

    Returns:
        (option_groups, initial_option)
        where option_groups is a list of Slack OptionGroup dicts
        and initial_option is a Slack Option dict matching current_model.
    """
    if catalog is None:
        catalog = load_provider_catalog()

    active_providers = get_active_providers(env_dict=env_dict, catalog=catalog)
    option_groups: list[dict[str, Any]] = []
    seen_models: set[str] = set()

    for provider in active_providers:
        cfg = catalog.get(provider, {})
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
                    "text": "Current Model",
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
                "text": "No API keys configured",
                "emoji": True,
            },
            "value": "none",
        }
        option_groups.append({
            "label": {
                "type": "plain_text",
                "text": "Warning",
                "emoji": True,
            },
            "options": [placeholder],
        })
        initial_option = placeholder

    return option_groups, initial_option
