import logging
import os
from typing import Any

from dotenv import dotenv_values

from core.config import is_slack_admin
from services.slack_home.agents_tab import (
    _get_example_agent_id,
    _get_example_agent_names,
    build_agent_roster_blocks,
    build_bulk_actions_blocks,
    build_panel_status_block,
)
from services.slack_home.governance_tab import (
    build_admin_required_modal,
    build_edit_workspace_context_modal,
    build_governance_blocks,
    build_workspace_context_block,
)
from services.slack_home.workflows_tab import build_workflows_blocks

logger = logging.getLogger(__name__)


def build_app_home_view(
    enabled_agents: set[str] | None = None,
    circuit_breaker_config: dict[str, Any] | None = None,
    scribe_enabled: bool | None = None,
    weekly_pricing_update_enabled: bool | None = None,
    convergence_fast_forward_enabled: bool | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """
    Generates the Block Kit Home Tab view for the Slack Application Home page.
    Displays panel agents, locked agents, thread cost circuit-breaker settings,
    Live Discussion Scribe toggle, emergency backup warning if active settings file is unreadable,
    and Workspace Baseline Context.
    """
    import orchestration.agent_settings as agent_settings_module
    from orchestration.agent_settings import (
        get_active_workflow_id,
        get_agents_config,
        get_enabled_agents,
        get_fixed_config,
        get_orchestration_mode,
        get_sandwich_config,
        sync_settings_if_modified,
    )
    from orchestration.app_settings import (
        get_circuit_breaker_config,
        get_daily_budget_usd,
        get_thread_skill_expiration_time,
        get_workspace_context,
        is_convergence_fast_forward_enabled,
        is_scribe_enabled,
        is_weekly_pricing_update_enabled,
    )
    from orchestration.workflow_manager import list_workflows

    sync_settings_if_modified()
    expiry_hours = get_thread_skill_expiration_time()
    daily_budget = get_daily_budget_usd()
    ws_ctx = get_workspace_context()
    is_admin = is_slack_admin(user_id)
    agents_cfg = get_agents_config()

    # Preload catalog and env once for all agent dropdowns to avoid repeated disk reads
    from core.model_catalog import _get_project_env_path, load_provider_catalog

    cached_catalog = load_provider_catalog()
    cached_env: dict[str, Any] = {}
    env_file = _get_project_env_path()
    if env_file.exists():
        try:
            cached_env.update(dotenv_values(env_file))
        except Exception as e:  # noqa: BLE001
            logger.debug("Failed to read project env file: %s", e)
    for k, v in os.environ.items():
        if k not in cached_env or not cached_env[k]:
            cached_env[k] = v

    if enabled_agents is None:
        enabled_agents = get_enabled_agents()

    if circuit_breaker_config is None:
        circuit_breaker_config = get_circuit_breaker_config()

    if scribe_enabled is None:
        scribe_on = is_scribe_enabled()
    else:
        scribe_on = bool(scribe_enabled)

    scribe_status_str = "🟢 *Enabled (ON)*" if scribe_on else "🔴 *Disabled (OFF)*"

    if convergence_fast_forward_enabled is None:
        ff_on = is_convergence_fast_forward_enabled()
    else:
        ff_on = bool(convergence_fast_forward_enabled)

    ff_status_str = "🟢 *Enabled (ON)*" if ff_on else "🔴 *Disabled (OFF)*"

    if weekly_pricing_update_enabled is None:
        weekly_pricing_on = is_weekly_pricing_update_enabled()
    else:
        weekly_pricing_on = bool(weekly_pricing_update_enabled)

    weekly_pricing_status_str = "🟢 *Enabled (ON)*" if weekly_pricing_on else "🔴 *Disabled (OFF)*"

    if hasattr(circuit_breaker_config, "to_dict"):
        cb_dict = circuit_breaker_config.to_dict()
    elif isinstance(circuit_breaker_config, dict):
        cb_dict = circuit_breaker_config
    else:
        cb_dict = {}

    cb_enabled = bool(cb_dict.get("enabled", False))
    cb_threshold = float(cb_dict.get("threshold_usd", 0.0))
    cb_status_str = (
        f"🟢 *Enabled (ON)* — Limit: *${cb_threshold:.2f} USD*"
        if cb_enabled
        else "🔴 *Disabled (OFF)*"
    )

    orch_mode = get_orchestration_mode()
    sandwich_cfg = get_sandwich_config()
    fixed_cfg = get_fixed_config()

    total_agents = len(agents_cfg)
    active_count = len(enabled_agents)

    blocks: list[dict[str, Any]] = []

    # Non-admin notice banner
    if user_id is not None and not is_admin:
        blocks.extend(
            [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            "🔒 *View-Only Mode*: You are viewing this panel in read-only mode. "
                            "Only Slack workspace administrators can modify agent states, models, and limits."
                        ),
                    },
                },
                {"type": "divider"},
            ]
        )

    # Emergency Backup Alert Banner if running from backup
    if agent_settings_module.is_running_from_backup():
        err_reason = agent_settings_module.get_backup_error_reason() or "Settings file unavailable."
        blocks.extend(
            [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            "⚠️ *WARNING: System running in EMERGENCY BACKUP mode!*\n"
                            f"The main settings file (`agent_settings.json`) is corrupted or unavailable ({err_reason}).\n"
                            "Settings have been temporarily restored from backup. "
                            "Click the button to restore settings from backup into active configuration:"
                        ),
                    },
                    "accessory": {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "🔄 Restore from backup",
                            "emoji": True,
                        },
                        "action_id": "restore_from_backup",
                        "style": "danger",
                    },
                },
                {"type": "divider"},
            ]
        )

    ws_block = build_workspace_context_block(ws_ctx, is_admin)

    blocks.extend(
        [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "────────🤖 Izvor Multi-Agent Panel Control Hub────────\n",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "*Welcome to Izvor Control Hub!*\n"
                        "Manage debate agents and spend limits (circuit-breaker) here.\n\n"
                        f"💡 *Note:* Disabled agents can still be directly invoked by tagging (e.g. `[{_get_example_agent_id(agents_cfg)}]`)."
                    ),
                },
            },
            build_panel_status_block(active_count, total_agents, cb_enabled, cb_threshold),
            {"type": "divider"},
            ws_block,
            {"type": "divider"},
        ]
    )

    # Governance blocks (Circuit breaker, scribe, fast-forward, weekly pricing, TTL)
    blocks.extend(
        build_governance_blocks(
            cb_enabled=cb_enabled,
            cb_threshold=cb_threshold,
            cb_status_str=cb_status_str,
            scribe_on=scribe_on,
            scribe_status_str=scribe_status_str,
            ff_on=ff_on,
            ff_status_str=ff_status_str,
            weekly_pricing_on=weekly_pricing_on,
            weekly_pricing_status_str=weekly_pricing_status_str,
            expiry_hours=expiry_hours,
            daily_budget=daily_budget,
        )
    )

    # Workflows blocks
    workflows = list_workflows()
    active_wf_id = get_active_workflow_id()
    blocks.extend(
        build_workflows_blocks(
            orch_mode=orch_mode,
            active_wf_id=active_wf_id,
            workflows=workflows,
            sandwich_cfg=sandwich_cfg,
            fixed_cfg=fixed_cfg,
            agents_cfg=agents_cfg,
        )
    )

    # Bulk agent toggle actions
    blocks.extend(build_bulk_actions_blocks(active_count, total_agents))

    # Agent roster blocks
    blocks.extend(
        build_agent_roster_blocks(
            agents_cfg=agents_cfg,
            enabled_agents=enabled_agents,
            cached_env=cached_env,
            cached_catalog=cached_catalog,
            current_block_count=len(blocks),
        )
    )

    # Ensure total block count never exceeds 95 (Slack hard limit is 100 blocks per view)
    if len(blocks) > 95:
        blocks = blocks[:94]
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "⚠️ _Remaining configuration items truncated to stay within Slack view limits._",
                    }
                ],
            }
        )

    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "⚡ _Izvor Multi-Agent Platform • Changes take effect immediately for all new discussions._",
                }
            ],
        }
    )

    return {"type": "home", "blocks": blocks}


__all__ = [
    "build_admin_required_modal",
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
