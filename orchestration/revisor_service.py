import logging
import litellm

from core.config import SYSTEM_AGENTS_CONFIG, get_system_agent_model
from core.llm_utils import execute_llm_with_retry
from orchestration.thread_state import thread_state_manager
from orchestration.turn_pipeline import TurnContext
from prompts.builder import build_agent_prompt
from services.slack import post_agent_response

logger = logging.getLogger(__name__)

async def run_internal_revisor(ctx: TurnContext):
    """
    Background task to run the InternalRevisor.
    Checks the last 2 agent messages against the initial user prompt.
    """
    try:
        # 1. Count assistant messages to determine if we are on a 2nd turn
        assistant_msgs = [m for m in ctx.full_history if m.get("role") == "assistant"]
        turn_count = len(assistant_msgs) + 1  # include the current turn

        if turn_count % 2 != 0:
            return None  # Only run on multiples of 2

        logger.info(f"[REVISOR] Triggering InternalRevisor on turn {turn_count}")

        # 2. Get the initial user prompt
        initial_prompt = thread_state_manager.get_initial_prompt(ctx.channel_id, ctx.thread_ts)
        if not initial_prompt:
            initial_prompt = ctx.raw_text

        # 3. Get the last two agent messages
        prev_msg = assistant_msgs[-1].get("content", "") if len(assistant_msgs) >= 1 else ""
        current_msg = f"[{ctx.agent_cfg.get('username', ctx.agent_key)}]: {ctx.final_text}"

        # Check if RAG/documents or tools were used in the current turn
        rag_used = any(s in ctx.skills_used for s in ["knowledge_base_rag", "document_reader"])
        context_block = ""
        retrieved_parts = []
        if rag_used and ctx.file_context:
            retrieved_parts.append(f"DOCUMENT/RAG CONTEXT:\n{ctx.file_context}")

        for msg in (getattr(ctx, "messages", None) or []):
            if isinstance(msg, dict) and msg.get("role") == "tool":
                tool_name = msg.get("name") or "tool"
                tool_content = msg.get("content", "")
                if tool_content:
                    retrieved_parts.append(f"TOOL OUTPUT [{tool_name}]:\n{tool_content}")

        if retrieved_parts:
            context_block = "\n\nRETRIEVED CONTEXT FROM SKILLS / TOOLS:\n" + "\n\n".join(retrieved_parts)

        sys_prompt = build_agent_prompt("InternalRevisor")

        user_content = (
            f"INITIAL USER PROMPT:\n{initial_prompt}{context_block}\n\n"
            f"AGENT MESSAGE 1:\n{prev_msg}\n\n"
            f"AGENT MESSAGE 2 (Latest):\n{current_msg}\n"
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
                        channel_id=ctx.channel_id,
                        thread_ts=ctx.thread_ts,
                        model_name=model,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                    )
                    trigger_usage_update(channel_id=ctx.channel_id, thread_ts=ctx.thread_ts)
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
                    channel_id=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
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
