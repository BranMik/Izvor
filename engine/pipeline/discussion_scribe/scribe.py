import asyncio
import logging
import re
import time
from typing import Any

import litellm

from core.config import get_orchestrator_fast_model, get_system_agent_model
from core.domain import DiscussionBrief, DiscussionConclusion, Turn

from prompts.builder import load_system_prompt

logger = logging.getLogger(__name__)

_DEFAULT_SCRIBE_PROMPT_FALLBACK = """<system_agent_specification id="Discussion_Scribe_Live" role="Live Strategic Synthesis Scribe">
  <task_objective>
    Analyze the ongoing multi-agent discussion transcript and synthesize a concise, high-density live strategic brief in Slack that always fits comfortably within Slack message limits.
  </task_objective>

  <processing_rules>
    1. Language Invariant: Write ALL headers, section titles, status indicators, and bullet points strictly in the designated thread language. Never switch languages mid-note.
    2. Section Structure & Strict Word Limits (translate section titles into the designated thread language):
       - *Core Objective & Problem Statement* (max 35 words): 1–2 concise sentences stating the core problem and goal.
       - *Key Perspectives* (max 190 words total; max 30 words per bullet): 1 compact bullet per speaking agent ("• **[AgentName]**: core proposal and key rationale").
       - *Risks & Open Questions* (max 75 words): Concise bullets for critical risks or unresolved questions surfaced during the discussion.
    3. Skip Empty Sections (CRITICAL): NEVER write a section title with empty content or placeholder text (such as "None", "N/A", or "Nothing identified yet"). If there is nothing substantive to write for a section, omit that section completely.
    4. Live Note Boundary (CRITICAL): Do NOT include consensus decisions, final conclusions, or action items in this live note (those belong exclusively in the separate Conclusion Note).
    5. Hard Total Ceiling: Keep the entire note strictly under 300 words (under 220 words while ongoing; under 50 words on the initial turn) unless overridden by an explicit user word-count directive.
    6. Strict Speaker Attribution: Attribute viewpoints and perspectives ONLY to agents who have an explicit "[AgentName]:" turn header in the transcript.
  </processing_rules>

  <output_contract>
    - Output ONLY the structured markdown live synthesis note containing the applicable non-empty translated sections above.
    - Zero introductory or closing meta-commentary.
  </output_contract>
</system_agent_specification>"""

_DEFAULT_CONCLUSION_PROMPT_FALLBACK = """<system_agent_specification id="Discussion_Scribe_Conclusion" role="Dialectical Conclusion Synthesizer">
  <task_objective>
    Analyze the completed multi-agent deliberation transcript and synthesize a compact, executive-level Conclusion Note capturing consensus decisions, divergent positions with trade-offs, and concrete next steps within Slack message limits.
  </task_objective>

  <processing_rules>
    1. Language Invariant: Write ALL headers, section titles, and bullet points strictly in the designated thread language. Never switch languages.
    2. Section Structure & Strict Word Limits (translate section titles into the designated thread language):
       - *Problem Statement* (max 25 words): Exactly 1 concise sentence stating the core question or objective addressed.
       - *Consensus Decisions* (max 85 words): Concise bullets stating decisions and propositions where participating agents reached broad agreement.
       - *Divergent Positions & Trade-offs* (max 140 words): Represent each opposing, minority, or alternative position as a single compact bullet combining attribution, rationale, and trade-offs:
         • **[Position / Recommendation]** ([Supporting Agents]): Core argument and evidence — *Trade-off / Counter:* key criticism or risk raised.
       - *Action Items & Next Steps* (max 70 words): Concrete, prioritized follow-up actions resulting from the deliberation.
    3. Skip Empty Sections (CRITICAL): NEVER write a section title with empty content or placeholder text (such as "None", "N/A", or "No opposing views"). If a section has nothing substantive to report (e.g., unanimous agreement with no divergent positions, or no action items), omit that section completely.
    4. Hard Total Ceiling: Keep the entire Conclusion Note strictly under 320 words unless overridden by an explicit user word-count directive.
    5. Dialectical Impartiality: Never suppress dissenting or minority viewpoints; represent minority arguments with the same analytical rigor as majority consensus.
    6. Strict Speaker Attribution: Attribute arguments, positions, and supporting agents ONLY to agents who have an explicit "[AgentName]:" turn header in the transcript. NEVER attribute viewpoints to agents who were merely nominated or mentioned at the end of another speaker's message but did not actually speak.
  </processing_rules>

  <output_contract>
    - Output ONLY the structured Conclusion Note in clean, scannable markdown containing the applicable non-empty translated sections.
    - Zero conversational filler or meta-commentary.
  </output_contract>
</system_agent_specification>"""

SCRIBE_DEFAULT_PROMPT = load_system_prompt("Discussion_Scribe_Live", fallback=_DEFAULT_SCRIBE_PROMPT_FALLBACK)
SCRIBE_CONCLUSION_PROMPT = load_system_prompt(
    "Discussion_Scribe_Conclusion", fallback=_DEFAULT_CONCLUSION_PROMPT_FALLBACK
)


def _clean_turn_content_for_scribe(content: str) -> str:
    """Iteratively strips token/fallback/skills footers and trailing nomination tags for clean scribe processing."""
    if not content:
        return ""
    cleaned = content.strip()
    trailing_patterns = (
        re.compile(r"\s*\{(?:[^{}]*:\s*)?\d+(?:/\d+)?\}\s*$"),
        re.compile(r"\s*⚠️\s*_?Fallback model used:\s*`[^`]+`\s*\(missing API key for\s*`[^`]+`\)_?\s*$"),
        re.compile(r"\s*Skills used:\s*(?:\[[^\]]+\](?:,\s*)?)+\s*$", re.IGNORECASE),
        re.compile(r"\s*/\s*\[\s*[^\]]+\s*\](?:\s+next)?[.!?]?\s*$", re.IGNORECASE),
    )
    while True:
        prev = cleaned
        for pattern in trailing_patterns:
            cleaned = pattern.sub("", cleaned).strip()
        if cleaned == prev:
            break
    return cleaned


_LEADING_SPEAKER_RE = re.compile(r"^\s*\[([^\[\]\r\n]+)\]:\s*")


def _extract_speaker_and_body(item: dict[str, Any] | Turn) -> tuple[str, str, str]:
    """Extracts (role, speaker_name, body_without_duplicate_prefix) from a Turn or history dict."""
    if isinstance(item, Turn):
        role = item.role
        raw_speaker = item.speaker
        raw_body = item.content or ""
    elif isinstance(item, dict):
        role = item.get("role", "user")
        raw_speaker = item.get("username") or ""
        raw_body = item.get("content") or item.get("text") or ""
    else:
        return "user", "", ""

    speaker_str = str(raw_speaker or "").strip()
    body = str(raw_body or "")
    extracted_name = ""

    while True:
        match = _LEADING_SPEAKER_RE.match(body)
        if not match:
            break
        prefix_name = match.group(1).strip()
        if not extracted_name:
            extracted_name = prefix_name
        body = body[match.end() :]

    if extracted_name and (not speaker_str or speaker_str.lower() in ("assistant", "bot")):
        speaker_str = extracted_name
    elif not speaker_str:
        speaker_str = role

    return role, speaker_str, body


_DEFAULT_SCRIBE_MODEL = "mistral/mistral-small-latest"
_DEFAULT_SCRIBE_GRACE_BUFFER = 50


def _get_scribe_grace_buffer() -> int:
    """Retrieves configurable word-limit grace buffer with a safe standalone default."""
    try:
        from orchestration.system_settings import get_word_limit_grace_buffer

        return max(0, int(get_word_limit_grace_buffer()))
    except Exception:  # noqa: BLE001
        return _DEFAULT_SCRIBE_GRACE_BUFFER


def _is_orchestrator_noise(speaker: str, raw_content: str) -> bool:
    """Returns True for Orchestrator system status banners that must not enter Scribe attribution."""
    if speaker.strip().lower() == "orchestrator":
        return True
    stripped = raw_content.strip()
    return stripped.lower().startswith("[orchestrator]:")


def _format_transcript(history: list[dict[str, Any]] | list[Turn]) -> tuple[str, str]:
    """Formats conversation turns into a scribe transcript and comma-separated speaker attribution list."""
    transcript_lines: list[str] = []
    participating_speakers: list[str] = []
    for item in history:
        if not isinstance(item, (Turn, dict)):
            continue
        raw_c = item.content if isinstance(item, Turn) else (item.get("content") or item.get("text") or "")
        role, speaker_str, body = _extract_speaker_and_body(item)
        if _is_orchestrator_noise(speaker_str, str(raw_c or "")):
            continue

        cleaned_c = _clean_turn_content_for_scribe(body)
        if not cleaned_c or cleaned_c.lower().startswith("[orchestrator]:"):
            continue
        transcript_lines.append(f"[{speaker_str}]: {cleaned_c}")
        if role == "assistant" and speaker_str and speaker_str not in participating_speakers:
            participating_speakers.append(speaker_str)

    transcript_text = "\n\n".join(transcript_lines)
    speakers_str = ", ".join(participating_speakers) if participating_speakers else "None"
    return transcript_text, speakers_str


def _build_length_instruction(
    scribe_max_words: int | None,
    scribe_min_words: int | None,
    noun: str,
) -> str:
    """Builds optional explicit word-count override directives for Scribe prompts."""
    directives: list[str] = []
    if scribe_max_words is not None:
        directives.append(
            f"CRITICAL LENGTH CONSTRAINT: The entire {noun} must strictly NOT exceed {scribe_max_words} words total. Be extremely concise."
        )
    if scribe_min_words is not None:
        directives.append(
            f"CRITICAL LENGTH CONSTRAINT: The entire {noun} must have a minimum length of {scribe_min_words} words. Elaborate sufficiently to meet this floor."
        )
    return f" {' '.join(directives)}" if directives else ""


def _apply_word_limit(
    text: str,
    max_words: int | None,
    default_max_words: int,
    label: str,
) -> str:
    """Enforces mechanical word ceiling while preserving markdown line breaks and bullet formatting."""
    if not text:
        return text
    try:
        effective_max = int(max_words) if max_words is not None else default_max_words
    except (ValueError, TypeError):
        effective_max = default_max_words

    cutoff = max(1, effective_max + _get_scribe_grace_buffer())
    word_spans = list(re.finditer(r"\S+", text))
    if len(word_spans) <= cutoff:
        return text

    logger.info(
        f"[SCRIBE MECHANICAL CUTOFF] {label} exceeded {cutoff} words ({len(word_spans)} words). "
        f"Mechanically truncated to {cutoff} words."
    )
    return text[: word_spans[cutoff - 1].end()].rstrip()


def _extract_token_counts(resp: Any) -> tuple[int, int]:
    """Extracts integer prompt and completion token counts from an LLM response."""
    usage = getattr(resp, "usage", None)
    if not usage:
        return 0, 0
    pt = getattr(usage, "prompt_tokens", 0) or 0
    ct = getattr(usage, "completion_tokens", 0) or 0
    return (
        int(pt) if isinstance(pt, (int, float)) else 0,
        int(ct) if isinstance(ct, (int, float)) else 0,
    )


def _extract_brief_fields(raw_text: str) -> dict[str, Any]:
    """Extracts structured domain fields from raw markdown brief."""
    objective = ""
    perspectives: list[str] = []
    consensus = ""
    risks: list[str] = []
    action_items: list[str] = []

    current_section = None
    lines = raw_text.splitlines()
    for line in lines:
        l_str = line.strip()
        lower = l_str.lower()
        is_star_header = (l_str.startswith("*") and l_str.endswith("*") and not l_str.startswith("**") and len(l_str) < 80)
        is_header = l_str.startswith(("#", "**", "Part", "part", "PART")) or is_star_header or (l_str.endswith(":") and len(l_str) < 60)
        if is_header:
            if any(k in lower for k in ("objective", "problem statement", "cilj", "problem", "svrha")):
                current_section = "objective"
                continue
            elif any(k in lower for k in ("consensus", "konsenzus", "sporazum", "slaganje")) and not any(k in lower for k in ("perspective", "perspektiv")):
                current_section = "consensus"
                continue
            elif any(k in lower for k in ("perspective", "perspektiv", "gledište", "stajališt")):
                current_section = "perspectives"
                continue
            elif any(k in lower for k in ("risk", "trade-off", "tradeoff", "open question", "rizik", "kompromis", "otvoreno pitanje")):
                current_section = "risks"
                continue
            elif any(k in lower for k in ("action item", "next step", "akcijsk", "sljedeći korak", "koraci")):
                current_section = "action_items"
                continue
            elif l_str.startswith("#"):
                current_section = None
                continue

        if not l_str:
            continue

        m = re.match(r"^(?:[-*•]|\d+\.)\s*(.*)", l_str)
        cleaned_bullet = m.group(1).strip() if m else l_str

        if current_section == "objective":
            objective += f" {l_str}"
        elif current_section == "perspectives":
            if m:
                perspectives.append(cleaned_bullet)
            elif "consensus" in lower or "konsenzus" in lower:
                consensus += f" {l_str}"
            else:
                perspectives.append(l_str)
        elif current_section == "consensus":
            consensus += f" {l_str}"
        elif current_section == "risks":
            if m:
                risks.append(cleaned_bullet)
            else:
                risks.append(l_str)
        elif current_section == "action_items":
            if m:
                action_items.append(cleaned_bullet)
            else:
                action_items.append(l_str)

    return {
        "objective": objective.strip(),
        "perspectives": perspectives,
        "consensus": consensus.strip(),
        "risks": risks,
        "action_items": action_items,
    }


def _extract_conclusion_fields(raw_text: str) -> dict[str, Any]:
    """Extracts structured domain fields from raw markdown conclusion."""
    problem_statement = ""
    consensus = ""
    opposing_conclusions: list[dict[str, Any]] = []
    action_items: list[str] = []

    current_section = None
    lines = raw_text.splitlines()
    for line in lines:
        l_str = line.strip()
        lower = l_str.lower()
        is_star_header = (l_str.startswith("*") and l_str.endswith("*") and not l_str.startswith("**") and len(l_str) < 80)
        is_header = l_str.startswith(("#", "**", "Part", "part", "PART")) or is_star_header or (l_str.endswith(":") and len(l_str) < 60)
        if is_header:
            if any(k in lower for k in ("problem statement", "problem", "cilj", "pitanje")):
                current_section = "problem"
                continue
            elif any(k in lower for k in ("consensus", "konsenzus", "zaključak", "slaganje")):
                current_section = "consensus"
                continue
            elif any(k in lower for k in ("opposing", "divergent", "suprotn", "različit", "drugačij", "trade-off", "tradeoff")):
                current_section = "opposing"
                continue
            elif any(k in lower for k in ("action item", "next step", "akcijsk", "sljedeći korak", "koraci")):
                current_section = "action_items"
                continue
            elif l_str.startswith("#"):
                current_section = None
                continue

        if not l_str:
            continue

        m = re.match(r"^(?:[-*•]|\d+\.)\s*(.*)", l_str)
        cleaned_bullet = m.group(1).strip() if m else l_str

        if current_section == "problem":
            problem_statement += f" {l_str}"
        elif current_section == "consensus":
            consensus += f" {l_str}"
        elif current_section == "opposing":
            if m:
                opposing_conclusions.append({"text": cleaned_bullet})
            else:
                opposing_conclusions.append({"text": l_str})
        elif current_section == "action_items":
            if m:
                action_items.append(cleaned_bullet)
            else:
                action_items.append(l_str)

    return {
        "problem_statement": problem_statement.strip(),
        "consensus": consensus.strip(),
        "opposing_conclusions": opposing_conclusions,
        "action_items": action_items,
    }


class DiscussionScribe:
    """
    Platform-agnostic discussion scribe service that analyzes conversations
    and produces structured synthesis briefs.
    """

    def __init__(
        self,
        model: str | None = None,
        system_prompt: str | None = None,
        completion_fn: Any = None,
    ):
        self._custom_model = model
        self.system_prompt = system_prompt or SCRIBE_DEFAULT_PROMPT
        self._completion_fn = completion_fn

    @property
    def model(self) -> str:
        return self._custom_model or get_system_agent_model("DiscussionScribe", fallback=_DEFAULT_SCRIBE_MODEL) or _DEFAULT_SCRIBE_MODEL

    @model.setter
    def model(self, val: str | None) -> None:
        self._custom_model = val

    async def _invoke_llm(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> Any:
        completion_fn = self._completion_fn or litellm.acompletion
        from core.llm_utils import execute_llm_with_retry
        from core.model_catalog import resolve_model_with_fallback

        resolved_model = resolve_model_with_fallback(
            model or self.model, log_context="SCRIBE"
        ).model
        kwargs: dict[str, Any] = {
            "model": resolved_model,
            "messages": messages,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        return await execute_llm_with_retry(
            completion_fn,
            kwargs=kwargs,
            agent_key="discussion_scribe",
            thread_key=("system", "discussion_scribe"),
            max_attempts=2,
        )

    async def _generate_text_with_fallback(
        self,
        messages: list[dict[str, str]],
        label: str,
        unavailable_noun: str,
    ) -> tuple[str, dict[str, Any], bool]:
        """Invokes LLM without artificial max_tokens caps and retries once on fallback model if empty."""
        from core.model_catalog import resolve_model_with_fallback

        primary_model = resolve_model_with_fallback(self.model, log_context="SCRIBE").model
        resp = await self._invoke_llm(messages, model=primary_model)
        total_pt, total_ct = _extract_token_counts(resp) if resp else (0, 0)
        choices = getattr(resp, "choices", None) if resp else None
        choice = choices[0] if choices else None
        msg = getattr(choice, "message", None) if choice is not None else None
        raw_content = getattr(msg, "content", None) if msg is not None else None
        text = raw_content.strip() if isinstance(raw_content, str) else ""
        used_model = primary_model

        if not text:
            finish_reason_raw = getattr(choice, "finish_reason", None) if choice is not None else None
            finish_reason = finish_reason_raw if isinstance(finish_reason_raw, str) else "unknown"
            reasoning_raw = getattr(msg, "reasoning_content", None) if msg is not None else None
            reasoning_len = len(reasoning_raw) if isinstance(reasoning_raw, str) else 0
            raw_fallback = get_orchestrator_fast_model(fallback=primary_model) or primary_model
            fallback_model = resolve_model_with_fallback(raw_fallback, log_context="SCRIBE").model
            logger.warning(
                f"[SCRIBE] Empty {label} output from model '{primary_model}' "
                f"(finish_reason={finish_reason}, reasoning_chars={reasoning_len}, "
                f"completion_tokens={total_ct}). Retrying with fallback model '{fallback_model}'."
            )
            try:
                retry_resp = await self._invoke_llm(messages, model=fallback_model)
            except Exception as retry_err:  # noqa: BLE001
                logger.warning(f"[SCRIBE] Fallback model '{fallback_model}' failed for {label}: {retry_err}")
                return (
                    f"*({unavailable_noun} unavailable: model returned empty content)*",
                    {"prompt_tokens": total_pt, "completion_tokens": total_ct, "model": primary_model},
                    False,
                )

            retry_pt, retry_ct = _extract_token_counts(retry_resp) if retry_resp else (0, 0)
            total_pt += retry_pt
            total_ct += retry_ct
            retry_choices = getattr(retry_resp, "choices", None) if retry_resp else None
            retry_choice = retry_choices[0] if retry_choices else None
            retry_msg = getattr(retry_choice, "message", None) if retry_choice is not None else None
            retry_content = getattr(retry_msg, "content", None) if retry_msg is not None else None
            text = retry_content.strip() if isinstance(retry_content, str) else ""
            used_model = fallback_model
            if not text:
                retry_fr_raw = getattr(retry_choice, "finish_reason", None) if retry_choice is not None else None
                retry_fr = retry_fr_raw if isinstance(retry_fr_raw, str) else "unknown"
                logger.warning(
                    f"[SCRIBE] Fallback model '{fallback_model}' also returned empty {label} output "
                    f"(finish_reason={retry_fr})."
                )
                return (
                    f"*({unavailable_noun} unavailable: model returned empty content)*",
                    {"prompt_tokens": total_pt, "completion_tokens": total_ct, "model": used_model},
                    False,
                )

        return (
            text,
            {"prompt_tokens": total_pt, "completion_tokens": total_ct, "model": used_model},
            True,
        )

    async def generate_brief(
        self,
        history: list[dict[str, Any]] | list[Turn],
        is_completed: bool = False,
        thread_language: str = "English",
        scribe_max_words: int | None = None,
        scribe_min_words: int | None = None,
    ) -> tuple[DiscussionBrief, dict[str, Any]]:
        """
        Generates or updates a structured DiscussionBrief from conversation history.
        Returns `(DiscussionBrief, token_usage_dict)`.
        """
        if not history:
            return DiscussionBrief(is_completed=is_completed, language=thread_language), {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "model": self.model,
            }

        # Count agent turns (excluding Orchestrator system banners)
        assistant_turns = 0
        for item in history:
            if not isinstance(item, (Turn, dict)):
                continue
            raw_c = item.content if isinstance(item, Turn) else (item.get("content") or item.get("text") or "")
            role, speaker, _ = _extract_speaker_and_body(item)
            if role == "assistant" and not _is_orchestrator_noise(speaker, str(raw_c or "")):
                assistant_turns += 1

        extra_len_instruction = _build_length_instruction(scribe_max_words, scribe_min_words, "synthesis note")

        if is_completed:
            if scribe_max_words is not None and scribe_max_words <= 60:
                structure_req = (
                    f"Keep it ultra-concise with a brief objective and core action items only. "
                    f"Do not use multiple verbose section headings. Do NOT include a conclusion. The entire response MUST be under {scribe_max_words} words."
                )
            else:
                structure_req = (
                    "Include *Core Objective & Problem Statement* (<=35 words), *Key Perspectives* (<=190 words total, <=30 words per bullet), "
                    "and *Risks & Open Questions* (<=75 words). Do NOT include consensus decisions, final conclusions, or action items (those belong in the dedicated Conclusion Note). "
                    "CRITICAL: If there is nothing substantive for a section, omit that section completely (never output empty sections)."
                )
            ceiling_text = (
                f"strictly under {scribe_max_words} words"
                if scribe_max_words is not None
                else "strictly under 300 words maximum ceiling"
            )
            status_directive = (
                f"The discussion has finished. Generate the FINAL concise strategic synthesis live notes ({ceiling_text}). "
                f"{structure_req} "
                f"CRITICAL: Write ALL titles and content strictly in {thread_language}. "
                "Use an appropriate natural status indicator translated into the thread language in the header (e.g. Completed ✅)."
                f"{extra_len_instruction}"
            )
        elif assistant_turns <= 1:
            status_directive = (
                "This is the initial turn of the discussion. Generate the initial concise strategic synthesis note (strictly under 50 words total). "
                "State the initial core objective and initial key perspective. Omit any empty sections. "
                f"CRITICAL: Write ALL titles and content strictly in {thread_language}. "
                "Use an appropriate natural status indicator translated into the thread language in the header (e.g. Updating live...)."
                f"{extra_len_instruction}"
            )
        else:
            ceiling_text = (
                f"strictly under {scribe_max_words} words"
                if scribe_max_words is not None
                else "strictly under 220 words maximum ceiling"
            )
            status_directive = (
                f"The discussion is actively ongoing. Generate the updated, concise strategic synthesis notes ({ceiling_text}). "
                "Include *Core Objective & Problem Statement* (<=35 words), *Key Perspectives* (<=140 words total), and *Risks & Open Questions* (<=45 words). "
                "CRITICAL: If there is nothing substantive for a section, omit that section completely (never output empty sections). "
                f"CRITICAL: Write ALL titles and content strictly in {thread_language}. "
                "Use an appropriate natural status indicator translated into the thread language in the header (e.g. Updating live...)."
                f"{extra_len_instruction}"
            )

        transcript_text, speakers_str = _format_transcript(history)
        if not transcript_text:
            return DiscussionBrief(is_completed=is_completed, language=thread_language), {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "model": self.model,
            }

        formatted_messages: list[dict[str, str]] = [
            {"role": "system", "content": f"{self.system_prompt}\n\n{status_directive}"},
            {
                "role": "user",
                "content": (
                    f"PARTICIPATING AGENTS WHO ACTUALLY SPOKE IN THIS TRANSCRIPT: [{speakers_str}]\n"
                    f"STRICT ATTRIBUTION CONSTRAINT: Attribute viewpoints and perspectives ONLY to agents listed above who actually spoke in the transcript.\n\n"
                    f"Here is the discussion transcript to synthesize:\n\n{transcript_text}\n\n"
                    f"Please generate the strategic synthesis notes now."
                ),
            },
        ]

        start_time = time.time()
        try:
            raw_text, tokens, is_usable = await self._generate_text_with_fallback(
                formatted_messages,
                label="brief",
                unavailable_noun="Synthesis",
            )
            if not is_usable:
                return DiscussionBrief(
                    raw_markdown=raw_text,
                    is_completed=is_completed,
                    language=thread_language,
                ), tokens

            default_max = 300 if is_completed else 220
            raw_text = _apply_word_limit(raw_text, scribe_max_words, default_max_words=default_max, label="Scribe brief")
            extracted_fields = _extract_brief_fields(raw_text)
            brief = DiscussionBrief(
                objective=extracted_fields.get("objective", ""),
                perspectives=extracted_fields.get("perspectives", []),
                consensus=extracted_fields.get("consensus", ""),
                risks=extracted_fields.get("risks", []),
                action_items=extracted_fields.get("action_items", []),
                is_completed=is_completed,
                raw_markdown=raw_text.strip(),
                language=thread_language,
            )
            logger.info(f"[SCRIBE] Generated brief ({len(raw_text)} chars) in {time.time() - start_time:.2f}s")
            return brief, tokens

        except (TimeoutError, asyncio.TimeoutError) as e:
            logger.error(f"[SCRIBE] Timeout generating brief: {e}")
            fallback_text = "*(Synthesis unavailable: request timed out)*"
            return DiscussionBrief(raw_markdown=fallback_text, is_completed=is_completed, language=thread_language), {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "model": self.model,
            }
        except (KeyError, IndexError, AttributeError) as e:
            logger.error(f"[SCRIBE] Response parsing error generating brief: {e}")
            fallback_text = f"*(Synthesis unavailable: {e})*"
            return DiscussionBrief(raw_markdown=fallback_text, is_completed=is_completed, language=thread_language), {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "model": self.model,
            }
        except Exception as e:  # noqa: BLE001 - Scribe synthesis error boundary returns fallback brief
            logger.error(f"[SCRIBE] Error generating brief: {e}")
            fallback_text = f"*(Synthesis unavailable: {e})*"
            return DiscussionBrief(raw_markdown=fallback_text, is_completed=is_completed, language=thread_language), {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "model": self.model,
            }

    async def generate_conclusion(
        self,
        history: list[dict[str, Any]] | list[Turn],
        thread_language: str = "English",
        scribe_max_words: int | None = None,
        scribe_min_words: int | None = None,
    ) -> tuple[DiscussionConclusion, dict[str, Any]]:
        """
        Generates a comprehensive Conclusion Note analyzing consensus, opposing viewpoints,
        arguments/counter-arguments, and supporting agent attribution.
        Returns `(DiscussionConclusion, token_usage_dict)`.
        """
        if not history:
            return DiscussionConclusion(is_completed=True, language=thread_language), {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "model": self.model,
            }

        extra_len_instruction = _build_length_instruction(scribe_max_words, scribe_min_words, "conclusion note")

        ceiling_text = (
            f"strictly under {scribe_max_words} words"
            if scribe_max_words is not None
            else "strictly under 320 words maximum ceiling"
        )
        status_directive = (
            f"The discussion has fully concluded. Analyze the complete debate and generate the compact Conclusion Note ({ceiling_text}). "
            f"Structure into: *Problem Statement* (<=25 words), *Consensus Decisions* (<=85 words), *Divergent Positions & Trade-offs* (<=140 words, 1 compact bullet per position), and *Action Items & Next Steps* (<=70 words). "
            f"CRITICAL: If there is nothing substantive for a section (e.g. unanimous agreement with no divergent positions, or no action items), omit that section completely (never output empty sections). "
            f"CRITICAL: Write ALL headers, titles, and text strictly in {thread_language}. "
            f"{extra_len_instruction}"
        )

        transcript_text, speakers_str = _format_transcript(history)
        if not transcript_text:
            return DiscussionConclusion(is_completed=True, language=thread_language), {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "model": self.model,
            }

        formatted_messages: list[dict[str, str]] = [
            {"role": "system", "content": f"{SCRIBE_CONCLUSION_PROMPT}\n\n{status_directive}"},
            {
                "role": "user",
                "content": (
                    f"PARTICIPATING AGENTS WHO ACTUALLY SPOKE IN THIS DELIBERATION: [{speakers_str}]\n"
                    f"STRICT ATTRIBUTION CONSTRAINT: Attribute opinions, consensus, and opposing arguments ONLY to agents listed in the participating list above who have an explicit turn header in the transcript. NEVER attribute positions or list as 'Supporting Agents' any agent who did not speak in the transcript.\n\n"
                    f"Here is the complete discussion transcript to analyze and conclude:\n\n{transcript_text}\n\n"
                    f"Please generate the comprehensive Conclusion Note now."
                ),
            },
        ]

        start_time = time.time()
        try:
            raw_text, tokens, is_usable = await self._generate_text_with_fallback(
                formatted_messages,
                label="conclusion",
                unavailable_noun="Conclusion",
            )
            if not is_usable:
                return DiscussionConclusion(
                    raw_markdown=raw_text,
                    is_completed=True,
                    language=thread_language,
                ), tokens

            raw_text = _apply_word_limit(raw_text, scribe_max_words, default_max_words=320, label="Conclusion")
            extracted_fields = _extract_conclusion_fields(raw_text)
            conclusion = DiscussionConclusion(
                problem_statement=extracted_fields.get("problem_statement", ""),
                consensus=extracted_fields.get("consensus", ""),
                opposing_conclusions=extracted_fields.get("opposing_conclusions", []),
                action_items=extracted_fields.get("action_items", []),
                raw_markdown=raw_text.strip(),
                is_completed=True,
                language=thread_language,
            )
            logger.info(f"[SCRIBE] Generated conclusion ({len(raw_text)} chars) in {time.time() - start_time:.2f}s")
            return conclusion, tokens

        except (TimeoutError, asyncio.TimeoutError) as e:
            logger.error(f"[SCRIBE] Timeout generating conclusion: {e}")
            fallback_text = "*(Conclusion unavailable: request timed out)*"
            return DiscussionConclusion(raw_markdown=fallback_text, is_completed=True, language=thread_language), {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "model": self.model,
            }
        except (KeyError, IndexError, AttributeError) as e:
            logger.error(f"[SCRIBE] Response parsing error generating conclusion: {e}")
            fallback_text = f"*(Conclusion unavailable: {e})*"
            return DiscussionConclusion(raw_markdown=fallback_text, is_completed=True, language=thread_language), {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "model": self.model,
            }
        except Exception as e:  # noqa: BLE001
            logger.error(f"[SCRIBE] Error generating conclusion: {e}")
            fallback_text = f"*(Conclusion unavailable: {e})*"
            return DiscussionConclusion(raw_markdown=fallback_text, is_completed=True, language=thread_language), {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "model": self.model,
            }


# Global default instance
discussion_scribe = DiscussionScribe()
