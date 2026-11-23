"""Stage 4 turn finalization collaborator: completion kwargs building and tool-free final synthesis."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

import litellm

from config_store.system_settings import system_settings_manager
from core.llm_utils import execute_llm_with_retry
from orchestration.protocols import PipelineDeps

if TYPE_CHECKING:
    from orchestration.turn_pipeline import TurnContext

logger = logging.getLogger(__name__)


def _is_explicit_meta_provider(model: str | None) -> bool:
    """Returns True only if the model explicitly targets a Meta custom provider endpoint."""
    if not model:
        return False
    m = str(model).lower().strip()
    return m.startswith("meta/") or m.startswith("custom_meta/")


def build_completion_kwargs(
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | None = None,
) -> dict[str, Any]:
    """Builds model-agnostic LiteLLM completion kwargs with optional tools and Meta overrides."""
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
    }
    if tools is not None:
        kwargs["tools"] = tools
    if tool_choice is not None:
        kwargs["tool_choice"] = tool_choice
    if _is_explicit_meta_provider(model):
        if os.getenv("META_API_KEY"):
            kwargs["api_key"] = os.getenv("META_API_KEY")
        if os.getenv("META_API_BASE"):
            kwargs["api_base"] = os.getenv("META_API_BASE")
    return kwargs


def ensure_all_tool_calls_responded(ctx: TurnContext) -> None:
    """Appends synthetic aborted responses for any unresponded assistant tool_calls in ctx.messages."""
    responded_ids = {
        m.get("tool_call_id") for m in ctx.messages if isinstance(m, dict) and m.get("role") == "tool"
    }
    for msg in list(ctx.messages):
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
            if not tc_id or tc_id in responded_ids:
                continue
            fn_obj = tc.get("function", {}) if isinstance(tc, dict) else getattr(tc, "function", None)
            tc_fn = fn_obj.get("name", "tool") if isinstance(fn_obj, dict) else getattr(fn_obj, "name", "tool")
            ctx.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "name": tc_fn,
                    "content": (
                        "<untrusted_file_content>\n"
                        f"TOOL EXECUTION ABORTED: Tool '{tc_fn}' was skipped because the tool loop timeout expired. "
                        "Synthesize your final response using the evidence already gathered above.\n"
                        "</untrusted_file_content>"
                    ),
                }
            )
            responded_ids.add(tc_id)


class TurnFinalizer:
    """Handles post-loop final text assignment and forced tool-free synthesis fallback."""

    def __init__(self, deps: PipelineDeps) -> None:
        self.deps = deps

    def finalize_message(self, ctx: TurnContext, response_message: Any | None) -> None:
        """Extracts final assistant text into ctx.output.final_text without overwriting non-empty fallbacks."""
        content = getattr(response_message, "content", None) if response_message is not None else None
        ctx.output.final_text = content or ctx.output.final_text or ""

    async def synthesize_if_needed(
        self,
        ctx: TurnContext,
        *,
        response_message: Any | None,
        tool_calls: Any,
        timed_out_with_evidence: bool,
        iteration: int,
        max_tool_iterations: int,
        allow_503_immediate: bool,
        on_status_update: Any = None,
    ) -> dict[str, Any] | None:
        """Runs a final tool-free LLM synthesis pass when max iterations or timeout truncated a response."""
        needs_final_synthesis = (
            (tool_calls and not (response_message and response_message.content and str(response_message.content).strip()))
            or timed_out_with_evidence
        ) and (
            not getattr(ctx.output, "waiting_hitl", False)
            and not getattr(ctx.output, "halted", False)
            and not ctx.is_superseded()
        )
        if not needs_final_synthesis:
            return None

        ensure_all_tool_calls_responded(ctx)
        if on_status_update is not None:
            await on_status_update(None)
        logger.info(
            f"[ROUTER] Running final tool-free synthesis call for {ctx.agent_key} "
            f"(iteration={iteration}/{max_tool_iterations}, timed_out_with_evidence={timed_out_with_evidence})."
        )
        ctx.messages.append(
            {
                "role": "user",
                "content": (
                    "Maximum tool call iterations reached. Do NOT request any further tools. "
                    "Provide your complete final response now using only the gathered context and tool outputs above."
                ),
            }
        )
        synthesis_kwargs = build_completion_kwargs(
            model=ctx.agent_cfg.get("model", ""),
            messages=ctx.messages,
        )
        exec_retry_fn = getattr(self.deps, "llm_executor", None) or execute_llm_with_retry
        synthesis_res = await exec_retry_fn(
            litellm.acompletion,
            synthesis_kwargs,
            agent_key=f"{ctx.agent_key} synthesis",
            thread_key=ctx.thread_key,
            is_superseded_fn=ctx.is_superseded,
            max_attempts=max(1, int(system_settings_manager.get_tool_call_num_retries()) + 1),
            allow_503_immediate_retry=allow_503_immediate,
            turn_token_mapping=ctx.turn_token_mapping,
        )
        if synthesis_res is None and ctx.is_superseded():
            return {"ok": False, "cancelled": True, "reason": "superseded"}
        if synthesis_res is not None and hasattr(synthesis_res, "choices") and synthesis_res.choices:
            ctx.add_usage(synthesis_res)
            synthesized_msg = synthesis_res.choices[0].message
            self.finalize_message(ctx, synthesized_msg)
        if not ctx.output.final_text and timed_out_with_evidence:
            ctx.output.final_text = "⚠️ _Tool execution timed out. Continuing discussion._"
        return None

