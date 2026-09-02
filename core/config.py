import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from prompts import build_agent_prompt

# Load environment variables from .env file (looked up in project root or current dir)
env_path = Path(__file__).parent.parent / ".env"
if not env_path.exists():
    env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

# Set environment variables for LiteLLM if loaded from .env
if os.getenv("GEMINI_API_KEY"):
    os.environ["GEMINI_API_KEY"] = os.getenv("GEMINI_API_KEY")
if os.getenv("MISTRAL_API_KEY"):
    os.environ["MISTRAL_API_KEY"] = os.getenv("MISTRAL_API_KEY")
if os.getenv("GROQ_API_KEY"):
    os.environ["GROQ_API_KEY"] = os.getenv("GROQ_API_KEY")
if os.getenv("TOGETHERAI_API_KEY"):
    os.environ["TOGETHERAI_API_KEY"] = os.getenv("TOGETHERAI_API_KEY")
if os.getenv("OPENAI_API_KEY"):
    os.environ["OPENAI_API_KEY"] = os.getenv("OPENAI_API_KEY")
if os.getenv("ANTHROPIC_API_KEY"):
    os.environ["ANTHROPIC_API_KEY"] = os.getenv("ANTHROPIC_API_KEY")

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")
MCP_WORKSPACE_DIR = Path(os.getenv("MCP_WORKSPACE_DIR", ".")).resolve()

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
    if icon_str.startswith("http://") or icon_str.startswith("https://"):
        return icon_str
    return f"{BASE_AVATAR_URL}{icon_str.lstrip('/')}"


# Default fallback agent if no tag is specified in user request
DEFAULT_AGENT_KEY = "Gemini_Visionary"

# Default system agent persona for warnings and moderation
SYSTEM_AGENT_USERNAME = os.getenv("SYSTEM_AGENT_USERNAME", "Panel System")
SYSTEM_AGENT_AVATAR = os.getenv("SYSTEM_AGENT_AVATAR", "static/v2/avatar_47.png")

# Usage channel configuration
USAGE_CHANNEL_NAME = os.getenv("USAGE_CHANNEL_NAME", "cailogics-usage")
USAGE_CHANNEL_ID = os.getenv("USAGE_CHANNEL_ID", "")

# Shared in-memory configuration maps (populated by AgentSettingsManager from data/agent_settings.json)
AGENTS_CONFIG: dict[str, dict[str, Any]] = {}
SYSTEM_AGENTS_CONFIG: dict[str, dict[str, Any]] = {}


def get_system_agent_model(name: str, fallback: str = "mistral/mistral-small-latest") -> str:
    """
    Retrieves the configured LLM model for a system agent.
    Checks environment variable override first (e.g. THREAD_NOTES_MODEL),
    then the JSON-configured system_agents config, then fallback.
    """
    env_var_key = f"{name.upper()}_MODEL"
    env_val = os.getenv(env_var_key)
    if env_val:
        return env_val
    sys_cfg = SYSTEM_AGENTS_CONFIG.get(name, {})
    return sys_cfg.get("model") or fallback


# System / Utility Agent prompts
THREAD_NOTES_PROMPT = build_agent_prompt("Thread_Notes_Agent")
USAGE_AGENT_PROMPT = build_agent_prompt("Usage_Agent")
PRICING_AGENT_PROMPT = build_agent_prompt("Pricing_Agent")


# Module-level getters / properties for backward compatibility
def get_thread_notes_model() -> str:
    return get_system_agent_model("thread_notes", os.getenv("THREAD_NOTES_MODEL", "mistral/mistral-small-latest"))


def get_usage_agent_model() -> str:
    return get_system_agent_model("usage_agent", os.getenv("USAGE_AGENT_MODEL", "mistral/mistral-small-latest"))


def get_pricing_agent_model() -> str:
    return get_system_agent_model("pricing_agent", os.getenv("PRICING_AGENT_MODEL", "mistral/mistral-small-latest"))


def get_intent_extractor_model() -> str:
    return get_system_agent_model("intent_extractor", os.getenv("INTENT_EXTRACTOR_MODEL", "mistral/mistral-small-latest"))


class _DynamicConfigProxy:
    """Provides dynamic attribute access for backward-compatible system model constants."""

    @property
    def THREAD_NOTES_MODEL(self) -> str:
        return get_thread_notes_model()

    @property
    def USAGE_AGENT_MODEL(self) -> str:
        return get_usage_agent_model()

    @property
    def PRICING_AGENT_MODEL(self) -> str:
        return get_pricing_agent_model()

    @property
    def INTENT_EXTRACTOR_MODEL(self) -> str:
        return get_intent_extractor_model()


_proxy = _DynamicConfigProxy()

# Module-level variable aliases (dynamically resolve via module __getattr__)
THREAD_NOTES_MODEL = os.getenv("THREAD_NOTES_MODEL", "mistral/mistral-small-latest")
USAGE_AGENT_MODEL = os.getenv("USAGE_AGENT_MODEL", "mistral/mistral-small-latest")
PRICING_AGENT_MODEL = os.getenv("PRICING_AGENT_MODEL", "mistral/mistral-small-latest")
INTENT_EXTRACTOR_MODEL = os.getenv("INTENT_EXTRACTOR_MODEL", "mistral/mistral-small-latest")


def __getattr__(name: str) -> Any:
    if name == "THREAD_NOTES_MODEL":
        return get_thread_notes_model()
    if name == "USAGE_AGENT_MODEL":
        return get_usage_agent_model()
    if name == "PRICING_AGENT_MODEL":
        return get_pricing_agent_model()
    if name == "INTENT_EXTRACTOR_MODEL":
        return get_intent_extractor_model()
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
