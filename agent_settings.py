import json
import logging
import sys
from pathlib import Path
from typing import Any

from config import AGENTS_CONFIG
from models import CircuitBreakerConfig

logger = logging.getLogger(__name__)

# Path to default persistent settings file
SETTINGS_FILE = Path(__file__).parent / "agent_settings.json"

# Mandatory locked agents that cannot be turned off
LOCKED_AGENTS = {"Gemini_Visionary", "OpAiTerra_Architect", "AnthrOpus_Sceptic"}


class AgentSettingsManager:
    """Manages persistent debate agent configurations and thread circuit breaker limits."""

    def __init__(
        self,
        settings_file: Path | None = None,
        locked_agents: set[str] | None = None,
    ):
        self.default_settings_file = settings_file or SETTINGS_FILE
        self.locked_agents = set(locked_agents or LOCKED_AGENTS)
        self.enabled_agents: set[str] = set(AGENTS_CONFIG.keys())
        self.circuit_breaker_enabled: bool = False
        self.circuit_breaker_threshold_usd: float = 1.0

    @property
    def active_settings_file(self) -> Path:
        router_mod = sys.modules.get("agent_router")
        if router_mod and hasattr(router_mod, "SETTINGS_FILE"):
            return Path(getattr(router_mod, "SETTINGS_FILE"))
        agent_settings_mod = sys.modules.get("agent_settings")
        if agent_settings_mod and hasattr(agent_settings_mod, "SETTINGS_FILE"):
            return Path(getattr(agent_settings_mod, "SETTINGS_FILE"))
        return self.default_settings_file

    def is_circuit_breaker_enabled(self) -> bool:
        """Returns True if the thread expense circuit-breaker is enabled."""
        return self.circuit_breaker_enabled

    def set_circuit_breaker_enabled(self, enabled: bool, save: bool = True) -> None:
        """Enables or disables the thread expense circuit-breaker and persists settings."""
        self.circuit_breaker_enabled = bool(enabled)
        if save:
            self.save()
        logger.info(
            f"[SETTINGS] Circuit-breaker enabled set to {self.circuit_breaker_enabled}."
        )

    def get_circuit_breaker_threshold(self) -> float:
        """Returns the current monetary threshold in USD for the circuit-breaker."""
        return float(self.circuit_breaker_threshold_usd)

    def set_circuit_breaker_threshold(self, threshold: float, save: bool = True) -> None:
        """Sets the monetary threshold in USD for the circuit-breaker and persists settings."""
        try:
            val = float(threshold)
            if val < 0.0:
                val = 0.0
            self.circuit_breaker_threshold_usd = val
            if save:
                self.save()
            logger.info(
                f"[SETTINGS] Circuit-breaker threshold set to ${self.circuit_breaker_threshold_usd:.2f} USD."
            )
        except (ValueError, TypeError) as e:
            logger.error(f"[SETTINGS] Invalid circuit-breaker threshold '{threshold}': {e}")

    def get_circuit_breaker_config(self) -> dict[str, Any]:
        """Returns the current circuit-breaker configuration dictionary."""
        return CircuitBreakerConfig(
            enabled=self.circuit_breaker_enabled,
            threshold_usd=self.circuit_breaker_threshold_usd,
        ).to_dict()

    def save(self) -> None:
        """Saves current enabled debate agents and circuit-breaker settings to a persistent JSON file."""
        target_file = self.active_settings_file
        try:
            data = {
                "enabled_agents": sorted(list(self.enabled_agents)),
                "circuit_breaker_enabled": self.circuit_breaker_enabled,
                "circuit_breaker_threshold_usd": self.circuit_breaker_threshold_usd,
            }
            with open(target_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            logger.info(
                f"[SETTINGS] Saved settings ({len(self.enabled_agents)} enabled agents, circuit_breaker={self.circuit_breaker_enabled}, threshold=${self.circuit_breaker_threshold_usd:.2f}) to {target_file.name}"
            )
        except Exception as e:
            logger.error(f"[SETTINGS] Error saving agent settings: {e}")

    def load(self) -> set[str]:
        """
        Loads enabled debate agents and circuit-breaker settings from persistent JSON file if available.
        Ensures locked agents are always included and unknown keys are filtered.
        """
        target_file = self.active_settings_file
        self.enabled_agents.clear()
        if target_file.exists():
            try:
                with open(target_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                loaded = set(data.get("enabled_agents", []))
                valid_keys = {k for k in loaded if k in AGENTS_CONFIG}
                valid_keys.update(self.locked_agents)
                self.enabled_agents.update(valid_keys)

                self.circuit_breaker_enabled = bool(
                    data.get("circuit_breaker_enabled", False)
                )
                self.circuit_breaker_threshold_usd = float(
                    data.get("circuit_breaker_threshold_usd", 1.0)
                )

                logger.info(
                    f"[SETTINGS] Loaded settings from {target_file.name}: {len(self.enabled_agents)} agents, circuit_breaker={self.circuit_breaker_enabled}, threshold=${self.circuit_breaker_threshold_usd:.2f}"
                )
                return set(self.enabled_agents)
            except Exception as e:
                logger.error(f"[SETTINGS] Error loading agent settings file: {e}")

        # Fallback default
        self.enabled_agents.update(AGENTS_CONFIG.keys())
        self.circuit_breaker_enabled = False
        self.circuit_breaker_threshold_usd = 1.0
        self.save()
        return set(self.enabled_agents)

    def reset(self, save: bool = True) -> None:
        """Resets all agents to enabled state."""
        self.enabled_agents.clear()
        self.enabled_agents.update(AGENTS_CONFIG.keys())
        if save:
            self.save()
        logger.info(f"[SETTINGS] Reset all {len(self.enabled_agents)} agents to ENABLED.")

    def get_enabled_agents(self) -> set[str]:
        """Returns a copy of currently enabled agents."""
        return set(self.enabled_agents)

    def set_agent_enabled(self, agent_key: str, enabled: bool) -> bool:
        """
        Enables or disables an agent for automatic debate.
        Locked agents cannot be disabled.
        """
        if agent_key in self.locked_agents and not enabled:
            logger.warning(
                f"[SETTINGS] Cannot disable locked agent '{agent_key}'. Ignoring request."
            )
            return False

        if agent_key not in AGENTS_CONFIG:
            logger.warning(f"[SETTINGS] Unknown agent key '{agent_key}'.")
            return False

        if enabled:
            self.enabled_agents.add(agent_key)
        else:
            self.enabled_agents.discard(agent_key)
        self.save()
        logger.info(
            f"[SETTINGS] Set agent '{agent_key}' enabled={enabled}. Total enabled: {len(self.enabled_agents)}"
        )
        return True

    def toggle_agent(self, agent_key: str) -> bool:
        """Toggles an agent's enabled state."""
        current_state = agent_key in self.enabled_agents
        new_state = not current_state
        self.set_agent_enabled(agent_key, new_state)
        return agent_key in self.enabled_agents


# Default singleton instance
settings_manager = AgentSettingsManager()

# Module-level variable mapping for backward compatibility
ENABLED_DEBATE_AGENTS = settings_manager.enabled_agents
CIRCUIT_BREAKER_ENABLED = settings_manager.circuit_breaker_enabled
CIRCUIT_BREAKER_THRESHOLD_USD = settings_manager.circuit_breaker_threshold_usd


# Backward-compatible function aliases
def is_circuit_breaker_enabled() -> bool:
    return settings_manager.is_circuit_breaker_enabled()


def set_circuit_breaker_enabled(enabled: bool, save: bool = True) -> None:
    settings_manager.set_circuit_breaker_enabled(enabled, save=save)


def get_circuit_breaker_threshold() -> float:
    return settings_manager.get_circuit_breaker_threshold()


def set_circuit_breaker_threshold(threshold: float, save: bool = True) -> None:
    settings_manager.set_circuit_breaker_threshold(threshold, save=save)


def get_circuit_breaker_config() -> dict[str, Any]:
    return settings_manager.get_circuit_breaker_config()


def save_enabled_agents() -> None:
    settings_manager.save()


def load_enabled_agents() -> set[str]:
    return settings_manager.load()


def reset_enabled_agents(save: bool = True) -> None:
    settings_manager.reset(save=save)


def get_enabled_agents() -> set[str]:
    return settings_manager.get_enabled_agents()


def set_agent_enabled(agent_key: str, enabled: bool) -> bool:
    return settings_manager.set_agent_enabled(agent_key, enabled)


def toggle_agent(agent_key: str) -> bool:
    return settings_manager.toggle_agent(agent_key)
