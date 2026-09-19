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
import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agents import (
    calculate_thread_costs,
    record_thread_tokens,
    trigger_thread_notes_update,
    trigger_usage_update,
)
from core.llm_utils import execute_llm_with_retry
from core.models import ActionDirective, MediaAttachment
from orchestration.stages import (
    ContextPrepStageMixin,
    ExecutionStageMixin,
    PostRecordStageMixin,
    ResolutionStageMixin,
    ValidationStageMixin,
    _build_tool_status_text,
)
from orchestration.thread_state import thread_state_manager
from services.flat_history_recorder import record_flat_history
from services.slack_client import (
    fetch_thread_files,
    fetch_thread_history,
    post_agent_response,
    post_system_message,
    update_slack_message,
)
from orchestration.protocols import (
    CostCalculator,
    FilesFetcher,
    HistoryFetcher,
    LLMExecutor,
    NotesUpdater,
    SlackMessageUpdater,
    SlackPoster,
    SleepFunction,
    SystemMessagePoster,
    TokenRecorder,
    UsageUpdater,
)

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

    # Unified directives & skills
    actions: list[ActionDirective] = field(default_factory=list)
    modifiers: dict[str, Any] = field(default_factory=dict)
    attachments: list[MediaAttachment] = field(default_factory=list)

    # Parsed directives & flags (legacy mirrors)
    raw_text_cleaned: str = ""
    history_limit: int | None = None
    max_words_limit: int | str | None = None
    min_words_limit: int | str | None = None
    scribe_max_words_limit: int | str | None = None
    scribe_min_words_limit: int | str | None = None
    excluded_agents: list[str] = field(default_factory=list)
    included_agents: list[str] = field(default_factory=list)
    exclude_off: bool = False
    include_off: bool = False
    revoke_target: str | None = None
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
    full_history: list[dict[str, Any]] = field(default_factory=list)
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
        return (
            thread_state_manager.get_generation(self.channel_id, self.thread_ts) != self.my_generation
            or bool(self.current_task and self.current_task.cancelled())
        )

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

    def __init__(
        self,
        router_module: Any = None,
        hooks: dict[str, Any] | None = None,
        *,
        llm_executor: LLMExecutor | None = None,
        slack_poster: SlackPoster | None = None,
        history_fetcher: HistoryFetcher | None = None,
        files_fetcher: FilesFetcher | None = None,
        token_recorder: TokenRecorder | None = None,
        notes_updater: NotesUpdater | None = None,
        usage_updater: UsageUpdater | None = None,
        sleep_fn: SleepFunction | None = None,
        post_system_message_fn: SystemMessagePoster | None = None,
        update_message_fn: SlackMessageUpdater | None = None,
        calc_costs_fn: CostCalculator | None = None,
    ):
        self._router_module = router_module
        self._hooks: dict[str, Any] = hooks or {}

        # Explicit Dependency Injection
        self.llm_executor = llm_executor
        self.slack_poster = slack_poster
        self.history_fetcher = history_fetcher
        self.files_fetcher = files_fetcher
        self.token_recorder = token_recorder
        self.notes_updater = notes_updater
        self.usage_updater = usage_updater
        self.sleep_fn = sleep_fn
        self.post_system_message_fn = post_system_message_fn
        self.update_message_fn = update_message_fn
        self.calc_costs_fn = calc_costs_fn

    @property
    def router(self):
        if self._router_module is not None:
            return self._router_module
        return sys.modules.get("orchestration.agent_router")

    def _get_hook(self, name: str, default: Any = None) -> Any:
        """Dynamically retrieves a function or constant from injected deps, hooks, router module, or default."""
        # 1. Injected dependencies take highest priority
        di_map = {
            "execute_llm_with_retry": self.llm_executor,
            "post_agent_response": self.slack_poster,
            "fetch_thread_history": self.history_fetcher,
            "fetch_thread_files": self.files_fetcher,
            "record_thread_tokens": self.token_recorder,
            "trigger_thread_notes_update": self.notes_updater,
            "trigger_usage_update": self.usage_updater,
            "post_system_message": self.post_system_message_fn,
            "update_slack_message": self.update_message_fn,
            "update_agent_message": self.update_message_fn,
            "calculate_thread_costs": self.calc_costs_fn,
        }
        if name in di_map and di_map[name] is not None:
            return di_map[name]

        # 2. Hooks dictionary passed to constructor
        if name in self._hooks and self._hooks[name] is not None:
            return self._hooks[name]

        # 3. Router module attribute or monkeypatch (for test monkeypatch compatibility)
        if self.router is not None and hasattr(self.router, name):
            return getattr(self.router, name)

        # 4. Default fallback if provided
        if default is not None:
            return default

        # 5. Default canonical service implementations
        fallback_map = {
            "execute_llm_with_retry": execute_llm_with_retry,
            "post_agent_response": post_agent_response,
            "fetch_thread_history": fetch_thread_history,
            "fetch_thread_files": fetch_thread_files,
            "record_thread_tokens": record_thread_tokens,
            "trigger_thread_notes_update": trigger_thread_notes_update,
            "trigger_usage_update": trigger_usage_update,
            "post_system_message": post_system_message,
            "update_slack_message": update_slack_message,
            "update_agent_message": update_slack_message,
            "calculate_thread_costs": calculate_thread_costs,
            "record_flat_history": record_flat_history,
        }
        return fallback_map.get(name, None)

    async def _sleep(self, seconds: float):
        """Asynchronous sleep respecting injected sleep_fn or router monkeypatch."""
        if self.sleep_fn is not None:
            await self.sleep_fn(seconds)
            return
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
                and (ctx.in_memory_history is None or ctx.is_completed)
            ):
                thread_state_manager.clear_active_task(ctx.channel_id, ctx.thread_ts, ctx.current_task)


__all__ = ["TurnContext", "TurnPipeline", "_build_tool_status_text"]
