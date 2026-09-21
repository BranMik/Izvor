"""
Pytest integration for E2E Hybrid Simulation Test Suite.
Allows running the scenarios via `pytest e2e_harness/`.
"""

import pytest

from e2e_harness.runner import E2ETestRunner
from e2e_harness.scenarios import ALL_SCENARIOS, Scenario

runner = E2ETestRunner()


@pytest.mark.anyio
@pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=[s.id for s in ALL_SCENARIOS])
async def test_scenario(scenario: Scenario):
    result = await runner.run_scenario(scenario)
    assert result.passed, f"{scenario.id} ({scenario.name}) failed: {result.details}"
