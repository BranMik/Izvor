"""
Pytest integration for E2E Hybrid Simulation Test Suite.
Allows running the scenarios via `pytest e2e_testing_harness/`.
Collects all scenarios from `ALL_SCENARIOS` and executes them using `E2ETestRunner`.
"""

import os
import pytest

from e2e_testing_harness.runner import E2ETestRunner
from e2e_testing_harness.scenarios import ALL_SCENARIOS, Scenario


@pytest.mark.anyio
@pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=[s.id for s in ALL_SCENARIOS])
async def test_scenario(scenario: Scenario, request):
    mocked = bool(
        request.config.getoption("hybrid_mocked_llm", default=False)
        if hasattr(request.config, "getoption")
        else False
    ) or bool(os.getenv("E2E_HYBRID_MOCKED_LLM"))
    runner = E2ETestRunner(mocked_llm=mocked, allow_all_live_llm=not mocked)
    result = await runner.run_scenario(scenario)
    assert result.passed, f"{scenario.id} ({scenario.name}) failed: {result.details}"
