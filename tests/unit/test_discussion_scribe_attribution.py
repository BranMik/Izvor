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
    text4 = "Analysis complete. /[Arbitrator] {gemini-3.8-flash : 150}"
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
        {"role": "assistant", "username": "Challenger", "content": "Monolith is much simpler. /[Arbitrator]"},
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
    assert "/[Arbitrator]" not in user_msg
    assert "[Innovator]: I propose microservices." in user_msg
    assert "[Challenger]: Monolith is much simpler." in user_msg


@pytest.mark.anyio
async def test_scribe_conclusion_strips_nominations_and_injects_speaker_roster():
    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(message=MagicMock(content="*Problem Statement*\nProblem.\n*Consensus Decisions*\nConsensus."))
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


@pytest.mark.anyio
async def test_scribe_filters_orchestrator_status_noise_from_transcript():
    """Verify Orchestrator banners and system status lines are excluded from transcript and attribution."""
    mock_resp = MagicMock()
    mock_resp.choices = [
        MagicMock(message=MagicMock(content="*Core Objective*\nAssess rollout."))
    ]
    mock_resp.usage = MagicMock(prompt_tokens=100, completion_tokens=40)
    mock_completion_fn = AsyncMock(return_value=mock_resp)

    scribe = DiscussionScribe(completion_fn=mock_completion_fn)

    history = [
        {"role": "user", "username": "Alice", "content": "Should we roll out v2?"},
        {"role": "assistant", "username": "Orchestrator", "content": "Thread language locked to English."},
        {"role": "assistant", "username": "user", "content": "[Orchestrator]: Round 1 started with 2 agents."},
        Turn(role="assistant", speaker="Orchestrator", content="Deliberation concluded."),
        Turn(role="assistant", speaker="Visionary", content="Proceed with canary deployment."),
        {"role": "assistant", "username": "SecurityArchitect", "content": "Require strict RBAC audit first."},
    ]

    await scribe.generate_brief(history, is_completed=True)

    assert mock_completion_fn.called
    user_msg = mock_completion_fn.call_args.kwargs["messages"][1]["content"]
    assert "PARTICIPATING AGENTS WHO ACTUALLY SPOKE IN THIS TRANSCRIPT: [Visionary, SecurityArchitect]" in user_msg
    assert "Orchestrator" not in user_msg
    assert "Thread language locked" not in user_msg
    assert "Round 1 started" not in user_msg
    assert "Deliberation concluded" not in user_msg


@pytest.mark.anyio
async def test_scribe_strips_multiline_footers_and_extracts_bracketed_persona():
    """TKT-1215: parses [Persona]: prefixes without falling back to [assistant] and strips Skills/token/fallback footers + nominations."""
    from pipeline.discussion_scribe.scribe import _format_transcript

    history = [
        {"role": "user", "content": "⚠️ Why did our server alert fire?"},
        {
            "role": "assistant",
            "username": "assistant",
            "content": (
                "[The Pragmatist]: Disk IOPS saturated during backup. /[The Critic]\n\n"
                "Skills used: [web_search], [db_inspect]\n\n"
                "⚠️ _Fallback model used: `openai/gpt-5.6-luna` (missing API key for `anthropic/claude-opus-5`)_\n\n"
                "{openai/gpt-5.6-luna : 412/98}"
            ),
        },
        {
            "role": "assistant",
            "username": "The Visionary",
            "content": "[The Visionary]: Consider async replication next. /[The Critic]",
        },
    ]

    transcript, speakers_str = _format_transcript(history)

    assert "assistant" not in speakers_str.lower()
    assert "The Critic" not in speakers_str
    assert "The Pragmatist" in speakers_str
    assert "The Visionary" in speakers_str
    assert "[The Pragmatist]: Disk IOPS saturated during backup." in transcript
    assert "[The Pragmatist]: [The Pragmatist]:" not in transcript
    assert "[The Visionary]: [The Visionary]:" not in transcript
    assert "Skills used:" not in transcript
    assert "{openai/gpt-5.6-luna : 412/98}" not in transcript
    assert "Fallback model used" not in transcript
    assert "/[The Critic]" not in transcript


@pytest.mark.anyio
async def test_scribe_handles_empty_choices_gracefully():
    """TKT-1217: _generate_text_with_fallback handles None/empty .choices without IndexError or AttributeError."""
    empty_resp = MagicMock()
    empty_resp.choices = []
    empty_resp.usage = None

    retry_resp = MagicMock()
    retry_resp.choices = None
    retry_resp.usage = None

    mock_completion_fn = AsyncMock(side_effect=[empty_resp, retry_resp])
    scribe = DiscussionScribe(completion_fn=mock_completion_fn)

    text, usage, ok = await scribe._generate_text_with_fallback(
        messages=[{"role": "user", "content": "hello"}],
        label="brief",
        unavailable_noun="Brief",
    )
    assert ok is False
    assert "Brief unavailable" in text

