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

# Base URL for resolving relative avatar icon paths for Slack messages
BASE_AVATAR_URL = os.getenv("BASE_AVATAR_URL", "https://avatars.example.com/").rstrip("/") + "/"


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


def _load_system_agents_from_ssot() -> dict[str, dict[str, Any]]:
    """Loads system_agents mapping directly from data/agent_settings.json SSOT."""
    settings_file = PROJECT_ROOT / "data" / "agent_settings.json"
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

    # 1. Check settings_manager if available in loaded modules
    settings_mod = sys.modules.get("orchestration.agent_settings") or sys.modules.get("agent_settings")
    if settings_mod and hasattr(settings_mod, "settings_manager"):
        mgr_cfg = getattr(settings_mod.settings_manager, "system_agents_config", {})
        if isinstance(mgr_cfg, dict) and name in mgr_cfg:
            entry = mgr_cfg[name]
            if isinstance(entry, dict) and entry.get("model"):
                return str(entry["model"]).strip()

    # 2. Check in-memory shared SYSTEM_AGENTS_CONFIG
    sys_cfg = SYSTEM_AGENTS_CONFIG.get(name, {})
    if isinstance(sys_cfg, dict) and sys_cfg.get("model"):
        return str(sys_cfg["model"]).strip()

    # 3. Direct load from SSOT file (data/agent_settings.json)
    ssot_agents = _load_system_agents_from_ssot()
    if name in ssot_agents:
        entry = ssot_agents[name]
        if isinstance(entry, dict) and entry.get("model"):
            SYSTEM_AGENTS_CONFIG[name] = entry
            return str(entry["model"]).strip()

    return fallback


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


# Module-level getters / properties (dynamically resolved from SSOT)
def get_thread_notes_model() -> str:
    return get_system_agent_model("thread_notes")


def get_usage_agent_model() -> str:
    return get_system_agent_model("usage_agent")


def get_pricing_agent_model() -> str:
    return get_system_agent_model("pricing_agent")


def get_intent_extractor_model() -> str:
    return get_system_agent_model("intent_extractor")


def get_language_detector_model() -> str:
    return get_system_agent_model("language_detector")



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


# Module-level variable aliases (dynamically resolve via module __getattr__)
def __getattr__(name: str) -> Any:
    if name == "THREAD_NOTES_MODEL":
        return get_thread_notes_model()
    if name == "USAGE_AGENT_MODEL":
        return get_usage_agent_model()
    if name == "PRICING_AGENT_MODEL":
        return get_pricing_agent_model()
    if name == "INTENT_EXTRACTOR_MODEL":
        return get_intent_extractor_model()
    if name == "LANGUAGE_DETECTOR_MODEL":
        return get_language_detector_model()
    if name == "THREAD_NOTES_PROMPT":
        return get_thread_notes_prompt()
    if name == "USAGE_AGENT_PROMPT":
        return get_usage_agent_prompt()
    if name == "PRICING_AGENT_PROMPT":
        return get_pricing_agent_prompt()
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


