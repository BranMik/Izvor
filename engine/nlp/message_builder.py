import re
from typing import Any


def markdown_to_slack(text: str) -> str:
    if not text or not isinstance(text, str):
        return text

    # Stash fenced code blocks (```...```) to protect syntax inside them
    fenced_blocks: list[str] = []

    def _stash_fenced(match: re.Match[str]) -> str:
        idx = len(fenced_blocks)
        fenced_blocks.append(match.group(0))
        return f"\x00FENCED_BLOCK_{idx}\x00"

    text = re.sub(r"```[\s\S]*?```", _stash_fenced, text)

    # Stash inline code spans (`...`) to protect syntax inside them
    inline_spans: list[str] = []

    def _stash_inline(match: re.Match[str]) -> str:
        idx = len(inline_spans)
        inline_spans.append(match.group(0))
        return f"\x00INLINE_SPAN_{idx}\x00"

    text = re.sub(r"`[^`\n]+`", _stash_inline, text)

    # Headings: ### Title -> *Title* (strip surrounding asterisks so '### **Title**' -> '*Title*', not '**Title**')
    text = re.sub(r"^#{1,6}\s*\**\s*(.+?)\s*\**\s*$", r"*\1*", text, flags=re.MULTILINE)
    # Horizontal rules: a line of 3+ '-', '*' or '_' -> Slack divider line
    text = re.sub(r"^\s*([-*_])(\s*\1){2,}\s*$", "──────────", text, flags=re.MULTILINE)
    # Bold: **text** -> *text*
    text = re.sub(r"\*\*(.*?)\*\*", r"*\1*", text)
    # Links: [label](url) -> <url|label>
    text = re.sub(r"\[(.*?)\]\((.*?)\)", r"<\2|\1>", text)

    # Restore inline code spans
    for i, span in enumerate(inline_spans):
        text = text.replace(f"\x00INLINE_SPAN_{i}\x00", span)

    # Restore fenced code blocks
    for i, block in enumerate(fenced_blocks):
        text = text.replace(f"\x00FENCED_BLOCK_{i}\x00", block)

    return text


def build_skill_resolution_hint(
    skill_names: list[str] | str | None,
    channel_id: str | None = None,
    thread_ts: str | None = None,
) -> str:
    """Helper to query SkillRegistry for a 1-line resolution hint naming authorized agents and /grant_skill."""
    if not skill_names:
        return "Grant access via '/grant_skill <skill> <agent>' or '/grant_skill_for_all <skill>'."
    try:
        from core.registry import get_skill_registry

        reg = get_skill_registry()
        if hasattr(reg, "get_skill_resolution_hint"):
            return reg.get_skill_resolution_hint(skill_names, channel_id=channel_id, thread_ts=thread_ts)
    except Exception:
        pass
    s_str = skill_names if isinstance(skill_names, str) else ", ".join(str(s) for s in skill_names)
    return f"Grant access via '/grant_skill {s_str} <agent>' or '/grant_skill_for_all {s_str}'."


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
    episodic_summary: str | None = None,
    shared_round_evidence: str | None = None,
    channel_id: str | None = None,
    thread_ts: str | None = None,
) -> list[dict[str, Any]]:
    """
    Constructs a normalized, provider-safe message sequence for LiteLLM.
    Guarantees:
    1. System prompt is at index 0 (clean static persona/system instructions).
    2. Dynamic orchestration context (available agents list / termination constraints / locked language)
       is appended directly to the final user prompt (benefiting from LLM recency attention).
    3. The conversational sequence starts with a 'user' turn (handles trimmed history).
    4. The conversational sequence strictly ends with a 'user' turn (never 'assistant'),
       which is strictly required by Anthropic (Claude), Mistral, and Google Gemini APIs.
    5. Injects on-demand attached file content if present and if the current agent is authorized.
    6. Injects explicit permission notices if an agent lacks authorization or required skills.
    7. Injects Tier-1 episodic thread summary and Tier-2 shared round evidence before the sliding window turns.
    """
    conversation: list[dict[str, Any]] = []

    # Format Tier-1 episodic summary and Tier-2 shared round evidence
    memory_blocks: list[str] = []
    if episodic_summary and episodic_summary.strip():
        memory_blocks.append(
            f"<episodic_thread_summary>\n{episodic_summary.strip()}\n</episodic_thread_summary>"
        )
    if shared_round_evidence and shared_round_evidence.strip():
        memory_blocks.append(
            f"<shared_round_evidence>\n{shared_round_evidence.strip()}\n</shared_round_evidence>"
        )
    memory_prefix = "\n\n".join(memory_blocks)

    # Check authorization and skill requirements for attached file
    file_block = ""
    required_skill = file_context.get("required_skill") if file_context else None
    if file_context and not required_skill:
        try:
            from core.registry import get_skill_registry
            skill_registry = get_skill_registry()

            file_skill = skill_registry.get_file_handler_skill()
            required_skill = file_skill.name if file_skill else None
        except (ImportError, AttributeError):
            required_skill = None

    agent_display = agent_cfg.get("username", agent_key)

    if file_context:
        authorized_agents = file_context.get("authorized_agents")
        file_name = file_context.get("file_name", "unknown")
        content = file_context.get("content", "")
        size = file_context.get("size", 0)

        is_agent_authorized = authorized_agents is None or agent_key in authorized_agents
        agent_skills = agent_cfg.get("skills") or []
        has_skill = (required_skill in agent_skills) if required_skill else True

        if not is_agent_authorized:
            auth_str = ", ".join(sorted(authorized_agents)) if authorized_agents else "none"
            file_block = (
                f"\n\n--- [FILE ACCESS RESTRICTED: NOT AUTHORIZED] ---\n"
                f"The file '{file_name}' was provided in this thread, but you ({agent_display}) are not in the authorized agents list to access it (Authorized: {auth_str}).\n"
                f"INSTRUCTION: State in at most 50 words that you do not have permission / authorization to access or read this file. "
                f"Include a 1-line hint naming an authorized agent or '/grant_skill'. "
                f"Do NOT report that the file was not found. Do NOT reflect on platform skills architecture. "
                f"If the prompt contains other questions or tasks not requiring this file, proceed normally to evaluate and answer those after the <= 50-word notice; "
                f"if the prompt only asks about the file, output ONLY the <= 50-word notice and stop.\n"
                f"--- [END OF NOTICE] ---"
            )
        elif not has_skill:
            hint = build_skill_resolution_hint(required_skill, channel_id=channel_id, thread_ts=thread_ts)
            file_block = (
                f"\n\n--- [FILE ACCESS RESTRICTED: MISSING SKILL] ---\n"
                f"The file '{file_name}' is available, but you ({agent_display}) do not have the required '{required_skill}' skill assigned to access or read files.\n"
                f"INSTRUCTION: State in at most 50 words that you do not have permission / the required '{required_skill}' skill to access or read this file. "
                f"Include this 1-line resolution hint: {hint}\n"
                f"Do NOT report that the file was not found. Do NOT reflect on platform skills architecture. "
                f"If the prompt contains other questions or tasks not requiring this file, proceed normally to evaluate and answer those after the <= 50-word notice; "
                f"if the prompt only asks about the file, output ONLY the <= 50-word notice and stop.\n"
                f"--- [END OF NOTICE] ---"
            )
        else:
            from core.security_utils import wrap_untrusted_content

            wrapped_content = wrap_untrusted_content(content)
            file_block = (
                f"\n\n--- [ATTACHED SLACK FILE: {file_name} ({size} bytes)] ---\n"
                f"{wrapped_content}\n"
                f"--- [END OF ATTACHED FILE] ---"
            )

    # Check for any required or inferred skills that this agent lacks
    skill_notice_block = ""
    if required_skills:
        agent_skills = agent_cfg.get("skills") or []
        missing = [s for s in required_skills if s not in agent_skills]
        if file_block and required_skill and required_skill in missing:
            missing = [s for s in missing if s != required_skill]
        if missing:
            missing_str = ", ".join(f"'{s}'" for s in missing)
            hint = build_skill_resolution_hint(missing, channel_id=channel_id, thread_ts=thread_ts)
            skill_notice_block = (
                f"\n\n--- [PERMISSION RESTRICTED: MISSING REQUIRED SKILL(S)] ---\n"
                f"The requested task requires or infers the following skill(s): {missing_str}, "
                f"but you ({agent_display}) do not have permission or this skill assigned.\n"
                f"INSTRUCTION: State in at most 50 words that you do not have permission / the required skill(s) ({missing_str}) to perform this action. "
                f"Include this 1-line resolution hint: {hint}\n"
                f"Do NOT report that the file or resource was not found. Do NOT reflect on platform skills architecture. "
                f"If the prompt contains other questions or tasks not requiring these skills, proceed normally to evaluate and answer those after the <= 50-word notice; "
                f"if the prompt only asks for this skill-based action, output ONLY the <= 50-word notice and stop.\n"
                f"--- [END OF NOTICE] ---"
            )

    # Dynamic orchestration directive
    orchestration_block = ""
    if force_termination:
        orchestration_block = (
            "\n\nCRITICAL CONSTRAINT: You are the last agent requested to speak in this turn. "
            "Do NOT nominate any other agent to speak next. Do NOT include any bracketed agent tag (like /[Agent_Name]) "
            "at the end of your response. Simply answer the question/summarize and finish your response."
        )
    elif next_designated_agent:
        orchestration_block = (
            f"\n\nDESIGNATED NEXT AGENT: At the very end of your response, nominate /[{next_designated_agent}] as the next agent to speak."
        )
    elif remaining_agents:
        formatted_agents = ", ".join(f"/[{a}]" for a in remaining_agents)
        orchestration_block = (
            f"\n\nAVAILABLE AGENTS WHO HAVE NOT SPOKEN YET (nominate the next agent at the very end in /[AGENT_KEY] format):\n"
            f"{formatted_agents}"
        )

    lang_directive = ""
    if thread_language:
        lang_directive = (
            f"CRITICAL: You MUST write your entire response strictly in {thread_language}, "
            "including all section labels, headings, and bullet points."
        )

    has_permission_restriction = bool(
        (file_block and ("RESTRICTED" in file_block or "MISSING SKILL" in file_block))
        or skill_notice_block
    )
    anti_echo_directive = ""
    if has_permission_restriction:
        anti_echo_directive = (
            "\n\nANTI-ECHO RULE: Do NOT debate, critique, or expand on previous agents' missing-skill notices or skills architecture. "
            "Keep your own missing-skill notice + 1-line hint strictly <= 50 words, then either evaluate any remaining non-skill parts "
            "of the user prompt normally, or—if the prompt has no other assessable parts—stop immediately and nominate the next agent."
        )

    if not is_bot_trigger:
        if effective_history:
            if memory_prefix:
                conversation.append(
                    {
                        "role": "user",
                        "content": f"Context from earlier in this discussion thread:\n\n{memory_prefix}",
                    }
                )
                conversation.append(
                    {
                        "role": "assistant",
                        "content": "Understood. I will take this earlier thread summary and verified evidence into account alongside the conversation history.",
                    }
                )
            conversation.extend(effective_history)
        lang_part = f"\n\n{lang_directive}" if lang_directive else ""
        mem_part = f"{memory_prefix}\n\n" if memory_prefix and not effective_history else ""
        user_prompt = f"{mem_part}{prompt}{lang_part}{file_block}{skill_notice_block}{orchestration_block}"
        conversation.append({"role": "user", "content": user_prompt})
    else:
        if effective_history:
            if memory_prefix:
                conversation.append(
                    {
                        "role": "user",
                        "content": f"Context from earlier in this discussion thread:\n\n{memory_prefix}",
                    }
                )
                conversation.append(
                    {
                        "role": "assistant",
                        "content": "Understood. I will take this earlier thread summary and verified evidence into account alongside the conversation history.",
                    }
                )
            conversation.extend(effective_history)
            persona_name = agent_cfg.get("displayname", agent_cfg.get("username", agent_key))
            lang_part = f" {lang_directive}" if lang_directive else ""
            instruction = (
                f"You have been called to speak as {persona_name}. "
                "Please evaluate the discussion above and provide your response in accordance with your role and instructions."
                f"{lang_part}"
            )
            instruction = f"{instruction}{file_block}{skill_notice_block}{anti_echo_directive}{orchestration_block}"
            conversation.append(
                {
                    "role": "user",
                    "content": instruction,
                }
            )
        else:
            lang_part = f"\n\n{lang_directive}" if lang_directive else ""
            mem_part = f"{memory_prefix}\n\n" if memory_prefix else ""
            user_prompt = f"{mem_part}{prompt}{lang_part}{file_block}{skill_notice_block}{anti_echo_directive}{orchestration_block}"
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
        persona_name = agent_cfg.get("displayname", agent_cfg.get("username", agent_key))
        instruction = (
            f"You have been called to speak as {persona_name}. "
            "Please evaluate the discussion above and provide your response in accordance with your role and instructions. "
            f"{lang_directive}"
        )
        instruction = f"{instruction}{file_block}{skill_notice_block}{anti_echo_directive}{orchestration_block}"
        conversation.append(
            {
                "role": "user",
                "content": instruction,
            }
        )

    system_item = {"role": "system", "content": system_prompt}
    return [system_item] + conversation

