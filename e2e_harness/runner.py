"""
E2E Behavioural Test Suite Runner.

Executes all hybrid simulation test scenarios and renders a structured verification report.
Supports dual-mode execution:
- Offline Simulation: fast, zero-delay in-memory test harness
- Live Slack Dispatch: streams authentic messages, Block Kit cards, avatars, and
  progress edits directly into a designated Slack test channel with configurable pacing.
Supports standalone CLI execution: `python -m e2e_harness.runner` or via `pytest`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
import sys
import time
from typing import Any
from unittest.mock import AsyncMock, patch

from core.config import SLACK_TEST_CHANNEL_ID
from e2e_harness.hybrid_interceptor import HybridInterceptor
from e2e_harness.protocol_client import SlackProtocolClient
from e2e_harness.scenarios import ALL_SCENARIOS, Scenario, ScenarioResult
from orchestration.agent_settings import (
    reset_enabled_agents,
    set_circuit_breaker_enabled,
    set_circuit_breaker_threshold,
)
from orchestration.thread_state import thread_state_manager

logger = logging.getLogger("e2e_harness.runner")


def get_next_test_run_prefix(run_id: str | None = None, run_num: int | None = None) -> str:
    """Computes a consistent test run identifier across all scenarios in one test invocation."""
    if run_id:
        return run_id.strip()
    if run_num is not None:
        return f"TEST_RUN_{run_num}"

    counter_file = Path(__file__).resolve().parent / ".test_run_counter"
    current_num = 0
    if counter_file.exists():
        try:
            current_num = int(counter_file.read_text().strip())
        except Exception:
            current_num = 0
    next_num = current_num + 1
    try:
        counter_file.write_text(str(next_num))
    except Exception:
        pass
    return f"TEST_RUN_{next_num}"


class E2ETestRunner:
    """Executes behavioral test cases with mock/live Slack client and hybrid LLM interceptor."""

    def __init__(
        self,
        allow_live_system_llm: bool = True,
        allow_all_live_llm: bool = False,
        live_slack: bool = False,
        live_channel_id: str | None = None,
        pacing: float | None = None,
        run_id: str | None = None,
        run_num: int | None = None,
    ) -> None:
        self.allow_live_system_llm = allow_live_system_llm
        self.allow_all_live_llm = allow_all_live_llm
        self.live_slack = live_slack
        self.live_channel_id = live_channel_id or (SLACK_TEST_CHANNEL_ID if live_slack else None)
        
        # Pacing: 1.0s default when live dispatch is active, 0.0s for offline simulation
        if pacing is not None:
            self.pacing = float(pacing)
        elif self.live_slack:
            self.pacing = 1.0
        else:
            self.pacing = 0.0

        self.run_prefix = get_next_test_run_prefix(run_id=run_id, run_num=run_num)
        self.interceptor = HybridInterceptor(
            allow_live_system_llm=allow_live_system_llm,
            allow_all_live_llm=allow_all_live_llm,
        )
        self.client = SlackProtocolClient(
            live_mode=self.live_slack,
            live_channel_id=self.live_channel_id,
        )

    async def run_scenario(self, scenario: Scenario) -> ScenarioResult:
        # Reset runtime state between tests
        self.client.bus.clear()
        reset_enabled_agents()
        set_circuit_breaker_enabled(False)
        set_circuit_breaker_threshold(1.50)
        thread_state_manager.reset_all()

        start = time.time()
        scenario_tag = f"{self.run_prefix}-[{scenario.id}]"

        # In live mode, initialize the root scenario card in the test channel
        if self.client.live_mode:
            await self.client.start_live_scenario(scenario_tag, scenario.name)

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
            patch("orchestration.stages.post_record.DEBATE_TURN_PACING_SECONDS", self.pacing),
            patch("orchestration.stages.post_record.SLACK_POST_PAUSE_SECONDS", self.pacing),
            patch("orchestration.stages.resolution.SLACK_POST_PAUSE_SECONDS", self.pacing),
        ]

        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)

            try:
                res = await scenario.run_fn(self.client)
                res.duration_seconds = time.time() - start
            except Exception as e:
                res = ScenarioResult(
                    id=scenario.id,
                    name=scenario.name,
                    passed=False,
                    details=f"Exception raised during execution: {e}",
                    duration_seconds=time.time() - start,
                )

            # In live mode, post completion card into the thread
            if self.client.live_mode:
                await self.client.finish_live_scenario(res)

            return res

    async def run_all(self, filter_id: str | None = None) -> list[ScenarioResult]:
        scenarios = ALL_SCENARIOS
        if filter_id:
            scenarios = [s for s in scenarios if s.id.lower() == filter_id.lower()]

        results: list[ScenarioResult] = []
        mode_desc = f"LIVE SLACK [Channel: {self.live_channel_id}, Pacing: {self.pacing}s]" if self.live_slack else "OFFLINE SIMULATION"
        llm_mode = "LIVE ALL LLM" if self.allow_all_live_llm else ("HYBRID" if self.allow_live_system_llm else "MOCK ONLY")
        print("\n" + "=" * 80)
        print("  CaiLogics SlackAgenticPanel — E2E Hybrid Simulation Test Suite")
        print("=" * 80)
        print(f"  Run Identifier:  {self.run_prefix}")
        print(f"  Execution Mode:  {mode_desc}")
        print(f"  LLM Pipeline:    {llm_mode}")
        print(f"  Total Scenarios: {len(scenarios)}\n")

        for sc in scenarios:
            scenario_tag = f"{self.run_prefix}-[{sc.id}]"
            sys.stdout.write(f"  {scenario_tag:22s} {sc.name:42s} ... ")
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
    parser.add_argument("--live-all-llm", action="store_true", help="Enables live LLM execution for all agents (including persona turns)")
    parser.add_argument("--live-slack", action="store_true", help="Enables live Slack dispatch to dedicated channel/thread")
    parser.add_argument("--channel", type=str, help="Target live Slack channel ID (defaults to SLACK_TEST_CHANNEL_ID in .env)")
    parser.add_argument("--pacing", type=float, help="Pacing delay in seconds between Slack posts (defaults to 1.0s in live mode, 0.0s in offline mode)")
    parser.add_argument("--run-num", type=int, help="Testing round sequence number for prefix (e.g. 1 for TEST_RUN_1)")
    parser.add_argument("--run-id", type=str, help="Custom testing round identifier prefix (e.g. TEST_RUN_1)")
    args = parser.parse_args()

    channel_id = args.channel or SLACK_TEST_CHANNEL_ID or None
    if args.live_slack and not channel_id:
        print("\n[ERROR] --live-slack requires a valid Slack channel ID. Please provide --channel <CHANNEL_ID> or set SLACK_TEST_CHANNEL_ID in your .env file.\n")
        sys.exit(1)

    runner = E2ETestRunner(
        allow_live_system_llm=not args.no_live_llm,
        allow_all_live_llm=args.live_all_llm,
        live_slack=args.live_slack,
        live_channel_id=channel_id,
        pacing=args.pacing,
        run_num=args.run_num,
        run_id=args.run_id,
    )
    results = asyncio.run(runner.run_all(filter_id=args.case))
    if any(not r.passed for r in results):
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
