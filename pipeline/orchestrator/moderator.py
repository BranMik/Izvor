import json
import logging
import os
from typing import Any

import litellm

from core.domain import SpeakerDecision, Turn

logger = logging.getLogger(__name__)

MODERATOR_SYSTEM_PROMPT = """You are an objective AI panel discussion moderator.
Your goal is to inspect the current multi-agent conversation and select the next most relevant specialist from the remaining candidate list.

Rules:
1. "next_speaker": Must be EXACTLY ONE name from the provided CANDIDATES list.
2. "consensus_reached": Boolean true ONLY if all key technical perspectives have converged, no open contradictions remain, and further debate is redundant. Otherwise false.
3. "reason": A brief one-sentence rationale for this choice.

Respond ONLY with valid JSON matching:
{
  "next_speaker": "<one of the candidate names>",
  "consensus_reached": <true or false>,
  "reason": "<short rationale>"
}"""


class DiscussionModerator:
    """
    Cognitive moderator powered by mistral-small-latest to dynamically steer
    middle-pool debate turns and detect early consensus.
    """

    def __init__(self, model: str | None = None):
        self.model = model or os.getenv("MODERATOR_MODEL", "mistral/mistral-small-latest")

    async def select_next_speaker(
        self,
        history: list[dict[str, Any]] | list[Turn],
        candidates: list[str],
        agents_map: dict[str, dict[str, Any]] | None = None,
    ) -> SpeakerDecision:
        """
        Calls mistral-small-latest to choose the next speaker from the candidate pool.
        Falls back deterministically to candidate[0] on any error.
        """
        if not candidates:
            return SpeakerDecision(
                next_speaker=None,
                reason="No candidate agents remaining",
                is_completed=True,
                method="deterministic",
            )

        if len(candidates) == 1:
            return SpeakerDecision(
                next_speaker=candidates[0],
                reason="Single remaining specialist in pool",
                is_completed=False,
                method="deterministic",
                candidates=candidates,
            )

        # Build concise context summary (last 3-4 turns to keep tokens & latency minimal)
        recent_turns: list[str] = []
        for item in history[-4:]:
            if isinstance(item, Turn):
                recent_turns.append(f"[{item.speaker}]: {item.content[:300]}")
            elif isinstance(item, dict):
                speaker = item.get("username") or item.get("role") or "Unknown"
                content = item.get("content") or item.get("text") or ""
                recent_turns.append(f"[{speaker}]: {content[:300]}")

        history_summary = "\n---\n".join(recent_turns) if recent_turns else "(Discussion just started)"

        candidate_hints = []
        agents_map = agents_map or {}
        for c in candidates:
            role_hint = agents_map.get(c, {}).get("username", c)
            candidate_hints.append(f"- {c} ({role_hint})")

        user_content = (
            f"RECENT DISCUSSION TURNS:\n{history_summary}\n\n"
            f"AVAILABLE CANDIDATES (Pick ONE):\n" + "\n".join(candidate_hints)
        )

        messages = [
            {"role": "system", "content": MODERATOR_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

        try:
            resp = await litellm.acompletion(
                model=self.model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.1,
                num_retries=1,
            )
            raw_content = resp.choices[0].message.content or "{}"
            parsed = json.loads(raw_content)

            chosen = parsed.get("next_speaker", "").strip()
            consensus = bool(parsed.get("consensus_reached", False))
            reason = parsed.get("reason", "Moderator selection")

            # Validate that chosen is strictly in candidates
            matching_candidate = next((c for c in candidates if c.lower() == chosen.lower()), None)
            if not matching_candidate:
                matching_candidate = candidates[0]
                reason = f"Fallback to {candidates[0]} (chosen '{chosen}' not in candidate list)"

            return SpeakerDecision(
                next_speaker=matching_candidate,
                reason=reason,
                is_completed=consensus,
                method="llm",
                candidates=candidates,
            )
        except Exception as e:
            logger.warning(f"[MODERATOR] Error calling moderator LLM ({e}). Falling back to deterministic order.")
            return SpeakerDecision(
                next_speaker=candidates[0],
                reason=f"Deterministic fallback due to moderator error: {e}",
                is_completed=False,
                method="deterministic",
                candidates=candidates,
            )
