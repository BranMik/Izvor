"""
DiscussionLoop: Unified multi-agent dialectical discussion orchestration loop.
Consolidates the main loop and continuation loop (e.g. following HITL approval resumption)
into a single robust, re-entrant executor with task registration, scatter-gather topology,
convergence stress-testing, and flat-history synchronization.
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
import inspect
import logging
from typing import Any, Awaitable, Callable

from core.config import get_max_discussion_turns
from core.models import OrchestrationMode
from orchestration import agent_settings
from orchestration.thread_state import thread_state_manager
from pipeline.spend_governance.circuit_breaker import spend_governance
from services.slack import fetch_thread_history

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScatterBranchResult:
    agent_key: str
    response: dict[str, Any] | None
    error: Exception | None = None


def get_orchestration_mode() -> str:
    return agent_settings.get_orchestration_mode()


def get_sandwich_config() -> dict[str, Any]:
    return agent_settings.get_sandwich_config()


class DiscussionLoop:
    """
    Executes an internal multi-agent discussion loop in-memory without relying on
    Slack Events API webhook bounce-backs.
    """

    def __init__(
        self,
        *,
        turn_executor: Callable[..., Awaitable[dict[str, Any]]],
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
        convergence_helper: Callable[..., tuple[list[str], int]] | None = None,
    ) -> None:
        self.turn_executor = turn_executor
        self.sleep_fn = sleep_fn or asyncio.sleep
        if convergence_helper is None:
            from orchestration.agent_router import (
                _apply_convergence_stress_test_or_fast_forward,
            )

            self.convergence_helper = _apply_convergence_stress_test_or_fast_forward
        else:
            self.convergence_helper = convergence_helper

    async def run(
        self,
        *,
        channel_id: str,
        thread_ts: str,
        user_id: str | None = None,
        bot_user_id: str | None = None,
        icon_url: str | None = None,
        max_turns: int | None = None,
        initial_raw_text: str = "",
        initial_turn_res: dict[str, Any] | None = None,
        accumulated_history: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """
        Runs or continues a multi-agent discussion.
        If initial_turn_res is None, executes Turn 1 using initial_raw_text and checks scatter-gather topology.
        If initial_turn_res is provided, resumes discussion from that completed turn result.
        """
        turn_ceiling = get_max_discussion_turns()
        if max_turns is None:
            max_turns = turn_ceiling
        else:
            max_turns = min(max_turns, turn_ceiling)

        thread_key = (channel_id, thread_ts)
        logger.info(
            f"\n[ORCHESTRATOR] Starting/resuming internal discussion loop for thread {thread_key}"
        )

        current_task = asyncio.current_task()
        if current_task:
            thread_state_manager.set_active_task(channel_id, thread_ts, current_task)

        try:
            spoken_agents: set[str] = set()
            consecutive_convergence_count = 0
            next_target_override: str | None = None

            if initial_turn_res is None:
                # --- FRESH START ---
                # 1. Fetch initial thread history from Slack once
                history_list = await fetch_thread_history(
                    channel_id, thread_ts, bot_user_id=bot_user_id
                )
                accumulated_history = list(history_list)

                # Ensure user prompt is in accumulated_history so subsequent agents see it
                if (
                    not accumulated_history
                    or accumulated_history[-1].get("role") != "user"
                    or accumulated_history[-1].get("content") != initial_raw_text
                ):
                    accumulated_history.append({"role": "user", "content": initial_raw_text})

                # 2. Run first turn with user prompt
                turn_res = await self.turn_executor(
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    raw_text=initial_raw_text,
                    bot_user_id=bot_user_id,
                    icon_url=icon_url,
                    is_bot_trigger=False,
                    run_internal_loop=False,
                    in_memory_history=accumulated_history,
                    user_id=user_id,
                )

                last_res = turn_res
                if "agent_key" in turn_res and turn_res["agent_key"]:
                    spoken_agents.add(turn_res["agent_key"])

                if turn_res.get("clarification_question"):
                    logger.info(
                        f"[ORCHESTRATOR] Clarification requested by {turn_res.get('agent_key')} in thread {thread_key}. Suspending thread."
                    )
                    thread_state_manager.suspend_thread(channel_id, thread_ts)
                    return turn_res

                if (
                    turn_res.get("halted")
                    or turn_res.get("cancelled")
                    or turn_res.get("is_completed")
                    or (not turn_res.get("ok", True) and not turn_res.get("final_text"))
                ):
                    logger.info(
                        f"[ORCHESTRATOR] Turn 1 completed or ended discussion for thread {thread_key}."
                    )
                    return turn_res

                if "final_text" in turn_res:
                    agent_name = turn_res.get(
                        "displayname", turn_res.get("username", "Unknown Agent")
                    )
                    accumulated_history.append(
                        {
                            "role": "assistant",
                            "content": f"[{agent_name}]: {turn_res['final_text']}",
                        }
                    )
                if turn_res.get("revisor_warning"):
                    accumulated_history.append(
                        {
                            "role": "system",
                            "content": f"[Internal Revisor Warning]: {turn_res['revisor_warning']}",
                        }
                    )

                # Scatter-Gather Topology Check
                active_mode = get_orchestration_mode()
                if active_mode in (OrchestrationMode.SCATTER_GATHER, "scatter_gather"):
                    remaining = thread_state_manager.get_remaining_agents(channel_id, thread_ts)
                    sandwich_cfg = get_sandwich_config()
                    end_chain = sandwich_cfg.get("end_chain", [])
                    scatter_agents = [a for a in remaining if a not in end_chain]

                    if scatter_agents:
                        logger.info(
                            f"[ORCHESTRATOR] Entering Parallel Scatter Phase for {thread_key}: running {scatter_agents} concurrently."
                        )

                        async def _run_scatter_branch(agent_k: str) -> ScatterBranchResult:
                            reservation_key = f"{channel_id}:{thread_ts}:{agent_k}"
                            spend_governance.reserve_budget(
                                session_id=f"{channel_id}:{thread_ts}",
                                reservation_id=reservation_key,
                                estimated_usd=0.05,
                            )
                            try:
                                branch_history = copy.deepcopy(accumulated_history)
                                branch_kwargs = {
                                    "channel_id": channel_id,
                                    "thread_ts": thread_ts,
                                    "raw_text": initial_raw_text,
                                    "bot_user_id": bot_user_id,
                                    "icon_url": icon_url,
                                    "is_bot_trigger": True,
                                    "run_internal_loop": False,
                                    "in_memory_history": branch_history,
                                    "user_id": user_id,
                                    "target_agent_override": agent_k,
                                }
                                executor_sig = inspect.signature(self.turn_executor)
                                if (
                                    "isolated_branch" in executor_sig.parameters
                                    or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in executor_sig.parameters.values())
                                ):
                                    branch_kwargs["isolated_branch"] = True

                                res = await self.turn_executor(**branch_kwargs)
                                return ScatterBranchResult(agent_key=agent_k, response=res, error=None)
                            except Exception as exc:
                                logger.warning(
                                    f"[ORCHESTRATOR] Scatter branch {agent_k} failed: {exc}",
                                    exc_info=True,
                                )
                                return ScatterBranchResult(agent_key=agent_k, response=None, error=exc)
                            finally:
                                spend_governance.release_reservation(
                                    session_id=f"{channel_id}:{thread_ts}",
                                    reservation_id=reservation_key,
                                )

                        branch_tasks: list[asyncio.Task[ScatterBranchResult]] = []
                        async with asyncio.TaskGroup() as tg:
                            for a in scatter_agents:
                                branch_tasks.append(tg.create_task(_run_scatter_branch(a)))

                        scatter_results: list[ScatterBranchResult] = [t.result() for t in branch_tasks]

                        # Single deterministic post-gather reducer
                        for s_res in scatter_results:
                            if (
                                s_res.response
                                and isinstance(s_res.response, dict)
                                and "final_text" in s_res.response
                            ):
                                s_name = s_res.response.get(
                                    "displayname", s_res.response.get("username", "Unknown Agent")
                                )
                                accumulated_history.append({
                                    "role": "assistant",
                                    "content": f"[{s_name}]: {s_res.response['final_text']}",
                                })
                                spoken_agents.add(s_res.agent_key)
                                last_res = s_res.response
                            elif s_res.error:
                                logger.warning(
                                    f"[ORCHESTRATOR] Scatter branch {s_res.agent_key} error noted: {s_res.error}"
                                )

                        remaining_after = [a for a in remaining if a not in scatter_agents]
                        thread_state_manager.set_remaining_agents(channel_id, thread_ts, remaining_after)

                        # Gather & Synthesis Phase
                        if remaining_after and not thread_state_manager.is_stopped(channel_id, thread_ts):
                            gather_agent = remaining_after[0]
                            logger.info(
                                f"[ORCHESTRATOR] Entering Parallel Gather Phase with synthesis agent {gather_agent}."
                            )
                            gather_res = await self.turn_executor(
                                channel_id=channel_id,
                                thread_ts=thread_ts,
                                raw_text="Synthesize the parallel specialist perspectives into a reconciled conclusion.",
                                bot_user_id=bot_user_id,
                                icon_url=icon_url,
                                is_bot_trigger=True,
                                run_internal_loop=False,
                                in_memory_history=accumulated_history,
                                user_id=user_id,
                                target_agent_override=gather_agent,
                            )
                            last_res = gather_res
                            if isinstance(gather_res, dict) and "final_text" in gather_res:
                                g_name = gather_res.get("displayname", gather_res.get("username", "Unknown Agent"))
                                accumulated_history.append({
                                    "role": "assistant",
                                    "content": f"[{g_name}]: {gather_res['final_text']}",
                                })

                        try:
                            from services.flat_history_recorder import record_flat_history

                            init_p = (
                                thread_state_manager.get_initial_prompt(channel_id, thread_ts)
                                or initial_raw_text
                            )
                            record_flat_history(
                                channel_id=channel_id,
                                thread_ts=thread_ts,
                                initial_prompt=init_p,
                                messages=accumulated_history,
                                bot_user_id=bot_user_id,
                            )
                        except Exception as e:  # noqa: BLE001
                            logger.debug(f"[ORCHESTRATOR] Flat history sync exception: {e}")

                        return last_res
            else:
                # --- RESUMPTION FROM PRIOR TURN ---
                turn_res = initial_turn_res
                last_res = turn_res
                if turn_res.get("halted") or turn_res.get("cancelled") or turn_res.get("is_completed"):
                    return turn_res

                if accumulated_history is None:
                    history_list = await fetch_thread_history(
                        channel_id, thread_ts, bot_user_id=bot_user_id
                    )
                    accumulated_history = list(history_list)

                if "agent_key" in turn_res and turn_res["agent_key"]:
                    spoken_agents.add(turn_res["agent_key"])

                if turn_res.get("clarification_question"):
                    logger.info(
                        f"[ORCHESTRATOR] Clarification requested in thread {thread_key}. Suspending thread."
                    )
                    thread_state_manager.suspend_thread(channel_id, thread_ts)
                    return turn_res

                if "final_text" in turn_res:
                    agent_name = turn_res.get("displayname", turn_res.get("username", "Unknown Agent"))
                    accumulated_history.append(
                        {
                            "role": "assistant",
                            "content": f"[{agent_name}]: {turn_res['final_text']}",
                        }
                    )
                if turn_res.get("revisor_warning"):
                    accumulated_history.append(
                        {
                            "role": "system",
                            "content": f"[Internal Revisor Warning]: {turn_res['revisor_warning']}",
                        }
                    )

            # Determine max_turns based on rounds configuration
            active_rounds = thread_state_manager.get_rounds(channel_id, thread_ts)
            round_seq = thread_state_manager.get_round_sequence(channel_id, thread_ts)
            if active_rounds > 0:
                expected_turns = (active_rounds + 1) * max(1, len(round_seq) or 6)
                max_turns = min(get_max_discussion_turns(), expected_turns)
            else:
                max_turns = min(max_turns or get_max_discussion_turns(), get_max_discussion_turns())

            # Convergence and stress test evaluation before starting subsequent turns
            prev_queue = list(thread_state_manager.get_remaining_agents(channel_id, thread_ts))
            updated_queue, consecutive_convergence_count = self.convergence_helper(
                channel_id=channel_id,
                thread_ts=thread_ts,
                spoken_agents=spoken_agents,
                consecutive_convergence_count=consecutive_convergence_count,
                last_speaker=turn_res.get("agent_key", ""),
                last_turn_converged=bool(turn_res.get("convergence_signaled", False)),
            )
            if updated_queue != prev_queue and updated_queue:
                next_target_override = updated_queue[0]
            else:
                next_target_override = None

            # Main turn progression loop
            start_turn = 2
            turn_num = start_turn
            while turn_num <= max_turns:
                if thread_state_manager.is_stopped(channel_id, thread_ts):
                    logger.info(
                        f"[ORCHESTRATOR] Thread {thread_key} stop detected before turn {turn_num}. Terminating loop."
                    )
                    thread_state_manager.clear_state(channel_id, thread_ts)
                    break

                if last_res.get("is_completed"):
                    logger.info(
                        f"[ORCHESTRATOR] Thread {thread_key} marked completed after turn {turn_num - 1}. Terminating loop."
                    )
                    break

                current_prompt = last_res.get("final_text", "")
                if not current_prompt:
                    logger.warning(
                        f"[ORCHESTRATOR] No prompt available from previous turn {turn_num - 1}. Terminating loop."
                    )
                    break

                logger.info(
                    f"[ORCHESTRATOR] Executing internal turn {turn_num} for thread {thread_key}"
                )
                turn_res = await self.turn_executor(
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    raw_text=current_prompt,
                    bot_user_id=bot_user_id,
                    icon_url=icon_url,
                    is_bot_trigger=True,
                    run_internal_loop=False,
                    in_memory_history=accumulated_history,
                    user_id=user_id,
                    target_agent_override=next_target_override,
                )
                next_target_override = None
                last_res = turn_res

                if "agent_key" in turn_res and turn_res["agent_key"]:
                    spoken_agents.add(turn_res["agent_key"])

                if turn_res.get("clarification_question"):
                    logger.info(
                        f"[ORCHESTRATOR] Clarification requested in thread {thread_key}. Suspending thread."
                    )
                    thread_state_manager.suspend_thread(channel_id, thread_ts)
                    break

                if (
                    turn_res.get("halted")
                    or turn_res.get("cancelled")
                    or turn_res.get("is_completed")
                    or (not turn_res.get("ok", True) and not turn_res.get("final_text"))
                ):
                    logger.info(
                        f"[ORCHESTRATOR] Turn {turn_num} completed or ended discussion for thread {thread_key}."
                    )
                    break

                if "final_text" in turn_res:
                    agent_name = turn_res.get(
                        "displayname", turn_res.get("username", "Unknown Agent")
                    )
                    accumulated_history.append(
                        {
                            "role": "assistant",
                            "content": f"[{agent_name}]: {turn_res['final_text']}",
                        }
                    )
                if turn_res.get("revisor_warning"):
                    accumulated_history.append(
                        {
                            "role": "system",
                            "content": f"[Internal Revisor Warning]: {turn_res['revisor_warning']}",
                        }
                    )

                prev_queue = list(thread_state_manager.get_remaining_agents(channel_id, thread_ts))
                updated_queue, consecutive_convergence_count = self.convergence_helper(
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    spoken_agents=spoken_agents,
                    consecutive_convergence_count=consecutive_convergence_count,
                    last_speaker=turn_res.get("agent_key", ""),
                    last_turn_converged=bool(turn_res.get("convergence_signaled", False)),
                )
                if updated_queue != prev_queue and updated_queue:
                    next_target_override = updated_queue[0]
                else:
                    next_target_override = None

                turn_num += 1

            try:
                from services.flat_history_recorder import record_flat_history

                init_p = (
                    thread_state_manager.get_initial_prompt(channel_id, thread_ts)
                    or initial_raw_text
                )
                record_flat_history(
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    initial_prompt=init_p,
                    messages=accumulated_history,
                    bot_user_id=bot_user_id,
                )
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[ORCHESTRATOR] Final flat history sync exception: {e}")

            if not last_res.get("halted") and not thread_state_manager.get_remaining_agents(channel_id, thread_ts):
                thread_state_manager.clear_round_execution_state(channel_id, thread_ts)

            return last_res
        except Exception as e:
            logger.exception(
                f"[ORCHESTRATOR] Unhandled exception in discussion loop for thread {thread_key}"
            )
            return {"ok": False, "error": str(e)}
        finally:
            if current_task and thread_state_manager.get_active_task(channel_id, thread_ts) == current_task:
                thread_state_manager.clear_active_task(channel_id, thread_ts, current_task)
