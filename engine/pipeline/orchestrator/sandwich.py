import logging
from typing import Any

from core.models import OrchestrationMode

logger = logging.getLogger(__name__)


class SandwichOrchestrator:
    """
    State machine orchestrator managing panel debate topology across Sandwich and Fixed configurations.
    Builds the initial agent execution sequence and computes phase routing information
    (start_chain, middle_pool, end_chain) for turn-taking and nomination enforcement.
    """

    def __init__(
        self,
        mode: OrchestrationMode | str = OrchestrationMode.SANDWICH,
        start_chain: list[str] | None = None,
        end_chain: list[str] | None = None,
        fixed_chain: list[str] | None = None,
        agents_map: dict[str, dict[str, Any]] | None = None,
        standby_agents: set[str] | None = None,
    ):
        mode_val = mode.value if isinstance(mode, OrchestrationMode) else str(mode)
        self.mode = mode_val.lower()
        self.start_chain = list(start_chain or [])
        self.end_chain = list(end_chain or [])
        self.fixed_chain = list(fixed_chain or [])
        self.agents_map = agents_map or {}
        self.standby_agents = set(standby_agents or [])

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

        if self.mode == OrchestrationMode.FIXED:
            # If fixed chain is defined, follow it strictly
            if self.fixed_chain:
                return [a for a in self.fixed_chain if a in valid_available]
            return valid_available

        # Sandwich Mode: start_chain -> middle_pool -> end_chain
        start = [a for a in self.start_chain if a in valid_available]
        end = [a for a in self.end_chain if a in valid_available]

        start_set = set(start)
        end_set = set(end)
        standby_set = set(self.standby_agents)

        middle = [
            a
            for a in valid_available
            if a not in start_set and a not in end_set and a not in standby_set
        ]

        # Combine into complete remaining sequence (standby agents enter dynamically via stress-test)
        return start + middle + end

    def get_phase_info(
        self,
        remaining_pool: list[str],
        is_custom_flow: bool = False,
        is_open_debate: bool = False,
        current_speaker: str | None = None,
    ) -> tuple[str | None, list[str]]:
        """
        Determines the designated next agent and eligible remaining agents for prompt injection
        and nomination enforcement based on the current topology phase.
        Returns: (designated_next_agent, eligible_remaining_agents)
        """
        if not remaining_pool:
            return None, []

        rebuttal_key: str | None = None
        try:
            from orchestration.agent_settings import get_rebuttal_agent

            rebuttal_key = get_rebuttal_agent()
        except Exception as e:  # noqa: BLE001
            logger.debug("Failed to retrieve rebuttal agent: %s", e)

        standby_set = set(self.standby_agents)
        if rebuttal_key:
            standby_set.add(rebuttal_key)

        # 1. Handoff Enforcement Between Challenger and RebuttalAdvocate (TKT-823)
        # When remaining_pool begins with rebuttal_key (Challenger is currently speaking and rebuttal_key is at remaining_pool[0])
        if remaining_pool and ((rebuttal_key and remaining_pool[0] == rebuttal_key) or remaining_pool[0] in standby_set):
            return remaining_pool[0], [remaining_pool[0]]

        if is_open_debate:
            designated = remaining_pool[0] if len(remaining_pool) == 1 else None
            return designated, list(remaining_pool)

        if is_custom_flow or self.mode == OrchestrationMode.FIXED:
            return remaining_pool[0], [remaining_pool[0]]

        start_set = set(self.start_chain)
        end_set = set(self.end_chain)

        # 2. When RebuttalAdvocate is currently speaking and middle pool specialists remain:
        # Allow nominating any remaining middle-pool specialist (Verdict B) or closing chain (Verdict A)
        if current_speaker and (current_speaker in standby_set or current_speaker == rebuttal_key):
            remaining_middle = [
                k for k in remaining_pool if k not in start_set and k not in end_set and k not in standby_set
            ]
            if remaining_middle:
                eligible = list(remaining_middle) + (self.end_chain[:1] if self.end_chain else [])
                return None, eligible

        middle_pool_set = {
            k for k in self.agents_map if k not in start_set and k not in end_set and k not in standby_set
        } if self.agents_map else {
            k for k in remaining_pool if k not in start_set and k not in end_set and k not in standby_set
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
