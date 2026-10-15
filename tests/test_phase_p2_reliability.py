"""
Phase P2 Reliability, Fallbacks & RAG Optimization Tests.
Verifies N9, N10, N11, N12, N15, B2, B4.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from core.models import OrchestrationMode
from orchestration.nomination import (
    NOMINATION_TAG_RE,
    NOMINATION_TAG_REGEX,
    enforce_valid_nomination,
    format_nomination,
    format_nomination_tag,
)
from orchestration.stages.post_record import (
    PostRecordStage,
    apply_mechanical_word_limit,
)
from orchestration.stages.resolution import ResolutionStage
from orchestration.turn_pipeline import TurnContext
from pipeline.directive_parser.slash_parser import (
    parse_grant_skill,
    strip_all_slash_commands,
)
from services.rag.config import get_rag_setting, load_rag_settings_from_file
from skills.knowledge_base_rag.tools import (
    SEARCH_KNOWLEDGE_RAG_SCHEMA,
    execute_search_knowledge_rag,
)


@pytest.mark.anyio
async def test_resolution_fallback_unconfigured_loop():
    """N9: Verify resolution stage skips unconfigured popped agents and avoids KeyError."""
    pipeline = MagicMock()
    post_sys_msg_mock = AsyncMock()
    pipeline._get_hook.side_effect = lambda name, default=None: (
        post_sys_msg_mock if name == "post_system_message" else default
    )

    stage = ResolutionStage(pipeline)

    ctx = TurnContext(
        channel_id="C_P2_TEST",
        thread_ts="1700000001.000100",
        raw_text="Hello team",
        bot_user_id="U_BOT",
        agent_key="ghost_agent",
    )

    # Mock remaining queue to first return 2 invalid agents, then a valid agent
    with patch(
        "orchestration.stages.resolution.thread_state_manager.pop_remaining_agent",
        side_effect=["missing_1", "missing_2", "agent-10002", None],
    ), patch.object(
        stage.pipeline,
        "_get_hook",
        side_effect=lambda name, default=None: (
            post_sys_msg_mock if name == "post_system_message" else default
        ),
    ), patch(
        "orchestration.stages.resolution.is_circuit_breaker_enabled",
        return_value=False,
    ), patch.dict(
        "orchestration.stages.resolution.settings_manager._agents_config",
        {"agent-10002": {"model": "mock-model", "username": "Security Architect"}},
        clear=True,
    ):
        res = await stage.stage_resolve_agent_and_governance(ctx)
        assert res is None  # Successful resolution, didn't halt
        assert ctx.agent_key == "agent-10002"
        assert ctx.agent_cfg["username"] == "Security Architect"
        post_sys_msg_mock.assert_awaited_once()
        assert "Handing off to [agent-10002]" in post_sys_msg_mock.call_args[1]["text"]


@pytest.mark.anyio
async def test_resolution_fallback_all_missing_halts():
    """N9: Verify resolution stage cleanly halts when all candidates are unconfigured."""
    pipeline = MagicMock()
    post_sys_msg_mock = AsyncMock()
    pipeline._get_hook.side_effect = lambda name, default=None: (
        post_sys_msg_mock if name == "post_system_message" else default
    )

    stage = ResolutionStage(pipeline)

    ctx = TurnContext(
        channel_id="C_P2_TEST",
        thread_ts="1700000001.000100",
        raw_text="Hello team",
        bot_user_id="U_BOT",
        agent_key="ghost_agent",
    )

    with patch(
        "orchestration.stages.resolution.thread_state_manager.pop_remaining_agent",
        side_effect=["missing_1", None],
    ), patch.dict(
        "orchestration.stages.resolution.settings_manager._agents_config",
        {"agent-10002": {"model": "mock-model", "username": "Security Architect"}},
        clear=True,
    ), patch(
        "orchestration.stages.resolution.settings_manager.get_enabled_agents",
        return_value=[],
    ), patch(
        "orchestration.stages.resolution.settings_manager.get_orchestration_mode",
        return_value=OrchestrationMode.SANDWICH,
    ), patch(
        "orchestration.stages.resolution.settings_manager.get_sandwich_config",
        return_value={"end_chain": []},
    ):
        res = await stage.stage_resolve_agent_and_governance(ctx)
        assert res is not None
        assert res.get("halted") is True
        assert res.get("reason") == "agent_not_configured"
        assert "no other agents are available" in post_sys_msg_mock.call_args[1]["text"]


def test_nomination_unicode_and_format_helpers():
    """N10: Verify consolidated regex handles unicode and canonical format helper works."""
    assert format_nomination("agent-10002") == "/[agent-10002]"
    assert format_nomination("/[agent-10002]") == "/[agent-10002]"
    assert format_nomination(" [Inženjer] ") == "/[Inženjer]"
    assert format_nomination_tag("agent-10002") == "/[agent-10002]"

    # Unicode diacritics matching
    sample_text = "Odlična analiza. Predajem riječ /[R&D Inženjer] i /[Agent-Češ]."
    matches = NOMINATION_TAG_REGEX.findall(sample_text)
    assert matches == ["R&D Inženjer", "Agent-Češ"]

    matches_alias = NOMINATION_TAG_RE.findall(sample_text)
    assert matches_alias == ["R&D Inženjer", "Agent-Češ"]

    # enforce_valid_nomination uses format_nomination
    corrected = enforce_valid_nomination(
        text="My report is ready.",
        current_agent_key="agent-10001",
        remaining=["agent-10003"],
        force_termination=False,
    )
    assert corrected.endswith("\n\n/[agent-10003]")


def test_mechanical_word_limit_with_unicode_tag():
    """N10: Verify apply_mechanical_word_limit preserves trailing unicode nomination tags."""
    long_body = " ".join(["riječ"] * 120)
    text = f"{long_body}\n\n/[R&D Inženjer Češ]"
    truncated = apply_mechanical_word_limit(text, max_words=50)

    assert truncated.endswith("\n\n/[R&D Inženjer Češ]")
    words = truncated.split()
    # 100 limit words (50 + 50) + nomination tag tokens
    assert len(words) <= 105


def test_deterministic_slash_parser_preserves_indentation_and_code():
    """N11: Verify slash parsing preserves indentation, code blocks, and doesn't split names like Andrea."""
    raw_prompt = (
        "/rounds 3\n"
        "Here is the config:\n"
        "```yaml\n"
        "server:\n"
        "  port: 8080\n"
        "  host: localhost\n"
        "```\n"
        "Andrea will review this."
    )
    cleaned = strip_all_slash_commands(raw_prompt)
    assert "port: 8080" in cleaned
    assert "  host: localhost" in cleaned
    assert "Andrea will review this." in cleaned
    assert "/rounds" not in cleaned

    # Rigid parse_grant_skill does not accept non-agent, non-bracketed words beyond the target
    matched, skill, agents, cleaned_grant = parse_grant_skill(
        "/grant_skill", "/grant_skill web_search [Security Architect] Andrea molim te provjeri"
    )
    assert matched is True
    assert skill == "web_search"
    assert agents == ["Security Architect"]
    assert "Andrea molim te provjeri" in cleaned_grant


def test_rag_retrieval_clamping():
    """N12: Verify max_results clamping in knowledge base tool and schema."""
    # Check schema bounds
    param_schema = SEARCH_KNOWLEDGE_RAG_SCHEMA["function"]["parameters"]["properties"]["max_results"]
    assert param_schema.get("minimum") == 1
    assert param_schema.get("maximum") == 10

    # Tool execution clamps
    with patch("skills.knowledge_base_rag.tools._run_coroutine_sync", return_value="mock results") as mock_run:
        execute_search_knowledge_rag({"query": "architecture", "max_results": 100})
        # Verify clamped to 10
        coro = mock_run.call_args[0][0]
        # Close coroutine to avoid warning
        coro.close()

    with patch("skills.knowledge_base_rag.tools.rag_service.search", new_callable=AsyncMock) as mock_search:
        mock_search.return_value = "results"
        execute_search_knowledge_rag({"query": "architecture", "max_results": 99})
        assert mock_search.call_args[1]["max_results"] == 10

        execute_search_knowledge_rag({"query": "architecture", "max_results": -5})
        assert mock_search.call_args[1]["max_results"] == 1

        execute_search_knowledge_rag({"query": "architecture", "max_results": "invalid"})
        assert mock_search.call_args[1]["max_results"] == 3


def test_rag_batch_fts_reindexing():
    """N12: Verify VectorStore.insert_chunks with rebuild_index=False defers FTS index creation."""
    from services.rag.vector_store import VectorStore
    from services.rag.document_parser import DocumentChunk

    store = VectorStore()
    mock_table = MagicMock()
    store.get_or_create_table = MagicMock(return_value=mock_table)
    store._ensure_fts_index = MagicMock()

    chunk = DocumentChunk(
        chunk_id="c1",
        agent_id="agent-1",
        doc_id="d1",
        filename="f1.md",
        section_header="Header",
        text="sample text",
    )
    vectors = [[0.1] * 3072]

    # Without rebuild_index (False)
    store.insert_chunks([chunk], vectors, rebuild_index=False)
    mock_table.add.assert_called_once()
    store._ensure_fts_index.assert_not_called()

    # Explicit rebuild
    store.rebuild_fts_index()
    store._ensure_fts_index.assert_called_once_with(mock_table)


@pytest.mark.anyio
async def test_async_flat_history_recording():
    """N15: Verify PostRecordStage calls async_record_flat_history without blocking event loop."""
    pipeline = MagicMock()
    pipeline._sleep = AsyncMock()
    async_flat_mock = AsyncMock()
    pipeline._get_hook.side_effect = lambda name, default=None: (
        async_flat_mock if name == "async_record_flat_history" else default
    )

    stage = PostRecordStage(pipeline)

    ctx = TurnContext(
        channel_id="C_P2_TEST",
        thread_ts="1700000002.000200",
        raw_text="Test prompt",
        bot_user_id="U_BOT",
        agent_key="agent-10002",
    )
    ctx.agent_cfg = {"model": "mock-model", "username": "Security Architect"}
    ctx.final_text = "Security review completed."

    with patch("orchestration.stages.post_record.get_debate_turn_pacing_seconds", return_value=0.0), \
         patch("orchestration.stages.post_record.get_slack_post_pause_seconds", return_value=0.0), \
         patch("orchestration.stages.post_record.post_agent_response", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = {"ok": True, "ts": "1700000002.000201"}
        res = await stage.stage_post_and_record(ctx)
        assert res.get("ok") is True
        async_flat_mock.assert_awaited_once()


def test_settings_manager_save_synchronous(tmp_path):
    """B2: Verify AgentSettingsManager.save() writes synchronously without background race."""
    from orchestration.agent_settings import AgentSettingsManager

    settings_file = tmp_path / "test_settings.json"
    backup_file = tmp_path / "test_backup.json"

    # Write initial settings file
    import json
    initial_data = {
        "agents": {
            "agent-10002": {
                "username": "Security Architect",
                "model": "gemini/gemini-3.8-flash",
                "displayname": "Security Architect",
                "icon_url": "static/image_1.png",
                "skills": [],
            }
        },
        "enabled_agents": ["agent-10002"],
    }
    with open(settings_file, "w", encoding="utf-8") as f:
        json.dump(initial_data, f)

    mgr = AgentSettingsManager(settings_file=settings_file, backup_file=backup_file, auto_load=True)
    mgr.enabled_agents.clear()
    mgr.save()

    # The file on disk must synchronously reflect the cleared enabled_agents immediately upon return
    with open(settings_file, "r", encoding="utf-8") as f:
        saved_data = json.load(f)
    assert saved_data["enabled_agents"] == []


def test_rag_config_delegation_to_system_settings():
    """B4: Verify get_rag_setting delegates to SystemSettingsManager without config drift."""
    from orchestration.system_settings import system_settings_manager

    # Override RAG setting in system_settings_manager in-memory
    system_settings_manager.rag_config["default_top_k"] = 5
    system_settings_manager.rag_config["pii_masking"] = "NONE"

    assert get_rag_setting("default_top_k") == 5
    assert get_rag_setting("pii_masking") == "NONE"

    # Verify load_rag_settings_from_file returns the manager's config
    cfg = load_rag_settings_from_file()
    assert cfg["default_top_k"] == 5
    assert cfg["pii_masking"] == "NONE"

    # Restore default
    system_settings_manager.rag_config["default_top_k"] = 3
    system_settings_manager.rag_config["pii_masking"] = "AT_RETRIEVAL"
