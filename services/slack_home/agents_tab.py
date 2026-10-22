import logging
from typing import Any

from core.model_catalog import get_available_models
logger = logging.getLogger(__name__)


def _get_example_agent_id(agents_cfg: dict[str, Any] | None = None) -> str:
    """Returns an example agent ID from configured agents, or 'agent-id' as fallback."""
    if agents_cfg is None:
        from orchestration.agent_settings import get_agents_config
        cfg = get_agents_config()
    else:
        cfg = agents_cfg
    if cfg:
        return next(iter(cfg.keys()))
    return "agent-id"


def _get_example_agent_names(agents_cfg: dict[str, Any] | None = None, count: int = 2) -> str:
    """Returns comma-separated example agent displaynames from configured agents."""
    if agents_cfg is None:
        from orchestration.agent_settings import get_agents_config
        cfg = get_agents_config()
    else:
        cfg = agents_cfg
    names: list[str] = []
    for k, v in (cfg or {}).items():
        if isinstance(v, dict):
            disp = v.get("displayname") or v.get("username")
            if disp and disp not in names:
                names.append(disp)
        if len(names) >= count:
            break
    if not names:
        return "Agent1, Agent2" if count > 1 else "Agent1"
    return ", ".join(names)


def build_panel_status_block(active_count: int, total_agents: int, cb_enabled: bool, cb_threshold: float) -> dict[str, Any]:
    """Builds the panel status summary block."""
    return {
        "type": "section",
        "block_id": f"cb_panel_status_section_{'on' if cb_enabled else 'off'}_{cb_threshold:.2f}",
        "fields": [
            {
                "type": "mrkdwn",
                "text": f"*📊 Panel Status:*\n🟢 *{active_count}* / *{total_agents}* agents active",
            },
        ],
    }


def build_bulk_actions_blocks(active_count: int, total_agents: int) -> list[dict[str, Any]]:
    """Builds the bulk enable/disable actions block."""
    return [
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


def build_agent_roster_blocks(
    agents_cfg: dict[str, Any],
    enabled_agents: set[str],
    cached_env: dict[str, Any],
    cached_catalog: dict[str, Any],
    current_block_count: int = 0,
) -> list[dict[str, Any]]:
    """Generates Block Kit elements for individual agent toggles and model selection dropdowns."""
    blocks: list[dict[str, Any]] = []
    total_cfg = len(agents_cfg)

    for i, (key, cfg) in enumerate(agents_cfg.items()):
        if current_block_count + len(blocks) >= 92:
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

        option_groups, initial_opt = get_available_models(
            current_model=model,
            env_dict=cached_env,
            catalog=cached_catalog,
        )
        model_block_id = (
            f"agent_model_block_{key}_{model.replace('/', '_').replace('.', '_')}"
        )
        model_section: dict[str, Any] = {
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
        if i < total_cfg - 1 and current_block_count + len(blocks) < 92:
            blocks.append({"type": "divider"})

    return blocks
