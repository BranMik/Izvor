"""
Action handler for the list_skills skill.
Executes when /list_skills or /list-skills is parsed.
If invoked without naming an agent and without additional prompt text,
uses the system model to list all skills and authorized agents, posts as a
system message, and halts further agent invocation.
"""

import logging
import re
from typing import Any

from core.config import get_system_agent_model
from core.llm_utils import execute_llm_with_retry
from pipeline.entity_resolver import entity_resolver
from services.slack_client import post_system_message

logger = logging.getLogger(__name__)


async def run(ctx: Any, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """
    Evaluates /list_skills slash command:
    - If no agent is named and no additional prompt text is present:
      uses system model to list all skills and halts the turn.
    - If an agent is named or additional prompt text is present:
      returns None, allowing normal routing to the designated agent.
    """
    raw = getattr(ctx, "raw_text", "") or ""
    channel_id = getattr(ctx, "channel_id", "")
    thread_ts = getattr(ctx, "thread_ts", "")

    # 1. Check if any agents were tagged or named
    tagged_agents = entity_resolver.extract_tagged_agents(raw)
    ambiguous_tags = entity_resolver.detect_ambiguous_tags(raw)
    unrecognized_tags = entity_resolver.detect_unrecognized_tags(raw)
    has_named_agent = bool(tagged_agents or ambiguous_tags or unrecognized_tags)

    # 2. Check if there is additional prompt text beyond the slash command and agent tags
    cleaned = re.sub(r"/(?:list_skills|list-skills)\b", "", raw, flags=re.IGNORECASE)
    for tag in tagged_agents:
        cleaned = re.sub(rf"\[?{re.escape(tag)}\]?", "", cleaned, flags=re.IGNORECASE)
    # Strip any remaining bracketed expressions (e.g. [UnknownAgent]) or Slack user mentions (<@U...>)
    cleaned = re.sub(r"\[[^\]]+\]", "", cleaned)
    cleaned = re.sub(r"<@[A-Z0-9]+>", "", cleaned)
    has_additional_text = bool(cleaned.strip())

    # If an agent was named or additional text is present, allow pipeline to continue to agent
    if has_named_agent or has_additional_text:
        logger.info(
            f"[LIST SKILLS] Targeted agent or additional prompt present for {channel_id}:{thread_ts}. "
            f"Passing to normal agent turn processing."
        )
        return None

    # Fast-path: /list_skills with no agent and no additional text -> system model response
    from skills import skill_registry

    skills_directory_text = skill_registry.format_skills_directory_markdown(
        channel_id=channel_id, thread_ts=thread_ts
    )

    system_model = get_system_agent_model("system_agent", fallback="mistral/mistral-small-latest")
    formatted_msg = skills_directory_text

    try:
        sys_prompt = (
            "You are the Panel System. Present the following application skills and authorized agents directory "
            "clearly, neatly, and accurately to the user in Slack markdown format. Preserve the list of skills and "
            "the authorized agents for each skill without omitting or inventing any."
        )
        user_msg = (
            f"Here is the current platform skills directory:\n\n{skills_directory_text}\n\n"
            f"Please present this directory to the user."
        )
        llm_resp = await execute_llm_with_retry(
            model=system_model,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.2,
            max_tokens=1000,
        )
        content = (
            llm_resp.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
        if content:
            formatted_msg = content
    except Exception as e:
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
