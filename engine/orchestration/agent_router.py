import asyncio
import logging
import sys
from typing import Any

import litellm

from agents import (
    calculate_thread_costs,
    record_thread_tokens,
    trigger_thread_notes_update,
    trigger_usage_update,
)
from core.llm_utils import execute_llm_with_retry
from core.models import OrchestrationMode
from orchestration.thread_state import thread_state_manager
from services.slack import (
    configure_fetch_thread_files,
    configure_fetch_thread_history,
    configure_post_agent_response,
    configure_post_system_message,
    fetch_thread_files,
    fetch_thread_history,
    post_agent_response,
    post_system_message,
    reset_fetch_thread_files,
    reset_fetch_thread_history,
    reset_post_agent_response,
    reset_post_system_message,
)

from .protocols import PipelineDeps, resolve_pipeline_deps
from .turn_pipeline import TurnContext, TurnPipeline

__all__ = [
    "_apply_convergence_stress_test_or_fast_forward",
    "configure_fetch_thread_files",
    "configure_fetch_thread_history",
    "configure_post_agent_response",
    "configure_post_system_message",
    "configure_router_deps",
    "configure_router_sleep_fn",
    "continue_discussion_from_turn_result",
    "get_router_deps",
    "orchestrate_discussion",
    "post_system_message",
    "process_and_respond",
    "reset_fetch_thread_files",
    "reset_fetch_thread_history",
    "reset_post_agent_response",
    "reset_post_system_message",
    "reset_router_deps",
    "reset_router_sleep_fn",
]

logger = logging.getLogger(__name__)

# Turn off verbose LiteLLM logging unless debugging
litellm.suppress_debug_info = True
litellm.drop_params = True

_router_deps_override: PipelineDeps | None = None
_router_sleep_fn_override: Any = None


def configure_router_deps(deps: PipelineDeps | None = None) -> None:
    """Configures global default PipelineDeps for agent_router."""
    global _router_deps_override
    _router_deps_override = deps


def reset_router_deps() -> None:
    """Resets global default PipelineDeps for agent_router."""
    global _router_deps_override
    _router_deps_override = None


def get_router_deps() -> PipelineDeps | None:
    """Returns the currently configured PipelineDeps override, if any."""
    return _router_deps_override


def configure_router_sleep_fn(fn: Any) -> None:
    """Configures an in-memory sleep function override for agent_router."""
    global _router_sleep_fn_override
    _router_sleep_fn_override = fn


def reset_router_sleep_fn() -> None:
    """Resets the in-memory sleep function override for agent_router."""
    global _router_sleep_fn_override
    _router_sleep_fn_override = None


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
        get_sandwich_config,
        settings_manager,
    )
    from orchestration.system_settings import (
        get_convergence_consecutive_threshold,
        is_dialectical_stress_test_enabled,
    )

    if last_turn_converged:
        consecutive_convergence_count += 1
        thread_state_manager.record_convergence_speaker(channel_id, thread_ts, last_speaker)
    else:
        consecutive_convergence_count = 0
        thread_state_manager.clear_convergence_speakers(channel_id, thread_ts)

    excluded = set(thread_state_manager.get_excluded_agents(channel_id, thread_ts))

    raw_challenger = settings_manager.get_challenger_agent()
    challenger_key = raw_challenger if raw_challenger and raw_challenger not in excluded else None
    challenger_name = (settings_manager.get_agent(challenger_key) or {}).get("displayname") or "Challenger"

    raw_arbitrator = settings_manager.get_arbitrator_agent()
    arbitrator_key = raw_arbitrator if raw_arbitrator and raw_arbitrator not in excluded else None
    arbitrator_name = (settings_manager.get_agent(arbitrator_key) or {}).get("displayname") or "Arbitrator"
    threshold = get_convergence_consecutive_threshold()
    wf_snap = thread_state_manager.get_workflow_snapshot(channel_id, thread_ts)
    sandwich_cfg = (wf_snap.get("sandwich_config") if wf_snap else None) or get_sandwich_config() or {}
    end_chain = sandwich_cfg.get("end_chain", []) if isinstance(sandwich_cfg, dict) else []
    remaining = thread_state_manager.get_remaining_agents(channel_id, thread_ts)

    mode = wf_snap.get("mode") if wf_snap else settings_manager.get_orchestration_mode()
    mode_val = mode.value if isinstance(mode, OrchestrationMode) else str(mode)
    is_sandwich = mode_val.lower() == OrchestrationMode.SANDWICH.value

    # Fixed-Chain & Small Remaining Pool Guard (Suggestion 2):
    # Only trigger fast-forward or stress testing in SANDWICH mode or when more than 2 remaining agents exist.
    # In fixed review chains with <= 2 remaining agents, let the sequence complete naturally.
    if not is_sandwich and len(remaining) <= 2:
        return remaining, consecutive_convergence_count

    # If stress test is disabled, bypass dialectical challenge injection entirely
    if not is_dialectical_stress_test_enabled():
        if consecutive_convergence_count >= threshold:
            new_queue = [e for e in end_chain if e in remaining or e not in spoken_agents]
            if not new_queue and end_chain:
                new_queue = list(end_chain)
            thread_state_manager.set_remaining_agents(channel_id, thread_ts, new_queue)
            thread_state_manager.clear_convergence_speakers(channel_id, thread_ts)
            logger.info(
                f"[CONVERGENCE_FF] Consecutive threshold ({threshold}) met (stress testing disabled). Fast-forwarding directly to end chain: {new_queue}"
            )
            return new_queue, consecutive_convergence_count
        return remaining, consecutive_convergence_count

    # Step 1: When last_speaker == arbitrator_key — Evaluating Arbitrator Verdict
    if arbitrator_key and last_speaker == arbitrator_key:
        if last_turn_converged:
            # Verdict A — Consensus Defended/Patched: truncate remaining_agents to end_chain and prune middle remaining
            new_queue = [e for e in end_chain if e in remaining or e not in spoken_agents]
            if not new_queue and end_chain:
                new_queue = list(end_chain)
            thread_state_manager.set_remaining_agents(channel_id, thread_ts, new_queue)
            thread_state_manager.clear_convergence_speakers(channel_id, thread_ts)
            logger.info(
                f"[CONVERGENCE_FF] Verdict A reached by {arbitrator_name} ({arbitrator_key}). Fast-forwarding to end chain: {new_queue}"
            )
            return new_queue, consecutive_convergence_count
        else:
            # Verdict B — Arbitrator Agrees That Consensus Failed:
            # Do NOT truncate remaining_agents! Reset count = 0 and normal dialectic continues.
            logger.info(
                f"[CONVERGENCE_FF] Verdict B reached by {arbitrator_name} ({arbitrator_key}). Fast-forward cancelled, normal dialectic continues."
            )
            return remaining, 0

    # If last speaker was the Challenger, ensure arbitrator_key is queued next if not already spoken
    if challenger_key and last_speaker == challenger_key:
        if arbitrator_key and arbitrator_key not in spoken_agents:
            if not remaining or remaining[0] != arbitrator_key:
                preserved_tail = [a for a in remaining if a != arbitrator_key]
                new_queue = [arbitrator_key] + preserved_tail
                thread_state_manager.set_remaining_agents(channel_id, thread_ts, new_queue)
                return new_queue, consecutive_convergence_count
        return remaining, consecutive_convergence_count

    # Step 2: When consecutive_convergence_count >= threshold
    if consecutive_convergence_count >= threshold:
        converged_speakers = set(thread_state_manager.get_convergence_speakers(channel_id, thread_ts))

        # Case A — Challenger is eligible (was not part of the current convergence streak and hasn't spoken yet in this streak)
        if challenger_key and challenger_key not in converged_speakers and challenger_key not in spoken_agents:
            preserved_tail = [
                a for a in remaining if a not in (challenger_key, arbitrator_key)
            ]
            new_queue = (
                [challenger_key]
                + ([arbitrator_key] if arbitrator_key and arbitrator_key not in spoken_agents else [])
                + preserved_tail
            )
            thread_state_manager.set_remaining_agents(channel_id, thread_ts, new_queue)
            thread_state_manager.clear_convergence_speakers(channel_id, thread_ts)
            logger.info(
                f"[CONVERGENCE_FF] Consecutive threshold ({threshold}) met. Stress testing with {challenger_key} -> {arbitrator_key}"
            )
            return new_queue, 0

        # Case B — Challenger has already spoken or participated, but arbitrator_key has NOT spoken yet
        if arbitrator_key and arbitrator_key not in spoken_agents:
            preserved_tail = [a for a in remaining if a != arbitrator_key]
            new_queue = [arbitrator_key] + preserved_tail
            thread_state_manager.set_remaining_agents(channel_id, thread_ts, new_queue)
            thread_state_manager.clear_convergence_speakers(channel_id, thread_ts)
            logger.info(
                f"[CONVERGENCE_FF] Consecutive threshold ({threshold}) met. Injecting arbitrator {arbitrator_key}"
            )
            return new_queue, 0

        # Case C — Both Challenger and arbitrator_key have already spoken / participated
        new_queue = [e for e in end_chain if e in remaining or e not in spoken_agents]
        if not new_queue and end_chain:
            new_queue = list(end_chain)
        thread_state_manager.set_remaining_agents(channel_id, thread_ts, new_queue)
        thread_state_manager.clear_convergence_speakers(channel_id, thread_ts)
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
    deps: PipelineDeps | None = None,
) -> dict[str, Any]:
    """
    Main background handler: parses prompt, builds conversation history,
    calls LiteLLM with agent parameters & tools, and posts response back to Slack thread.
    If run_internal_loop is True, executes the entire multi-agent discussion loop internally.
    """
    target_deps = deps or _router_deps_override
    active_deps = resolve_pipeline_deps(
        target_deps,
        llm_executor=(target_deps.llm_executor if target_deps and target_deps.llm_executor is not None else execute_llm_with_retry),
        slack_poster=(target_deps.slack_poster if target_deps and target_deps.slack_poster is not None else post_agent_response),
        post_system_message_fn=(target_deps.post_system_message_fn if target_deps and target_deps.post_system_message_fn is not None else post_system_message),
        history_fetcher=(target_deps.history_fetcher if target_deps and target_deps.history_fetcher is not None else fetch_thread_history),
        files_fetcher=(target_deps.files_fetcher if target_deps and target_deps.files_fetcher is not None else fetch_thread_files),
        token_recorder=(target_deps.token_recorder if target_deps and target_deps.token_recorder is not None else record_thread_tokens),
        notes_updater=(target_deps.notes_updater if target_deps and target_deps.notes_updater is not None else trigger_thread_notes_update),
        usage_updater=(target_deps.usage_updater if target_deps and target_deps.usage_updater is not None else trigger_usage_update),
        calc_costs_fn=(target_deps.calc_costs_fn if target_deps and target_deps.calc_costs_fn is not None else calculate_thread_costs),
        sleep_fn=_router_sleep_fn_override or (target_deps.sleep_fn if target_deps else None) or asyncio.sleep,
    )

    # Guardrail Check for user inputs
    if not is_bot_trigger:
        try:
            from services.security.guardrail import guardrail_service
            is_injection = await guardrail_service.is_prompt_injection(raw_text)
            if is_injection:
                logger.warning(f"[GUARDRAIL] Blocked malicious request in {channel_id}")
                post_sys_fn = getattr(active_deps, "post_system_message_fn", None) or post_system_message
                await post_sys_fn(
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

                post_sys_fn = getattr(active_deps, "post_system_message_fn", None) or post_system_message
                await post_sys_fn(
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    text="⚠️ *Security Guardrail Error*\nGuardrail verification failed in fail-closed mode. This request has been blocked.",
                )
                thread_state_manager.request_stop(channel_id, thread_ts)
                return {"status": "blocked_by_guardrail"}
            logger.error(f"[GUARDRAIL] Check failed, allowing request: {e}")

    if run_internal_loop:
        kwargs_to_pass: dict[str, Any] = {
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "raw_text": raw_text,
            "bot_user_id": bot_user_id,
            "icon_url": icon_url,
            "user_id": user_id,
        }
        if deps is not None:
            kwargs_to_pass["deps"] = deps
        return await orchestrate_discussion(**kwargs_to_pass)

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
    pipeline = TurnPipeline(deps=active_deps)
    return await pipeline.run(ctx)


def _build_discussion_loop(sleep_fn: Any = None, deps: PipelineDeps | None = None) -> Any:
    from orchestration.discussion_loop import DiscussionLoop

    active_deps = deps or _router_deps_override
    effective_sleep = (
        sleep_fn
        or _router_sleep_fn_override
        or (getattr(active_deps, "sleep_fn", None) if active_deps else None)
        or asyncio.sleep
    )

    async def _turn_exec(*args: Any, **kwargs: Any) -> dict[str, Any]:
        if "deps" not in kwargs and active_deps is not None:
            kwargs["deps"] = active_deps
        return await process_and_respond(*args, **kwargs)

    return DiscussionLoop(
        turn_executor=_turn_exec,
        sleep_fn=effective_sleep,
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
    target_agent_override: str | None = None,
    is_bot_trigger: bool = False,
    deps: PipelineDeps | None = None,
) -> dict[str, Any]:
    """
    Internal multi-agent discussion loop:
    Orchestrates the entire multi-agent discussion chain in-memory without relying on
    Slack Events API webhook bounce-backs.
    """
    return await _build_discussion_loop(deps=deps).run(
        channel_id=channel_id,
        thread_ts=thread_ts,
        initial_raw_text=raw_text,
        bot_user_id=bot_user_id,
        icon_url=icon_url,
        max_turns=max_turns,
        user_id=user_id,
        target_agent_override=target_agent_override,
        is_bot_trigger=is_bot_trigger,
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
    deps: PipelineDeps | None = None,
) -> dict[str, Any]:
    """
    Continues multi-agent discussion loop after a turn finishes (e.g. following HITL approval resumption).
    Drives remaining agents until round completion or termination.
    """
    return await _build_discussion_loop(deps=deps).run(
        channel_id=channel_id,
        thread_ts=thread_ts,
        initial_turn_res=turn_res,
        bot_user_id=bot_user_id,
        icon_url=icon_url,
        max_turns=max_turns,
        user_id=user_id,
        accumulated_history=accumulated_history,
    )

