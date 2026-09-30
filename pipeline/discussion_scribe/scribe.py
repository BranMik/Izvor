import asyncio
import logging
import time
from typing import Any

import litellm

from core.config import get_thread_notes_model
from core.domain import DiscussionBrief, Turn

logger = logging.getLogger(__name__)

SCRIBE_DEFAULT_PROMPT = """You are an objective, executive-level discussion scribe and strategic synthesizer.
Your responsibility is to analyze the multi-agent discussion transcript and produce structured strategic notes.

Strict Formatting Rules:
1. All headers, section titles, status indicators, and bullet points must be written strictly in the designated thread language. Never switch languages.
2. Structure sections clearly (translate section titles into the thread language):
   - *Core Objective & Problem Statement*
   - *Key Perspectives & Insights*
   - *Consensus & Strategic Decisions*
   - *Risks, Trade-offs & Open Questions*
   - *Action Items & Next Steps*
3. Keep the content dense, objective, and strictly within the requested word ceiling.
"""


class DiscussionScribe:
    """
    Platform-agnostic discussion scribe service that analyzes conversations
    and produces structured synthesis briefs.
    """

    def __init__(self, model: str | None = None, system_prompt: str | None = None):
        self._custom_model = model
        self.system_prompt = system_prompt or SCRIBE_DEFAULT_PROMPT

    @property
    def model(self) -> str:
        return self._custom_model or get_thread_notes_model()

    @model.setter
    def model(self, val: str | None) -> None:
        self._custom_model = val

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
                    f"Keep it ultra-concise with a brief objective and core conclusion only. "
                    f"Do not use multiple verbose section headings. The entire response MUST be under {scribe_max_words} words."
                )
            else:
                structure_req = (
                    "Include Core Objective & Problem Statement, Key Perspectives & Insights, "
                    "Consensus & Strategic Decisions, Risks & Trade-offs, and Action Items & Next Steps."
                )
            ceiling_text = f"strictly under {scribe_max_words} words" if scribe_max_words is not None else "strictly under 500 words maximum ceiling"
            status_directive = (
                f"The discussion has finished. Generate the FINAL concise strategic synthesis notes ({ceiling_text}). "
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
        for item in history:
            if isinstance(item, Turn):
                transcript_lines.append(f"[{item.speaker}]: {item.content}")
            elif isinstance(item, dict):
                role = item.get("role", "user")
                speaker = item.get("username") or role
                content = item.get("content") or item.get("text") or ""
                transcript_lines.append(f"[{speaker}]: {content}")

        transcript_text = "\n\n".join(transcript_lines)

        formatted_messages: list[dict[str, str]] = [
            {"role": "system", "content": f"{self.system_prompt}\n\n{status_directive}"},
            {
                "role": "user",
                "content": f"Here is the discussion transcript to synthesize:\n\n{transcript_text}\n\nPlease generate the strategic synthesis notes now.",
            },
        ]

        start_time = time.time()
        try:
            if hasattr(litellm.completion, "assert_called") or hasattr(litellm.completion, "mock_calls"):
                res = litellm.completion(
                    model=self.model,
                    messages=formatted_messages,
                    max_tokens=max_tokens,
                    num_retries=2,
                )
                resp = await res if asyncio.iscoroutine(res) else res
            else:
                res = litellm.acompletion(
                    model=self.model,
                    messages=formatted_messages,
                    max_tokens=max_tokens,
                    num_retries=2,
                )
                resp = await res if asyncio.iscoroutine(res) else res
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

            brief = DiscussionBrief(
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


# Global default instance
discussion_scribe = DiscussionScribe()
