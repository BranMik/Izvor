import json
from unittest.mock import MagicMock, patch

import pytest

from nlp.intent_extractor import extract_user_intent, should_skip_intent_extractor


def test_should_skip_intent_extractor_rules():
    # Rule 1: Always skip on /stop
    assert should_skip_intent_extractor("/stop") is True
    assert should_skip_intent_extractor("<@U123> /stop please") is True

    # Rule 2: Never skip on /file alone
    assert (
        should_skip_intent_extractor(
            "/file: schema.sql",
            is_file_cmd=True,
            has_slash_max_words=False,
            has_slash_history=False,
            has_thread_files=True,
        )
        is False
    )

    # Rule 3: Do not skip if /history or /max_words is used with thread files present (without /file)
    assert (
        should_skip_intent_extractor(
            "/max_words 50 inspect this",
            is_file_cmd=False,
            has_slash_max_words=True,
            has_slash_history=False,
            has_thread_files=True,
        )
        is False
    )
    assert (
        should_skip_intent_extractor(
            "/history 3 inspect this",
            is_file_cmd=False,
            has_slash_max_words=False,
            has_slash_history=True,
            has_thread_files=True,
        )
        is False
    )

    # Rule 4: Skip if /file is called explicitly along with /max_words or /history
    assert (
        should_skip_intent_extractor(
            "/file: test.py /max_words 50",
            is_file_cmd=True,
            has_slash_max_words=True,
            has_slash_history=False,
            has_thread_files=True,
        )
        is True
    )
    assert (
        should_skip_intent_extractor(
            "/file: test.py /history 2",
            is_file_cmd=True,
            has_slash_max_words=False,
            has_slash_history=True,
            has_thread_files=True,
        )
        is True
    )
    assert (
        should_skip_intent_extractor(
            "/file: test.py /max_words 50 /history 2",
            is_file_cmd=True,
            has_slash_max_words=True,
            has_slash_history=True,
            has_thread_files=True,
        )
        is True
    )

    # Both max_words and history explicit and no thread files -> skip
    assert (
        should_skip_intent_extractor(
            "/max_words 50 /history 2",
            is_file_cmd=False,
            has_slash_max_words=True,
            has_slash_history=True,
            has_thread_files=False,
        )
        is True
    )

    # Standard natural language -> do not skip
    assert (
        should_skip_intent_extractor(
            "explain how this works in 50 words",
            is_file_cmd=False,
            has_slash_max_words=False,
            has_slash_history=False,
            has_thread_files=False,
        )
        is False
    )


    # Presence of /exclude does NOT affect should_skip rules
    assert (
        should_skip_intent_extractor(
            "/exclude [TechLead] debate this topic in 50 words",
            is_file_cmd=False,
            has_slash_max_words=False,
            has_slash_history=False,
            has_thread_files=False,
        )
        is False
    )
    assert (
        should_skip_intent_extractor(
            "/exclude [TechLead] /max_words 50 /history 2",
            is_file_cmd=False,
            has_slash_max_words=True,
            has_slash_history=True,
            has_thread_files=False,
        )
        is True
    )


@pytest.mark.anyio
async def test_extract_user_intent_success():
    mock_response = MagicMock()
    mock_response.choices = [
        MagicMock(
            message=MagicMock(
                content=json.dumps(
                    {
                        "max_words": 60,
                        "history": 4,
                        "file_use": True,
                        "target_file": "schema.sql",
                        "excluded_agents": ["TechLead", "Innovator"],
                    }
                )
            )
        )
    ]

    with patch("nlp.intent_extractor.litellm.completion", return_value=mock_response):
        intent = await extract_user_intent(
            "Please analyze schema.sql based on last 4 messages in under 60 words and exclude TechLead",
            available_files=[{"name": "schema.sql", "id": "F123"}],
        )

        assert intent["max_words"] == 60
        assert intent["history"] == 4
        assert intent["file_use"] is True
        assert intent["target_file"] == "schema.sql"
        assert intent["excluded_agents"] == ["TechLead", "Innovator"]


@pytest.mark.anyio
async def test_extract_user_intent_empty_text():
    intent = await extract_user_intent("")
    assert intent == {
        "max_words": None,
        "history": None,
        "file_use": False,
        "target_file": None,
        "excluded_agents": [],
    }


@pytest.mark.anyio
async def test_extract_user_intent_failure_fallback():
    with patch("nlp.intent_extractor.litellm.completion", side_effect=Exception("API Down")):
        intent = await extract_user_intent("some message")
        assert intent == {
            "max_words": None,
            "history": None,
            "file_use": False,
            "target_file": None,
            "excluded_agents": [],
        }
