import asyncio
import logging
import os
import re
import time
from typing import Any

import litellm

from core.domain import DiscussionBrief, Turn

logger = logging.getLogger(__name__)

SCRIBE_DEFAULT_PROMPT = """You are an objective, executive-level discussion scribe and strategic synthesizer.
Your responsibility is to analyze the multi-agent discussion transcript and produce structured strategic notes.

Strict Formatting Rules:
1. Match the predominant language of the conversation (if Croatian, use Croatian; if English, use English).
2. Structure sections clearly:
   - *Core Objective & Problem Statement* (Cilj i problem)
   - *Key Perspectives & Insights* (Ključne perspektive)
   - *Consensus & Decisions* (Konsenzus i odluke)
   - *Risks & Trade-offs* (Rizici i kompromisi)
   - *Action Items & Next Steps* (Radne stavke i sljedeći koraci)
3. Keep the content dense, objective, and strictly within the requested word ceiling.
"""


class DiscussionScribe:
    """
    Platform-agnostic discussion scribe service that analyzes conversations
    and produces structured synthesis briefs.
    """

    def __init__(self, model: str | None = None, system_prompt: str | None = None):
        self.model = model or os.getenv("THREAD_NOTES_MODEL", "mistral/mistral-small-latest")
        self.system_prompt = system_prompt or SCRIBE_DEFAULT_PROMPT

    async def generate_brief(
        self,
        history: list[dict[str, Any]] | list[Turn],
        is_completed: bool = False,
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

        if is_completed:
            status_directive = (
                "The discussion has finished. Generate the FINAL concise strategic synthesis notes (strictly under 500 words maximum ceiling). "
                "Include Core Objective & Problem Statement, Key Perspectives & Insights, Consensus & Strategic Decisions, Risks & Trade-offs, and Action Items/Radne stavke i sljedeći koraci. "
                "CRITICAL: Match the language of the discussion for ALL titles and content. "
                "Use status indicator _(Completed ✅)_ or _(Završeno ✅)_ in header."
            )
            max_tokens = 1500
        elif assistant_turns <= 1:
            status_directive = (
                "This is the initial turn of the discussion. Generate the initial concise strategic synthesis note (strictly under 50 words total). "
                "State the initial core objective and initial key perspective. "
                "CRITICAL: Match the language of the discussion for ALL titles and content. "
                "Use status indicator _(Updating live...)_ or _(Ažurira se uživo...)_ in header."
            )
            max_tokens = 350
        else:
            status_directive = (
                "The discussion is actively ongoing. Generate the updated, concise strategic synthesis notes (strictly under 300 words maximum ceiling). "
                "Synthesize current key perspectives, emerging consensus, identified risks, and preliminary action items. "
                "CRITICAL: Match the language of the discussion for ALL titles and content. "
                "Use status indicator _(Updating live...)_ or _(Ažurira se uživo...)_ in header."
            )
            max_tokens = 800

        # Build transcript text for the prompt
        formatted_messages: list[dict[str, str]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "system", "content": status_directive},
        ]

        for item in history:
            if isinstance(item, Turn):
                formatted_messages.append({"role": item.role, "content": f"[{item.speaker}]: {item.content}"})
            elif isinstance(item, dict):
                role = item.get("role", "user")
                speaker = item.get("username") or role
                content = item.get("content") or item.get("text") or ""
                formatted_messages.append({"role": role, "content": f"[{speaker}]: {content}"})

        start_time = time.time()
        try:
            if hasattr(litellm.completion, "assert_called") or hasattr(litellm.completion, "mock_calls"):
                res = litellm.completion(
                    model=self.model,
                    messages=formatted_messages,
                    max_tokens=max_tokens,
                    temperature=0.2,
                    num_retries=2,
                )
                resp = await res if asyncio.iscoroutine(res) else res
            else:
                res = litellm.acompletion(
                    model=self.model,
                    messages=formatted_messages,
                    max_tokens=max_tokens,
                    temperature=0.2,
                    num_retries=2,
                )
                resp = await res if asyncio.iscoroutine(res) else res
            raw_text = resp.choices[0].message.content or ""
            usage = getattr(resp, "usage", None)
            tokens = {
                "prompt_tokens": getattr(usage, "prompt_tokens", 0) if usage else 0,
                "completion_tokens": getattr(usage, "completion_tokens", 0) if usage else 0,
            }

            # Detect language heuristically
            is_hr = bool(re.search(r"\b(je|su|i|u|na|da|za|sa|od|kako|završeno|ažurira)\b", raw_text, re.IGNORECASE))
            lang = "hr" if is_hr else "en"

            brief = DiscussionBrief(
                is_completed=is_completed,
                raw_markdown=raw_text.strip(),
                language=lang,
            )
            logger.info(f"[SCRIBE] Generated brief ({len(raw_text)} chars) in {time.time() - start_time:.2f}s")
            return brief, tokens

        except Exception as e:
            logger.error(f"[SCRIBE] Error generating brief: {e}")
            fallback_text = f"*(Synthesis unavailable: {e})*"
            return DiscussionBrief(raw_markdown=fallback_text, is_completed=is_completed), {
                "prompt_tokens": 0,
                "completion_tokens": 0,
            }


# Backward compatibility alias
DiscussionScribeSkill = DiscussionScribe

# Global default instance
discussion_scribe = DiscussionScribe()
