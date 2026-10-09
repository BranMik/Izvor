import logging
from typing import Any
import litellm

from orchestration.turn_pipeline import TurnContext
from core.llm_utils import execute_llm_with_retry
from services.slack_client import post_agent_response
from prompts.builder import build_agent_prompt
from orchestration.thread_state import thread_state_manager

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
            return  # Only run on multiples of 2
            
        logger.info(f"[REVISOR] Triggering InternalRevisor on turn {turn_count}")
        
        # 2. Get the initial user prompt
        initial_prompt = thread_state_manager.get_initial_prompt(ctx.channel_id, ctx.thread_ts)
        if not initial_prompt:
            initial_prompt = ctx.raw_text
            
        # 3. Get the last two agent messages
        prev_msg = assistant_msgs[-1].get("content", "") if len(assistant_msgs) >= 1 else ""
        current_msg = f"[{ctx.agent_cfg.get('username', ctx.agent_key)}]: {ctx.final_text}"
        
        # Check if RAG/documents were used in the current turn
        # If so, we can optionally append the file_context. 
        # Per user request, subsequent agents are checked only against initial prompt, 
        # but the agent using retrieval gets checked against the context.
        rag_used = any(s in ctx.skills_used for s in ["knowledge_base_rag", "document_reader"])
        context_block = ""
        if rag_used and ctx.file_context:
            context_block = f"\n\nRETRIEVED CONTEXT FROM SKILLS:\n{ctx.file_context}"
            
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
        
        kwargs = {
            "model": "mistral/mistral-small-latest",
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
        
        # 5. Check response and post if hallucination detected
        if res and hasattr(res, "choices") and res.choices:
            output = res.choices[0].message.content.strip()
            
            # The prompt says: "If NO hallucinations are found, simply output "NO_HALLUCINATION"."
            if output != "NO_HALLUCINATION" and "ATTENTION:" in output:
                logger.warning(f"[REVISOR] Hallucination detected! Posting to Slack.")
                await post_agent_response(
                    channel_id=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text=output,
                    username="Internal Revisor",
                    icon_url="static/image_99.png"
                )
            else:
                logger.info("[REVISOR] No hallucinations detected.")
                
    except Exception as e:
        logger.exception(f"[REVISOR] Error executing InternalRevisor: {e}")
