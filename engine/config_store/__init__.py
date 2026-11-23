"""Layer 1.5 configuration store: settings SSOT, snapshots, workflow presets, and integrity validation."""

from __future__ import annotations

from config_store.agent_settings import (
    AgentSettingsManager,
    apply_workflow,
    get_active_workflow_id,
    get_agents_config,
    get_arbitrator_agent,
    get_challenger_agent,
    get_enabled_agents,
    get_orchestration_mode,
    get_workflow_snapshot,
    load_enabled_agents,
    register_on_agents_changed,
    set_prompt_builder_hook,
    settings_manager,
    sync_settings_if_modified,
    unregister_on_agents_changed,
)
from config_store.app_settings import (
    AppSettingsManager,
    AppSettingsModel,
    app_settings_manager,
    get_app_settings_snapshot,
)
from config_store.integrity import ConfigIntegrityValidator
from config_store.settings_store import JsonSettingsStore, atomic_write_json
from config_store.system_settings import (
    McpServerConfig,
    SystemSettingsManager,
    SystemSettingsModel,
    get_system_settings_snapshot,
    system_settings_manager,
)
from config_store.workflow_manager import WorkflowManager, slugify, workflow_manager

__all__ = [
    "AgentSettingsManager",
    "AppSettingsManager",
    "AppSettingsModel",
    "ConfigIntegrityValidator",
    "JsonSettingsStore",
    "McpServerConfig",
    "SystemSettingsManager",
    "SystemSettingsModel",
    "WorkflowManager",
    "app_settings_manager",
    "apply_workflow",
    "atomic_write_json",
    "get_active_workflow_id",
    "get_agents_config",
    "get_app_settings_snapshot",
    "get_arbitrator_agent",
    "get_challenger_agent",
    "get_enabled_agents",
    "get_orchestration_mode",
    "get_system_settings_snapshot",
    "get_workflow_snapshot",
    "load_enabled_agents",
    "register_on_agents_changed",
    "set_prompt_builder_hook",
    "settings_manager",
    "slugify",
    "sync_settings_if_modified",
    "system_settings_manager",
    "unregister_on_agents_changed",
    "workflow_manager",
]

