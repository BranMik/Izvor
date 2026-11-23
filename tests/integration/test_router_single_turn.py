from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.thread_notes_agent import configure_trigger_thread_notes_update
from agents.usage_agent import configure_record_thread_tokens
from conftest import async_noop_sleep, make_mock_acompletion
from core.llm_utils import configure_llm_completion_fn
from core.models import ActionDirective, ExtractedIntent
from nlp.intent_extractor import configure_intent_extractor
from nlp.message_builder import build_llm_messages
from orchestration.agent_router import (
    configure_router_sleep_fn,
    process_and_respond,
)
from config_store.agent_settings import (
    get_enabled_agents,
    reset_enabled_agents,
    set_fixed_config,
    set_orchestration_mode,
    set_sandwich_config,
)
from orchestration.nomination import parse_agent_and_prompt
from orchestration.thread_state import thread_state_manager
from services.slack import (
    configure_fetch_thread_history,
    configure_post_agent_response,
    configure_post_system_message,
)


@pytest.fixture(autouse=True)
def setup_test_environment():
    reset_enabled_agents()
    set_orchestration_mode("sandwich")
    set_sandwich_config([], [])
    set_fixed_config([])
    thread_state_manager.reset_all()
    configure_router_sleep_fn(async_noop_sleep)


def test_parse_explicit_bracket_tags():
    agent_key, prompt, was_explicit = parse_agent_and_prompt(
        "<@U12345> /[agent-10001] Create a new UI flow", "C12345", "123456789.0001"
    )
    assert agent_key == "agent-10001"
    assert prompt == "Create a new UI flow"
    assert was_explicit is True


def test_parse_mistral_techlead_tag():
    agent_key, prompt, was_explicit = parse_agent_and_prompt(
        "/[agent-10011] Check this database query", "C12345", "123456789.0001"
    )
    assert agent_key == "agent-10011"
    assert prompt == "Check this database query"
    assert was_explicit is True


def test_parse_multiple_tags_first_invalid():
    agent_key, prompt, was_explicit = parse_agent_and_prompt(
        "/[InvalidTag] /[agent-10011] Check this database query", "C12345", "123456789.0001"
    )
    assert agent_key == "agent-10011"
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
        "<@U12345> [agent-10001] generate a concept",
        "C12345",
        "123456789.0002",
    )
    assert agent_key_plain in get_enabled_agents()
    assert prompt_plain == "[agent-10001] generate a concept"
    assert was_explicit_plain is False

    # Slash bracketed tag is the only recognized explicit agent tag
    agent_key_tagged, prompt_tagged, was_explicit_tagged = parse_agent_and_prompt(
        "<@U12345> /[agent-10001] generate a concept",
        "C12345",
        "123456789.0003",
    )
    assert agent_key_tagged == "agent-10001"
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
async def test_process_and_respond_token_appending():
    mock_usage = MagicMock()
    mock_usage.prompt_tokens = 150
    mock_usage.completion_tokens = 50

    mock_choice = MagicMock()
    mock_choice.message.content = "This is a response message."
    mock_choice.message.tool_calls = None

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.usage = mock_usage

    posted_messages = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted_messages.append(text)
        return {"ok": True, "ts": "12345.6789"}

    configure_llm_completion_fn(make_mock_acompletion(lambda *a, **k: mock_response))
    configure_post_agent_response(mock_post_agent_response)
    configure_fetch_thread_history(AsyncMock(return_value=[]))

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.6789",
        raw_text="Hello /[agent-10001]",
    )

    assert len(posted_messages) == 1
    assert posted_messages[0].endswith("\n\n{gemini/gemini-3.8-flash : 150/50}")


@pytest.mark.anyio
async def test_process_and_respond_model_token_footer_openai():
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

    posted_messages = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted_messages.append(text)
        return {"ok": True, "ts": "12345.9999"}

    configure_llm_completion_fn(make_mock_acompletion(lambda *a, **k: mock_response))
    configure_post_agent_response(mock_post_agent_response)
    configure_fetch_thread_history(AsyncMock(return_value=[]))

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.9999",
        raw_text="Hello /[agent-10008]",
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
async def test_scenario_1_single_agent_tagged():
    """
    Scenario: User tags ONLY one agent.
    Expected: Single agent speaks, force_termination=True, any generated nomination tag stripped, flow cleanly ends.
    """
    thread_state_manager.reset_all()

    mock_choice = MagicMock()
    # Model attempts to nominate Sceptic even though user only wanted 1 agent
    mock_choice.message.content = "My single vision response. /[agent-10003]"
    mock_choice.message.tool_calls = None

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.usage = None

    def handle_turn(*args, **kwargs):
        captured_messages = kwargs.get("messages") or []
        handle_turn.captured_user_prompt = captured_messages[-1]["content"]
        return mock_response

    posted_messages = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted_messages.append(text)
        return {"ok": True, "ts": "12345.0001"}

    configure_llm_completion_fn(make_mock_acompletion(handle_turn))
    configure_post_agent_response(mock_post_agent_response)
    configure_fetch_thread_history(AsyncMock(return_value=[]))

    thread_key = ("C123", "12345.0001")

    await process_and_respond(
        channel_id="C123",
        thread_ts="12345.0001",
        raw_text="/[agent-10001] Explain this concept only",
        is_bot_trigger=False,
    )

    # Verify:
    # 1. User prompt forced termination
    assert "CRITICAL CONSTRAINT" in handle_turn.captured_user_prompt
    # 2. State cleaned up
    assert not thread_state_manager.discussion.is_custom_flow(thread_key[0], thread_key[1])
    assert not thread_state_manager.discussion.has_remaining_agents_key(thread_key[0], thread_key[1])
    # 3. Posted message has complete nomination stripped (not just brackets, agent name is removed too)
    assert len(posted_messages) == 1
    assert "/[agent-10003]" not in posted_messages[0]
    assert "agent-10003" not in posted_messages[0]
    assert posted_messages[0].startswith("My single vision response.")


@pytest.mark.anyio
async def test_extract_history_limit_formats():
    from pipeline.directive_parser import directive_parser

    # 1. Standard format /history 5
    res = await directive_parser.parse("<@U12345> Tell me a joke /history 5")
    assert res.history_limit == 5
    assert res.cleaned_prompt == "<@U12345> Tell me a joke"

    # 2. Bracketed format /history [3]
    res = await directive_parser.parse("@Izvor [agent-10011] Explain rust /history [3]")
    assert res.history_limit == 3
    assert res.cleaned_prompt == "@Izvor [agent-10011] Explain rust"

    # 3. Explicit int format /history [2:int]
    res = await directive_parser.parse("@app_mention /history [2:int] summarize")
    assert res.history_limit == 2
    assert res.cleaned_prompt == "@app_mention summarize"

    # 4. Colon format /history: 10 and /history:4
    res = await directive_parser.parse("@Izvor /history: 10 test prompt")
    assert res.history_limit == 10
    assert res.cleaned_prompt == "@Izvor test prompt"

    res = await directive_parser.parse("@Izvor /history:4 test prompt")
    assert res.history_limit == 4
    assert res.cleaned_prompt == "@Izvor test prompt"

    # 5. Equals format /history=7
    res = await directive_parser.parse("@Izvor /history=7 test prompt")
    assert res.history_limit == 7
    assert res.cleaned_prompt == "@Izvor test prompt"

    # 6. Zero history /history 0
    res = await directive_parser.parse("Prompt with /history 0")
    assert res.history_limit == 0
    assert res.cleaned_prompt == "Prompt with"

    # 7. No history command
    res = await directive_parser.parse("Plain prompt without history")
    assert res.history_limit is None
    assert res.cleaned_prompt == "Plain prompt without history"

    # 8. Command in middle of text
    res = await directive_parser.parse("@Izvor What is /history 3 quantum computing?")
    assert res.history_limit == 3
    assert res.cleaned_prompt == "@Izvor What is quantum computing?"
    # limit, cleaned = extract_history_limit("last 3 messages analyze the issue")
    # assert limit == 3
    # assert cleaned == "analyze the issue"
    #
    # limit, cleaned = extract_history_limit("history of 4 messages and respond")
    # assert limit == 4
    # assert cleaned == "and respond"


def test_parse_agent_and_prompt_cleans_history_command():
    agent_key, prompt, was_explicit = parse_agent_and_prompt(
        "<@U12345> /[agent-10001] What is AI? /history 4",
        "C_HIST_PARSE",
        "12345.0001",
    )
    assert agent_key == "agent-10001"
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
async def test_process_and_respond_history_limit_initial_prompt():
    """
    Scenario: Thread has 5 prior messages. User submits prompt with /history 2.
    Expected:
    - LiteLLM receives only the last 2 prior messages + current user prompt.
    - Active limit is stored in THREAD_HISTORY_LIMITS.
    """
    thread_key = ("C_HIST_TEST", "12345.2001")
    thread_state_manager.context.clear_history_limit(thread_key[0], thread_key[1])
    thread_state_manager.discussion.clear_remaining_agents(thread_key[0], thread_key[1])

    mock_history = [
        {"role": "user", "content": "Message 1"},
        {"role": "assistant", "content": "Reply 1"},
        {"role": "user", "content": "Message 2"},
        {"role": "assistant", "content": "Reply 2"},
        {"role": "user", "content": "Message 3"},
        {"role": "user", "content": "<@U12345> What now? /history 2"},
    ]

    configure_fetch_thread_history(AsyncMock(return_value=mock_history))

    captured_messages = []

    mock_choice = MagicMock()
    mock_choice.message.content = "Visionary response. /[agent-10011]"
    mock_choice.message.tool_calls = None
    mock_resp = MagicMock(choices=[mock_choice], usage=None)

    def handle_turn(*args, **kwargs):
        msgs = kwargs.get("messages") or []
        if msgs:
            captured_messages.extend(msgs)
        return mock_resp

    configure_llm_completion_fn(make_mock_acompletion(handle_turn))
    configure_post_agent_response(AsyncMock(return_value={"ok": True, "ts": "12345.2002"}))

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
    # 3: Message 3 coalesced with What now? (strict cross-provider alternating roles)
    assert len(captured_messages) == 4
    assert captured_messages[0]["role"] == "system"
    assert captured_messages[1]["role"] == "user"
    assert captured_messages[2] == {"role": "assistant", "content": "Reply 2"}
    assert captured_messages[3]["role"] == "user"
    assert "Message 3" in captured_messages[3]["content"]
    assert "What now?" in captured_messages[3]["content"]

    # Verify THREAD_HISTORY_LIMITS is preserved for subsequent bot turns
    limit_info = thread_state_manager.get_history_limit("C_HIST_TEST", "12345.2001")
    assert limit_info is not None
    assert limit_info["limit"] == 2
    assert limit_info["prior_cutoff_index"] == 5


@pytest.mark.anyio
async def test_process_and_respond_history_zero():
    """
    Scenario: User specifies /history 0.
    Expected:
    - LiteLLM receives ONLY system prompt + current user prompt.
    """
    thread_key = ("C_HIST_ZERO", "12345.5001")
    thread_state_manager.context.clear_history_limit(thread_key[0], thread_key[1])

    mock_history = [
        {"role": "user", "content": "Message 1"},
        {"role": "assistant", "content": "Reply 1"},
        {"role": "user", "content": "<@U12345> Fresh start /history 0"},
    ]

    configure_fetch_thread_history(AsyncMock(return_value=mock_history))

    captured_messages = []

    mock_choice = MagicMock()
    mock_choice.message.content = "Fresh response. [agent-10011]"
    mock_choice.message.tool_calls = None
    mock_resp = MagicMock(choices=[mock_choice], usage=None)

    def handle_turn(*args, **kwargs):
        msgs = kwargs.get("messages") or []
        if msgs and not captured_messages:
            captured_messages.extend(msgs)
        return mock_resp

    configure_trigger_thread_notes_update(MagicMock())
    configure_llm_completion_fn(make_mock_acompletion(handle_turn))
    configure_post_agent_response(AsyncMock(return_value={"ok": True, "ts": "12345.5002"}))

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
        {"role": "assistant", "content": "[agent-10002]: Propose X [agent-10003]"},
    ]
    agent_cfg = {"username": "The Sceptic"}
    messages = build_llm_messages(
        system_prompt=sys_prompt,
        effective_history=history,
        prompt="Propose X",
        is_bot_trigger=True,
        agent_key="agent-10003",
        agent_cfg=agent_cfg,
    )
    assert len(messages) == 4
    assert messages[0] == {"role": "system", "content": sys_prompt}
    assert messages[1] == {"role": "user", "content": "Initial question"}
    assert messages[2] == {
        "role": "assistant",
        "content": "[agent-10002]: Propose X [agent-10003]",
    }
    assert messages[3]["role"] == "user"
    assert "The Sceptic" in messages[3]["content"]


def test_build_llm_messages_leading_assistant_gets_user_prefix():
    """TKT-1217: trimmed history starting with consecutive assistant turns gets user prefix and coalesces adjacent assistant turns."""
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
        agent_key="agent-10011",
        agent_cfg=agent_cfg,
    )
    # index 0: system
    # index 1: user prefix
    # index 2: coalesced assistant turns
    # index 3: user closing directive
    assert len(messages) == 4
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert messages[2] == {"role": "assistant", "content": "Assistant turn 1\n\nAssistant turn 2"}
    assert messages[3]["role"] == "user"


def test_build_llm_messages_normalizes_mid_system_and_coalesces_consecutive_roles():
    """TKT-1217: converts mid-history system roles into user notifications and alternates [system, user, assistant, user]."""
    messages = build_llm_messages(
        system_prompt="System prompt",
        effective_history=[
            {"role": "user", "content": "User turn 1"},
            {"role": "assistant", "username": "Visionary", "content": "A1"},
            {"role": "assistant", "username": "Sceptic", "content": "A2"},
            {"role": "assistant", "username": "Critic", "content": "A3"},
            {"role": "system", "content": "[Internal Revisor Warning]: Check evidence"},
        ],
        prompt="Next turn",
        is_bot_trigger=True,
        agent_key="agent-10006",
        agent_cfg={"username": "Arbitrator"},
    )
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert all("username" not in m for m in messages)
    assert messages[2]["content"] == "A1\n\nA2\n\nA3"
    assert "[System Notification]" in messages[3]["content"]
    assert "Check evidence" in messages[3]["content"]


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
async def test_process_and_respond_with_max_words():
    thread_state_manager.reset_all()

    channel_id = "C_MAX_WORDS"
    thread_ts = "99999.0001"
    thread_key = (channel_id, thread_ts)
    thread_state_manager.discussion.set_remaining_agents(thread_key[0], thread_key[1], ["agent-10008"])

    captured_messages = []

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message = MagicMock(
        content="Concise response under limit /[agent-10008]", tool_calls=None
    )
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=15, completion_tokens=10)

    def handle_turn(*args, **kwargs):
        captured_messages.append(kwargs.get("messages") or [])
        return mock_response

    posted_messages = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted_messages.append({"text": text, "username": username})
        return {"ok": True, "ts": thread_ts}

    configure_post_agent_response(mock_post_agent_response)
    configure_llm_completion_fn(make_mock_acompletion(handle_turn))
    configure_fetch_thread_history(AsyncMock(return_value=[]))
    configure_record_thread_tokens(MagicMock())

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="/[agent-10011] /[agent-10008] /max_words [40:int] Explain network ingress architecture",
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
    # Compact mode (40 < 60): skips 3-part output_constraints and strips <output_contract>
    assert "<output_contract>" not in system_msg
    assert "1 sentence stating your core verdict/concern." not in system_msg
    assert "2-3 concise bullet points" not in system_msg

    # User message had the /max_words command stripped cleanly
    user_msg = captured_messages[0][-1]["content"]
    assert "/max_words" not in user_msg
    assert "Explain network ingress architecture" in user_msg


@pytest.mark.anyio
async def test_process_and_respond_with_min_words():
    thread_state_manager.reset_all()

    channel_id = "C_MIN_WORDS"
    thread_ts = "88888.0001"
    thread_key = (channel_id, thread_ts)
    thread_state_manager.discussion.set_remaining_agents(thread_key[0], thread_key[1], ["agent-10008"])

    captured_messages = []

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message = MagicMock(
        content="Detailed thorough response with at least eighty words /[agent-10008]",
        tool_calls=None,
    )
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=15, completion_tokens=85)

    def handle_turn(*args, **kwargs):
        captured_messages.append(kwargs.get("messages") or [])
        return mock_response

    posted_messages = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted_messages.append({"text": text, "username": username})
        return {"ok": True, "ts": thread_ts}

    configure_post_agent_response(mock_post_agent_response)
    configure_llm_completion_fn(make_mock_acompletion(handle_turn))
    configure_fetch_thread_history(AsyncMock(return_value=[]))
    configure_record_thread_tokens(MagicMock())

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="/[agent-10011] /[agent-10008] /min_words [80:int] Explain network ingress architecture",
        is_bot_trigger=False,
    )

    assert res.get("ok") is True
    # Thread level setting persisted for remaining agents in flow
    assert thread_state_manager.get_min_words(channel_id, thread_ts) == 80

    # System prompt in LLM call has min_words instruction, removes 200w ceiling, and preserves 3-part Rules 2-4
    assert len(captured_messages) == 1
    system_msg = captured_messages[0][0]["content"]
    assert (
        "Minimum Length: Your entire response must have a minimum length of 80 words."
        in system_msg
    )
    assert "Hard Limit: Your entire response must strictly NOT exceed 200 words." not in system_msg
    assert "1 sentence stating your core verdict/concern." in system_msg
    assert "2-3 concise bullet points" in system_msg

    # User message had the /min_words command stripped cleanly
    user_msg = captured_messages[0][-1]["content"]
    assert "/min_words" not in user_msg
    assert "Explain network ingress architecture" in user_msg


@pytest.mark.anyio
async def test_process_and_respond_with_min_and_max_words():
    thread_state_manager.reset_all()

    channel_id = "C_MIN_MAX_WORDS"
    thread_ts = "77777.0001"
    thread_key = (channel_id, thread_ts)
    thread_state_manager.discussion.set_remaining_agents(thread_key[0], thread_key[1], ["agent-10008"])

    captured_messages = []

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message = MagicMock(
        content="Response between bounds /[agent-10008]", tool_calls=None
    )
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=15, completion_tokens=60)

    def handle_turn(*args, **kwargs):
        captured_messages.append(kwargs.get("messages") or [])
        return mock_response

    posted_messages = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted_messages.append({"text": text, "username": username})
        return {"ok": True, "ts": thread_ts}

    configure_post_agent_response(mock_post_agent_response)
    configure_llm_completion_fn(make_mock_acompletion(handle_turn))
    configure_fetch_thread_history(AsyncMock(return_value=[]))
    configure_record_thread_tokens(MagicMock())

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="/[agent-10011] /[agent-10008] /min_words [50:int] /max_words [150:int] Analyze database scaling",
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
async def test_process_and_respond_with_natural_language_limits():
    thread_state_manager.reset_all()

    channel_id = "C_NL_LIMITS"
    thread_ts = "66666.0001"
    thread_key = (channel_id, thread_ts)
    thread_state_manager.discussion.set_remaining_agents(thread_key[0], thread_key[1], ["agent-10008"])

    captured_messages = []

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message = MagicMock(
        content="Response under 60 words /[agent-10008]", tool_calls=None
    )
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=15, completion_tokens=30)

    def handle_turn(*args, **kwargs):
        captured_messages.append(kwargs.get("messages") or [])
        return mock_response

    posted_messages = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted_messages.append({"text": text, "username": username})
        return {"ok": True, "ts": thread_ts}

    configure_post_agent_response(mock_post_agent_response)
    configure_llm_completion_fn(make_mock_acompletion(handle_turn))
    configure_fetch_thread_history(AsyncMock(return_value=[]))
    configure_intent_extractor(
        AsyncMock(return_value=ExtractedIntent(modifiers={"max_words": 60}))
    )

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="/[agent-10011] /[agent-10008] odgovori u maksimalno 60 riječi o mikroservisima",
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
    # assert "maximum 60 words" not in user_msg
    assert "mikroservisima" in user_msg


@pytest.mark.anyio
async def test_process_and_respond_with_exclude_command():
    thread_state_manager.reset_all()

    channel_id = "C_EXCLUDE_CMD"
    thread_ts = "88888.0001"
    thread_key = (channel_id, thread_ts)

    captured_messages = []

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message = MagicMock(
        content="Reviewing without innovator /[agent-10005]", tool_calls=None
    )
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=20, completion_tokens=40)

    def handle_turn(*args, **kwargs):
        captured_messages.append(kwargs.get("messages") or [])
        return mock_response

    posted_messages = []

    async def mock_post_agent_response(
        channel_id, thread_ts, text, username, icon_url=None
    ):
        posted_messages.append({"text": text, "username": username})
        return {"ok": True, "ts": thread_ts}

    configure_post_agent_response(mock_post_agent_response)
    configure_llm_completion_fn(make_mock_acompletion(handle_turn))
    configure_fetch_thread_history(AsyncMock(return_value=[]))
    configure_record_thread_tokens(MagicMock())

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="/[agent-10011] /[agent-10002] /[agent-10005] /exclude [agent-10002] Evaluate the refactoring",
        is_bot_trigger=False,
    )

    assert res.get("ok") is True
    # Innovator should be removed immediately from remaining agents
    assert "agent-10002" not in (thread_state_manager.discussion.get_remaining_agents(thread_key[0], thread_key[1]) or [])
    assert thread_state_manager.discussion.get_remaining_agents(thread_key[0], thread_key[1]) == ["agent-10005"]

    # Command should be cleanly stripped from user message passed to model
    user_msg = captured_messages[0][-1]["content"]
    assert "/exclude" not in user_msg
    assert "Evaluate the refactoring" in user_msg


@pytest.mark.anyio
async def test_process_and_respond_with_multi_agent_exclude_command():
    thread_state_manager.reset_all()

    channel_id = "C_MULTI_EXCLUDE"
    thread_ts = "88888.0002"
    thread_key = (channel_id, thread_ts)

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message = MagicMock(
        content="Reviewing /[agent-10007]", tool_calls=None
    )
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=20, completion_tokens=40)

    configure_post_agent_response(AsyncMock(return_value={"ok": True, "ts": thread_ts}))
    configure_llm_completion_fn(make_mock_acompletion(lambda *a, **k: mock_response))
    configure_fetch_thread_history(AsyncMock(return_value=[]))
    configure_record_thread_tokens(MagicMock())

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="/[agent-10011] /[agent-10002] /[agent-10005] /[agent-10007] /exclude [agent-10002] [agent-10005] Plan sprint",
        is_bot_trigger=False,
    )

    assert res.get("ok") is True
    # Both Innovator and ScopeOptimizer should be removed from remaining agents
    assert thread_state_manager.discussion.get_remaining_agents(thread_key[0], thread_key[1]) == ["agent-10007"]


@pytest.mark.anyio
async def test_process_and_respond_with_default_flow_and_exclude():
    thread_state_manager.reset_all()

    channel_id = "C_DEFAULT_EXCLUDE"
    thread_ts = "88888.0003"
    thread_key = (channel_id, thread_ts)

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message = MagicMock(
        content="Debating without techlead and innovator /[agent-10003]", tool_calls=None
    )
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=20, completion_tokens=40)

    configure_post_agent_response(AsyncMock(return_value={"ok": True, "ts": thread_ts}))
    configure_llm_completion_fn(make_mock_acompletion(lambda *a, **k: mock_response))
    configure_fetch_thread_history(AsyncMock(return_value=[]))
    configure_record_thread_tokens(MagicMock())

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="Debate this architecture /exclude [agent-10011] [agent-10002]",
        is_bot_trigger=False,
    )

    assert res.get("ok") is True
    assert "agent-10011" not in (thread_state_manager.discussion.get_remaining_agents(thread_key[0], thread_key[1]) or [])
    assert "agent-10002" not in (thread_state_manager.discussion.get_remaining_agents(thread_key[0], thread_key[1]) or [])


@pytest.mark.anyio
async def test_process_and_respond_with_nl_exclude():
    thread_state_manager.reset_all()

    channel_id = "C_NL_EXCLUDE"
    thread_ts = "88888.0004"
    thread_key = (channel_id, thread_ts)

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message = MagicMock(content="Debating topic [agent-10003]", tool_calls=None)
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=20, completion_tokens=40)

    configure_post_agent_response(AsyncMock(return_value={"ok": True, "ts": thread_ts}))
    configure_llm_completion_fn(make_mock_acompletion(lambda *a, **k: mock_response))
    configure_fetch_thread_history(AsyncMock(return_value=[]))
    configure_intent_extractor(
        AsyncMock(
            return_value=ExtractedIntent(
                actions=[
                    ActionDirective(
                        name="exclude_agents", params={"agents": ["agent-10011"]}
                    )
                ]
            )
        )
    )

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="Raspravite o arhitekturi bez agenta LegalAdvisor",
        is_bot_trigger=False,
    )

    assert res.get("ok") is True
    assert "agent-10011" not in (thread_state_manager.discussion.get_remaining_agents(thread_key[0], thread_key[1]) or [])


@pytest.mark.anyio
async def test_partial_tag_routing():
    thread_state_manager.reset_all()

    channel_id = "C_PARTIAL_TAG"
    thread_ts = "99999.0001"

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message = MagicMock(
        content="Visionary speaking /[agent-10011]", tool_calls=None
    )
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=20, completion_tokens=40)

    posted_messages = []

    async def mock_post_agent(channel_id, thread_ts, text, username, icon_url=None):
        posted_messages.append({"username": username, "text": text})
        return {"ok": True, "ts": thread_ts}

    configure_post_agent_response(mock_post_agent)
    configure_llm_completion_fn(make_mock_acompletion(lambda *a, **k: mock_response))
    configure_fetch_thread_history(AsyncMock(return_value=[]))
    configure_record_thread_tokens(MagicMock())

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
async def test_ambiguous_tag_system_warning_halt():
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

    def handle_turn(*args, **kwargs):
        nonlocal llm_called
        llm_called = True
        return MagicMock()

    configure_post_system_message(mock_post_system)
    configure_llm_completion_fn(make_mock_acompletion(handle_turn))
    configure_fetch_thread_history(AsyncMock(return_value=[]))

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
    assert "agent-10008" in system_posts[0]["text"]
    assert "agent-10008" in system_posts[0]["text"]
    assert "[" not in system_posts[0]["text"]
    assert "]" not in system_posts[0]["text"]


@pytest.mark.anyio
async def test_unrecognized_tag_system_warning_halt():
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

    def handle_turn(*args, **kwargs):
        nonlocal llm_called
        llm_called = True
        return MagicMock()

    configure_post_system_message(mock_post_system)
    configure_llm_completion_fn(make_mock_acompletion(handle_turn))
    configure_fetch_thread_history(AsyncMock(return_value=[]))

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
async def test_plain_bracket_does_not_halt_or_warn():
    """
    Verifies that plain brackets like [SomeUnknownTag] or [agent-10008] do NOT trigger
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

    configure_post_system_message(mock_post_system)
    configure_post_agent_response(mock_post_agent)
    configure_llm_completion_fn(make_mock_acompletion(lambda *a, **k: mock_response))
    configure_fetch_thread_history(AsyncMock(return_value=[]))
    configure_record_thread_tokens(MagicMock())

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
async def test_multi_agent_nomination_syntax():
    """
    Scenario: User prompt nominates multiple agents:
    '/[agent-10008] /[agent-10003] /[agent-10002] Pa sad ostatak prompta.'
    Expected:
    - First agent speaks (SecurityArchitect)
    - Remaining agents in thread are ['agent-10003', 'agent-10002']
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

    def handle_turn(*args, **kwargs):
        nonlocal captured_prompt
        messages = kwargs.get("messages") or []
        captured_prompt = messages[-1]["content"]
        return mock_response

    posted_messages = []
    async def mock_post_agent(channel_id, thread_ts, text, username, icon_url=None):
        posted_messages.append({"username": username, "text": text})
        return {"ok": True, "ts": thread_ts}

    configure_post_agent_response(mock_post_agent)
    configure_llm_completion_fn(make_mock_acompletion(handle_turn))
    configure_fetch_thread_history(AsyncMock(return_value=[]))
    configure_record_thread_tokens(MagicMock())

    await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="/[agent-10008] /[agent-10003] /[agent-10002] Pa sad ostatak prompta.",
        is_bot_trigger=False,
    )

    assert len(posted_messages) == 1
    assert posted_messages[0]["username"] == "SecurityArchitect"
    assert thread_state_manager.discussion.get_remaining_agents(thread_key[0], thread_key[1]) == ["agent-10003", "agent-10002"]
    assert "Pa sad ostatak prompta." in captured_prompt
    assert "/[agent-10008]" not in captured_prompt
    assert "/[agent-10003]" not in captured_prompt
    assert "/[agent-10002]" not in captured_prompt


@pytest.mark.anyio
async def test_process_and_respond_persists_string_valued_max_words_from_intent():
    """LLM-inferred max_words returned as a JSON string ("25") must still be coerced and persisted,
    so both the prompt-level limit and the mechanical cutoff activate."""
    thread_state_manager.reset_all()

    channel_id = "C_STR_MAXWORDS"
    thread_ts = "55555.0001"
    thread_key = (channel_id, thread_ts)
    thread_state_manager.discussion.set_remaining_agents(thread_key[0], thread_key[1], ["agent-10008"])

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message = MagicMock(content="Kratak odgovor /[agent-10008]", tool_calls=None)
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=10, completion_tokens=20)

    configure_post_agent_response(AsyncMock(return_value={"ok": True, "ts": thread_ts}))
    configure_llm_completion_fn(make_mock_acompletion(lambda *a, **k: mock_response))
    configure_fetch_thread_history(AsyncMock(return_value=[]))
    configure_record_thread_tokens(MagicMock())
    configure_intent_extractor(
        AsyncMock(return_value=ExtractedIntent(modifiers={"max_words": "25"}))
    )

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="Novi test, do 25 rijeci.",
        is_bot_trigger=False,
    )

    assert res.get("ok") is True
    assert thread_state_manager.get_max_words(channel_id, thread_ts) == 25


@pytest.mark.anyio
async def test_ambiguous_tag_halt_with_empty_agents_config():
    """TKT-1211 (H11): the ambiguous-tag halt path must not raise UnboundLocalError (F823)
    even when settings_manager.agents_config is empty (falls back to AGENTS_CONFIG)."""
    import copy
    from config_store.agent_settings import settings_manager

    thread_state_manager.reset_all()
    orig_agents_cfg = copy.deepcopy(settings_manager.agents_config)
    settings_manager.agents_config.clear()
    # With agents_config empty, the resolver sees only Challenger/Arbitrator, so '/[r]'
    # is still ambiguous (substring match) while validation falls back to AGENTS_CONFIG.

    channel_id = "C_AMBIG_EMPTY"
    thread_ts = "99999.0003"

    system_posts = []

    async def mock_post_system(
        channel_id, thread_ts, text, username=None, icon_url=None
    ):
        system_posts.append({"username": username, "text": text})
        return {"ok": True, "ts": thread_ts}

    llm_called = False

    def handle_turn(*args, **kwargs):
        nonlocal llm_called
        llm_called = True
        return MagicMock()

    configure_post_system_message(mock_post_system)
    configure_llm_completion_fn(make_mock_acompletion(handle_turn))
    configure_fetch_thread_history(AsyncMock(return_value=[]))

    try:
        res = await process_and_respond(
            channel_id=channel_id,
            thread_ts=thread_ts,
            raw_text="/[r] please review this design",
            is_bot_trigger=False,
        )

        assert res.get("halted") is True
        assert res.get("reason") == "ambiguous_agent"
        assert llm_called is False
        assert len(system_posts) == 1
        assert "Ambiguous Agent Reference" in system_posts[0]["text"]
        assert "Challenger" in system_posts[0]["text"]
        assert "Arbitrator" in system_posts[0]["text"]
    finally:
        settings_manager.agents_config.clear()
        settings_manager.agents_config.update(orig_agents_cfg)
        thread_state_manager.cleanup_thread(channel_id, thread_ts)


@pytest.mark.anyio
async def test_process_and_respond_internal_loop_runs_guardrail_once():
    """TKT-1221 (H16): process_and_respond(..., run_internal_loop=True) must invoke guardrail exactly once per human prompt."""
    from orchestration.agent_router import configure_guardrail_checker, reset_guardrail_checker

    thread_state_manager.reset_all()
    set_orchestration_mode("fixed")
    set_fixed_config(["agent-10001"])

    channel_id = "C_GUARD_ONCE"
    thread_ts = "99999.0004"

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message = MagicMock(content="Reviewed cleanly.", tool_calls=None)
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=10, completion_tokens=15)

    guardrail_mock = AsyncMock(return_value=False)
    configure_guardrail_checker(guardrail_mock)
    configure_post_agent_response(AsyncMock(return_value={"ok": True, "ts": thread_ts}))
    configure_llm_completion_fn(make_mock_acompletion(lambda *a, **k: mock_response))
    configure_fetch_thread_history(AsyncMock(return_value=[]))
    configure_record_thread_tokens(MagicMock())
    configure_intent_extractor(AsyncMock(return_value=ExtractedIntent()))

    try:
        res = await process_and_respond(
            channel_id=channel_id,
            thread_ts=thread_ts,
            raw_text="Review caching strategy",
            is_bot_trigger=False,
            run_internal_loop=True,
        )
        assert res.get("ok") is True
        assert guardrail_mock.await_count == 1
    finally:
        reset_guardrail_checker()
        thread_state_manager.cleanup_thread(channel_id, thread_ts)


def test_thread_state_batched_clear_and_interrupt_single_update_per_store():
    """TKT-1221: interrupt_discussion() and clear_round_execution_state() batch resets into one update per store."""
    from orchestration.state import DiscussionStateStore, ThreadContextStore
    from orchestration.thread_state import ThreadStateManager

    class _CountingDiscussionStore(DiscussionStateStore):
        def __init__(self) -> None:
            super().__init__()
            self.update_calls: list[dict] = []

        def _update_fields(self, channel_id: str, thread_ts: str, **fields) -> None:
            self.update_calls.append(dict(fields))
            super()._update_fields(channel_id, thread_ts, **fields)

    class _CountingContextStore(ThreadContextStore):
        def __init__(self) -> None:
            super().__init__()
            self.update_calls: list[dict] = []

        def _update_fields(self, channel_id: str, thread_ts: str, **fields) -> None:
            self.update_calls.append(dict(fields))
            super()._update_fields(channel_id, thread_ts, **fields)

    ch = "C_BATCH_CLEAR"
    ts = "99999.0005"
    mgr = ThreadStateManager()
    mgr.discussion = _CountingDiscussionStore()
    mgr.context = _CountingContextStore()
    mgr.cleanup_thread(ch, ts)

    def _populate():
        mgr.set_remaining_agents(ch, ts, ["agent-10001", "agent-10002"])
        mgr.set_rounds(ch, ts, 2)
        mgr.set_open_debate(ch, ts, True)
        mgr.set_consensus_reached(ch, ts, True)
        mgr.record_convergence_speaker(ch, ts, "agent-10001")
        mgr.set_is_pure_skill_task(ch, ts, True)
        mgr.set_history_limit(ch, ts, limit=5, prior_cutoff_index=3)
        mgr.set_file_context(ch, ts, {"file_name": "notes.md", "content": "abc"})
        mgr.evidence.add_evidence(
            ch, ts, "web_search", {"q": "cache"}, "Evidence body", agent_key="agent-10001"
        )

    try:
        _populate()
        mgr.discussion.update_calls.clear()
        mgr.context.update_calls.clear()

        mgr.interrupt_discussion(ch, ts)
        assert len(mgr.discussion.update_calls) == 1
        assert len(mgr.context.update_calls) == 1
        assert mgr.is_stopped(ch, ts) is False
        assert mgr.get_remaining_agents(ch, ts) == []
        assert mgr.is_consensus_reached(ch, ts) is False
        assert mgr.get_convergence_speakers(ch, ts) == []
        assert mgr.get_rounds(ch, ts) == 0
        assert mgr.is_open_debate(ch, ts) is False
        assert mgr.get_history_limit(ch, ts) is None
        assert mgr.get_file_context(ch, ts) is None
        assert mgr.format_round_evidence(ch, ts) is None

        _populate()
        mgr.discussion.update_calls.clear()
        mgr.context.update_calls.clear()
        mgr.request_stop(ch, ts)
        assert len(mgr.discussion.update_calls) == 1
        assert len(mgr.context.update_calls) == 1
        assert mgr.is_stopped(ch, ts) is True

        _populate()
        mgr.discussion.update_calls.clear()
        mgr.context.update_calls.clear()
        mgr.clear_round_execution_state(ch, ts)
        assert len(mgr.discussion.update_calls) == 1
        assert len(mgr.context.update_calls) == 1
        assert mgr.is_stopped(ch, ts) is False
        assert mgr.get_remaining_agents(ch, ts) == []
        assert mgr.is_consensus_reached(ch, ts) is False
        assert mgr.get_convergence_speakers(ch, ts) == []
        assert mgr.get_file_context(ch, ts) is None
        # clear_round_execution_state clears ephemeral state but PRESERVES verified round evidence
        evidence = mgr.format_round_evidence(ch, ts)
        assert evidence is not None and "Evidence body" in evidence
    finally:
        mgr.cleanup_thread(ch, ts)
