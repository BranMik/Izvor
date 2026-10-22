import logging
from typing import Any

logger = logging.getLogger(__name__)


def build_workspace_context_block(ws_ctx: str, is_admin: bool) -> dict[str, Any]:
    """Constructs the Workspace Baseline Context section block."""
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
    return ws_block


def build_governance_blocks(
    cb_enabled: bool,
    cb_threshold: float,
    cb_status_str: str,
    scribe_on: bool,
    scribe_status_str: str,
    ff_on: bool,
    ff_status_str: str,
    weekly_pricing_on: bool,
    weekly_pricing_status_str: str,
    expiry_hours: float,
) -> list[dict[str, Any]]:
    """Generates Block Kit elements for spend circuit-breaker, scribe, and TTL governance."""
    return [
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


def build_admin_required_modal(user_id: str | None, action_id: str = "") -> dict[str, Any]:
    """
    Constructs an informational Block Kit modal explaining that workspace
    admin privileges are required for the attempted action, displaying
    the user's Slack User ID and instructions for requesting or configuring access.
    """
    uid_str = user_id or "Unknown"
    clean_action = (
        action_id.split(":", 1)[0].replace("_", " ").title()
        if action_id
        else "System Configuration Change"
    )

    return {
        "type": "modal",
        "title": {
            "type": "plain_text",
            "text": "Admin Access Required",
            "emoji": True,
        },
        "close": {
            "type": "plain_text",
            "text": "Got it",
            "emoji": True,
        },
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "🔒 *Permission Denied: Workspace Administrator Privileges Required*\n\n"
                        f"You attempted to perform: *{clean_action}*.\n\n"
                        "To protect deliberation integrity, model routing, and spend limits, this setting can "
                        "only be modified by authorized workspace administrators."
                    ),
                },
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "📋 *How to Request or Grant Access:*\n"
                        f"• Your Slack User ID is: `{uid_str}`\n"
                        "• Please forward this ID to your workspace administrator to request access.\n"
                        "• Administrators can enable access by adding your User ID to `SLACK_ADMIN_USER_IDS` in the server environment (`.env`)."
                    ),
                },
            },
        ],
    }

