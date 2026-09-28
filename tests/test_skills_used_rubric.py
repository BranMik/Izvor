import pytest
from unittest.mock import MagicMock

from orchestration.turn_pipeline import TurnContext, TurnPipeline
from skills.loader import skill_registry


def test_find_skill_for_tool():
    """Verify that skill_registry maps tool names to their declaring skill."""
    assert skill_registry.find_skill_for_tool("search_knowledge_rag") == "knowledge_base_rag"
    assert skill_registry.find_skill_for_tool("web_search") == "web_research"
    assert skill_registry.find_skill_for_tool("read_file") == "codebase_inspection"
    assert skill_registry.find_skill_for_tool("list_directory") == "codebase_inspection"
    assert skill_registry.find_skill_for_tool("nonexistent_tool_xyz") is None


@pytest.mark.anyio
async def test_post_record_with_single_skill_used():
    """Verify Skills used rubric is placed between nomination tag and token footer."""
    pipeline = TurnPipeline()
    ctx = TurnContext(
        channel_id="C_SKILL_TEST",
        thread_ts="1234.5678",
        raw_text="Hello",
    )
    ctx.thread_key = ("C_SKILL_TEST", "1234.5678")
    ctx.agent_key = "Architect"
    ctx.agent_cfg = {"username": "Architect", "model": "openai/gpt-4o"}
    ctx.eligible_remaining_agents = ["TechLead"]
    ctx.remaining = ["TechLead"]
    ctx.final_text = "I reviewed the architecture.\n\n/[TechLead]"
    ctx.skills_used = ["knowledge_base_rag"]
    ctx.total_prompt_tokens = 500
    ctx.total_completion_tokens = 100

    posted_payload = {}

    async def mock_post(channel_id, thread_ts, text, username, icon_url):
        posted_payload["text"] = text
        return {"ok": True, "ts": "1234.9999"}

    pipeline._hooks["post_agent_response"] = mock_post
    pipeline._hooks["DEBATE_TURN_PACING_SECONDS"] = 0
    pipeline._hooks["SLACK_POST_PAUSE_SECONDS"] = 0

    await pipeline.stage_post_and_record(ctx)

    text = posted_payload["text"]
    assert "Skills used: [knowledge_base_rag]" in text
    # Verify exact ordering: /[TechLead] then Skills used then {model : p/c}
    idx_nom = text.index("/[TechLead]")
    idx_skill = text.index("Skills used: [knowledge_base_rag]")
    idx_footer = text.index("{openai/gpt-4o : 500/100}")

    assert idx_nom < idx_skill < idx_footer


@pytest.mark.anyio
async def test_post_record_with_multiple_skills_used():
    """Verify multiple skills used are formatted as comma-separated bracketed list."""
    pipeline = TurnPipeline()
    ctx = TurnContext(
        channel_id="C_SKILL_TEST",
        thread_ts="1234.5678",
        raw_text="Hello",
    )
    ctx.thread_key = ("C_SKILL_TEST", "1234.5678")
    ctx.agent_key = "TechLead"
    ctx.agent_cfg = {"username": "TechLead", "model": "mistral/mistral-large"}
    ctx.eligible_remaining_agents = ["Visionary"]
    ctx.remaining = ["Visionary"]
    ctx.final_text = "Analysis complete.\n\n/[Visionary]"
    ctx.skills_used = ["codebase_inspection", "web_research"]
    ctx.total_prompt_tokens = 600
    ctx.total_completion_tokens = 150

    posted_payload = {}

    async def mock_post(channel_id, thread_ts, text, username, icon_url):
        posted_payload["text"] = text
        return {"ok": True, "ts": "1234.9999"}

    pipeline._hooks["post_agent_response"] = mock_post
    pipeline._hooks["DEBATE_TURN_PACING_SECONDS"] = 0
    pipeline._hooks["SLACK_POST_PAUSE_SECONDS"] = 0

    await pipeline.stage_post_and_record(ctx)

    text = posted_payload["text"]
    assert "Skills used: [codebase_inspection], [web_research]" in text
    idx_nom = text.index("/[Visionary]")
    idx_skill = text.index("Skills used: [codebase_inspection], [web_research]")
    idx_footer = text.index("{mistral/mistral-large : 600/150}")

    assert idx_nom < idx_skill < idx_footer


@pytest.mark.anyio
async def test_post_record_omits_skills_used_when_none():
    """Verify Skills used rubric is completely omitted when no skills were used."""
    pipeline = TurnPipeline()
    ctx = TurnContext(
        channel_id="C_SKILL_TEST",
        thread_ts="1234.5678",
        raw_text="Hello",
    )
    ctx.thread_key = ("C_SKILL_TEST", "1234.5678")
    ctx.agent_key = "Visionary"
    ctx.agent_cfg = {"username": "Visionary", "model": "gemini/gemini-3.8-flash"}
    ctx.eligible_remaining_agents = ["Sceptic"]
    ctx.remaining = ["Sceptic"]
    ctx.final_text = "Creative thought.\n\n/[Sceptic]"
    ctx.skills_used = []
    ctx.total_prompt_tokens = 300
    ctx.total_completion_tokens = 50

    posted_payload = {}

    async def mock_post(channel_id, thread_ts, text, username, icon_url):
        posted_payload["text"] = text
        return {"ok": True, "ts": "1234.9999"}

    pipeline._hooks["post_agent_response"] = mock_post
    pipeline._hooks["DEBATE_TURN_PACING_SECONDS"] = 0
    pipeline._hooks["SLACK_POST_PAUSE_SECONDS"] = 0

    await pipeline.stage_post_and_record(ctx)

    text = posted_payload["text"]
    assert "Skills used" not in text
    assert text.endswith("\n\n{gemini/gemini-3.8-flash : 300/50}")
