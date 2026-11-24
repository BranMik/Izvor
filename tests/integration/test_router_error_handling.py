from unittest.mock import AsyncMock, MagicMock

import pytest

from conftest import async_noop_sleep
from core.llm_utils import (
    classify_agent_error,
    configure_llm_completion_fn,
    configure_llm_sleep_fn,
    execute_llm_with_retry,
    is_high_demand_503_error,
)
from orchestration.agent_router import (
    configure_router_sleep_fn,
    process_and_respond,
)
from config_store.agent_settings import (
    get_agents_config,
    reset_enabled_agents,
    set_fixed_config,
    set_orchestration_mode,
    set_sandwich_config,
)
from orchestration.thread_state import thread_state_manager
from services.slack import configure_fetch_thread_history, configure_post_agent_response

AGENTS_CONFIG = get_agents_config()


@pytest.fixture(autouse=True)
def setup_test_environment():
    reset_enabled_agents()
    set_orchestration_mode("sandwich")
    set_sandwich_config([], [])
    set_fixed_config([])
    thread_state_manager.reset_all()
    configure_router_sleep_fn(async_noop_sleep)
    configure_llm_sleep_fn(async_noop_sleep)


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
            Exception("503 Service Unavailable")
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

    # General error & timeout classification (TKT-1411)
    assert (
        classify_agent_error(Exception("Connection reset by peer")) == "general_error"
    )
    assert classify_agent_error(TimeoutError("upstream timed out")) == "service_unavailable"
    assert classify_agent_error(Exception("Request timeout after 120s")) == "service_unavailable"


@pytest.mark.anyio
async def test_process_and_respond_api_key_not_found():
    thread_state_manager.reset_all()

    async def mock_fail_api_key(*args, **kwargs):
        raise RuntimeError("AuthenticationError: Missing API key for Gemini")

    posted = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted.append({"text": text, "username": username, "icon_url": icon_url})
        return {"ok": True, "ts": "12345.0001"}

    configure_llm_completion_fn(mock_fail_api_key)
    configure_post_agent_response(mock_post_agent_response)
    configure_fetch_thread_history(AsyncMock(return_value=[]))

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
async def test_process_and_respond_model_not_found():
    thread_state_manager.reset_all()

    thread_key = ("C123", "12345.0002")
    thread_state_manager.discussion.set_remaining_agents(thread_key[0], thread_key[1], ["agent-10002"])

    async def mock_fail_model(*args, **kwargs):
        raise RuntimeError("NotFoundError: Model gemini/gemini-3.8-flash does not exist")

    posted = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted.append({"text": text, "username": username, "icon_url": icon_url})
        return {"ok": True, "ts": "12345.0002"}

    configure_llm_completion_fn(mock_fail_model)
    configure_post_agent_response(mock_post_agent_response)
    configure_fetch_thread_history(AsyncMock(return_value=[]))

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0002",
        raw_text="/[agent-10011] Analyze this code",
        is_bot_trigger=True,
    )

    assert len(posted) == 1
    assert posted[0]["username"] == "LegalAdvisor"
    assert "MODEL NOT FOUND" in posted[0]["text"]
    assert "/[agent-10002]" in posted[0]["text"]


@pytest.mark.anyio
async def test_process_and_respond_retries_and_clean_error_message():
    thread_state_manager.reset_all()

    thread_key = ("C123", "12345.9999")
    thread_state_manager.discussion.set_remaining_agents(thread_key[0], thread_key[1], ["agent-10002"])

    call_count = 0

    async def mock_fail_general_error(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("Internal Server Error: Unexpected JSON decoding crash")

    posted = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted.append({"text": text, "username": username, "icon_url": icon_url})
        return {"ok": True, "ts": "12345.9999"}

    configure_llm_completion_fn(mock_fail_general_error)
    configure_post_agent_response(mock_post_agent_response)
    configure_fetch_thread_history(AsyncMock(return_value=[]))

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.9999",
        raw_text="/[agent-10011] Analyze this code",
        is_bot_trigger=True,
    )

    # Must have retried 3 times
    assert call_count == 3
    assert len(posted) == 1
    assert posted[0]["username"] == "LegalAdvisor"
    # Clean error message without raw traceback
    assert "Error processing request - check the logs" in posted[0]["text"]
    assert "Unexpected JSON decoding crash" not in posted[0]["text"]
    # Continues thread to next agent
    assert "/[agent-10002]" in posted[0]["text"]


@pytest.mark.anyio
async def test_process_and_respond_rate_limits_and_retry_flow():
    thread_state_manager.reset_all()

    thread_key = ("C123", "12345.0003")

    # Set up specific short flow: LegalAdvisor -> Innovator -> (retry LegalAdvisor) -> SecurityArchitect
    thread_state_manager.discussion.set_remaining_agents(thread_key[0], thread_key[1], ["agent-10002"])

    posted = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted.append({"text": text, "username": username, "icon_url": icon_url})
        return {"ok": True, "ts": "12345.0003"}

    # 1. First run of LegalAdvisor encounters 429 RateLimitError
    async def mock_fail_rate_limit(*args, **kwargs):
        raise RuntimeError("RateLimitError: 429 Too Many Requests, quota exceeded")

    configure_llm_completion_fn(mock_fail_rate_limit)
    configure_post_agent_response(mock_post_agent_response)
    configure_fetch_thread_history(AsyncMock(return_value=[]))

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0003",
        raw_text="/[agent-10011] Database architecture review",
        is_bot_trigger=True,
    )

    assert len(posted) == 1
    assert posted[0]["username"] == "LegalAdvisor"
    assert (
        "RATE LIMITS CROSSED, will retry at the end of discussion" in posted[0]["text"]
    )
    assert "/[agent-10002]" in posted[0]["text"]
    assert thread_key in thread_state_manager.retry.rate_limit_retry_queue
    assert "agent-10011" in thread_state_manager.retry.rate_limit_retry_queue[thread_key]

    # 2. Innovator runs normally, remaining is empty -> nominates retry for LegalAdvisor!
    mock_choice = MagicMock()
    mock_choice.message.content = "My innovator perspective."
    mock_choice.message.tool_calls = None
    mock_resp = MagicMock(choices=[mock_choice], usage=None)

    async def mock_success_innovator(*args, **kwargs):
        return mock_resp

    configure_llm_completion_fn(mock_success_innovator)

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0003",
        raw_text="/[agent-10002] Your turn",
        is_bot_trigger=True,
    )

    assert len(posted) == 2
    assert posted[1]["username"] == "Innovator"
    assert "/[agent-10011]" in posted[1]["text"]
    # Verify LegalAdvisor is now marked as already retried once
    assert "agent-10011" in thread_state_manager.retry.rate_limit_already_retried[thread_key]

    # 3. LegalAdvisor retries at the end of discussion, fails again with RateLimit:
    # Must post 'RATE LIMITS CROSSED' and NOT be retried again.
    configure_llm_completion_fn(mock_fail_rate_limit)

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0003",
        raw_text="/[agent-10011] Retry turn",
        is_bot_trigger=True,
    )

    assert len(posted) == 3
    assert posted[2]["username"] == "LegalAdvisor"
    # Should say RATE LIMITS CROSSED without 'will retry'
    assert "RATE LIMITS CROSSED" in posted[2]["text"]
    assert "will retry" not in posted[2]["text"]
    # Should NOT be added back to RATE_LIMIT_RETRY_QUEUE
    assert "agent-10011" not in thread_state_manager.retry.rate_limit_retry_queue.get(thread_key, [])
    # Clean termination without forced closing nomination tag
    assert "/[agent-10008]" not in posted[2]["text"]


@pytest.mark.anyio
async def test_process_and_respond_503_high_demand_first_skip_and_random_handoff():
    thread_state_manager.reset_all()

    thread_key = ("C_503", "12345.5031")
    # Remaining agents list before LegalAdvisor speaks: Innovator, DeepSeek_Challenger
    thread_state_manager.discussion.set_remaining_agents(thread_key[0], thread_key[1], ["agent-10002", "DeepSeek_Challenger"])

    posted = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted.append({"text": text, "username": username, "icon_url": icon_url})
        return {"ok": True, "ts": "12345.5031"}

    # Simulate 503 High Demand error
    async def mock_fail_503(*args, **kwargs):
        raise RuntimeError(
            "ServiceUnavailableError: 503 The server is currently overloaded with high demand"
        )

    configure_llm_completion_fn(mock_fail_503)
    configure_post_agent_response(mock_post_agent_response)
    configure_fetch_thread_history(AsyncMock(return_value=[]))

    await process_and_respond(
        channel_id="C_503",
        thread_ts="12345.5031",
        raw_text="/[agent-10011] Analyze backend structure",
        is_bot_trigger=True,
    )

    assert len(posted) == 1
    assert posted[0]["username"] == "LegalAdvisor"
    assert (
        "HIGH DEMAND / SERVICE OVERLOADED (503) - Skipping for now, will retry later"
        in posted[0]["text"]
    )
    # Verify LegalAdvisor is recorded as 503-errored for this thread
    assert thread_key in thread_state_manager.retry.high_demand_errored_agents
    assert "agent-10011" in thread_state_manager.retry.high_demand_errored_agents[thread_key]

    # Verify LegalAdvisor is kept in remaining agents
    rem_agents = thread_state_manager.discussion.get_remaining_agents(thread_key[0], thread_key[1])
    assert "agent-10011" in rem_agents
    assert "agent-10002" in rem_agents
    assert "DeepSeek_Challenger" in rem_agents

    # Verify next nomination is one of the other candidates
    nominated_tag = None
    if "/[agent-10002]" in posted[0]["text"]:
        nominated_tag = "agent-10002"
    elif "/[DeepSeek_Challenger]" in posted[0]["text"]:
        nominated_tag = "DeepSeek_Challenger"
    assert nominated_tag in ["agent-10002", "DeepSeek_Challenger"]


@pytest.mark.anyio
async def test_process_and_respond_503_high_demand_second_query_retries_and_terminates():
    thread_state_manager.reset_all()

    thread_key = ("C_503", "12345.5032")
    # LegalAdvisor already encountered 503 once
    thread_state_manager.retry.high_demand_errored_agents[thread_key] = {"agent-10011"}
    thread_state_manager.discussion.set_remaining_agents(thread_key[0], thread_key[1], [])  # No other agents left

    posted = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted.append({"text": text, "username": username, "icon_url": icon_url})
        return {"ok": True, "ts": "12345.5032"}

    attempts_count = 0

    async def mock_fail_503_retry(*args, **kwargs):
        nonlocal attempts_count
        attempts_count += 1
        raise RuntimeError("503 Server Temporarily Unavailable (high demand spike)")

    configure_llm_completion_fn(mock_fail_503_retry)
    configure_post_agent_response(mock_post_agent_response)
    configure_fetch_thread_history(AsyncMock(return_value=[]))

    await process_and_respond(
        channel_id="C_503",
        thread_ts="12345.5032",
        raw_text="/[agent-10011] Second attempt query",
        is_bot_trigger=True,
    )

    assert len(posted) == 1
    assert posted[0]["username"] == "LegalAdvisor"
    # Should say HIGH DEMAND / SERVICE OVERLOADED (503) without 'will retry later'
    assert "HIGH DEMAND / SERVICE OVERLOADED (503)" in posted[0]["text"]
    assert "will retry later" not in posted[0]["text"]
    # Retried 3 times standardly because allow_503_immediate_retry=True on second query
    assert attempts_count == 3
    # Should NOT be added back to remaining
    assert "agent-10011" not in thread_state_manager.discussion.get_remaining_agents(thread_key[0], thread_key[1])
    # Clean termination without forced closing nomination tag
    assert "/[agent-10008]" not in posted[0]["text"]


@pytest.mark.anyio
async def test_execute_llm_with_retry_503_selective_bypass():
    call_count = 0

    def mock_503_llm(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("503 Service Unavailable: overloaded")

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

    def mock_network_error(*args, **kwargs):
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


def test_classify_agent_error_litellm_missing_key_wrappers():
    """Verify LiteLLM InternalServerError/APIConnectionError missing-key errors classify as api_key_not_found, not 503."""
    import litellm

    openai_err = litellm.exceptions.InternalServerError(
        message="OpenAIException - Missing credentials. Please pass an `api_key`, or set OPENAI_API_KEY.",
        llm_provider="openai",
        model="openai/gpt-5.6-sol",
    )
    meta_err = litellm.exceptions.InternalServerError(
        message="MetaException - Missing credentials. Please pass an `api_key`.",
        llm_provider="meta",
        model="meta/llama-4-scout",
    )
    gemini_err = litellm.exceptions.APIConnectionError(
        message="Missing Gemini API key. Set the GEMINI_API_KEY or GOOGLE_API_KEY environment variable.",
        llm_provider="gemini",
        model="gemini/gemini-3.8-flash",
    )
    real_500 = litellm.exceptions.InternalServerError(
        message="500 Internal Server Error",
        llm_provider="openai",
        model="openai/gpt-5.6-sol",
    )
    real_conn = litellm.exceptions.APIConnectionError(
        message="Connection reset by peer",
        llm_provider="gemini",
        model="gemini/gemini-3.8-flash",
    )

    assert classify_agent_error(openai_err) == "api_key_not_found"
    assert classify_agent_error(meta_err) == "api_key_not_found"
    assert classify_agent_error(gemini_err) == "api_key_not_found"
    assert classify_agent_error(real_500) == "service_unavailable"
    assert classify_agent_error(real_conn) == "service_unavailable"


@pytest.mark.anyio
async def test_execute_llm_with_retry_skips_retries_on_api_key_not_found():
    """Verify api_key_not_found raises immediately on attempt 1 without wasting retry attempts."""
    import litellm

    call_count = 0

    def mock_missing_key(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise litellm.exceptions.APIConnectionError(
            message="Missing Gemini API key. Set the GEMINI_API_KEY or GOOGLE_API_KEY environment variable.",
            llm_provider="gemini",
            model="gemini/gemini-3.8-flash",
        )

    with pytest.raises(litellm.exceptions.APIConnectionError):
        await execute_llm_with_retry(
            mock_missing_key,
            {},
            agent_key="TestAgent",
            thread_key=("C", "T"),
            max_attempts=3,
        )
    assert call_count == 1


@pytest.mark.anyio
async def test_process_and_respond_missing_key_falls_back_with_slack_footer():
    """Verify missing agent API key falls back in-memory to default_reasoning_model (or catalog) with Slack footer."""
    from core.model_catalog import configure_env_provider
    from config_store.agent_settings import settings_manager

    thread_state_manager.reset_all()
    original_configured = settings_manager.agents_config["agent-10002"]["model"]
    assert original_configured == "anthropic/claude-opus-5"

    configure_env_provider(lambda: {"MISTRAL_API_KEY": "valid-mistral-key", "GEMINI_API_KEY": "AIzaSyValid"})

    called_models = []
    posted = []

    async def mock_completion(*args, **kwargs):
        called_models.append(kwargs.get("model"))
        choice = MagicMock()
        choice.message.content = "Fallback response complete."
        choice.message.tool_calls = None
        resp = MagicMock()
        resp.choices = [choice]
        resp.usage = MagicMock(prompt_tokens=100, completion_tokens=40)
        return resp

    async def mock_post(channel_id, thread_ts, text, username, icon_url=None):
        posted.append({"text": text, "username": username})
        return {"ok": True, "ts": "99999.0001"}

    configure_llm_completion_fn(mock_completion)
    configure_post_agent_response(mock_post)
    configure_fetch_thread_history(AsyncMock(return_value=[]))

    await process_and_respond(
        channel_id="C_FALLBACK",
        thread_ts="99999.0001",
        raw_text="/[agent-10002] Evaluate microservices",
        is_bot_trigger=True,
    )

    assert called_models == ["mistral/mistral-large-latest"]
    assert len(posted) == 1
    assert (
        "⚠️ _Fallback model used: `mistral/mistral-large-latest` "
        "(missing API key for `anthropic/claude-opus-5`)_"
    ) in posted[0]["text"]
    assert "{mistral/mistral-large-latest : 100/40}" in posted[0]["text"]
    # Shared agent_settings must NOT be mutated in place
    assert settings_manager.agents_config["agent-10002"]["model"] == original_configured


@pytest.mark.anyio
async def test_handle_error_503_with_agent_already_in_remaining_no_unbound_local():
    """TKT-1211 (H11): a 503 error where ctx.agent_key is already inside ctx.remaining must not
    raise UnboundLocalError: mode in PostRecordStage.handle_error."""
    from orchestration.stages.post_record import PostRecordStage
    from orchestration.turn_pipeline import TurnContext, TurnPipeline

    thread_state_manager.reset_all()
    channel_id = "C_503_UB"
    thread_ts = "777.0001"
    thread_state_manager.discussion.set_remaining_agents(
        channel_id, thread_ts, ["agent-10011", "agent-10002"]
    )

    mock_post = AsyncMock(return_value={"ok": True, "ts": thread_ts})
    pipeline = TurnPipeline(
        slack_poster=mock_post,
        usage_updater=MagicMock(),
        notes_updater=MagicMock(),
        sleep_fn=AsyncMock(return_value=None),
        debate_turn_pacing_seconds=0,
        slack_post_pause_seconds=0,
    )
    stage = PostRecordStage(pipeline.deps)

    ctx = TurnContext(channel_id=channel_id, thread_ts=thread_ts, raw_text="test", user_id="U_ADMIN")
    ctx.thread_key = (channel_id, thread_ts)
    ctx.agent_key = "agent-10011"
    ctx.agent_cfg = {"username": "LegalAdvisor", "model": "test-model"}
    ctx.remaining = ["agent-10011", "agent-10002"]

    res = await stage.handle_error(ctx, RuntimeError("503 Service Unavailable"))

    assert res["ok"] is False
    assert res["classified_error"] == "service_unavailable"
    assert "HIGH DEMAND" in res["final_text"]
    thread_state_manager.cleanup_thread(channel_id, thread_ts)


def test_classify_agent_error_bad_request_and_word_bounded_codes():
    """TKT-1213: 500 errors with 401-containing request IDs stay service_unavailable; 400 classifies as bad_request."""
    import litellm

    err_with_id = litellm.exceptions.InternalServerError(
        message="500 Internal Server Error (request_id: req_40199)",
        llm_provider="openai",
        model="openai/gpt-5.6-sol",
    )
    assert classify_agent_error(err_with_id) == "service_unavailable"
    assert classify_agent_error(Exception("HTTP 500 Internal Server Error req_40199")) == "service_unavailable"

    bad_req = litellm.exceptions.BadRequestError(
        message="400 invalid parameter value",
        llm_provider="openai",
        model="openai/gpt-5.6-sol",
    )
    assert classify_agent_error(bad_req) == "bad_request"
    assert classify_agent_error(Exception("HTTP 400 Bad Request: malformed schema")) == "bad_request"

    # Gemini/Google may wrap missing/invalid API keys in 400 BadRequestError
    bad_req_key = litellm.exceptions.BadRequestError(
        message="API key not valid. Please pass a valid API key.",
        llm_provider="gemini",
        model="gemini/gemini-3.8-flash",
    )
    assert classify_agent_error(bad_req_key) == "api_key_not_found"


@pytest.mark.anyio
async def test_execute_llm_with_retry_single_layer_backoff_dynamic_and_dict_unmask():
    """TKT-1213: forces num_retries=0 on copied kwargs, fails fast on 404/400, backs off exponentially, and unmasks dict tool_calls."""
    from types import SimpleNamespace
    from core import llm_utils
    from config_store.system_settings import system_settings_manager
    from services.security.pii_masker import PIIMasker

    # 1. Non-mutating kwargs copy & num_retries=0 enforcement + fail-fast on 404 / 400
    seen_kwargs: list[dict] = []
    call_count_404 = 0
    caller_payload = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hello"}],
        "num_retries": 2,
    }

    async def fail_404(**kw):
        nonlocal call_count_404
        call_count_404 += 1
        seen_kwargs.append(dict(kw))
        raise Exception("Model xyz not found (404)")

    with pytest.raises(Exception, match="404"):
        await execute_llm_with_retry(fail_404, caller_payload, agent_key="A1", thread_key=("C", "T"), max_attempts=3)
    assert call_count_404 == 1
    assert caller_payload["num_retries"] == 2
    assert seen_kwargs[0]["num_retries"] == 0
    assert seen_kwargs[0]["timeout"] == 120.0

    call_count_400 = 0

    async def fail_400(**kw):
        nonlocal call_count_400
        call_count_400 += 1
        raise Exception("400 Bad Request: invalid parameter")

    with pytest.raises(Exception, match="400"):
        await execute_llm_with_retry(fail_400, {"model": "m", "messages": []}, agent_key="A1", thread_key=("C", "T"), max_attempts=3)
    assert call_count_400 == 1

    # 2. Dynamic max_attempts & exponential backoff with deterministic injected sleep
    sleep_delays: list[float] = []
    timeout_calls = 0

    async def fake_sleep(delay: float):
        sleep_delays.append(delay)

    async def always_timeout(**kw):
        nonlocal timeout_calls
        timeout_calls += 1
        raise TimeoutError("timed out")

    configure_llm_sleep_fn(fake_sleep)
    llm_utils.set_llm_retry_config_provider(lambda: (4, 0.5))
    try:
        with pytest.raises(TimeoutError):
            await execute_llm_with_retry(
                always_timeout,
                {"model": "m", "messages": []},
                agent_key="A1",
                thread_key=("C", "T"),
            )
    finally:
        configure_llm_sleep_fn(None)
        system_settings_manager._register_core_providers()

    assert timeout_calls == 4
    assert sleep_delays == [0.5, 1.0, 2.0]

    # 3. Dict-style tool_calls stay masked through execute_llm_with_retry, while non-tool final replies unmask content
    masker = PIIMasker()
    llm_utils.set_privacy_mode_provider(lambda: True)
    llm_utils.set_pii_masker_instance(masker)
    try:
        async def return_dict_tool_call(**kw):
            masked_user = kw["messages"][0]["content"]
            token = masked_user.split("Email ")[1]
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=f"Sending to {token}",
                            tool_calls=[{"id": "tc1", "type": "function", "function": {"name": "send", "arguments": f'{{"to": "{token}"}}'}}],
                        )
                    )
                ]
            )

        res = await execute_llm_with_retry(
            return_dict_tool_call,
            {"model": "m", "messages": [{"role": "user", "content": "Email alice@example.com"}]},
            agent_key="A1",
            thread_key=("C", "T"),
        )
        assert res.choices[0].message.tool_calls[0]["function"]["arguments"] == '{"to": "<EMAIL_0>"}'
        assert "alice@example.com" not in res.choices[0].message.content

        async def return_final_reply(**kw):
            masked_user = kw["messages"][0]["content"]
            token = masked_user.split("Email ")[1]
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=f"Sent to {token}", tool_calls=None)
                    )
                ]
            )

        final_res = await execute_llm_with_retry(
            return_final_reply,
            {"model": "m", "messages": [{"role": "user", "content": "Email alice@example.com"}]},
            agent_key="A1",
            thread_key=("C", "T"),
        )
        assert final_res.choices[0].message.content == "Sent to alice@example.com"
    finally:
        system_settings_manager._register_core_providers()


@pytest.mark.anyio
async def test_pre_stage3_error_preserves_sqlite_remaining_agents():
    """TKT-1409 (CB-10): Exceptions raised before Stage 3 hydrates ctx.remaining preserve SQLite remaining_agents in handle_error()."""
    from orchestration.protocols import PipelineDeps
    from orchestration.stages.post_record import PostRecordStage
    from orchestration.turn_pipeline import TurnContext

    ch = "C_ERR_PRE3"
    ts = "1700008888.0001"
    thread_state_manager.clear_state(ch, ts)
    thread_state_manager.set_remaining_agents(ch, ts, ["agent-10001", "agent-10002", "agent-10003"])

    ctx = TurnContext(channel_id=ch, thread_ts=ts, raw_text="Topic")
    ctx.agent_key = "agent-10001"
    ctx.agent_cfg = {"username": "Architect", "model": "test-model"}
    # Deliberately leave ctx.remaining == [] as happens before Stage 3
    assert ctx.remaining == []

    stage = PostRecordStage(
        PipelineDeps(
            slack_poster=AsyncMock(return_value={"ok": True, "ts": "999.1"}),
            debate_turn_pacing_seconds=0.0,
            slack_post_pause_seconds=0.0,
            token_recorder=lambda **kw: None,
            notes_updater=lambda **kw: None,
            usage_updater=lambda **kw: None,
        )
    )
    res = await stage.handle_error(ctx, RuntimeError("Stage 2 lookup failed"))
    assert res["is_completed"] is False
    assert thread_state_manager.get_remaining_agents(ch, ts) == ["agent-10001", "agent-10002", "agent-10003"]

