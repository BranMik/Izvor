from unittest.mock import AsyncMock, MagicMock, patch

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
    mock_resp.usage = None
    return mock_resp


def test_detect_language_fallbacks():
    """Deterministic fallbacks for empty text or pure code without natural language."""
    assert detect_language("") == "English"
    assert detect_language("   ") == "English"
    assert detect_language("12345 67890") == "English"
    assert detect_language("SELECT * FROM users WHERE id = 1;") == "English"
    assert detect_language("```\ndef foo():\n    return 42\n```") == "English"


@pytest.mark.anyio
async def test_detect_language_llm_slack_tag_stripping():
    """Ensures Slack tags like <@U0BR4EH6XMY> are stripped before calling the LLM."""
    text = "<@U0BR4EH6XMY> kratki test, do 5 rijeci"
    with patch("nlp.language_detector.litellm.acompletion", AsyncMock(return_value=_make_mock_response("Croatian"))) as mock_comp:
        res = await detect_language_llm(text)
        assert res == "Croatian"
        # Check that the prompt sent to LLM had the Slack tag stripped
        call_kwargs = mock_comp.call_args[1]
        user_message = call_kwargs["messages"][1]["content"]
        assert "<@U0BR4EH6XMY>" not in user_message
        assert "kratki test, do 5 rijeci" in user_message


def test_detect_language_heuristic():
    """Verifies that synchronous detect_language correctly classifies via heuristic."""
    test_cases = [
        ("What are the architectural trade-offs?", "English"),
        ("Kako funkcionira ovaj sustav?", "Croatian"),
        ("kratki test, do 5 rijeci", "Croatian"),
        ("Wie funktioniert das System und die Architektur?", "German"),
        ("Comment ça marche pour les utilisateurs?", "French"),
        ("¿Cómo funciona esto y la base de datos?", "Spanish"),
    ]
    for prompt, expected in test_cases:
        assert detect_language(prompt) == expected


@pytest.mark.anyio
async def test_detect_language_llm_mocked_success():
    text = "analiza sustava"
    with patch("nlp.language_detector.litellm.acompletion", AsyncMock(return_value=_make_mock_response("Croatian"))):
        res = await detect_language_llm(text)
        assert res == "Croatian"


@pytest.mark.anyio
async def test_detect_language_llm_uses_configured_model():
    """Verifies that detect_language_llm uses the model configured in SSOT."""
    from core.config import get_orchestrator_fast_model

    expected_model = get_orchestrator_fast_model()
    text = "kratki test"
    with patch("nlp.language_detector.litellm.acompletion", AsyncMock(return_value=_make_mock_response("Croatian"))) as mock_comp:
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
    mock_resp.usage = None

    with patch("nlp.language_detector.litellm.acompletion", AsyncMock(return_value=mock_resp)):
        res = await detect_language_llm("Ajmo opet, isti upit.")
        assert res == "Croatian"


@pytest.mark.anyio
async def test_detect_language_llm_fallback_on_error():
    """When LLM call fails, falls back to the deterministic heuristic (then default) instead of blindly defaulting."""
    text = "wie geht es dir heute mein freund"
    with patch("nlp.language_detector.litellm.acompletion", side_effect=Exception("API connection timeout")):
        res = await detect_language_llm(text)
        assert res == "English"  # heuristic finds <2 German keywords here, so default applies


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


@pytest.mark.anyio
async def test_detect_language_llm_escalates_timeout_then_succeeds():
    """First classification attempt times out -> re-run once with escalated (x1.5) timeout -> succeeds."""
    import asyncio

    with patch(
        "core.llm_utils.execute_llm_with_retry",
        AsyncMock(side_effect=[asyncio.TimeoutError(), _make_mock_response("Croatian")]),
    ) as mock_exec:
        res = await detect_language_llm("Ovo je test na hrvatskom jeziku.", timeout_seconds=0.01)

    assert res == "Croatian"
    assert mock_exec.call_count == 2  # base attempt + one escalated attempt


@pytest.mark.anyio
async def test_detect_language_llm_timeout_falls_back_to_heuristic():
    """Both attempts time out -> deterministic heuristic (not a blind English default)."""
    import asyncio

    with patch(
        "core.llm_utils.execute_llm_with_retry",
        AsyncMock(side_effect=asyncio.TimeoutError()),
    ) as mock_exec:
        res = await detect_language_llm("Ovo je čisto hrvatski tekst, zar ne?", timeout_seconds=0.01)

    assert res == "Croatian"  # via _heuristic_detect (distinctive č), despite LLM being down
    assert mock_exec.call_count == 2


@pytest.mark.anyio
async def test_detect_language_llm_falls_back_when_key_missing():
    """When LanguageDetector's fast model key is missing, it falls back to default_reasoning_model / catalog."""
    from core.model_catalog import configure_env_provider

    configure_env_provider(lambda: {"OPENAI_API_KEY": "sk-valid-openai"})
    with patch(
        "nlp.language_detector.litellm.acompletion",
        AsyncMock(return_value=_make_mock_response("Croatian")),
    ) as mock_comp:
        res = await detect_language_llm("kratki test", model="gemini/gemini-4-hydrogen")

    assert res == "Croatian"
    assert mock_comp.call_args.kwargs["model"] == "openai/gpt-6.2-astra"

