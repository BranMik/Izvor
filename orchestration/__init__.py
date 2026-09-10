"""Orchestration package for SlackAgenticPanel.

Exposes routing, settings management, agent nomination, and thread state management.
"""

from .agent_router import (
    orchestrate_discussion,
    process_and_respond,
)
from .agent_settings import (
    CIRCUIT_BREAKER_ENABLED,
    CIRCUIT_BREAKER_THRESHOLD_USD,
    ENABLED_DEBATE_AGENTS,
    LOCKED_AGENTS,
    SETTINGS_FILE,
    AgentSettingsManager,
    OrchestrationMode,
    get_circuit_breaker_config,
    get_circuit_breaker_threshold,
    get_enabled_agents,
    is_circuit_breaker_enabled,
    load_enabled_agents,
    reset_enabled_agents,
    save_enabled_agents,
    set_agent_enabled,
    set_circuit_breaker_enabled,
    set_circuit_breaker_threshold,
    settings_manager,
    toggle_agent,
)
from .nomination import (
    enforce_valid_nomination,
    get_initial_agents,
    parse_agent_and_prompt,
    resolve_target_file,
)
from .thread_state import (
    DiscussionStateStore,
    ThreadContextStore,
    ThreadStateManager,
    TurnRetryStateStore,
    thread_state_manager,
)

# Exports: Routing, Settings, Circuit Breaker, Nomination, and Thread State
__all__ = [
    "CIRCUIT_BREAKER_ENABLED",
    "CIRCUIT_BREAKER_THRESHOLD_USD",
    "ENABLED_DEBATE_AGENTS",
    "LOCKED_AGENTS",
    "SETTINGS_FILE",
    "AgentSettingsManager",
    "DiscussionStateStore",
    "OrchestrationMode",
    "ThreadContextStore",
    "ThreadStateManager",
    "TurnRetryStateStore",
    "enforce_valid_nomination",
    "get_circuit_breaker_config",
    "get_circuit_breaker_threshold",
    "get_enabled_agents",
    "get_initial_agents",
    "is_circuit_breaker_enabled",
    "load_enabled_agents",
    "orchestrate_discussion",
    "parse_agent_and_prompt",
    "process_and_respond",
    "reset_enabled_agents",
    "resolve_target_file",
    "save_enabled_agents",
    "set_agent_enabled",
    "set_circuit_breaker_enabled",
    "set_circuit_breaker_threshold",
    "settings_manager",
    "thread_state_manager",
    "toggle_agent",
]
