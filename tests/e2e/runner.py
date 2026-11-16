"""
E2E Behavioural Test Suite Runner.

Executes all hybrid simulation test scenarios and renders a structured verification report.
Supports dual-mode execution:
- Live LLM (DEFAULT): authentic LLM execution for all agents
- Offline Simulation (`--mock`): fast, deterministic in-memory test harness via pure DI hooks
- Live Slack Dispatch: streams authentic messages, Block Kit cards, avatars, and
  progress edits directly into a designated Slack test channel with configurable pacing.
Supports standalone CLI execution: `python -m tests.e2e.runner` or via `pytest`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

_root_dir = str(Path(__file__).resolve().parents[2])
_engine_dir = str(Path(__file__).resolve().parents[2] / "engine")
if _root_dir not in sys.path:
    sys.path.insert(0, _root_dir)
if _engine_dir not in sys.path:
    sys.path.insert(0, _engine_dir)

import main  # noqa: F401,E402 - Ensures FastAPI app and IoC hooks initialize
from agents.usage_agent import usage_tracker  # noqa: E402
from core.config import (  # noqa: E402
    SLACK_BOT_TOKEN,
    SLACK_TEST_CHANNEL_ID,
    configure_token,
    reset_token,
    set_admin_slack_user_ids_override,
)
from core.llm_utils import (  # noqa: E402
    configure_llm_completion_fn,
    configure_llm_sleep_fn,
    reset_llm_completion_fn,
    reset_llm_sleep_fn,
)
from orchestration.agent_router import (  # noqa: E402
    configure_router_sleep_fn,
    reset_router_deps,
    reset_router_sleep_fn,
)
from orchestration.agent_settings import (  # noqa: E402
    reset_enabled_agents,
    settings_manager,
)
from orchestration.app_settings import (  # noqa: E402
    set_circuit_breaker_enabled,
    set_circuit_breaker_threshold,
)
from orchestration.system_settings import system_settings_manager  # noqa: E402
from orchestration.thread_skills_manager import thread_skills_manager  # noqa: E402
from orchestration.thread_state import thread_state_manager  # noqa: E402
from services.mcp.client_manager import mcp_client_manager  # noqa: E402
from services.slack import (  # noqa: E402
    configure_fetch_thread_files,
    configure_fetch_thread_history,
    configure_post_agent_response,
    configure_post_system_message,
    configure_slack_async_client,
    reset_fetch_thread_files,
    reset_fetch_thread_history,
    reset_post_agent_response,
    reset_post_system_message,
    reset_slack_async_client,
)
from services.web_tools import (  # noqa: E402
    configure_web_search_fn,
    reset_web_fetch_fn,
    reset_web_search_fn,
)
from tests.e2e.hybrid_interceptor import HybridInterceptor  # noqa: E402
from tests.e2e.protocol_client import (  # noqa: E402
    TEST_ADMIN_USER_ID,
    SlackProtocolClient,
)
from tests.e2e.scenarios import ALL_SCENARIOS, Scenario, ScenarioResult  # noqa: E402

logger = logging.getLogger("tests.e2e.runner")


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


async def _noop_sleep(*args: Any, **kwargs: Any) -> None:
    return None


async def _mock_web_search(query: str, max_results: int = 5) -> str:
    """Fixture returning scenario-relevant search results (Claude pricing or Vector DBs)."""
    q = str(query or "").lower()
    if "vector" in q:
        return json.dumps({
            "results": [
                {
                    "title": "Pinecone Vector Database",
                    "url": "https://pinecone.io",
                    "snippet": "Pinecone is a managed cloud vector database for AI similarity search.",
                },
                {
                    "title": "Qdrant Vector Search",
                    "url": "https://qdrant.tech",
                    "snippet": "Qdrant is an open-source vector search engine and database with payload filtering.",
                },
                {
                    "title": "Milvus Vector DB",
                    "url": "https://milvus.io",
                    "snippet": "Milvus is a distributed open-source vector database for massive similarity search.",
                },
            ]
        })
    return json.dumps({
        "results": [
            {
                "title": "Claude Pricing",
                "url": "https://anthropic.com/pricing",
                "snippet": "Claude 3.7 Sonnet: $3.00 / MTok input, $15.00 / MTok output",
            }
        ]
    })


@contextmanager
def _standalone_cli_isolation() -> Iterator[None]:
    """
    Provides temporary path and database isolation when runner.py is invoked directly
    via CLI outside pytest (where tests/conftest.py autouse fixtures are absent).
    """
    if os.getenv("PYTEST_CURRENT_TEST"):
        yield
        return

    from core.database import db_manager
    from core.paths import AGENTS_RESOURCES_DIR, PRESIDIO_SKILL_EXCLUSIONS_FILE, PROJECT_ROOT
    from orchestration.agent_settings import (
        BACKUP_FILE as REAL_BACKUP_FILE,
        DEFAULT_SYSTEM_AGENT_SETTINGS_FILE,
        SETTINGS_FILE as REAL_SETTINGS_FILE,
        SYSTEM_AGENT_BACKUP_FILE as REAL_SYS_AGENT_BACKUP_FILE,
        SYSTEM_AGENT_SETTINGS_FILE as REAL_SYS_AGENT_SETTINGS_FILE,
    )
    from orchestration.app_settings import (
        APP_BACKUP_FILE as REAL_APP_BACKUP_FILE,
        APP_SETTINGS_FILE as REAL_APP_SETTINGS_FILE,
        app_settings_manager,
    )
    from orchestration.system_settings import (
        SYSTEM_BACKUP_FILE as REAL_SYS_BACKUP_FILE,
        SYSTEM_SETTINGS_FILE as REAL_SYS_SETTINGS_FILE,
    )
    from services.audit_logger import audit_logger
    from services.flat_history_recorder import (
        configure_paths as configure_flat_paths,
        reset_paths as reset_flat_paths,
    )
    from services.local_tools import (
        configure_paths as configure_local_tools_paths,
        reset_paths as reset_local_tools_paths,
    )
    from services.pricing_service import (
        PRICING_CACHE_FILE as REAL_PRICING_CACHE_FILE,
        pricing_service,
    )
    from services.rag.config import EMBEDDING_DIM
    from services.rag.embeddings import EmbeddingService
    from services.security.guardrail import guardrail_service
    from services.security.pii_masker import pii_masker
    from services.thread_notes_writer import (
        configure_paths as configure_note_paths,
        reset_paths as reset_note_paths,
    )

    with tempfile.TemporaryDirectory(prefix="izvor_e2e_cli_") as tmp_str:
        iso = Path(tmp_str)
        audit_dir = iso / "audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        temp_flat = iso / "flat-histories"
        temp_flat.mkdir(parents=True, exist_ok=True)
        temp_notes = iso / "thread_notes"
        temp_notes.mkdir(parents=True, exist_ok=True)
        temp_res = iso / "agents_resources"
        temp_res.mkdir(parents=True, exist_ok=True)
        sys_spec = AGENTS_RESOURCES_DIR / "sys_spec.txt"
        if sys_spec.exists():
            shutil.copy2(sys_spec, temp_res / "sys_spec.txt")

        temp_pricing = iso / "model_pricing_cache.json"
        if REAL_PRICING_CACHE_FILE.exists():
            shutil.copy2(REAL_PRICING_CACHE_FILE, temp_pricing)

        temp_settings = iso / "agent_settings.json"
        temp_backup = iso / "agent_settings_backup.json"
        temp_sys_ag = iso / "system_agent_settings.json"
        temp_sys_ag_bak = iso / "system_agent_settings_backup.json"
        if REAL_SETTINGS_FILE.exists():
            shutil.copy2(REAL_SETTINGS_FILE, temp_settings)
        if REAL_BACKUP_FILE.exists():
            shutil.copy2(REAL_BACKUP_FILE, temp_backup)
        if REAL_SYS_AGENT_SETTINGS_FILE.exists():
            shutil.copy2(REAL_SYS_AGENT_SETTINGS_FILE, temp_sys_ag)
        elif DEFAULT_SYSTEM_AGENT_SETTINGS_FILE.exists():
            shutil.copy2(DEFAULT_SYSTEM_AGENT_SETTINGS_FILE, temp_sys_ag)
        if REAL_SYS_AGENT_BACKUP_FILE.exists():
            shutil.copy2(REAL_SYS_AGENT_BACKUP_FILE, temp_sys_ag_bak)
        elif DEFAULT_SYSTEM_AGENT_SETTINGS_FILE.exists():
            shutil.copy2(DEFAULT_SYSTEM_AGENT_SETTINGS_FILE, temp_sys_ag_bak)

        temp_wf = iso / "workflows"
        temp_wf.mkdir(parents=True, exist_ok=True)
        real_wf = settings_manager.workflow_manager.workflows_dir
        if real_wf.exists():
            for wf in real_wf.glob("*.json"):
                shutil.copy2(wf, temp_wf / wf.name)

        temp_app = iso / "app_settings.json"
        temp_app_bak = iso / "app_settings_backup.json"
        if REAL_APP_SETTINGS_FILE.exists():
            shutil.copy2(REAL_APP_SETTINGS_FILE, temp_app)
        if REAL_APP_BACKUP_FILE.exists():
            shutil.copy2(REAL_APP_BACKUP_FILE, temp_app_bak)

        temp_sys = iso / "system_settings.json"
        temp_sys_bak = iso / "system_settings_backup.json"
        if REAL_SYS_SETTINGS_FILE.exists():
            shutil.copy2(REAL_SYS_SETTINGS_FILE, temp_sys)
        if REAL_SYS_BACKUP_FILE.exists():
            shutil.copy2(REAL_SYS_BACKUP_FILE, temp_sys_bak)

        temp_excl = iso / "presidio_skill_exclusions.json"
        if PRESIDIO_SKILL_EXCLUSIONS_FILE.exists():
            shutil.copy2(PRESIDIO_SKILL_EXCLUSIONS_FILE, temp_excl)

        async def _dummy_embeddings(texts: list[str]) -> list[list[float]]:
            return [[0.0] * EMBEDDING_DIM for _ in texts] if texts else []

        temp_lancedb = iso / "lancedb"
        temp_lancedb.mkdir(parents=True, exist_ok=True)

        audit_logger.configure_paths(audit_dir=audit_dir)
        pricing_service.configure_paths(cache_file=temp_pricing)
        settings_manager.configure_paths(
            settings_file=temp_settings,
            backup_file=temp_backup,
            system_agent_settings_file=temp_sys_ag,
            system_agent_backup_file=temp_sys_ag_bak,
            workflows_dir=temp_wf,
        )
        app_settings_manager.configure_paths(settings_file=temp_app, backup_file=temp_app_bak)
        system_settings_manager.configure_paths(settings_file=temp_sys, backup_file=temp_sys_bak)
        pii_masker.configure_paths(exclusions_file=temp_excl)
        db_manager.configure_paths(db_path=iso / "e2e_cli_izvor.db")
        configure_flat_paths(temp_flat)
        configure_note_paths(temp_notes)
        configure_local_tools_paths(workspace_dir=PROJECT_ROOT, agents_resources_dir=temp_res)
        guardrail_service.configure_paths(db_dir=temp_lancedb)
        EmbeddingService.configure_embeddings_fn(_dummy_embeddings)
        try:
            yield
        finally:
            EmbeddingService.reset_embeddings_fn()
            guardrail_service.reset_paths()
            reset_local_tools_paths()
            reset_note_paths()
            reset_flat_paths()
            db_manager.reset_paths()
            pii_masker.reset_paths()
            system_settings_manager.reset_paths()
            app_settings_manager.reset_paths()
            settings_manager.reset_paths()
            pricing_service.reset_paths()
            audit_logger.reset_paths()


class E2ETestRunner:
    """Executes behavioral test cases with pure DI Slack/tool doubles and optional LLM interception."""

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
                self.live_slack = os.getenv("E2E_LIVE_SLACK", "0").strip().lower() in (
                    "1",
                    "true",
                    "yes",
                )

            if mocked_llm:
                effective_allow_all_live = False
            elif allow_all_live_llm is not None:
                effective_allow_all_live = allow_all_live_llm
            else:
                effective_allow_all_live = True

            self.allow_live_system_llm = allow_live_system_llm
            self.allow_all_live_llm = effective_allow_all_live
            self.mocked_llm = not effective_allow_all_live

        self.live_channel_id = live_channel_id or (
            SLACK_TEST_CHANNEL_ID if self.live_slack else None
        )

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
        # Attach runtime metadata for scenarios that inspect or adjust mode-specific fixtures
        self.client.interceptor = self.interceptor  # type: ignore[attr-defined]
        self.client.mock = self.mock  # type: ignore[attr-defined]
        self.client.mocked_llm = self.mocked_llm  # type: ignore[attr-defined]
        os.environ["E2E_TESTING"] = "1"

    async def aclose(self) -> None:
        await self.client.aclose()

    async def run_scenario(self, scenario: Scenario) -> ScenarioResult:
        with _standalone_cli_isolation():
            return await self._run_scenario_isolated(scenario)

    async def _run_scenario_isolated(self, scenario: Scenario) -> ScenarioResult:
        from adapters.slack.events import PROCESSED_EVENTS

        PROCESSED_EVENTS.clear()
        self.client.bus.clear()
        self.client.slack_client_double.reset_faults()
        self.interceptor.reset_flags()
        self.interceptor.call_history.clear()
        reset_enabled_agents()
        set_circuit_breaker_enabled(False, save=False)
        set_circuit_breaker_threshold(1.50, save=False)
        thread_state_manager.reset_all()
        thread_skills_manager.clear_all()
        usage_tracker.clear_all()
        mcp_client_manager.reset_transport_factory()
        reset_router_deps()

        start = time.time()
        scenario_tag = f"{self.run_prefix}-[{scenario.id}]"
        self.client.set_active_scenario(scenario_tag, scenario.name)

        orig_pacing = system_settings_manager.debate_turn_pacing_seconds
        orig_pause = system_settings_manager.slack_post_pause_seconds
        orig_signing_secret = os.environ.get("SLACK_SIGNING_SECRET")

        try:
            # Ensure authentic HMAC-SHA256 verification succeeds when no real signing secret is in env
            if not orig_signing_secret or orig_signing_secret == "your-slack-signing-secret":
                os.environ["SLACK_SIGNING_SECRET"] = self.client.signing_secret

            system_settings_manager.set_debate_turn_pacing_seconds(self.pacing, save=False)
            system_settings_manager.set_slack_post_pause_seconds(self.pacing, save=False)
            set_admin_slack_user_ids_override({TEST_ADMIN_USER_ID})

            # Pure DI outbound Slack & history/files hooks (always install bus spy; forwards live when live_slack=True)
            configure_post_agent_response(self.client.mock_post_agent_response)
            configure_post_system_message(self.client.mock_post_system_message)
            configure_fetch_thread_history(self.client.mock_fetch_thread_history)
            configure_fetch_thread_files(self.client.mock_fetch_thread_files)
            configure_slack_async_client(self.client.slack_client_double)

            if not self.live_slack:
                configure_token("xoxb-e2e-simulated-token")
                configure_router_sleep_fn(_noop_sleep)

            if self.mock or self.mocked_llm or not self.allow_all_live_llm:
                configure_llm_completion_fn(self.interceptor.intercept_acompletion)
            if self.mock:
                configure_llm_sleep_fn(_noop_sleep)
                configure_web_search_fn(_mock_web_search)

            try:
                res = await scenario.run_fn(self.client)
                res.duration_seconds = time.time() - start
            except Exception as e:  # noqa: BLE001 - Runner must record scenario failures cleanly
                res = ScenarioResult(
                    id=scenario.id,
                    name=scenario.name,
                    passed=False,
                    details=f"Exception raised during execution: {e}",
                    duration_seconds=time.time() - start,
                )

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
                    logger.debug("Background scribe task timed out or cancelled during scenario completion.")
                except Exception as e:  # noqa: BLE001
                    logger.warning("Unexpected error awaiting background scribe task: %s", e)
                await self.client.finish_live_scenario(res)

            return res
        finally:
            await self.client.drain_background_tasks()
            if orig_signing_secret is None:
                os.environ.pop("SLACK_SIGNING_SECRET", None)
            else:
                os.environ["SLACK_SIGNING_SECRET"] = orig_signing_secret

            system_settings_manager.set_debate_turn_pacing_seconds(orig_pacing, save=False)
            system_settings_manager.set_slack_post_pause_seconds(orig_pause, save=False)
            set_circuit_breaker_enabled(True, save=False)
            set_admin_slack_user_ids_override(None)
            reset_post_agent_response()
            reset_post_system_message()
            reset_fetch_thread_history()
            reset_fetch_thread_files()
            reset_slack_async_client()
            reset_token()
            reset_llm_completion_fn()
            reset_llm_sleep_fn()
            reset_router_sleep_fn()
            reset_router_deps()
            reset_web_search_fn()
            reset_web_fetch_fn()
            mcp_client_manager.reset_transport_factory()

    async def run_all(self, filter_id: str | None = None) -> list[ScenarioResult]:
        scenarios = ALL_SCENARIOS
        if filter_id:
            allowed_ids = {x.strip().lower() for x in filter_id.split(",") if x.strip()}
            scenarios = [s for s in scenarios if s.id.lower() in allowed_ids]

        results: list[ScenarioResult] = []
        mode_desc = (
            f"LIVE SLACK [Channel: {self.live_channel_id}, Pacing: {self.pacing}s]"
            if self.live_slack
            else "OFFLINE SIMULATION"
        )
        llm_mode = (
            "LIVE ALL LLM"
            if self.allow_all_live_llm
            else ("HYBRID" if self.allow_live_system_llm else "MOCK ONLY")
        )
        print("\n" + "=" * 80)
        print("  Izvor — E2E Hybrid Simulation Test Suite")
        print("=" * 80)
        print(f"  Run Identifier:  {self.run_prefix}")
        print(f"  Execution Mode:  {mode_desc}")
        print(f"  LLM Pipeline:    {llm_mode}")
        print(f"  Total Scenarios: {len(scenarios)}\n")

        initial_workflow = settings_manager.get_workflow_config()
        try:
            for sc in scenarios:
                scenario_tag = f"{self.run_prefix}-[{sc.id}]"
                sys.stdout.write(f"  {scenario_tag:22s} {sc.name:44s} ... ")
                sys.stdout.flush()
                res = await self.run_scenario(sc)
                results.append(res)
                status_tag = "PASS" if res.passed else "FAIL"
                print(f"[{status_tag}] ({res.duration_seconds:.2f}s)")
                if not res.passed:
                    print(f"         --> [WARN] {res.details}")
        finally:
            await self.aclose()
            if initial_workflow:
                settings_manager.set_workflow_config(initial_workflow)

        print("\n" + "-" * 80)
        passed_count = sum(1 for r in results if r.passed)
        failed_count = len(results) - passed_count
        print(
            f"Execution Summary: {passed_count} PASSED, {failed_count} FAILED out of {len(results)} total."
        )
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
    parser.add_argument(
        "--no-live-llm",
        action="store_true",
        help="Disables live LLM fallback for system agents",
    )
    parser.add_argument(
        "--live-all-llm",
        action="store_true",
        default=True,
        help="Enables live LLM execution for all agents",
    )
    parser.add_argument(
        "--live-slack",
        action="store_true",
        default=None,
        help="Explicitly enables live Slack dispatch",
    )
    parser.add_argument(
        "--no-live-slack",
        action="store_true",
        default=False,
        help="Disables live Slack dispatch",
    )
    parser.add_argument(
        "--channel",
        type=str,
        help="Target live Slack channel ID (defaults to SLACK_TEST_CHANNEL_ID in .env)",
    )
    parser.add_argument(
        "--pacing",
        type=float,
        help="Pacing delay in seconds between Slack posts (defaults to 1.0s in live mode, 0.0s in offline mode)",
    )
    parser.add_argument(
        "--run-num",
        type=int,
        help="Testing round sequence number for prefix (e.g. 1 for TEST_RUN_1)",
    )
    parser.add_argument(
        "--run-id",
        type=str,
        help="Custom testing round identifier prefix (e.g. TEST_RUN_1)",
    )
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
            live_slack = os.getenv("E2E_LIVE_SLACK", "0").strip().lower() in (
                "1",
                "true",
                "yes",
            )

        use_mocked_llm = args.hybrid_mocked_llm
        allow_all_live = not use_mocked_llm

    if live_slack and not channel_id:
        print(
            "\n[ERROR] Live Slack mode requires a valid Slack channel ID. "
            "Please provide --channel <CHANNEL_ID> or set SLACK_TEST_CHANNEL_ID in your .env file, or pass --mock for offline simulation.\n"
        )
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
