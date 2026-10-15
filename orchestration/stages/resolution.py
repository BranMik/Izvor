"""
Stage 2: Agent configuration lookup, fallback rotation, and circuit breaker spend governance.
"""

from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING, Any

from agents import (
    calculate_thread_costs,
    trigger_thread_notes_update,
    trigger_usage_update,
)
from core.config import AGENTS_CONFIG
from orchestration.system_settings import get_slack_post_pause_seconds
from core.models import OrchestrationMode
from orchestration.agent_settings import (
    get_circuit_breaker_threshold,
    is_circuit_breaker_enabled,
    settings_manager,
)
from orchestration.thread_state import thread_state_manager
from pipeline.spend_governance import spend_governance
from services.slack_client import post_agent_response, post_system_message

if TYPE_CHECKING:
    from orchestration.turn_pipeline import TurnContext

logger = logging.getLogger(__name__)


class ResolutionStage:
    def __init__(self, pipeline: Any):
        self.pipeline = pipeline
    """Provides stage 2 agent resolution and spend governance for TurnPipeline."""




    async def stage_resolve_agent_and_governance(
        self, ctx: TurnContext
    ) -> dict[str, Any] | None:
        """Stage 2: Agent config lookup, fallback rotation, and circuit breaker verification."""
        post_sys_msg = self.pipeline._get_hook("post_system_message", post_system_message)
        agents_map = settings_manager.agents_config or AGENTS_CONFIG

        if ctx.agent_key not in agents_map:
            logger.error(
                f"[ROUTER] Agent '{ctx.agent_key}' not found in configuration."
            )
            next_candidate = None
            while True:
                candidate = thread_state_manager.pop_remaining_agent(
                    ctx.channel_id, ctx.thread_ts
                )
                if not candidate:
                    break
                if candidate in agents_map and candidate != ctx.agent_key:
                    next_candidate = candidate
                    break

            if not next_candidate:
                mode = settings_manager.get_orchestration_mode()
                if mode == OrchestrationMode.SANDWICH:
                    end_chain = [
                        k
                        for k in settings_manager.get_sandwich_config().get(
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
                    channel_id=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text=warning_msg,
                )
                ctx.agent_key = next_candidate
                ctx.agent_cfg = agents_map[ctx.agent_key]
            else:
                warning_msg = (
                    f"⚠️ *Configuration Error*: Agent '{ctx.agent_key}' is not configured and no other agents are available."
                )
                await post_sys_msg(
                    channel_id=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
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
            f"[ROUTER] Parsed Agent: {ctx.agent_key} ({ctx.agent_cfg['model']}) | Explicit target: {ctx.was_explicit_target} | Bot trigger: {ctx.is_bot_trigger} | Gen: {ctx.my_generation} | History limit: {ctx.history_limit}"
        )

        if ctx.is_superseded():
            logger.info(
                f"[ROUTER] Thread {ctx.thread_key} turn was superseded. Aborting."
            )
            return {"ok": False, "cancelled": True, "reason": "superseded"}

        # Circuit breaker verification
        if is_circuit_breaker_enabled():
            calc_costs_fn = self.pipeline._get_hook(
                "calculate_thread_costs", calculate_thread_costs
            )
            cost_info = calc_costs_fn(ctx.channel_id, ctx.thread_ts)
            current_cost = cost_info.get("grand_total_cost_usd", 0.0)
            threshold = get_circuit_breaker_threshold()
            session_id = f"{ctx.channel_id}:{ctx.thread_ts}"
            from orchestration.app_settings import get_daily_budget_usd

            daily_budget = get_daily_budget_usd()
            tripped, current_cost, threshold = (
                spend_governance.is_budget_exceeded(
                    session_id=session_id,
                    threshold_usd=threshold,
                    enabled=True,
                    current_cost_usd=current_cost,
                    daily_budget_usd=daily_budget,
                )
            )
            if tripped:
                logger.warning(
                    f"[CIRCUIT BREAKER] Thread {ctx.thread_key} cost (${current_cost:.5f}) reached or exceeded threshold (${threshold:.5f}). Interrupting response."
                )
                from services.audit_logger import log_audit_event

                log_audit_event(
                    event_type="CIRCUIT_BREAKER_TRIGGERED",
                    actor={"type": "system", "id": "spend_governance"},
                    action="trip_circuit_breaker",
                    target=ctx.thread_key,
                    details={
                        "channel_id": ctx.channel_id,
                        "thread_ts": ctx.thread_ts,
                        "current_cost_usd": current_cost,
                        "threshold_usd": threshold,
                    },
                    severity="CRITICAL",
                    status="SUCCESS",
                )
                circuit_breaker_msg = (
                    "EXPENSE LIMIT REACHED FOR THE THREAD. ENDING CALLS"
                )

                post_resp_fn = self.pipeline._get_hook(
                    "post_agent_response", post_agent_response
                )
                blocks = [
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": circuit_breaker_msg}
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Continue Sequence"},
                                "action_id": "resume_sequence_action",
                                "style": "primary"
                            }
                        ]
                    }
                ]
                slack_res = await post_resp_fn(
                    channel_id=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text=circuit_breaker_msg,
                    username=ctx.agent_cfg["username"],
                    icon_url=ctx.agent_cfg.get("icon_url"),
                    blocks=blocks,
                )
                pause_secs = self.pipeline._get_hook(
                    "SLACK_POST_PAUSE_SECONDS", get_slack_post_pause_seconds()
                )
                await self.pipeline._sleep(pause_secs)

                # Note: trigger_thread_notes_update is intentionally suppressed here
                # to prevent burning further LLM tokens when the budget is exceeded.

                trig_usage = self.pipeline._get_hook(
                    "trigger_usage_update", trigger_usage_update
                )
                trig_usage(
                    channel_id=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    is_completed=True,
                )

                thread_state_manager.suspend_thread(ctx.channel_id, ctx.thread_ts)
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
