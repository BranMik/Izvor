"""
Control Plane tools for explicit multi-agent turn-taking, user clarification, and consensus signaling.
"""

from __future__ import annotations

import json
from typing import Any

def build_control_plane_tool_schemas(
    allow_clarification: bool = True,
    allow_convergence: bool = True,
    eligible_agents: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Builds LiteLLM tool schemas for control-plane tools with optional toggles and enums."""
    nominate_params: dict[str, Any] = {
        "type": "object",
        "properties": {
            "agent_name": {
                "type": "string",
                "description": (
                    "The username, display name, or key of the next agent from the active panel."
                ),
            },
            "rationale": {
                "type": "string",
                "description": "The specific question or rationale directed at the nominated agent.",
            },
        },
        "required": ["agent_name", "rationale"],
    }
    if eligible_agents:
        nominate_params["properties"]["agent_name"]["enum"] = list(eligible_agents)

    schemas: list[dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": "nominate_next_speaker",
                "description": (
                    "Explicitly nominates the next agent to speak in the multi-agent panel, "
                    "specifying their agent key or username and a concrete rationale/question."
                ),
                "parameters": nominate_params,
            },
        }
    ]

    if allow_clarification:
        schemas.append({
            "type": "function",
            "function": {
                "name": "request_user_clarification",
                "description": (
                    "Pauses the autonomous multi-agent discussion chain to request essential clarification, "
                    "context, or decisions directly from the human user in Slack."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "The specific question or clarification request directed to the user.",
                        },
                    },
                    "required": ["question"],
                },
            },
        })

    if allow_convergence:
        schemas.append({
            "type": "function",
            "function": {
                "name": "signal_consensus_convergence",
                "description": (
                    "Signals that the participating agents have reached convergence or consensus on the core issue, "
                    "permitting the orchestrator to advance to conclusion synthesis and scribe brief generation."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "summary_of_agreement": {
                            "type": "string",
                            "description": "Concise summary of the consensus and recommendations agreed upon.",
                        },
                        "dissenting_points": {
                            "type": "string",
                            "description": "Optional notes on minority viewpoints, trade-offs, or open concerns.",
                        },
                    },
                    "required": ["summary_of_agreement"],
                },
            },
        })

    return schemas


TOOL_SCHEMAS: list[dict[str, Any]] = build_control_plane_tool_schemas()



def _resolve_canonical_agent_key(raw_name: str) -> str:
    """Resolves a username, display name, or key to its canonical agent key when available."""
    cleaned = str(raw_name or "").strip()
    if not cleaned:
        return ""
    try:
        from pipeline.entity_resolver import entity_resolver

        resolved = entity_resolver.resolve_agent_name(cleaned)
        if resolved:
            return resolved
    except (ImportError, AttributeError, KeyError, TypeError, ValueError):
        return cleaned
    return cleaned


def handle_nominate_next_speaker(
    args: dict[str, Any],
    agent_name: str = "",
    **kwargs: Any,
) -> str:
    nominated_raw = str(args.get("agent_name", "")).strip()
    rationale = str(args.get("rationale", "")).strip()

    caller_key = _resolve_canonical_agent_key(agent_name)
    target_key = _resolve_canonical_agent_key(nominated_raw)
    if caller_key and target_key and caller_key == target_key:
        return json.dumps({
            "error": (
                f"Self-nomination is not permitted: '{caller_key}' cannot nominate itself "
                f"as the next speaker. Nominate a peer specialist from the remaining panel."
            ),
            "status": "rejected",
            "agent_name": nominated_raw,
        })

    return json.dumps({
        "status": "nominated",
        "agent_name": nominated_raw,
        "rationale": rationale,
    })


def handle_request_user_clarification(args: dict[str, Any], **kwargs: Any) -> str:
    question = str(args.get("question", "")).strip()
    if not question:
        return json.dumps({
            "isError": True,
            "status": "rejected",
            "error": "Clarification question must be non-empty.",
        })
    return json.dumps({
        "status": "clarification_requested",
        "question": question,
    })


def handle_signal_consensus_convergence(args: dict[str, Any], **kwargs: Any) -> str:
    summary = str(args.get("summary_of_agreement", "")).strip()
    dissent = str(args.get("dissenting_points", "")).strip()
    if not summary:
        return json.dumps({
            "isError": True,
            "status": "rejected",
            "error": "summary_of_agreement must be non-empty.",
        })
    return json.dumps({
        "status": "consensus_signaled",
        "summary_of_agreement": summary,
        "dissenting_points": dissent,
    })


TOOL_HANDLERS = {
    "nominate_next_speaker": handle_nominate_next_speaker,
    "request_user_clarification": handle_request_user_clarification,
    "signal_consensus_convergence": handle_signal_consensus_convergence,
}
