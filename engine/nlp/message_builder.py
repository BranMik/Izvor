import logging
import re
from typing import Any

from prompts.builder import render_prompt_template

logger = logging.getLogger(__name__)


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


def _select_single_authorized_username(
    authorized_agents: set[str] | list[str] | None,
    *,
    channel_id: str | None = None,
    thread_ts: str | None = None,
    exclude_agent_key: str | None = None,
) -> str | None:
    """Selects 1 recommended authorized agent's username from candidate agent keys."""
    if not authorized_agents:
        return None
    try:
        from core.registry import get_skill_registry

        reg = get_skill_registry()
        if hasattr(reg, "recommend_agent_from_keys"):
            return reg.recommend_agent_from_keys(
                authorized_agents,
                channel_id=channel_id,
                thread_ts=thread_ts,
                exclude_agent_key=exclude_agent_key,
            )
    except (ImportError, AttributeError, KeyError, RuntimeError, TypeError, ValueError) as exc:
        logger.debug(f"[MESSAGE BUILDER] SkillRegistry recommendation fallback: {exc}")
    try:
        from skills.recommender import select_recommended_agent_from_keys

        return select_recommended_agent_from_keys(
            authorized_agents,
            channel_id=channel_id,
            thread_ts=thread_ts,
            exclude_agent_key=exclude_agent_key,
        )
    except (ImportError, AttributeError, KeyError, RuntimeError, TypeError, ValueError) as exc:
        logger.debug(f"[MESSAGE BUILDER] Recommender fallback unavailable: {exc}")
        return None


def build_skill_resolution_hint(
    skill_names: list[str] | str | None,
    channel_id: str | None = None,
    thread_ts: str | None = None,
    exclude_agent_key: str | None = None,
) -> str:
    """Helper to query SkillRegistry for a 1-line resolution hint recommending 1 agent by username and /grant_skill."""
    if not skill_names:
        return "Grant access via '/grant_skill <skill> <agent>' or '/grant_skill_for_all <skill>'."
    try:
        from core.registry import get_skill_registry

        reg = get_skill_registry()
        if hasattr(reg, "get_skill_resolution_hint"):
            return reg.get_skill_resolution_hint(
                skill_names,
                channel_id=channel_id,
                thread_ts=thread_ts,
                exclude_agent_key=exclude_agent_key,
            )
    except (ImportError, AttributeError, KeyError, RuntimeError, TypeError, ValueError) as exc:
        logger.debug(f"[MESSAGE BUILDER] Skill resolution hint fallback: {exc}")
    s_str = skill_names if isinstance(skill_names, str) else ", ".join(str(s) for s in skill_names)
    return f"Grant access via '/grant_skill {s_str} <agent>' or '/grant_skill_for_all {s_str}'."


def has_prior_verified_skill_output(
    shared_round_evidence: str | None,
    effective_history: list[dict[str, Any]] | None,
) -> bool:
    """Returns True if an authorized speaker already produced verified skill output in this round."""
    return bool(
        (shared_round_evidence and shared_round_evidence.strip())
        or any(
            isinstance(m, dict)
            and m.get("role") == "assistant"
            and "Skills used:" in str(m.get("content", ""))
            for m in (effective_history or [])
        )
    )


def build_deterministic_missing_skill_notice(
    *,
    missing_skills: list[str],
    file_name: str | None = None,
    authorized_agents: set[str] | list[str] | None = None,
    channel_id: str | None = None,
    thread_ts: str | None = None,
    agent_key: str | None = None,
) -> str:
    """Builds a concise deterministic missing-skill / file-access notice with a 1-line resolution hint."""
    if authorized_agents is not None and not missing_skills:
        target_label = f"'{file_name}'" if file_name else "this file"
        rec_username = _select_single_authorized_username(
            authorized_agents,
            channel_id=channel_id,
            thread_ts=thread_ts,
            exclude_agent_key=agent_key,
        )
        if rec_username:
            return (
                f"I do not have authorization to access {target_label}. "
                f"Recommended agent: {rec_username} — or grant access via '/grant_skill'."
            )
        return (
            f"I do not have authorization to access {target_label}. "
            f"Grant access via '/grant_skill' or tag an authorized agent."
        )

    skills_list = [str(s) for s in missing_skills if s]
    skills_fmt = ", ".join(f"'{s}'" for s in skills_list) if skills_list else "the required skill"
    hint = build_skill_resolution_hint(
        skills_list,
        channel_id=channel_id,
        thread_ts=thread_ts,
        exclude_agent_key=agent_key,
    )
    if file_name:
        return f"I lack the required {skills_fmt} skill to read '{file_name}'. {hint}"
    return f"I lack the required {skills_fmt} skill to perform this task. {hint}"


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
    max_words: int | None = None,
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
    notice_word_cap = min(50, max_words) if (max_words is not None and max_words > 0) else 50

    prior_verified = has_prior_verified_skill_output(shared_round_evidence, effective_history)
    downstream_file_clause = render_prompt_template(
        "turn_directives",
        "downstream_file_clause_with_prior" if prior_verified else "downstream_file_clause_no_prior",
        notice_word_cap=notice_word_cap,
    )
    downstream_skill_clause = render_prompt_template(
        "turn_directives",
        "downstream_skill_clause_with_prior" if prior_verified else "downstream_skill_clause_no_prior",
        notice_word_cap=notice_word_cap,
    )

    if file_context:
        authorized_agents = file_context.get("authorized_agents")
        file_name = file_context.get("file_name", "unknown")
        content = file_context.get("content", "")
        size = file_context.get("size", 0)

        is_agent_authorized = authorized_agents is None or agent_key in authorized_agents
        agent_skills = agent_cfg.get("skills") or []
        has_skill = (required_skill in agent_skills) if required_skill else True

        if not is_agent_authorized:
            rec_username = _select_single_authorized_username(
                authorized_agents,
                channel_id=channel_id,
                thread_ts=thread_ts,
                exclude_agent_key=agent_key,
            )
            hint = (
                f"Recommended agent: {rec_username} — or grant access via '/grant_skill'."
                if rec_username
                else "Grant access via '/grant_skill'."
            )
            file_block = render_prompt_template(
                "turn_directives",
                "file_access_not_authorized_notice",
                file_name=file_name,
                agent_display=agent_display,
                notice_word_cap=notice_word_cap,
                hint=hint,
                downstream_file_clause=downstream_file_clause,
            )
        elif not has_skill:
            hint = build_skill_resolution_hint(
                required_skill,
                channel_id=channel_id,
                thread_ts=thread_ts,
                exclude_agent_key=agent_key,
            )
            file_block = render_prompt_template(
                "turn_directives",
                "file_access_missing_skill_notice",
                file_name=file_name,
                agent_display=agent_display,
                required_skill=required_skill,
                notice_word_cap=notice_word_cap,
                hint=hint,
                downstream_file_clause=downstream_file_clause,
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
            hint = build_skill_resolution_hint(
                missing,
                channel_id=channel_id,
                thread_ts=thread_ts,
                exclude_agent_key=agent_key,
            )
            skill_notice_block = render_prompt_template(
                "turn_directives",
                "skill_missing_notice",
                missing_str=missing_str,
                agent_display=agent_display,
                notice_word_cap=notice_word_cap,
                hint=hint,
                downstream_skill_clause=downstream_skill_clause,
            )

    # Dynamic orchestration directive
    orchestration_block = ""
    if force_termination:
        orchestration_block = render_prompt_template(
            "turn_directives", "orchestration_force_termination"
        )
    elif next_designated_agent:
        orchestration_block = render_prompt_template(
            "turn_directives",
            "orchestration_designated_next",
            next_designated_agent=next_designated_agent,
        )
    elif remaining_agents:
        formatted_agents = ", ".join(f"/[{a}]" for a in remaining_agents)
        orchestration_block = render_prompt_template(
            "turn_directives",
            "orchestration_available_agents",
            formatted_agents=formatted_agents,
        )

    lang_directive = ""
    if thread_language:
        lang_directive = render_prompt_template(
            "turn_directives",
            "language_lock_directive",
            thread_language=thread_language,
        )

    has_permission_restriction = bool(
        (file_block and ("RESTRICTED" in file_block or "MISSING SKILL" in file_block))
        or skill_notice_block
    )
    has_skill_context = bool(file_block or required_skills)
    anti_echo_directive = ""
    if has_permission_restriction:
        anti_echo_directive = render_prompt_template(
            "turn_directives",
            "anti_echo_restricted",
            notice_word_cap=notice_word_cap,
        )
    elif has_skill_context:
        anti_echo_directive = render_prompt_template(
            "turn_directives", "anti_echo_authorized"
        )

    clean_prompt = prompt.strip() if prompt else ""
    prompt_anchor = f"\n\nPRIMARY USER REQUEST TO ADDRESS:\n{clean_prompt}" if clean_prompt else ""

    def _append_memory_prefix() -> None:
        if memory_prefix:
            conversation.append(
                {
                    "role": "user",
                    "content": render_prompt_template(
                        "turn_directives",
                        "memory_context_user_prefix",
                        memory_prefix=memory_prefix,
                    ),
                }
            )
            conversation.append(
                {
                    "role": "assistant",
                    "content": render_prompt_template(
                        "turn_directives", "memory_context_assistant_ack"
                    ),
                }
            )

    def _build_bot_trigger_instruction() -> str:
        persona_name = agent_cfg.get("displayname", agent_cfg.get("username", agent_key))
        lang_part = f" {lang_directive}" if lang_directive else ""
        instruction = render_prompt_template(
            "turn_directives",
            "bot_trigger_instruction",
            persona_name=persona_name,
            lang_part=lang_part,
        )
        if file_block and "<untrusted_file_content>" in file_block:
            return f"{file_block}\n\n{instruction}{prompt_anchor}{skill_notice_block}{anti_echo_directive}{orchestration_block}"
        return f"{instruction}{prompt_anchor}{file_block}{skill_notice_block}{anti_echo_directive}{orchestration_block}"

    if not is_bot_trigger:
        if effective_history:
            _append_memory_prefix()
            conversation.extend(effective_history)
        lang_part = f"\n\n{lang_directive}" if lang_directive else ""
        mem_part = f"{memory_prefix}\n\n" if memory_prefix and not effective_history else ""
        long_file_anchor = prompt_anchor if (file_block and "<untrusted_file_content>" in file_block and len(file_block) > 500) else ""
        user_prompt = f"{mem_part}{prompt}{lang_part}{file_block}{long_file_anchor}{skill_notice_block}{anti_echo_directive}{orchestration_block}"
        conversation.append({"role": "user", "content": user_prompt})
    else:
        if effective_history:
            _append_memory_prefix()
            conversation.extend(effective_history)
            conversation.append({"role": "user", "content": _build_bot_trigger_instruction()})
        else:
            lang_part = f"\n\n{lang_directive}" if lang_directive else ""
            mem_part = f"{memory_prefix}\n\n" if memory_prefix else ""
            long_file_anchor = prompt_anchor if (file_block and "<untrusted_file_content>" in file_block and len(file_block) > 500) else ""
            user_prompt = f"{mem_part}{prompt}{lang_part}{file_block}{long_file_anchor}{skill_notice_block}{anti_echo_directive}{orchestration_block}"
            conversation.append({"role": "user", "content": user_prompt})

    # Ensure the first conversational message has role 'user' (for strict APIs like Anthropic/Gemini)
    if conversation and conversation[0]["role"] != "user":
        conversation.insert(
            0,
            {
                "role": "user",
                "content": render_prompt_template(
                    "turn_directives", "fallback_user_context_header"
                ),
            },
        )

    # Ensure the last conversational message has role 'user'
    if conversation and conversation[-1]["role"] != "user":
        conversation.append({"role": "user", "content": _build_bot_trigger_instruction()})

    conversation = _normalize_conversation_messages(conversation)
    system_item = {"role": "system", "content": system_prompt}
    return [system_item] + conversation


def _normalize_conversation_messages(conversation: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Normalizes conversation history for strict cross-provider compatibility:
    - Converts any mid-conversation `system` role messages into `user` notifications.
    - Strips non-protocol metadata (e.g. Slack `username`) while keeping tool-call fields.
    - Coalesces consecutive same-role messages (`user`+`user` or `assistant`+`assistant` without `tool_calls`).
    - Ensures leading and trailing conversation items are `user` messages.
    """
    normalized: list[dict[str, Any]] = []
    for raw_msg in conversation:
        if not isinstance(raw_msg, dict):
            continue
        role = str(raw_msg.get("role") or "user")
        content = raw_msg.get("content")
        if role == "system":
            role = "user"
            content = f"[System Notification]: {content or ''}"

        item: dict[str, Any] = {"role": role, "content": content if content is not None else ""}
        for optional_key in ("tool_calls", "tool_call_id", "name"):
            if optional_key in raw_msg and raw_msg[optional_key] is not None:
                item[optional_key] = raw_msg[optional_key]

        if (
            normalized
            and normalized[-1]["role"] == role
            and role in ("user", "assistant")
            and not normalized[-1].get("tool_calls")
            and not item.get("tool_calls")
        ):
            prev_text = str(normalized[-1].get("content") or "")
            curr_text = str(item.get("content") or "")
            if prev_text and curr_text:
                normalized[-1]["content"] = f"{prev_text}\n\n{curr_text}"
            elif curr_text:
                normalized[-1]["content"] = curr_text
        else:
            normalized.append(item)

    if normalized and normalized[0]["role"] != "user":
        normalized.insert(
            0,
            {
                "role": "user",
                "content": render_prompt_template(
                    "turn_directives", "fallback_user_context_header"
                ),
            },
        )
    if normalized and normalized[-1]["role"] != "user":
        normalized.append(
            {
                "role": "user",
                "content": render_prompt_template(
                    "turn_directives", "fallback_user_tail_instruction"
                ),
            }
        )
    return normalized

