import hashlib
import logging
import time
from pathlib import Path
from typing import Any

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from core.config import (
    AGENTS_CONFIG,
    MCP_WORKSPACE_DIR,
    SLACK_BOT_TOKEN,
    USAGE_CHANNEL_ID,
    USAGE_CHANNEL_NAME,
    resolve_avatar_url,
)
from core.models import OrchestrationMode

logger = logging.getLogger(__name__)

# Initialize async web client
slack_async_client = AsyncWebClient(token=SLACK_BOT_TOKEN)

# In-memory caches for channel metadata
CHANNEL_NAME_CACHE: dict[str, str] = {}
_RESOLVED_USAGE_CHANNEL_ID: str | None = None


def build_app_home_view(
    enabled_agents: set[str] | None = None,
    circuit_breaker_config: dict[str, Any] | None = None,
    scribe_enabled: bool | None = None,
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

    start_chain_str = ", ".join(sandwich_cfg.get("start_chain", []))
    end_chain_str = ", ".join(sandwich_cfg.get("end_chain", []))
    fixed_chain_str = ", ".join(fixed_cfg.get("chain", []))

    fixed_hash = hashlib.md5(fixed_chain_str.encode("utf-8")).hexdigest()[:8]
    start_hash = hashlib.md5(start_chain_str.encode("utf-8")).hexdigest()[:8]
    end_hash = hashlib.md5(end_chain_str.encode("utf-8")).hexdigest()[:8]

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
                        "💡 *Note:* Disabled agents can still be directly invoked by tagging (e.g. `[TechLead]`)."
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
            },
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
            },
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
                "accessory": {
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
                },
            },
        ]
    )

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
                },
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
                            "text": "e.g. Architect (comma-separated)",
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
            ]
        )
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
                },
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
                            "text": "e.g. TechLead, Innovator, Architect (comma-separated)",
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
            ]
        )

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
            },
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


async def publish_app_home(
    user_id: str,
    enabled_agents: set[str] | None = None,
    circuit_breaker_config: dict[str, Any] | None = None,
    scribe_enabled: bool | None = None,
) -> dict[str, Any]:
    """
    Publishes the multi-agent control panel view to the user's Slack App Home tab.
    """
    if not SLACK_BOT_TOKEN or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token":
        logger.info(f"[MOCK APP HOME] Would publish App Home view to user {user_id}")
        return {"ok": True, "mock": True}

    try:
        view = build_app_home_view(
            enabled_agents=enabled_agents,
            circuit_breaker_config=circuit_breaker_config,
            scribe_enabled=scribe_enabled,
        )
        response = await slack_async_client.views_publish(
            user_id=user_id,
            view=view,
        )
        logger.info(f"[SLACK] Published App Home view for user {user_id}")
        return response.data
    except SlackApiError as e:
        logger.error(
            f"Error publishing App Home view to user {user_id}: {e.response['error']}"
        )
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.error(f"Error publishing App Home view to user {user_id}: {e}")
        return {"ok": False, "error": str(e)}


async def post_agent_response(
    channel_id: str,
    thread_ts: str,
    text: str,
    username: str,
    icon_url: str | None = None,
) -> dict[str, Any]:
    """
    Posts a message to Slack thread using dynamic persona fields (chat:write.customize).
    """
    if not SLACK_BOT_TOKEN or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token":
        logger.warning(
            f"[MOCK POST] Channel: {channel_id}, Thread: {thread_ts}, Persona: {username} ({icon_url})\nText: {text}"
        )
        return {"ok": True, "mock": True}

    try:
        resolved_icon = resolve_avatar_url(icon_url)
        if resolved_icon:
            # Dynamic cache-busting based on file modification timestamp (mtime)
            mtime_v = None
            if icon_url:
                local_path = Path(str(icon_url).lstrip("/"))
                if not local_path.exists():
                    local_path = Path(__file__).resolve().parent.parent / str(
                        icon_url
                    ).lstrip("/")
                if local_path.exists():
                    mtime_v = int(local_path.stat().st_mtime)
            v_param = f"v={mtime_v}" if mtime_v else "v=3"
            sep = "&" if "?" in resolved_icon else "?"
            final_icon_url = f"{resolved_icon}{sep}{v_param}"
        else:
            final_icon_url = None

        logger.info(
            f"[SLACK] chat_postMessage channel={channel_id} thread_ts={thread_ts} as '{username}' icon_url='{final_icon_url}'"
        )

        response = await slack_async_client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=text,
            username=username,
            icon_url=final_icon_url,
        )
        return response.data
    except SlackApiError as e:
        logger.error(f"Error posting message to Slack: {e.response['error']}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error posting message to Slack: {e}")
        raise


async def post_system_message(
    channel_id: str,
    thread_ts: str,
    text: str,
    username: str | None = None,
    icon_url: str | None = None,
) -> dict[str, Any]:
    """
    Posts a system warning/notice message to a Slack thread using the System Agent persona.
    """
    default_username = "Panel System"
    default_icon = "static/v2/avatar_47.png"

    try:
        from core.config import SYSTEM_AGENT_AVATAR, SYSTEM_AGENT_USERNAME

        default_username = SYSTEM_AGENT_USERNAME
        default_icon = SYSTEM_AGENT_AVATAR
    except (ImportError, AttributeError) as e:
        logger.debug(f"[SLACK CLIENT] Fallback core.config persona lookup skipped: {e}")
    except Exception as e:
        logger.debug(
            f"[SLACK CLIENT] Unexpected error loading core.config persona: {e}"
        )

    try:
        from orchestration.agent_settings import settings_manager

        sys_cfg = settings_manager.system_agents_config.get("system_agent", {})
        if sys_cfg.get("username"):
            default_username = sys_cfg["username"]
        if sys_cfg.get("icon_url"):
            default_icon = sys_cfg["icon_url"]
    except (ImportError, AttributeError, KeyError) as e:
        logger.debug(
            f"[SLACK CLIENT] Fallback settings_manager persona lookup skipped: {e}"
        )
    except Exception as e:
        logger.debug(
            f"[SLACK CLIENT] Unexpected error loading settings_manager persona: {e}"
        )

    final_username = username or default_username
    final_icon = icon_url or default_icon

    # Strip square brackets from system messages to avoid triggering Slack agent nomination / bracket parsers
    sanitized_text = text.replace("[", "").replace("]", "")

    return await post_agent_response(
        channel_id=channel_id,
        thread_ts=thread_ts,
        text=sanitized_text,
        username=final_username,
        icon_url=final_icon,
    )


async def fetch_thread_history(
    channel_id: str, thread_ts: str, bot_user_id: str | None = None
) -> list[dict[str, str]]:
    """
    Fetches past messages in a Slack thread and formats them as LiteLLM conversation history.
    """
    if not SLACK_BOT_TOKEN or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token":
        return []

    try:
        response = await slack_async_client.conversations_replies(
            channel=channel_id, ts=thread_ts, limit=100
        )
        messages = response.get("messages", [])
        formatted_history: list[dict[str, str]] = []
        short_history_logging: list[dict[str, str]] = []

        for msg in messages:
            msg_text = msg.get("text", "")
            username = msg.get("username", "")
            subtype = msg.get("subtype")
            user_id = msg.get("user")

            # Ignore ephemeral status messages from thread conversation history
            if msg_text.strip().startswith(("_🔍", "_🌐", "_⚡", "_⏳")):
                continue

            # Determine role
            if subtype == "bot_message" or username or user_id == bot_user_id:
                persona_name = username if username else "Assistant"
                formatted_history.append(
                    {"role": "assistant", "content": f"[{persona_name}]: {msg_text}"}
                )
                short_history_logging.append(
                    {
                        "role": "assistant",
                        "content": f"[{persona_name}]: {msg_text[:20]}",
                    }
                )
            else:
                formatted_history.append({"role": "user", "content": msg_text})
                short_history_logging.append({"role": "user", "content": msg_text})

        logger.info(f"\n short_history:\n {short_history_logging}")
        return formatted_history
    except SlackApiError as e:
        logger.error(f"Error fetching thread history: {e.response['error']}")
        return []


def _sanitize_and_truncate_slack_text(text: str, max_chars: int = 3800) -> str:
    """Ensures Slack message text never exceeds Slack API limits (4000 characters)."""
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    last_nl = truncated.rfind("\n")
    if last_nl > max_chars - 300:
        truncated = truncated[:last_nl]
    return f"{truncated.strip()}\n\n_...[truncated to fit Slack limits]_"


async def post_thread_note(
    channel_id: str,
    thread_ts: str,
    text: str,
    pin: bool = True,
    username: str = "Live Discussion Scribe",
    icon_emoji: str = ":memo:",
) -> dict[str, Any]:
    """
    Posts a live updatable summary/notes message inside the given Slack thread.
    Optionally pins the note message for quick discovery.
    """
    text = _sanitize_and_truncate_slack_text(text)

    if not SLACK_BOT_TOKEN or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token":
        mock_ts = f"mock_note_{int(time.time() * 1000)}.000"
        logger.info(
            f"[MOCK NOTE] Posted mock thread note in channel {channel_id}, thread {thread_ts}: {mock_ts}"
        )
        return {"ok": True, "ts": mock_ts, "mock": True}

    try:
        response = await slack_async_client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=text,
            username=username,
            icon_emoji=icon_emoji,
        )
        msg_ts = response.get("ts")
        logger.info(
            f"[SLACK NOTE] Posted live note message {msg_ts} in thread {thread_ts} of channel {channel_id} ({username})"
        )

        if pin and msg_ts:
            try:
                await slack_async_client.pins_add(channel=channel_id, timestamp=msg_ts)
                logger.info(
                    f"[SLACK NOTE] Pinned note message {msg_ts} to channel {channel_id}"
                )
            except SlackApiError as pin_err:
                err = pin_err.response.get("error", "")
                if err != "already_pinned":
                    logger.debug(
                        f"[SLACK NOTE] Note message pinning skipped/failed: {err}"
                    )
            except (KeyError, TypeError) as pin_ex:
                logger.debug(f"[SLACK NOTE] Note message pinning skipped: {pin_ex}")
            except Exception as pin_ex:
                logger.debug(f"[SLACK NOTE] Note message pinning skipped: {pin_ex}")

        return {"ok": True, "ts": msg_ts}
    except SlackApiError as e:
        logger.error(
            f"[SLACK NOTE] Error posting thread note: {e.response.get('error', e)}"
        )
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.error(f"[SLACK NOTE] Unexpected error posting thread note: {e}")
        return {"ok": False, "error": str(e)}


async def update_thread_note(
    channel_id: str,
    message_ts: str,
    text: str,
) -> dict[str, Any]:
    """
    Updates an existing live summary/notes message in-place via Slack chat.update.
    """
    text = _sanitize_and_truncate_slack_text(text)

    if (
        not SLACK_BOT_TOKEN
        or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token"
        or (message_ts and str(message_ts).startswith("mock_"))
    ):
        logger.info(f"[MOCK NOTE] Updated mock note {message_ts} ({len(text)} chars)")
        return {"ok": True, "ts": message_ts, "mock": True}

    try:
        await slack_async_client.chat_update(
            channel=channel_id,
            ts=message_ts,
            text=text,
        )
        logger.info(f"[SLACK NOTE] Successfully updated live note message {message_ts}")
        return {"ok": True, "ts": message_ts}
    except SlackApiError as e:
        logger.error(
            f"[SLACK NOTE] Error updating note {message_ts}: {e.response.get('error', e)}"
        )
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.error(f"[SLACK NOTE] Unexpected error updating note {message_ts}: {e}")
        return {"ok": False, "error": str(e)}


async def get_channel_name(channel_id: str) -> str:
    """
    Fetches the human-readable channel name (e.g. '#general') with in-memory caching.
    """
    if not channel_id:
        return "#unknown"
    if channel_id in CHANNEL_NAME_CACHE:
        return CHANNEL_NAME_CACHE[channel_id]

    if not SLACK_BOT_TOKEN or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token":
        name = f"#{channel_id}"
        CHANNEL_NAME_CACHE[channel_id] = name
        return name

    try:
        res = await slack_async_client.conversations_info(channel=channel_id)
        ch_info = res.get("channel", {})
        name = ch_info.get("name")
        if name:
            formatted = f"#{name}"
            CHANNEL_NAME_CACHE[channel_id] = formatted
            return formatted
    except SlackApiError as e:
        logger.debug(
            f"[SLACK] SlackApiError fetching channel name for {channel_id}: {e}"
        )
    except (KeyError, TypeError) as e:
        logger.debug(
            f"[SLACK] Parsing error fetching channel name for {channel_id}: {e}"
        )
    except Exception as e:
        logger.debug(f"[SLACK] Could not fetch channel name for {channel_id}: {e}")

    fallback = f"#{channel_id}"
    CHANNEL_NAME_CACHE[channel_id] = fallback
    return fallback


async def get_or_create_usage_channel() -> str | None:
    """
    Resolves the ID of the 'cailogics-usage' channel.
    1. Checks USAGE_CHANNEL_ID env/config if specified.
    2. Checks in-memory cached channel ID.
    3. Searches workspace channels for 'cailogics-usage' (or USAGE_CHANNEL_NAME).
    4. If not found, automatically creates the channel and joins it.
    """
    global _RESOLVED_USAGE_CHANNEL_ID
    if USAGE_CHANNEL_ID:
        return USAGE_CHANNEL_ID

    if _RESOLVED_USAGE_CHANNEL_ID:
        return _RESOLVED_USAGE_CHANNEL_ID

    target_name = USAGE_CHANNEL_NAME.lstrip("#").strip().lower()

    if not SLACK_BOT_TOKEN or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token":
        _RESOLVED_USAGE_CHANNEL_ID = f"mock_{target_name}_channel"
        return _RESOLVED_USAGE_CHANNEL_ID

    try:
        cursor = None
        while True:
            res = await slack_async_client.conversations_list(
                types="public_channel,private_channel",
                limit=200,
                cursor=cursor,
            )
            channels = res.get("channels", [])
            for ch in channels:
                if ch.get("name", "").lower() == target_name:
                    ch_id = ch.get("id")
                    _RESOLVED_USAGE_CHANNEL_ID = ch_id
                    logger.info(
                        f"[SLACK] Found existing usage channel '{target_name}' with ID: {ch_id}"
                    )
                    if not ch.get("is_member", True) and ch_id:
                        try:
                            await slack_async_client.conversations_join(channel=ch_id)
                            logger.info(
                                f"[SLACK] Joined existing usage channel '{target_name}' ({ch_id})"
                            )
                        except SlackApiError as join_err:
                            logger.debug(
                                f"[SLACK] Non-critical join attempt for {target_name}: {join_err}"
                            )
                        except Exception as join_ex:
                            logger.debug(
                                f"[SLACK] Non-critical join attempt for {target_name}: {join_ex}"
                            )
                    return ch_id

            cursor = res.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        logger.info(
            f"[SLACK] Usage channel '{target_name}' not found. Creating channel..."
        )
        create_res = await slack_async_client.conversations_create(
            name=target_name,
            is_private=False,
        )
        created_ch = create_res.get("channel", {})
        ch_id = created_ch.get("id")
        if ch_id:
            _RESOLVED_USAGE_CHANNEL_ID = ch_id
            logger.info(
                f"[SLACK] Successfully created usage channel '{target_name}' with ID: {ch_id}"
            )
            return ch_id

    except SlackApiError as e:
        err = e.response.get("error", "")
        if err == "name_taken":
            logger.info(
                f"[SLACK] Channel '{target_name}' name_taken, attempting retrieval via list..."
            )
            try:
                res = await slack_async_client.conversations_list(
                    types="public_channel,private_channel", limit=200
                )
                for ch in res.get("channels", []):
                    if ch.get("name", "").lower() == target_name:
                        _RESOLVED_USAGE_CHANNEL_ID = ch.get("id")
                        return _RESOLVED_USAGE_CHANNEL_ID
            except SlackApiError as err:
                logger.debug(
                    f"[SLACK] conversations_list API error during channel resolution: {err}"
                )
            except Exception as err:
                logger.debug(
                    f"[SLACK] Unexpected error during channel resolution: {err}"
                )
        logger.error(
            f"[SLACK] Error resolving/creating usage channel '{target_name}': {e}"
        )
    except Exception as e:
        logger.error(f"[SLACK] Unexpected error resolving/creating usage channel: {e}")

    return None


async def post_usage_message(
    channel_id: str,
    text: str,
    username: str = "CaiLogics Usage Analyst",
    icon_emoji: str = ":bar_chart:",
) -> dict[str, Any]:
    """
    Posts a new usage tracking report message in the specified usage channel.
    """
    text = _sanitize_and_truncate_slack_text(text)

    if not SLACK_BOT_TOKEN or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token":
        mock_ts = f"mock_usage_{int(time.time() * 1000)}.000"
        logger.info(
            f"[MOCK USAGE] Posted mock usage message in channel {channel_id}: {mock_ts}"
        )
        return {"ok": True, "ts": mock_ts, "mock": True}

    try:
        response = await slack_async_client.chat_postMessage(
            channel=channel_id,
            text=text,
            username=username,
            icon_emoji=icon_emoji,
        )
        msg_ts = response.get("ts")
        logger.info(
            f"[SLACK USAGE] Posted usage message {msg_ts} in channel {channel_id}"
        )
        return {"ok": True, "ts": msg_ts}
    except SlackApiError as e:
        logger.error(
            f"[SLACK USAGE] Error posting usage message: {e.response.get('error', e)}"
        )
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.error(f"[SLACK USAGE] Unexpected error posting usage message: {e}")
        return {"ok": False, "error": str(e)}


async def update_usage_message(
    channel_id: str,
    message_ts: str,
    text: str,
) -> dict[str, Any]:
    """
    Updates an existing usage tracking report message in-place via chat.update.
    """
    text = _sanitize_and_truncate_slack_text(text)

    if (
        not SLACK_BOT_TOKEN
        or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token"
        or (message_ts and str(message_ts).startswith("mock_"))
    ):
        logger.info(
            f"[MOCK USAGE] Updated mock usage message {message_ts} ({len(text)} chars)"
        )
        return {"ok": True, "ts": message_ts, "mock": True}

    try:
        await slack_async_client.chat_update(
            channel=channel_id,
            ts=message_ts,
            text=text,
        )
        logger.info(f"[SLACK USAGE] Successfully updated usage message {message_ts}")
        return {"ok": True, "ts": message_ts}
    except SlackApiError as e:
        logger.error(
            f"[SLACK USAGE] Error updating usage message {message_ts}: {e.response.get('error', e)}"
        )
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.error(
            f"[SLACK USAGE] Unexpected error updating usage message {message_ts}: {e}"
        )
        return {"ok": False, "error": str(e)}


async def fetch_thread_files(channel_id: str, thread_ts: str) -> list[dict[str, Any]]:
    """
    Fetches all file attachment metadata uploaded in a Slack thread.
    Returns a list of file dicts with keys: id, name, title, mimetype, filetype, size, url_private_download, url_private.
    """
    if not SLACK_BOT_TOKEN or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token":
        return []

    try:
        response = await slack_async_client.conversations_replies(
            channel=channel_id, ts=thread_ts, limit=100
        )
        messages = response.get("messages", [])
        thread_files: list[dict[str, Any]] = []
        for msg in messages:
            files = msg.get("files", [])
            for f in files:
                thread_files.append(
                    {
                        "id": f.get("id"),
                        "name": f.get("name", "unknown_file"),
                        "title": f.get("title", ""),
                        "mimetype": f.get("mimetype", ""),
                        "filetype": f.get("filetype", ""),
                        "size": f.get("size", 0),
                        "url_private_download": f.get("url_private_download")
                        or f.get("url_private", ""),
                        "url_private": f.get("url_private", ""),
                        "user": f.get("user") or msg.get("user"),
                        "ts": msg.get("ts"),
                    }
                )
        logger.info(
            f"[SLACK FILES] Found {len(thread_files)} files in thread {thread_ts}"
        )
        return thread_files
    except SlackApiError as e:
        logger.error(
            f"[SLACK FILES] Error fetching thread files: {e.response.get('error', e)}"
        )
        return []
    except Exception as e:
        logger.error(f"[SLACK FILES] Unexpected error fetching thread files: {e}")
        return []


async def download_slack_file(
    file_info: dict[str, Any], max_chars: int = 50000
) -> dict[str, Any]:
    """
    Downloads a private Slack file attachment using SLACK_BOT_TOKEN and extracts its text content.
    Returns dict: {"ok": bool, "file_name": str, "content": str, "truncated": bool, "size": int, "error": str | None}
    """
    import httpx

    file_name = file_info.get("name", "unknown_file")
    url = file_info.get("url_private_download") or file_info.get("url_private")
    file_size = file_info.get("size", 0)

    if not url:
        return {
            "ok": False,
            "file_name": file_name,
            "content": "",
            "truncated": False,
            "size": file_size,
            "error": "No download URL available for this file.",
        }

    if not SLACK_BOT_TOKEN or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token":
        return {
            "ok": True,
            "file_name": file_name,
            "content": f"[Mock file content for {file_name}]",
            "truncated": False,
            "size": file_size,
            "error": None,
        }

    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, headers=headers, follow_redirects=True)
            if resp.status_code != 200:
                return {
                    "ok": False,
                    "file_name": file_name,
                    "content": "",
                    "truncated": False,
                    "size": file_size,
                    "error": f"Slack file download failed with HTTP status {resp.status_code}",
                }

            raw_bytes = resp.content
            if file_name.lower().endswith(".pdf"):
                try:
                    import io
                    import pypdf

                    pdf_stream = io.BytesIO(raw_bytes)
                    reader = pypdf.PdfReader(pdf_stream)
                    pages_text = []
                    for idx, page in enumerate(reader.pages):
                        page_text = (page.extract_text() or "").strip()
                        if page_text:
                            pages_text.append(f"--- Page {idx + 1} ---\n{page_text}")
                        else:
                            pages_text.append(
                                f"--- Page {idx + 1} ---\n[No extractable text]"
                            )
                    text_content = (
                        "\n\n".join(pages_text)
                        if pages_text
                        else "[Empty PDF document]"
                    )
                except Exception as pdf_err:
                    logger.warning(
                        f"[SLACK FILE] Could not parse PDF attachment {file_name}: {pdf_err}"
                    )
                    text_content = f"[Failed to extract text from PDF: {pdf_err}]"
            else:
                text_content = raw_bytes.decode("utf-8", errors="replace")
            truncated = False
            if len(text_content) > max_chars:
                text_content = (
                    text_content[:max_chars]
                    + f"\n\n... [TRUNCATED: File exceeds {max_chars} characters] ..."
                )
                truncated = True

            try:
                downloads_dir = MCP_WORKSPACE_DIR / "downloads"
                downloads_dir.mkdir(parents=True, exist_ok=True)
                file_path = downloads_dir / file_name
                file_path.write_bytes(raw_bytes)
                logger.info(f"[SLACK FILE] Cached Slack file to workspace: {file_path}")
            except (PermissionError, FileNotFoundError, OSError) as save_err:
                logger.warning(
                    f"[SLACK FILE] Could not save local copy to workspace: {save_err}"
                )
            except Exception as save_err:
                logger.warning(
                    f"[SLACK FILE] Could not save local copy to workspace: {save_err}"
                )

            return {
                "ok": True,
                "file_name": file_name,
                "content": text_content,
                "truncated": truncated,
                "size": len(raw_bytes),
                "error": None,
            }
    except (httpx.TimeoutException, httpx.RequestError) as e:
        logger.error(
            f"[SLACK FILE] Network error downloading Slack file {file_name}: {e}"
        )
        return {
            "ok": False,
            "file_name": file_name,
            "content": "",
            "truncated": False,
            "size": file_size,
            "error": str(e),
        }
    except Exception as e:
        logger.error(f"[SLACK FILE] Error downloading Slack file {file_name}: {e}")
        return {
            "ok": False,
            "file_name": file_name,
            "content": "",
            "truncated": False,
            "size": file_size,
            "error": str(e),
        }


def extract_thread_files_metadata(
    files_list: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "id": f.get("id"),
            "name": f.get("name"),
            "size": f.get("size"),
            "filetype": f.get("filetype"),
        }
        for f in files_list
    ]


async def get_bot_user_id() -> str | None:
    if not SLACK_BOT_TOKEN or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token":
        return "U_MOCK_BOT"
    try:
        auth_info = await slack_async_client.auth_test()
        return auth_info.get("user_id")
    except SlackApiError as e:
        logger.error(f"Slack API error fetching bot user ID: {e}")
        return None
    except Exception as e:
        logger.error(f"Error fetching bot user ID: {e}")
        return None


async def open_dm_channel(user_id: str) -> str | None:
    if not SLACK_BOT_TOKEN or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token":
        return f"D_MOCK_{user_id}"
    try:
        res = await slack_async_client.conversations_open(users=user_id)
        ch = res.get("channel", {})
        return ch.get("id")
    except SlackApiError as e:
        logger.error(f"Slack API error opening DM channel with {user_id}: {e}")
        return None
    except Exception as e:
        logger.error(f"Error opening DM channel with {user_id}: {e}")
        return None


async def post_ephemeral_to_slack(
    channel_id: str,
    user_id: str,
    text: str,
    thread_ts: str | None = None,
) -> dict[str, Any]:
    if not SLACK_BOT_TOKEN or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token":
        return {"ok": True, "mock": True}
    try:
        kwargs = {
            "channel": channel_id,
            "user": user_id,
            "text": text,
        }
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        res = await slack_async_client.chat_postEphemeral(**kwargs)
        return res.data
    except SlackApiError as e:
        logger.error(f"Slack API error posting ephemeral message: {e}")
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.error(f"Error posting ephemeral message: {e}")
        return {"ok": False, "error": str(e)}


async def post_message_to_slack(
    channel_id: str,
    text: str,
    thread_ts: str | None = None,
    username: str | None = None,
    icon_url: str | None = None,
    icon_emoji: str | None = None,
) -> dict[str, Any]:
    if not SLACK_BOT_TOKEN or SLACK_BOT_TOKEN == "xoxb-your-bot-user-oauth-token":
        return {"ok": True, "mock": True}
    try:
        kwargs = {"channel": channel_id, "text": text}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        if username:
            kwargs["username"] = username
        if icon_url:
            kwargs["icon_url"] = icon_url
        if icon_emoji:
            kwargs["icon_emoji"] = icon_emoji
        res = await slack_async_client.chat_postMessage(**kwargs)
        return res.data
    except SlackApiError as e:
        logger.error(f"Slack API error posting message to Slack: {e}")
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.error(f"Error posting message to Slack: {e}")
        return {"ok": False, "error": str(e)}


async def update_slack_message(
    channel_id: str,
    ts: str,
    text: str,
) -> dict[str, Any]:
    return await update_thread_note(channel_id=channel_id, message_ts=ts, text=text)


async def read_slack_file_text(
    file_info: dict[str, Any], max_chars: int = 50000
) -> str:
    res = await download_slack_file(file_info, max_chars=max_chars)
    return res.get("content", "")
