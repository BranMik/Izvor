"""
Stage 5: Nomination enforcement, token accounting, Slack dispatch, and centralized error handling.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import re
import time
from typing import TYPE_CHECKING, Any

from agents import (
    record_thread_tokens,
    trigger_thread_notes_update,
    trigger_usage_update,
)
from core.config import AGENTS_CONFIG
from orchestration.system_settings import (
    get_debate_turn_pacing_seconds,
    get_slack_post_pause_seconds,
    get_word_limit_grace_buffer,
)
from core.llm_utils import classify_agent_error
from core.models import OrchestrationMode
from orchestration.agent_settings import settings_manager
from orchestration.nomination import enforce_valid_nomination, get_initial_agents
from orchestration.protocols import PipelineDeps, WorkspaceAdapter
from orchestration.thread_state import thread_state_manager
from pipeline.entity_resolver import (
    entity_resolver,
)
from services.flat_history_recorder import (
    async_record_flat_history,
)
from services.slack import (
    post_agent_response,
)

if TYPE_CHECKING:
    from orchestration.turn_pipeline import TurnContext

logger = logging.getLogger(__name__)


def _extract_resolved_nomination_keys(text: str) -> list[str]:
    """
    Extracts all bracketed nomination tags from text and resolves each tag to its
    canonical agent key (e.g. 'The Visionary' -> 'agent-10003').
    """
    raw_tags = re.findall(r"/\[\s*([^\]\r\n]+?)\s*\]", text)
    resolved: list[str] = []
    for tag in raw_tags:
        canonical = entity_resolver.resolve_agent_name(tag)
        resolved.append(canonical if canonical else tag.strip())
    return resolved


def apply_mechanical_word_limit(text: str, max_words: int, grace_buffer: int | None = None) -> str:
    """
    Mechanically truncates response text to (max_words + grace_buffer) words if it exceeds that threshold.
    Preserves trailing nomination tags (e.g. [agent-10007]) so the debate chain is not disrupted.
    """
    if not text:
        return text
    buffer_val = grace_buffer if grace_buffer is not None else get_word_limit_grace_buffer()
    cutoff = max_words + buffer_val
    # Match any trailing nomination tag at the end: e.g. "... /[agent-10007]" or "... /[agent-10007] next"
    trailing_tag_pattern = re.compile(
        r"(?:\n+|\s+)\s*(/\[\s*[^\]\r\n]+?\s*\](?:\s+next)?\s*[.!?]?)\s*$",
        re.IGNORECASE | re.UNICODE,
    )
    match = trailing_tag_pattern.search(text)
    trailing_tag = ""
    body = text
    if match:
        trailing_tag = match.group(1).strip()
        body = text[: match.start()].rstrip()

    words = body.split()
    if len(words) > cutoff:
        truncated_body = " ".join(words[:cutoff])
        logger.info(
            f"[MECHANICAL CUTOFF] Agent response exceeded {cutoff} words ({len(words)} words). "
            f"Mechanically truncated to {cutoff} words."
        )
        if trailing_tag:
            return f"{truncated_body}\n\n{trailing_tag}"
        return truncated_body
    return text


class PostRecordStage:
    """Provides stage 5 dispatch, token accounting, and error handling for TurnPipeline."""

    def __init__(self, deps: PipelineDeps):
        self.deps = deps

    @property
    def workspace_adapter(self) -> WorkspaceAdapter:
        if hasattr(self.deps, "workspace_adapter") and self.deps.workspace_adapter is not None:
            return self.deps.workspace_adapter
        from adapters.slack.workspace_adapter import SlackWorkspaceAdapter

        return SlackWorkspaceAdapter()

    async def _sleep(self, seconds: float) -> None:
        sleep_fn = getattr(self.deps, "sleep_fn", None)
        if sleep_fn is not None:
            await sleep_fn(seconds)
        else:
            await asyncio.sleep(seconds)

    async def _finalize_turn_bookkeeping(
        self,
        ctx: TurnContext,
        is_completed: bool,
        record_flat_history: bool = True,
    ) -> None:
        """Centralized helper for notes updating, usage updating, and flat history."""
        # 1. Trigger notes update
        trig_notes = getattr(self.deps, "notes_updater", None) or trigger_thread_notes_update
        if trig_notes:
            trig_notes(
                channel_id=ctx.input.channel_id,
                thread_ts=ctx.input.thread_ts,
                is_completed=is_completed,
                bot_user_id=ctx.input.bot_user_id,
            )

        # 3. Trigger usage update
        trig_usage = getattr(self.deps, "usage_updater", None) or trigger_usage_update
        if trig_usage:
            trig_usage(
                channel_id=ctx.input.channel_id,
                thread_ts=ctx.input.thread_ts,
                is_completed=is_completed,
            )

        # 4. Record flat history
        if record_flat_history:
            try:
                initial_prompt = thread_state_manager.get_initial_prompt(
                    ctx.input.channel_id, ctx.input.thread_ts
                )
                if not initial_prompt and not ctx.input.is_bot_trigger:
                    initial_prompt = ctx.input.raw_text

                history_to_record = list(getattr(ctx, "full_history", []))
                if ctx.output.final_text:
                    agent_name = ctx.agent_cfg.get("username", ctx.agent_key) if ctx.agent_cfg else ctx.agent_key
                    history_to_record.append(
                        {
                            "role": "assistant",
                            "content": f"[{agent_name}]: {ctx.output.final_text}",
                        }
                    )

                record_flat_fn = getattr(self.deps, "flat_history_recorder", None) or async_record_flat_history
                if record_flat_fn:
                    if inspect.iscoroutinefunction(record_flat_fn):
                        await record_flat_fn(
                            channel_id=ctx.input.channel_id,
                            thread_ts=ctx.input.thread_ts,
                            initial_prompt=initial_prompt,
                            messages=history_to_record,
                        )
                    else:
                        record_flat_fn(
                            channel_id=ctx.input.channel_id,
                            thread_ts=ctx.input.thread_ts,
                            initial_prompt=initial_prompt,
                            messages=history_to_record,
                        )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[WORKSPACE] Failed to record flat history for {ctx.thread_key}: {e}")




    async def stage_post_and_record(self, ctx: TurnContext) -> dict[str, Any]:
        """Stage 5: Nomination enforcement, token accounting, Slack posting, and notifications."""
        if ctx.is_superseded():
            logger.info(
                f"[ROUTER] Thread {ctx.thread_key} turn was superseded before pacing delay. Aborting."
            )
            return {"ok": False, "cancelled": True, "reason": "superseded"}

        if thread_state_manager.is_stopped(ctx.input.channel_id, ctx.input.thread_ts):
            ctx.force_termination = True
            logger.info(
                f"[ROUTER] Thread {ctx.thread_key} received stop request during execution. Forcing termination."
            )

        retries = thread_state_manager.get_retry_queue(
            ctx.input.channel_id, ctx.input.thread_ts
        )

        # Check if more discussion rounds remain when active round ends
        if (
            not ctx.input.isolated_branch
            and not ctx.remaining
            and not retries
            and not ctx.force_termination
            and not thread_state_manager.is_stopped(ctx.input.channel_id, ctx.input.thread_ts)
        ):
            active_rounds = thread_state_manager.get_rounds(
                ctx.input.channel_id, ctx.input.thread_ts
            )
            if thread_state_manager.is_consensus_reached(ctx.input.channel_id, ctx.input.thread_ts):
                thread_state_manager.clear_rounds(ctx.input.channel_id, ctx.input.thread_ts)
                ctx.force_termination = True
                logger.info(
                    f"[ROUTER] Consensus convergence reached for thread {ctx.thread_key}. Concluding discussion and clearing rounds."
                )
            elif active_rounds > 1:
                thread_state_manager.decrement_rounds(
                    ctx.input.channel_id, ctx.input.thread_ts
                )
                round_seq = thread_state_manager.get_round_sequence(
                    ctx.input.channel_id, ctx.input.thread_ts
                )
                if not round_seq:
                    round_seq = get_initial_agents(ctx.input.channel_id, ctx.input.thread_ts)
                if round_seq:
                    next_speaker = round_seq[0]
                    ctx.next_designated_agent = next_speaker
                    ctx.eligible_remaining_agents = [next_speaker]
                    thread_state_manager.set_remaining_agents(
                        ctx.input.channel_id, ctx.input.thread_ts, list(round_seq)
                    )
                    ctx.force_termination = False
                    logger.info(
                        f"[ROUTER] Round completed for thread {ctx.thread_key}. "
                        f"Continuing to next round ({active_rounds - 1} left) starting with [{next_speaker}]."
                    )
            elif active_rounds == 1:
                thread_state_manager.clear_rounds(
                    ctx.input.channel_id, ctx.input.thread_ts
                )
                logger.info(
                    f"[ROUTER] Final round completed for thread {ctx.thread_key}. Concluding discussion."
                )
        if not ctx.output.final_text:
            if ctx.force_termination:
                ctx.output.final_text = "(No text response generated)"
            else:
                if ctx.next_designated_agent:
                    next_tag = ctx.next_designated_agent
                    ctx.output.final_text = (
                        f"(No text response generated) /[{next_tag}] next"
                    )
                elif ctx.eligible_remaining_agents:
                    next_tag = ctx.eligible_remaining_agents[0]
                    ctx.output.final_text = (
                        f"(No text response generated) /[{next_tag}] next"
                    )
                elif retries:
                    next_tag = retries[0]
                    ctx.output.final_text = (
                        f"(No text response generated) /[{next_tag}] next"
                    )
                else:
                    ctx.output.final_text = "(No text response generated)"
                    ctx.force_termination = True
            logger.warning(f"[ROUTER] final_text was empty for {ctx.agent_key}!")
        else:
            # Mechanical word limit (max_words + 50 words)
            active_max_words = None
            raw_max = ctx.modifiers.get("max_words") if isinstance(ctx.modifiers, dict) else None
            if raw_max is None:
                raw_max = thread_state_manager.get_max_words(ctx.input.channel_id, ctx.input.thread_ts)
            if raw_max is not None and str(raw_max).strip().lower() != "off":
                try:
                    p_max = int(raw_max)
                    if p_max > 0:
                        active_max_words = p_max
                except (ValueError, TypeError):
                    active_max_words = None

            if active_max_words is not None:
                ctx.output.final_text = apply_mechanical_word_limit(ctx.output.final_text, active_max_words)

            formatted = self.workspace_adapter.format_outbound_text(ctx.output.final_text)
            if isinstance(formatted, str):
                ctx.output.final_text = formatted
            from orchestration.agent_settings import get_arbitrator_agent, get_sandwich_config

            arbitrator_k = get_arbitrator_agent()
            if ctx.agent_key == arbitrator_k and (
                getattr(ctx.control, "consensus_signaled", False)
                or getattr(ctx, "convergence_signaled", False)
            ):
                wf_snap = thread_state_manager.get_workflow_snapshot(ctx.input.channel_id, ctx.input.thread_ts)
                sandwich_cfg = wf_snap.get("sandwich_config") if wf_snap else get_sandwich_config()
                end_chain = sandwich_cfg.get("end_chain", [])
                if end_chain:
                    ctx.next_designated_agent = end_chain[0]
                    ctx.eligible_remaining_agents = list(end_chain)

            if not getattr(ctx.output, "waiting_hitl", False) and not getattr(ctx.control, "clarification_requested", False):
                ctx.output.final_text = enforce_valid_nomination(
                    ctx.output.final_text,
                    ctx.agent_key,
                    []
                    if ctx.force_termination
                    else ctx.eligible_remaining_agents,
                    ctx.force_termination,
                    pending_retries=retries,
                    designated_next_agent=ctx.next_designated_agent,
                    control_plane_nomination=getattr(ctx.control, "nomination", None),
                )
            elif getattr(ctx.control, "clarification_requested", False):
                from orchestration.nomination import strip_all_nominations
                ctx.output.final_text = strip_all_nominations(ctx.output.final_text)

        retries = thread_state_manager.get_retry_queue(
            ctx.input.channel_id, ctx.input.thread_ts
        )
        resolved_tags = _extract_resolved_nomination_keys(ctx.output.final_text)
        if not ctx.remaining and retries:
            for r_agent in list(retries):
                if r_agent in resolved_tags or f"[{r_agent}]" in ctx.output.final_text:
                    thread_state_manager.pop_retry_agent(
                        ctx.input.channel_id, ctx.input.thread_ts, r_agent
                    )
                    thread_state_manager.mark_already_retried(
                        ctx.input.channel_id, ctx.input.thread_ts, r_agent
                    )
                    break

        agent_model = ctx.agent_cfg.get("model", "")
        if agent_model:
            token_footer = f"{{{agent_model} : {ctx.output.total_prompt_tokens}/{ctx.output.total_completion_tokens}}}"
        else:
            token_footer = (
                f"{{{ctx.output.total_prompt_tokens}/{ctx.output.total_completion_tokens}}}"
            )

        if ctx.output.skills_used:
            skills_str = ", ".join(f"[{s}]" for s in ctx.output.skills_used)
            ctx.output.final_text = f"{ctx.output.final_text}\n\nSkills used: {skills_str}\n\n{token_footer}"
        else:
            ctx.output.final_text = f"{ctx.output.final_text}\n\n{token_footer}"
        ctx.output.generated_tags = resolved_tags
        logger.info(
            f"[ROUTER] Agent {ctx.agent_key} generated tag(s): {ctx.output.generated_tags}. Full text length: {len(ctx.output.final_text)}"
        )

        pacing_secs = getattr(self.deps, "debate_turn_pacing_seconds", None)
        if pacing_secs is None:
            pacing_secs = get_debate_turn_pacing_seconds()
        await self._sleep(pacing_secs)

        if ctx.is_superseded():
            logger.info(
                f"[ROUTER] Thread {ctx.thread_key} turn was superseded after pacing delay. Aborting post."
            )
            return {"ok": False, "cancelled": True, "reason": "superseded"}

        resolved_turn_icon = ctx.icon_url or ctx.agent_cfg.get("icon_url")
        resolved_username = ctx.agent_cfg.get("username", ctx.agent_key) if ctx.agent_cfg else ctx.agent_key
        if ctx.intermediate_msg_ts:
            logger.info(
                f"[WORKSPACE] Updating in-place intermediate message {ctx.intermediate_msg_ts} with final text for {resolved_username}"
            )
            slack_res = await self.workspace_adapter.update_agent_turn(
                channel_id=ctx.input.channel_id,
                message_ts=ctx.intermediate_msg_ts,
                text=ctx.output.final_text,
            )
            if isinstance(slack_res, dict) and not slack_res.get("ok"):
                logger.warning(
                    f"[WORKSPACE] In-place update failed ({slack_res.get('error')}), falling back to post_agent_turn."
                )
                slack_res = await self.workspace_adapter.post_agent_turn(
                    channel_id=ctx.input.channel_id,
                    thread_ts=ctx.input.thread_ts,
                    text=ctx.output.final_text,
                    username=resolved_username,
                    icon_url=resolved_turn_icon,
                )
                posted_ts = slack_res.get("ts") if isinstance(slack_res, dict) else None
            else:
                posted_ts = ctx.intermediate_msg_ts
        else:
            logger.info(
                f"[WORKSPACE] Posting agent response to channel {ctx.input.channel_id}, thread_ts {ctx.input.thread_ts} as {resolved_username} (icon: {resolved_turn_icon})"
            )
            slack_res = await self.workspace_adapter.post_agent_turn(
                channel_id=ctx.input.channel_id,
                thread_ts=ctx.input.thread_ts,
                text=ctx.output.final_text,
                username=resolved_username,
                icon_url=resolved_turn_icon,
            )
            posted_ts = slack_res.get("ts") if isinstance(slack_res, dict) else None
        ctx.output.slack_res = slack_res
        logger.info(f"[WORKSPACE] Successfully posted message! Received ts: {posted_ts}")

        pause_secs = getattr(self.deps, "slack_post_pause_seconds", None)
        if pause_secs is None:
            pause_secs = get_slack_post_pause_seconds()
        await self._sleep(pause_secs)

        has_pending_retries = thread_state_manager.has_pending_retries(
            ctx.input.channel_id, ctx.input.thread_ts
        )
        if getattr(ctx.output, "waiting_hitl", False):
            has_nominated_next = False
            ctx.output.is_completed = False
            ctx.output.halted = True
        else:
            known_agents = (
                set(AGENTS_CONFIG.keys())
                | set(settings_manager.get_enabled_agents())
                | set(settings_manager.system_agents_config.keys())
            )
            has_nominated_next = any(tag in known_agents for tag in ctx.output.generated_tags)
            ctx.output.is_completed = bool(
                ctx.force_termination
                or (
                    not ctx.remaining
                    and not has_pending_retries
                    and not has_nominated_next
                )
                or (thread_state_manager.is_stopped(ctx.input.channel_id, ctx.input.thread_ts))
            )

        await self._finalize_turn_bookkeeping(
            ctx, is_completed=ctx.output.is_completed, record_flat_history=True
        )

        if ctx.output.is_completed and not ctx.input.isolated_branch:
            logger.info(
                f"[ROUTER] Thread {ctx.thread_key} reached termination (forced or natural). Cleaning up state."
            )
            thread_state_manager.clear_round_execution_state(ctx.input.channel_id, ctx.input.thread_ts)

        if isinstance(slack_res, dict):
            slack_res["is_completed"] = ctx.output.is_completed
            slack_res["halted"] = getattr(ctx.output, "halted", False)
            slack_res["waiting_hitl"] = getattr(ctx.output, "waiting_hitl", False)
            slack_res["final_text"] = ctx.output.final_text
            resolved_user = ctx.agent_cfg.get("username", ctx.agent_key) if ctx.agent_cfg else ctx.agent_key
            slack_res["username"] = resolved_user
            slack_res["displayname"] = ctx.agent_cfg.get("displayname", resolved_user) if ctx.agent_cfg else resolved_user
            slack_res["agent_key"] = ctx.agent_key
            slack_res["icon_url"] = resolved_turn_icon
            slack_res["generated_tags"] = ctx.output.generated_tags
            slack_res["convergence_signaled"] = bool(
                getattr(ctx.control, "consensus_signaled", False)
                or getattr(ctx, "convergence_signaled", False)
            )
            slack_res["clarification_question"] = getattr(
                ctx, "control_plane_clarification_question", None
            ) or getattr(ctx.control, "clarification_question", None)

        logger.info(
            f"[{time.time()}] <<< END process_and_respond for thread_ts: {ctx.input.thread_ts} (Total: {time.time() - ctx.start_time:.2f}s) <<<\n"
        )
        return slack_res

    async def handle_error(
        self, ctx: TurnContext, e: Exception
    ) -> dict[str, Any]:
        """Classifies and handles exceptions, performing 503 rotation or rate-limit retry scheduling."""
        if ctx.is_superseded():
            logger.info(
                f"[ROUTER] Exception occurred on superseded turn for thread {ctx.thread_key}. Aborting without posting."
            )
            return {"ok": False, "cancelled": True, "reason": "superseded"}

        logger.exception("[ERROR] Exception during agent completion or post")
        err_category = classify_agent_error(e)
        logger.info(
            f"[ROUTER] Classified error for {ctx.agent_key} as: '{err_category}'"
        )

        stop_requested = thread_state_manager.is_stopped(ctx.input.channel_id, ctx.input.thread_ts)
        already_503 = thread_state_manager.has_agent_503_errored(
            ctx.input.channel_id, ctx.input.thread_ts, ctx.agent_key
        )

        is_first_503_skip = False
        if err_category == "api_key_not_found":
            error_sentence = "API KEY NOT FOUND"
        elif err_category == "model_not_found":
            error_sentence = "MODEL NOT FOUND"
        elif err_category == "rate_limit":
            already_retried = thread_state_manager.is_already_retried(
                ctx.input.channel_id, ctx.input.thread_ts, ctx.agent_key
            )
            if not already_retried and not stop_requested:
                error_sentence = (
                    "RATE LIMITS CROSSED, will retry at the end of discussion"
                )
                thread_state_manager.add_to_retry_queue(
                    ctx.input.channel_id, ctx.input.thread_ts, ctx.agent_key
                )
            else:
                error_sentence = "RATE LIMITS CROSSED"
        elif err_category == "service_unavailable":
            if (
                not already_503
                and not stop_requested
                and not ctx.force_termination
            ):
                thread_state_manager.mark_agent_503_errored(
                    ctx.input.channel_id, ctx.input.thread_ts, ctx.agent_key
                )
                if ctx.agent_key not in ctx.remaining:
                    wf_snap = thread_state_manager.get_workflow_snapshot(ctx.input.channel_id, ctx.input.thread_ts)
                    mode = wf_snap.get("mode") if wf_snap else settings_manager.get_orchestration_mode()
                    if mode == OrchestrationMode.SANDWICH:
                        sandwich_cfg = wf_snap.get("sandwich_config") if wf_snap else settings_manager.get_sandwich_config()
                        end_chain = sandwich_cfg.get("end_chain", [])
                        end_chain_indices = [
                            i
                            for i, k in enumerate(ctx.remaining)
                            if k in end_chain
                        ]
                        if end_chain_indices and ctx.agent_key not in end_chain:
                            ctx.remaining.insert(
                                end_chain_indices[0], ctx.agent_key
                            )
                        else:
                            ctx.remaining.append(ctx.agent_key)
                    else:
                        ctx.remaining.append(ctx.agent_key)
                if not ctx.input.isolated_branch:
                    thread_state_manager.set_remaining_agents(
                        ctx.input.channel_id, ctx.input.thread_ts, ctx.remaining
                    )
                standby_exclusions = {
                    k for k in (settings_manager.get_challenger_agent(), settings_manager.get_arbitrator_agent()) if k
                }
                other_candidates = [
                    a for a in ctx.remaining if a != ctx.agent_key and a not in standby_exclusions
                ]
                if other_candidates:
                    is_first_503_skip = True
                    error_sentence = (
                        "HIGH DEMAND / SERVICE OVERLOADED (503) - Skipping for now, will retry later"
                    )
                else:
                    error_sentence = "HIGH DEMAND / SERVICE OVERLOADED (503)"
            else:
                error_sentence = "HIGH DEMAND / SERVICE OVERLOADED (503)"
        else:
            error_sentence = "Error processing request - check the logs"

        next_nomination = None
        if not stop_requested:
            if is_first_503_skip:
                standby_exclusions = {
                    k for k in (settings_manager.get_challenger_agent(), settings_manager.get_arbitrator_agent()) if k
                }
                other_candidates = [
                    a for a in ctx.remaining if a != ctx.agent_key and a not in standby_exclusions
                ]
                if other_candidates:
                    if mode in (OrchestrationMode.FIXED, OrchestrationMode.SANDWICH, OrchestrationMode.SCATTER_GATHER) or ctx.is_custom_flow:
                        next_agent = other_candidates[0]
                    else:
                        next_agent = random.choice(other_candidates)
                    next_nomination = f"/[{next_agent}]"
                elif [a for a in ctx.remaining if a not in standby_exclusions]:
                    eligible_remaining = [a for a in ctx.remaining if a not in standby_exclusions]
                    next_agent = eligible_remaining[0]
                    next_nomination = f"/[{next_agent}]"
            elif ctx.remaining:
                next_agent = ctx.remaining[0]
                next_nomination = f"/[{next_agent}]"
            else:
                next_agent = thread_state_manager.pop_retry_agent(
                    ctx.input.channel_id, ctx.input.thread_ts
                )
                if next_agent:
                    thread_state_manager.mark_already_retried(
                        ctx.input.channel_id, ctx.input.thread_ts, next_agent
                    )
                    next_nomination = f"/[{next_agent}]"

        if next_nomination:
            error_post_text = f"{error_sentence}\n\n{next_nomination}"
        else:
            error_post_text = error_sentence

        logger.info(
            f"[SLACK] Posting gracious error message to channel {ctx.input.channel_id}, thread_ts {ctx.input.thread_ts} as {ctx.agent_cfg.get('username', ctx.agent_key)}"
        )
        post_resp_fn = getattr(self.deps, "slack_poster", None) or post_agent_response
        slack_res = await post_resp_fn(
            channel_id=ctx.input.channel_id,
            thread_ts=ctx.input.thread_ts,
            text=error_post_text,
            username=ctx.agent_cfg.get("username", ctx.agent_key),
            icon_url=ctx.agent_cfg.get("icon_url"),
        )

        pause_secs = getattr(self.deps, "slack_post_pause_seconds", None)
        if pause_secs is None:
            pause_secs = get_slack_post_pause_seconds()
        await self._sleep(pause_secs)

        ctx.output.is_completed = bool(
            ctx.force_termination
            or not next_nomination
            or thread_state_manager.is_stopped(ctx.input.channel_id, ctx.input.thread_ts)
        )

        await self._finalize_turn_bookkeeping(
            ctx, is_completed=ctx.output.is_completed, record_flat_history=False
        )

        if ctx.output.is_completed and not ctx.input.isolated_branch:
            thread_state_manager.clear_round_execution_state(ctx.input.channel_id, ctx.input.thread_ts)

        if isinstance(slack_res, dict):
            slack_res["is_completed"] = ctx.output.is_completed
            slack_res["final_text"] = error_post_text
            slack_res["username"] = ctx.agent_cfg.get(
                "username", ctx.agent_key
            )
            slack_res["displayname"] = ctx.agent_cfg.get(
                "displayname", slack_res["username"]
            )
            slack_res["agent_key"] = ctx.agent_key

        return {
            "ok": False,
            "error": str(e),
            "classified_error": err_category,
            "slack_res": slack_res,
            "is_completed": ctx.output.is_completed,
            "final_text": error_post_text,
            "username": ctx.agent_cfg.get("username", ctx.agent_key),
            "agent_key": ctx.agent_key,
        }
