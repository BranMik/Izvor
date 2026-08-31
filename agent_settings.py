import json
import logging
import sys
from pathlib import Path
from typing import Any

from config import AGENTS_CONFIG

logger = logging.getLogger(__name__)

# Path to persistent settings file
SETTINGS_FILE = Path(__file__).parent / "agent_settings.json"

# Mandatory locked agents that cannot be turned off
LOCKED_AGENTS = {"Gemini_Visionary", "OpAiTerra_Architect", "AnthrOpus_Sceptic"}

# Global in-memory set of enabled debate agents (default: all agents in AGENTS_CONFIG)
ENABLED_DEBATE_AGENTS: set[str] = set(AGENTS_CONFIG.keys())

# Circuit-Breaker configuration for thread expense limits
CIRCUIT_BREAKER_ENABLED: bool = False
CIRCUIT_BREAKER_THRESHOLD_USD: float = 1.0


def _get_active_settings_file() -> Path:
    router_mod = sys.modules.get("agent_router")
    if router_mod and hasattr(router_mod, "SETTINGS_FILE"):
        return Path(getattr(router_mod, "SETTINGS_FILE"))
    return SETTINGS_FILE


def is_circuit_breaker_enabled() -> bool:
    """Returns True if the thread expense circuit-breaker is enabled."""
    return CIRCUIT_BREAKER_ENABLED


def set_circuit_breaker_enabled(enabled: bool, save: bool = True) -> None:
    """Enables or disables the thread expense circuit-breaker and persists settings."""
    global CIRCUIT_BREAKER_ENABLED
    CIRCUIT_BREAKER_ENABLED = bool(enabled)
    if save:
        save_enabled_agents()
    logger.info(f"[SETTINGS] Circuit-breaker enabled set to {CIRCUIT_BREAKER_ENABLED}.")


def get_circuit_breaker_threshold() -> float:
    """Returns the current monetary threshold in USD for the circuit-breaker."""
    return float(CIRCUIT_BREAKER_THRESHOLD_USD)


def set_circuit_breaker_threshold(threshold: float, save: bool = True) -> None:
    """Sets the monetary threshold in USD for the circuit-breaker and persists settings."""
    global CIRCUIT_BREAKER_THRESHOLD_USD
    try:
        val = float(threshold)
        if val < 0.0:
            val = 0.0
        CIRCUIT_BREAKER_THRESHOLD_USD = val
        if save:
            save_enabled_agents()
        logger.info(
            f"[SETTINGS] Circuit-breaker threshold set to ${CIRCUIT_BREAKER_THRESHOLD_USD:.2f} USD."
        )
    except (ValueError, TypeError) as e:
        logger.error(f"[SETTINGS] Invalid circuit-breaker threshold '{threshold}': {e}")


def get_circuit_breaker_config() -> dict[str, Any]:
    """Returns the current circuit-breaker configuration dictionary."""
    return {
        "enabled": CIRCUIT_BREAKER_ENABLED,
        "threshold_usd": CIRCUIT_BREAKER_THRESHOLD_USD,
    }


def save_enabled_agents() -> None:
    """Saves the current enabled debate agents and circuit-breaker settings to a persistent JSON configuration file."""
    target_file = _get_active_settings_file()
    try:
        data = {
            "enabled_agents": sorted(list(ENABLED_DEBATE_AGENTS)),
            "circuit_breaker_enabled": CIRCUIT_BREAKER_ENABLED,
            "circuit_breaker_threshold_usd": CIRCUIT_BREAKER_THRESHOLD_USD,
        }
        with open(target_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        logger.info(
            f"[SETTINGS] Saved settings ({len(ENABLED_DEBATE_AGENTS)} enabled agents, circuit_breaker={CIRCUIT_BREAKER_ENABLED}, threshold=${CIRCUIT_BREAKER_THRESHOLD_USD:.2f}) to {target_file.name}"
        )
    except Exception as e:
        logger.error(f"[SETTINGS] Error saving agent settings: {e}")


def load_enabled_agents() -> set[str]:
    """
    Loads enabled debate agents and circuit-breaker settings from persistent JSON file if available.
    Ensures locked agents are always included and unknown keys are filtered.
    Falls back to default (all agents ON, circuit_breaker OFF) if no settings file exists.
    """
    global CIRCUIT_BREAKER_ENABLED, CIRCUIT_BREAKER_THRESHOLD_USD
    target_file = _get_active_settings_file()
    ENABLED_DEBATE_AGENTS.clear()
    if target_file.exists():
        try:
            with open(target_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            loaded = set(data.get("enabled_agents", []))
            # Validate keys against AGENTS_CONFIG
            valid_keys = {k for k in loaded if k in AGENTS_CONFIG}
            # Ensure locked agents are always present
            valid_keys.update(LOCKED_AGENTS)
            ENABLED_DEBATE_AGENTS.update(valid_keys)

            CIRCUIT_BREAKER_ENABLED = bool(data.get("circuit_breaker_enabled", False))
            CIRCUIT_BREAKER_THRESHOLD_USD = float(
                data.get("circuit_breaker_threshold_usd", 1.0)
            )

            logger.info(
                f"[SETTINGS] Loaded settings from {target_file.name}: {len(ENABLED_DEBATE_AGENTS)} agents, circuit_breaker={CIRCUIT_BREAKER_ENABLED}, threshold=${CIRCUIT_BREAKER_THRESHOLD_USD:.2f}"
            )
            return set(ENABLED_DEBATE_AGENTS)
        except Exception as e:
            logger.error(f"[SETTINGS] Error loading agent settings file: {e}")

    # Fallback default: all agents enabled
    ENABLED_DEBATE_AGENTS.update(AGENTS_CONFIG.keys())
    CIRCUIT_BREAKER_ENABLED = False
    CIRCUIT_BREAKER_THRESHOLD_USD = 1.0
    save_enabled_agents()
    return set(ENABLED_DEBATE_AGENTS)


def reset_enabled_agents(save: bool = True) -> None:
    """Resets all agents to enabled state."""
    ENABLED_DEBATE_AGENTS.clear()
    ENABLED_DEBATE_AGENTS.update(AGENTS_CONFIG.keys())
    if save:
        save_enabled_agents()
    logger.info(f"[SETTINGS] Reset all {len(ENABLED_DEBATE_AGENTS)} agents to ENABLED.")


def get_enabled_agents() -> set[str]:
    """Returns a copy of currently enabled agents."""
    return set(ENABLED_DEBATE_AGENTS)


def set_agent_enabled(agent_key: str, enabled: bool) -> bool:
    """
    Enables or disables an agent for automatic debate.
    Locked agents (Visionary, Architect, Sceptic) cannot be disabled.
    Persists changes to agent_settings.json.
    Returns True if state was successfully updated, False otherwise.
    """
    if agent_key in LOCKED_AGENTS and not enabled:
        logger.warning(
            f"[SETTINGS] Cannot disable locked agent '{agent_key}'. Ignoring request."
        )
        return False

    if agent_key not in AGENTS_CONFIG:
        logger.warning(f"[SETTINGS] Unknown agent key '{agent_key}'.")
        return False

    if enabled:
        ENABLED_DEBATE_AGENTS.add(agent_key)
    else:
        ENABLED_DEBATE_AGENTS.discard(agent_key)
    save_enabled_agents()
    logger.info(
        f"[SETTINGS] Set agent '{agent_key}' enabled={enabled}. Total enabled: {len(ENABLED_DEBATE_AGENTS)}"
    )
    return True


def toggle_agent(agent_key: str) -> bool:
    """Toggles an agent's enabled state. Returns the new enabled state."""
    current_state = agent_key in ENABLED_DEBATE_AGENTS
    new_state = not current_state
    set_agent_enabled(agent_key, new_state)
    return agent_key in ENABLED_DEBATE_AGENTS
