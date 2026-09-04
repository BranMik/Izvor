"""
Stage 5: Nomination enforcement, token accounting, Slack dispatch, and centralized error handling.
"""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Any, TYPE_CHECKING

from agents import (
    record_thread_tokens,
    trigger_thread_notes_update,
    trigger_usage_update,
)
from core.config import (
    AGENTS_CONFIG,
    DEBATE_TURN_PACING_SECONDS,
    SLACK_POST_PAUSE_SECONDS,
)
from core.llm_utils import classify_agent_error
from core.models import OrchestrationMode
from nlp.message_builder import markdown_to_slack
from services.slack_client import (
    post_agent_response,
    update_slack_message,
)
from orchestration.agent_settings import settings_manager
from orchestration.nomination import enforce_valid_nomination
from orchestration.thread_state import (
    clear_thread_state,
    has_agent_503_errored,
    is_thread_stopped,
    mark_agent_503_errored,
    thread_state_manager,
)

if TYPE_CHECKING:
    from orchestration.turn_pipeline import TurnContext

logger = logging.getLogger(__name__)


class PostRecordStageMixin:
    """Provides stage 5 dispatch, token accounting, and error handling for TurnPipeline."""

    def _get_hook(self, name: str, default: Any = None) -> Any:
        raise NotImplementedError

    async def _sleep(self, seconds: float):
        raise NotImplementedError

    async def stage_post_and_record(self, ctx: TurnContext) -> dict[str, Any]:
        """Stage 5: Nomination enforcement, token accounting, Slack posting, and notifications."""
        if ctx.is_superseded():
            logger.info(
                f"[ROUTER] Thread {ctx.thread_key} turn was superseded before pacing delay. Aborting."
            )
            return {"ok": False, "cancelled": True, "reason": "superseded"}

        if is_thread_stopped(ctx.channel_id, ctx.thread_ts):
            ctx.force_termination = True
            logger.info(
                f"[ROUTER] Thread {ctx.thread_key} received stop request during execution. Forcing termination."
            )

        retries = thread_state_manager.get_retry_queue(
            ctx.channel_id, ctx.thread_ts
        )
        if not ctx.final_text:
            if ctx.force_termination:
                ctx.final_text = "(No text response generated)"
            else:
                if ctx.next_designated_agent:
                    next_tag = ctx.next_designated_agent
                    ctx.final_text = (
                        f"(No text response generated) [{next_tag}] next"
                    )
                elif ctx.eligible_remaining_agents:
                    next_tag = ctx.eligible_remaining_agents[0]
                    ctx.final_text = (
                        f"(No text response generated) [{next_tag}] next"
                    )
                elif retries:
                    next_tag = retries[0]
                    ctx.final_text = (
                        f"(No text response generated) [{next_tag}] next"
                    )
                else:
                    ctx.final_text = "(No text response generated)"
                    ctx.force_termination = True
            logger.warning(f"[ROUTER] final_text was empty for {ctx.agent_key}!")
        else:
            ctx.final_text = markdown_to_slack(ctx.final_text)
            ctx.final_text = enforce_valid_nomination(
                ctx.final_text,
                ctx.agent_key,
                []
                if ctx.force_termination
                else ctx.eligible_remaining_agents,
                ctx.force_termination,
                pending_retries=retries,
                designated_next_agent=ctx.next_designated_agent,
            )

        retries = thread_state_manager.get_retry_queue(
            ctx.channel_id, ctx.thread_ts
        )
        if not ctx.remaining and retries:
            for r_agent in list(retries):
                if f"[{r_agent}]" in ctx.final_text:
                    thread_state_manager.pop_retry_agent(
                        ctx.channel_id, ctx.thread_ts, r_agent
                    )
                    thread_state_manager.mark_already_retried(
                        ctx.channel_id, ctx.thread_ts, r_agent
                    )
                    break

        agent_model = ctx.agent_cfg.get("model", "")
        if agent_model:
            token_footer = f"{{{agent_model} : {ctx.total_prompt_tokens}/{ctx.total_completion_tokens}}}"
        else:
            token_footer = (
                f"{{{ctx.total_prompt_tokens}/{ctx.total_completion_tokens}}}"
            )

        ctx.final_text = f"{ctx.final_text}\n\n{token_footer}"
        ctx.generated_tags = re.findall(
            r"[\[{]\s*([^\]}\r\n]+?)\s*[\]}]", ctx.final_text
        )
        logger.info(
            f"[ROUTER] Agent {ctx.agent_key} generated tag(s): {ctx.generated_tags}. Full text length: {len(ctx.final_text)}"
        )

        pacing_secs = self._get_hook(
            "DEBATE_TURN_PACING_SECONDS", DEBATE_TURN_PACING_SECONDS
        )
        await self._sleep(pacing_secs)

        if ctx.is_superseded():
            logger.info(
                f"[ROUTER] Thread {ctx.thread_key} turn was superseded after pacing delay. Aborting post."
            )
            return {"ok": False, "cancelled": True, "reason": "superseded"}

        resolved_turn_icon = ctx.icon_url or ctx.agent_cfg.get("icon_url")
        update_msg_fn = self._get_hook(
            "update_slack_message", update_slack_message
        )
        post_resp_fn = self._get_hook(
            "post_agent_response", post_agent_response
        )
        if ctx.intermediate_msg_ts:
            logger.info(
                f"[SLACK] Updating in-place intermediate message {ctx.intermediate_msg_ts} with final text for {ctx.agent_cfg['username']}"
            )
            slack_res = await update_msg_fn(
                channel_id=ctx.channel_id,
                ts=ctx.intermediate_msg_ts,
                text=ctx.final_text,
            )
            if isinstance(slack_res, dict) and not slack_res.get("ok"):
                logger.warning(
                    f"[SLACK] In-place update failed ({slack_res.get('error')}), falling back to post_agent_response."
                )
                slack_res = await post_resp_fn(
                    channel_id=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text=ctx.final_text,
                    username=ctx.agent_cfg["username"],
                    icon_url=resolved_turn_icon,
                )
                posted_ts = slack_res.get("ts") if isinstance(slack_res, dict) else None
            else:
                posted_ts = ctx.intermediate_msg_ts
        else:
            logger.info(
                f"[SLACK] Posting agent response to channel {ctx.channel_id}, thread_ts {ctx.thread_ts} as {ctx.agent_cfg['username']} (icon: {resolved_turn_icon})"
            )
            slack_res = await post_resp_fn(
                channel_id=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                text=ctx.final_text,
                username=ctx.agent_cfg["username"],
                icon_url=resolved_turn_icon,
            )
            posted_ts = slack_res.get("ts") if isinstance(slack_res, dict) else None
        ctx.slack_res = slack_res
        logger.info(f"[SLACK] Successfully posted message! Received ts: {posted_ts}")

        pause_secs = self._get_hook(
            "SLACK_POST_PAUSE_SECONDS", SLACK_POST_PAUSE_SECONDS
        )
        await self._sleep(pause_secs)

        has_pending_retries = thread_state_manager.has_pending_retries(
            ctx.channel_id, ctx.thread_ts
        )
        agents_map = settings_manager.agents_config or AGENTS_CONFIG
        has_nominated_next = any(tag in agents_map for tag in ctx.generated_tags)
        ctx.is_completed = bool(
            ctx.force_termination
            or (
                not ctx.remaining
                and not has_pending_retries
                and not has_nominated_next
            )
            or (is_thread_stopped(ctx.channel_id, ctx.thread_ts))
        )

        record_tokens_fn = self._get_hook(
            "record_thread_tokens", record_thread_tokens
        )
        record_tokens_fn(
            channel_id=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            model_name=ctx.agent_cfg["model"],
            prompt_tokens=ctx.total_prompt_tokens,
            completion_tokens=ctx.total_completion_tokens,
        )

        trig_notes = self._get_hook(
            "trigger_thread_notes_update", trigger_thread_notes_update
        )
        trig_notes(
            channel_id=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            is_completed=ctx.is_completed,
            bot_user_id=ctx.bot_user_id,
        )

        trig_usage = self._get_hook(
            "trigger_usage_update", trigger_usage_update
        )
        trig_usage(
            channel_id=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            is_completed=ctx.is_completed,
        )

        if ctx.is_completed:
            logger.info(
                f"[ROUTER] Thread {ctx.thread_key} reached termination (forced or natural). Cleaning up state."
            )
            clear_thread_state(ctx.channel_id, ctx.thread_ts)

        if isinstance(slack_res, dict):
            slack_res["is_completed"] = ctx.is_completed
            slack_res["final_text"] = ctx.final_text
            slack_res["username"] = ctx.agent_cfg["username"]
            slack_res["agent_key"] = ctx.agent_key
            slack_res["icon_url"] = resolved_turn_icon
            slack_res["generated_tags"] = ctx.generated_tags

        logger.info(
            f"[{time.time()}] <<< END process_and_respond for thread_ts: {ctx.thread_ts} (Total: {time.time() - ctx.start_time:.2f}s) <<<\n"
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

        logger.exception(f"[ERROR] Exception during agent completion or post: {e}")
        err_category = classify_agent_error(e)
        logger.info(
            f"[ROUTER] Classified error for {ctx.agent_key} as: '{err_category}'"
        )

        stop_requested = is_thread_stopped(ctx.channel_id, ctx.thread_ts)
        already_503 = has_agent_503_errored(
            ctx.channel_id, ctx.thread_ts, ctx.agent_key
        )

        is_first_503_skip = False
        if err_category == "api_key_not_found":
            error_sentence = "API KEY NOT FOUND"
        elif err_category == "model_not_found":
            error_sentence = "MODEL NOT FOUND"
        elif err_category == "rate_limit":
            already_retried = thread_state_manager.is_already_retried(
                ctx.channel_id, ctx.thread_ts, ctx.agent_key
            )
            if not already_retried and not stop_requested:
                error_sentence = (
                    "RATE LIMITS CROSSED, will retry at the end of discussion"
                )
                thread_state_manager.add_to_retry_queue(
                    ctx.channel_id, ctx.thread_ts, ctx.agent_key
                )
            else:
                error_sentence = "RATE LIMITS CROSSED"
        elif err_category == "service_unavailable":
            if (
                not already_503
                and not stop_requested
                and not ctx.force_termination
            ):
                mark_agent_503_errored(
                    ctx.channel_id, ctx.thread_ts, ctx.agent_key
                )
                if ctx.agent_key not in ctx.remaining:
                    mode = settings_manager.get_orchestration_mode()
                    if mode == OrchestrationMode.SANDWICH:
                        sandwich_cfg = settings_manager.get_sandwich_config()
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
                thread_state_manager.set_remaining_agents(
                    ctx.channel_id, ctx.thread_ts, ctx.remaining
                )
                other_candidates = [
                    a for a in ctx.remaining if a != ctx.agent_key
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
                other_candidates = [
                    a for a in ctx.remaining if a != ctx.agent_key
                ]
                if other_candidates:
                    next_agent = random.choice(other_candidates)
                    next_nomination = f"[{next_agent}]"
                elif ctx.remaining:
                    next_agent = ctx.remaining[0]
                    next_nomination = f"[{next_agent}]"
            elif ctx.remaining:
                next_agent = ctx.remaining[0]
                next_nomination = f"[{next_agent}]"
            else:
                next_agent = thread_state_manager.pop_retry_agent(
                    ctx.channel_id, ctx.thread_ts
                )
                if next_agent:
                    thread_state_manager.mark_already_retried(
                        ctx.channel_id, ctx.thread_ts, next_agent
                    )
                    next_nomination = f"[{next_agent}]"

        if next_nomination:
            error_post_text = f"{error_sentence}\n\n{next_nomination}"
        else:
            error_post_text = error_sentence

        logger.info(
            f"[SLACK] Posting gracious error message to channel {ctx.channel_id}, thread_ts {ctx.thread_ts} as {ctx.agent_cfg.get('username', ctx.agent_key)}"
        )
        post_resp_fn = self._get_hook(
            "post_agent_response", post_agent_response
        )
        slack_res = await post_resp_fn(
            channel_id=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            text=error_post_text,
            username=ctx.agent_cfg.get("username", ctx.agent_key),
            icon_url=ctx.agent_cfg.get("icon_url"),
        )

        pause_secs = self._get_hook(
            "SLACK_POST_PAUSE_SECONDS", SLACK_POST_PAUSE_SECONDS
        )
        await self._sleep(pause_secs)

        ctx.is_completed = bool(
            ctx.force_termination
            or not next_nomination
            or is_thread_stopped(ctx.channel_id, ctx.thread_ts)
        )

        record_tokens_fn = self._get_hook(
            "record_thread_tokens", record_thread_tokens
        )
        record_tokens_fn(
            channel_id=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            model_name=ctx.agent_cfg.get("model", ""),
            prompt_tokens=0,
            completion_tokens=0,
        )

        trig_notes = self._get_hook(
            "trigger_thread_notes_update", trigger_thread_notes_update
        )
        trig_notes(
            channel_id=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            is_completed=ctx.is_completed,
            bot_user_id=ctx.bot_user_id,
        )

        trig_usage = self._get_hook(
            "trigger_usage_update", trigger_usage_update
        )
        trig_usage(
            channel_id=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            is_completed=ctx.is_completed,
        )

        if ctx.is_completed:
            clear_thread_state(ctx.channel_id, ctx.thread_ts)

        if isinstance(slack_res, dict):
            slack_res["is_completed"] = ctx.is_completed
            slack_res["final_text"] = error_post_text
            slack_res["username"] = ctx.agent_cfg.get(
                "username", ctx.agent_key
            )
            slack_res["agent_key"] = ctx.agent_key

        return {
            "ok": False,
            "error": str(e),
            "classified_error": err_category,
            "slack_res": slack_res,
            "is_completed": ctx.is_completed,
            "final_text": error_post_text,
            "username": ctx.agent_cfg.get("username", ctx.agent_key),
            "agent_key": ctx.agent_key,
        }
