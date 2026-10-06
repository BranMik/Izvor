import hashlib
import logging
from typing import Any

from core.config import AGENTS_CONFIG
from core.models import OrchestrationMode

logger = logging.getLogger(__name__)


def build_app_home_view(
    enabled_agents: set[str] | None = None,
    circuit_breaker_config: dict[str, Any] | None = None,
    scribe_enabled: bool | None = None,
    weekly_pricing_update_enabled: bool | None = None,
) -> dict[str, Any]:
    """
    Generates the Block Kit Home Tab view for the Slack Application Home page.
    Displays panel agents, locked agents, thread cost circuit-breaker settings,
    Live Discussion Scribe toggle, and emergency backup warning if active settings file is unreadable.
    """
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
        sync_settings_if_modified,
    )
    from orchestration.app_settings import get_thread_skill_expiration_time
    from orchestration.workflow_manager import list_workflows

    sync_settings_if_modified()
    expiry_hours = get_thread_skill_expiration_time()

    if enabled_agents is None:
        enabled_agents = get_enabled_agents()

    if circuit_breaker_config is None:
        circuit_breaker_config = get_circuit_breaker_config()

    if scribe_enabled is None:
        scribe_on = is_scribe_enabled()
    else:
        scribe_on = bool(scribe_enabled)

    scribe_status_str = "🟢 *Enabled (ON)*" if scribe_on else "🔴 *Disabled (OFF)*"

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

    blocks.extend(
        [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "────────🤖 CaiLogics Multi-Agent Panel Control Hub────────\n",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "*Welcome to CaiLogics Control Hub!*\n"
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
        icon = "🥪" if wf.mode == OrchestrationMode.SANDWICH.value else "🎯"
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
                active_wf_name = f"{'🥪' if wf.mode == OrchestrationMode.SANDWICH.value else '🎯'} {wf.name}"
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
                        f"Current mode: *{'🥪 Sandwich Chain (Start ➔ Middle ➔ End)' if orch_mode == OrchestrationMode.SANDWICH else '🎯 Fixed Sequence'}*"
                    ),
                },
            }
        ]
    )

    if not (active_wf_id in ("default_review_flow", "default_sandwich")):
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
                        "text": "🎯 Fixed Sequence",
                        "emoji": True,
                    },
                    "value": f"mode:{OrchestrationMode.FIXED.value}",
                },
            ],
            "initial_option": {
                "text": {
                    "type": "plain_text",
                    "text": "🥪 Sandwich Flow"
                    if orch_mode == OrchestrationMode.SANDWICH
                    else "🎯 Fixed Sequence",
                    "emoji": True,
                },
                "value": f"mode:{orch_mode}",
            },
        }

    is_default_wf = active_wf_id in ("default_review_flow", "default_sandwich")

    # Mode-specific chain configuration UI
    if orch_mode == OrchestrationMode.SANDWICH:
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

    from core.model_catalog import get_available_models

    # Render all agents as fully toggleable items with dynamic model dropdowns
    for key, cfg in AGENTS_CONFIG.items():
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

        # Dynamic model selection dropdown based on .env API keys
        option_groups, initial_opt = get_available_models(current_model=model)
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
        blocks.append({"type": "divider"})

    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "⚡ _CaiLogics Multi-Agent Platform • Changes take effect immediately for all new discussions._",
                }
            ],
        }
    )

    return {"type": "home", "blocks": blocks}


__all__ = ["build_app_home_view"]
