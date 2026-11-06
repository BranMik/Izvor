from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
import pytest

from core.domain import Turn
from pipeline.discussion_scribe.scribe import (
    DiscussionScribe,
    _clean_turn_content_for_scribe,
)


def test_clean_turn_content_for_scribe():
    # Strips trailing nomination tags
    text1 = "Here is my core recommendation. /[agent-10003]"
    assert _clean_turn_content_for_scribe(text1) == "Here is my core recommendation."

    # Strips trailing nomination with whitespace
    text2 = "Here is another point.\n\n/ [Sceptic]"
    assert _clean_turn_content_for_scribe(text2) == "Here is another point."

    # Strips trailing token footer
    text3 = "Here is my analysis. {gemini-3.8-flash : 320}"
    assert _clean_turn_content_for_scribe(text3) == "Here is my analysis."

    # Strips both nomination tag and token footer
    text4 = "Analysis complete. /[RebuttalAdvocate] {gemini-3.8-flash : 150}"
    assert _clean_turn_content_for_scribe(text4) == "Analysis complete."

    # Empty / None handling
    assert _clean_turn_content_for_scribe("") == ""
    assert _clean_turn_content_for_scribe(None) == ""


@pytest.mark.anyio
async def test_scribe_brief_strips_nominations_and_injects_speaker_roster():
    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(message=MagicMock(content="*Core Objective & Problem Statement*\nSynthesized brief."))
    ]
    mock_resp.usage = MagicMock(prompt_tokens=100, completion_tokens=50)
    mock_completion_fn = AsyncMock(return_value=mock_resp)

    scribe = DiscussionScribe(completion_fn=mock_completion_fn)

    history = [
        {"role": "user", "username": "Alice", "content": "Let us discuss the architecture."},
        {"role": "assistant", "username": "Innovator", "content": "I propose microservices. /[Sceptic]"},
        {"role": "assistant", "username": "Challenger", "content": "Monolith is much simpler. /[RebuttalAdvocate]"},
    ]

    brief, tokens = await scribe.generate_brief(history)

    assert mock_completion_fn.called
    kwargs = mock_completion_fn.call_args.kwargs
    messages = kwargs["messages"]
    user_msg = messages[1]["content"]

    # Verify roster of actual speakers is injected
    assert "PARTICIPATING AGENTS WHO ACTUALLY SPOKE IN THIS TRANSCRIPT: [Innovator, Challenger]" in user_msg
    assert "STRICT ATTRIBUTION CONSTRAINT:" in user_msg

    # Verify trailing nomination tags were stripped from transcript text
    assert "/[Sceptic]" not in user_msg
    assert "/[RebuttalAdvocate]" not in user_msg
    assert "[Innovator]: I propose microservices." in user_msg
    assert "[Challenger]: Monolith is much simpler." in user_msg


@pytest.mark.anyio
async def test_scribe_conclusion_strips_nominations_and_injects_speaker_roster():
    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(message=MagicMock(content="Part 1: *Problem Statement*\nProblem.\nPart 2: *Conclusion and Argumentation*\nConsensus."))
    ]
    mock_resp.usage = MagicMock(prompt_tokens=200, completion_tokens=100)
    mock_completion_fn = AsyncMock(return_value=mock_resp)

    scribe = DiscussionScribe(completion_fn=mock_completion_fn)

    history = [
        Turn(role="user", speaker="Bob", content="Should we deploy containers?"),
        Turn(role="assistant", speaker="Visionary", content="Yes, container deployment offers fast scale. /[SecurityArchitect]"),
        Turn(role="assistant", speaker="SecurityArchitect", content="We must verify permissions first. /[agent-10006]"),
    ]

    conclusion, tokens = await scribe.generate_conclusion(history)

    assert mock_completion_fn.called
    kwargs = mock_completion_fn.call_args.kwargs
    messages = kwargs["messages"]
    user_msg = messages[1]["content"]

    # Verify participating speakers roster
    assert "PARTICIPATING AGENTS WHO ACTUALLY SPOKE IN THIS DELIBERATION: [Visionary, SecurityArchitect]" in user_msg
    assert "STRICT ATTRIBUTION CONSTRAINT:" in user_msg

    # Verify trailing nomination tags were stripped
    assert "/[SecurityArchitect]" not in user_msg
    assert "/[agent-10006]" not in user_msg
    assert "[Visionary]: Yes, container deployment offers fast scale." in user_msg
    assert "[SecurityArchitect]: We must verify permissions first." in user_msg
