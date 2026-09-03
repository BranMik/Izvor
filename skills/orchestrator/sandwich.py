import logging
import random
from typing import Any, Literal

from core.domain import SessionContext, SpeakerDecision, Turn
from .moderator import DiscussionModerator

logger = logging.getLogger(__name__)


class SandwichOrchestrator:
    """
    State machine orchestrator managing panel debates across Sandwich and Fixed topologies.
    Implements hybrid deterministic fast-path for start/end phases and cognitive
    mistral-small-latest moderation for open middle-pool debate.
    """

    def __init__(
        self,
        mode: str = "sandwich",
        start_chain: list[str] | None = None,
        end_chain: list[str] | None = None,
        fixed_chain: list[str] | None = None,
        moderator: DiscussionModerator | None = None,
        agents_map: dict[str, dict[str, Any]] | None = None,
    ):
        self.mode = mode.lower()
        self.start_chain = list(start_chain or [])
        self.end_chain = list(end_chain or [])
        self.fixed_chain = list(fixed_chain or [])
        self.moderator = moderator or DiscussionModerator()
        self.agents_map = agents_map or {}

    def build_initial_sequence(
        self,
        available_agents: list[str],
        active_exclusions: list[str] | None = None,
    ) -> list[str]:
        """
        Builds the initial ordered list of remaining agents for the session,
        filtering out any excluded agents.
        """
        exclusions = set(active_exclusions or [])
        valid_available = [a for a in available_agents if a not in exclusions]

        if self.mode == "fixed":
            # If fixed chain is defined, follow it strictly
            if self.fixed_chain:
                return [a for a in self.fixed_chain if a in valid_available]
            return valid_available

        # Sandwich Mode: start_chain -> middle_pool -> end_chain
        start = [a for a in self.start_chain if a in valid_available]
        end = [a for a in self.end_chain if a in valid_available]

        start_set = set(start)
        end_set = set(end)
        middle = [a for a in valid_available if a not in start_set and a not in end_set]

        # Combine into complete remaining sequence
        return start + middle + end

    def get_phase_info(
        self,
        remaining_pool: list[str],
        is_custom_flow: bool = False,
    ) -> tuple[str | None, list[str]]:
        """
        Determines the designated next agent and eligible remaining agents for prompt injection
        and nomination enforcement based on the current topology phase.
        Returns: (designated_next_agent, eligible_remaining_agents)
        """
        if not remaining_pool:
            return None, []

        if is_custom_flow or self.mode == "fixed":
            return remaining_pool[0], [remaining_pool[0]]

        start_set = set(self.start_chain)
        end_set = set(self.end_chain)
        middle_pool_set = {
            k for k in self.agents_map if k not in start_set and k not in end_set
        } if self.agents_map else {
            k for k in remaining_pool if k not in start_set and k not in end_set
        }

        remaining_middle = [k for k in remaining_pool if k in middle_pool_set]

        if remaining_middle:
            first_middle_idx = next(
                i for i, k in enumerate(remaining_pool) if k in middle_pool_set
            )
            if first_middle_idx > 0:
                return remaining_pool[0], [remaining_pool[0]]
            else:
                eligible = list(remaining_middle)
                designated = remaining_middle[0] if len(remaining_middle) == 1 else None
                return designated, eligible

        end_chain_len = len(self.end_chain)
        if not middle_pool_set and len(remaining_pool) > end_chain_len:
            return remaining_pool[0], [remaining_pool[0]]
        else:
            return remaining_pool[0], [remaining_pool[0]]

    async def get_next_speaker(
        self,
        remaining_pool: list[str],
        history: list[dict[str, Any]] | list[Turn],
        is_custom_flow: bool = False,
        tagged_nomination: str | None = None,
        active_exclusions: list[str] | None = None,
    ) -> tuple[SpeakerDecision, list[str]]:
        """
        Computes the next speaker and returns `(SpeakerDecision, updated_remaining_pool)`.
        """
        exclusions = set(active_exclusions or [])
        active_remaining = [a for a in remaining_pool if a not in exclusions]

        if not active_remaining:
            return (
                SpeakerDecision(
                    next_speaker=None,
                    reason="All available agents have completed or are excluded",
                    is_completed=True,
                    method="deterministic",
                    phase="completed",
                ),
                [],
            )

        # 1. Custom Flow Override (User explicitly provided sequence of tags)
        if is_custom_flow or self.mode == "fixed":
            next_agent = active_remaining.pop(0)
            return (
                SpeakerDecision(
                    next_speaker=next_agent,
                    reason="Sequential fixed or custom tag flow",
                    is_completed=(len(active_remaining) == 0),
                    method="deterministic",
                    phase="fixed_chain" if self.mode == "fixed" else "custom_flow",
                ),
                active_remaining,
            )

        # 2. Sandwich Mode
        start_set = set(self.start_chain)
        end_set = set(self.end_chain)

        # Middle pool agents are those not in start_chain and not in end_chain
        middle_pool_set = {
            k for k in self.agents_map if k not in start_set and k not in end_set
        } if self.agents_map else {
            k for k in active_remaining if k not in start_set and k not in end_set
        }

        remaining_middle = [k for k in active_remaining if k in middle_pool_set]

        if remaining_middle:
            first_middle_idx = next(
                i for i, k in enumerate(active_remaining) if k in middle_pool_set
            )
            if first_middle_idx > 0:
                # Still in start_chain: strictly sequential
                next_agent = active_remaining.pop(0)
                return (
                    SpeakerDecision(
                        next_speaker=next_agent,
                        reason="Sequential execution of sandwich start_chain",
                        is_completed=False,
                        method="deterministic",
                        phase="start_chain",
                    ),
                    active_remaining,
                )
            else:
                # In middle_pool: check nominations or call moderator
                if tagged_nomination and tagged_nomination in remaining_middle:
                    next_agent = tagged_nomination
                    active_remaining.remove(next_agent)
                    return (
                        SpeakerDecision(
                            next_speaker=next_agent,
                            reason=f"Honored explicit nomination of [{next_agent}]",
                            is_completed=False,
                            method="deterministic",
                            phase="middle_pool",
                        ),
                        active_remaining,
                    )

                if len(remaining_middle) == 1:
                    next_agent = remaining_middle[0]
                    active_remaining.remove(next_agent)
                    return (
                        SpeakerDecision(
                            next_speaker=next_agent,
                            reason="Final remaining specialist in middle pool",
                            is_completed=False,
                            method="deterministic",
                            phase="middle_pool",
                        ),
                        active_remaining,
                    )

                # Cognitive choice via mistral-small-latest
                decision = await self.moderator.select_next_speaker(
                    history=history,
                    candidates=remaining_middle,
                    agents_map=self.agents_map,
                )
                decision.phase = "middle_pool"

                # If the moderator detected consensus, skip remaining middle debate
                if decision.is_completed:
                    logger.info("[ORCHESTRATOR] Moderator detected consensus in middle pool. Advancing to end_chain.")
                    active_remaining = [a for a in active_remaining if a not in middle_pool_set]
                    if active_remaining:
                        end_agent = active_remaining.pop(0)
                        return (
                            SpeakerDecision(
                                next_speaker=end_agent,
                                reason="Consensus reached; advancing directly to end_chain synthesis",
                                is_completed=(len(active_remaining) == 0),
                                method="llm",
                                phase="end_chain",
                            ),
                            active_remaining,
                        )
                    else:
                        return (
                            SpeakerDecision(
                                next_speaker=None,
                                reason="Consensus reached and debate complete",
                                is_completed=True,
                                method="llm",
                                phase="completed",
                            ),
                            [],
                        )

                if decision.next_speaker and decision.next_speaker in active_remaining:
                    active_remaining.remove(decision.next_speaker)
                return decision, active_remaining

        # Middle pool is exhausted or was empty: in start_chain (if any remains) or end_chain
        end_chain_len = len(self.end_chain)
        if len(active_remaining) > end_chain_len:
            # Still in start_chain
            next_agent = active_remaining.pop(0)
            return (
                SpeakerDecision(
                    next_speaker=next_agent,
                    reason="Sequential execution of sandwich start_chain",
                    is_completed=False,
                    method="deterministic",
                    phase="start_chain",
                ),
                active_remaining,
            )
        else:
            # In end_chain
            next_agent = active_remaining.pop(0)
            return (
                SpeakerDecision(
                    next_speaker=next_agent,
                    reason="Sequential execution of sandwich end_chain synthesis",
                    is_completed=(len(active_remaining) == 0),
                    method="deterministic",
                    phase="end_chain",
                ),
                active_remaining,
            )
