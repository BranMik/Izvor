"""
Stage 3: Context preparation, history slicing, word limit injection, and prompt construction.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from core.config import AGENTS_CONFIG
from nlp.message_builder import build_llm_messages
from orchestration.agent_settings import (
    get_orchestration_mode,
    get_sandwich_config,
    settings_manager,
)
from orchestration.nomination import get_initial_agents
from orchestration.thread_state import thread_state_manager
from pipeline.orchestrator import SandwichOrchestrator
from prompts import build_agent_prompt
from services.slack_client import fetch_thread_history

if TYPE_CHECKING:
    from orchestration.turn_pipeline import TurnContext

logger = logging.getLogger(__name__)


class ContextPrepStage:
    def __init__(self, pipeline: Any):
        self.pipeline = pipeline
    """Provides stage 3 context preparation and prompt construction for TurnPipeline."""



    async def stage_prepare_context(
        self, ctx: TurnContext
    ) -> dict[str, Any] | None:
        """Stage 3: Fetch thread history, apply history/word limits, and construct LLM prompts."""
        if ctx.in_memory_history is not None:
            history = list(ctx.in_memory_history)
            logger.info(
                f"[ROUTER] Using in-memory history with {len(history)} messages."
            )
        else:
            fetch_hist_fn = self.pipeline._get_hook(
                "fetch_thread_history", fetch_thread_history
            )
            history = await fetch_hist_fn(
                ctx.channel_id, ctx.thread_ts, bot_user_id=ctx.bot_user_id
            )
            logger.info(
                f"[ROUTER] Fetched {len(history)} messages from thread history."
            )

        if ctx.is_superseded():
            logger.info(
                f"[ROUTER] Thread {ctx.thread_key} turn was superseded. Aborting."
            )
            return {"ok": False, "cancelled": True, "reason": "superseded"}

        if not ctx.is_bot_trigger and (not history or history[-1].get("role") != "user"):
            history = list(history) + [{"role": "user", "content": ctx.raw_text}]

        ctx.full_history = list(history)

        if not ctx.is_bot_trigger:
            if history and history[-1]["role"] == "user":
                prior_messages = history[:-1]
            else:
                prior_messages = history

            if ctx.history_limit is not None:
                thread_state_manager.set_history_limit(
                    channel_id=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    limit=ctx.history_limit,
                    prior_cutoff_index=len(prior_messages),
                )
                effective_prior = (
                    prior_messages[-ctx.history_limit :]
                    if ctx.history_limit > 0
                    else []
                )
                logger.info(
                    f"[ROUTER] Applying /history {ctx.history_limit}: kept {len(effective_prior)} of {len(prior_messages)} prior messages for user prompt."
                )
            else:
                thread_state_manager.clear_history_limit(ctx.channel_id, ctx.thread_ts)
                effective_prior = prior_messages

            ctx.effective_history = effective_prior
        else:
            hist_limit_info = thread_state_manager.get_history_limit(
                ctx.channel_id, ctx.thread_ts
            )
            if hist_limit_info is not None:
                limit = hist_limit_info["limit"]
                cutoff = hist_limit_info["prior_cutoff_index"]
                prior_part = history[:cutoff]
                discussion_part = history[cutoff:]
                effective_prior = prior_part[-limit:] if limit > 0 else []
                ctx.effective_history = effective_prior + discussion_part
                logger.info(
                    f"[ROUTER] Applying active history limit ({limit}): kept {len(effective_prior)}/{len(prior_part)} prior messages + {len(discussion_part)} discussion messages."
                )
            else:
                ctx.effective_history = history

        # System prompt generation & Thread-Global & Agent Skills
        from orchestration.thread_skills_manager import thread_skills_manager
        from core.registry import get_skill_registry
        skill_registry = get_skill_registry()
        global_skills = skill_registry.get_global_skills()
        active_thread_skills = thread_skills_manager.get_active_skills(ctx.channel_id, ctx.thread_ts)
        active_agent_skills = thread_skills_manager.get_active_agent_skills(ctx.channel_id, ctx.thread_ts, ctx.agent_key)
        extra_skills = list(dict.fromkeys(global_skills + active_thread_skills + active_agent_skills))
        effective_skills = None
        if extra_skills:
            base_skills = ctx.agent_cfg.get("skills")
            if base_skills is None:
                base_skills = settings_manager.get_agent_skills(ctx.agent_key)
            effective_skills = list(dict.fromkeys((base_skills or []) + extra_skills))
            ctx.agent_cfg = dict(ctx.agent_cfg, skills=effective_skills)

        active_max_words = thread_state_manager.get_max_words(ctx.channel_id, ctx.thread_ts)
        active_min_words = thread_state_manager.get_min_words(ctx.channel_id, ctx.thread_ts)
        if active_max_words is not None or active_min_words is not None or ctx.modifiers or extra_skills:
            logger.info(
                f"[ROUTER] Applying active prompt modifiers (min_words={active_min_words}, max_words={active_max_words}, modifiers={ctx.modifiers}, extra_skills={extra_skills}) for agent {ctx.agent_key}."
            )
            ctx.system_prompt = build_agent_prompt(
                ctx.agent_key,
                max_words=active_max_words,
                min_words=active_min_words,
                skills=effective_skills,
                modifiers=ctx.modifiers,
            )
        else:
            ctx.system_prompt = ctx.agent_cfg["system_prompt"]

        ctx.remaining = thread_state_manager.get_remaining_agents(
            ctx.channel_id, ctx.thread_ts, default=get_initial_agents()
        )
        ctx.is_custom_flow = thread_state_manager.is_custom_flow(
            ctx.channel_id, ctx.thread_ts
        )

        active_rounds = thread_state_manager.get_rounds(ctx.channel_id, ctx.thread_ts)
        ctx.force_termination = bool(
            (
                (ctx.was_explicit_target and not ctx.is_bot_trigger and not ctx.remaining)
                or (ctx.is_custom_flow and not ctx.remaining)
            )
            and active_rounds <= 1
            or thread_state_manager.is_stopped(ctx.channel_id, ctx.thread_ts)
        )

        if ctx.force_termination:
            logger.info(
                f"[ROUTER] Agent {ctx.agent_key} forcing termination (no remaining agents in sequence or stop requested)."
            )
        else:
            logger.info(
                f"[ROUTER] Agent {ctx.agent_key} remaining available agents: {ctx.remaining} (active rounds: {active_rounds})"
            )

        mode = get_orchestration_mode()
        ctx.next_designated_agent = None
        ctx.eligible_remaining_agents = list(ctx.remaining)
        if not ctx.force_termination and ctx.remaining:
            orchestrator = SandwichOrchestrator(
                mode=mode,
                start_chain=get_sandwich_config().get("start_chain", []),
                end_chain=get_sandwich_config().get("end_chain", []),
                fixed_chain=settings_manager.get_fixed_config().get(
                    "chain", []
                ),
                agents_map=settings_manager.agents_config or AGENTS_CONFIG,
            )
            (
                ctx.next_designated_agent,
                ctx.eligible_remaining_agents,
            ) = orchestrator.get_phase_info(
                ctx.remaining, is_custom_flow=ctx.is_custom_flow
            )
        elif not ctx.force_termination and not ctx.remaining and active_rounds > 1:
            round_seq = thread_state_manager.get_round_sequence(
                ctx.channel_id, ctx.thread_ts
            ) or get_initial_agents()
            if round_seq:
                ctx.next_designated_agent = round_seq[0]
                ctx.eligible_remaining_agents = [round_seq[0]]

        ctx.file_context = thread_state_manager.get_file_context(
            ctx.channel_id, ctx.thread_ts
        )
        if not getattr(ctx, "thread_language", None):
            ctx.thread_language = (
                thread_state_manager.get_language(ctx.channel_id, ctx.thread_ts)
                or "English"
            )

        # Determine any required or inferred skills for this turn
        required_skills: list[str] = []
        if getattr(ctx, "actions", None):
            known_agent_skills = {s.name for s in skill_registry.get_agent_skills()}
            for act in ctx.actions:
                if act.name in known_agent_skills and act.name not in required_skills:
                    required_skills.append(act.name)

        if getattr(ctx, "is_file_req", False):
            file_skill = skill_registry.get_file_handler_skill()
            if file_skill and file_skill.name not in required_skills:
                required_skills.append(file_skill.name)

        if ctx.file_context:
            req_s = ctx.file_context.get("required_skill")
            if req_s and req_s not in required_skills:
                required_skills.append(req_s)

        ctx.messages = build_llm_messages(
            system_prompt=ctx.system_prompt,
            effective_history=ctx.effective_history,
            prompt=ctx.prompt,
            is_bot_trigger=ctx.is_bot_trigger,
            agent_key=ctx.agent_key,
            agent_cfg=ctx.agent_cfg,
            file_context=ctx.file_context,
            remaining_agents=ctx.eligible_remaining_agents,
            force_termination=ctx.force_termination,
            next_designated_agent=ctx.next_designated_agent,
            thread_language=ctx.thread_language,
            required_skills=required_skills,
        )
        return None
