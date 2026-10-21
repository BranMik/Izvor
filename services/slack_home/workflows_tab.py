import hashlib
import logging
from typing import Any

from core.models import OrchestrationMode, REQUIRED_DEFAULT_WORKFLOW_IDS
from services.slack_home.agents_tab import _get_example_agent_names

logger = logging.getLogger(__name__)


def build_workflows_blocks(
    orch_mode: OrchestrationMode | str,
    active_wf_id: str | None,
    workflows: list[Any],
    sandwich_cfg: dict[str, Any],
    fixed_cfg: dict[str, Any],
    agents_cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    """Builds workflow preset selection, orchestration mode, and chain configuration blocks."""
    blocks: list[dict[str, Any]] = []

    start_chain_str = ", ".join(agents_cfg.get(a, {}).get("username", a) for a in sandwich_cfg.get("start_chain", []))
    end_chain_str = ", ".join(agents_cfg.get(a, {}).get("username", a) for a in sandwich_cfg.get("end_chain", []))
    fixed_chain_str = ", ".join(agents_cfg.get(a, {}).get("username", a) for a in fixed_cfg.get("chain", []))

    fixed_hash = hashlib.md5((", ".join(fixed_cfg.get("chain", []))).encode("utf-8")).hexdigest()[:8]
    start_hash = hashlib.md5((", ".join(sandwich_cfg.get("start_chain", []))).encode("utf-8")).hexdigest()[:8]
    end_hash = hashlib.md5((", ".join(sandwich_cfg.get("end_chain", []))).encode("utf-8")).hexdigest()[:8]

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

    mode_val = orch_mode.value if isinstance(orch_mode, OrchestrationMode) else str(orch_mode)
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
                        f"Current mode: *{'🥪 Sandwich Chain (Start ➔ Middle ➔ End)' if mode_val == OrchestrationMode.SANDWICH.value else ('⚡ Parallel Dialectic (Scatter-Gather)' if mode_val == OrchestrationMode.SCATTER_GATHER.value else '🎯 Fixed Sequence')}*"
                    ),
                },
            }
        ]
    )

    if not (active_wf_id in ("default_review_flow", "default_sandwich", "parallel_dialectic_flow")):
        initial_label = "🥪 Sandwich Flow"
        if mode_val == OrchestrationMode.SCATTER_GATHER.value:
            initial_label = "⚡ Parallel Dialectic"
        elif mode_val == OrchestrationMode.FIXED.value:
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
                "value": f"mode:{mode_val}",
            },
        }

    is_default_wf = active_wf_id in REQUIRED_DEFAULT_WORKFLOW_IDS

    # Mode-specific chain configuration UI
    if mode_val == OrchestrationMode.SANDWICH.value:
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
                            "text": f"e.g. {_get_example_agent_names(agents_cfg, count=2)} (comma-separated)",
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
                            "text": f"e.g. {_get_example_agent_names(agents_cfg, count=1)} (comma-separated)",
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
    elif mode_val == OrchestrationMode.SCATTER_GATHER.value:
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
                            "text": f"e.g. {_get_example_agent_names(agents_cfg, count=2)} (comma-separated)",
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
                            "text": f"e.g. {_get_example_agent_names(agents_cfg, count=1)} (comma-separated)",
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
                            "text": f"e.g. {_get_example_agent_names(agents_cfg, count=3)} (comma-separated)",
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

    if not is_default_wf:
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

    return blocks
