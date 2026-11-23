"""
Core Configuration Module for Izvor.

Provides centralized environment resolution, workspace paths, default LLM model
specifications (from data/system_settings.json SSOT), and dynamic model getter
functions that read from agent_settings.json SSOT without circular dependencies.
"""

from collections.abc import Callable, Mapping
import logging
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .paths import (
    AGENT_SETTINGS_FILE,
    DEFAULT_SYSTEM_AGENT_SETTINGS_FILE,
    PROJECT_ROOT,
    PROMPTS_DIR,
    SYSTEM_AGENT_SETTINGS_FILE,
    SYSTEM_SETTINGS_FILE,
)

_env_path = PROJECT_ROOT / ".env"
if _env_path.exists():
    load_dotenv(dotenv_path=_env_path)
else:
    load_dotenv()


logger = logging.getLogger(__name__)

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")
SLACK_TEST_CHANNEL_ID = os.getenv("SLACK_TEST_CHANNEL_ID", "").strip()

_slack_bot_token_override: str | None = None


def configure_token(token: str | None) -> None:
    """Configures or clears an in-memory Slack bot token override for test isolation."""
    global _slack_bot_token_override
    _slack_bot_token_override = token


def reset_token() -> None:
    """Resets the in-memory Slack bot token override."""
    configure_token(None)


def get_slack_bot_token() -> str:
    """Returns the configured Slack bot token, checking in-memory override first."""
    if _slack_bot_token_override is not None:
        return _slack_bot_token_override
    return SLACK_BOT_TOKEN


_admin_slack_user_ids_override: set[str] | None = None
_production_override: bool | None = None
_admin_username_override: str | None = None
_admin_password_override: str | None = None


def set_admin_slack_user_ids_override(admin_ids: set[str] | list[str] | str | None) -> None:
    """Sets or clears an in-memory override for Slack admin user IDs (used for test isolation)."""
    global _admin_slack_user_ids_override
    if admin_ids is None:
        _admin_slack_user_ids_override = None
    elif isinstance(admin_ids, str):
        _admin_slack_user_ids_override = {x.strip() for x in admin_ids.split(",") if x.strip()}
    else:
        _admin_slack_user_ids_override = {str(x).strip() for x in admin_ids if str(x).strip()}


def get_admin_slack_user_ids() -> set[str]:
    """Returns the set of authorized Slack Admin User IDs from override or environment configuration."""
    if _admin_slack_user_ids_override is not None:
        return _admin_slack_user_ids_override
    raw = os.getenv("SLACK_ADMIN_USER_IDS", "").strip()
    if not raw:
        return set()
    return {x.strip() for x in raw.split(",") if x.strip()}


def is_slack_admin(user_id: str | None) -> bool:
    """
    Checks if a Slack user has administrative privileges.
    FAILS CLOSED: Returns False if user_id is empty or if no admin IDs are configured.
    """
    if not user_id:
        return False
    u = str(user_id).strip()
    return bool(u and u in get_admin_slack_user_ids())


def set_production_override(is_prod: bool | None) -> None:
    """Sets or clears an in-memory override for production environment detection."""
    global _production_override
    _production_override = is_prod


def is_production() -> bool:
    """
    Determines if the application is currently running in a production environment.
    Checks in-memory override first, then ENVIRONMENT, IZVOR_ENV, and APP_ENV.
    """
    if _production_override is not None:
        return _production_override
    env = os.getenv("ENVIRONMENT", os.getenv("IZVOR_ENV", os.getenv("APP_ENV", "development"))).strip().lower()
    return env in ("production", "prod")


# Tools workspace directory: defaults to project root (one directory above 'core' and 'services')
_raw_workspace = os.getenv("IZVOR_WORKSPACE_DIR", os.getenv("TOOLS_WORKSPACE_DIR", "")).strip()
if _raw_workspace and _raw_workspace != ".":
    _workspace_path = Path(_raw_workspace)
    TOOLS_WORKSPACE_DIR = (
        _workspace_path.resolve()
        if _workspace_path.is_absolute()
        else (PROJECT_ROOT / _workspace_path).resolve()
    )
else:
    TOOLS_WORKSPACE_DIR = PROJECT_ROOT


# Admin HTTP Basic Authentication credentials
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")


def set_admin_credentials_override(username: str | None = None, password: str | None = None) -> None:
    """Configures in-memory admin credentials override for test isolation."""
    global _admin_username_override, _admin_password_override
    _admin_username_override = username
    _admin_password_override = password


def get_admin_username() -> str:
    """Returns active admin username, checking in-memory override first."""
    if _admin_username_override is not None:
        return _admin_username_override
    return os.getenv("ADMIN_USERNAME", "admin")


def get_admin_password() -> str:
    """Returns active admin password, checking in-memory override first."""
    if _admin_password_override is not None:
        return _admin_password_override
    return os.getenv("ADMIN_PASSWORD", "")

# Placeholder default for the public base URL used to resolve relative avatar icon paths.
# Slack fetches icon_url server-side, so this MUST be a publicly reachable URL (e.g. the
# Cloudflare tunnel hostname); the placeholder never resolves from Slack's servers.
DEFAULT_BASE_AVATAR_URL = "https://izvor.local/"

_base_avatar_url_placeholder_warned = False


def get_base_avatar_url(env: Mapping[str, str] | None = None) -> str:
    """
    Returns the public base URL (with trailing slash) for resolving relative avatar icon URLs.
    Reads the BASE_AVATAR_URL env var at call time (or from injected env) so tests/deployments
    can override it via env.
    Warns once if the placeholder default is still in use, since Slack cannot fetch from it.
    """
    global _base_avatar_url_placeholder_warned
    active_env = env if env is not None else os.environ
    raw = active_env.get("BASE_AVATAR_URL", "")
    if not raw or raw.rstrip("/") + "/" == DEFAULT_BASE_AVATAR_URL:
        if not _base_avatar_url_placeholder_warned:
            _base_avatar_url_placeholder_warned = True
            logger.warning(
                "[CONFIG] BASE_AVATAR_URL is not set (or still the placeholder '%s'). "
                "Slack will NOT be able to load agent avatar icons. Set it to the public URL of this app "
                "(e.g. the Cloudflare tunnel hostname).",
                DEFAULT_BASE_AVATAR_URL,
            )
    return (raw or DEFAULT_BASE_AVATAR_URL).rstrip("/") + "/"


def resolve_avatar_url(icon_url: str | None, env: Mapping[str, str] | None = None) -> str | None:
    """
    Resolves an agent's avatar icon URL.
    If icon_url is a relative path (e.g. 'avatars/image_43.png'), prepends the public
    BASE_AVATAR_URL (see get_base_avatar_url).
    If icon_url is already an absolute HTTP/HTTPS URL, returns it as is.
    """
    if not icon_url:
        return None
    icon_str = str(icon_url).strip()
    if icon_str.startswith(("http://", "https://")):
        return icon_str
    return f"{get_base_avatar_url(env=env)}{icon_str.lstrip('/')}"


# Usage channel configuration
USAGE_CHANNEL_NAME = os.getenv("USAGE_CHANNEL_NAME", "izvor-usage")
USAGE_CHANNEL_ID = os.getenv("USAGE_CHANNEL_ID", "")


def _load_system_agents_from_ssot() -> dict[str, dict[str, Any]]:
    """Loads system_agents mapping directly from engine/settings/system_agent_settings.json SSOT."""
    settings_file = SYSTEM_AGENT_SETTINGS_FILE
    if not settings_file.exists():
        settings_file = DEFAULT_SYSTEM_AGENT_SETTINGS_FILE
    if not settings_file.exists():
        return {}
    try:
        import json

        with open(settings_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("system_agents", {})
        if isinstance(raw, dict):
            return {k: v for k, v in raw.items() if isinstance(v, dict)}
    except (OSError, json.JSONDecodeError, UnicodeError, AttributeError, ValueError) as e:
        logger.debug(f"[CONFIG] System agent settings load error: {e}")
    return {}


def _load_agents_from_ssot() -> dict[str, dict[str, Any]]:
    """Loads agents mapping directly from data/settings/agent_settings.json SSOT."""
    settings_file = AGENT_SETTINGS_FILE
    if not settings_file.exists():
        return {}
    try:
        import json

        with open(settings_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("agents", {})
        if isinstance(raw, dict):
            return {k: v for k, v in raw.items() if isinstance(v, dict)}
    except (OSError, json.JSONDecodeError, UnicodeError, AttributeError, ValueError) as e:
        logger.debug(f"[CONFIG] Agent settings load error: {e}")
    return {}


def _load_orchestrator_from_ssot() -> dict[str, Any]:
    """Loads orchestrator mapping directly from engine/settings/system_agent_settings.json SSOT."""
    settings_file = SYSTEM_AGENT_SETTINGS_FILE
    if not settings_file.exists():
        settings_file = DEFAULT_SYSTEM_AGENT_SETTINGS_FILE
    if not settings_file.exists():
        return {}
    try:
        import json

        with open(settings_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("orchestrator", {})
        if isinstance(raw, dict):
            return dict(raw)
    except (OSError, json.JSONDecodeError, UnicodeError, AttributeError, ValueError) as e:
        logger.debug(f"[CONFIG] Orchestrator settings load error: {e}")
    return {}


# Shared in-memory configuration maps (populated from data/agent_settings.json SSOT)
AGENTS_CONFIG: dict[str, dict[str, Any]] = _load_agents_from_ssot()
SYSTEM_AGENTS_CONFIG: dict[str, dict[str, Any]] = _load_system_agents_from_ssot()
ORCHESTRATOR_CONFIG: dict[str, Any] = _load_orchestrator_from_ssot()


def _load_default_models_from_ssot() -> tuple[str, str]:
    """Loads default_fast_model and default_reasoning_model from data/settings/system_settings.json SSOT."""
    settings_file = SYSTEM_SETTINGS_FILE
    fast_default = "gemini/gemini-3.8-flash"
    reasoning_default = "mistral/mistral-large-latest"
    if not settings_file.exists():
        return fast_default, reasoning_default
    try:
        import json

        with open(settings_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        fast = str(data.get("default_fast_model", fast_default)).strip() or fast_default
        reasoning = (
            str(data.get("default_reasoning_model", reasoning_default)).strip()
            or reasoning_default
        )
        return fast, reasoning
    except (OSError, json.JSONDecodeError, ValueError) as e:
        logger.debug(f"[CONFIG] System settings default models load error: {e}")
        return fast_default, reasoning_default


_fast_model_provider: Callable[[], str] | None = None
_reasoning_model_provider: Callable[[], str] | None = None


def set_default_model_providers(
    fast_provider: Callable[[], str] | None = None,
    reasoning_provider: Callable[[], str] | None = None,
) -> None:
    """Injectable providers for default models, decoupling core/config from orchestration."""
    global _fast_model_provider, _reasoning_model_provider
    if fast_provider is not None:
        _fast_model_provider = fast_provider
    if reasoning_provider is not None:
        _reasoning_model_provider = reasoning_provider


def get_default_fast_model() -> str:
    """Returns the system-level default fast LLM model dynamically from SSOT."""
    if _fast_model_provider is not None:
        try:
            return _fast_model_provider()
        except (AttributeError, ImportError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            logger.debug(f"[CONFIG] Fast model provider fallback: {exc}")
    fast, _ = _load_default_models_from_ssot()
    return fast


def get_default_reasoning_model() -> str:
    """Returns the system-level default reasoning LLM model dynamically from SSOT."""
    if _reasoning_model_provider is not None:
        try:
            return _reasoning_model_provider()
        except (AttributeError, ImportError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            logger.debug(f"[CONFIG] Reasoning model provider fallback: {exc}")
    _, reasoning = _load_default_models_from_ssot()
    return reasoning


def get_system_agent_model(name: str, fallback: str = "") -> str:
    """
    Retrieves the configured LLM model for a system agent from SSOT (data/agent_settings.json).
    Allows environment variable override (e.g. THREAD_NOTES_MODEL, INTENT_EXTRACTOR_MODEL,
    LANGUAGE_DETECTOR_MODEL) only if explicitly defined in the environment.
    """
    env_var_key = f"{name.upper()}_MODEL"
    env_val = os.getenv(env_var_key)
    if env_val and env_val.strip():
        return env_val.strip()

    # 1. Check in-memory shared SYSTEM_AGENTS_CONFIG
    sys_cfg = SYSTEM_AGENTS_CONFIG.get(name, {})
    if isinstance(sys_cfg, dict) and sys_cfg.get("model"):
        return str(sys_cfg["model"]).strip()

    # 2. Direct load from SSOT file (data/agent_settings.json)
    ssot_agents = _load_system_agents_from_ssot()
    if name in ssot_agents:
        entry = ssot_agents[name]
        if isinstance(entry, dict) and entry.get("model"):
            SYSTEM_AGENTS_CONFIG[name] = entry
            return str(entry["model"]).strip()

    return fallback or get_orchestrator_fast_model()


def get_orchestrator_fast_model(fallback: str | None = None) -> str:
    """Retrieves the fast_model configured for the Orchestrator from env, in-memory ORCHESTRATOR_CONFIG, or SSOT."""
    effective_fallback = fallback or get_default_fast_model()
    env_val = os.getenv("ORCHESTRATOR_FAST_MODEL")
    if env_val and env_val.strip():
        return env_val.strip()

    # 1. Check in-memory shared ORCHESTRATOR_CONFIG
    if isinstance(ORCHESTRATOR_CONFIG, dict) and ORCHESTRATOR_CONFIG.get("fast_model"):
        return str(ORCHESTRATOR_CONFIG["fast_model"]).strip()

    # 2. Check direct SSOT file
    ssot_orch = _load_orchestrator_from_ssot()
    if isinstance(ssot_orch, dict) and ssot_orch.get("fast_model"):
        ORCHESTRATOR_CONFIG.update(ssot_orch)
        return str(ssot_orch["fast_model"]).strip()

    return effective_fallback


def get_orchestrator_reasoning_model(fallback: str | None = None) -> str:
    """Retrieves the reasoning_model configured for the Orchestrator from env, in-memory ORCHESTRATOR_CONFIG, or SSOT."""
    effective_fallback = fallback or get_default_reasoning_model()
    env_val = os.getenv("ORCHESTRATOR_REASONING_MODEL")
    if env_val and env_val.strip():
        return env_val.strip()

    # 1. Check in-memory shared ORCHESTRATOR_CONFIG
    if isinstance(ORCHESTRATOR_CONFIG, dict) and ORCHESTRATOR_CONFIG.get("reasoning_model"):
        return str(ORCHESTRATOR_CONFIG["reasoning_model"]).strip()

    # 2. Check direct SSOT file
    ssot_orch = _load_orchestrator_from_ssot()
    if isinstance(ssot_orch, dict) and ssot_orch.get("reasoning_model"):
        ORCHESTRATOR_CONFIG.update(ssot_orch)
        return str(ssot_orch["reasoning_model"]).strip()

    return effective_fallback


# System / Utility Agent prompts (lazily built to prevent circular imports with prompts)
_CACHED_PROMPTS: dict[str, str] = {}


def get_pricing_agent_prompt() -> str:
    if "pricing_agent" not in _CACHED_PROMPTS:
        prompt_file = PROMPTS_DIR / "system_agents" / "Pricing_Agent.md"
        if prompt_file.exists():
            _CACHED_PROMPTS["pricing_agent"] = prompt_file.read_text(encoding="utf-8")
        else:
            _CACHED_PROMPTS["pricing_agent"] = "You are a pricing analyst."
    return _CACHED_PROMPTS["pricing_agent"]


_max_discussion_turns_provider: Callable[[], int] | None = None


def set_max_discussion_turns_provider(provider: Callable[[], int] | None) -> None:
    """Injectable provider for max discussion turns, decoupling core/config from orchestration."""
    global _max_discussion_turns_provider
    _max_discussion_turns_provider = provider


def get_max_discussion_turns() -> int:
    """Returns max discussion turns from system_settings.json (SSOT)."""
    if _max_discussion_turns_provider is not None:
        try:
            return _max_discussion_turns_provider()
        except (AttributeError, ImportError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            logger.debug(f"[CONFIG] Max discussion turns provider fallback: {exc}")
    try:
        import json

        settings_file = SYSTEM_SETTINGS_FILE
        if settings_file.exists():
            with open(settings_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return int(data.get("max_discussion_turns", 20))
    except (OSError, ValueError, TypeError) as exc:
        logger.debug(f"[CONFIG] Failed reading max_discussion_turns from SSOT: {exc}")
    return 20


TOOL_INTERMEDIATE_UPDATE_DELAY_SECONDS = float(
    os.getenv("TOOL_INTERMEDIATE_UPDATE_DELAY_SECONDS", "5.0")
)

_internal_revisor_enabled_override: bool | None = None


def set_internal_revisor_enabled_override(enabled: bool | None) -> None:
    """Sets or clears an in-memory override for is_internal_revisor_enabled."""
    global _internal_revisor_enabled_override
    _internal_revisor_enabled_override = enabled


def is_internal_revisor_enabled() -> bool:
    """Checks if InternalRevisor is enabled in data/settings/system_settings.json."""
    if _internal_revisor_enabled_override is not None:
        return _internal_revisor_enabled_override
    settings_file = SYSTEM_SETTINGS_FILE
    if not settings_file.exists():
        return True
    try:
        import json
        with open(settings_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return bool(data.get("internal_revisor_enabled", True))
    except (OSError, json.JSONDecodeError, ValueError) as e:
        logger.debug(f"[CONFIG] System settings load error: {e}")
        return True

__all__ = [
    "PROJECT_ROOT",
    "logger",
    "SLACK_BOT_TOKEN",
    "configure_token",
    "reset_token",
    "get_slack_bot_token",
    "SLACK_SIGNING_SECRET",
    "SLACK_TEST_CHANNEL_ID",
    "TOOLS_WORKSPACE_DIR",
    "ADMIN_USERNAME",
    "ADMIN_PASSWORD",
    "get_admin_username",
    "get_admin_password",
    "set_admin_credentials_override",
    "set_admin_slack_user_ids_override",
    "set_production_override",
    "DEFAULT_BASE_AVATAR_URL",
    "get_base_avatar_url",
    "USAGE_CHANNEL_NAME",
    "USAGE_CHANNEL_ID",
    "AGENTS_CONFIG",
    "SYSTEM_AGENTS_CONFIG",
    "ORCHESTRATOR_CONFIG",
    "TOOL_INTERMEDIATE_UPDATE_DELAY_SECONDS",
    "resolve_avatar_url",
    "get_default_fast_model",
    "get_default_reasoning_model",
    "get_system_agent_model",
    "get_orchestrator_fast_model",
    "get_orchestrator_reasoning_model",
    "get_pricing_agent_prompt",
    "is_internal_revisor_enabled",
    "set_internal_revisor_enabled_override",
]


