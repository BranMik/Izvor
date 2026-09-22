import re
from typing import Any


def markdown_to_slack(text: str) -> str:
    # Bold: **tekst** -> *tekst*
    text = re.sub(r"\*\*(.*?)\*\*", r"*\1*", text)
    # Headings: ### Naslov -> *Naslov*
    text = re.sub(r"^#{1,6}\s*(.+)$", r"*\1*", text, flags=re.MULTILINE)
    # Links: [label](url) -> <url|label>
    text = re.sub(r"\[(.*?)\]\((.*?)\)", r"<\2|\1>", text)
    return text


def build_llm_messages(
    system_prompt: str,
    effective_history: list[dict[str, Any]],
    prompt: str,
    is_bot_trigger: bool,
    agent_key: str,
    agent_cfg: dict[str, Any],
    file_context: dict[str, Any] | None = None,
    remaining_agents: list[str] | None = None,
    force_termination: bool = False,
    next_designated_agent: str | None = None,
    thread_language: str = "English",
    required_skills: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Constructs a normalized, provider-safe message sequence for LiteLLM.
    Guarantees:
    1. System prompt is at index 0 (100% static for robust Anthropic prompt caching).
    2. Dynamic orchestration context (available agents list / termination constraints / locked language)
       is appended directly to the final user prompt (benefiting from LLM recency attention).
    3. The conversational sequence starts with a 'user' turn (handles trimmed history).
    4. The conversational sequence strictly ends with a 'user' turn (never 'assistant'),
       which is strictly required by Anthropic (Claude), Mistral, and Google Gemini APIs.
    5. Injects on-demand attached file content if present and if the current agent is authorized.
    6. Injects explicit permission notices if an agent lacks authorization or required skills.
    """
    conversation: list[dict[str, Any]] = []

    # Check authorization and skill requirements for attached file
    file_block = ""
    required_skill = file_context.get("required_skill") if file_context else None
    if file_context and not required_skill:
        try:
            from skills import skill_registry

            file_skill = skill_registry.get_file_handler_skill()
            required_skill = file_skill.name if file_skill else None
        except (ImportError, AttributeError):
            required_skill = None

    if file_context:
        authorized_agents = file_context.get("authorized_agents")
        file_name = file_context.get("file_name", "unknown")
        content = file_context.get("content", "")
        size = file_context.get("size", 0)

        is_agent_authorized = authorized_agents is None or agent_key in authorized_agents
        agent_skills = agent_cfg.get("skills")
        has_skill = True
        if required_skill and agent_skills is not None:
            has_skill = required_skill in agent_skills

        if not is_agent_authorized:
            file_block = (
                f"\n\n--- [FILE ACCESS RESTRICTED: NOT AUTHORIZED] ---\n"
                f"The file '{file_name}' was provided in this thread, but you ({agent_key}) are not in the authorized agents list to access it.\n"
                f"You must explicitly inform the user that you do not have permission / authorization to access or read this file. "
                f"Do NOT report that the file was not found.\n"
                f"--- [END OF NOTICE] ---"
            )
        elif not has_skill:
            file_block = (
                f"\n\n--- [FILE ACCESS RESTRICTED: MISSING SKILL] ---\n"
                f"The file '{file_name}' is available, but you ({agent_key}) do not have the required '{required_skill}' skill assigned to access or read files.\n"
                f"You must explicitly state to the user that you do not have permission / the required '{required_skill}' skill to access or read this file. "
                f"Do NOT report that the file was not found.\n"
                f"--- [END OF NOTICE] ---"
            )
        else:
            file_block = (
                f"\n\n--- [ATTACHED SLACK FILE: {file_name} ({size} bytes)] ---\n"
                f"{content}\n"
                f"--- [END OF ATTACHED FILE] ---"
            )

    # Check for any required or inferred skills that this agent lacks
    skill_notice_block = ""
    if required_skills:
        agent_skills = agent_cfg.get("skills")
        if agent_skills is not None:
            missing = [s for s in required_skills if s not in agent_skills]
            if file_block and required_skill and required_skill in missing:
                missing = [s for s in missing if s != required_skill]
            if missing:
                missing_str = ", ".join(f"'{s}'" for s in missing)
                skill_notice_block = (
                    f"\n\n--- [PERMISSION RESTRICTED: MISSING REQUIRED SKILL(S)] ---\n"
                    f"The requested task requires or infers the following skill(s): {missing_str}, "
                    f"but you ({agent_key}) do not have permission or this skill assigned.\n"
                    f"You must explicitly state to the user that you do not have permission / the required skill(s) ({missing_str}) "
                    f"to perform this action. Do NOT report that the file or resource was not found.\n"
                    f"--- [END OF NOTICE] ---"
                )

    # Dynamic orchestration directive
    orchestration_block = ""
    if force_termination:
        orchestration_block = (
            "\n\nCRITICAL CONSTRAINT: You are the last agent requested to speak in this turn. "
            "Do NOT nominate any other agent to speak next. Do NOT include any bracketed agent key (like [Agent_Name]) "
            "at the end of your response. Simply answer the question/summarize and finish your response."
        )
    elif next_designated_agent:
        orchestration_block = (
            f"\n\nDESIGNATED NEXT AGENT: At the very end of your response, nominate [{next_designated_agent}] as the next agent to speak."
        )
    elif remaining_agents:
        orchestration_block = (
            f"\n\nAVAILABLE AGENTS WHO HAVE NOT SPOKEN YET (nominate the next agent at the very end in [AGENT_KEY] format):\n"
            f"{', '.join(remaining_agents)}"
        )

    lang_directive = ""
    if thread_language:
        lang_directive = (
            f"CRITICAL: You MUST write your entire response strictly in {thread_language}, "
            "including all section labels, headings, and bullet points."
        )

    if not is_bot_trigger:
        if effective_history:
            conversation.extend(effective_history)
        lang_part = f"\n\n{lang_directive}" if lang_directive else ""
        user_prompt = f"{prompt}{lang_part}{file_block}{skill_notice_block}{orchestration_block}"
        conversation.append({"role": "user", "content": user_prompt})
    else:
        if effective_history:
            conversation.extend(effective_history)
            persona_name = agent_cfg.get("username", agent_key)
            lang_part = f" {lang_directive}" if lang_directive else ""
            instruction = (
                f"You have been called to speak as {persona_name}. "
                "Please evaluate the discussion above and provide your response in accordance with your role and instructions."
                f"{lang_part}"
            )
            instruction = f"{instruction}{file_block}{skill_notice_block}{orchestration_block}"
            conversation.append(
                {
                    "role": "user",
                    "content": instruction,
                }
            )
        else:
            lang_part = f"\n\n{lang_directive}" if lang_directive else ""
            user_prompt = f"{prompt}{lang_part}{file_block}{skill_notice_block}{orchestration_block}"
            conversation.append({"role": "user", "content": user_prompt})

    # Ensure the first conversational message has role 'user' (for strict APIs like Anthropic/Gemini)
    if conversation and conversation[0]["role"] != "user":
        conversation.insert(
            0,
            {
                "role": "user",
                "content": "Context from previous discussion in thread:",
            },
        )

    # Ensure the last conversational message has role 'user'
    if conversation and conversation[-1]["role"] != "user":
        persona_name = agent_cfg.get("username", agent_key)
        instruction = (
            f"You have been called to speak as {persona_name}. "
            "Please evaluate the discussion above and provide your response in accordance with your role and instructions. "
            f"{lang_directive}"
        )
        instruction = f"{instruction}{file_block}{orchestration_block}"
        conversation.append(
            {
                "role": "user",
                "content": instruction,
            }
        )

    system_item = {"role": "system", "content": system_prompt}
    return [system_item] + conversation

