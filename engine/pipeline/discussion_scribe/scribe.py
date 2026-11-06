import asyncio
import logging
import re
import time
from typing import Any

import litellm

from core.config import get_thread_notes_model
from core.domain import DiscussionBrief, DiscussionConclusion, Turn

from prompts.builder import load_system_prompt

logger = logging.getLogger(__name__)

_DEFAULT_SCRIBE_PROMPT_FALLBACK = """<system_agent_specification id="Discussion_Scribe_Live" role="Live Strategic Synthesis Scribe">
  <task_objective>
    Analyze the ongoing multi-agent discussion transcript and synthesize a structured, high-density live strategic brief in Slack.
  </task_objective>

  <processing_rules>
    1. Language Invariant: Write ALL headers, section titles, status indicators, and bullet points strictly in the designated thread language. Never switch languages mid-note.
    2. Mandatory Section Structure (translate section titles into the designated thread language):
       - *Core Objective & Problem Statement*
       - *Key Perspectives & Emerging Consensus*
       - *Risks, Trade-offs & Open Questions*
       - *Action Items & Next Steps*
    3. Live Note Boundary (CRITICAL): Do NOT include a final conclusion or concluding summary in this live note (final conclusions are synthesized separately in a dedicated conclusion note).
    4. Information Density: Keep all points objective, scannable, and strictly within the requested word ceiling without conversational preamble.
    5. Strict Speaker Attribution: Attribute viewpoints and perspectives ONLY to agents who have an explicit "[AgentName]:" turn header in the transcript.
  </processing_rules>

  <output_contract>
    - Output ONLY the structured markdown live synthesis note containing the 4 translated sections above.
    - Zero introductory or closing meta-commentary.
  </output_contract>
</system_agent_specification>"""

_DEFAULT_CONCLUSION_PROMPT_FALLBACK = """<system_agent_specification id="Discussion_Scribe_Conclusion" role="Dialectical Conclusion Synthesizer">
  <task_objective>
    Analyze the complete multi-agent deliberation transcript and synthesize the definitive, executive-level Conclusion Note capturing both consensus decisions and opposing dialectical viewpoints.
  </task_objective>

  <processing_rules>
    1. Language Invariant: Write ALL headers, section titles, status indicators, and bullet points strictly in the designated thread language. Never switch languages.
    2. Mandatory Two-Part Structure (translate titles into the designated thread language):
       - Part 1: *Problem Statement*
         • Concise executive summary of the core objective, problem statement, and scope addressed by the panel.
       - Part 2: *Conclusion and Argumentation*
         • *Consensus Decisions*: Clear statement of decisions and propositions where participating agents reached broad agreement.
         • *Opposing Conclusions & Divergent Advice*: Detail all opposing conclusions, divergent solutions, or alternative recommendations surfaced during deliberation (including both majority and minority/contrarian positions). For each opposing position, specify:
           - Clearly identified conclusion or recommendation.
           - Supporting Agents: Participating agents who advocated for this position.
           - Arguments: Core evidence, reasoning, and justifications supporting this position.
           - Counter-Arguments: Criticisms, risks, or trade-offs raised by other agents against this position.
         • *Strategic Trade-offs & Key Unresolved Questions*: Critical unresolved tensions or trade-offs between the competing conclusions.
    3. Dialectical Impartiality: Never suppress dissenting or minority viewpoints; represent minority arguments with the same analytical rigor as majority consensus.
    4. Strict Speaker Attribution: Attribute arguments, positions, and "Supporting Agents" ONLY to agents who have an explicit "[AgentName]:" turn header in the transcript. NEVER attribute viewpoints to agents who were merely nominated or mentioned at the end of another speaker's message but did not actually speak.
  </processing_rules>

  <output_contract>
    - Output ONLY the structured two-part Conclusion Note in clean, scannable markdown.
    - Zero conversational filler or meta-commentary.
  </output_contract>
</system_agent_specification>"""

SCRIBE_DEFAULT_PROMPT = load_system_prompt("Discussion_Scribe_Live", fallback=_DEFAULT_SCRIBE_PROMPT_FALLBACK)
SCRIBE_CONCLUSION_PROMPT = load_system_prompt(
    "Discussion_Scribe_Conclusion", fallback=_DEFAULT_CONCLUSION_PROMPT_FALLBACK
)


def _clean_turn_content_for_scribe(content: str) -> str:
    """Strips trailing agent nomination tags (/[AgentName]) and token footers ({model : tokens}) for clean scribe processing."""
    if not content:
        return ""
    # Strip token stats footer e.g. {gemini-3.8-flash : 210} or {model: tokens}
    cleaned = re.sub(r"\{\s*[^}]+:\s*\d+\s*\}\s*$", "", content.strip())
    # Strip trailing nomination tags e.g. /[AgentName], /[agent-10003], / [Agent]
    cleaned = re.sub(r"/\s*\[\s*[^\]]+\s*\]\s*$", "", cleaned.strip())
    return cleaned.strip()


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
        is_header = l_str.startswith(("#", "**", "Part", "part", "PART")) or (l_str.endswith(":") and len(l_str) < 60)
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
            elif any(k in lower for k in ("risk", "trade-off", "open question", "rizik", "kompromis", "otvoreno pitanje")):
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

    current_section = None
    lines = raw_text.splitlines()
    for line in lines:
        l_str = line.strip()
        lower = l_str.lower()
        is_header = l_str.startswith(("#", "**", "Part", "part", "PART")) or (l_str.endswith(":") and len(l_str) < 60)
        if is_header:
            if any(k in lower for k in ("problem statement", "problem", "cilj", "pitanje")):
                current_section = "problem"
                continue
            elif any(k in lower for k in ("consensus", "konsenzus", "zaključak", "slaganje")):
                current_section = "consensus"
                continue
            elif any(k in lower for k in ("opposing", "divergent", "suprotn", "različit", "drugačij")):
                current_section = "opposing"
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

    return {
        "problem_statement": problem_statement.strip(),
        "consensus": consensus.strip(),
        "opposing_conclusions": opposing_conclusions,
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
        return self._custom_model or get_thread_notes_model()

    @model.setter
    def model(self, val: str | None) -> None:
        self._custom_model = val

    async def _invoke_llm(self, messages: list[dict[str, str]], max_tokens: int) -> Any:
        completion_fn = self._completion_fn or litellm.acompletion
        from core.llm_utils import execute_llm_with_retry

        return await execute_llm_with_retry(
            completion_fn,
            kwargs={
                "model": self.model,
                "messages": messages,
                "max_tokens": max_tokens,
                "num_retries": 1,
            },
            agent_key="discussion_scribe",
            thread_key=("system", "discussion_scribe"),
            max_attempts=2,
        )

    async def generate_brief(
        self,
        history: list[dict[str, Any]] | list[Turn],
        is_completed: bool = False,
        thread_language: str = "English",
        scribe_max_words: int | None = None,
        scribe_min_words: int | None = None,
    ) -> tuple[DiscussionBrief, dict[str, int]]:
        """
        Generates or updates a structured DiscussionBrief from conversation history.
        Returns `(DiscussionBrief, token_usage_dict)`.
        """
        if not history:
            return DiscussionBrief(is_completed=is_completed), {"prompt_tokens": 0, "completion_tokens": 0}

        # Count agent turns
        assistant_turns = 0
        for item in history:
            role = item.role if isinstance(item, Turn) else item.get("role")
            if role == "assistant":
                assistant_turns += 1

        length_directives = []
        if scribe_max_words is not None:
            length_directives.append(
                f"CRITICAL LENGTH CONSTRAINT: The entire synthesis note must strictly NOT exceed {scribe_max_words} words total. Be extremely concise."
            )
        if scribe_min_words is not None:
            length_directives.append(
                f"CRITICAL LENGTH CONSTRAINT: The entire synthesis note must have a minimum length of {scribe_min_words} words. Elaborate sufficiently to meet this floor."
            )
        extra_len_instruction = f" {' '.join(length_directives)}" if length_directives else ""

        if is_completed:
            if scribe_max_words is not None and scribe_max_words <= 60:
                structure_req = (
                    f"Keep it ultra-concise with a brief objective and core action items only. "
                    f"Do not use multiple verbose section headings. Do NOT include a conclusion. The entire response MUST be under {scribe_max_words} words."
                )
            else:
                structure_req = (
                    "Include Core Objective & Problem Statement, Key Perspectives & Emerging Consensus, "
                    "Risks & Trade-offs, and Action Items & Next Steps. Do NOT include a conclusion section."
                )
            ceiling_text = (
                f"strictly under {scribe_max_words} words"
                if scribe_max_words is not None
                else "strictly under 500 words maximum ceiling"
            )
            status_directive = (
                f"The discussion has finished. Generate the FINAL concise strategic synthesis live notes ({ceiling_text}). "
                f"{structure_req} "
                f"CRITICAL: Write ALL titles and content strictly in {thread_language}. "
                "Use an appropriate natural status indicator translated into the thread language in the header (e.g. Completed ✅)."
                f"{extra_len_instruction}"
            )
            max_tokens = 1500
        elif assistant_turns <= 1:
            status_directive = (
                "This is the initial turn of the discussion. Generate the initial concise strategic synthesis note (strictly under 50 words total). "
                "State the initial core objective and initial key perspective. "
                f"CRITICAL: Write ALL titles and content strictly in {thread_language}. "
                "Use an appropriate natural status indicator translated into the thread language in the header (e.g. Updating live...)."
                f"{extra_len_instruction}"
            )
            max_tokens = 350
        else:
            status_directive = (
                "The discussion is actively ongoing. Generate the updated, concise strategic synthesis notes (strictly under 300 words maximum ceiling). "
                "Synthesize current key perspectives, emerging consensus, identified risks, and preliminary action items. "
                f"CRITICAL: Write ALL titles and content strictly in {thread_language}. "
                "Use an appropriate natural status indicator translated into the thread language in the header (e.g. Updating live...)."
                f"{extra_len_instruction}"
            )
            max_tokens = 800

        if scribe_min_words is not None:
            max_tokens = max(max_tokens, int(scribe_min_words * 3))

        # Build transcript text for the prompt
        transcript_lines: list[str] = []
        participating_speakers: list[str] = []
        for item in history:
            if isinstance(item, Turn):
                role = item.role
                speaker = item.speaker
                raw_c = item.content or ""
            elif isinstance(item, dict):
                role = item.get("role", "user")
                speaker = item.get("username") or role
                raw_c = item.get("content") or item.get("text") or ""
            else:
                continue

            cleaned_c = _clean_turn_content_for_scribe(raw_c)
            if not cleaned_c:
                continue
            transcript_lines.append(f"[{speaker}]: {cleaned_c}")
            if role == "assistant" and speaker and speaker not in participating_speakers:
                participating_speakers.append(speaker)

        transcript_text = "\n\n".join(transcript_lines)
        speakers_str = ", ".join(participating_speakers) if participating_speakers else "None"

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
            resp = await self._invoke_llm(formatted_messages, max_tokens)
            raw_text = resp.choices[0].message.content or ""
            if scribe_max_words is not None:
                try:
                    smw = int(scribe_max_words)
                    cutoff = smw + 50
                    words = raw_text.split()
                    if len(words) > cutoff:
                        logger.info(
                            f"[SCRIBE MECHANICAL CUTOFF] Scribe brief exceeded {cutoff} words ({len(words)} words). "
                            f"Mechanically truncated to {cutoff} words."
                        )
                        raw_text = " ".join(words[:cutoff])
                except (ValueError, TypeError):
                    pass
            usage = getattr(resp, "usage", None)
            tokens = {
                "prompt_tokens": getattr(usage, "prompt_tokens", 0) if usage else 0,
                "completion_tokens": getattr(usage, "completion_tokens", 0) if usage else 0,
            }

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
            return DiscussionBrief(raw_markdown=fallback_text, is_completed=is_completed), {
                "prompt_tokens": 0,
                "completion_tokens": 0,
            }
        except (KeyError, IndexError, AttributeError) as e:
            logger.error(f"[SCRIBE] Response parsing error generating brief: {e}")
            fallback_text = f"*(Synthesis unavailable: {e})*"
            return DiscussionBrief(raw_markdown=fallback_text, is_completed=is_completed), {
                "prompt_tokens": 0,
                "completion_tokens": 0,
            }
        except Exception as e:  # noqa: BLE001 - Scribe synthesis error boundary returns fallback brief
            logger.error(f"[SCRIBE] Error generating brief: {e}")
            fallback_text = f"*(Synthesis unavailable: {e})*"
            return DiscussionBrief(raw_markdown=fallback_text, is_completed=is_completed), {
                "prompt_tokens": 0,
                "completion_tokens": 0,
            }

    async def generate_conclusion(
        self,
        history: list[dict[str, Any]] | list[Turn],
        thread_language: str = "English",
        scribe_max_words: int | None = None,
        scribe_min_words: int | None = None,
    ) -> tuple[DiscussionConclusion, dict[str, int]]:
        """
        Generates a comprehensive Conclusion Note analyzing consensus, opposing viewpoints,
        arguments/counter-arguments, and supporting agent attribution.
        Returns `(DiscussionConclusion, token_usage_dict)`.
        """
        if not history:
            return DiscussionConclusion(is_completed=True, language=thread_language), {
                "prompt_tokens": 0,
                "completion_tokens": 0,
            }

        length_directives = []
        if scribe_max_words is not None:
            length_directives.append(
                f"CRITICAL LENGTH CONSTRAINT: The entire conclusion note must strictly NOT exceed {scribe_max_words} words total. Be concise while preserving all divergent viewpoints."
            )
        if scribe_min_words is not None:
            length_directives.append(
                f"CRITICAL LENGTH CONSTRAINT: The entire conclusion note must have a minimum length of {scribe_min_words} words. Elaborate sufficiently to meet this floor."
            )
        extra_len_instruction = f" {' '.join(length_directives)}" if length_directives else ""

        status_directive = (
            f"The discussion has fully concluded. Analyze the complete debate and generate the comprehensive Conclusion Note. "
            f"Structure strictly into Part 1: Problem Statement and Part 2: Conclusion and Argumentation. "
            f"Ensure ALL opposing/divergent viewpoints from the debate are explicitly articulated with supporting agents, arguments, and counter-arguments. "
            f"CRITICAL: Write ALL headers, titles, and text strictly in {thread_language}. "
            f"{extra_len_instruction}"
        )

        max_tokens = 2500
        if scribe_min_words is not None:
            max_tokens = max(max_tokens, int(scribe_min_words * 4))

        transcript_lines: list[str] = []
        participating_speakers: list[str] = []
        for item in history:
            if isinstance(item, Turn):
                role = item.role
                speaker = item.speaker
                raw_c = item.content or ""
            elif isinstance(item, dict):
                role = item.get("role", "user")
                speaker = item.get("username") or role
                raw_c = item.get("content") or item.get("text") or ""
            else:
                continue

            cleaned_c = _clean_turn_content_for_scribe(raw_c)
            if not cleaned_c:
                continue
            transcript_lines.append(f"[{speaker}]: {cleaned_c}")
            if role == "assistant" and speaker and speaker not in participating_speakers:
                participating_speakers.append(speaker)

        transcript_text = "\n\n".join(transcript_lines)
        speakers_str = ", ".join(participating_speakers) if participating_speakers else "None"

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
            resp = await self._invoke_llm(formatted_messages, max_tokens)
            raw_text = resp.choices[0].message.content or ""
            if scribe_max_words is not None:
                try:
                    smw = int(scribe_max_words)
                    cutoff = smw + 50
                    words = raw_text.split()
                    if len(words) > cutoff:
                        logger.info(
                            f"[SCRIBE MECHANICAL CUTOFF] Conclusion exceeded {cutoff} words ({len(words)} words). "
                            f"Mechanically truncated to {cutoff} words."
                        )
                        raw_text = " ".join(words[:cutoff])
                except (ValueError, TypeError):
                    pass

            usage = getattr(resp, "usage", None)
            tokens = {
                "prompt_tokens": getattr(usage, "prompt_tokens", 0) if usage else 0,
                "completion_tokens": getattr(usage, "completion_tokens", 0) if usage else 0,
            }

            extracted_fields = _extract_conclusion_fields(raw_text)
            conclusion = DiscussionConclusion(
                problem_statement=extracted_fields.get("problem_statement", ""),
                consensus=extracted_fields.get("consensus", ""),
                opposing_conclusions=extracted_fields.get("opposing_conclusions", []),
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
            }
        except (KeyError, IndexError, AttributeError) as e:
            logger.error(f"[SCRIBE] Response parsing error generating conclusion: {e}")
            fallback_text = f"*(Conclusion unavailable: {e})*"
            return DiscussionConclusion(raw_markdown=fallback_text, is_completed=True, language=thread_language), {
                "prompt_tokens": 0,
                "completion_tokens": 0,
            }
        except Exception as e:  # noqa: BLE001
            logger.error(f"[SCRIBE] Error generating conclusion: {e}")
            fallback_text = f"*(Conclusion unavailable: {e})*"
            return DiscussionConclusion(raw_markdown=fallback_text, is_completed=True, language=thread_language), {
                "prompt_tokens": 0,
                "completion_tokens": 0,
            }


# Global default instance
discussion_scribe = DiscussionScribe()
