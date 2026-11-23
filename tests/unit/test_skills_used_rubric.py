import pytest

from orchestration.turn_pipeline import TurnContext, TurnPipeline
from skills.registry import skill_registry


def test_find_skill_for_tool():
    """Verify that skill_registry maps tool names to their declaring skill."""
    assert skill_registry.find_skill_for_tool("search_knowledge_rag") == "knowledge_base_rag"
    assert skill_registry.find_skill_for_tool("web_search") == "web_research"
    assert skill_registry.find_skill_for_tool("read_document") == "document_reader"
    assert skill_registry.find_skill_for_tool("list_documents") == "document_reader"
    assert skill_registry.find_skill_for_tool("nonexistent_tool_xyz") is None


@pytest.mark.anyio
async def test_post_record_with_single_skill_used():
    """Verify Skills used rubric is placed between nomination tag and token footer."""
    posted_payload = {}

    async def mock_post(channel_id, thread_ts, text, username, icon_url):
        posted_payload["text"] = text
        return {"ok": True, "ts": "1234.9999"}

    pipeline = TurnPipeline(
        slack_poster=mock_post,
        debate_turn_pacing_seconds=0,
        slack_post_pause_seconds=0,
    )
    ctx = TurnContext(
        channel_id="C_SKILL_TEST",
        thread_ts="1234.5678",
        raw_text="Hello",
    )
    ctx.thread_key = ("C_SKILL_TEST", "1234.5678")
    ctx.agent_key = "agent-10008"
    ctx.agent_cfg = {"username": "agent-10008", "model": "openai/gpt-4o"}
    ctx.eligible_remaining_agents = ["agent-10011"]
    ctx.remaining = ["agent-10011"]
    ctx.output.final_text = "I reviewed the architecture.\n\n/[agent-10011]"
    ctx.output.skills_used = ["knowledge_base_rag"]
    ctx.output.total_prompt_tokens = 500
    ctx.output.total_completion_tokens = 100

    await pipeline.post_record_stage.stage_post_and_record(ctx)

    text = posted_payload["text"]
    assert "Skills used: [knowledge_base_rag]" in text
    # Verify exact ordering: /[agent-10011] then Skills used then {model : p/c}
    idx_nom = text.index("/[agent-10011]")
    idx_skill = text.index("Skills used: [knowledge_base_rag]")
    idx_footer = text.index("{openai/gpt-4o : 500/100}")

    assert idx_nom < idx_skill < idx_footer


@pytest.mark.anyio
async def test_post_record_with_multiple_skills_used():
    """Verify multiple skills used are formatted as comma-separated bracketed list."""
    posted_payload = {}

    async def mock_post(channel_id, thread_ts, text, username, icon_url):
        posted_payload["text"] = text
        return {"ok": True, "ts": "1234.9999"}

    pipeline = TurnPipeline(
        slack_poster=mock_post,
        debate_turn_pacing_seconds=0,
        slack_post_pause_seconds=0,
    )
    ctx = TurnContext(
        channel_id="C_SKILL_TEST",
        thread_ts="1234.5678",
        raw_text="Hello",
    )
    ctx.thread_key = ("C_SKILL_TEST", "1234.5678")
    ctx.agent_key = "agent-10011"
    ctx.agent_cfg = {"username": "agent-10011", "model": "mistral/mistral-large"}
    ctx.eligible_remaining_agents = ["agent-10001"]
    ctx.remaining = ["agent-10001"]
    ctx.output.final_text = "Analysis complete.\n\n/[agent-10001]"
    ctx.output.skills_used = ["document_reader", "web_research"]
    ctx.output.total_prompt_tokens = 600
    ctx.output.total_completion_tokens = 150

    await pipeline.post_record_stage.stage_post_and_record(ctx)

    text = posted_payload["text"]
    assert "Skills used: [document_reader], [web_research]" in text
    idx_nom = text.index("/[agent-10001]")
    idx_skill = text.index("Skills used: [document_reader], [web_research]")
    idx_footer = text.index("{mistral/mistral-large : 600/150}")

    assert idx_nom < idx_skill < idx_footer


@pytest.mark.anyio
async def test_post_record_omits_skills_used_when_none():
    """Verify Skills used rubric is completely omitted when no skills were used."""
    posted_payload = {}

    async def mock_post(channel_id, thread_ts, text, username, icon_url):
        posted_payload["text"] = text
        return {"ok": True, "ts": "1234.9999"}

    pipeline = TurnPipeline(
        slack_poster=mock_post,
        debate_turn_pacing_seconds=0,
        slack_post_pause_seconds=0,
    )
    ctx = TurnContext(
        channel_id="C_SKILL_TEST",
        thread_ts="1234.5678",
        raw_text="Hello",
    )
    ctx.thread_key = ("C_SKILL_TEST", "1234.5678")
    ctx.agent_key = "agent-10001"
    ctx.agent_cfg = {"username": "agent-10001", "model": "gemini/gemini-3.8-flash"}
    ctx.eligible_remaining_agents = ["agent-10003"]
    ctx.remaining = ["agent-10003"]
    ctx.output.final_text = "Creative thought.\n\n/[agent-10003]"
    ctx.output.skills_used = []
    ctx.output.total_prompt_tokens = 300
    ctx.output.total_completion_tokens = 50

    await pipeline.post_record_stage.stage_post_and_record(ctx)

    text = posted_payload["text"]
    assert "Skills used" not in text
    assert text.endswith("\n\n{gemini/gemini-3.8-flash : 300/50}")


@pytest.mark.anyio
async def test_context_prep_records_passively_injected_file_skill():
    """Verify that when an agent equipped with document_reader accesses file_context,
    the skill is added to skills_used, round evidence is recorded, and post_record renders the rubric."""
    from orchestration.thread_state import thread_state_manager

    channel_id = "C_SKILL_TEST_INJECT"
    thread_ts = "9999.0001"
    thread_state_manager.clear_round_execution_state(channel_id, thread_ts)
    thread_state_manager.evidence.clear_thread_evidence(channel_id, thread_ts)

    posted_payload = {}

    async def mock_post(channel_id, thread_ts, text, username=None, icon_url=None, **kwargs):
        posted_payload["text"] = text
        return {"ok": True, "ts": "9999.9999"}

    pipeline = TurnPipeline(
        slack_poster=mock_post,
        debate_turn_pacing_seconds=0,
        slack_post_pause_seconds=0,
    )
    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="Read playbook",
        in_memory_history=[{"role": "user", "content": "Read playbook"}],
    )
    ctx.thread_key = (channel_id, thread_ts)
    ctx.agent_key = "agent-10002"
    ctx.agent_cfg = {"username": "Innovator", "model": "anthropic/claude-opus-5", "skills": ["document_reader"]}
    ctx.eligible_remaining_agents = ["agent-10003"]
    ctx.remaining = ["agent-10003"]
    ctx.file_context = {
        "file_name": "05_Use_Case_Playbook.pdf",
        "content": "Sales use cases: 1. Pre-call research briefs",
        "size": 1500,
        "authorized_agents": None,
        "required_skill": "document_reader",
    }
    thread_state_manager.set_file_context(channel_id, thread_ts, ctx.file_context)

    # Execute Stage 3 Context Preparation
    await pipeline.context_prep_stage.stage_prepare_context(ctx)

    # Verify skill was recorded into skills_used
    assert "document_reader" in ctx.output.skills_used

    # Verify round evidence was registered with the FULL unwrapped document payload as ground truth
    evidence_block = thread_state_manager.format_round_evidence(channel_id, thread_ts)
    assert evidence_block is not None
    assert "document_reader" in evidence_block
    assert "05_Use_Case_Playbook.pdf" in evidence_block
    assert "Sales use cases: 1. Pre-call research briefs" in evidence_block

    # The active file_context document is injected directly into file_block this turn, so it must be
    # excluded from the same-turn shared evidence block to avoid duplicate prompt tokens.
    assert not ctx.shared_round_evidence or "Sales use cases" not in ctx.shared_round_evidence

    # Execute Stage 5 Post & Record to ensure rubric renders
    ctx.output.final_text = "Analysis from playbook.\n\n/[agent-10003]"
    ctx.output.total_prompt_tokens = 7000
    ctx.output.total_completion_tokens = 500

    await pipeline.post_record_stage.stage_post_and_record(ctx)

    rendered_text = posted_payload["text"]
    assert "Skills used: [document_reader]" in rendered_text
    idx_skill = rendered_text.index("Skills used: [document_reader]")
    idx_footer = rendered_text.index("{anthropic/claude-opus-5 : 7000/500}")
    assert idx_skill < idx_footer
    assert "/[" in rendered_text[:idx_skill]


@pytest.mark.anyio
async def test_context_prep_does_not_record_file_skill_when_agent_lacks_skill():
    """Verify that when an agent lacks the required file skill, it is NOT added to skills_used."""
    from orchestration.thread_state import thread_state_manager

    channel_id = "C_SKILL_TEST_NOAUTH"
    thread_ts = "9999.0002"
    thread_state_manager.clear_round_execution_state(channel_id, thread_ts)
    thread_state_manager.evidence.clear_thread_evidence(channel_id, thread_ts)

    pipeline = TurnPipeline(
        debate_turn_pacing_seconds=0,
        slack_post_pause_seconds=0,
    )
    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="Read playbook",
        in_memory_history=[{"role": "user", "content": "Read playbook"}],
    )
    ctx.thread_key = (channel_id, thread_ts)
    ctx.agent_key = "agent-10003"
    ctx.agent_cfg = {"username": "Sceptic", "model": "anthropic/claude-opus-5", "skills": []}
    ctx.file_context = {
        "file_name": "05_Use_Case_Playbook.pdf",
        "content": "Sales use cases: 1. Pre-call research briefs",
        "size": 1500,
        "authorized_agents": None,
        "required_skill": "document_reader",
    }
    thread_state_manager.set_file_context(channel_id, thread_ts, ctx.file_context)

    await pipeline.context_prep_stage.stage_prepare_context(ctx)

    assert "document_reader" not in ctx.output.skills_used
    assert ctx.output.skills_used == []
    evidence = thread_state_manager.format_round_evidence(channel_id, thread_ts)
    assert evidence is None


@pytest.mark.anyio
async def test_context_prep_does_not_record_file_skill_on_error_content():
    """Verify that file error placeholder content does not trigger skill tracking."""
    channel_id = "C_SKILL_TEST_ERR"
    thread_ts = "9999.0003"

    pipeline = TurnPipeline(
        debate_turn_pacing_seconds=0,
        slack_post_pause_seconds=0,
    )
    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="Read playbook",
        in_memory_history=[{"role": "user", "content": "Read playbook"}],
    )
    ctx.thread_key = (channel_id, thread_ts)
    ctx.agent_key = "agent-10002"
    ctx.agent_cfg = {"username": "Innovator", "skills": ["document_reader"]}
    ctx.file_context = {
        "file_name": "missing.pdf",
        "content": "[Error reading file: not found]",
        "size": 0,
        "authorized_agents": None,
        "required_skill": "document_reader",
    }

    await pipeline.context_prep_stage.stage_prepare_context(ctx)

    assert "document_reader" not in ctx.output.skills_used
    assert ctx.output.skills_used == []

