"""
Stage 2: Agent configuration lookup, fallback rotation, and circuit breaker spend governance.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import TYPE_CHECKING, Any

from agents import (
    calculate_thread_costs,
    trigger_usage_update,
)
from core.config import AGENTS_CONFIG
from orchestration.system_settings import get_slack_post_pause_seconds
from core.models import OrchestrationMode
from orchestration.agent_settings import settings_manager
from orchestration.app_settings import (
    get_circuit_breaker_threshold,
    is_circuit_breaker_enabled,
)
from orchestration.protocols import PipelineDeps, WorkspaceAdapter
from orchestration.thread_state import thread_state_manager
from services.slack import (
    post_system_message,
)

if TYPE_CHECKING:
    from orchestration.turn_pipeline import TurnContext

logger = logging.getLogger(__name__)


class ResolutionStage:
    """Provides stage 2 agent resolution and spend governance for TurnPipeline."""

    def __init__(self, deps: PipelineDeps):
        self.deps = deps

    @property
    def spend_governance(self) -> Any:
        sg = getattr(self.deps, "spend_governance", None)
        if sg is None:
            from services.spend_governance import spend_governance as default_sg

            sg = default_sg
        return sg

    async def _sleep(self, seconds: float) -> None:
        sleep_fn = getattr(self.deps, "sleep_fn", None)
        if sleep_fn is not None:
            await sleep_fn(seconds)
        else:
            await asyncio.sleep(seconds)

    @property
    def workspace_adapter(self) -> WorkspaceAdapter:
        if hasattr(self.deps, "workspace_adapter") and self.deps.workspace_adapter is not None:
            return self.deps.workspace_adapter
        from adapters.slack.workspace_adapter import SlackWorkspaceAdapter

        return SlackWorkspaceAdapter()

    async def stage_resolve_agent_and_governance(
        self, ctx: TurnContext
    ) -> dict[str, Any] | None:
        """Stage 2: Agent config lookup, fallback rotation, and circuit breaker verification."""
        post_sys_msg = getattr(self.deps, "post_system_message_fn", None) or post_system_message

        agents_map = settings_manager.agents_config or AGENTS_CONFIG

        if ctx.agent_key not in agents_map:
            if ctx.agent_key and ctx.agent_key in settings_manager.system_agents_config:
                ctx.agent_cfg = settings_manager.system_agents_config[ctx.agent_key]
            else:
                logger.error(
                    f"[ROUTER] Agent '{ctx.agent_key}' not found in configuration."
                )
            next_candidate = None
            while True:
                candidate = thread_state_manager.pop_remaining_agent(
                    ctx.input.channel_id, ctx.input.thread_ts
                )
                if not candidate:
                    break
                if candidate in agents_map and candidate != ctx.agent_key:
                    next_candidate = candidate
                    break

            if not next_candidate:
                wf_snap = thread_state_manager.get_workflow_snapshot(ctx.input.channel_id, ctx.input.thread_ts)
                mode = wf_snap.get("mode") if wf_snap else settings_manager.get_orchestration_mode()
                if mode in (OrchestrationMode.SANDWICH, OrchestrationMode.SANDWICH.value):
                    sw_cfg = wf_snap.get("sandwich_config") if wf_snap else settings_manager.get_sandwich_config()
                    end_chain = [
                        k
                        for k in sw_cfg.get(
                            "end_chain", []
                        )
                        if k in agents_map and k != ctx.agent_key
                    ]
                    if end_chain:
                        next_candidate = end_chain[0]
                if not next_candidate:
                    available = [
                        k
                        for k in settings_manager.get_enabled_agents()
                        if k in agents_map and k != ctx.agent_key
                    ]
                    if available:
                        next_candidate = random.choice(available)

            if next_candidate:
                warning_msg = (
                    f"⚠️ *Configuration Error*: Agent '{ctx.agent_key}' is not configured. "
                    f"Handing off to [{next_candidate}]."
                )
                await post_sys_msg(
                    channel_id=ctx.input.channel_id,
                    thread_ts=ctx.input.thread_ts,
                    text=warning_msg,
                )
                ctx.agent_key = next_candidate
                ctx.agent_cfg = agents_map[ctx.agent_key]
            else:
                warning_msg = (
                    f"⚠️ *Configuration Error*: Agent '{ctx.agent_key}' is not configured and no other agents are available."
                )
                await post_sys_msg(
                    channel_id=ctx.input.channel_id,
                    thread_ts=ctx.input.thread_ts,
                    text=warning_msg,
                )
                return {
                    "ok": False,
                    "halted": True,
                    "reason": "agent_not_configured",
                }
        else:
            ctx.agent_cfg = agents_map[ctx.agent_key]

        # Ensure agent's configured avatar icon is propagated to turn context
        if not ctx.icon_url and ctx.agent_cfg.get("icon_url"):
            ctx.icon_url = ctx.agent_cfg.get("icon_url")

        logger.info(
            f"[ROUTER] Parsed Agent: {ctx.agent_key} ({ctx.agent_cfg['model']}) | Explicit target: {ctx.was_explicit_target} | Bot trigger: {ctx.input.is_bot_trigger} | Gen: {ctx.my_generation} | History limit: {ctx.history_limit}"
        )

        if ctx.is_superseded():
            logger.info(
                f"[ROUTER] Thread {ctx.thread_key} turn was superseded. Aborting."
            )
            return {"ok": False, "cancelled": True, "reason": "superseded"}

        # Circuit breaker & daily budget verification
        calc_costs_fn = getattr(self.deps, "calc_costs_fn", None) or calculate_thread_costs
        cost_info = calc_costs_fn(ctx.input.channel_id, ctx.input.thread_ts)
        current_cost = cost_info.get("grand_total_cost_usd", 0.0)
        threshold = get_circuit_breaker_threshold()
        session_id = f"{ctx.input.channel_id}:{ctx.input.thread_ts}"
        from orchestration.app_settings import get_daily_budget_usd

        daily_budget = get_daily_budget_usd()
        tripped, current_cost, threshold = (
            self.spend_governance.is_budget_exceeded(
                session_id=session_id,
                threshold_usd=threshold,
                enabled=is_circuit_breaker_enabled(),
                current_cost_usd=current_cost,
                daily_budget_usd=daily_budget,
            )
        )
        if tripped:
            logger.warning(
                f"[CIRCUIT BREAKER] Thread {ctx.thread_key} or daily budget reached/exceeded threshold (${threshold:.5f}). Interrupting response."
            )
            from services.audit_logger import log_audit_event

            log_audit_event(
                event_type="CIRCUIT_BREAKER_TRIGGERED",
                actor={"type": "system", "id": "spend_governance"},
                action="trip_circuit_breaker",
                target=ctx.thread_key,
                details={
                    "channel_id": ctx.input.channel_id,
                    "thread_ts": ctx.input.thread_ts,
                    "current_cost_usd": current_cost,
                    "threshold_usd": threshold,
                },
                severity="CRITICAL",
                status="SUCCESS",
            )
            circuit_breaker_msg = (
                "EXPENSE LIMIT REACHED FOR THE THREAD. ENDING CALLS"
            )

            session_id = getattr(ctx, "session_id", f"{ctx.input.channel_id}:{ctx.input.thread_ts}")
            try:
                import uuid
                from core.domain import TurnCheckpoint
                from services.turn_checkpoint_store import turn_checkpoint_store

                chk_id = str(uuid.uuid4())
                chk = TurnCheckpoint(
                    checkpoint_id=chk_id,
                    session_id=session_id,
                    channel_id=ctx.input.channel_id,
                    thread_ts=ctx.input.thread_ts,
                    agent_key=ctx.agent_key,
                    pause_reason="circuit_breaker",
                    messages=ctx.messages or [],
                    accumulated_history=ctx.input.in_memory_history or [],
                    prompt_tokens=ctx.output.total_prompt_tokens,
                    completion_tokens=ctx.output.total_completion_tokens,
                )
                turn_checkpoint_store.save_checkpoint(chk)
                ctx.output.checkpoint_id = chk_id
            except Exception as ex:  # noqa: BLE001
                logger.debug(f"[RESOLUTION] Failed to save circuit breaker checkpoint: {ex}")

            blocks = self.workspace_adapter.build_budget_halt_blocks(
                circuit_breaker_msg, session_id
            )
            slack_res = await self.workspace_adapter.post_agent_turn(
                channel_id=ctx.input.channel_id,
                thread_ts=ctx.input.thread_ts,
                text=circuit_breaker_msg,
                username=ctx.agent_cfg["username"],
                icon_url=ctx.agent_cfg.get("icon_url"),
                blocks=blocks,
            )
            pause_secs = getattr(self.deps, "slack_post_pause_seconds", None)
            if pause_secs is None:
                pause_secs = get_slack_post_pause_seconds()
            await self._sleep(pause_secs)

            # Note: trigger_thread_notes_update is intentionally suppressed here
            # to prevent burning further LLM tokens when the budget is exceeded.

            trig_usage = getattr(self.deps, "usage_updater", None) or trigger_usage_update
            trig_usage(
                channel_id=ctx.input.channel_id,
                thread_ts=ctx.input.thread_ts,
                is_completed=True,
            )

            thread_state_manager.suspend_thread(ctx.input.channel_id, ctx.input.thread_ts)
            if isinstance(slack_res, dict):
                slack_res["is_completed"] = True
                slack_res["final_text"] = circuit_breaker_msg
                slack_res["username"] = ctx.agent_cfg["username"]
                slack_res["agent_key"] = ctx.agent_key
            return {
                "ok": True,
                "circuit_breaker_triggered": True,
                "is_completed": True,
                "slack_res": slack_res,
                "final_text": circuit_breaker_msg,
                "username": ctx.agent_cfg["username"],
                "agent_key": ctx.agent_key,
            }

        return None
