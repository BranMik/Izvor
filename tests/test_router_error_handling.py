from unittest.mock import AsyncMock, MagicMock

import pytest

from core.config import AGENTS_CONFIG
from core.llm_utils import (
    classify_agent_error,
    execute_llm_with_retry,
    is_high_demand_503_error,
)
from orchestration import agent_router
from orchestration.agent_router import process_and_respond
from orchestration.agent_settings import (
    reset_enabled_agents,
    set_fixed_config,
    set_orchestration_mode,
    set_sandwich_config,
)
from orchestration.thread_state import thread_state_manager


@pytest.fixture(autouse=True)
def setup_test_environment(monkeypatch):
    reset_enabled_agents()
    set_orchestration_mode("sandwich")
    set_sandwich_config([], [])
    set_fixed_config([])
    thread_state_manager.reset_all()
    monkeypatch.setattr(agent_router.asyncio, "sleep", AsyncMock(return_value=None))


def test_classify_agent_error():
    # Rate limit errors
    assert (
        classify_agent_error(
            Exception("Rate limit reached for requests per minute (RPM)")
        )
        == "rate_limit"
    )
    assert classify_agent_error(Exception("429 Too Many Requests")) == "rate_limit"
    assert (
        classify_agent_error(Exception("ResourceExhausted quota exceeded"))
        == "rate_limit"
    )

    # Service unavailable / 503 / High demand spike errors
    assert (
        classify_agent_error(Exception("503 Service Unavailable"))
        == "service_unavailable"
    )
    assert classify_agent_error(Exception("529 Overloaded")) == "service_unavailable"
    assert (
        classify_agent_error(Exception("The model is overloaded due to high demand"))
        == "service_unavailable"
    )
    assert (
        classify_agent_error(
            Exception("ServiceUnavailableError: upstream connect error or disconnect")
        )
        == "service_unavailable"
    )
    assert (
        classify_agent_error(
            Exception("Server temporarily overloaded (high demand spike)")
        )
        == "service_unavailable"
    )
    assert is_high_demand_503_error(Exception("503 Service Unavailable")) is True
    assert is_high_demand_503_error(Exception("429 Too Many Requests")) is False

    # API key errors
    assert (
        classify_agent_error(Exception("Authentication error: api_key not found"))
        == "api_key_not_found"
    )
    assert (
        classify_agent_error(
            Exception("No API key provided. Please set GEMINI_API_KEY")
        )
        == "api_key_not_found"
    )
    assert (
        classify_agent_error(Exception("Unauthorized: 401 Invalid Token"))
        == "api_key_not_found"
    )

    # Model not found errors
    assert (
        classify_agent_error(
            Exception("Model 'mistral/mistral-nonexistent' not found (404)")
        )
        == "model_not_found"
    )
    assert (
        classify_agent_error(Exception("The model `xyz` does not exist"))
        == "model_not_found"
    )

    # General error
    assert (
        classify_agent_error(Exception("Connection reset by peer")) == "general_error"
    )


@pytest.mark.anyio
async def test_process_and_respond_api_key_not_found(monkeypatch):
    thread_state_manager.reset_all()

    async def mock_run_in_threadpool(func, *args, **kwargs):
        raise Exception("AuthenticationError: Missing API key for Gemini")

    posted = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted.append({"text": text, "username": username, "icon_url": icon_url})
        return {"ok": True, "ts": "12345.0001"}

    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_run_in_threadpool
    )
    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent_response
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0001",
        raw_text="Hello, help me design a backend architecture",
    )

    assert len(posted) == 1
    # Verify posted as responsible agent persona
    assert posted[0]["username"] in [v["username"] for v in AGENTS_CONFIG.values()]
    # Verify single sentence statement
    assert "API KEY NOT FOUND" in posted[0]["text"]
    # Verify next nomination exists so discussion flows
    assert "[" in posted[0]["text"]


@pytest.mark.anyio
async def test_process_and_respond_model_not_found(monkeypatch):
    thread_state_manager.reset_all()

    thread_key = ("C123", "12345.0002")
    thread_state_manager.remaining_agents_map[thread_key] = ["Innovator"]

    async def mock_run_in_threadpool(func, *args, **kwargs):
        raise Exception("NotFoundError: Model gemini/gemini-3.8-flash does not exist")

    posted = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted.append({"text": text, "username": username, "icon_url": icon_url})
        return {"ok": True, "ts": "12345.0002"}

    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_run_in_threadpool
    )
    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent_response
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0002",
        raw_text="[TechLead] Analyze this code",
        is_bot_trigger=True,
    )

    assert len(posted) == 1
    assert posted[0]["username"] == "TechLead"
    assert "MODEL NOT FOUND" in posted[0]["text"]
    assert "[Innovator]" in posted[0]["text"]


@pytest.mark.anyio
async def test_process_and_respond_retries_and_clean_error_message(monkeypatch):
    thread_state_manager.reset_all()

    thread_key = ("C123", "12345.9999")
    thread_state_manager.remaining_agents_map[thread_key] = ["Innovator"]

    call_count = 0

    async def mock_fail_general_error(func, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise Exception("Internal Server Error: Unexpected JSON decoding crash")

    posted = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted.append({"text": text, "username": username, "icon_url": icon_url})
        return {"ok": True, "ts": "12345.9999"}

    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_fail_general_error
    )
    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent_response
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.9999",
        raw_text="[TechLead] Analyze this code",
        is_bot_trigger=True,
    )

    # Must have retried 3 times
    assert call_count == 3
    assert len(posted) == 1
    assert posted[0]["username"] == "TechLead"
    # Clean error message without raw traceback
    assert "Error processing request - check the logs" in posted[0]["text"]
    assert "Unexpected JSON decoding crash" not in posted[0]["text"]
    # Continues thread to next agent
    assert "[Innovator]" in posted[0]["text"]


@pytest.mark.anyio
async def test_process_and_respond_rate_limits_and_retry_flow(monkeypatch):
    thread_state_manager.reset_all()

    thread_key = ("C123", "12345.0003")

    # Set up specific short flow: TechLead -> Innovator -> (retry TechLead) -> Architect
    thread_state_manager.remaining_agents_map[thread_key] = ["Innovator"]

    posted = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted.append({"text": text, "username": username, "icon_url": icon_url})
        return {"ok": True, "ts": "12345.0003"}

    # 1. First run of TechLead encounters 429 RateLimitError
    async def mock_fail_rate_limit(func, *args, **kwargs):
        raise Exception("RateLimitError: 429 Too Many Requests, quota exceeded")

    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_fail_rate_limit
    )
    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent_response
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0003",
        raw_text="[TechLead] Database architecture review",
        is_bot_trigger=True,
    )

    assert len(posted) == 1
    assert posted[0]["username"] == "TechLead"
    assert (
        "RATE LIMITS CROSSED, will retry at the end of discussion" in posted[0]["text"]
    )
    assert "[Innovator]" in posted[0]["text"]
    assert thread_key in thread_state_manager.rate_limit_retry_queue
    assert "TechLead" in thread_state_manager.rate_limit_retry_queue[thread_key]

    # 2. Innovator runs normally, remaining is empty -> nominates retry for TechLead!
    mock_choice = MagicMock()
    mock_choice.message.content = "My innovator perspective."
    mock_choice.message.tool_calls = None
    mock_resp = MagicMock(choices=[mock_choice], usage=None)

    async def mock_success_innovator(func, *args, **kwargs):
        return mock_resp

    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_success_innovator
    )

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0003",
        raw_text="[Innovator] Your turn",
        is_bot_trigger=True,
    )

    assert len(posted) == 2
    assert posted[1]["username"] == "Innovator"
    assert "[TechLead]" in posted[1]["text"]
    # Verify TechLead is now marked as already retried once
    assert "TechLead" in thread_state_manager.rate_limit_already_retried[thread_key]

    # 3. TechLead retries at the end of discussion, fails again with RateLimit:
    # Must post 'RATE LIMITS CROSSED' and NOT be retried again.
    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_fail_rate_limit
    )

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0003",
        raw_text="[TechLead] Retry turn",
        is_bot_trigger=True,
    )

    assert len(posted) == 3
    assert posted[2]["username"] == "TechLead"
    # Should say RATE LIMITS CROSSED without 'will retry'
    assert "RATE LIMITS CROSSED" in posted[2]["text"]
    assert "will retry" not in posted[2]["text"]
    # Should NOT be added back to RATE_LIMIT_RETRY_QUEUE
    assert "TechLead" not in thread_state_manager.rate_limit_retry_queue.get(thread_key, [])
    # Clean termination without forced closing nomination tag
    assert "[Architect]" not in posted[2]["text"]


@pytest.mark.anyio
async def test_process_and_respond_503_high_demand_first_skip_and_random_handoff(
    monkeypatch,
):
    thread_state_manager.reset_all()

    thread_key = ("C_503", "12345.5031")
    # Remaining agents list before TechLead speaks: Innovator, DeepSeek_Challenger
    thread_state_manager.remaining_agents_map[thread_key] = ["Innovator", "DeepSeek_Challenger"]

    posted = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted.append({"text": text, "username": username, "icon_url": icon_url})
        return {"ok": True, "ts": "12345.5031"}

    # Simulate 503 High Demand error
    async def mock_fail_503(func, *args, **kwargs):
        raise Exception(
            "ServiceUnavailableError: 503 The server is currently overloaded with high demand"
        )

    monkeypatch.setattr("orchestration.agent_router.run_in_threadpool", mock_fail_503)
    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent_response
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )

    await process_and_respond(
        channel_id="C_503",
        thread_ts="12345.5031",
        raw_text="[TechLead] Analyze backend structure",
        is_bot_trigger=True,
    )

    assert len(posted) == 1
    assert posted[0]["username"] == "TechLead"
    assert (
        "HIGH DEMAND / SERVICE OVERLOADED (503) - Skipping for now, will retry later"
        in posted[0]["text"]
    )
    # Verify TechLead is recorded as 503-errored for this thread
    assert thread_key in thread_state_manager.high_demand_errored_agents
    assert "TechLead" in thread_state_manager.high_demand_errored_agents[thread_key]

    # Verify TechLead is kept in REMAINING_AGENTS_MAP
    assert "TechLead" in thread_state_manager.remaining_agents_map[thread_key]
    assert "Innovator" in thread_state_manager.remaining_agents_map[thread_key]
    assert "DeepSeek_Challenger" in thread_state_manager.remaining_agents_map[thread_key]

    # Verify next nomination is one of the other candidates
    nominated_tag = None
    if "[Innovator]" in posted[0]["text"]:
        nominated_tag = "Innovator"
    elif "[DeepSeek_Challenger]" in posted[0]["text"]:
        nominated_tag = "DeepSeek_Challenger"
    assert nominated_tag in ["Innovator", "DeepSeek_Challenger"]


@pytest.mark.anyio
async def test_process_and_respond_503_high_demand_second_query_retries_and_terminates(
    monkeypatch,
):
    thread_state_manager.reset_all()

    thread_key = ("C_503", "12345.5032")
    # TechLead already encountered 503 once
    thread_state_manager.high_demand_errored_agents[thread_key] = {"TechLead"}
    thread_state_manager.remaining_agents_map[thread_key] = []  # No other agents left

    posted = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted.append({"text": text, "username": username, "icon_url": icon_url})
        return {"ok": True, "ts": "12345.5032"}

    attempts_count = 0

    async def mock_fail_503_retry(func, *args, **kwargs):
        nonlocal attempts_count
        attempts_count += 1
        raise Exception("503 Server Temporarily Unavailable (high demand spike)")

    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_fail_503_retry
    )
    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent_response
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )

    await process_and_respond(
        channel_id="C_503",
        thread_ts="12345.5032",
        raw_text="[TechLead] Second attempt query",
        is_bot_trigger=True,
    )

    assert len(posted) == 1
    assert posted[0]["username"] == "TechLead"
    # Should say HIGH DEMAND / SERVICE OVERLOADED (503) without 'will retry later'
    assert "HIGH DEMAND / SERVICE OVERLOADED (503)" in posted[0]["text"]
    assert "will retry later" not in posted[0]["text"]
    # Retried 3 times standardly because allow_503_immediate_retry=True on second query
    assert attempts_count == 3
    # Should NOT be added back to remaining
    assert "TechLead" not in thread_state_manager.remaining_agents_map.get(thread_key, [])
    # Clean termination without forced closing nomination tag
    assert "[Architect]" not in posted[0]["text"]


@pytest.mark.anyio
async def test_execute_llm_with_retry_503_selective_bypass():
    call_count = 0

    def mock_503_llm():
        nonlocal call_count
        call_count += 1
        raise Exception("503 Service Unavailable: overloaded")

    # 1. When allow_503_immediate_retry=False (first encounter): raises immediately after 1 attempt
    call_count = 0
    with pytest.raises(Exception, match="503 Service Unavailable"):
        await execute_llm_with_retry(
            mock_503_llm,
            {},
            agent_key="TestAgent",
            thread_key=("C", "T"),
            allow_503_immediate_retry=False,
        )
    assert call_count == 1

    # 2. When allow_503_immediate_retry=True (second encounter): retries 3 times
    call_count = 0
    with pytest.raises(Exception, match="503 Service Unavailable"):
        await execute_llm_with_retry(
            mock_503_llm,
            {},
            agent_key="TestAgent",
            thread_key=("C", "T"),
            allow_503_immediate_retry=True,
        )
    assert call_count == 3

    # 3. For non-503 errors with allow_503_immediate_retry=False: STILL retries 3 times as standard!
    call_count = 0

    def mock_network_error():
        nonlocal call_count
        call_count += 1
        raise ConnectionResetError("Connection reset by peer")

    with pytest.raises(ConnectionResetError):
        await execute_llm_with_retry(
            mock_network_error,
            {},
            agent_key="TestAgent",
            thread_key=("C", "T"),
            allow_503_immediate_retry=False,
        )
    assert call_count == 3
