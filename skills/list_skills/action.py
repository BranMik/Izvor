"""
Action handler for the list_skills skill.
Executes when /list_skills or /list-skills is parsed.
If invoked without naming an agent and without additional prompt text,
uses the system model to list all skills and authorized agents, posts as a
system message, and halts further agent invocation.
"""

import logging
from typing import Any

from core.config import get_system_agent_model
from core.llm_utils import execute_llm_with_retry
from services.slack_client import post_system_message

logger = logging.getLogger(__name__)


async def run(ctx: Any, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    # /list_skills is strictly exclusive: presents platform skills directory and halts discussion
    channel_id = getattr(ctx, "channel_id", "")
    thread_ts = getattr(ctx, "thread_ts", "")
    from core.registry import get_skill_registry
    skill_registry = get_skill_registry()

    skills_directory_text = skill_registry.format_skills_directory_markdown(
        channel_id=channel_id, thread_ts=thread_ts
    )

    system_model = get_system_agent_model("system_agent", fallback="mistral/mistral-small-latest")
    formatted_msg = skills_directory_text

    try:
        from prompts.builder import load_system_prompt

        _default_sys_prompt_fallback = (
            "You are the Panel System. Present the following application skills and authorized agents directory "
            "clearly, neatly, and accurately to the user in Slack markdown format. Preserve the list of skills and "
            "the authorized agents for each skill without omitting or inventing any."
        )
        sys_prompt = load_system_prompt("List_Skills", fallback=_default_sys_prompt_fallback)
        user_msg = (
            f"Here is the current platform skills directory:\n\n{skills_directory_text}\n\n"
            f"Please present this directory to the user."
        )
        import litellm

        llm_resp = await execute_llm_with_retry(
            litellm.acompletion,
            kwargs={
                "model": system_model,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_msg},
                ],
                "max_tokens": 1000,
            },
            agent_key="list_skills",
            thread_key=(channel_id, thread_ts),
            max_attempts=2,
        )
        content = (
            llm_resp.choices[0].message.content.strip()
            if llm_resp and getattr(llm_resp, "choices", None) and llm_resp.choices[0].message.content
            else ""
        )
        if content:
            formatted_msg = content

        if llm_resp and getattr(llm_resp, "usage", None) and channel_id and thread_ts:
            try:
                from agents.usage_agent import record_thread_tokens
                p_tok = getattr(llm_resp.usage, "prompt_tokens", 0) or 0
                c_tok = getattr(llm_resp.usage, "completion_tokens", 0) or 0
                if p_tok or c_tok:
                    record_thread_tokens(
                        channel_id=channel_id,
                        thread_ts=thread_ts,
                        model=system_model,
                        prompt_tokens=p_tok,
                        completion_tokens=c_tok,
                    )
            except Exception as ue:
                logger.debug(f"[LIST SKILLS] Error recording token spend: {ue}")
    except Exception as e:  # noqa: BLE001 - Skill presentation fallback to raw directory text on LLM failure
        logger.warning(f"[LIST SKILLS] LLM presentation fallback to direct text: {e}")
        formatted_msg = skills_directory_text

    # Post message as system agent
    await post_system_message(
        channel_id=channel_id,
        thread_ts=thread_ts,
        text=formatted_msg,
    )

    # Halt pipeline so no more agents are called
    return {"ok": True, "halted": True, "action": "list_skills"}
