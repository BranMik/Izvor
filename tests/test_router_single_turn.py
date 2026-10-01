from unittest.mock import AsyncMock, MagicMock

import pytest

from core.models import ActionDirective, ExtractedIntent
from nlp.message_builder import build_llm_messages
from orchestration import agent_router
from orchestration.agent_router import process_and_respond
from orchestration.agent_settings import (
    get_enabled_agents,
    reset_enabled_agents,
    set_fixed_config,
    set_orchestration_mode,
    set_sandwich_config,
)
from orchestration.nomination import parse_agent_and_prompt
from orchestration.thread_state import thread_state_manager


@pytest.fixture(autouse=True)
def setup_test_environment(monkeypatch):
    reset_enabled_agents()
    set_orchestration_mode("sandwich")
    set_sandwich_config([], [])
    set_fixed_config([])
    thread_state_manager.reset_all()
    monkeypatch.setattr(agent_router.asyncio, "sleep", AsyncMock(return_value=None))


def test_parse_explicit_bracket_tags():
    agent_key, prompt, was_explicit = parse_agent_and_prompt(
        "<@U12345> /[Visionary] Create a new UI flow", "C12345", "123456789.0001"
    )
    assert agent_key == "Visionary"
    assert prompt == "Create a new UI flow"
    assert was_explicit is True


def test_parse_mistral_techlead_tag():
    agent_key, prompt, was_explicit = parse_agent_and_prompt(
        "/[LegalAdvisor] Check this database query", "C12345", "123456789.0001"
    )
    assert agent_key == "LegalAdvisor"
    assert prompt == "Check this database query"
    assert was_explicit is True


def test_parse_multiple_tags_first_invalid():
    agent_key, prompt, was_explicit = parse_agent_and_prompt(
        "/[InvalidTag] /[LegalAdvisor] Check this database query", "C12345", "123456789.0001"
    )
    assert agent_key == "LegalAdvisor"
    assert prompt == "/[InvalidTag]  Check this database query"
    assert was_explicit is True


def test_parse_at_mention_persona():
    # Non-bracketed name mention is no longer an explicit agent tag; falls back to dynamic enabled agent
    agent_key, prompt, was_explicit = parse_agent_and_prompt(
        "<@U12345> @Gemini Visionary generate a concept",
        "C12345",
        "123456789.0001",
    )
    assert agent_key in get_enabled_agents()
    assert prompt == "@Gemini Visionary generate a concept"
    assert was_explicit is False

    # Bracketed tag without slash is NOT an explicit agent tag
    agent_key_plain, prompt_plain, was_explicit_plain = parse_agent_and_prompt(
        "<@U12345> [Visionary] generate a concept",
        "C12345",
        "123456789.0002",
    )
    assert agent_key_plain in get_enabled_agents()
    assert prompt_plain == "[Visionary] generate a concept"
    assert was_explicit_plain is False

    # Slash bracketed tag is the only recognized explicit agent tag
    agent_key_tagged, prompt_tagged, was_explicit_tagged = parse_agent_and_prompt(
        "<@U12345> /[Visionary] generate a concept",
        "C12345",
        "123456789.0003",
    )
    assert agent_key_tagged == "Visionary"
    assert prompt_tagged == "generate a concept"
    assert was_explicit_tagged is True


def test_parse_fallback_default_agent():
    agent_key, prompt, was_explicit = parse_agent_and_prompt(
        "<@U12345> Tell me how to structure this application",
        "C12345",
        "123456789.0001",
    )
    assert agent_key in get_enabled_agents()
    assert prompt == "Tell me how to structure this application"
    assert was_explicit is False


@pytest.mark.anyio
async def test_process_and_respond_token_appending(monkeypatch):
    mock_usage = MagicMock()
    mock_usage.prompt_tokens = 150
    mock_usage.completion_tokens = 50

    mock_choice = MagicMock()
    mock_choice.message.content = "This is a response message."
    mock_choice.message.tool_calls = None

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.usage = mock_usage

    async def mock_run_in_threadpool(func, *args, **kwargs):
        return mock_response

    posted_messages = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted_messages.append(text)
        return {"ok": True, "ts": "12345.6789"}

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
        thread_ts="12345.6789",
        raw_text="Hello /[Visionary]",
    )

    assert len(posted_messages) == 1
    assert posted_messages[0].endswith("\n\n{gemini/gemini-3.8-flash : 150/50}")


@pytest.mark.anyio
async def test_process_and_respond_model_token_footer_openai(monkeypatch):
    """
    Verifies that an agent using SecurityArchitect ends with {mistral/mistral-large-latest : 721/64}.
    """
    mock_usage = MagicMock()
    mock_usage.prompt_tokens = 721
    mock_usage.completion_tokens = 64

    mock_choice = MagicMock()
    mock_choice.message.content = "Analysis completed."
    mock_choice.message.tool_calls = None

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.usage = mock_usage

    async def mock_run_in_threadpool(func, *args, **kwargs):
        return mock_response

    posted_messages = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted_messages.append(text)
        return {"ok": True, "ts": "12345.9999"}

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
        thread_ts="12345.9999",
        raw_text="Hello /[SecurityArchitect]",
    )

    assert len(posted_messages) == 1
    assert posted_messages[0].endswith("\n\n{mistral/mistral-large-latest : 721/64}")


@pytest.mark.anyio
async def test_model_token_footer_ignored_by_tag_detection():
    """
    Verifies that {mistral/mistral-large-latest : 721/64} is not flagged as unrecognized or ambiguous agent tag.
    """
    from pipeline.entity_resolver import entity_resolver

    sample_text = "Here is our response.\n\n{mistral/mistral-large-latest : 721/64}"
    unrecognized = entity_resolver.detect_unrecognized_tags(sample_text)
    assert unrecognized == []

    ambiguous = entity_resolver.detect_ambiguous_tags(sample_text)
    assert ambiguous == []


@pytest.mark.anyio
async def test_scenario_1_single_agent_tagged(monkeypatch):
    """
    Scenario: User tags ONLY one agent.
    Expected: Single agent speaks, force_termination=True, any generated nomination tag stripped, flow cleanly ends.
    """
    thread_state_manager.reset_all()

    mock_choice = MagicMock()
    # Model attempts to nominate Sceptic even though user only wanted 1 agent
    mock_choice.message.content = "My single vision response. /[Sceptic]"
    mock_choice.message.tool_calls = None

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.usage = None

    async def mock_run_in_threadpool(func, *args, **kwargs):
        captured_messages = kwargs.get("messages") or args[2]
        mock_run_in_threadpool.captured_user_prompt = captured_messages[-1]["content"]
        return mock_response

    posted_messages = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted_messages.append(text)
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

    thread_key = ("C123", "12345.0001")

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0001",
        raw_text="/[Visionary] Explain this concept only",
        is_bot_trigger=False,
    )

    # Verify:
    # 1. User prompt forced termination
    assert "CRITICAL CONSTRAINT" in mock_run_in_threadpool.captured_user_prompt
    # 2. State cleaned up
    assert thread_key not in thread_state_manager.custom_flow_threads
    assert thread_key not in thread_state_manager.remaining_agents_map
    # 3. Posted message has complete nomination stripped (not just brackets, agent name is removed too)
    assert len(posted_messages) == 1
    assert "/[Sceptic]" not in posted_messages[0]
    assert "Sceptic" not in posted_messages[0]
    assert posted_messages[0].startswith("My single vision response.")


@pytest.mark.anyio
async def test_extract_history_limit_formats():
    from pipeline.directive_parser import directive_parser

    # 1. Standard format /history 5
    res = await directive_parser.parse("<@U12345> Tell me a joke /history 5")
    assert res.history_limit == 5
    assert res.cleaned_prompt == "<@U12345> Tell me a joke"

    # 2. Bracketed format /history [3]
    res = await directive_parser.parse("@CaiLogics [LegalAdvisor] Explain rust /history [3]")
    assert res.history_limit == 3
    assert res.cleaned_prompt == "@CaiLogics [LegalAdvisor] Explain rust"

    # 3. Explicit int format /history [2:int]
    res = await directive_parser.parse("@app_mention /history [2:int] summarize")
    assert res.history_limit == 2
    assert res.cleaned_prompt == "@app_mention summarize"

    # 4. Colon format /history: 10 and /history:4
    res = await directive_parser.parse("@CaiLogics /history: 10 test prompt")
    assert res.history_limit == 10
    assert res.cleaned_prompt == "@CaiLogics test prompt"

    res = await directive_parser.parse("@CaiLogics /history:4 test prompt")
    assert res.history_limit == 4
    assert res.cleaned_prompt == "@CaiLogics test prompt"

    # 5. Equals format /history=7
    res = await directive_parser.parse("@CaiLogics /history=7 test prompt")
    assert res.history_limit == 7
    assert res.cleaned_prompt == "@CaiLogics test prompt"

    # 6. Zero history /history 0
    res = await directive_parser.parse("Prompt with /history 0")
    assert res.history_limit == 0
    assert res.cleaned_prompt == "Prompt with"

    # 7. No history command
    res = await directive_parser.parse("Plain prompt without history")
    assert res.history_limit is None
    assert res.cleaned_prompt == "Plain prompt without history"

    # 8. Command in middle of text
    res = await directive_parser.parse("@CaiLogics What is /history 3 quantum computing?")
    assert res.history_limit == 3
    assert res.cleaned_prompt == "@CaiLogics What is quantum computing?"
    # limit, cleaned = extract_history_limit("last 3 messages analyze the issue")
    # assert limit == 3
    # assert cleaned == "analyze the issue"
    #
    # limit, cleaned = extract_history_limit("povijest od 4 poruke i odgovori")
    # assert limit == 4
    # assert cleaned == "i odgovori"


def test_parse_agent_and_prompt_cleans_history_command():
    agent_key, prompt, was_explicit = parse_agent_and_prompt(
        "<@U12345> /[Visionary] What is AI? /history 4",
        "C_HIST_PARSE",
        "12345.0001",
    )
    assert agent_key == "Visionary"
    assert prompt == "What is AI?"
    assert was_explicit is True

    agent_key2, prompt2, was_explicit2 = parse_agent_and_prompt(
        "<@U12345> /history [2] What is AI?",
        "C_HIST_PARSE",
        "12345.0002",
    )
    assert agent_key2 in get_enabled_agents()
    assert prompt2 == "What is AI?"
    assert was_explicit2 is False


@pytest.mark.anyio
async def test_process_and_respond_history_limit_initial_prompt(monkeypatch):
    """
    Scenario: Thread has 5 prior messages. User submits prompt with /history 2.
    Expected:
    - LiteLLM receives only the last 2 prior messages + current user prompt.
    - Active limit is stored in THREAD_HISTORY_LIMITS.
    """
    thread_key = ("C_HIST_TEST", "12345.2001")
    thread_state_manager.context.thread_history_limits.pop(thread_key, None)
    thread_state_manager.remaining_agents_map.pop(thread_key, None)

    mock_history = [
        {"role": "user", "content": "Message 1"},
        {"role": "assistant", "content": "Reply 1"},
        {"role": "user", "content": "Message 2"},
        {"role": "assistant", "content": "Reply 2"},
        {"role": "user", "content": "Message 3"},
        {"role": "user", "content": "<@U12345> What now? /history 2"},
    ]

    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history",
        AsyncMock(return_value=mock_history),
    )

    captured_messages = []

    mock_choice = MagicMock()
    mock_choice.message.content = "Visionary response. /[LegalAdvisor]"
    mock_choice.message.tool_calls = None
    mock_resp = MagicMock(choices=[mock_choice], usage=None)

    async def mock_run(func, *args, **kwargs):
        if "messages" in kwargs:
            captured_messages.extend(kwargs["messages"])
        return mock_resp

    monkeypatch.setattr("orchestration.agent_router.run_in_threadpool", mock_run)
    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response",
        AsyncMock(return_value={"ok": True, "ts": "12345.2002"}),
    )

    await process_and_respond(
        channel_id="C_HIST_TEST",
        thread_ts="12345.2001",
        raw_text="<@U12345> What now? /history 2",
        is_bot_trigger=False,
    )

    # Verify captured_messages:
    # 0: system prompt
    # 1: context prefix (ensures conversational sequence starts with user)
    # 2: Reply 2 (4th message in history)
    # 3: Message 3 (5th message in history)
    # 4: What now? (current user prompt)
    assert len(captured_messages) == 5
    assert captured_messages[0]["role"] == "system"
    assert captured_messages[1]["role"] == "user"
    assert captured_messages[2] == {"role": "assistant", "content": "Reply 2"}
    assert captured_messages[3] == {"role": "user", "content": "Message 3"}
    assert captured_messages[4]["role"] == "user"
    assert "What now?" in captured_messages[4]["content"]

    # Verify THREAD_HISTORY_LIMITS is preserved for subsequent bot turns
    limit_info = thread_state_manager.get_history_limit("C_HIST_TEST", "12345.2001")
    assert limit_info is not None
    assert limit_info["limit"] == 2
    assert limit_info["prior_cutoff_index"] == 5


@pytest.mark.anyio
async def test_process_and_respond_history_zero(monkeypatch):
    """
    Scenario: User specifies /history 0.
    Expected:
    - LiteLLM receives ONLY system prompt + current user prompt.
    """
    thread_key = ("C_HIST_ZERO", "12345.5001")
    thread_state_manager.context.thread_history_limits.pop(thread_key, None)

    mock_history = [
        {"role": "user", "content": "Message 1"},
        {"role": "assistant", "content": "Reply 1"},
        {"role": "user", "content": "<@U12345> Fresh start /history 0"},
    ]

    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history",
        AsyncMock(return_value=mock_history),
    )

    captured_messages = []

    mock_choice = MagicMock()
    mock_choice.message.content = "Fresh response. [LegalAdvisor]"
    mock_choice.message.tool_calls = None
    mock_resp = MagicMock(choices=[mock_choice], usage=None)

    async def mock_run(func, *args, **kwargs):
        if "messages" in kwargs:
            captured_messages.extend(kwargs["messages"])
        return mock_resp

    monkeypatch.setattr("orchestration.agent_router.run_in_threadpool", mock_run)
    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response",
        AsyncMock(return_value={"ok": True, "ts": "12345.5002"}),
    )

    await process_and_respond(
        channel_id="C_HIST_ZERO",
        thread_ts="12345.5001",
        raw_text="<@U12345> Fresh start /history 0",
        is_bot_trigger=False,
    )

    # Only system prompt + user prompt (0 prior messages)
    assert len(captured_messages) == 2
    assert captured_messages[0]["role"] == "system"
    assert captured_messages[1]["role"] == "user"
    assert "Fresh start" in captured_messages[1]["content"]


def test_build_llm_messages_user_trigger():
    """Test standard user trigger message construction."""
    sys_prompt = "You are a helpful assistant."
    history = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    agent_cfg = {"username": "Test Bot"}
    messages = build_llm_messages(
        system_prompt=sys_prompt,
        effective_history=history,
        prompt="Tell me more",
        is_bot_trigger=False,
        agent_key="Test_Bot",
        agent_cfg=agent_cfg,
    )
    assert len(messages) == 4
    assert messages[0] == {"role": "system", "content": sys_prompt}
    assert messages[1] == {"role": "user", "content": "Hello"}
    assert messages[2] == {"role": "assistant", "content": "Hi there!"}
    assert messages[3]["role"] == "user"
    assert messages[3]["content"].startswith("Tell me more")


def test_build_llm_messages_bot_trigger_strictly_ends_with_user_turn():
    """Test bot trigger appends user turn so Anthropic/Mistral/Gemini won't reject with 400 Bad Request."""
    sys_prompt = "You are The Sceptic."
    history = [
        {"role": "user", "content": "Initial question"},
        {"role": "assistant", "content": "[Innovator]: Propose X [Sceptic]"},
    ]
    agent_cfg = {"username": "The Sceptic"}
    messages = build_llm_messages(
        system_prompt=sys_prompt,
        effective_history=history,
        prompt="Propose X",
        is_bot_trigger=True,
        agent_key="Sceptic",
        agent_cfg=agent_cfg,
    )
    assert len(messages) == 4
    assert messages[0] == {"role": "system", "content": sys_prompt}
    assert messages[1] == {"role": "user", "content": "Initial question"}
    assert messages[2] == {
        "role": "assistant",
        "content": "[Innovator]: Propose X [Sceptic]",
    }
    assert messages[3]["role"] == "user"
    assert "The Sceptic" in messages[3]["content"]


def test_build_llm_messages_leading_assistant_gets_user_prefix():
    """Test that trimmed history starting with assistant message gets a user prefix so API accepts it."""
    sys_prompt = "You are Legal Advisor."
    history = [
        {"role": "assistant", "content": "Assistant turn 1"},
        {"role": "assistant", "content": "Assistant turn 2"},
    ]
    agent_cfg = {"username": "Legal Advisor"}
    messages = build_llm_messages(
        system_prompt=sys_prompt,
        effective_history=history,
        prompt="Test",
        is_bot_trigger=True,
        agent_key="LegalAdvisor",
        agent_cfg=agent_cfg,
    )
    # index 0: system
    # index 1: user prefix
    # index 2: assistant 1
    # index 3: assistant 2
    # index 4: user closing directive
    assert len(messages) == 5
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert messages[2] == {"role": "assistant", "content": "Assistant turn 1"}
    assert messages[3] == {"role": "assistant", "content": "Assistant turn 2"}
    assert messages[4]["role"] == "user"


@pytest.mark.anyio
async def test_extract_max_words_limit():
    from pipeline.directive_parser import directive_parser

    # Standard syntax
    res = await directive_parser.parse("/max_words 50 hello world")
    assert res.max_words == 50
    assert res.cleaned_prompt == "hello world"

    # Bracketed typed syntax /max_words [x:int]
    res = await directive_parser.parse("/max_words [75:int] evaluate this")
    assert res.max_words == 75
    assert res.cleaned_prompt == "evaluate this"

    # Colon and equals syntax
    res = await directive_parser.parse("/max_words: 30 test")
    assert res.max_words == 30
    assert res.cleaned_prompt == "test"

    res = await directive_parser.parse("/max_words=100 test")
    assert res.max_words == 100
    assert res.cleaned_prompt == "test"

    # Hyphen and single-word variations
    res = await directive_parser.parse("/max-words 25 test")
    assert res.max_words == 25

    res = await directive_parser.parse("/maxwords 15 test")
    assert res.max_words == 15

    # No command present
    res = await directive_parser.parse("Just a regular prompt")
    assert res.max_words is None
    assert res.cleaned_prompt == "Just a regular prompt"


@pytest.mark.anyio
async def test_extract_min_words_limit():
    from pipeline.directive_parser import directive_parser

    # Standard syntax
    res = await directive_parser.parse("/min_words 50 hello world")
    assert res.min_words == 50
    assert res.cleaned_prompt == "hello world"

    # Bracketed typed syntax /min_words [x:int]
    res = await directive_parser.parse("/min_words [75:int] evaluate this")
    assert res.min_words == 75
    assert res.cleaned_prompt == "evaluate this"

    # Colon and equals syntax
    res = await directive_parser.parse("/min_words: 30 test")
    assert res.min_words == 30
    assert res.cleaned_prompt == "test"

    res = await directive_parser.parse("/min_words=100 test")
    assert res.min_words == 100
    assert res.cleaned_prompt == "test"

    # Hyphen and single-word variations
    res = await directive_parser.parse("/min-words 25 test")
    assert res.min_words == 25

    res = await directive_parser.parse("/minwords 15 test")
    assert res.min_words == 15

    # No command present
    res = await directive_parser.parse("Just a regular prompt")
    assert res.min_words is None
    assert res.cleaned_prompt == "Just a regular prompt"


@pytest.mark.anyio
async def test_process_and_respond_with_max_words(monkeypatch):
    thread_state_manager.reset_all()

    channel_id = "C_MAX_WORDS"
    thread_ts = "99999.0001"
    thread_key = (channel_id, thread_ts)
    thread_state_manager.remaining_agents_map[thread_key] = ["SecurityArchitect"]

    captured_messages = []

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message = MagicMock(
        content="Concise response under limit /[SecurityArchitect]", tool_calls=None
    )
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=15, completion_tokens=10)

    async def mock_run_in_threadpool(func, *args, **kwargs):
        captured_messages.append(kwargs.get("messages", []))
        return mock_response

    posted_messages = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted_messages.append({"text": text, "username": username})
        return {"ok": True, "ts": thread_ts}

    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent_response
    )
    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_run_in_threadpool
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )
    monkeypatch.setattr("orchestration.agent_router.record_thread_tokens", MagicMock())

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="/[LegalAdvisor] /[SecurityArchitect] /max_words [40:int] Explain Kubernetes ingress architecture",
        is_bot_trigger=False,
    )

    assert res.get("ok") is True
    # Thread level setting persisted for remaining agents in flow
    assert thread_state_manager.get_max_words(channel_id, thread_ts) == 40

    # System prompt in LLM call has 40 words limit and skips standard output constraints
    assert len(captured_messages) == 1
    system_msg = captured_messages[0][0]["content"]
    assert (
        "Hard Limit: Your entire response must strictly NOT exceed 40 words."
        in system_msg
    )
    # Skipped standard output_constraints
    assert "1 sentence stating your core verdict/concern." not in system_msg
    assert "2-3 concise bullet points" not in system_msg

    # User message had the /max_words command stripped cleanly
    user_msg = captured_messages[0][-1]["content"]
    assert "/max_words" not in user_msg
    assert "Explain Kubernetes ingress architecture" in user_msg


@pytest.mark.anyio
async def test_process_and_respond_with_min_words(monkeypatch):
    thread_state_manager.reset_all()

    channel_id = "C_MIN_WORDS"
    thread_ts = "88888.0001"
    thread_key = (channel_id, thread_ts)
    thread_state_manager.remaining_agents_map[thread_key] = ["SecurityArchitect"]

    captured_messages = []

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message = MagicMock(
        content="Detailed thorough response with at least eighty words /[SecurityArchitect]",
        tool_calls=None,
    )
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=15, completion_tokens=85)

    async def mock_run_in_threadpool(func, *args, **kwargs):
        captured_messages.append(kwargs.get("messages", []))
        return mock_response

    posted_messages = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted_messages.append({"text": text, "username": username})
        return {"ok": True, "ts": thread_ts}

    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent_response
    )
    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_run_in_threadpool
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )
    monkeypatch.setattr("orchestration.agent_router.record_thread_tokens", MagicMock())

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="/[LegalAdvisor] /[SecurityArchitect] /min_words [80:int] Explain Kubernetes ingress architecture",
        is_bot_trigger=False,
    )

    assert res.get("ok") is True
    # Thread level setting persisted for remaining agents in flow
    assert thread_state_manager.get_min_words(channel_id, thread_ts) == 80

    # System prompt in LLM call has min_words instruction and skips standard output constraints
    assert len(captured_messages) == 1
    system_msg = captured_messages[0][0]["content"]
    assert (
        "Minimum Length: Your entire response must have a minimum length of 80 words."
        in system_msg
    )
    # Skipped standard output_constraints
    assert "1 sentence stating your core verdict/concern." not in system_msg
    assert "2-3 concise bullet points" not in system_msg

    # User message had the /min_words command stripped cleanly
    user_msg = captured_messages[0][-1]["content"]
    assert "/min_words" not in user_msg
    assert "Explain Kubernetes ingress architecture" in user_msg


@pytest.mark.anyio
async def test_process_and_respond_with_min_and_max_words(monkeypatch):
    thread_state_manager.reset_all()

    channel_id = "C_MIN_MAX_WORDS"
    thread_ts = "77777.0001"
    thread_key = (channel_id, thread_ts)
    thread_state_manager.remaining_agents_map[thread_key] = ["SecurityArchitect"]

    captured_messages = []

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message = MagicMock(
        content="Response between bounds /[SecurityArchitect]", tool_calls=None
    )
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=15, completion_tokens=60)

    async def mock_run_in_threadpool(func, *args, **kwargs):
        captured_messages.append(kwargs.get("messages", []))
        return mock_response

    posted_messages = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted_messages.append({"text": text, "username": username})
        return {"ok": True, "ts": thread_ts}

    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent_response
    )
    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_run_in_threadpool
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )
    monkeypatch.setattr("orchestration.agent_router.record_thread_tokens", MagicMock())

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="/[LegalAdvisor] /[SecurityArchitect] /min_words [50:int] /max_words [150:int] Analyze database scaling",
        is_bot_trigger=False,
    )

    assert res.get("ok") is True
    assert thread_state_manager.get_min_words(channel_id, thread_ts) == 50
    assert thread_state_manager.get_max_words(channel_id, thread_ts) == 150

    assert len(captured_messages) == 1
    system_msg = captured_messages[0][0]["content"]
    assert (
        "Minimum Length: Your entire response must have a minimum length of 50 words."
        in system_msg
    )
    assert (
        "Hard Limit: Your entire response must strictly NOT exceed 150 words."
        in system_msg
    )

    user_msg = captured_messages[0][-1]["content"]
    assert "/min_words" not in user_msg
    assert "/max_words" not in user_msg
    assert "Analyze database scaling" in user_msg


@pytest.mark.anyio
async def test_process_and_respond_with_natural_language_limits(monkeypatch):
    thread_state_manager.reset_all()

    channel_id = "C_NL_LIMITS"
    thread_ts = "66666.0001"
    thread_key = (channel_id, thread_ts)
    thread_state_manager.remaining_agents_map[thread_key] = ["SecurityArchitect"]

    captured_messages = []

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message = MagicMock(
        content="Response under 60 words /[SecurityArchitect]", tool_calls=None
    )
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=15, completion_tokens=30)

    async def mock_run_in_threadpool(func, *args, **kwargs):
        captured_messages.append(kwargs.get("messages", []))
        return mock_response

    posted_messages = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted_messages.append({"text": text, "username": username})
        return {"ok": True, "ts": thread_ts}

    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent_response
    )
    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_run_in_threadpool
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        "nlp.intent_extractor.extract_intent",
        AsyncMock(return_value=ExtractedIntent(modifiers={"max_words": 60})),
    )

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="/[LegalAdvisor] /[SecurityArchitect] odgovori u maksimalno 60 riječi o mikroservisima",
        is_bot_trigger=False,
    )

    assert res.get("ok") is True
    assert thread_state_manager.get_max_words(channel_id, thread_ts) == 60

    assert len(captured_messages) == 1
    system_msg = captured_messages[0][0]["content"]
    assert (
        "Hard Limit: Your entire response must strictly NOT exceed 60 words."
        in system_msg
    )

    user_msg = captured_messages[0][-1]["content"]
    # TODO: With pure IntentExtractor, the user prompt is preserved without destructive regex stripping.
    # assert "maksimalno 60 riječi" not in user_msg
    assert "mikroservisima" in user_msg


@pytest.mark.anyio
async def test_process_and_respond_with_exclude_command(monkeypatch):
    thread_state_manager.reset_all()

    channel_id = "C_EXCLUDE_CMD"
    thread_ts = "88888.0001"
    thread_key = (channel_id, thread_ts)

    captured_messages = []

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message = MagicMock(
        content="Reviewing without innovator /[ScopeOptimizer]", tool_calls=None
    )
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=20, completion_tokens=40)

    async def mock_run_in_threadpool(func, *args, **kwargs):
        captured_messages.append(kwargs.get("messages", []))
        return mock_response

    posted_messages = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted_messages.append({"text": text, "username": username})
        return {"ok": True, "ts": thread_ts}

    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent_response
    )
    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_run_in_threadpool
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )
    monkeypatch.setattr("orchestration.agent_router.record_thread_tokens", MagicMock())

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="/[LegalAdvisor] /[Innovator] /[ScopeOptimizer] /exclude [Innovator] Evaluate the refactoring",
        is_bot_trigger=False,
    )

    assert res.get("ok") is True
    # Innovator should be removed immediately from remaining agents
    assert "Innovator" not in thread_state_manager.remaining_agents_map[thread_key]
    assert thread_state_manager.remaining_agents_map[thread_key] == ["ScopeOptimizer"]

    # Command should be cleanly stripped from user message passed to model
    user_msg = captured_messages[0][-1]["content"]
    assert "/exclude" not in user_msg
    assert "Evaluate the refactoring" in user_msg


@pytest.mark.anyio
async def test_process_and_respond_with_multi_agent_exclude_command(monkeypatch):
    thread_state_manager.reset_all()

    channel_id = "C_MULTI_EXCLUDE"
    thread_ts = "88888.0002"
    thread_key = (channel_id, thread_ts)

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message = MagicMock(
        content="Reviewing /[UserAdvocate]", tool_calls=None
    )
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=20, completion_tokens=40)

    async def mock_run_in_threadpool(func, *args, **kwargs):
        return mock_response

    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response",
        AsyncMock(return_value={"ok": True, "ts": thread_ts}),
    )
    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_run_in_threadpool
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )
    monkeypatch.setattr("orchestration.agent_router.record_thread_tokens", MagicMock())

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="/[LegalAdvisor] /[Innovator] /[ScopeOptimizer] /[UserAdvocate] /exclude [Innovator] [ScopeOptimizer] Plan sprint",
        is_bot_trigger=False,
    )

    assert res.get("ok") is True
    # Both Innovator and ScopeOptimizer should be removed from remaining agents
    assert thread_state_manager.remaining_agents_map[thread_key] == ["UserAdvocate"]


@pytest.mark.anyio
async def test_process_and_respond_with_default_flow_and_exclude(monkeypatch):
    thread_state_manager.reset_all()

    channel_id = "C_DEFAULT_EXCLUDE"
    thread_ts = "88888.0003"
    thread_key = (channel_id, thread_ts)

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message = MagicMock(
        content="Debating without techlead and innovator /[Sceptic]", tool_calls=None
    )
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=20, completion_tokens=40)

    async def mock_run_in_threadpool(func, *args, **kwargs):
        return mock_response

    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response",
        AsyncMock(return_value={"ok": True, "ts": thread_ts}),
    )
    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_run_in_threadpool
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )
    monkeypatch.setattr("orchestration.agent_router.record_thread_tokens", MagicMock())

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="Debate this architecture /exclude [LegalAdvisor] [Innovator]",
        is_bot_trigger=False,
    )

    assert res.get("ok") is True
    assert "LegalAdvisor" not in thread_state_manager.remaining_agents_map[thread_key]
    assert "Innovator" not in thread_state_manager.remaining_agents_map[thread_key]


@pytest.mark.anyio
async def test_process_and_respond_with_nl_exclude(monkeypatch):
    thread_state_manager.reset_all()

    channel_id = "C_NL_EXCLUDE"
    thread_ts = "88888.0004"
    thread_key = (channel_id, thread_ts)

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message = MagicMock(content="Debating topic [Sceptic]", tool_calls=None)
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=20, completion_tokens=40)

    async def mock_run_in_threadpool(func, *args, **kwargs):
        return mock_response

    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response",
        AsyncMock(return_value={"ok": True, "ts": thread_ts}),
    )
    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_run_in_threadpool
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        "nlp.intent_extractor.extract_intent",
        AsyncMock(
            return_value=ExtractedIntent(
                actions=[
                    ActionDirective(
                        name="exclude_agents", params={"agents": ["LegalAdvisor"]}
                    )
                ]
            )
        ),
    )

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="Raspravite o arhitekturi bez agenta LegalAdvisor",
        is_bot_trigger=False,
    )

    assert res.get("ok") is True
    assert "LegalAdvisor" not in thread_state_manager.remaining_agents_map[thread_key]


@pytest.mark.anyio
async def test_partial_tag_routing(monkeypatch):
    thread_state_manager.reset_all()

    channel_id = "C_PARTIAL_TAG"
    thread_ts = "99999.0001"

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message = MagicMock(
        content="Visionary speaking /[LegalAdvisor]", tool_calls=None
    )
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=20, completion_tokens=40)

    posted_messages = []

    async def mock_post_agent(channel_id, thread_ts, text, username, icon_url=None):
        posted_messages.append({"username": username, "text": text})
        return {"ok": True, "ts": thread_ts}

    monkeypatch.setattr(
        "orchestration.agent_router.post_agent_response", mock_post_agent
    )
    monkeypatch.setattr(
        "services.slack_client.post_agent_response", mock_post_agent
    )
    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool",
        AsyncMock(return_value=mock_response),
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )
    monkeypatch.setattr("orchestration.agent_router.record_thread_tokens", MagicMock())

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="/[Vision] what are your architectural thoughts?",
        is_bot_trigger=False,
    )

    assert res.get("ok") is True
    assert len(posted_messages) == 1
    assert "Visionary" in posted_messages[0]["username"]


@pytest.mark.anyio
async def test_ambiguous_tag_system_warning_halt(monkeypatch):
    thread_state_manager.reset_all()

    channel_id = "C_AMBIG_TAG"
    thread_ts = "99999.0002"

    system_posts = []

    async def mock_post_system(
        channel_id, thread_ts, text, username=None, icon_url=None
    ):
        system_posts.append({"username": username, "text": text})
        return {"ok": True, "ts": thread_ts}

    llm_called = False

    async def mock_run_in_threadpool(func, *args, **kwargs):
        nonlocal llm_called
        llm_called = True
        return MagicMock()

    monkeypatch.setattr(
        "orchestration.agent_router.post_system_message", mock_post_system
    )
    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_run_in_threadpool
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="/[a] please review this design",
        is_bot_trigger=False,
    )

    assert res.get("halted") is True
    assert res.get("reason") == "ambiguous_agent"
    assert llm_called is False
    assert len(system_posts) == 1
    assert "Ambiguous Agent Reference" in system_posts[0]["text"]
    assert "SecurityArchitect" in system_posts[0]["text"]
    assert "SecurityArchitect" in system_posts[0]["text"]
    assert "[" not in system_posts[0]["text"]
    assert "]" not in system_posts[0]["text"]


@pytest.mark.anyio
async def test_unrecognized_tag_system_warning_halt(monkeypatch):
    thread_state_manager.reset_all()

    channel_id = "C_UNREC_TAG"
    thread_ts = "99999.0003"

    system_posts = []

    async def mock_post_system(
        channel_id, thread_ts, text, username=None, icon_url=None
    ):
        system_posts.append({"username": username, "text": text})
        return {"ok": True, "ts": thread_ts}

    llm_called = False

    async def mock_run_in_threadpool(func, *args, **kwargs):
        nonlocal llm_called
        llm_called = True
        return MagicMock()

    monkeypatch.setattr(
        "orchestration.agent_router.post_system_message", mock_post_system
    )
    monkeypatch.setattr(
        "orchestration.agent_router.run_in_threadpool", mock_run_in_threadpool
    )
    monkeypatch.setattr(
        "orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[])
    )

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="/[SomeUnknownAgent] please review this design",
        is_bot_trigger=False,
    )

    assert res.get("halted") is True
    assert res.get("reason") == "unrecognized_agent"
    assert llm_called is False
    assert len(system_posts) == 1
    assert "Unrecognized Agent Tag" in system_posts[0]["text"]
    assert "SomeUnknownAgent" in system_posts[0]["text"]
    assert "[" not in system_posts[0]["text"]
    assert "]" not in system_posts[0]["text"]


@pytest.mark.anyio
async def test_plain_bracket_does_not_halt_or_warn(monkeypatch):
    """
    Verifies that plain brackets like [SomeUnknownTag] or [SecurityArchitect] do NOT trigger
    unrecognized tag warnings or nomination tags, and normal execution proceeds.
    """
    thread_state_manager.reset_all()

    channel_id = "C_PLAIN_BRACKET"
    thread_ts = "99999.0004"

    system_posts = []

    async def mock_post_system(channel_id, thread_ts, text, username=None, icon_url=None):
        system_posts.append({"username": username, "text": text})
        return {"ok": True, "ts": thread_ts}

    posted_messages = []
    async def mock_post_agent(channel_id, thread_ts, text, username, icon_url=None):
        posted_messages.append({"username": username, "text": text})
        return {"ok": True, "ts": thread_ts}

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message = MagicMock(content="Reviewing plain bracket design.", tool_calls=None)
    mock_response.choices = [mock_choice]
    mock_response.usage = None

    async def mock_run_in_threadpool(func, *args, **kwargs):
        return mock_response

    monkeypatch.setattr("orchestration.agent_router.post_system_message", mock_post_system)
    monkeypatch.setattr("orchestration.agent_router.post_agent_response", mock_post_agent)
    monkeypatch.setattr("orchestration.agent_router.run_in_threadpool", mock_run_in_threadpool)
    monkeypatch.setattr("orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[]))
    monkeypatch.setattr("orchestration.agent_router.record_thread_tokens", MagicMock())

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="[SomeUnknownTag] and [150/50] please review this design",
        is_bot_trigger=False,
    )

    # Must NOT be halted and no warning posted!
    assert res.get("halted") is not True
    assert len(system_posts) == 0
    assert len(posted_messages) == 1


@pytest.mark.anyio
async def test_multi_agent_nomination_syntax(monkeypatch):
    """
    Scenario: User prompt nominates multiple agents:
    '/[SecurityArchitect] /[Sceptic] /[Innovator] Pa sad ostatak prompta.'
    Expected:
    - First agent speaks (SecurityArchitect)
    - Remaining agents in thread are ['Sceptic', 'Innovator']
    - User prompt received by model is cleanly 'Pa sad ostatak prompta.'
    """
    thread_state_manager.reset_all()

    channel_id = "C_MULTI_NOM"
    thread_ts = "99999.0005"
    thread_key = (channel_id, thread_ts)

    captured_prompt = None

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message = MagicMock(content="Arhitektonska analiza.", tool_calls=None)
    mock_response.choices = [mock_choice]
    mock_response.usage = None

    async def mock_run_in_threadpool(func, *args, **kwargs):
        nonlocal captured_prompt
        messages = kwargs.get("messages") or args[2]
        captured_prompt = messages[-1]["content"]
        return mock_response

    posted_messages = []
    async def mock_post_agent(channel_id, thread_ts, text, username, icon_url=None):
        posted_messages.append({"username": username, "text": text})
        return {"ok": True, "ts": thread_ts}

    monkeypatch.setattr("orchestration.agent_router.post_agent_response", mock_post_agent)
    monkeypatch.setattr("orchestration.agent_router.run_in_threadpool", mock_run_in_threadpool)
    monkeypatch.setattr("orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[]))
    monkeypatch.setattr("orchestration.agent_router.record_thread_tokens", MagicMock())

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="/[SecurityArchitect] /[Sceptic] /[Innovator] Pa sad ostatak prompta.",
        is_bot_trigger=False,
    )

    assert res.get("ok") is True
    assert len(posted_messages) == 1
    assert posted_messages[0]["username"] == "Security Architect"
    assert thread_state_manager.remaining_agents_map[thread_key] == ["Sceptic", "Innovator"]
    assert "Pa sad ostatak prompta." in captured_prompt
    assert "/[SecurityArchitect]" not in captured_prompt
    assert "/[Sceptic]" not in captured_prompt
    assert "/[Innovator]" not in captured_prompt
