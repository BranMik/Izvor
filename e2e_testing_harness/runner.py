"""
E2E Behavioural Test Suite Runner.

Executes all hybrid simulation test scenarios and renders a structured verification report.
Supports dual-mode execution:
- Offline Simulation: fast, zero-delay in-memory test harness
- Live Slack Dispatch: streams authentic messages, Block Kit cards, avatars, and
  progress edits directly into a designated Slack test channel with configurable pacing.
Supports standalone CLI execution: `python -m e2e_testing_harness.runner` or via `pytest`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

from core.config import SLACK_BOT_TOKEN, SLACK_TEST_CHANNEL_ID
from e2e_testing_harness.hybrid_interceptor import HybridInterceptor
from e2e_testing_harness.protocol_client import SlackProtocolClient
from e2e_testing_harness.scenarios import ALL_SCENARIOS, Scenario, ScenarioResult
from orchestration.agent_settings import (
    reset_enabled_agents,
    set_circuit_breaker_enabled,
    set_circuit_breaker_threshold,
    settings_manager,
)
from orchestration.thread_state import thread_state_manager

logger = logging.getLogger("e2e_testing_harness.runner")


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
        except (ValueError, OSError):
            current_num = 0
    next_num = current_num + 1
    try:
        counter_file.write_text(str(next_num))
    except OSError as e:
        logger.warning("Could not write test run counter to %s: %s", counter_file, e)
    return f"TEST_RUN_{next_num}"


def _mock_web_search(query: str, max_results: int = 5) -> str:
    """Fixture returning scenario-relevant search results (Claude pricing or Vector DBs)."""
    q = str(query or "").lower()
    if "vector" in q:
        return json.dumps({
            "results": [
                {"title": "Pinecone Vector Database", "url": "https://pinecone.io", "snippet": "Pinecone is a managed cloud vector database for AI similarity search."},
                {"title": "Qdrant Vector Search", "url": "https://qdrant.tech", "snippet": "Qdrant is an open-source vector search engine and database with payload filtering."},
                {"title": "Milvus Vector DB", "url": "https://milvus.io", "snippet": "Milvus is a distributed open-source vector database for massive similarity search."}
            ]
        })
    return json.dumps({
        "results": [
            {"title": "Claude Pricing", "url": "https://anthropic.com/pricing", "snippet": "Claude 3.7 Sonnet: $3.00 / MTok input, $15.00 / MTok output"}
        ]
    })


class E2ETestRunner:
    """Executes behavioral test cases with mock/live Slack client and hybrid LLM interceptor."""

    def __init__(
        self,
        allow_live_system_llm: bool = True,
        allow_all_live_llm: bool | None = None,
        mocked_llm: bool = False,
        live_slack: bool | None = None,
        live_channel_id: str | None = None,
        pacing: float | None = None,
        run_id: str | None = None,
        run_num: int | None = None,
        mock: bool = False,
    ) -> None:
        self.mock = mock
        if mock:
            self.live_slack = False
            self.allow_all_live_llm = False
            self.allow_live_system_llm = False
            self.mocked_llm = True
        else:
            if live_slack is not None:
                self.live_slack = live_slack
            else:
                self.live_slack = bool(
                    SLACK_TEST_CHANNEL_ID
                    and SLACK_BOT_TOKEN
                    and SLACK_BOT_TOKEN != "xoxb-your-bot-user-oauth-token"
                )

            if mocked_llm:
                effective_allow_all_live = False
            elif allow_all_live_llm is not None:
                effective_allow_all_live = allow_all_live_llm
            else:
                # Default: Live LLM execution for all agents
                effective_allow_all_live = True

            self.allow_live_system_llm = allow_live_system_llm
            self.allow_all_live_llm = effective_allow_all_live
            self.mocked_llm = not effective_allow_all_live

        self.live_channel_id = live_channel_id or (
            SLACK_TEST_CHANNEL_ID if self.live_slack else None
        )

        # Pacing: 1.0s default when live dispatch is active, 0.0s for offline simulation
        if pacing is not None:
            self.pacing = float(pacing)
        elif self.live_slack:
            self.pacing = 1.0
        else:
            self.pacing = 0.0

        self.run_prefix = get_next_test_run_prefix(run_id=run_id, run_num=run_num)
        self.interceptor = HybridInterceptor(
            allow_live_system_llm=self.allow_live_system_llm,
            allow_all_live_llm=self.allow_all_live_llm,
        )
        self.client = SlackProtocolClient(
            live_mode=self.live_slack,
            live_channel_id=self.live_channel_id,
        )
        os.environ["E2E_TESTING"] = "1"

    async def run_scenario(self, scenario: Scenario) -> ScenarioResult:
        # Reset runtime state between tests
        self.client.bus.clear()
        reset_enabled_agents()
        set_circuit_breaker_enabled(False)
        set_circuit_breaker_threshold(1.50)
        thread_state_manager.reset_all()

        start = time.time()
        scenario_tag = f"{self.run_prefix}-[{scenario.id}]"
        self.client.set_active_scenario(scenario_tag, scenario.name)

        from contextlib import ExitStack

        patches = [
            patch("core.slack_security.verify_slack_signature", lambda body, headers: True),
            patch("main.verify_slack_signature", lambda body, headers: True),
            patch("orchestration.stages.post_record.DEBATE_TURN_PACING_SECONDS", self.pacing),
            patch("orchestration.stages.post_record.SLACK_POST_PAUSE_SECONDS", self.pacing),
            patch("orchestration.stages.resolution.SLACK_POST_PAUSE_SECONDS", self.pacing),
            patch("adapters.slack.interactivity.open_dm_channel", AsyncMock(return_value="D_TEST_DM")),
            patch("services.flat_history_recorder.record_flat_history", lambda *args, **kwargs: None),
            patch("services.flat_history_recorder.async_record_flat_history", new_callable=AsyncMock),
            patch("orchestration.stages.post_record.record_flat_history", lambda *args, **kwargs: None),
        ]

        # In live mode with live LLMs, do NOT intercept LiteLLM calls
        if not self.mock and not self.mocked_llm and self.allow_all_live_llm:
            pass
        else:
            patches.extend([
                patch("litellm.acompletion", self.interceptor.intercept_acompletion),
                patch("litellm.completion", self.interceptor.intercept_completion),
            ])

        if self.mock:
            patches.append(patch("services.mcp_tools.web_search", _mock_web_search))

        # Always attach client tracking hooks so messages are recorded into self.client.bus
        patches.extend([
            patch("services.slack_client.post_agent_response", self.client.mock_post_agent_response),
            patch("services.slack_client.update_slack_message", self.client.mock_update_slack_message),
            patch("orchestration.stages.post_record.update_slack_message", self.client.mock_update_slack_message),
            patch("services.slack_client.post_system_message", self.client.mock_post_system_message),
            patch("services.slack_client.post_ephemeral_to_slack", self.client.mock_post_ephemeral),
            patch("services.slack_client.upload_text_file_to_slack", self.client.mock_upload_file),
            patch("adapters.slack.interactivity.post_ephemeral_to_slack", self.client.mock_post_ephemeral),
            patch("adapters.slack.interactivity.upload_text_file_to_slack", self.client.mock_upload_file),
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
            patch("services.slack_client.post_thread_note", self.client.mock_post_thread_note),
            patch("services.slack_client.update_thread_note", self.client.mock_update_thread_note),
            patch("agents.thread_notes_agent.post_thread_note", self.client.mock_post_thread_note),
            patch("agents.thread_notes_agent.update_thread_note", self.client.mock_update_thread_note),
            patch("agents.thread_notes_agent.fetch_thread_history", self.client.mock_fetch_thread_history),
        ])

        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)

            try:
                res = await scenario.run_fn(self.client)
                res.duration_seconds = time.time() - start
            except Exception as e:  # noqa: BLE001 - Test runner must catch all scenario exceptions to record failure without crashing
                res = ScenarioResult(
                    id=scenario.id,
                    name=scenario.name,
                    passed=False,
                    details=f"Exception raised during execution: {e}",
                    duration_seconds=time.time() - start,
                )

            # In live mode, allow any active background scribe task to finish before posting completion card
            if self.client.live_mode:
                try:
                    from agents.thread_notes_agent import _RUNNING_TASKS
                    target_thread = self.client.current_thread_ts
                    if target_thread:
                        target_key = (self.client.get_channel(), target_thread)
                        bg_task = _RUNNING_TASKS.get(target_key)
                        if bg_task and not bg_task.done():
                            await asyncio.wait_for(asyncio.shield(bg_task), timeout=8.0)
                except (TimeoutError, asyncio.CancelledError):
                    logger.debug("Background scribe task timed out or was cancelled during scenario completion.")
                except Exception as e:  # noqa: BLE001 - Resilient cleanup of background task
                    logger.warning("Unexpected error awaiting background scribe task: %s", e)
                await self.client.finish_live_scenario(res)

            return res

    async def run_all(self, filter_id: str | None = None) -> list[ScenarioResult]:
        scenarios = ALL_SCENARIOS
        if filter_id:
            allowed_ids = {x.strip().lower() for x in filter_id.split(",") if x.strip()}
            scenarios = [s for s in scenarios if s.id.lower() in allowed_ids]

        results: list[ScenarioResult] = []
        mode_desc = f"LIVE SLACK [Channel: {self.live_channel_id}, Pacing: {self.pacing}s]" if self.live_slack else "OFFLINE SIMULATION"
        llm_mode = "LIVE ALL LLM" if self.allow_all_live_llm else ("HYBRID" if self.allow_live_system_llm else "MOCK ONLY")
        print("\n" + "=" * 80)
        print("  CaiLogics SilvanAgora — E2E Hybrid Simulation Test Suite")
        print("=" * 80)
        print(f"  Run Identifier:  {self.run_prefix}")
        print(f"  Execution Mode:  {mode_desc}")
        print(f"  LLM Pipeline:    {llm_mode}")
        print(f"  Total Scenarios: {len(scenarios)}\n")

        initial_workflow = settings_manager.get_workflow_config()
        try:
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
        finally:
            if initial_workflow:
                settings_manager.set_workflow_config(initial_workflow)

        print("\n" + "-" * 80)
        passed_count = sum(1 for r in results if r.passed)
        failed_count = len(results) - passed_count
        print(f"Execution Summary: {passed_count} PASSED, {failed_count} FAILED out of {len(results)} total.")
        print("-" * 80 + "\n")
        return results


def main() -> None:
    parser = argparse.ArgumentParser(description="E2E Behavioural Test Suite Runner")
    parser.add_argument("--case", type=str, help="Specific Scenario ID to execute (e.g. TC-06)")
    parser.add_argument(
        "--mock",
        "--offline",
        dest="mock",
        action="store_true",
        default=False,
        help="Runs offline in-memory simulation without real Slack or LLM APIs (DEFAULT is live authentic execution)",
    )
    parser.add_argument(
        "--hybrid--mocked-llm",
        "--hybrid-mocked-llm",
        dest="hybrid_mocked_llm",
        action="store_true",
        help="Activates hybrid mocked LLM testing while keeping Slack live",
    )
    parser.add_argument("--no-live-llm", action="store_true", help="Disables live LLM fallback for system agents")
    parser.add_argument("--live-all-llm", action="store_true", default=True, help="Enables live LLM execution for all agents")
    parser.add_argument("--live-slack", action="store_true", default=None, help="Explicitly enables live Slack dispatch")
    parser.add_argument("--no-live-slack", action="store_true", default=False, help="Disables live Slack dispatch")
    parser.add_argument("--channel", type=str, help="Target live Slack channel ID (defaults to SLACK_TEST_CHANNEL_ID in .env)")
    parser.add_argument("--pacing", type=float, help="Pacing delay in seconds between Slack posts (defaults to 1.0s in live mode, 0.0s in offline mode)")
    parser.add_argument("--run-num", type=int, help="Testing round sequence number for prefix (e.g. 1 for TEST_RUN_1)")
    parser.add_argument("--run-id", type=str, help="Custom testing round identifier prefix (e.g. TEST_RUN_1)")
    args = parser.parse_args()

    channel_id = args.channel or SLACK_TEST_CHANNEL_ID or None

    if args.mock:
        live_slack = False
        use_mocked_llm = True
        allow_all_live = False
    else:
        if args.no_live_slack:
            live_slack = False
        elif args.live_slack is not None:
            live_slack = args.live_slack
        else:
            live_slack = bool(channel_id and SLACK_BOT_TOKEN and SLACK_BOT_TOKEN != "xoxb-your-bot-user-oauth-token")

        use_mocked_llm = args.hybrid_mocked_llm
        allow_all_live = not use_mocked_llm

    if live_slack and not channel_id:
        print("\n[ERROR] Live Slack mode requires a valid Slack channel ID. Please provide --channel <CHANNEL_ID> or set SLACK_TEST_CHANNEL_ID in your .env file, or pass --mock for offline simulation.\n")
        sys.exit(1)

    runner = E2ETestRunner(
        allow_live_system_llm=not args.no_live_llm and not args.mock,
        allow_all_live_llm=allow_all_live,
        mocked_llm=use_mocked_llm,
        live_slack=live_slack,
        live_channel_id=channel_id,
        pacing=args.pacing,
        run_num=args.run_num,
        run_id=args.run_id,
        mock=args.mock,
    )
    results = asyncio.run(runner.run_all(filter_id=args.case))
    if any(not r.passed for r in results):
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
