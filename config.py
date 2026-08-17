import os
from pathlib import Path

from dotenv import load_dotenv

# Load environment variables from .env file
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

# Set environment variables for LiteLLM if loaded from .env
if os.getenv("GEMINI_API_KEY"):
    os.environ["GEMINI_API_KEY"] = os.getenv("GEMINI_API_KEY")
if os.getenv("MISTRAL_API_KEY"):
    os.environ["MISTRAL_API_KEY"] = os.getenv("MISTRAL_API_KEY")

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")
MCP_WORKSPACE_DIR = Path(os.getenv("MCP_WORKSPACE_DIR", ".")).resolve()

# Default fallback agent if no tag is specified in user request
DEFAULT_AGENT_KEY = "Gemini_Architect"

# Configuration matrix for LiteLLM routing and Slack personas
AGENTS_CONFIG = {
    "Gemini_Visionary": {
        "model": "gemini/gemini-3.5-flash",
        "temperature": 0.9,
        "username": "CaiLogics Visionary",
        "icon_url": "https://agents-slack.cailogics.com/static/avatar_22.png",
        "system_prompt": (
            "You are CaiLogics Visionary, a creative AI agent specialized in generating creative ideas, "
            "imaginative concepts, visual frameworks, and bold architectural possibilities. "
            "Your tone is inspiring, innovative, and forward-thinking."
        ),
    },
    "Gemini_Architect": {
        "model": "gemini/gemini-3.5-flash",
        "temperature": 0.1,
        "username": "CaiLogics Architect",
        "icon_url": "https://agents-slack.cailogics.com/static/avatar_33.png",
        "system_prompt": (
            "You are CaiLogics Architect, an analytical AI agent specialized in system structure, "
            "component design, database modeling, direct instructions, and clean structural synthesis. "
            "Your tone is structured, precise, clear, and methodical."
        ),
    },
    "Mistral_Innovator": {
        "model": "mistral/mistral-medium-latest",
        "temperature": 0.8,
        "username": "CaiLogics Innovator",
        "icon_url": "https://agents-slack.cailogics.com/static/avatar_26.png",
        "system_prompt": (
            "You are CaiLogics Innovator, a software engineering agent specialized in alternative code solutions, "
            "creative algorithms, novel optimization techniques, and modern programming paradigms. "
            "Your tone is pragmatic yet inventive."
        ),
    },
    "Mistral_TechLead": {
        "model": "mistral/mistral-medium-latest",
        "temperature": 0.0,
        "username": "CaiLogics TechLead",
        "icon_url": "https://agents-slack.cailogics.com/static/avatar_21.png",
        "system_prompt": (
            "You are CaiLogics TechLead, a strict and deterministic software lead specialized in code review, "
            "logic verification, edge-case safety, reliability, and exact technical directives. "
            "Your tone is authoritative, rigorous, clear, and uncompromising on quality."
        ),
    },
}
