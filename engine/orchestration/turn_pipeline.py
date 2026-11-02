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
import time
from dataclasses import dataclass, field, replace
from typing import Any

from agents import (
    record_thread_tokens,
    trigger_thread_notes_update,
    trigger_usage_update,
)

__all__ = [
    "TurnContext",
    "TurnControlPlane",
    "TurnInput",
    "TurnOutput",
    "TurnPipeline",
    "_build_tool_status_text",
    "record_thread_tokens",
    "trigger_thread_notes_update",
    "trigger_usage_update",
]
from core.models import ActionDirective, MediaAttachment
from orchestration.protocols import (
    PipelineDeps,
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

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TurnInput:
    """Immutable input parameters capturing the trigger of a single agent turn."""

    channel_id: str
    thread_ts: str
    raw_text: str
    user_id: str | None = None
    bot_user_id: str | None = None
    icon_url: str | None = None
    is_bot_trigger: bool = False
    in_memory_history: tuple[dict[str, Any], ...] | None = None
    target_agent_override: str | None = None
    isolated_branch: bool = False
    my_generation: int = 0
    start_time: float = field(default_factory=time.time)


@dataclass
class TurnControlPlane:
    """Control plane consensus, nomination, and clarification state."""

    consensus_signaled: bool = False
    consensus_summary: str | None = None
    nomination: str | None = None
    nomination_rationale: str | None = None
    clarification_requested: bool = False
    clarification_question: str | None = None
    convergence_summary: str | None = None


@dataclass
class TurnOutput:
    """Execution output, token accounting, and dispatch artifacts for a turn."""

    final_text: str = ""
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    skills_used: list[str] = field(default_factory=list)
    generated_tags: list[str] = field(default_factory=list)
    slack_res: dict[str, Any] | None = None
    is_completed: bool = False
    halted: bool = False
    waiting_hitl: bool = False
    checkpoint_id: str | None = None
    revisor_warning: str | None = None


class TurnContext:
    """Encapsulates all runtime state, directives, and metadata for a single agent turn execution."""

    inp: TurnInput
    control_plane: TurnControlPlane
    out: TurnOutput

    # Lifecycle state
    thread_key: tuple[str, str]
    start_time: float
    my_generation: int
    current_task: asyncio.Task | None

    # Directives & Skills
    actions: list[ActionDirective]
    modifiers: dict[str, Any]
    attachments: list[MediaAttachment]
    raw_text_cleaned: str
    history_limit: int | None
    excluded_agents: list[str]
    included_agents: list[str]
    thread_files: list[dict[str, Any]]

    # Resolved routing
    agent_key: str
    prompt: str
    was_explicit_target: bool
    agent_cfg: dict[str, Any]
    remaining: list[str]
    is_custom_flow: bool
    is_open_debate: bool
    force_termination: bool
    eligible_remaining_agents: list[str]
    next_designated_agent: str | None
    target_agent_override: str | None

    # LLM & Conversation Context
    effective_history: list[dict[str, Any]]
    full_history: list[dict[str, Any]]
    system_prompt: str
    file_context: dict[str, Any] | None
    thread_language: str
    messages: list[dict[str, Any]]
    turn_token_mapping: dict[str, str]
    intermediate_msg_ts: str | None

    # Observability & Evidence
    episodic_summary: str | None
    shared_round_evidence: str
    authorized_skills: list[str]
    token_recorder: Any
    session_id: str
    session: Any

    def __init__(
        self,
        channel_id: str | None = None,
        thread_ts: str | None = None,
        raw_text: str = "",
        user_id: str | None = None,
        bot_user_id: str | None = None,
        icon_url: str | None = None,
        is_bot_trigger: bool = False,
        in_memory_history: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
        target_agent_override: str | None = None,
        isolated_branch: bool = False,
        my_generation: int = 0,
        start_time: float | None = None,
        inp: TurnInput | None = None,
        control_plane: TurnControlPlane | None = None,
        out: TurnOutput | None = None,
        **kwargs: Any,
    ):
        if inp is not None:
            self.inp = inp
        else:
            if channel_id is None or thread_ts is None:
                raise ValueError("TurnContext requires channel_id and thread_ts or TurnInput.")
            hist_tuple = tuple(in_memory_history) if in_memory_history is not None else None
            self.inp = TurnInput(
                channel_id=channel_id,
                thread_ts=thread_ts,
                raw_text=raw_text,
                user_id=user_id,
                bot_user_id=bot_user_id,
                icon_url=icon_url,
                is_bot_trigger=is_bot_trigger,
                in_memory_history=hist_tuple,
                target_agent_override=target_agent_override,
                isolated_branch=isolated_branch,
                my_generation=my_generation,
                start_time=start_time or time.time(),
            )

        self.control_plane: TurnControlPlane = control_plane or TurnControlPlane()
        self.out: TurnOutput = out or TurnOutput()

        # Lifecycle state
        self.thread_key = (self.inp.channel_id, self.inp.thread_ts)
        self.start_time = self.inp.start_time
        self.my_generation = self.inp.my_generation
        self.current_task: asyncio.Task | None = kwargs.get("current_task")

        # Directives & Skills
        self.actions: list[ActionDirective] = list(kwargs.get("actions", []))
        self.modifiers: dict[str, Any] = dict(kwargs.get("modifiers", {}))
        self.attachments: list[MediaAttachment] = list(kwargs.get("attachments", []))
        self.raw_text_cleaned: str = kwargs.get("raw_text_cleaned", self.inp.raw_text)
        self.history_limit: int | None = kwargs.get("history_limit")
        self.excluded_agents: list[str] = list(kwargs.get("excluded_agents", []))
        self.included_agents: list[str] = list(kwargs.get("included_agents", []))
        self.thread_files: list[dict[str, Any]] = list(kwargs.get("thread_files", []))

        # Resolved routing
        self.agent_key: str = kwargs.get("agent_key", "")
        self.prompt: str = kwargs.get("prompt", "")
        self.was_explicit_target: bool = kwargs.get("was_explicit_target", False)
        self.agent_cfg: dict[str, Any] = dict(kwargs.get("agent_cfg") or {})
        self.remaining: list[str] = list(kwargs.get("remaining", []))
        self.is_custom_flow: bool = kwargs.get("is_custom_flow", False)
        self.is_open_debate: bool = kwargs.get("is_open_debate", False)
        self.force_termination: bool = kwargs.get("force_termination", False)
        self.eligible_remaining_agents: list[str] = list(kwargs.get("eligible_remaining_agents", []))
        self.next_designated_agent: str | None = kwargs.get("next_designated_agent")
        self.target_agent_override: str | None = kwargs.get("target_agent_override", self.inp.target_agent_override)

        # LLM & Conversation Context
        self.effective_history: list[dict[str, Any]] = list(kwargs.get("effective_history", []))
        self.full_history: list[dict[str, Any]] = list(kwargs.get("full_history", []))
        self.system_prompt: str = kwargs.get("system_prompt", "")
        self.file_context: dict[str, Any] | None = kwargs.get("file_context")
        self.thread_language: str = kwargs.get("thread_language", "English")
        self.messages: list[dict[str, Any]] = list(kwargs.get("messages", []))
        self.turn_token_mapping: dict[str, str] = dict(kwargs.get("turn_token_mapping", {}))
        self.intermediate_msg_ts: str | None = kwargs.get("intermediate_msg_ts")

        # Explicitly declared typed fields previously monkeypatched (C13)
        self.episodic_summary: str | None = kwargs.get("episodic_summary")
        self.shared_round_evidence: str = kwargs.get("shared_round_evidence", "")
        self.authorized_skills: list[str] = list(kwargs.get("authorized_skills", []))
        self.token_recorder: Any = kwargs.get("token_recorder")

        # Session
        self.session_id: str = kwargs.get("session_id", f"{self.inp.channel_id}:{self.inp.thread_ts}")
        self.session: Any = kwargs.get("session")
        if self.session is None:
            from core.domain import SessionContext

            in_mem = list(self.inp.in_memory_history) if self.inp.in_memory_history else None
            self.session = SessionContext.from_thread(self.inp.channel_id, self.inp.thread_ts, in_mem)

    # Input properties
    @property
    def channel_id(self) -> str:
        return getattr(self, "_channel_id_override", self.inp.channel_id)

    @channel_id.setter
    def channel_id(self, val: str) -> None:
        self._channel_id_override = val

    @property
    def thread_ts(self) -> str:
        return getattr(self, "_thread_ts_override", self.inp.thread_ts)

    @thread_ts.setter
    def thread_ts(self, val: str) -> None:
        self._thread_ts_override = val

    @property
    def raw_text(self) -> str:
        return getattr(self, "_raw_text_override", self.inp.raw_text)

    @raw_text.setter
    def raw_text(self, val: str) -> None:
        self._raw_text_override = val

    @property
    def user_id(self) -> str | None:
        return getattr(self, "_user_id_override", self.inp.user_id)

    @user_id.setter
    def user_id(self, val: str | None) -> None:
        self._user_id_override = val

    @property
    def bot_user_id(self) -> str | None:
        return getattr(self, "_bot_user_id_override", self.inp.bot_user_id)

    @bot_user_id.setter
    def bot_user_id(self, val: str | None) -> None:
        self._bot_user_id_override = val

    @property
    def icon_url(self) -> str | None:
        return getattr(self, "_icon_url_override", self.inp.icon_url)

    @icon_url.setter
    def icon_url(self, val: str | None) -> None:
        self._icon_url_override = val

    @property
    def is_bot_trigger(self) -> bool:
        return getattr(self, "_is_bot_trigger_override", self.inp.is_bot_trigger)

    @is_bot_trigger.setter
    def is_bot_trigger(self, val: bool) -> None:
        self._is_bot_trigger_override = val

    @property
    def in_memory_history(self) -> list[dict[str, Any]] | None:
        if hasattr(self, "_in_memory_history_override"):
            return self._in_memory_history_override
        return list(self.inp.in_memory_history) if self.inp.in_memory_history is not None else None

    @in_memory_history.setter
    def in_memory_history(self, val: list[dict[str, Any]] | None) -> None:
        self._in_memory_history_override = list(val) if val is not None else None

    @property
    def isolated_branch(self) -> bool:
        return getattr(self, "_isolated_branch_override", self.inp.isolated_branch)

    @isolated_branch.setter
    def isolated_branch(self, val: bool) -> None:
        self._isolated_branch_override = val

    @property
    def consensus_signaled(self) -> bool:
        return self.control_plane.consensus_signaled

    @consensus_signaled.setter
    def consensus_signaled(self, val: bool) -> None:
        self.control_plane.consensus_signaled = val

    @property
    def is_file_req(self) -> bool:
        return getattr(self, "_is_file_req", False)

    @is_file_req.setter
    def is_file_req(self, val: bool) -> None:
        self._is_file_req = val

    @property
    def target_filename(self) -> str | None:
        return getattr(self, "_target_filename", None)

    @target_filename.setter
    def target_filename(self, val: str | None) -> None:
        self._target_filename = val

    # Output properties
    @property
    def final_text(self) -> str:
        return self.out.final_text

    @final_text.setter
    def final_text(self, val: str):
        self.out.final_text = val

    @property
    def total_prompt_tokens(self) -> int:
        return self.out.total_prompt_tokens

    @total_prompt_tokens.setter
    def total_prompt_tokens(self, val: int):
        self.out.total_prompt_tokens = val

    @property
    def total_completion_tokens(self) -> int:
        return self.out.total_completion_tokens

    @total_completion_tokens.setter
    def total_completion_tokens(self, val: int):
        self.out.total_completion_tokens = val

    @property
    def skills_used(self) -> list[str]:
        return self.out.skills_used

    @skills_used.setter
    def skills_used(self, val: list[str]):
        self.out.skills_used = val

    @property
    def generated_tags(self) -> list[str]:
        return self.out.generated_tags

    @generated_tags.setter
    def generated_tags(self, val: list[str]):
        self.out.generated_tags = val

    @property
    def slack_res(self) -> dict[str, Any] | None:
        return self.out.slack_res

    @slack_res.setter
    def slack_res(self, val: dict[str, Any] | None):
        self.out.slack_res = val

    @property
    def is_completed(self) -> bool:
        return self.out.is_completed

    @is_completed.setter
    def is_completed(self, val: bool):
        self.out.is_completed = val

    @property
    def halted(self) -> bool:
        return self.out.halted

    @halted.setter
    def halted(self, val: bool):
        self.out.halted = val

    @property
    def waiting_hitl(self) -> bool:
        return self.out.waiting_hitl

    @waiting_hitl.setter
    def waiting_hitl(self, val: bool):
        self.out.waiting_hitl = val

    @property
    def checkpoint_id(self) -> str | None:
        return self.out.checkpoint_id

    @checkpoint_id.setter
    def checkpoint_id(self, val: str | None):
        self.out.checkpoint_id = val

    @property
    def revisor_warning(self) -> str | None:
        return self.out.revisor_warning

    @revisor_warning.setter
    def revisor_warning(self, val: str | None):
        self.out.revisor_warning = val

    # Control plane properties
    @property
    def clarification_question(self) -> str | None:
        return self.control_plane.clarification_question

    @clarification_question.setter
    def clarification_question(self, val: str | None):
        self.control_plane.clarification_question = val

    @property
    def convergence_summary(self) -> str | None:
        return self.control_plane.convergence_summary

    @convergence_summary.setter
    def convergence_summary(self, val: str | None):
        self.control_plane.convergence_summary = val

    @property
    def control_plane_nomination(self) -> str | None:
        return self.control_plane.nomination

    @control_plane_nomination.setter
    def control_plane_nomination(self, val: str | None):
        self.control_plane.nomination = val

    @property
    def control_plane_rationale(self) -> str | None:
        return self.control_plane.nomination_rationale

    @control_plane_rationale.setter
    def control_plane_rationale(self, val: str | None):
        self.control_plane.nomination_rationale = val

    @property
    def control_plane_clarification_requested(self) -> bool:
        return self.control_plane.clarification_requested

    @control_plane_clarification_requested.setter
    def control_plane_clarification_requested(self, val: bool):
        self.control_plane.clarification_requested = val

    @property
    def control_plane_clarification_question(self) -> str | None:
        return self.control_plane.clarification_question

    @control_plane_clarification_question.setter
    def control_plane_clarification_question(self, val: str | None):
        self.control_plane.clarification_question = val

    @property
    def control_plane_consensus_signaled(self) -> bool:
        return self.control_plane.consensus_signaled

    @control_plane_consensus_signaled.setter
    def control_plane_consensus_signaled(self, val: bool):
        self.control_plane.consensus_signaled = val

    @property
    def control_plane_consensus_summary(self) -> str | None:
        return self.control_plane.consensus_summary

    @control_plane_consensus_summary.setter
    def control_plane_consensus_summary(self, val: str | None):
        self.control_plane.consensus_summary = val

    def is_superseded(self) -> bool:
        """Determines if this turn was superseded by a newer query or task cancellation."""
        return thread_state_manager.get_generation(self.channel_id, self.thread_ts) != self.my_generation or bool(
            self.current_task and self.current_task.cancelled()
        )

    def add_usage(self, resp: Any):
        """Accumulates prompt and completion token counts and records per-call usage delta."""
        if not resp:
            return
        usage = getattr(resp, "usage", None)
        if not usage:
            return
        if isinstance(usage, dict):
            prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
            completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        else:
            prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)

        self.out.total_prompt_tokens += prompt_tokens
        self.out.total_completion_tokens += completion_tokens

        if prompt_tokens > 0 or completion_tokens > 0:
            resp_model = getattr(resp, "model", None)
            model_name = resp_model or (self.agent_cfg.get("model", "") if self.agent_cfg else "") or "unknown"
            recorder = getattr(self, "token_recorder", None) or record_thread_tokens
            if recorder:
                try:
                    recorder(
                        channel_id=self.channel_id,
                        thread_ts=self.thread_ts,
                        model_name=model_name,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                    )
                except Exception as ex:  # noqa: BLE001
                    logger.debug(f"[TURN CONTEXT] Failed to record usage delta: {ex}")


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
        deps: PipelineDeps | None = None,
        hooks: dict[str, Any] | None = None,
        **overrides: Any,
    ):
        from orchestration.protocols import resolve_pipeline_deps

        self.deps = resolve_pipeline_deps(deps, hooks, **overrides)

        # Initialize the composed stage instances
        self.validation_stage = ValidationStage(self.deps)
        self.resolution_stage = ResolutionStage(self.deps)
        self.context_prep_stage = ContextPrepStage(self.deps)
        self.execution_stage = ExecutionStage(self.deps)
        self.post_record_stage = PostRecordStage(self.deps)

    @property
    def post_system_message_fn(self) -> Any:
        return getattr(self.deps, "post_system_message_fn", None)

    @post_system_message_fn.setter
    def post_system_message_fn(self, value: Any) -> None:
        import dataclasses

        self.deps = dataclasses.replace(self.deps, post_system_message_fn=value)
        if hasattr(self, "validation_stage") and self.validation_stage:
            self.validation_stage.deps = self.deps
        if hasattr(self, "resolution_stage") and self.resolution_stage:
            self.resolution_stage.deps = self.deps
        if hasattr(self, "context_prep_stage") and self.context_prep_stage:
            self.context_prep_stage.deps = self.deps
        if hasattr(self, "execution_stage") and self.execution_stage:
            self.execution_stage.deps = self.deps
        if hasattr(self, "post_record_stage") and self.post_record_stage:
            self.post_record_stage.deps = self.deps

    @property
    def slack_poster(self) -> Any:
        return getattr(self.deps, "slack_poster", None)

    @slack_poster.setter
    def slack_poster(self, value: Any) -> None:
        import dataclasses
        from adapters.slack.workspace_adapter import SlackWorkspaceAdapter

        ws = SlackWorkspaceAdapter(
            post_agent_fn=value,
            update_msg_fn=getattr(self.deps, "update_message_fn", None),
            post_system_fn=getattr(self.deps, "post_system_message_fn", None),
        )
        self.deps = dataclasses.replace(self.deps, slack_poster=value, workspace_adapter=ws)
        if hasattr(self, "post_record_stage") and self.post_record_stage:
            self.post_record_stage.deps = self.deps

    @property
    def workspace_adapter(self) -> Any:
        return getattr(self.deps, "workspace_adapter", None)

    @workspace_adapter.setter
    def workspace_adapter(self, value: Any) -> None:
        import dataclasses

        self.deps = dataclasses.replace(self.deps, workspace_adapter=value)
        if hasattr(self, "post_record_stage") and self.post_record_stage:
            self.post_record_stage.deps = self.deps

    async def _sleep(self, seconds: float):
        """Asynchronous sleep respecting injected sleep_fn."""
        if seconds > 0:
            sleep_func = self.deps.sleep_fn or asyncio.sleep
            await sleep_func(seconds)

    async def run(self, ctx: TurnContext) -> dict[str, Any]:
        """Runs the entire turn execution lifecycle."""
        from services.web_tools import reset_turn_web_budget

        reset_turn_web_budget()

        ctx.token_recorder = getattr(self.deps, "token_recorder", None) or record_thread_tokens

        logger.info(f"\n[{ctx.start_time}] >>> START process_and_respond for thread_ts: {ctx.thread_ts} <<<")

        if not ctx.is_bot_trigger:
            thread_state_manager.increment_generation(ctx.channel_id, ctx.thread_ts)

        ctx.my_generation = thread_state_manager.get_generation(ctx.channel_id, ctx.thread_ts)
        ctx.current_task = asyncio.current_task()
        if ctx.current_task and not ctx.isolated_branch:
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
            except Exception as e:  # noqa: BLE001
                logger.error(f"[REVISOR] Failed to execute internal revisor: {e}")

            return slack_res

        except asyncio.CancelledError:
            logger.info(
                f"[ROUTER] Task cancelled for thread {ctx.thread_key} (agent {ctx.agent_key}, gen {ctx.my_generation}). Aborting cleanly."
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
        ctx.token_recorder = getattr(self.deps, "token_recorder", None) or record_thread_tokens

        agents_map = settings_manager.agents_config or AGENTS_CONFIG
        ctx.agent_cfg = dict(agents_map.get(ctx.agent_key, {}))

        from orchestration.thread_skills_manager import thread_skills_manager
        from skills.loader import skill_registry

        base_skills = list(ctx.agent_cfg.get("skills") or settings_manager.get_agent_skills(ctx.agent_key) or [])
        global_skills = skill_registry.get_global_skills()
        active_thread_skills = thread_skills_manager.get_active_skills(ctx.channel_id, ctx.thread_ts)
        active_agent_skills = thread_skills_manager.get_active_agent_skills(
            ctx.channel_id, ctx.thread_ts, ctx.agent_key
        )
        ctx.authorized_skills = list(
            dict.fromkeys(base_skills + global_skills + active_thread_skills + active_agent_skills)
        )

        active_tools = list(chk.active_tools or [])
        pending_tool = chk.pending_tool_call

        if tool_result is not None and pending_tool:
            tool_call_id = pending_tool.get("id") or "call_resumed"
            fn_name = pending_tool.get("name") or "tool"
            from core.security_utils import wrap_untrusted_content

            tool_result_str = wrap_untrusted_content(str(tool_result))

            replaced = False
            for m in reversed(ctx.messages):
                if isinstance(m, dict) and m.get("role") == "tool" and m.get("tool_call_id") == tool_call_id:
                    m["content"] = tool_result_str
                    replaced = True
                    break
            if not replaced:
                ctx.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "name": fn_name,
                        "content": tool_result_str,
                    }
                )

        exec_retry_fn = self.deps.llm_executor or execute_llm_with_retry
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
        except Exception as e:  # noqa: BLE001
            logger.error(f"[REVISOR] Failed to execute internal revisor: {e}")

        turn_checkpoint_store.mark_resumed(chk.checkpoint_id)
        return slack_res
