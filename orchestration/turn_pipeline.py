"""
Turn Execution Pipeline for Izvor.

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
    WorkspaceAdapter,
)
from orchestration.stages import (
    ContextPrepStage,
    ExecutionStage,
    PostRecordStage,
    ResolutionStage,
    ValidationStage,
    _build_tool_status_text,
)
from orchestration.thread_state import thread_state_manager
from services.flat_history_recorder import (
    async_record_flat_history,
    record_flat_history,
)
from services.slack_client import (
    fetch_thread_files,
    fetch_thread_history,
    post_agent_response,
    post_system_message,
    update_slack_message,
)

logger = logging.getLogger(__name__)


@dataclass
class TurnContext:
    """Encapsulates all runtime state and metadata for a single agent turn execution."""

    channel_id: str
    thread_ts: str
    raw_text: str
    user_id: str | None = None
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
    is_open_debate: bool = False
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
    skills_used: list[str] = field(default_factory=list)
    generated_tags: list[str] = field(default_factory=list)
    intermediate_msg_ts: str | None = None
    slack_res: dict[str, Any] | None = None
    is_completed: bool = False
    turn_token_mapping: dict[str, str] = field(default_factory=dict)
    session_id: str = ""
    session: Any = None
    checkpoint_id: str | None = None
    revisor_warning: str | None = None
    structured_nomination: str | None = None
    clarification_question: str | None = None
    convergence_signaled: bool = False
    convergence_summary: str | None = None
    halted: bool = False
    waiting_hitl: bool = False
    target_agent_override: str | None = None

    def __post_init__(self):
        self.thread_key = (self.channel_id, self.thread_ts)
        self.start_time = time.time()
        self.raw_text_cleaned = self.raw_text
        if not self.session_id:
            self.session_id = f"{self.channel_id}:{self.thread_ts}"
        if self.session is None:
            from core.domain import SessionContext

            self.session = SessionContext.from_thread(
                self.channel_id, self.thread_ts, self.in_memory_history
            )


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


class TurnPipeline:
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
        workspace_adapter: WorkspaceAdapter | None = None,
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

        # Workspace Adapter Port (Hexagonal Architecture)
        if workspace_adapter is not None:
            self.workspace_adapter = workspace_adapter
        else:
            from adapters.slack.workspace_adapter import SlackWorkspaceAdapter

            self.workspace_adapter = SlackWorkspaceAdapter(
                post_agent_fn=self.slack_poster,
                update_msg_fn=self.update_message_fn,
                post_system_fn=self.post_system_message_fn,
                hook_resolver=self._get_hook,
            )

        # Initialize the composed stage instances
        self.validation_stage = ValidationStage(self)
        self.resolution_stage = ResolutionStage(self)
        self.context_prep_stage = ContextPrepStage(self)
        self.execution_stage = ExecutionStage(self)
        self.post_record_stage = PostRecordStage(self)

    @property
    def router(self):
        if self._router_module is not None:
            return self._router_module
        from orchestration import agent_router

        return agent_router


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

        # 2. Hooks dictionary passed to constructor (supports both upper and lowercase keys)
        if name in self._hooks and self._hooks[name] is not None:
            return self._hooks[name]
        alt_name = name.lower() if name.isupper() else name.upper()
        if alt_name in self._hooks and self._hooks[alt_name] is not None:
            return self._hooks[alt_name]

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
            "async_record_flat_history": async_record_flat_history,
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
            from services.telemetry import record_turn_trace

            # Stage 1: Validate input, parse directives & resolve candidate agents
            t0 = time.time()
            halt = await self.validation_stage.stage_validate_and_parse(ctx)
            record_turn_trace(
                session_id=getattr(ctx, "session_id", f"{ctx.channel_id}:{ctx.thread_ts}"),
                channel_id=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                agent_key=ctx.agent_key or "unknown",
                stage_name="validation",
                duration_ms=(time.time() - t0) * 1000.0,
                status="HALTED" if halt else "SUCCESS",
            )
            if halt:
                return halt

            # Stage 2: Resolve agent config & spend governance (circuit breaker)
            t0 = time.time()
            halt = await self.resolution_stage.stage_resolve_agent_and_governance(ctx)
            record_turn_trace(
                session_id=getattr(ctx, "session_id", f"{ctx.channel_id}:{ctx.thread_ts}"),
                channel_id=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                agent_key=ctx.agent_key or "unknown",
                stage_name="resolution",
                duration_ms=(time.time() - t0) * 1000.0,
                status="HALTED" if halt else "SUCCESS",
            )
            if halt:
                return halt

            # Stage 3: Prepare conversation context, history slicing & prompt assembly
            t0 = time.time()
            halt = await self.context_prep_stage.stage_prepare_context(ctx)
            record_turn_trace(
                session_id=getattr(ctx, "session_id", f"{ctx.channel_id}:{ctx.thread_ts}"),
                channel_id=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                agent_key=ctx.agent_key or "unknown",
                stage_name="context_prep",
                duration_ms=(time.time() - t0) * 1000.0,
                status="HALTED" if halt else "SUCCESS",
            )
            if halt:
                return halt

            # Stage 4: Execute LLM turn and handle any tool calls
            t0 = time.time()
            halt = await self.execution_stage.stage_execute_llm(ctx)
            record_turn_trace(
                session_id=getattr(ctx, "session_id", f"{ctx.channel_id}:{ctx.thread_ts}"),
                channel_id=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                agent_key=ctx.agent_key or "unknown",
                stage_name="execution",
                duration_ms=(time.time() - t0) * 1000.0,
                prompt_tokens=ctx.total_prompt_tokens,
                completion_tokens=ctx.total_completion_tokens,
                tools_called=ctx.skills_used,
                status="HALTED" if halt else "SUCCESS",
            )
            if halt:
                return halt

            # Stage 5: Format response, enforce nominations, post to Slack, and audit
            t0 = time.time()
            slack_res = await self.post_record_stage.stage_post_and_record(ctx)
            record_turn_trace(
                session_id=getattr(ctx, "session_id", f"{ctx.channel_id}:{ctx.thread_ts}"),
                channel_id=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                agent_key=ctx.agent_key or "unknown",
                stage_name="post_record",
                duration_ms=(time.time() - t0) * 1000.0,
                prompt_tokens=ctx.total_prompt_tokens,
                completion_tokens=ctx.total_completion_tokens,
                tools_called=ctx.skills_used,
                status="SUCCESS" if isinstance(slack_res, dict) and slack_res.get("ok", True) else "FAILED",
            )

            # --- Phase 2: CASCADING HALLUCINATION PREVENTION (TKT-820) ---
            try:
                from core.config import is_internal_revisor_enabled
                if is_internal_revisor_enabled():
                    from orchestration.revisor_service import run_internal_revisor
                    warning = await asyncio.wait_for(run_internal_revisor(ctx), timeout=3.5)
                    if warning and isinstance(slack_res, dict):
                        slack_res["revisor_warning"] = warning
                        ctx.revisor_warning = warning
            except asyncio.TimeoutError:
                logger.warning(f"[REVISOR] InternalRevisor barrier timed out after 3.5s on {ctx.thread_key}")
            except Exception as e:
                logger.error(f"[REVISOR] Failed to execute internal revisor: {e}")

            return slack_res

        except asyncio.CancelledError:
            logger.info(
                f"[ROUTER] Task cancelled for thread {ctx.thread_key} (agent {ctx.agent_key}, gen {ctx.my_generation}). Aborting cleanly."
            )
            if ctx.total_prompt_tokens > 0 or ctx.total_completion_tokens > 0:
                record_tokens_fn = self._get_hook(
                    "record_thread_tokens", record_thread_tokens
                )
                if record_tokens_fn:
                    record_tokens_fn(
                        channel_id=ctx.channel_id,
                        thread_ts=ctx.thread_ts,
                        model_name=ctx.agent_cfg.get("model", "") if ctx.agent_cfg else "",
                        prompt_tokens=ctx.total_prompt_tokens,
                        completion_tokens=ctx.total_completion_tokens,
                    )
            return {"ok": False, "cancelled": True, "reason": "cancelled"}
        except Exception as e:  # noqa: BLE001 - Pipeline error boundary catches unexpected stage errors and delegates to handle_error
            return await self.post_record_stage.handle_error(ctx, e)
        finally:
            if (
                ctx.current_task
                and thread_state_manager.get_active_task(ctx.channel_id, ctx.thread_ts) == ctx.current_task
                and (ctx.in_memory_history is None or ctx.is_completed)
            ):
                thread_state_manager.clear_active_task(ctx.channel_id, ctx.thread_ts, ctx.current_task)

    async def resume_from_checkpoint(
        self,
        checkpoint: Any,
        tool_result: Any | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Resumes an in-flight agent turn from a durable TurnCheckpoint (TKT-810).
        Restores TurnContext, injects tool response with exact tool_call_id, executes LLM follow-up
        completion, runs Stage 5 post & record, and marks the checkpoint as RESUMED.
        """
        import json
        import os
        import litellm
        from core.config import AGENTS_CONFIG
        from core.llm_utils import execute_llm_with_retry
        from orchestration.stages.execution import _is_explicit_meta_provider
        from orchestration.agent_settings import settings_manager
        from orchestration.system_settings import system_settings_manager
        from services.turn_checkpoint_store import turn_checkpoint_store

        if isinstance(checkpoint, str):
            chk = turn_checkpoint_store.get_checkpoint(checkpoint)
            if not chk:
                raise ValueError(f"TurnCheckpoint '{checkpoint}' not found.")
        else:
            chk = checkpoint

        ctx = TurnContext(
            channel_id=chk.channel_id,
            thread_ts=chk.thread_ts,
            raw_text="",
            user_id=user_id,
            is_bot_trigger=True,
            in_memory_history=list(chk.accumulated_history or []),
        )
        ctx.my_generation = thread_state_manager.get_generation(ctx.channel_id, ctx.thread_ts)
        ctx.current_task = asyncio.current_task()
        ctx.session_id = chk.session_id
        ctx.agent_key = chk.agent_key
        ctx.messages = list(chk.messages or [])
        ctx.turn_token_mapping = dict(chk.token_mapping or {})
        ctx.skills_used = list(chk.skills_used or [])
        ctx.total_prompt_tokens = chk.prompt_tokens
        ctx.total_completion_tokens = chk.completion_tokens
        ctx.checkpoint_id = chk.checkpoint_id

        agents_map = settings_manager.agents_config or AGENTS_CONFIG
        ctx.agent_cfg = dict(agents_map.get(ctx.agent_key, {}))

        active_tools = list(chk.active_tools or [])
        pending_tool = chk.pending_tool_call

        if tool_result is not None and pending_tool:
            tool_call_id = pending_tool.get("id") or "call_resumed"
            fn_name = pending_tool.get("name") or "tool"
            tool_result_str = (
                f"<untrusted_file_content>\n{tool_result}\n</untrusted_file_content>"
                if isinstance(tool_result, str)
                else str(tool_result)
            )

            replaced = False
            for m in reversed(ctx.messages):
                if isinstance(m, dict) and m.get("role") == "tool" and m.get("tool_call_id") == tool_call_id:
                    m["content"] = tool_result_str
                    replaced = True
                    break
            if not replaced:
                ctx.messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": fn_name,
                    "content": tool_result_str,
                })

        exec_retry_fn = self._get_hook("execute_llm_with_retry", execute_llm_with_retry)
        follow_up_kwargs: dict[str, Any] = {
            "model": ctx.agent_cfg.get("model", ""),
            "messages": ctx.messages,
            "tools": active_tools if active_tools else None,
            "tool_choice": "auto" if active_tools else None,
            "num_retries": system_settings_manager.get_tool_call_num_retries(),
        }
        if _is_explicit_meta_provider(ctx.agent_cfg.get("model")):
            if os.getenv("META_API_KEY"):
                follow_up_kwargs["api_key"] = os.getenv("META_API_KEY")
            if os.getenv("META_API_BASE"):
                follow_up_kwargs["api_base"] = os.getenv("META_API_BASE")

        follow_up = await exec_retry_fn(
            litellm.acompletion,
            follow_up_kwargs,
            agent_key=f"{ctx.agent_key} resumed",
            thread_key=ctx.thread_key,
            is_superseded_fn=ctx.is_superseded,
            turn_token_mapping=ctx.turn_token_mapping,
        )
        if follow_up is not None:
            ctx.add_usage(follow_up)
            if hasattr(follow_up, "choices") and follow_up.choices:
                response_message = follow_up.choices[0].message
                ctx.final_text = response_message.content or ""

        slack_res = await self.post_record_stage.stage_post_and_record(ctx)

        try:
            from core.config import is_internal_revisor_enabled
            if is_internal_revisor_enabled():
                from orchestration.revisor_service import run_internal_revisor
                warning = await asyncio.wait_for(run_internal_revisor(ctx), timeout=3.5)
                if warning and isinstance(slack_res, dict):
                    slack_res["revisor_warning"] = warning
                    ctx.revisor_warning = warning
        except asyncio.TimeoutError:
            logger.warning(f"[REVISOR] InternalRevisor barrier timed out after 3.5s on {ctx.thread_key}")
        except Exception as e:
            logger.error(f"[REVISOR] Failed to execute internal revisor: {e}")

        turn_checkpoint_store.mark_resumed(chk.checkpoint_id)
        return slack_res


__all__ = ["TurnContext", "TurnPipeline", "_build_tool_status_text"]
