import json
import logging
import re
import litellm

from core.config import SYSTEM_AGENTS_CONFIG, get_system_agent_model
from core.llm_utils import execute_llm_with_retry
from orchestration.thread_state import thread_state_manager
from orchestration.turn_pipeline import TurnContext
from prompts.builder import build_agent_prompt
from services.slack import post_agent_response

logger = logging.getLogger(__name__)

CONTROL_PLANE_TOOLS: frozenset[str] = frozenset({
    "load_skill",
    "nominate_next_speaker",
    "request_user_clarification",
    "signal_consensus_convergence",
})

def _is_pure_missing_skill_notice(ctx: TurnContext) -> bool:
    """Checks if the turn was a compressed missing-skill refusal with no tool execution."""
    missing = getattr(ctx, "missing_required_skills", None)
    if not missing:
        return False
    if getattr(ctx.output, "skills_used", None):
        return False
    final_text = getattr(ctx.output, "final_text", "") or ""
    import re
    cleaned = re.sub(r"\{[^{}]+:\s*\d+/\d+\}", "", final_text)
    cleaned = re.sub(r"/\[[^\]]+\](?:\s+next)?", "", cleaned).strip()
    words = cleaned.split()
    return len(words) <= 55


async def run_internal_revisor(ctx: TurnContext):
    """
    Background task to run the InternalRevisor.
    Checks the last 2 agent messages against the initial user prompt and verified round evidence.
    """
    try:
        # 1. Count assistant messages to determine if we are on a 2nd turn or terminating turn
        assistant_msgs = [m for m in ctx.full_history if m.get("role") == "assistant"]
        turn_count = len(assistant_msgs) + 1  # include the current turn

        rag_used = any(s in getattr(ctx.output, "skills_used", []) for s in ["knowledge_base_rag", "document_reader"])
        is_terminating = (
            getattr(ctx, "force_termination", False)
            or getattr(ctx.output, "is_completed", False)
            or not thread_state_manager.get_remaining_agents(ctx.input.channel_id, ctx.input.thread_ts)
        )

        if turn_count % 2 != 0 and not (is_terminating and (turn_count > 1 or rag_used)):
            return None  # Only run on multiples of 2 or terminating turns with context

        if _is_pure_missing_skill_notice(ctx):
            logger.info(
                f"[REVISOR] Skipping InternalRevisor on turn {turn_count} (compressed missing-skill notice with no tool execution)."
            )
            return None

        logger.info(f"[REVISOR] Triggering InternalRevisor on turn {turn_count}")

        # 2. Get the initial user prompt
        initial_prompt = thread_state_manager.get_initial_prompt(ctx.input.channel_id, ctx.input.thread_ts)
        if not initial_prompt:
            initial_prompt = ctx.input.raw_text

        # 3. Get the last two agent messages with speaker attribution
        prev_msg = assistant_msgs[-1].get("content", "") if len(assistant_msgs) >= 1 else "(No prior agent message; single-turn response)"
        prev_speaker = "Previous Agent"
        if assistant_msgs:
            raw_prev = assistant_msgs[-1].get("content", "")
            match = re.match(r"^\[([^\]]+)\]:", str(raw_prev).strip())
            if match:
                prev_speaker = match.group(1).strip()

        curr_speaker = ctx.agent_cfg.get("username", ctx.agent_key)
        current_msg = f"[{curr_speaker}]: {ctx.output.final_text}"

        # 4. Collect context: current turn files/tools + thread-level verified evidence
        retrieved_parts = []

        file_ctx = ctx.file_context or thread_state_manager.get_file_context(ctx.input.channel_id, ctx.input.thread_ts)
        if file_ctx:
            if isinstance(file_ctx, dict):
                f_name = file_ctx.get("file_name") or file_ctx.get("filename") or file_ctx.get("name") or "Document"
                f_body = file_ctx.get("content") or file_ctx.get("text") or json.dumps(file_ctx, indent=2)
                retrieved_parts.append(f"DOCUMENT/RAG CONTEXT ({f_name}):\n{f_body}")
            else:
                retrieved_parts.append(f"DOCUMENT/RAG CONTEXT:\n{file_ctx}")

        for msg in (getattr(ctx, "messages", None) or []):
            if isinstance(msg, dict) and msg.get("role") == "tool":
                tool_name = msg.get("name") or "tool"
                if tool_name in CONTROL_PLANE_TOOLS:
                    continue
                tool_content = msg.get("content", "")
                if tool_content:
                    retrieved_parts.append(f"TOOL OUTPUT [{tool_name}]:\n{tool_content}")

        # Hydrate from thread-level evidence store (previous agents in the round/thread)
        from orchestration.state.evidence_store import READ_ONLY_CACHEABLE_TOOLS
        evidence_items = thread_state_manager.get_round_evidence(ctx.input.channel_id, ctx.input.thread_ts)
        seen_evidence = set()
        for ev in evidence_items:
            if ev.scope_key == "*" and ev.tool_name in READ_ONLY_CACHEABLE_TOOLS:
                ev_key = (ev.tool_name, getattr(ev, "args_hash", ""))
                if ev_key in seen_evidence:
                    continue
                seen_evidence.add(ev_key)
                ev_res = str(ev.result).strip()
                ev_body = ev_res
                if ev_res.startswith("{") and ev_res.endswith("}"):
                    try:
                        parsed = json.loads(ev_res)
                        if isinstance(parsed, dict) and "content" in parsed and parsed["content"]:
                            doc_name = parsed.get("document") or parsed.get("file") or ev.tool_name
                            ev_body = f"DOCUMENT ({doc_name}):\n{parsed['content']}"
                    except (json.JSONDecodeError, ValueError, TypeError):
                        pass
                retrieved_parts.append(f"VERIFIED EVIDENCE [{ev.tool_name} by {ev.agent_key}]:\n{ev_body}")

        if retrieved_parts:
            context_block = "\n\nRETRIEVED CONTEXT FROM SKILLS / TOOLS:\n" + "\n\n".join(retrieved_parts)
        else:
            context_block = (
                "\n\nRETRIEVED CONTEXT FROM SKILLS / TOOLS: "
                "[None — No external document or tool output was retrieved. "
                "Verify claims strictly against INITIAL USER PROMPT; do NOT guess external file contents.]"
            )

        sys_prompt = build_agent_prompt("InternalRevisor")

        user_content = (
            f"INITIAL USER PROMPT:\n{initial_prompt}{context_block}\n\n"
            f"CANDIDATE MESSAGE FROM [{prev_speaker}]:\n{prev_msg}\n\n"
            f"CANDIDATE MESSAGE FROM [{curr_speaker}] (Latest):\n{current_msg}\n"
        )

        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_content}
        ]

        model = get_system_agent_model("InternalRevisor", fallback="mistral/mistral-small-latest")

        kwargs = {
            "model": model,
            "messages": messages,
            "temperature": 0.0
        }

        # 4. Execute LLM
        res = await execute_llm_with_retry(
            func=litellm.acompletion,
            kwargs=kwargs,
            agent_key="InternalRevisor",
            thread_key=ctx.thread_key,
            allow_503_immediate_retry=False
        )

        # Record spend & token metrics for InternalRevisor
        if res and hasattr(res, "usage") and res.usage:
            prompt_tokens = int(getattr(res.usage, "prompt_tokens", 0) or 0)
            completion_tokens = int(getattr(res.usage, "completion_tokens", 0) or 0)
            if prompt_tokens > 0 or completion_tokens > 0:
                try:
                    from agents.usage_agent import record_thread_tokens, trigger_usage_update

                    record_thread_tokens(
                        channel_id=ctx.input.channel_id,
                        thread_ts=ctx.input.thread_ts,
                        model_name=model,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                    )
                    trigger_usage_update(channel_id=ctx.input.channel_id, thread_ts=ctx.input.thread_ts)
                except Exception as ex:  # noqa: BLE001
                    logger.debug(f"[REVISOR] Could not record token usage for InternalRevisor: {ex}")

        # 5. Check response and post if hallucination detected
        if res and hasattr(res, "choices") and res.choices:
            output = res.choices[0].message.content.strip()

            # The prompt says: "If NO hallucinations are found, simply output "NO_HALLUCINATION"."
            if output != "NO_HALLUCINATION" and "ATTENTION:" in output:
                logger.warning("[REVISOR] Hallucination detected! Posting to Slack.")
                sys_cfg = SYSTEM_AGENTS_CONFIG.get("InternalRevisor", {})
                display_name = sys_cfg.get("displayname") or "Internal Revisor"
                icon_url = sys_cfg.get("icon_url") or "static/image_99.png"
                await post_agent_response(
                    channel_id=ctx.input.channel_id,
                    thread_ts=ctx.input.thread_ts,
                    text=output,
                    username=display_name,
                    icon_url=icon_url
                )
                return output
            else:
                logger.info("[REVISOR] No hallucinations detected.")
                return None

    except Exception as e:
        logger.exception(f"[REVISOR] Error executing InternalRevisor: {e}")
        return None
