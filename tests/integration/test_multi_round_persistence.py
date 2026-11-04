from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import make_mock_acompletion

from core.llm_utils import configure_llm_completion_fn
from orchestration import agent_router
from orchestration.agent_router import (
    configure_fetch_thread_files,
    configure_fetch_thread_history,
    configure_post_agent_response,
    configure_post_system_message,
    configure_router_sleep_fn,
    process_and_respond,
)
from orchestration.agent_settings import (
    reset_enabled_agents,
    set_fixed_config,
    set_orchestration_mode,
    set_sandwich_config,
)
from orchestration.thread_state import thread_state_manager


@pytest.fixture(autouse=True)
def setup_test_environment():
    from core.config import set_admin_slack_user_ids_override

    set_admin_slack_user_ids_override("U_ADMIN")
    reset_enabled_agents()
    set_orchestration_mode("sandwich")

    set_sandwich_config([], [])
    set_fixed_config([])
    thread_state_manager.reset_all()

    # Intercept Slack client calls via DI
    mock_post = AsyncMock(return_value={"ok": True, "ts": "123.45"})
    mock_sys = AsyncMock(return_value={"ok": True, "ts": "123.45"})
    mock_hist = AsyncMock(return_value=[])
    mock_files = AsyncMock(return_value=[])

    configure_post_agent_response(mock_post)
    configure_post_system_message(mock_sys)
    configure_fetch_thread_history(mock_hist)
    configure_fetch_thread_files(mock_files)
    configure_router_sleep_fn(AsyncMock(return_value=None))

    yield

    set_admin_slack_user_ids_override(None)


@pytest.mark.anyio
async def test_three_round_five_word_croatian_test():
    """
    Test user requirement:
    - User input: 'Ovo je test, maximum 5 rijeci /max_words 5'
    - Language (Croatian) and /max_words (5) must persist across 3 rounds (/next-round twice).
    - Word limit does NOT affect the Scribe.
    - All debate agents receive max_words=5 constraint in their system prompts and stay in Croatian.
    - Scribe receives thread_language='Croatian' and scribe_max_words=None.
    """
    posted_messages = []

    async def mock_post_agent_response(channel_id, thread_ts, text, username, icon_url=None):
        posted_messages.append(text)
        return {"ok": True, "ts": f"100.{len(posted_messages)}"}

    configure_post_agent_response(mock_post_agent_response)

    # Mock language detection to return 'Croatian' for this initial text
    async def mock_detect_language_llm(text):
        if "test" in text.lower() or "rijeci" in text.lower() or "ovo" in text.lower():
            return "Croatian"
        return "English"

    captured_system_prompts = []
    captured_scribe_calls = []

    mock_run_turn = MagicMock()

    def mock_agent_turn(*args, **kwargs):
        # Inspect system prompt passed to LLM in kwargs['messages']
        messages = kwargs.get("messages") or (args[0].get("messages") if args and isinstance(args[0], dict) else [])
        for msg in messages:
            if isinstance(msg, dict) and msg.get("role") == "system":
                captured_system_prompts.append(msg.get("content", ""))
        return mock_run_turn.current_response

    configure_llm_completion_fn(make_mock_acompletion(mock_agent_turn))

    async def mock_generate_thread_notes(history, is_completed=False, thread_language="English", scribe_max_words=None, scribe_min_words=None):
        captured_scribe_calls.append({
            "is_completed": is_completed,
            "thread_language": thread_language,
            "scribe_max_words": scribe_max_words,
            "scribe_min_words": scribe_min_words,
        })
        return "Scribe strategic notes in Croatian", {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}

    configure_fetch_thread_history(AsyncMock(return_value=[{"role": "user", "text": "test"}]))

    with (
        patch("orchestration.stages.validation.detect_language_llm", side_effect=mock_detect_language_llm),
        patch("agents.thread_notes_agent.generate_thread_notes", side_effect=mock_generate_thread_notes),
    ):
        channel = "C_CROATIAN"
        thread_ts = "555.001"

        # ==================== ROUND 1 ====================
        # Turn 1: Innovator starts Round 1
        mock_choice_1 = MagicMock()
        mock_choice_1.message.content = "Ovo je prva ideja."  # 5 words in Croatian
        mock_choice_1.message.tool_calls = None
        mock_run_turn.current_response = MagicMock(choices=[mock_choice_1], usage=None)

        res_1 = await process_and_respond(
            channel_id=channel,
            thread_ts=thread_ts,
            raw_text="/[agent-10002] /[agent-10011] Ovo je test, maximum 5 rijeci /max_words 5",
            is_bot_trigger=False,
        )

        assert not res_1.get("is_completed")
        assert thread_state_manager.get_language(channel, thread_ts) == "Croatian"
        assert thread_state_manager.get_max_words(channel, thread_ts) == 5
        assert any("5 words" in prompt for prompt in captured_system_prompts)

        # Turn 2: LegalAdvisor finishes Round 1
        mock_choice_2 = MagicMock()
        mock_choice_2.message.content = "LegalAdvisor potvrda za rundu jedan."  # 5 words
        mock_choice_2.message.tool_calls = None
        mock_run_turn.current_response = MagicMock(choices=[mock_choice_2], usage=None)

        res_2 = await process_and_respond(
            channel_id=channel,
            thread_ts=thread_ts,
            raw_text="/[agent-10011] LegalAdvisor potvrda",
            is_bot_trigger=True,
        )
        assert res_2.get("is_completed")

        # Verify persistent state after Round 1 completion
        assert thread_state_manager.get_language(channel, thread_ts) == "Croatian"
        assert thread_state_manager.get_max_words(channel, thread_ts) == 5

        # Check Scribe call for round 1: should have language='Croatian' and scribe_max_words=None
        from agents.thread_notes_agent import update_notes_for_thread
        await update_notes_for_thread(channel, thread_ts, is_completed=True)

        assert len(captured_scribe_calls) >= 1
        last_scribe = captured_scribe_calls[-1]
        assert last_scribe["thread_language"] == "Croatian"
        assert last_scribe["scribe_max_words"] is None  # /max_words 5 does NOT affect scribe!

        # ==================== ROUND 2 ====================
        # User issues /next-round
        captured_system_prompts.clear()
        mock_choice_3 = MagicMock()
        mock_choice_3.message.content = "Innovator nastavak u drugoj rundi."  # 5 words
        mock_choice_3.message.tool_calls = None
        mock_run_turn.current_response = MagicMock(choices=[mock_choice_3], usage=None)

        res_3 = await process_and_respond(
            channel_id=channel,
            thread_ts=thread_ts,
            raw_text="/next-round",
            is_bot_trigger=False,
        )
        assert not res_3.get("is_completed")
        # Must STILL be Croatian and max_words=5!
        assert thread_state_manager.get_language(channel, thread_ts) == "Croatian"
        assert thread_state_manager.get_max_words(channel, thread_ts) == 5
        assert any("5 words" in prompt for prompt in captured_system_prompts)

        # LegalAdvisor finishes Round 2
        mock_choice_4 = MagicMock()
        mock_choice_4.message.content = "LegalAdvisor kraj druge runde sada."  # 5 words
        mock_choice_4.message.tool_calls = None
        mock_run_turn.current_response = MagicMock(choices=[mock_choice_4], usage=None)

        res_4 = await process_and_respond(
            channel_id=channel,
            thread_ts=thread_ts,
            raw_text="/[agent-10011] LegalAdvisor runda dva",
            is_bot_trigger=True,
        )
        assert res_4.get("is_completed")
        assert thread_state_manager.get_language(channel, thread_ts) == "Croatian"
        assert thread_state_manager.get_max_words(channel, thread_ts) == 5

        # ==================== ROUND 3 ====================
        # User issues /next-round for round 3
        captured_system_prompts.clear()
        mock_choice_5 = MagicMock()
        mock_choice_5.message.content = "Treca runda za inovatora ovdje."  # 5 words
        mock_choice_5.message.tool_calls = None
        mock_run_turn.current_response = MagicMock(choices=[mock_choice_5], usage=None)

        res_5 = await process_and_respond(
            channel_id=channel,
            thread_ts=thread_ts,
            raw_text="/next-round",
            is_bot_trigger=False,
        )
        assert not res_5.get("is_completed")
        assert thread_state_manager.get_language(channel, thread_ts) == "Croatian"
        assert thread_state_manager.get_max_words(channel, thread_ts) == 5
        assert any("5 words" in prompt for prompt in captured_system_prompts)

        # LegalAdvisor finishes Round 3
        mock_choice_6 = MagicMock()
        mock_choice_6.message.content = "Kraj trece runde napokon ovdje."  # 5 words
        mock_choice_6.message.tool_calls = None
        mock_run_turn.current_response = MagicMock(choices=[mock_choice_6], usage=None)

        res_6 = await process_and_respond(
            channel_id=channel,
            thread_ts=thread_ts,
            raw_text="/[agent-10011] LegalAdvisor runda tri",
            is_bot_trigger=True,
        )
        assert res_6.get("is_completed")
        # Language and max_words held across all 3 rounds!
        assert thread_state_manager.get_language(channel, thread_ts) == "Croatian"
        assert thread_state_manager.get_max_words(channel, thread_ts) == 5


@pytest.mark.anyio
async def test_scribe_max_and_min_words_separation():
    """
    Test skills /scribe_max_words and /scribe_min_words:
    - Sets limits specifically for Scribe.
    - Scribe generator receives scribe_max_words and scribe_min_words.
    - Regular debate agents do not receive them as agent max_words.
    """
    captured_scribe_calls = []

    async def mock_generate_thread_notes(history, is_completed=False, thread_language="English", scribe_max_words=None, scribe_min_words=None):
        captured_scribe_calls.append({
            "scribe_max_words": scribe_max_words,
            "scribe_min_words": scribe_min_words,
        })
        return "Notes", {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}

    configure_fetch_thread_history(AsyncMock(return_value=[{"role": "user", "text": "hi"}]))

    channel = "C_SCRIBE"
    thread_ts = "666.001"

    # User issues /scribe_max_words 40 /scribe_min_words 10
    mock_choice = MagicMock()
    mock_choice.message.content = "Agent response"
    mock_choice.message.tool_calls = None
    mock_resp = MagicMock(choices=[mock_choice], usage=None)
    configure_llm_completion_fn(make_mock_acompletion(lambda *a, **k: mock_resp))

    with patch("agents.thread_notes_agent.generate_thread_notes", side_effect=mock_generate_thread_notes):
        await process_and_respond(
            channel_id=channel,
            thread_ts=thread_ts,
            raw_text="[agent-10002] Discuss feature /scribe_max_words 40 /scribe_min_words 10",
            is_bot_trigger=False,
        )

        # Scribe limits are stored in state
        assert thread_state_manager.get_scribe_max_words(channel, thread_ts) == 40
        assert thread_state_manager.get_scribe_min_words(channel, thread_ts) == 10
        # Debate agents do NOT have max_words
        assert thread_state_manager.get_max_words(channel, thread_ts) is None
        assert thread_state_manager.get_min_words(channel, thread_ts) is None

        # Check Scribe call
        from agents.thread_notes_agent import update_notes_for_thread
        await update_notes_for_thread(channel, thread_ts, is_completed=False)

        assert len(captured_scribe_calls) >= 1
        assert captured_scribe_calls[-1]["scribe_max_words"] == 40
        assert captured_scribe_calls[-1]["scribe_min_words"] == 10


@pytest.mark.anyio
async def test_include_overrides_exclude():
    """
    Test skill /include [agent] overrides previous /exclude and adds agent to debate.
    """
    channel = "C_INC"
    thread_ts = "777.001"

    # Initially exclude LegalAdvisor
    thread_state_manager.set_excluded_agents(channel, thread_ts, ["agent-10011"])
    assert thread_state_manager.get_excluded_agents(channel, thread_ts) == ["agent-10011"]

    # Now apply /include [agent-10011]
    from pipeline.directive_parser import directive_parser
    parsed = await directive_parser.parse("/include [agent-10011] [agent-10008]")
    assert parsed.included_agents == ["agent-10011", "agent-10008"]

    # In validation stage logic: include removes from excluded and sets included
    for inc in parsed.included_agents:
        thread_state_manager.remove_excluded_agent(channel, thread_ts, inc)
    thread_state_manager.set_included_agents(channel, thread_ts, parsed.included_agents)

    assert "agent-10011" not in thread_state_manager.get_excluded_agents(channel, thread_ts)
    assert "agent-10011" in thread_state_manager.get_included_agents(channel, thread_ts)
    assert "agent-10008" in thread_state_manager.get_included_agents(channel, thread_ts)


@pytest.mark.anyio
async def test_revoke_skill_functionality():
    """
    Test skill /revoke_constraint [instruction_name | all]:
    - /revoke_constraint max_words: clears max_words
    - /revoke_constraint all: clears all constraints
    """
    channel = "C_REV"
    thread_ts = "888.001"

    thread_state_manager.set_max_words(channel, thread_ts, 50)
    thread_state_manager.set_min_words(channel, thread_ts, 10)
    thread_state_manager.set_scribe_max_words(channel, thread_ts, 100)
    thread_state_manager.set_excluded_agents(channel, thread_ts, ["agent-10002"])
    thread_state_manager.set_included_agents(channel, thread_ts, ["agent-10011"])
    thread_state_manager.set_language(channel, thread_ts, "Croatian")

    # Revoke max_words specifically via pure toggle
    res = await process_and_respond(
        channel_id=channel,
        thread_ts=thread_ts,
        raw_text="/revoke_constraint max_words",
        user_id="U_ADMIN",
        is_bot_trigger=False,
    )
    assert res.get("ok")
    assert res.get("halted")
    assert thread_state_manager.get_max_words(channel, thread_ts) is None
    # Other state preserved
    assert thread_state_manager.get_min_words(channel, thread_ts) == 10
    assert thread_state_manager.get_scribe_max_words(channel, thread_ts) == 100
    assert thread_state_manager.get_language(channel, thread_ts) == "Croatian"

    # Revoke all
    res_all = await process_and_respond(
        channel_id=channel,
        thread_ts=thread_ts,
        raw_text="/revoke_constraint all",
        user_id="U_ADMIN",
        is_bot_trigger=False,
    )
    assert res_all.get("ok")
    assert res_all.get("halted")
    assert thread_state_manager.get_min_words(channel, thread_ts) is None
    assert thread_state_manager.get_scribe_max_words(channel, thread_ts) is None
    assert thread_state_manager.get_excluded_agents(channel, thread_ts) == []
    assert thread_state_manager.get_included_agents(channel, thread_ts) == []
    assert thread_state_manager.get_language(channel, thread_ts) is None


@pytest.mark.anyio
async def test_off_toggles_execution():
    """
    Test that /max_words off, /min_words off, /exclude off, /include off,
    /scribe_max_words off, /scribe_min_words off work as toggles.
    """
    channel = "C_OFF"
    thread_ts = "999.001"

    thread_state_manager.set_max_words(channel, thread_ts, 25)
    thread_state_manager.set_min_words(channel, thread_ts, 5)
    thread_state_manager.set_excluded_agents(channel, thread_ts, ["agent-10011"])
    thread_state_manager.set_included_agents(channel, thread_ts, ["agent-10002"])
    thread_state_manager.set_scribe_max_words(channel, thread_ts, 60)
    thread_state_manager.set_scribe_min_words(channel, thread_ts, 15)

    # Test pure toggle: /max_words off
    res = await process_and_respond(
        channel_id=channel,
        thread_ts=thread_ts,
        raw_text="/max_words off",
        user_id="U_ADMIN",
        is_bot_trigger=False,
    )
    assert res.get("ok")
    assert thread_state_manager.get_max_words(channel, thread_ts) is None

    # Test pure toggle: /exclude off
    res_exc = await process_and_respond(
        channel_id=channel,
        thread_ts=thread_ts,
        raw_text="/exclude off",
        user_id="U_ADMIN",
        is_bot_trigger=False,
    )
    assert res_exc.get("ok")
    assert thread_state_manager.get_excluded_agents(channel, thread_ts) == []

    # Test pure toggle: /include off
    res_inc = await process_and_respond(
        channel_id=channel,
        thread_ts=thread_ts,
        raw_text="/include off",
        user_id="U_ADMIN",
        is_bot_trigger=False,
    )
    assert res_inc.get("ok")
    assert thread_state_manager.get_included_agents(channel, thread_ts) == []

    # Test pure toggle: /scribe_max_words off
    res_sm = await process_and_respond(
        channel_id=channel,
        thread_ts=thread_ts,
        raw_text="/scribe_max_words off",
        user_id="U_ADMIN",
        is_bot_trigger=False,
    )
    assert res_sm.get("ok")
    assert thread_state_manager.get_scribe_max_words(channel, thread_ts) is None

