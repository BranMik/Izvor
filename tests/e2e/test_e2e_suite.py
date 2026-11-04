"""
Pytest integration for E2E Hybrid Simulation Test Suite.
Allows running the scenarios via `pytest tests/e2e/test_e2e_suite.py`.
Collects all scenarios from `ALL_SCENARIOS` and executes them using `E2ETestRunner`.
"""

import os

import pytest

from tests.e2e.runner import E2ETestRunner
from tests.e2e.scenarios import ALL_SCENARIOS, Scenario


@pytest.mark.anyio
@pytest.mark.parametrize("scenario", ALL_SCENARIOS, ids=[s.id for s in ALL_SCENARIOS])
async def test_scenario(scenario: Scenario, request):
    mock_opt = (
        request.config.getoption("mock")
        if hasattr(request.config, "getoption")
        else False
    )
    mock_env = os.getenv("E2E_MOCK", "1").strip().lower()
    mock = mock_opt or (mock_env not in ("0", "false", "no"))

    llm_opt = (
        request.config.getoption("hybrid_mocked_llm")
        if hasattr(request.config, "getoption")
        else False
    )
    llm_env = os.getenv("E2E_HYBRID_MOCKED_LLM", "1").strip().lower()
    mocked_llm = llm_opt or (llm_env not in ("0", "false", "no"))
    runner = E2ETestRunner(mock=mock, mocked_llm=mocked_llm)
    result = await runner.run_scenario(scenario)
    assert result.passed, f"{scenario.id} ({scenario.name}) failed: {result.details}"
