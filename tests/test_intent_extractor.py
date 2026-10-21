import json
from unittest.mock import MagicMock, patch

import pytest

from nlp.intent_extractor import extract_user_intent, should_skip_intent_extractor


def test_should_skip_intent_extractor_rules():
    # Only /stop command triggers skip
    assert should_skip_intent_extractor("/stop") is True
    assert should_skip_intent_extractor("<@U123> /stop please") is True
    assert should_skip_intent_extractor("/STOP") is True

    # All other prompts and command combinations do NOT skip intent extraction
    assert should_skip_intent_extractor("/file: schema.sql") is False
    assert should_skip_intent_extractor("/max_words 50 inspect this") is False
    assert should_skip_intent_extractor("/history 3 inspect this") is False
    assert should_skip_intent_extractor("/file: test.py /max_words 50") is False
    assert should_skip_intent_extractor("/file: test.py /history 2") is False
    assert should_skip_intent_extractor("/file: test.py /max_words 50 /history 2") is False
    assert should_skip_intent_extractor("/max_words 50 /history 2") is False
    assert should_skip_intent_extractor("explain how this works in 50 words") is False
    assert should_skip_intent_extractor("/exclude [agent-10011] debate this topic in 50 words") is False
    assert should_skip_intent_extractor("/exclude [agent-10011] /max_words 50 /history 2") is False
    assert should_skip_intent_extractor("<@U0BR4EH6XMY> Test /max_words 5, exclude Visionary") is False
    assert should_skip_intent_extractor("<@U0BR4EH6XMY> Test /max_words 50 /history 2, exclude Visionary") is False
    assert should_skip_intent_extractor("Rasprava /max_words 10, bez Visionary") is False


@pytest.mark.anyio
async def test_stop_discussion_never_inferred_from_llm():
    """
    Verifies that even if an LLM returns action 'stop_discussion' for conversational phrases
    like 'cut scope' or 'please conclude', it is filtered out and never emitted.
    """
    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(
            message=MagicMock(
                content=json.dumps(
                    {
                        "actions": [{"name": "stop_discussion", "params": {}}],
                    }
                )
            )
        )
    ]

    from nlp.intent_extractor import intent_extractor

    with patch("nlp.intent_extractor.litellm.completion", return_value=mock_resp):
        intent = await intent_extractor.extract_intent("Cut scope ruthlessly")
        assert not intent.has_action("stop_discussion")
        assert "stop_discussion" not in [a.name for a in intent.actions]


@pytest.mark.anyio
async def test_extract_user_intent_success():
    mock_response = MagicMock()
    mock_response.choices = [
        MagicMock(
            message=MagicMock(
                content=json.dumps(
                    {
                        "modifiers": {
                            "max_words": 60,
                            "history": 4,
                        },
                        "actions": [
                            {"name": "document_reader", "params": {"target_file": "schema.sql"}},
                            {"name": "exclude_agents", "params": {"agents": ["agent-10011", "agent-10002"]}},
                        ],
                    }
                )
            )
        )
    ]

    with patch("nlp.intent_extractor.litellm.completion", return_value=mock_response):
        intent = await extract_user_intent(
            "Please analyze schema.sql based on last 4 messages in under 60 words and exclude LegalAdvisor",
            available_files=[{"name": "schema.sql", "id": "F123"}],
        )

        assert intent["max_words"] == 60
        assert intent["history"] == 4
        assert intent["file_use"] is True
        assert intent["target_file"] == "schema.sql"
        assert intent["excluded_agents"] == ["agent-10011", "agent-10002"]
        assert intent["modifiers"]["max_words"] == 60
        assert intent["modifiers"]["history"] == 4
        assert any(a["name"] == "document_reader" for a in intent["actions"])
        assert any(a["name"] == "exclude_agents" for a in intent["actions"])


@pytest.mark.anyio
async def test_extract_user_intent_empty_text():
    intent = await extract_user_intent("")
    assert intent["max_words"] is None
    assert intent["min_words"] is None
    assert intent["history"] is None
    assert intent["file_use"] is False
    assert intent["target_file"] is None
    assert intent["excluded_agents"] == []
    assert intent["actions"] == []
    assert intent["modifiers"] == {}


@pytest.mark.anyio
async def test_extract_user_intent_failure_fallback():
    with patch("nlp.intent_extractor.litellm.completion", side_effect=Exception("API Down")):
        intent = await extract_user_intent("some message")
        assert intent["max_words"] is None
        assert intent["min_words"] is None
        assert intent["history"] is None
        assert intent["file_use"] is False
        assert intent["target_file"] is None
        assert intent["excluded_agents"] == []
        assert intent["actions"] == []
        assert intent["modifiers"] == {}


@pytest.mark.anyio
async def test_directive_parser_uses_intent_extractor_for_max_words():
    """
    Verifies that when NL regexes are disabled, DirectiveParser relies
    on IntentExtractor (Micro-LLM) to extract max_words seamlessly.
    """
    from pipeline.directive_parser import directive_parser

    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(
            message=MagicMock(
                content=json.dumps(
                    {
                        "modifiers": {"max_words": 5},
                        "actions": [],
                    }
                )
            )
        )
    ]

    with patch("nlp.intent_extractor.litellm.completion", return_value=mock_resp):
        res = await directive_parser.parse("objasni baze podataka u do 5 riječi")
        assert res.max_words == 5
        assert "objasni baze podataka" in res.cleaned_prompt


@pytest.mark.anyio
async def test_directive_parser_uses_intent_extractor_for_all_directives():
    """
    Verifies that all natural language directives (min_words, history, file_use, excluded_agents)
    are extracted purely via IntentExtractor without relying on hardcoded NL regexes.
    """
    from pipeline.directive_parser import directive_parser

    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(
            message=MagicMock(
                content=json.dumps(
                    {
                        "modifiers": {
                            "max_words": 100,
                            "min_words": 30,
                            "history": 4,
                        },
                        "actions": [
                            {"name": "document_reader", "params": {"target_file": "schema.sql"}},
                            {"name": "exclude_agents", "params": {"agents": ["agent-10011"]}},
                        ],
                    }
                )
            )
        )
    ]

    with patch("nlp.intent_extractor.litellm.completion", return_value=mock_resp):
        res = await directive_parser.parse(
            "pogledaj priloženi schema.sql, uzmi zadnje 4 poruke, bez LegalAdvisora, između 30 i 100 riječi",
            available_files=[{"name": "schema.sql", "id": "F123"}],
        )
        assert res.max_words == 100
        assert res.min_words == 30
        assert res.history_limit == 4
        assert res.is_file_req is True
        assert res.target_filename == "schema.sql"
        assert res.excluded_agents == ["agent-10011"]


@pytest.mark.anyio
async def test_sandwich_exclusion_and_bot_turn_not_stopped(monkeypatch):
    """
    Verifies that when an agent in Sandwich start_chain (e.g. Visionary) is excluded via
    natural language alongside /max_words:
    1. IntentExtractor extracts exclude_agents without being skipped.
    2. Visionary is removed from remaining agents.
    3. ScopeOptimizer speaks first; if it tags [agent-10001], it is corrected.
    4. Turn 2 with is_bot_trigger=True does NOT parse user directives or stop discussion.
    """
    from unittest.mock import AsyncMock

    from core.config import AGENTS_CONFIG
    from orchestration.agent_router import process_and_respond
    from orchestration.agent_settings import (
        set_agent_enabled,
        set_orchestration_mode,
        set_sandwich_config,
    )
    from orchestration.thread_state import thread_state_manager

    set_orchestration_mode("sandwich")
    set_sandwich_config(
        start_chain=["agent-10005", "agent-10001"],
        end_chain=["agent-10008"],
    )
    for a in AGENTS_CONFIG:
        set_agent_enabled(a, a in ("agent-10005", "agent-10001", "agent-10008", "agent-10011"))

    posted_messages = []

    async def mock_post(channel_id, thread_ts, text, username, icon_url=None):
        posted_messages.append({"username": username, "text": text})
        return {"ok": True, "ts": f"77777.{len(posted_messages)}"}

    monkeypatch.setattr("orchestration.agent_router.post_agent_response", mock_post)
    monkeypatch.setattr("orchestration.agent_router.fetch_thread_history", AsyncMock(return_value=[]))
    monkeypatch.setattr("orchestration.agent_router.record_thread_tokens", MagicMock())

    mock_intent_resp = MagicMock()
    mock_intent_resp.choices = [
        MagicMock(
            message=MagicMock(
                content=json.dumps(
                    {
                        "modifiers": {"max_words": 5},
                        "actions": [{"name": "exclude_agents", "params": {"agents": ["agent-10001"]}}],
                    }
                )
            )
        )
    ]

    mock_choice_1 = MagicMock()
    mock_choice_1.message.content = "Cut scope ruthlessly. /[agent-10001]"
    mock_choice_1.message.tool_calls = None
    mock_llm_1 = MagicMock(choices=[mock_choice_1], usage=None)

    async def mock_run(func, *args, **kwargs):
        # If litellm called for intent extractor:
        msgs = kwargs.get("messages") or (args[2] if len(args) > 2 else [])
        if any("intent" in str(m.get("content", "")).lower() for m in msgs if isinstance(m, dict)):
            return mock_intent_resp
        return mock_llm_1

    import litellm
    monkeypatch.setattr("orchestration.agent_router.run_in_threadpool", mock_run)
    monkeypatch.setattr(litellm, "completion", lambda **kwargs: mock_intent_resp)

    thread_key = ("C_TEST", "77777.0001")
    thread_state_manager.clear_state(thread_key[0], thread_key[1])

    # Turn 1: User prompt
    res_1 = await process_and_respond(
        channel_id="C_TEST",
        thread_ts="77777.0001",
        raw_text="<@U0BR4EH6XMY> Test /max_words 5, exclude Visionary",
        is_bot_trigger=False,
    )

    assert res_1.get("ok") is True
    assert len(posted_messages) == 1
    assert posted_messages[0]["username"] == "ScopeOptimizer"
    # Visionary was excluded so remaining sequence corrected nomination to next eligible (LegalAdvisor / SecurityArchitect)
    assert "/[agent-10001]" not in posted_messages[0]["text"]
    remaining = thread_state_manager.get_remaining_agents(thread_key[0], thread_key[1])
    assert "agent-10001" not in remaining

    # Turn 2: Next agent triggered by bot response
    mock_choice_2 = MagicMock()
    mock_choice_2.message.content = "Next agent response."
    mock_choice_2.message.tool_calls = None
    mock_llm_2 = MagicMock(choices=[mock_choice_2], usage=None)

    async def mock_run_2(func, *args, **kwargs):
        return mock_llm_2

    monkeypatch.setattr("orchestration.agent_router.run_in_threadpool", mock_run_2)

    res_2 = await process_and_respond(
        channel_id="C_TEST",
        thread_ts="77777.0001",
        raw_text=posted_messages[0]["text"],
        is_bot_trigger=True,
    )

    assert res_2.get("ok") is True
    assert res_2.get("halted") is not True
    assert len(posted_messages) == 2


@pytest.mark.anyio
async def test_validation_stage_locks_language_from_unified_intent_extractor(monkeypatch):
    """Verifies TKT-821: Stage 1 locks language directly from unified intent extraction without extra LLM call."""
    from unittest.mock import AsyncMock
    from orchestration.stages.validation import ValidationStage
    from orchestration.thread_state import thread_state_manager
    from orchestration.turn_pipeline import TurnContext, TurnPipeline
    from pipeline.directive_parser.parser import ParsedDirectives

    channel_id = "C_LANG_TEST"
    thread_ts = "12345.6789"
    thread_state_manager.clear_state(channel_id, thread_ts)

    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="Molim vas objasnite detalje projekta na hrvatskom jeziku.",
        is_bot_trigger=False,
    )

    detect_llm_called = False

    async def mock_detect_llm(text):
        nonlocal detect_llm_called
        detect_llm_called = True
        return "hr"

    monkeypatch.setattr("orchestration.stages.validation.detect_language_llm", mock_detect_llm)
    monkeypatch.setattr(
        "orchestration.stages.validation.directive_parser.parse",
        AsyncMock(return_value=ParsedDirectives(cleaned_prompt=ctx.raw_text, detected_language="hr")),
    )

    pipeline = TurnPipeline()
    validation_stage = ValidationStage(pipeline)

    res = await validation_stage.stage_validate_and_parse(ctx)
    assert res is None  # no halt
    assert ctx.thread_language == "Croatian"
    assert thread_state_manager.get_language(channel_id, thread_ts) == "Croatian"
    assert not detect_llm_called, "detect_language_llm should NOT be called when intent extractor detected language"

