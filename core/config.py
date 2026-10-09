"""
Core Configuration Module for Izvor.

Provides centralized environment resolution, workspace paths, default LLM model
specifications (from data/system_settings.json SSOT), and dynamic model getter
functions that read from agent_settings.json SSOT without circular dependencies.
"""

import logging
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Load environment variables from .env file (looked up in project root or current dir)
env_path = Path(__file__).parent.parent / ".env"
if not env_path.exists():
    env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
logger = logging.getLogger(__name__)

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")
SLACK_TEST_CHANNEL_ID = os.getenv("SLACK_TEST_CHANNEL_ID", "").strip()
ADMIN_SLACK_USER_ID = os.getenv("ADMIN_SLACK_USER_ID", "").strip()

# MCP workspace directory: defaults to project root (one directory above 'core' and 'services')
_raw_workspace = os.getenv("MCP_WORKSPACE_DIR", "").strip()
if _raw_workspace and _raw_workspace != ".":
    _workspace_path = Path(_raw_workspace)
    MCP_WORKSPACE_DIR = (
        _workspace_path.resolve()
        if _workspace_path.is_absolute()
        else (PROJECT_ROOT / _workspace_path).resolve()
    )
else:
    MCP_WORKSPACE_DIR = PROJECT_ROOT

# Logging level configuration
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Admin HTTP Basic Authentication credentials
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

# Base URL for resolving relative avatar icon paths for Slack messages
BASE_AVATAR_URL = os.getenv("BASE_AVATAR_URL", "https://agents-slack.cailogics.com/").rstrip("/") + "/"


def resolve_avatar_url(icon_url: str | None) -> str | None:
    """
    Resolves an agent's avatar icon URL.
    If icon_url is a relative path (e.g. 'static/avatar_25.png'), prepends BASE_AVATAR_URL.
    If icon_url is already an absolute HTTP/HTTPS URL, returns it as is.
    """
    if not icon_url:
        return None
    icon_str = str(icon_url).strip()
    if icon_str.startswith(("http://", "https://")):
        return icon_str
    return f"{BASE_AVATAR_URL}{icon_str.lstrip('/')}"


# Default system agent persona for warnings and moderation
SYSTEM_AGENT_USERNAME = os.getenv("SYSTEM_AGENT_USERNAME", "Panel System")
SYSTEM_AGENT_AVATAR = os.getenv("SYSTEM_AGENT_AVATAR", "static/v2/avatar_47.png")

# Usage channel configuration
USAGE_CHANNEL_NAME = os.getenv("USAGE_CHANNEL_NAME", "cailogics-usage")
USAGE_CHANNEL_ID = os.getenv("USAGE_CHANNEL_ID", "")

# Shared in-memory configuration maps (populated by AgentSettingsManager from data/agent_settings.json)
AGENTS_CONFIG: dict[str, dict[str, Any]] = {}
SYSTEM_AGENTS_CONFIG: dict[str, dict[str, Any]] = {}
ORCHESTRATOR_CONFIG: dict[str, Any] = {}


def _load_default_models_from_ssot() -> tuple[str, str]:
    """Loads default_fast_model and default_reasoning_model from data/settings/system_settings.json SSOT."""
    settings_file = PROJECT_ROOT / "data" / "settings" / "system_settings.json"
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
    except Exception as e:
        logger.debug(f"[CONFIG] System settings default models load error: {e}")
        return fast_default, reasoning_default


DEFAULT_FAST_MODEL, DEFAULT_REASONING_MODEL = _load_default_models_from_ssot()


def get_default_fast_model() -> str:
    """Returns the system-level default fast LLM model."""
    return DEFAULT_FAST_MODEL


def get_default_reasoning_model() -> str:
    """Returns the system-level default reasoning LLM model."""
    return DEFAULT_REASONING_MODEL


def _load_system_agents_from_ssot() -> dict[str, dict[str, Any]]:
    """Loads system_agents mapping directly from data/settings/agent_settings.json SSOT."""
    settings_file = PROJECT_ROOT / "data" / "settings" / "agent_settings.json"
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
    settings_file = PROJECT_ROOT / "data" / "settings" / "agent_settings.json"
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


def _load_orchestrator_from_ssot() -> dict[str, Any]:
    """Loads orchestrator mapping directly from data/settings/agent_settings.json SSOT."""
    settings_file = PROJECT_ROOT / "data" / "settings" / "agent_settings.json"
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


def get_thread_notes_prompt() -> str:
    if "thread_notes" not in _CACHED_PROMPTS:
        from prompts import build_agent_prompt

        _CACHED_PROMPTS["thread_notes"] = build_agent_prompt("Thread_Notes_Agent")
    return _CACHED_PROMPTS["thread_notes"]


def get_usage_agent_prompt() -> str:
    if "usage_agent" not in _CACHED_PROMPTS:
        from prompts import build_agent_prompt

        _CACHED_PROMPTS["usage_agent"] = build_agent_prompt("Usage_Agent")
    return _CACHED_PROMPTS["usage_agent"]


def get_pricing_agent_prompt() -> str:
    if "pricing_agent" not in _CACHED_PROMPTS:
        from prompts import build_agent_prompt

        _CACHED_PROMPTS["pricing_agent"] = build_agent_prompt("Pricing_Agent")
    return _CACHED_PROMPTS["pricing_agent"]


# Module-level getters (dynamically resolved from SSOT)
def get_thread_notes_model() -> str:
    return get_orchestrator_reasoning_model()


def get_usage_agent_model() -> str:
    return get_system_agent_model("usage_agent", fallback="mistral/mistral-small-latest")


def get_pricing_agent_model() -> str:
    return get_orchestrator_fast_model()


def get_intent_extractor_model() -> str:
    return get_orchestrator_fast_model()


def get_language_detector_model() -> str:
    return get_orchestrator_fast_model()


# Pacing & Orchestration Defaults (configurable via environment variables)
DEBATE_TURN_PACING_SECONDS = float(
    os.getenv("DEBATE_TURN_PACING_SECONDS", "0.01" if "pytest" in sys.modules else "3.0")
)
SLACK_POST_PAUSE_SECONDS = float(
    os.getenv("SLACK_POST_PAUSE_SECONDS", "0.01" if "pytest" in sys.modules else "2.0")
)
MAX_DISCUSSION_TURNS = int(os.getenv("MAX_DISCUSSION_TURNS", "20"))
TOOL_CALL_LOOP_TIMEOUT_SECONDS = float(
    os.getenv("TOOL_CALL_LOOP_TIMEOUT_SECONDS", "60.0")
)
TOOL_INTERMEDIATE_UPDATE_DELAY_SECONDS = float(
    os.getenv("TOOL_INTERMEDIATE_UPDATE_DELAY_SECONDS", "5.0")
)

def is_internal_revisor_enabled() -> bool:
    """Checks if InternalRevisor is enabled in data/settings/system_settings.json."""
    settings_file = PROJECT_ROOT / "data" / "settings" / "system_settings.json"
    if not settings_file.exists():
        return True
    try:
        import json
        with open(settings_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return bool(data.get("internal_revisor_enabled", True))
    except Exception as e:
        logger.debug(f"[CONFIG] System settings load error: {e}")
        return True

__all__ = [
    "PROJECT_ROOT",
    "logger",
    "SLACK_BOT_TOKEN",
    "SLACK_SIGNING_SECRET",
    "SLACK_TEST_CHANNEL_ID",
    "MCP_WORKSPACE_DIR",
    "LOG_LEVEL",
    "ADMIN_USERNAME",
    "ADMIN_PASSWORD",
    "BASE_AVATAR_URL",
    "SYSTEM_AGENT_USERNAME",
    "SYSTEM_AGENT_AVATAR",
    "USAGE_CHANNEL_NAME",
    "USAGE_CHANNEL_ID",
    "AGENTS_CONFIG",
    "SYSTEM_AGENTS_CONFIG",
    "ORCHESTRATOR_CONFIG",
    "DEFAULT_FAST_MODEL",
    "DEFAULT_REASONING_MODEL",
    "DEBATE_TURN_PACING_SECONDS",
    "SLACK_POST_PAUSE_SECONDS",
    "MAX_DISCUSSION_TURNS",
    "TOOL_CALL_LOOP_TIMEOUT_SECONDS",
    "TOOL_INTERMEDIATE_UPDATE_DELAY_SECONDS",
    "resolve_avatar_url",
    "get_default_fast_model",
    "get_default_reasoning_model",
    "get_system_agent_model",
    "get_orchestrator_fast_model",
    "get_orchestrator_reasoning_model",
    "get_thread_notes_model",
    "get_usage_agent_model",
    "get_pricing_agent_model",
    "get_intent_extractor_model",
    "get_language_detector_model",
    "get_thread_notes_prompt",
    "get_usage_agent_prompt",
    "get_pricing_agent_prompt",
    "is_internal_revisor_enabled",
]


