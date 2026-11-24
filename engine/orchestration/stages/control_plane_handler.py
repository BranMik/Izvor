"""Stage 4 control-plane collaborator: skill schema unlocking and control-plane tool state transitions."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from config_store.agent_settings import settings_manager
from config_store.app_settings import is_convergence_fast_forward_enabled
from orchestration.protocols import PipelineDeps
from orchestration.thread_state import thread_state_manager

if TYPE_CHECKING:
    from orchestration.turn_pipeline import TurnContext


def unlock_skill_tools(
    *,
    skill_def: Any,
    ctx: TurnContext,
    active_tools: list[dict[str, Any]],
) -> None:
    """Appends schemas unlocked by load_skill, scoping control_plane nominations to non-self peers."""
    if skill_def.name == "control_plane":
        from skills.builtin.control_plane.tools import build_control_plane_tool_schemas

        eligible_peers: list[str] = []
        seen_keys: set[str] = set()
        raw_candidates = (
            []
            if getattr(ctx, "force_termination", False)
            else list(getattr(ctx, "eligible_remaining_agents", None) or [])
        )
        for peer_key in raw_candidates:
            if not isinstance(peer_key, str) or peer_key == ctx.agent_key or peer_key in seen_keys:
                continue
            seen_keys.add(peer_key)
            agent_cfg = settings_manager.get_agent(peer_key)
            label = agent_cfg.get("username", peer_key) if isinstance(agent_cfg, dict) else peer_key
            eligible_peers.append(label)

        candidate_schemas = build_control_plane_tool_schemas(
            eligible_agents=eligible_peers or None,
        )
        if not eligible_peers:
            candidate_schemas = [
                s for s in candidate_schemas if s.get("function", {}).get("name") != "nominate_next_speaker"
            ]
    else:
        candidate_schemas = skill_def.get_tool_schemas()

    existing_names = {
        s.get("function", {}).get("name")
        for s in active_tools
        if isinstance(s, dict) and "function" in s
    }
    for s_schema in candidate_schemas:
        s_name = s_schema.get("function", {}).get("name")
        if s_name == "signal_consensus_convergence" and not is_convergence_fast_forward_enabled():
            continue
        if s_name:
            if s_name not in existing_names:
                active_tools.append(s_schema)
                existing_names.add(s_name)
        elif s_schema not in active_tools:
            active_tools.append(s_schema)


class ControlPlaneHandler:
    """Applies control-plane tool calls (nomination, clarification, convergence) and skill schema unlocking."""

    def __init__(self, deps: PipelineDeps) -> None:
        self.deps = deps

    def unlock_tools(
        self,
        *,
        skill_def: Any,
        ctx: TurnContext,
        active_tools: list[dict[str, Any]],
    ) -> None:
        """Unlocks schemas declared by a loaded skill into active_tools."""
        unlock_skill_tools(skill_def=skill_def, ctx=ctx, active_tools=active_tools)

    def apply_tool_effect(
        self,
        *,
        fn_name: str,
        fn_args: dict[str, Any],
        ctx: TurnContext,
    ) -> str | None:
        """
        Mutates TurnContext control-plane state after a non-errored control-plane tool invocation.
        Returns a structured JSON error string if runtime validation rejects the tool arguments, or None.
        """
        if fn_name == "nominate_next_speaker":
            from pipeline.entity_resolver import entity_resolver

            nom_agent = fn_args.get("agent_name")
            nom_str = str(nom_agent).strip() if nom_agent else ""
            if not nom_str:
                return json.dumps({
                    "isError": True,
                    "status": "rejected",
                    "error": "agent_name must be non-empty.",
                })
            resolved_nom = entity_resolver.resolve_agent_name(nom_str) or nom_str
            if resolved_nom == ctx.agent_key:
                return None
            eligible = list(getattr(ctx, "eligible_remaining_agents", None) or [])
            if eligible:
                eligible_canon = {entity_resolver.resolve_agent_name(a) or a for a in eligible}
                if resolved_nom not in eligible_canon:
                    return json.dumps({
                        "isError": True,
                        "status": "rejected",
                        "error": f"Agent '{nom_str}' is not an eligible remaining speaker.",
                    })
            ctx.control.nomination = nom_str
            ctx.control.nomination_rationale = str(fn_args.get("rationale", "")).strip()
        elif fn_name == "request_user_clarification":
            question = str(fn_args.get("question", "")).strip()
            if not question:
                return json.dumps({
                    "isError": True,
                    "status": "rejected",
                    "error": "Clarification question must be non-empty.",
                })
            ctx.control.clarification_requested = True
            ctx.control.clarification_question = question
            ctx.output.halted = True
        elif fn_name == "signal_consensus_convergence":
            summary = str(fn_args.get("summary_of_agreement", "")).strip()
            if not summary:
                return json.dumps({
                    "isError": True,
                    "status": "rejected",
                    "error": "summary_of_agreement must be non-empty.",
                })
            ctx.control.consensus_signaled = True
            ctx.control.consensus_summary = summary
            thread_state_manager.set_consensus_reached(ctx.input.channel_id, ctx.input.thread_ts, True)
        return None

