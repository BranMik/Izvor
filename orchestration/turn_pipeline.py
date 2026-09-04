"""
Turn Execution Pipeline for SlackAgenticPanel.

Coordinates turn processing across modular stages with an explicit TurnContext container:
  1. ValidationStageMixin: Directives, file inspection, tag validation
  2. ResolutionStageMixin: Agent config lookup, spend governance / circuit breaker
  3. ContextPrepStageMixin: History slicing, prompt formatting, word limits
  4. ExecutionStageMixin: LLM invocation with retries and tool loop
  5. PostRecordStageMixin: Dispatch, token accounting, and centralized error handling
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import sys
import time
from typing import Any

from agents import (
    calculate_thread_costs,
    record_thread_tokens,
    trigger_thread_notes_update,
    trigger_usage_update,
)
from orchestration.stages import (
    _build_tool_status_text,
    ValidationStageMixin,
    ResolutionStageMixin,
    ContextPrepStageMixin,
    ExecutionStageMixin,
    PostRecordStageMixin,
)
from orchestration.thread_state import thread_state_manager

logger = logging.getLogger(__name__)


@dataclass
class TurnContext:
    """Encapsulates all runtime state and metadata for a single agent turn execution."""

    channel_id: str
    thread_ts: str
    raw_text: str
    bot_user_id: str | None = None
    icon_url: str | None = None
    is_bot_trigger: bool = False
    in_memory_history: list[dict[str, Any]] | None = None

    # Lifecycle state
    thread_key: tuple[str, str] = field(init=False)
    start_time: float = field(init=False)
    my_generation: int = 0
    current_task: asyncio.Task | None = None

    # Parsed directives & flags
    raw_text_cleaned: str = ""
    history_limit: int | None = None
    max_words_limit: int | None = None
    min_words_limit: int | None = None
    excluded_agents: list[str] = field(default_factory=list)
    is_file_req: bool = False
    target_filename: str | None = None
    thread_files: list[dict[str, Any]] = field(default_factory=list)

    # Resolved routing
    agent_key: str = ""
    prompt: str = ""
    was_explicit_target: bool = False
    agent_cfg: dict[str, Any] = field(default_factory=dict)
    remaining: list[str] = field(default_factory=list)
    is_custom_flow: bool = False
    force_termination: bool = False
    eligible_remaining_agents: list[str] = field(default_factory=list)
    next_designated_agent: str | None = None

    # LLM & Conversation Context
    effective_history: list[dict[str, Any]] = field(default_factory=list)
    system_prompt: str = ""
    file_context: dict[str, Any] | None = None
    thread_language: str = "English"
    messages: list[dict[str, Any]] = field(default_factory=list)

    # Token accounting & Response
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    final_text: str = ""
    generated_tags: list[str] = field(default_factory=list)
    intermediate_msg_ts: str | None = None
    slack_res: dict[str, Any] | None = None
    is_completed: bool = False

    def __post_init__(self):
        self.thread_key = (self.channel_id, self.thread_ts)
        self.start_time = time.time()
        self.raw_text_cleaned = self.raw_text

    def is_superseded(self) -> bool:
        """Determines if this turn was superseded by a newer query or task cancellation."""
        if thread_state_manager.get_generation(self.channel_id, self.thread_ts) != self.my_generation:
            return True
        if self.current_task and self.current_task.cancelled():
            return True
        return False

    def add_usage(self, resp: Any):
        """Accumulates prompt and completion token counts from an LLM response."""
        if not resp:
            return
        usage = getattr(resp, "usage", None)
        if not usage:
            return
        if isinstance(usage, dict):
            self.total_prompt_tokens += usage.get("prompt_tokens", 0) or 0
            self.total_completion_tokens += usage.get("completion_tokens", 0) or 0
        else:
            self.total_prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
            self.total_completion_tokens += getattr(usage, "completion_tokens", 0) or 0


class TurnPipeline(
    ValidationStageMixin,
    ResolutionStageMixin,
    ContextPrepStageMixin,
    ExecutionStageMixin,
    PostRecordStageMixin,
):
    """
    Orchestrates turn processing across modular stages:
      1. Validation & Directive Parsing
      2. Agent Resolution & Spend Governance
      3. History Slicing & Prompt Construction
      4. LLM Completion & Tool Invocation Loop
      5. Nomination Enforcement, Slack Dispatch & Error Handling
    """

    def __init__(self, router_module=None, hooks: dict[str, Any] | None = None):
        self._router_module = router_module
        self._hooks: dict[str, Any] = hooks or {}

    @property
    def router(self):
        if self._router_module is not None:
            return self._router_module
        return sys.modules.get("orchestration.agent_router")

    def _get_hook(self, name: str, default: Any = None) -> Any:
        """Dynamically retrieves a function or constant from hooks, router module, or default."""
        if name in self._hooks:
            return self._hooks[name]
        r = self.router
        if r is not None and hasattr(r, name):
            return getattr(r, name)
        tp = sys.modules.get("orchestration.turn_pipeline")
        if tp is not None and hasattr(tp, name):
            return getattr(tp, name)
        return default

    async def _sleep(self, seconds: float):
        """Asynchronous sleep respecting any monkeypatch on agent_router.asyncio.sleep."""
        r = self.router
        sleep_fn = getattr(getattr(r, "asyncio", asyncio), "sleep", asyncio.sleep)
        await sleep_fn(seconds)

    async def run(self, ctx: TurnContext) -> dict[str, Any]:
        """Runs the entire turn execution lifecycle."""
        logger.info(
            f"\n[{ctx.start_time}] >>> START process_and_respond for thread_ts: {ctx.thread_ts} <<<"
        )

        if not ctx.is_bot_trigger:
            thread_state_manager.increment_generation(ctx.channel_id, ctx.thread_ts)

        ctx.my_generation = thread_state_manager.get_generation(ctx.channel_id, ctx.thread_ts)
        ctx.current_task = asyncio.current_task()
        if ctx.current_task:
            thread_state_manager.set_active_task(ctx.channel_id, ctx.thread_ts, ctx.current_task)

        try:
            # Stage 1: Validate input, parse directives & resolve candidate agents
            halt = await self.stage_validate_and_parse(ctx)
            if halt:
                return halt

            # Stage 2: Resolve agent config & spend governance (circuit breaker)
            halt = await self.stage_resolve_agent_and_governance(ctx)
            if halt:
                return halt

            # Stage 3: Prepare conversation context, history slicing & prompt assembly
            halt = await self.stage_prepare_context(ctx)
            if halt:
                return halt

            # Stage 4: Execute LLM turn and handle any tool calls
            halt = await self.stage_execute_llm(ctx)
            if halt:
                return halt

            # Stage 5: Format response, enforce nominations, post to Slack, and audit
            return await self.stage_post_and_record(ctx)

        except asyncio.CancelledError:
            logger.info(
                f"[ROUTER] Task cancelled for thread {ctx.thread_key} (agent {ctx.agent_key}, gen {ctx.my_generation}). Aborting cleanly."
            )
            return {"ok": False, "cancelled": True, "reason": "cancelled"}
        except Exception as e:
            return await self.handle_error(ctx, e)
        finally:
            if (
                ctx.current_task
                and thread_state_manager.get_active_task(ctx.channel_id, ctx.thread_ts) == ctx.current_task
            ):
                if ctx.in_memory_history is None or ctx.is_completed:
                    thread_state_manager.clear_active_task(ctx.channel_id, ctx.thread_ts, ctx.current_task)


__all__ = ["TurnContext", "TurnPipeline", "_build_tool_status_text"]
