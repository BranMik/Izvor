from unittest.mock import MagicMock, patch

import pytest

from nlp.language_detector import (
    detect_language,
    detect_language_llm,
    normalize_language_name,
)
from nlp.message_builder import build_llm_messages
from orchestration.thread_state import thread_state_manager


def _make_mock_response(content: str) -> MagicMock:
    mock_resp = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = content
    mock_resp.choices = [mock_choice]
    return mock_resp


def test_detect_language_fallbacks():
    """Deterministic fallbacks for empty text or pure code without natural language."""
    assert detect_language("") == "English"
    assert detect_language("   ") == "English"
    assert detect_language("12345 67890") == "English"
    assert detect_language("SELECT * FROM users WHERE id = 1;") == "English"
    assert detect_language("```\ndef foo():\n    return 42\n```") == "English"


def test_detect_language_llm_slack_tag_stripping():
    """Ensures Slack tags like <@U0BR4EH6XMY> are stripped before calling the LLM."""
    text = "<@U0BR4EH6XMY> kratki test, do 5 rijeci"
    with patch("litellm.completion", return_value=_make_mock_response("Croatian")) as mock_comp:
        res = detect_language(text)
        assert res == "Croatian"
        # Check that the prompt sent to LLM had the Slack tag stripped
        call_kwargs = mock_comp.call_args[1]
        user_message = call_kwargs["messages"][1]["content"]
        assert "<@U0BR4EH6XMY>" not in user_message
        assert "kratki test, do 5 rijeci" in user_message


def test_detect_language_mocked():
    """Verifies that detect_language correctly invokes LLM and normalizes response."""
    test_cases = [
        ("What are the architectural trade-offs?", "English", "English"),
        ("Kako funkcionira ovaj sustav?", "Croatian", "Croatian"),
        ("kratki test, do 5 rijeci", "hrvatski", "Croatian"),
        ("Wie funktioniert das System?", "German", "German"),
        ("Comment ça marche?", "French", "French"),
        ("¿Cómo funciona esto?", "Spanish", "Spanish"),
    ]
    for prompt, llm_output, expected in test_cases:
        with patch("litellm.completion", return_value=_make_mock_response(llm_output)):
            assert detect_language(prompt) == expected


@pytest.mark.anyio
async def test_detect_language_llm_mocked_success():
    text = "analiza sustava"
    with patch("litellm.completion", return_value=_make_mock_response("Croatian")):
        res = await detect_language_llm(text)
        assert res == "Croatian"


@pytest.mark.anyio
async def test_detect_language_llm_uses_configured_model():
    """Verifies that detect_language_llm uses the model configured in SSOT."""
    from core.config import get_language_detector_model

    expected_model = get_language_detector_model()
    text = "kratki test"
    with patch("litellm.completion", return_value=_make_mock_response("Croatian")) as mock_comp:
        res = await detect_language_llm(text)
        assert res == "Croatian"
        call_kwargs = mock_comp.call_args[1]
        assert call_kwargs["model"] == expected_model
        assert "temperature" not in call_kwargs
        assert "max_tokens" not in call_kwargs


@pytest.mark.anyio
async def test_detect_language_reasoning_content_fallback():
    """If model outputs empty content but provides reasoning_content, extracts language cleanly."""
    mock_resp = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = ""
    mock_choice.message.reasoning_content = "Thinking Process: The text is clearly Croatian. Answer: Croatian"
    mock_resp.choices = [mock_choice]

    with patch("litellm.completion", return_value=mock_resp):
        res = await detect_language_llm("Ajmo opet, isti upit.")
        assert res == "Croatian"


@pytest.mark.anyio
async def test_detect_language_llm_fallback_on_error():
    """When LLM call fails or times out, cleanly falls back to default."""
    text = "wie geht es dir heute mein freund"
    with patch("litellm.completion", side_effect=Exception("API connection timeout")):
        res = await detect_language_llm(text)
        assert res == "English"


def test_thread_state_language_management():
    channel_id = "C_LANG_TEST"
    thread_ts = "1700000000.123456"

    # Initially None
    assert thread_state_manager.get_language(channel_id, thread_ts) is None

    # Set and lock language
    thread_state_manager.set_language(channel_id, thread_ts, "Croatian")
    assert thread_state_manager.get_language(channel_id, thread_ts) == "Croatian"

    # Clear language
    thread_state_manager.clear_language(channel_id, thread_ts)
    assert thread_state_manager.get_language(channel_id, thread_ts) is None

    # Test cleanup_thread
    thread_state_manager.set_language(channel_id, thread_ts, "English")
    thread_state_manager.cleanup_thread(channel_id, thread_ts)
    assert thread_state_manager.get_language(channel_id, thread_ts) is None


def test_build_llm_messages_with_thread_language():
    system_prompt = "You are an assistant."
    prompt = "Analyze system architecture."
    agent_cfg = {"username": "agent-10011"}

    # 1. Bot trigger with English
    msgs_eng = build_llm_messages(
        system_prompt=system_prompt,
        effective_history=[{"role": "user", "content": "Hello"}],
        prompt=prompt,
        is_bot_trigger=True,
        agent_key="agent-10011",
        agent_cfg=agent_cfg,
        thread_language="English",
    )
    user_msg_eng = msgs_eng[-1]["content"]
    assert "strictly in English" in user_msg_eng
    assert "German" not in user_msg_eng
    assert "Croatian" not in user_msg_eng

    # 2. Bot trigger with Croatian
    msgs_cro = build_llm_messages(
        system_prompt=system_prompt,
        effective_history=[{"role": "user", "content": "Bok"}],
        prompt="",
        is_bot_trigger=True,
        agent_key="agent-10011",
        agent_cfg=agent_cfg,
        thread_language="Croatian",
    )
    user_msg_cro = msgs_cro[-1]["content"]
    assert "strictly in Croatian" in user_msg_cro
    assert "German" not in user_msg_cro


def test_normalize_language_name():
    assert normalize_language_name("croatian") == "Croatian"
    assert normalize_language_name("hrvatski") == "Croatian"
    assert normalize_language_name("bosnian") == "Bosnian"
    assert normalize_language_name("bosanski") == "Bosnian"
    assert normalize_language_name("serbian") == "Serbian"
    assert normalize_language_name("srpski") == "Serbian"
    assert normalize_language_name("ENGLISH") == "English"
    assert normalize_language_name("deutsch") == "German"
    assert normalize_language_name("español") == "Spanish"
    assert normalize_language_name("francais") == "French"
    assert normalize_language_name("`German`.") == "German"
    assert normalize_language_name("japanese") == "Japanese"
