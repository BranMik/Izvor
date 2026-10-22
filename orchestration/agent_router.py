import asyncio
import logging
import sys
from typing import Any

import litellm
from starlette.concurrency import run_in_threadpool

from agents import (
    calculate_thread_costs,
    record_thread_tokens,
    trigger_thread_notes_update,
    trigger_usage_update,
)
from core.llm_utils import execute_llm_with_retry
from orchestration.thread_state import thread_state_manager
from services.slack import (
    fetch_thread_files,
    fetch_thread_history,
    post_agent_response,
    post_system_message,
)

from .turn_pipeline import TurnContext, TurnPipeline

__all__ = [
    "_apply_convergence_stress_test_or_fast_forward",
    "asyncio",
    "continue_discussion_from_turn_result",
    "orchestrate_discussion",
    "post_system_message",
    "process_and_respond",
    "run_in_threadpool",
]

logger = logging.getLogger(__name__)

# Turn off verbose LiteLLM logging unless debugging
litellm.suppress_debug_info = True
litellm.drop_params = True


_ORIG_POST_AGENT = post_agent_response
_ORIG_POST_SYS = post_system_message
_ORIG_FETCH_HIST = fetch_thread_history
_ORIG_FETCH_FILES = fetch_thread_files


def _get_dynamic_dep(name: str, original_func: Any) -> Any:
    current_router_val = getattr(sys.modules.get(__name__), name, None)
    if current_router_val is not None and current_router_val is not original_func:
        return current_router_val
    slack_mod = sys.modules.get("services.slack")
    if slack_mod is not None:
        current_slack_val = getattr(slack_mod, name, None)
        if current_slack_val is not None and current_slack_val is not original_func:
            return current_slack_val
    return current_router_val or original_func


def _apply_convergence_stress_test_or_fast_forward(
    channel_id: str,
    thread_ts: str,
    spoken_agents: set[str],
    consecutive_convergence_count: int,
    last_speaker: str,
    last_turn_converged: bool,
) -> tuple[list[str], int]:
    """
    Evaluates post-turn convergence state and returns (updated_remaining_agents, updated_consecutive_count).
    """
    from orchestration.app_settings import is_convergence_fast_forward_enabled

    if thread_state_manager.is_custom_flow(channel_id, thread_ts) or not is_convergence_fast_forward_enabled():
        return thread_state_manager.get_remaining_agents(channel_id, thread_ts), 0

    from orchestration.agent_settings import (
        get_challenger_agents,
        get_rebuttal_agent,
        get_sandwich_config,
        settings_manager,
    )
    from orchestration.system_settings import (
        get_convergence_consecutive_threshold,
    )

    if last_turn_converged:
        consecutive_convergence_count += 1
    else:
        consecutive_convergence_count = 0

    challenger_candidates = get_challenger_agents()
    rebuttal_key = get_rebuttal_agent()
    rebuttal_name = (settings_manager.get_agent(rebuttal_key) or {}).get("displayname") or "Rebuttal Agent"
    threshold = get_convergence_consecutive_threshold()
    sandwich_cfg = get_sandwich_config()
    end_chain = sandwich_cfg.get("end_chain", [])
    remaining = thread_state_manager.get_remaining_agents(channel_id, thread_ts)

    # Step 1: When last_speaker == rebuttal_key — Evaluating Rebuttal Verdict
    if rebuttal_key and last_speaker == rebuttal_key:
        if last_turn_converged:
            # Verdict A — Consensus Defended/Patched: truncate remaining_agents to end_chain
            new_queue = [e for e in end_chain if e in remaining or e not in spoken_agents]
            if not new_queue and end_chain:
                new_queue = list(end_chain)
            thread_state_manager.set_remaining_agents(channel_id, thread_ts, new_queue)
            logger.info(
                f"[CONVERGENCE_FF] Verdict A reached by {rebuttal_name} ({rebuttal_key}). Fast-forwarding to end chain: {new_queue}"
            )
            return new_queue, consecutive_convergence_count
        else:
            # Verdict B — Rebuttal Agent Agrees That Consensus Failed:
            # Do NOT truncate remaining_agents! Reset count = 0 and normal dialectic continues.
            logger.info(
                f"[CONVERGENCE_FF] Verdict B reached by {rebuttal_name} ({rebuttal_key}). Fast-forward cancelled, normal dialectic continues."
            )
            return remaining, 0

    # If last speaker was one of the challengers, ensure rebuttal_key is queued next if not already spoken
    if last_speaker in challenger_candidates:
        if rebuttal_key and rebuttal_key not in spoken_agents:
            if not remaining or remaining[0] != rebuttal_key:
                preserved_tail = [a for a in remaining if a != rebuttal_key]
                new_queue = [rebuttal_key] + preserved_tail
                thread_state_manager.set_remaining_agents(channel_id, thread_ts, new_queue)
                return new_queue, consecutive_convergence_count
        return remaining, consecutive_convergence_count

    # Step 2: When consecutive_convergence_count >= threshold
    if consecutive_convergence_count >= threshold:
        # Case A — No Challenger in challenger_candidates has spoken yet
        if spoken_agents.isdisjoint(challenger_candidates):
            chosen_challenger = next(
                (c for c in challenger_candidates if c in remaining),
                challenger_candidates[0] if challenger_candidates else None,
            )
            if chosen_challenger:
                preserved_tail = [
                    a for a in remaining if a not in (chosen_challenger, rebuttal_key)
                ]
                new_queue = (
                    [chosen_challenger]
                    + ([rebuttal_key] if rebuttal_key else [])
                    + preserved_tail
                )
                thread_state_manager.set_remaining_agents(channel_id, thread_ts, new_queue)
                logger.info(
                    f"[CONVERGENCE_FF] Consecutive threshold ({threshold}) met. Stress testing with {chosen_challenger} -> {rebuttal_key}"
                )
                return new_queue, 0

        # Case B — A Challenger HAS already spoken earlier in the round, and rebuttal_key has NOT spoken yet
        if rebuttal_key and rebuttal_key not in spoken_agents:
            preserved_tail = [a for a in remaining if a != rebuttal_key]
            new_queue = ([rebuttal_key] if rebuttal_key else []) + preserved_tail
            thread_state_manager.set_remaining_agents(channel_id, thread_ts, new_queue)
            logger.info(
                f"[CONVERGENCE_FF] Consecutive threshold ({threshold}) met. Injecting rebuttal {rebuttal_key}"
            )
            return new_queue, 0

        # Case C — Both a Challenger and rebuttal_key have ALREADY spoken earlier in the round
        new_queue = [e for e in end_chain if e in remaining or e not in spoken_agents]
        if not new_queue and end_chain:
            new_queue = list(end_chain)
        thread_state_manager.set_remaining_agents(channel_id, thread_ts, new_queue)
        logger.info(
            f"[CONVERGENCE_FF] Consecutive threshold ({threshold}) met after stress test cycle. Fast-forwarding directly to end chain: {new_queue}"
        )
        return new_queue, consecutive_convergence_count

    return remaining, consecutive_convergence_count



async def process_and_respond(
    channel_id: str,
    thread_ts: str,
    raw_text: str,
    bot_user_id: str | None = None,
    icon_url: str | None = None,
    is_bot_trigger: bool = False,
    run_internal_loop: bool = False,
    in_memory_history: list[dict[str, Any]] | None = None,
    user_id: str | None = None,
    target_agent_override: str | None = None,
    isolated_branch: bool = False,
) -> dict[str, Any]:
    """
    Main background handler: parses prompt, builds conversation history,
    calls LiteLLM with agent parameters & MCP tools, and posts response back to Slack thread.
    If run_internal_loop is True, executes the entire multi-agent discussion loop internally.
    """
    # Guardrail Check for user inputs
    if not is_bot_trigger:
        try:
            from services.security.guardrail import guardrail_service
            is_injection = await guardrail_service.is_prompt_injection(raw_text)
            if is_injection:
                logger.warning(f"[GUARDRAIL] Blocked malicious request in {channel_id}")
                await post_system_message(
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    text="⚠️ *Security Guardrail Triggered*\nThis request violates safety policies and has been blocked.",
                )
                thread_state_manager.request_stop(channel_id, thread_ts)
                return {"status": "blocked_by_guardrail"}
        except Exception as e:  # noqa: BLE001
            from orchestration.system_settings import get_guardrail_fail_mode
            fail_mode = get_guardrail_fail_mode()
            if fail_mode == "closed":
                logger.critical(
                    f"[GUARDRAIL] Check threw exception in fail-closed mode, halting turn: {e}"
                )
                try:
                    from services.audit_logger import log_audit_event
                    log_audit_event(
                        event_type="GUARDRAIL_FAIL_CLOSED_BLOCKED",
                        actor={"type": "user", "id": user_id or "unknown"},
                        action="evaluate_prompt_injection",
                        target=channel_id,
                        severity="CRITICAL",
                        status="BLOCKED",
                        details={
                            "channel_id": channel_id,
                            "thread_ts": thread_ts,
                            "error": str(e),
                            "fail_mode": "closed",
                        },
                    )
                except Exception as ae:  # noqa: BLE001
                    logger.error(f"[GUARDRAIL] Failed to emit fail-closed audit event: {ae}")

                await post_system_message(
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    text="⚠️ *Security Guardrail Error*\nGuardrail verification failed in fail-closed mode. This request has been blocked.",
                )
                thread_state_manager.request_stop(channel_id, thread_ts)
                return {"status": "blocked_by_guardrail"}
            logger.error(f"[GUARDRAIL] Check failed, allowing request: {e}")

    if run_internal_loop:
        return await orchestrate_discussion(
            channel_id=channel_id,
            thread_ts=thread_ts,
            raw_text=raw_text,
            bot_user_id=bot_user_id,
            icon_url=icon_url,
            user_id=user_id,
        )

    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text=raw_text,
        user_id=user_id,
        bot_user_id=bot_user_id,
        icon_url=icon_url,
        is_bot_trigger=is_bot_trigger,
        in_memory_history=in_memory_history,
        target_agent_override=target_agent_override,
        isolated_branch=isolated_branch,
    )
    slack_poster_fn = _get_dynamic_dep("post_agent_response", _ORIG_POST_AGENT)
    post_sys_fn = _get_dynamic_dep("post_system_message", _ORIG_POST_SYS)
    fetch_hist_fn = _get_dynamic_dep("fetch_thread_history", _ORIG_FETCH_HIST)
    fetch_files_fn = _get_dynamic_dep("fetch_thread_files", _ORIG_FETCH_FILES)
    run_threadpool_fn = getattr(sys.modules.get(__name__), "run_in_threadpool", run_in_threadpool)
    sleep_fn = getattr(sys.modules.get(__name__).asyncio, "sleep", None)

    pipeline = TurnPipeline(
        hooks={"run_in_threadpool": run_threadpool_fn},
        llm_executor=execute_llm_with_retry,
        slack_poster=slack_poster_fn,
        post_system_message_fn=post_sys_fn,
        history_fetcher=fetch_hist_fn,
        files_fetcher=fetch_files_fn,
        token_recorder=record_thread_tokens,
        notes_updater=trigger_thread_notes_update,
        usage_updater=trigger_usage_update,
        calc_costs_fn=calculate_thread_costs,
        sleep_fn=sleep_fn,
    )
    return await pipeline.run(ctx)


def _build_discussion_loop() -> Any:
    from orchestration.discussion_loop import DiscussionLoop

    return DiscussionLoop(
        turn_executor=process_and_respond,
        convergence_helper=_apply_convergence_stress_test_or_fast_forward,
    )


async def orchestrate_discussion(
    channel_id: str,
    thread_ts: str,
    raw_text: str,
    bot_user_id: str | None = None,
    icon_url: str | None = None,
    max_turns: int | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """
    Internal multi-agent discussion loop:
    Orchestrates the entire multi-agent discussion chain in-memory without relying on
    Slack Events API webhook bounce-backs.
    """
    return await _build_discussion_loop().run(
        channel_id=channel_id,
        thread_ts=thread_ts,
        initial_raw_text=raw_text,
        bot_user_id=bot_user_id,
        icon_url=icon_url,
        max_turns=max_turns,
        user_id=user_id,
    )


async def continue_discussion_from_turn_result(
    channel_id: str,
    thread_ts: str,
    turn_res: dict[str, Any],
    bot_user_id: str | None = None,
    icon_url: str | None = None,
    max_turns: int | None = None,
    user_id: str | None = None,
    accumulated_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Continues multi-agent discussion loop after a turn finishes (e.g. following HITL approval resumption).
    Drives remaining agents until round completion or termination.
    """
    return await _build_discussion_loop().run(
        channel_id=channel_id,
        thread_ts=thread_ts,
        initial_turn_res=turn_res,
        bot_user_id=bot_user_id,
        icon_url=icon_url,
        max_turns=max_turns,
        user_id=user_id,
        accumulated_history=accumulated_history,
    )

