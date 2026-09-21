"""
E2E Behavioural Test Suite Runner.

Executes all hybrid simulation test scenarios and renders a structured verification report.
Supports standalone CLI execution: `python -m e2e_harness.runner` or via `pytest`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any
from unittest.mock import AsyncMock, patch

from e2e_harness.hybrid_interceptor import HybridInterceptor, mock_execute_mcp_tool
from e2e_harness.protocol_client import SlackProtocolClient
from e2e_harness.scenarios import ALL_SCENARIOS, Scenario, ScenarioResult
from orchestration.agent_settings import (
    reset_enabled_agents,
    set_circuit_breaker_enabled,
    set_circuit_breaker_threshold,
)
from orchestration.thread_state import thread_state_manager


class E2ETestRunner:
    """Executes behavioral test cases with mock Slack client and hybrid LLM interceptor."""

    def __init__(self, allow_live_system_llm: bool = True) -> None:
        self.interceptor = HybridInterceptor(allow_live_system_llm=allow_live_system_llm)
        self.client = SlackProtocolClient()

    async def run_scenario(self, scenario: Scenario) -> ScenarioResult:
        # Reset runtime state between tests
        self.client.bus.clear()
        reset_enabled_agents()
        set_circuit_breaker_enabled(False)
        set_circuit_breaker_threshold(1.50)
        thread_state_manager.reset_all()

        start = time.time()
        from contextlib import ExitStack

        patches = [
            patch("services.slack_client.post_agent_response", self.client.mock_post_agent_response),
            patch("services.slack_client.update_slack_message", self.client.mock_update_slack_message),
            patch("services.slack_client.post_system_message", self.client.mock_post_system_message),
            patch("services.slack_client.post_ephemeral_to_slack", self.client.mock_post_ephemeral),
            patch("services.slack_client.upload_text_file_to_slack", self.client.mock_upload_file),
            patch("adapters.slack.interactivity.post_ephemeral_to_slack", self.client.mock_post_ephemeral),
            patch("adapters.slack.interactivity.upload_text_file_to_slack", self.client.mock_upload_file),
            patch("adapters.slack.interactivity.open_dm_channel", AsyncMock(return_value="D_TEST_DM")),
            patch("services.slack_client.publish_app_home", self.client.mock_publish_app_home),
            patch("services.slack_client.fetch_thread_history", self.client.mock_fetch_thread_history),
            patch("services.slack_client.fetch_thread_files", self.client.mock_fetch_thread_files),
            patch("orchestration.agent_router.post_agent_response", self.client.mock_post_agent_response),
            patch("orchestration.agent_router.post_system_message", self.client.mock_post_system_message),
            patch("orchestration.agent_router.fetch_thread_history", self.client.mock_fetch_thread_history),
            patch("orchestration.agent_router.fetch_thread_files", self.client.mock_fetch_thread_files),
            patch("orchestration.stages.validation.post_system_message", self.client.mock_post_system_message),
            patch("orchestration.stages.resolution.post_system_message", self.client.mock_post_system_message),
            patch("orchestration.stages.resolution.post_agent_response", self.client.mock_post_agent_response),
            patch("orchestration.stages.execution.post_agent_response", self.client.mock_post_agent_response),
            patch("orchestration.stages.post_record.post_agent_response", self.client.mock_post_agent_response),
            patch("orchestration.stages.post_record.update_slack_message", self.client.mock_update_slack_message),
            patch("core.slack_security.verify_slack_signature", lambda body, headers: True),
            patch("main.verify_slack_signature", lambda body, headers: True),
            patch("litellm.acompletion", self.interceptor.intercept_acompletion),
            patch("services.mcp_tools.web_search", lambda query, max_results=5: json.dumps({"results": [{"title": "Claude Pricing", "url": "https://anthropic.com/pricing", "snippet": "Claude 3.7 Sonnet: $3.00/$15.00"}]})),
            patch("orchestration.stages.post_record.DEBATE_TURN_PACING_SECONDS", 0.0),
            patch("orchestration.stages.post_record.SLACK_POST_PAUSE_SECONDS", 0.0),
            patch("orchestration.stages.resolution.SLACK_POST_PAUSE_SECONDS", 0.0),
        ]

        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)

            try:
                res = await scenario.run_fn(self.client)
                res.duration_seconds = time.time() - start
                return res
            except Exception as e:
                return ScenarioResult(
                    id=scenario.id,
                    name=scenario.name,
                    passed=False,
                    details=f"Exception raised during execution: {e}",
                    duration_seconds=time.time() - start,
                )

    async def run_all(self, filter_id: str | None = None) -> list[ScenarioResult]:
        scenarios = ALL_SCENARIOS
        if filter_id:
            scenarios = [s for s in scenarios if s.id.lower() == filter_id.lower()]

        results: list[ScenarioResult] = []
        print("\n" + "=" * 80)
        print("  CaiLogics SlackAgenticPanel — E2E Hybrid Simulation Test Suite")
        print("=" * 80)
        print(f"Total Scenarios to Execute: {len(scenarios)}\n")

        for sc in scenarios:
            sys.stdout.write(f"  [{sc.id}] {sc.name:45s} ... ")
            sys.stdout.flush()
            res = await self.run_scenario(sc)
            results.append(res)
            status_tag = "PASS" if res.passed else "FAIL"
            print(f"[{status_tag}] ({res.duration_seconds:.2f}s)")
            if not res.passed:
                print(f"         --> [WARN] {res.details}")

        print("\n" + "-" * 80)
        passed_count = sum(1 for r in results if r.passed)
        failed_count = len(results) - passed_count
        print(f"Execution Summary: {passed_count} PASSED, {failed_count} FAILED out of {len(results)} total.")
        print("-" * 80 + "\n")
        return results


def main() -> None:
    parser = argparse.ArgumentParser(description="E2E Hybrid Simulation Test Runner")
    parser.add_argument("--case", type=str, help="Specific Scenario ID to execute (e.g. TC-06)")
    parser.add_argument("--no-live-llm", action="store_true", help="Disables live LLM fallback for system agents")
    args = parser.parse_args()

    runner = E2ETestRunner(allow_live_system_llm=not args.no_live_llm)
    results = asyncio.run(runner.run_all(filter_id=args.case))
    if any(not r.passed for r in results):
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
