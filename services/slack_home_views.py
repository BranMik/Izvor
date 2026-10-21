import hashlib
import logging
import os
from typing import Any

from core.config import AGENTS_CONFIG
from core.models import OrchestrationMode

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
    from core.config import is_slack_admin
    from orchestration.agent_settings import (
        get_active_workflow_id,
        get_backup_error_reason,
        get_circuit_breaker_config,
        get_enabled_agents,
        get_fixed_config,
        get_orchestration_mode,
        get_sandwich_config,
        is_running_from_backup,
        is_scribe_enabled,
        is_weekly_pricing_update_enabled,
        is_convergence_fast_forward_enabled,
        sync_settings_if_modified,
    )
    from orchestration.app_settings import (
        get_thread_skill_expiration_time,
        get_workspace_context,
    )
    from orchestration.workflow_manager import list_workflows

    from core.model_catalog import (
        _get_project_env_path,
        get_available_models,
        load_provider_catalog,
    )
    from dotenv import dotenv_values

    sync_settings_if_modified()
    expiry_hours = get_thread_skill_expiration_time()
    ws_ctx = get_workspace_context()
    is_admin = is_slack_admin(user_id)

    # Preload catalog and env once for all agent dropdowns to avoid repeated disk reads
    cached_catalog = load_provider_catalog()
    cached_env: dict[str, Any] = {}
    env_file = _get_project_env_path()
    if env_file.exists():
        try:
            cached_env.update(dotenv_values(env_file))
        except Exception:
            pass
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

    cb_enabled = bool(circuit_breaker_config.get("enabled", False))
    cb_threshold = float(circuit_breaker_config.get("threshold_usd", 0.0))
    if not cb_enabled:
        cb_threshold = 0.0
    cb_status_str = (
        f"🟢 *Enabled (ON)* — Limit: *${cb_threshold:.2f} USD*"
        if cb_enabled
        else "🔴 *Disabled (OFF)*"
    )

    orch_mode = get_orchestration_mode()
    sandwich_cfg = get_sandwich_config()
    fixed_cfg = get_fixed_config()

    start_chain_str = ", ".join(AGENTS_CONFIG.get(a, {}).get("username", a) for a in sandwich_cfg.get("start_chain", []))
    end_chain_str = ", ".join(AGENTS_CONFIG.get(a, {}).get("username", a) for a in sandwich_cfg.get("end_chain", []))
    fixed_chain_str = ", ".join(AGENTS_CONFIG.get(a, {}).get("username", a) for a in fixed_cfg.get("chain", []))

    fixed_hash = hashlib.md5((", ".join(fixed_cfg.get("chain", []))).encode("utf-8")).hexdigest()[:8]
    start_hash = hashlib.md5((", ".join(sandwich_cfg.get("start_chain", []))).encode("utf-8")).hexdigest()[:8]
    end_hash = hashlib.md5((", ".join(sandwich_cfg.get("end_chain", []))).encode("utf-8")).hexdigest()[:8]

    total_agents = len(AGENTS_CONFIG)
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
    if is_running_from_backup():
        err_reason = get_backup_error_reason() or "Settings file unavailable."
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

    ws_display = f"> {ws_ctx}" if ws_ctx else "_No baseline workspace context configured yet._"
    role_note = "_(✏️ Admin editable)_" if is_admin else "_(🔒 View-only: Admin managed)_"
    ws_section_text = (
        f"*────────🌐 Workspace Baseline Context:────────*\n"
        f"{ws_display}\n\n"
        f"💡 *Scope:* Persistent across all debate agents and threads. {role_note}"
    )

    ws_block: dict[str, Any] = {
        "type": "section",
        "block_id": "workspace_context_display_section",
        "text": {
            "type": "mrkdwn",
            "text": ws_section_text,
        },
    }
    if is_admin:
        ws_block["accessory"] = {
            "type": "button",
            "action_id": "open_edit_workspace_context_modal",
            "text": {
                "type": "plain_text",
                "text": "✏️ Edit Context",
                "emoji": True,
            },
            "style": "primary",
        }

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
                        "💡 *Note:* Disabled agents can still be directly invoked by tagging (e.g. `[agent-10011]`)."
                    ),
                },
            },
            {
                "type": "section",
                "block_id": f"cb_panel_status_section_{'on' if cb_enabled else 'off'}_{cb_threshold:.2f}",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*📊 Panel Status:*\n🟢 *{active_count}* / *{total_agents}* agents active",
                    },
                ],
            },
            {"type": "divider"},
            ws_block,
            {"type": "divider"},
            {
                "type": "section",
                "block_id": f"cb_toggle_section_{'on' if cb_enabled else 'off'}_{cb_threshold:.2f}",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"\n *────────🛡️ Thread Circuit Breaker (Cost Safety Limit):────────* \n"
                        f"Status: {cb_status_str}\n"
                    ),
                },
                "accessory": {
                    "type": "radio_buttons",
                    "action_id": "toggle_circuit_breaker_radio",
                    "options": [
                        {
                            "text": {
                                "type": "plain_text",
                                "text": "🟢 ON",
                                "emoji": True,
                            },
                            "value": "cb:on",
                        },
                        {
                            "text": {
                                "type": "plain_text",
                                "text": "🔴 OFF",
                                "emoji": True,
                            },
                            "value": "cb:off",
                        },
                    ],
                    "initial_option": {
                        "text": {
                            "type": "plain_text",
                            "text": "🟢 ON" if cb_enabled else "🔴 OFF",
                            "emoji": True,
                        },
                        "value": "cb:on" if cb_enabled else "cb:off",
                    },
                },
            },
            {
                "type": "input",
                "block_id": f"cb_threshold_block_{cb_threshold:.2f}",
                "dispatch_action": True,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "set_circuit_breaker_threshold_input",
                    "initial_value": f"{cb_threshold:.2f}",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Enter amount in USD (e.g. 0.05, 0.50, 1.00)",
                    },
                    "dispatch_action_config": {
                        "trigger_actions_on": ["on_enter_pressed"],
                    },
                },
                "label": {
                    "type": "plain_text",
                    "text": "💵 Maximum limit per thread ($ USD):",
                    "emoji": True,
                },
            },
            {
                "type": "actions",
                "block_id": f"cb_actions_block_{cb_threshold:.2f}_{'on' if cb_enabled else 'off'}",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "💾 Save limit",
                            "emoji": True,
                        },
                        "action_id": "save_circuit_breaker_threshold_button",
                        "style": "primary",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "$0.05"},
                        "action_id": "set_circuit_breaker_preset:0.05",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "$0.25"},
                        "action_id": "set_circuit_breaker_preset:0.25",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "$0.50"},
                        "action_id": "set_circuit_breaker_preset:0.50",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "$1.00"},
                        "action_id": "set_circuit_breaker_preset:1.00",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "$2.00"},
                        "action_id": "set_circuit_breaker_preset:2.00",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "$5.00"},
                        "action_id": "set_circuit_breaker_preset:5.00",
                    },
                ],
            },
            {"type": "divider"},
            {
                "type": "section",
                "block_id": f"scribe_toggle_section_{'on' if scribe_on else 'off'}",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"\n *────────📝 Live Discussion Scribe:────────* \n"
                        f"Status: {scribe_status_str}\n"
                        f"_Generates and live-updates synthesis notes in discussion threads._"
                    ),
                },
                "accessory": {
                    "type": "radio_buttons",
                    "action_id": "toggle_scribe_radio",
                    "options": [
                        {
                            "text": {
                                "type": "plain_text",
                                "text": "🟢 ON",
                                "emoji": True,
                            },
                            "value": "scribe:on",
                        },
                        {
                            "text": {
                                "type": "plain_text",
                                "text": "🔴 OFF",
                                "emoji": True,
                            },
                            "value": "scribe:off",
                        },
                    ],
                    "initial_option": {
                        "text": {
                            "type": "plain_text",
                            "text": "🟢 ON" if scribe_on else "🔴 OFF",
                            "emoji": True,
                        },
                        "value": "scribe:on" if scribe_on else "scribe:off",
                    },
                },
            },
            {"type": "divider"},
            {
                "type": "section",
                "block_id": f"convergence_ff_toggle_section_{'on' if ff_on else 'off'}",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"\n *────────⚡ Consensus Convergence Fast-Forward:────────* \n"
                        f"Status: {ff_status_str}\n"
                        f"_When ON, consecutive consensus signals trigger the Challenger → Rebuttal stress-test and fast-forward to closing if the consensus holds._"
                    ),
                },
                "accessory": {
                    "type": "radio_buttons",
                    "action_id": "toggle_convergence_fast_forward_radio",
                    "options": [
                        {
                            "text": {"type": "plain_text", "text": "🟢 ON", "emoji": True},
                            "value": "convergence_ff:on",
                        },
                        {
                            "text": {"type": "plain_text", "text": "🔴 OFF", "emoji": True},
                            "value": "convergence_ff:off",
                        },
                    ],
                    "initial_option": {
                        "text": {
                            "type": "plain_text",
                            "text": "🟢 ON" if ff_on else "🔴 OFF",
                            "emoji": True,
                        },
                        "value": "convergence_ff:on" if ff_on else "convergence_ff:off",
                    },
                },
            },
            {"type": "divider"},
            {
                "type": "section",
                "block_id": f"weekly_pricing_toggle_section_{'on' if weekly_pricing_on else 'off'}",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"\n *────────🏷️ Weekly Model Pricing Updates:────────* \n"
                        f"Status: {weekly_pricing_status_str}\n"
                        f"_Controls weekly token pricing refresh. When OFF, models with existing pricing will not update, while models without set pricing will still fetch pricing._"
                    ),
                },
                "accessory": {
                    "type": "radio_buttons",
                    "action_id": "toggle_weekly_pricing_update_radio",
                    "options": [
                        {
                            "text": {
                                "type": "plain_text",
                                "text": "🟢 ON",
                                "emoji": True,
                            },
                            "value": "pricing_update:on",
                        },
                        {
                            "text": {
                                "type": "plain_text",
                                "text": "🔴 OFF",
                                "emoji": True,
                            },
                            "value": "pricing_update:off",
                        },
                    ],
                    "initial_option": {
                        "text": {
                            "type": "plain_text",
                            "text": "🟢 ON" if weekly_pricing_on else "🔴 OFF",
                            "emoji": True,
                        },
                        "value": "pricing_update:on" if weekly_pricing_on else "pricing_update:off",
                    },
                },
            },
            {"type": "divider"},
            {
                "type": "section",
                "block_id": f"thread_skill_expiry_section_{expiry_hours:.1f}",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"\n *────────⏳ Thread Global Skills Expiration Time:────────* \n"
                        f"Current TTL: *{expiry_hours:.1f} hours*\n"
                        f"_Controls how long thread-global skills (`/grant_for_all`) remain active for agents._"
                    ),
                },
            },
            {
                "type": "actions",
                "block_id": f"thread_skill_expiry_actions_{expiry_hours:.1f}",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "1h" + (" (active)" if expiry_hours == 1.0 else "")},
                        "action_id": "set_thread_skill_expiry_preset:1.0",
                        **({"style": "primary"} if expiry_hours == 1.0 else {}),
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "4h" + (" (active)" if expiry_hours == 4.0 else "")},
                        "action_id": "set_thread_skill_expiry_preset:4.0",
                        **({"style": "primary"} if expiry_hours == 4.0 else {}),
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "6h" + (" (active)" if expiry_hours == 6.0 else "")},
                        "action_id": "set_thread_skill_expiry_preset:6.0",
                        **({"style": "primary"} if expiry_hours == 6.0 else {}),
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "12h" + (" (active)" if expiry_hours == 12.0 else "")},
                        "action_id": "set_thread_skill_expiry_preset:12.0",
                        **({"style": "primary"} if expiry_hours == 12.0 else {}),
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "24h" + (" (active)" if expiry_hours == 24.0 else "")},
                        "action_id": "set_thread_skill_expiry_preset:24.0",
                        **({"style": "primary"} if expiry_hours == 24.0 else {}),
                    },
                ],
            },
        ]
    )

    workflows = list_workflows()
    active_wf_id = get_active_workflow_id()

    wf_options = []
    for wf in workflows:
        icon = "🥪" if wf.mode == OrchestrationMode.SANDWICH.value else ("⚡" if wf.mode == OrchestrationMode.SCATTER_GATHER.value else "🎯")
        label = f"{icon} {wf.name}"
        if len(label) > 75:
            label = label[:72] + "..."
        wf_options.append(
            {
                "text": {
                    "type": "plain_text",
                    "text": label,
                    "emoji": True,
                },
                "value": f"wf:{wf.id}",
            }
        )

    initial_wf_option = None
    if active_wf_id:
        for opt in wf_options:
            if opt["value"] == f"wf:{active_wf_id}":
                initial_wf_option = opt
                break
    if not initial_wf_option and wf_options:
        initial_wf_option = wf_options[0]

    active_wf_name = "Custom / Unsaved"
    if active_wf_id:
        for wf in workflows:
            if wf.id == active_wf_id:
                active_icon = "🥪" if wf.mode == OrchestrationMode.SANDWICH.value else ("⚡" if wf.mode == OrchestrationMode.SCATTER_GATHER.value else "🎯")
                active_wf_name = f"{active_icon} {wf.name}"
                break

    preset_select_element: dict[str, Any] = {
        "type": "static_select",
        "action_id": "select_workflow_preset",
        "placeholder": {
            "type": "plain_text",
            "text": "Choose a preset...",
            "emoji": True,
        },
        "options": wf_options
        if wf_options
        else [
            {
                "text": {
                    "type": "plain_text",
                    "text": "No saved presets",
                    "emoji": True,
                },
                "value": "wf:none",
            }
        ],
    }
    if initial_wf_option:
        preset_select_element["initial_option"] = initial_wf_option

    blocks.extend(
        [
            {"type": "divider"},
            {
                "type": "section",
                "block_id": f"workflow_preset_select_block_{active_wf_id or 'none'}",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f" *────────📑 Saved Workflow Presets:────────* \n"
                        f"Active preset: *{active_wf_name}*\n"
                        f"_Select a preset to load or delete from `data/workflows/`:_"
                    ),
                },
                "accessory": preset_select_element,
            }
        ]
    )

    if active_wf_id not in ("default_review_flow", "default_sandwich"):
        blocks.append(
            {
                "type": "actions",
                "block_id": f"workflow_preset_actions_block_{active_wf_id or 'none'}",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "🗑️ Delete Selected Preset",
                            "emoji": True,
                        },
                        "action_id": "delete_workflow_preset_button",
                        "style": "danger",
                    },
                ],
            }
        )

    blocks.extend(
        [
            {"type": "divider"},
            {"type": "divider"},
            {
                "type": "section",
                "block_id": f"orchestration_mode_section_{orch_mode}",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"\n *────────🔀 Discussion Orchestration Mode:────────* \n"
                        f"Current mode: *{'🥪 Sandwich Chain (Start ➔ Middle ➔ End)' if (orch_mode == OrchestrationMode.SANDWICH or orch_mode == OrchestrationMode.SANDWICH.value) else ('⚡ Parallel Dialectic (Scatter-Gather)' if (orch_mode == OrchestrationMode.SCATTER_GATHER or orch_mode == OrchestrationMode.SCATTER_GATHER.value) else '🎯 Fixed Sequence')}*"
                    ),
                },
            }
        ]
    )

    if not (active_wf_id in ("default_review_flow", "default_sandwich", "parallel_dialectic_flow")):
        initial_label = "🥪 Sandwich Flow"
        if orch_mode in (OrchestrationMode.SCATTER_GATHER, OrchestrationMode.SCATTER_GATHER.value):
            initial_label = "⚡ Parallel Dialectic"
        elif orch_mode in (OrchestrationMode.FIXED, OrchestrationMode.FIXED.value):
            initial_label = "🎯 Fixed Sequence"

        blocks[-1]["accessory"] = {
            "type": "radio_buttons",
            "action_id": "select_orchestration_mode",
            "options": [
                {
                    "text": {
                        "type": "plain_text",
                        "text": "🥪 Sandwich Flow",
                        "emoji": True,
                    },
                    "value": f"mode:{OrchestrationMode.SANDWICH.value}",
                },
                {
                    "text": {
                        "type": "plain_text",
                        "text": "⚡ Parallel Dialectic",
                        "emoji": True,
                    },
                    "value": f"mode:{OrchestrationMode.SCATTER_GATHER.value}",
                },
                {
                    "text": {
                        "type": "plain_text",
                        "text": "🎯 Fixed Sequence",
                        "emoji": True,
                    },
                    "value": f"mode:{OrchestrationMode.FIXED.value}",
                },
            ],
            "initial_option": {
                "text": {
                    "type": "plain_text",
                    "text": initial_label,
                    "emoji": True,
                },
                "value": f"mode:{orch_mode if isinstance(orch_mode, str) else orch_mode.value}",
            },
        }

    is_default_wf = active_wf_id in ("default_review_flow", "default_sandwich", "parallel_dialectic_flow")

    # Mode-specific chain configuration UI
    if orch_mode in (OrchestrationMode.SANDWICH, OrchestrationMode.SANDWICH.value):
        blocks.extend(
            [
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": "ℹ️ *Sandwich Flow:* Begins with _Start Chain_, continues with remaining active agents in the _middle (flexible)_, and concludes with _End Chain_. If chains are empty, order is 100% flexible.",
                        }
                    ],
                }
            ]
        )
        if is_default_wf:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"🚫 *Editing Disabled for System Defaults*\n*🚩 Start Chain:* {start_chain_str or 'None'}\n*🏁 End Chain:* {end_chain_str or 'None'}"
                }
            })
        else:
            blocks.extend([
                {
                    "type": "input",
                    "block_id": f"sandwich_start_chain_block_{start_hash}",
                    "dispatch_action": True,
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "set_start_chain_input",
                        "initial_value": start_chain_str,
                        "placeholder": {
                            "type": "plain_text",
                            "text": "e.g. Visionary, Innovator (comma-separated)",
                        },
                    },
                    "label": {
                        "type": "plain_text",
                        "text": "🚩 Start Chain:",
                        "emoji": True,
                    },
                },
                {
                    "type": "input",
                    "block_id": f"sandwich_end_chain_block_{end_hash}",
                    "dispatch_action": True,
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "set_end_chain_input",
                        "initial_value": end_chain_str,
                        "placeholder": {
                            "type": "plain_text",
                            "text": "e.g. Security Architect (comma-separated)",
                        },
                    },
                    "label": {
                        "type": "plain_text",
                        "text": "🏁 End Chain:",
                        "emoji": True,
                    },
                },
                {
                    "type": "actions",
                    "block_id": f"sandwich_chain_actions_{start_hash}_{end_hash}",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "💾 Save Sandwich chains",
                                "emoji": True,
                            },
                            "action_id": "save_sandwich_chains_button",
                            "style": "primary",
                        },
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "🗑️ Clear chains (Flexible)",
                                "emoji": True,
                            },
                            "action_id": "clear_sandwich_chains_button",
                        },
                    ],
                },
            ])
    elif orch_mode in (OrchestrationMode.SCATTER_GATHER, OrchestrationMode.SCATTER_GATHER.value):
        blocks.extend(
            [
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": "ℹ️ *Parallel Dialectic Flow:* Specialist agents explore simultaneously in parallel with zero-anchor isolation snapshots, followed by dialectical synthesis via the closing agent.",
                        }
                    ],
                }
            ]
        )
        if is_default_wf:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"🚫 *Editing Disabled for System Defaults*\n*🚩 Parallel Specialists:* {start_chain_str or 'None'}\n*🏁 Synthesis Agent:* {end_chain_str or 'None'}",
                },
            })
        else:
            blocks.extend([
                {
                    "type": "input",
                    "block_id": f"sandwich_start_chain_block_{start_hash}",
                    "dispatch_action": True,
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "set_start_chain_input",
                        "initial_value": start_chain_str,
                        "placeholder": {
                            "type": "plain_text",
                            "text": "e.g. Visionary, Innovator (comma-separated)",
                        },
                    },
                    "label": {
                        "type": "plain_text",
                        "text": "🚩 Parallel Specialists:",
                        "emoji": True,
                    },
                },
                {
                    "type": "input",
                    "block_id": f"sandwich_end_chain_block_{end_hash}",
                    "dispatch_action": True,
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "set_end_chain_input",
                        "initial_value": end_chain_str,
                        "placeholder": {
                            "type": "plain_text",
                            "text": "e.g. Security Architect (comma-separated)",
                        },
                    },
                    "label": {
                        "type": "plain_text",
                        "text": "🏁 Synthesis Agent:",
                        "emoji": True,
                    },
                },
                {
                    "type": "actions",
                    "block_id": f"sandwich_chain_actions_{start_hash}_{end_hash}",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "💾 Save Parallel chains",
                                "emoji": True,
                            },
                            "action_id": "save_sandwich_chains_button",
                            "style": "primary",
                        },
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "🗑️ Clear chains",
                                "emoji": True,
                            },
                            "action_id": "clear_sandwich_chains_button",
                        },
                    ],
                },
            ])
    else:
        blocks.extend(
            [
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": "ℹ️ *Fixed Sequence Flow:* Only listed agents participate in strict sequence. If chain is empty, all active agents participate in flexible order.",
                        }
                    ],
                }
            ]
        )
        if is_default_wf:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"🚫 *Editing Disabled for System Defaults*\n*🎯 Fixed Sequence Order:* {fixed_chain_str or 'None'}"
                }
            })
        else:
            blocks.extend([
                {
                    "type": "input",
                    "block_id": f"fixed_chain_block_{fixed_hash}",
                    "dispatch_action": True,
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "set_fixed_chain_input",
                        "initial_value": fixed_chain_str,
                        "placeholder": {
                            "type": "plain_text",
                            "text": "e.g. LegalAdvisor, Innovator, Security Architect (comma-separated)",
                        },
                    },
                    "label": {
                        "type": "plain_text",
                        "text": "🎯 Fixed Sequence Order:",
                        "emoji": True,
                    },
                },
                {
                    "type": "actions",
                    "block_id": f"fixed_chain_actions_{fixed_hash}",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "💾 Save Fixed chain",
                                "emoji": True,
                            },
                            "action_id": "save_fixed_chain_button",
                            "style": "primary",
                        },
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "🗑️ Clear chain (Flexible)",
                                "emoji": True,
                            },
                            "action_id": "clear_fixed_chain_button",
                        },
                    ],
                },
            ])
    if active_wf_id not in ("default_review_flow", "default_sandwich"):
        blocks.extend(
            [
                {
                    "type": "input",
                    "block_id": f"save_workflow_preset_block_{active_wf_id or 'none'}",
                    "dispatch_action": False,
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "save_workflow_preset_name_input",
                        "placeholder": {
                            "type": "plain_text",
                            "text": "e.g. Full Security Audit",
                        },
                    },
                    "label": {
                        "type": "plain_text",
                        "text": "💾 Save Current Configuration as Preset:",
                        "emoji": True,
                    },
                },
                {
                    "type": "actions",
                    "block_id": f"save_workflow_preset_actions_{active_wf_id or 'none'}",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "💾 Save Preset",
                                "emoji": True,
                            },
                            "action_id": "save_workflow_preset_button",
                        },
                    ],
                }
            ]
        )

    blocks.extend(
        [
            {"type": "divider"},
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "\n *────────👥 Agent Panel Configuration (Enabled/Disabled):────────* ",
                },
            },
            {
                "type": "actions",
                "block_id": f"bulk_agent_actions_{active_count}_{total_agents}",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "✅ Enable all agents",
                            "emoji": True,
                        },
                        "action_id": "bulk_enable_all",
                        "style": "primary",
                    },
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "❌ Disable all agents",
                            "emoji": True,
                        },
                        "action_id": "bulk_disable_all",
                    },
                ],
            },
            {"type": "divider"},
        ]
    )

    # Render all agents as fully toggleable items with dynamic model dropdowns
    for i, (key, cfg) in enumerate(AGENTS_CONFIG.items()):
        if len(blocks) >= 92:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "⚠️ _Additional agents omitted to stay within Slack block limits._",
                    },
                }
            )
            break

        username = cfg.get("username", key)
        model = cfg.get("model", "")
        is_on = key in enabled_agents
        blocks.append(
            {
                "type": "section",
                "block_id": f"agent_toggle_block_{key}_{'on' if is_on else 'off'}",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{username}* [{key}]\n`{model}`",
                },
                "accessory": {
                    "type": "radio_buttons",
                    "action_id": f"toggle_agent_radio:{key}",
                    "options": [
                        {
                            "text": {
                                "type": "plain_text",
                                "text": "🟢 ON",
                                "emoji": True,
                            },
                            "value": f"{key}:on",
                        },
                        {
                            "text": {
                                "type": "plain_text",
                                "text": "🔴 OFF",
                                "emoji": True,
                            },
                            "value": f"{key}:off",
                        },
                    ],
                    "initial_option": {
                        "text": {
                            "type": "plain_text",
                            "text": "🟢 ON" if is_on else "🔴 OFF",
                            "emoji": True,
                        },
                        "value": f"{key}:on" if is_on else f"{key}:off",
                    },
                },
            }
        )

        # Dynamic model selection dropdown based on cached .env API keys & catalog
        option_groups, initial_opt = get_available_models(
            current_model=model,
            env_dict=cached_env,
            catalog=cached_catalog,
        )
        model_block_id = (
            f"agent_model_block_{key}_{model.replace('/', '_').replace('.', '_')}"
        )
        model_section = {
            "type": "section",
            "block_id": model_block_id,
            "text": {
                "type": "mrkdwn",
                "text": "⚙️ *Model:*",
            },
            "accessory": {
                "type": "static_select",
                "action_id": f"select_agent_model:{key}",
                "placeholder": {
                    "type": "plain_text",
                    "text": "Select model",
                    "emoji": True,
                },
                "option_groups": option_groups,
            },
        }
        if initial_opt:
            model_section["accessory"]["initial_option"] = initial_opt
        blocks.append(model_section)
        if i < len(AGENTS_CONFIG) - 1 and len(blocks) < 92:
            blocks.append({"type": "divider"})

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


def build_edit_workspace_context_modal(current_context: str = "") -> dict[str, Any]:
    """
    Constructs the Block Kit modal dialog for editing the global Workspace Baseline Context.
    Capped at 1,000 characters.
    """
    return {
        "type": "modal",
        "callback_id": "submit_workspace_context_modal",
        "title": {
            "type": "plain_text",
            "text": "Workspace Context",
            "emoji": True,
        },
        "submit": {
            "type": "plain_text",
            "text": "Save",
            "emoji": True,
        },
        "close": {
            "type": "plain_text",
            "text": "Cancel",
            "emoji": True,
        },
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "Provide global background, organizational guidelines, or rules that *all debate agents* "
                        "receive in their system prompt across all threads.\n\n"
                        "🔒 *Admin only. Maximum 1,000 characters.*"
                    ),
                },
            },
            {
                "type": "input",
                "block_id": "workspace_context_input_block",
                "label": {
                    "type": "plain_text",
                    "text": "Baseline Context / Guidelines",
                    "emoji": True,
                },
                "element": {
                    "type": "plain_text_input",
                    "action_id": "workspace_context_input",
                    "multiline": True,
                    "max_length": 1000,
                    "initial_value": current_context or "",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "e.g., We are Acme Corp building enterprise healthcare software. Always prioritize HIPAA compliance, user privacy, and scalable cloud architectures.",
                    },
                },
                "optional": True,
            },
        ],
    }


__all__ = ["build_app_home_view", "build_edit_workspace_context_modal"]
