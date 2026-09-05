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

__all__ = [
    # Routing
    "process_and_respond",
    "orchestrate_discussion",
    # Settings & Circuit Breaker
    "CIRCUIT_BREAKER_ENABLED",
    "CIRCUIT_BREAKER_THRESHOLD_USD",
    "ENABLED_DEBATE_AGENTS",
    "LOCKED_AGENTS",
    "SETTINGS_FILE",
    "AgentSettingsManager",
    "OrchestrationMode",
    "get_circuit_breaker_config",
    "get_circuit_breaker_threshold",
    "get_enabled_agents",
    "is_circuit_breaker_enabled",
    "load_enabled_agents",
    "reset_enabled_agents",
    "save_enabled_agents",
    "set_agent_enabled",
    "set_circuit_breaker_enabled",
    "set_circuit_breaker_threshold",
    "settings_manager",
    "toggle_agent",
    # Nomination
    "enforce_valid_nomination",
    "get_initial_agents",
    "parse_agent_and_prompt",
    "resolve_target_file",
    # Thread State
    "ThreadStateManager",
    "thread_state_manager",
    "DiscussionStateStore",
    "TurnRetryStateStore",
    "ThreadContextStore",
]
