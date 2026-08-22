import os
from pathlib import Path

from dotenv import load_dotenv

from prompts import build_agent_prompt

# Load environment variables from .env file
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


# Default fallback agent if no tag is specified in user request
DEFAULT_AGENT_KEY = "Gemini_Visionary"

# Thread Notes / Scribe Agent configuration (Live in-thread updatable summary)
THREAD_NOTES_MODEL = os.getenv("THREAD_NOTES_MODEL", "mistral/mistral-small-latest")
THREAD_NOTES_PROMPT = build_agent_prompt("Thread_Notes_Agent")

# Usage / Cost Calculator Agent configuration
USAGE_CHANNEL_NAME = os.getenv("USAGE_CHANNEL_NAME", "cailogics-usage")
USAGE_CHANNEL_ID = os.getenv("USAGE_CHANNEL_ID", "")
USAGE_AGENT_MODEL = os.getenv("USAGE_AGENT_MODEL", "gemini/gemini-3.7-flash")
USAGE_AGENT_PROMPT = build_agent_prompt("Usage_Agent")

# Configuration matrix for LiteLLM routing and Slack personas
AGENTS_CONFIG = {
    "Gemini_Visionary": {
        "model": "gemini/gemini-3.7-flash",
        "username": "Gemini Visionary",
        "icon_url": "https://avatars.example.com/static/avatar_25.png",
        "system_prompt": build_agent_prompt("Gemini_Visionary"),
    },
    "OpAiTerra_Architect": {
        "model": "openai/gpt-5.6-terra",
        "username": "OpAiTerra Architect",
        "icon_url": "https://avatars.example.com/static/avatar_33.png",
        "system_prompt": build_agent_prompt("OpAiTerra_Architect"),
    },
    "AnthrOpus_Innovator": {
        "model": "anthropic/claude-opus-5",
        "username": "AnthrOpus Innovator",
        "icon_url": "https://avatars.example.com/static/avatar_22.png",
        "system_prompt": build_agent_prompt("AnthrOpus_Innovator"),
    },
    "MistLar_TechLead": {
        "model": "mistral/mistral-large-latest",
        "username": "MistLar TechLead",
        "icon_url": "https://avatars.example.com/static/avatar_21.png",
        "system_prompt": build_agent_prompt("MistLar_TechLead"),
    },
    "AnthrOpus_Sceptic": {
        "model": "anthropic/claude-opus-5",
        "username": "AnthrOpus Sceptic",
        "icon_url": "https://avatars.example.com/static/avatar_31.png",
        "system_prompt": build_agent_prompt("AnthrOpus_Sceptic"),
    },
    "Gemini_DataAndAnalyticsStrategist": {
        "model": "gemini/gemini-3.7-flash",
        "username": "Gemini Data And Analytics Strategist",
        "icon_url": "https://avatars.example.com/static/avatar_27.png",
        "system_prompt": build_agent_prompt("Gemini_DataAndAnalyticsStrategist"),
    },
    "MistMed_CompliancePrivacyAndGovernanceOfficer": {
        "model": "mistral/mistral-large-latest",
        "username": "MistMed Compliance, Privacy And Governance Officer",
        "icon_url": "https://avatars.example.com/static/avatar_32.png",
        "system_prompt": build_agent_prompt(
            "MistMed_CompliancePrivacyAndGovernanceOfficer"
        ),
    },
    "MistMed_ScopeOptimizer": {
        "model": "mistral/mistral-large-latest",
        "username": "MistMed Scope Optimizer",
        "icon_url": "https://avatars.example.com/static/avatar_26.png",
        "system_prompt": build_agent_prompt("MistMed_ScopeOptimizer"),
    },
    "OpAiTerra_ROIStrategist": {
        "model": "openai/gpt-5.6-terra",
        "username": "OpAiTerra ROI Strategist",
        "icon_url": "https://avatars.example.com/static/avatar_29.png",
        "system_prompt": build_agent_prompt("OpAiTerra_ROIStrategist"),
    },
    "OpAiTerra_UserAdvocate": {
        "model": "openai/gpt-5.6-terra",
        "username": "OpAiTerra User Advocate",
        "icon_url": "https://avatars.example.com/static/avatar_30.png",
        "system_prompt": build_agent_prompt("OpAiTerra_UserAdvocate"),
    },
    "MistLar_SecurityArchitect": {
        "model": "mistral/mistral-large-latest",
        "username": "MistLar Security Architect",
        "icon_url": "https://avatars.example.com/static/avatar_20.png",
        "system_prompt": build_agent_prompt("MistLar_SecurityArchitect"),
    },
    "Gemini_ScalabilityEngineer": {
        "model": "gemini/gemini-3.7-flash",
        "username": "Gemini Scalability Engineer",
        "icon_url": "https://avatars.example.com/static/avatar_28.png",
        "system_prompt": build_agent_prompt("Gemini_ScalabilityEngineer"),
    },
    "MistMed_OperationalReliabilityLead": {
        "model": "mistral/mistral-large-latest",
        "username": "MistMed Operational Reliability Lead",
        "icon_url": "https://avatars.example.com/static/avatar_24.png",
        "system_prompt": build_agent_prompt("MistMed_OperationalReliabilityLead"),
    },
    "MistMed_DeploymentReleaseStrategist": {
        "model": "gemini/gemini-3.7-flash",
        "username": "MistMed Deployment & Release Strategist",
        "icon_url": "https://avatars.example.com/static/avatar_26.png",
        "system_prompt": build_agent_prompt("MistMed_DeploymentReleaseStrategist"),
    },
    "OpAiTerra_MarketAndCompetitionResearchExpert": {
        "model": "openai/gpt-5.6-terra",
        "username": "OpAiTerra Market & Competition Research Expert",
        "icon_url": "https://avatars.example.com/static/avatar_6.png",
        "system_prompt": build_agent_prompt(
            "OpAiTerra_MarketAndCompetitionResearchExpert"
        ),
    },
    "MistMed_MonitoringAndAlertingSpecialist": {
        "model": "mistral/mistral-large-latest",
        "username": "MistMed Monitoring & Alerting Specialist",
        "icon_url": "https://avatars.example.com/static/avatar_23.png",
        "system_prompt": build_agent_prompt("MistMed_MonitoringAndAlertingSpecialist"),
    },
}
