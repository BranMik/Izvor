import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.models import ExtractedIntent
from orchestration.orchestrator_agent import (
    OrchestratorAgent,
    OrchestratorTask,
    orchestrator_agent,
)


def test_orchestrator_dual_model_resolution():
    agent = OrchestratorAgent(
        fast_model="gemini/gemini-3.8-flash",
        reasoning_model="mistral/mistral-large-latest",
    )
    # fast_model tasks
    assert agent.get_model_for_task(OrchestratorTask.INTENT_EXTRACTION) == "gemini/gemini-3.8-flash"
    assert agent.get_model_for_task(OrchestratorTask.LANGUAGE_DETECTION) == "gemini/gemini-3.8-flash"
    assert agent.get_model_for_task(OrchestratorTask.SYSTEM_MODERATOR) == "gemini/gemini-3.8-flash"
    assert agent.get_model_for_task(OrchestratorTask.PRICING_RESEARCH) == "gemini/gemini-3.8-flash"

    # reasoning_model task
    assert agent.get_model_for_task(OrchestratorTask.THREAD_NOTES) == "mistral/mistral-large-latest"


def test_orchestrator_dynamic_prompt_injection():
    agent = OrchestratorAgent()

    # Intake / Intent prompt
    intent_prompt = agent.build_prompt_for_task(OrchestratorTask.INTENT_EXTRACTION)
    assert "intent" in intent_prompt.lower() or "directive" in intent_prompt.lower()

    # Language classification prompt
    lang_prompt = agent.build_prompt_for_task(OrchestratorTask.LANGUAGE_DETECTION)
    assert "language classifier" in lang_prompt.lower()

    # System moderator prompt
    mod_prompt = agent.build_prompt_for_task(OrchestratorTask.SYSTEM_MODERATOR)
    assert "system moderator" in mod_prompt.lower()

    # Thread notes synthesis prompt
    notes_prompt = agent.build_prompt_for_task(OrchestratorTask.THREAD_NOTES)
    assert "scribe" in notes_prompt.lower() or "synthesis" in notes_prompt.lower()


def test_orchestrator_dynamic_tool_injection():
    agent = OrchestratorAgent()

    # Pre-turn latency critical tasks must have 0 tools (no schema bloat)
    assert agent.get_tools_for_task(OrchestratorTask.INTENT_EXTRACTION) == []
    assert agent.get_tools_for_task(OrchestratorTask.LANGUAGE_DETECTION) == []
    assert agent.get_tools_for_task(OrchestratorTask.SYSTEM_MODERATOR) == []


@pytest.mark.anyio
async def test_orchestrator_extract_intent_mock():
    mock_choice = MagicMock()
    mock_choice.message.content = '{"modifiers": {"max_words": 150}, "actions": []}'
    mock_resp = MagicMock(choices=[mock_choice])

    with patch("orchestration.orchestrator_agent.litellm.completion", return_value=mock_resp):
        intent = await orchestrator_agent.extract_intent("Limit debate to 150 words")
        assert isinstance(intent, ExtractedIntent)
        assert intent.modifiers.get("max_words") == 150


@pytest.mark.anyio
async def test_orchestrator_detect_language_mock():
    mock_choice = MagicMock()
    mock_choice.message.content = "Croatian"
    mock_resp = MagicMock(choices=[mock_choice])

    with patch("orchestration.orchestrator_agent.litellm.completion", return_value=mock_resp) as mock_comp:
        lang = await orchestrator_agent.detect_language("Bok, kako si danas?")
        assert lang == "Croatian"
        call_kwargs = mock_comp.call_args[1]
        assert "temperature" not in call_kwargs
        assert "max_tokens" not in call_kwargs
