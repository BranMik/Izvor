"""
Slack App Home views facade.
Refactored into services.slack_home package.
"""

from services.slack_home import (
    _get_example_agent_id,
    _get_example_agent_names,
    build_agent_roster_blocks,
    build_app_home_view,
    build_bulk_actions_blocks,
    build_edit_workspace_context_modal,
    build_governance_blocks,
    build_panel_status_block,
    build_workflows_blocks,
    build_workspace_context_block,
)

__all__ = [
    "build_app_home_view",
    "build_edit_workspace_context_modal",
    "_get_example_agent_id",
    "_get_example_agent_names",
    "build_agent_roster_blocks",
    "build_bulk_actions_blocks",
    "build_governance_blocks",
    "build_panel_status_block",
    "build_workflows_blocks",
    "build_workspace_context_block",
]
