"""
Model catalog defining supported LLM providers, model identifiers,
and dynamic filtering based on API keys defined in the .env file.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from core.paths import MODEL_PROVIDER_CATALOG_FILE, PROJECT_ROOT

logger = logging.getLogger(__name__)

_env_provider: Callable[[], Mapping[str, str | None]] | None = None

_PROVIDER_PREFIX_ENV_KEYS: dict[str, tuple[str, ...]] = {
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
    "mistral": ("MISTRAL_API_KEY",),
    "meta": ("META_API_KEY",),
    "custom_meta": ("META_API_KEY",),
    "groq": ("GROQ_API_KEY",),
    "together_ai": ("TOGETHER_API_KEY", "TOGETHERAI_API_KEY"),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
    "cohere": ("COHERE_API_KEY", "CO_API_KEY"),
    "perplexity": ("PERPLEXITY_API_KEY", "PERPLEXITYAI_API_KEY"),
}

_BARE_MODEL_PREFIX_ENV_KEYS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("gpt-", "o1-", "o3-", "o4-"), ("OPENAI_API_KEY",)),
    (("claude-",), ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")),
    (("gemini-",), ("GEMINI_API_KEY", "GOOGLE_API_KEY")),
    (("mistral-", "codestral", "ministral"), ("MISTRAL_API_KEY",)),
    (("command-",), ("COHERE_API_KEY", "CO_API_KEY")),
)


@dataclass(frozen=True)
class ModelResolution:
    """Result of resolving a configured LLM model against available API keys."""

    model: str
    configured_model: str
    fallback_used: bool = False
    reason: str | None = None
    missing_env_keys: tuple[str, ...] = ()


def configure_env_provider(provider: Callable[[], Mapping[str, str | None]] | None) -> None:
    """Configures an in-memory environment mapping provider for test isolation."""
    global _env_provider
    _env_provider = provider


def reset_env_provider() -> None:
    """Resets the in-memory environment mapping provider."""
    global _env_provider
    _env_provider = None


def format_model_fallback_footer(configured_model: str, fallback_model: str) -> str:
    """Formats a brief Slack mrkdwn footer indicating an API-key fallback model was used."""
    return (
        f"⚠️ _Fallback model used: `{fallback_model}` "
        f"(missing API key for `{configured_model}`)_"
    )


def load_provider_catalog(catalog_path: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """
    Loads provider and model definitions from the configuration JSON file in the data folder.
    Defaults to data/pricing/model_provider_catalog.json.
    """
    target_path = Path(catalog_path) if catalog_path else MODEL_PROVIDER_CATALOG_FILE
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


def _get_project_env_path() -> Path:
    """Resolves path to .env file."""
    return PROJECT_ROOT / ".env"


def _is_valid_key(value: Any) -> bool:
    """Checks if an API key value is valid (non-empty and not a dummy placeholder)."""
    if not value or not isinstance(value, str):
        return False
    clean = value.strip()
    if not clean:
        return False
    lower = clean.lower()
    return not (lower.startswith(("your-", "xoxb-your")) or "your-" in lower)


def _load_merged_env(
    env_dict: Mapping[str, str | None] | None = None,
    *,
    env_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Loads environment variables from explicit dict, injected provider, or .env + os.environ."""
    if env_dict is not None:
        return dict(env_dict)
    if _env_provider is not None and env_path is None and environ is None:
        return dict(_env_provider())

    merged_env: dict[str, Any] = {}
    env_file = env_path if env_path is not None else _get_project_env_path()
    if env_file.exists():
        try:
            raw_values = dotenv_values(env_file)
            merged_env.update(raw_values)
        except (OSError, UnicodeDecodeError, ValueError) as e:
            logger.warning(f"[MODEL_CATALOG] Failed to read .env file at {env_file}: {e}")
    active_environ = environ if environ is not None else os.environ
    for k, v in active_environ.items():
        if v and v.strip() and (_is_valid_key(v) or not _is_valid_key(merged_env.get(k))):
            merged_env[k] = v
    return merged_env


def get_active_providers(
    env_dict: Mapping[str, str | None] | None = None,
    catalog: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    """
    Returns a list of provider names that have active API keys defined in the .env file or environment.
    If env_dict is not provided, reads from injected provider or .env + os.environ.
    """
    if catalog is None:
        catalog = load_provider_catalog()

    merged_env = _load_merged_env(env_dict)

    active_providers = []
    for provider, cfg in catalog.items():
        keys = cfg.get("env_keys", [])
        if any(_is_valid_key(merged_env.get(k)) for k in keys):
            active_providers.append(provider)

    return active_providers


def get_required_env_keys_for_model(
    model: str | None,
    catalog: dict[str, dict[str, Any]] | None = None,
) -> list[str] | None:
    """
    Determines the candidate API key environment variables required for a model.
    Returns None for unknown/local providers (e.g. ollama/) so they are not blocked pre-flight.
    """
    if not model or not isinstance(model, str):
        return None
    clean = model.strip()
    if not clean:
        return None
    lower = clean.lower()

    if catalog is None:
        catalog = load_provider_catalog()

    keys: list[str] = []
    for cfg in catalog.values():
        cat_models = cfg.get("models", [])
        if any(isinstance(m, str) and m.strip().lower() == lower for m in cat_models):
            for k in cfg.get("env_keys", []):
                if k not in keys:
                    keys.append(k)

    if "/" in lower:
        prefix = lower.split("/", 1)[0].strip()
        for k in _PROVIDER_PREFIX_ENV_KEYS.get(prefix, ()):
            if k not in keys:
                keys.append(k)
        return keys or None

    for prefixes, env_keys in _BARE_MODEL_PREFIX_ENV_KEYS:
        if lower.startswith(prefixes):
            for k in env_keys:
                if k not in keys:
                    keys.append(k)
            return keys or None

    return keys or None


def has_api_key_for_model(
    model: str | None,
    env_dict: Mapping[str, str | None] | None = None,
    catalog: dict[str, dict[str, Any]] | None = None,
) -> bool:
    """
    Returns True if a valid API key is present for the model, or if the model
    belongs to an unknown/local provider with no required key mapping.
    """
    req_keys = get_required_env_keys_for_model(model, catalog=catalog)
    if req_keys is None:
        return True
    merged_env = _load_merged_env(env_dict)
    return any(_is_valid_key(merged_env.get(k)) for k in req_keys)


def resolve_model_with_fallback(
    configured_model: str | None,
    preferred_fallback_model: str | None = None,
    env_dict: Mapping[str, str | None] | None = None,
    catalog: dict[str, dict[str, Any]] | None = None,
    log_context: str | None = None,
) -> ModelResolution:
    """
    Resolves a configured model against active API keys in .env / os.environ.

    When the configured model's provider key is missing:
    1. Prefers `preferred_fallback_model` (defaults to system_settings `default_reasoning_model`)
       if its API key is present.
    2. Otherwise picks the first available model in `model_provider_catalog.json` order.
    3. If no provider keys exist anywhere, preserves `configured_model` so callers fail fast
       with `api_key_not_found`.
    """
    clean_configured = str(configured_model or "").strip()
    if not clean_configured:
        return ModelResolution(model="", configured_model="", fallback_used=False)

    if catalog is None:
        catalog = load_provider_catalog()
    merged_env = _load_merged_env(env_dict)

    req_keys = get_required_env_keys_for_model(clean_configured, catalog=catalog)
    if req_keys is None or any(_is_valid_key(merged_env.get(k)) for k in req_keys):
        return ModelResolution(
            model=clean_configured,
            configured_model=clean_configured,
            fallback_used=False,
        )

    if preferred_fallback_model is None:
        from core.config import get_default_reasoning_model

        preferred_fallback_model = get_default_reasoning_model()

    preferred = str(preferred_fallback_model or "").strip()
    resolution: ModelResolution | None = None
    if preferred and preferred != clean_configured:
        pref_keys = get_required_env_keys_for_model(preferred, catalog=catalog)
        if pref_keys is not None and any(_is_valid_key(merged_env.get(k)) for k in pref_keys):
            resolution = ModelResolution(
                model=preferred,
                configured_model=clean_configured,
                fallback_used=True,
                reason="default_reasoning_model",
                missing_env_keys=tuple(req_keys),
            )

    if resolution is None:
        active_providers = get_active_providers(env_dict=merged_env, catalog=catalog)
        for provider in active_providers:
            models = catalog.get(provider, {}).get("models", [])
            for candidate in models:
                if isinstance(candidate, str) and candidate.strip():
                    resolution = ModelResolution(
                        model=candidate.strip(),
                        configured_model=clean_configured,
                        fallback_used=True,
                        reason=f"first_available:{provider}",
                        missing_env_keys=tuple(req_keys),
                    )
                    break
            if resolution is not None:
                break

    if resolution is not None:
        if log_context:
            logger.warning(
                f"[{log_context}] API key missing for '{clean_configured}' "
                f"(keys={list(req_keys)}). Falling back to '{resolution.model}' ({resolution.reason})."
            )
        return resolution

    return ModelResolution(
        model=clean_configured,
        configured_model=clean_configured,
        fallback_used=False,
        reason="no_available_fallback",
        missing_env_keys=tuple(req_keys),
    )


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
