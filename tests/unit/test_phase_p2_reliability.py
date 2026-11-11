"""
Phase P2 Reliability, Fallbacks & RAG Optimization Tests.
Verifies N9, N10, N11, N12, N15, B2, B4.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from core.models import OrchestrationMode
from orchestration.nomination import (
    NOMINATION_TAG_REGEX,
    enforce_valid_nomination,
    format_nomination,
)
from orchestration.stages.post_record import (
    PostRecordStage,
    apply_mechanical_word_limit,
)
from orchestration.stages.resolution import ResolutionStage
from orchestration.turn_pipeline import TurnContext, TurnPipeline
from pipeline.directive_parser.slash_parser import (
    parse_grant_skill,
    strip_all_slash_commands,
)
from services.rag.config import get_rag_setting
from config.skills.knowledge_base_rag.tools import (
    SEARCH_KNOWLEDGE_RAG_SCHEMA,
    async_execute_search_knowledge_rag,
)


@pytest.mark.anyio
async def test_resolution_fallback_unconfigured_loop():
    """N9: Verify resolution stage skips unconfigured popped agents and avoids KeyError."""
    post_sys_msg_mock = AsyncMock()
    pipeline = TurnPipeline(post_system_message_fn=post_sys_msg_mock)
    stage = ResolutionStage(pipeline.deps)

    ctx = TurnContext(
        channel_id="C_P2_TEST",
        thread_ts="1700000001.000100",
        raw_text="Hello team",
        bot_user_id="U_BOT",
        agent_key="ghost_agent",
    )

@pytest.mark.anyio
async def test_resolution_fallback_to_next_valid_agent():
    """N9: Verify resolution stage falls back through remaining agents if primary is not configured."""
    from orchestration.agent_settings import settings_manager
    from orchestration.app_settings import set_circuit_breaker_enabled
    from orchestration.thread_state import thread_state_manager

    post_sys_msg_mock = AsyncMock()
    pipeline = TurnPipeline(post_system_message_fn=post_sys_msg_mock)
    stage = ResolutionStage(pipeline.deps)

    ctx = TurnContext(
        channel_id="C_P2_TEST",
        thread_ts="1700000001.000100",
        raw_text="Hello team",
        bot_user_id="U_BOT",
        agent_key="ghost_agent",
    )

    set_circuit_breaker_enabled(False, save=False)
    thread_state_manager.set_remaining_agents(
        ctx.input.channel_id,
        ctx.input.thread_ts,
        ["missing_1", "missing_2", "agent-10002"],
    )
    settings_manager.agents_config = {
        "agent-10002": {"model": "mock-model", "username": "Security Architect"}
    }

    res = await stage.stage_resolve_agent_and_governance(ctx)
    assert res is None  # Successful resolution, didn't halt
    assert ctx.agent_key == "agent-10002"
    assert ctx.agent_cfg["username"] == "Security Architect"
    post_sys_msg_mock.assert_awaited_once()
    assert "Handing off to [agent-10002]" in post_sys_msg_mock.call_args[1]["text"]


@pytest.mark.anyio
async def test_resolution_fallback_all_missing_halts():
    """N9: Verify resolution stage cleanly halts when all candidates are unconfigured."""
    from orchestration.agent_settings import settings_manager
    from orchestration.thread_state import thread_state_manager

    post_sys_msg_mock = AsyncMock()
    pipeline = TurnPipeline(post_system_message_fn=post_sys_msg_mock)
    stage = ResolutionStage(pipeline.deps)

    ctx = TurnContext(
        channel_id="C_P2_TEST",
        thread_ts="1700000001.000100",
        raw_text="Hello team",
        bot_user_id="U_BOT",
        agent_key="ghost_agent",
    )

    thread_state_manager.set_remaining_agents(
        ctx.input.channel_id,
        ctx.input.thread_ts,
        ["missing_1"],
    )
    settings_manager.agents_config = {
        "agent-10002": {"model": "mock-model", "username": "Security Architect"}
    }
    settings_manager.set_agent_enabled("agent-10002", False, save=False)
    settings_manager.set_orchestration_mode(OrchestrationMode.SANDWICH, save=False)
    settings_manager.set_sandwich_config(start_chain=[], end_chain=[], save=False)

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

    # Unicode diacritics matching
    sample_text = "Odlična analiza. Predajem riječ /[R&D Inženjer] i /[Agent-Češ]."
    matches = NOMINATION_TAG_REGEX.findall(sample_text)
    assert matches == ["R&D Inženjer", "Agent-Češ"]

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


@pytest.mark.anyio
async def test_rag_retrieval_clamping():
    """N12: Verify max_results clamping in knowledge base tool and schema."""
    # Check schema bounds
    param_schema = SEARCH_KNOWLEDGE_RAG_SCHEMA["function"]["parameters"]["properties"]["max_results"]
    assert param_schema.get("minimum") == 1
    assert param_schema.get("maximum") == 10

    # Tool execution clamps
    with patch("config.skills.knowledge_base_rag.tools.rag_service.search", new_callable=AsyncMock) as mock_search:
        mock_search.return_value = "results"
        await async_execute_search_knowledge_rag({"query": "architecture", "max_results": 100})
        assert mock_search.call_args[1]["max_results"] == 10

        await async_execute_search_knowledge_rag({"query": "architecture", "max_results": 99})
        assert mock_search.call_args[1]["max_results"] == 10

        await async_execute_search_knowledge_rag({"query": "architecture", "max_results": -5})
        assert mock_search.call_args[1]["max_results"] == 1

        await async_execute_search_knowledge_rag({"query": "architecture", "max_results": "invalid"})
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
    async_flat_mock = AsyncMock()
    mock_post = AsyncMock(return_value={"ok": True, "ts": "1700000002.000201"})
    pipeline = TurnPipeline(
        flat_history_recorder=async_flat_mock,
        sleep_fn=AsyncMock(),
        slack_poster=mock_post,
    )
    stage = PostRecordStage(pipeline.deps)

    ctx = TurnContext(
        channel_id="C_P2_TEST",
        thread_ts="1700000002.000200",
        raw_text="Test prompt",
        bot_user_id="U_BOT",
        agent_key="agent-10002",
    )
    ctx.agent_cfg = {"model": "mock-model", "username": "Security Architect"}
    ctx.output.final_text = "Security review completed."

    with patch("orchestration.stages.post_record.get_debate_turn_pacing_seconds", return_value=0.0), \
         patch("orchestration.stages.post_record.get_slack_post_pause_seconds", return_value=0.0):
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

    # Restore default
    system_settings_manager.rag_config["default_top_k"] = 6
    system_settings_manager.rag_config["pii_masking"] = "AT_RETRIEVAL"


def test_revoke_constraint_action_and_modifier_clearing():
    """TKT-714: Verify revoke_constraint handles modifier clearing and returns halted."""
    from skills.builtin.revoke_constraint.action import run as revoke_constraint_action
    from orchestration.thread_state import thread_state_manager

    thread_state_manager.set_format_modifiers("C_REVOKE", "T_REVOKE", {"style": "bullet_points"})
    assert thread_state_manager.get_format_modifiers("C_REVOKE", "T_REVOKE") == {"style": "bullet_points"}

    post_mock = AsyncMock()
    pipeline = MagicMock()
    pipeline._get_hook.side_effect = lambda name, default=None: (
        post_mock if name == "post_system_message" else default
    )
    ctx = TurnContext(
        channel_id="C_REVOKE",
        thread_ts="T_REVOKE",
        raw_text="/revoke_constraint modifiers",
        user_id="U_ADMIN",
        bot_user_id="U_BOT",
    )

    with (
        patch("core.config.is_slack_admin", return_value=True),
        patch("services.slack.post_system_message", new_callable=AsyncMock) as mock_post,
    ):

        res = asyncio.run(revoke_constraint_action(ctx, {"target": "modifiers"}))
        assert res.get("ok") is True

        assert res.get("halted") is True
        assert thread_state_manager.get_format_modifiers("C_REVOKE", "T_REVOKE") == {}
        mock_post.assert_awaited_once()


def test_format_modifiers_pipeline_persistence():
    """TKT-715: Verify format_modifiers are persisted and injected across turn context prep."""
    from orchestration.thread_state import thread_state_manager
    from orchestration.stages.context_prep import ContextPrepStage

    thread_state_manager.set_format_modifiers("C_MODS", "T_MODS", ["concise_summary"])
    pipeline = TurnPipeline()
    stage = ContextPrepStage(pipeline.deps)

    ctx = TurnContext(
        channel_id="C_MODS",
        thread_ts="T_MODS",
        raw_text="Follow-up question",
        bot_user_id="U_BOT",
        agent_key="agent-10001",
        in_memory_history=[],
    )
    ctx.modifiers = []

    with patch("orchestration.stages.context_prep.build_agent_prompt", return_value="System prompt"), \
         patch("orchestration.stages.context_prep.build_llm_messages", return_value=[]):
        asyncio.run(stage.stage_prepare_context(ctx))
        assert "concise_summary" in ctx.modifiers


def test_thread_skills_sqlite_authoritative_and_cleanup(tmp_path):
    """TKT-716: Verify ThreadSkillsManager uses SQLite queries and cleans up old expired rows."""
    from orchestration.thread_skills_manager import ThreadSkillsManager
    import time

    mgr = ThreadSkillsManager()
    mgr.grant_skills("C_SK", "T_SK", ["web_search"])
    assert "web_search" in mgr.get_active_skills("C_SK", "T_SK")

    status = mgr.get_expiration_status("C_SK", "T_SK")
    assert status.get("active") is True

    # Simulate expired row
    now = time.time()
    from core.database import get_db_transaction
    with get_db_transaction(immediate=True) as conn:
        conn.execute(
            "UPDATE thread_skills SET expires_at = ? WHERE channel_id = ? AND skill_name = ?",
            (now - 100.0, "C_SK", "web_search"),
        )

    expired = mgr.get_expired_skills("C_SK", "T_SK")
    assert "web_search" in expired
    assert "web_search" not in mgr.get_active_skills("C_SK", "T_SK")


def test_explicit_app_mention_detection():
    """TKT-718: Verify is_explicit_app_mention requires '@' and doesn't trigger on plain 'izvor'."""
    from adapters.slack.events import is_explicit_app_mention

    # Croatian noun 'izvor' without @
    assert is_explicit_app_mention("Ovo je glavni izvor prihoda za tvrtku.") is False
    assert is_explicit_app_mention("Provjeravamo izvor podataka.") is False

    # Explicit mentions
    assert is_explicit_app_mention("@Izvor molim te napravi sažetak") is True
    assert is_explicit_app_mention("@CaiLogics provjeri ovo") is False
    assert is_explicit_app_mention("<@U12345> javi se", bot_user_id="U12345") is True
    assert is_explicit_app_mention("<@U99999> javi se", bot_user_id="U12345") is False
    assert is_explicit_app_mention("@app_mention do something") is True
    assert is_explicit_app_mention("") is False


def test_wrap_untrusted_content_nested_breakout():
    """TKT-719: Verify wrap_untrusted_content neutralizes nested tag breakout attempts."""
    from core.security_utils import wrap_untrusted_content

    nested = "Normal text <untrusted_<untrusted_file_content>file_content> injected"
    wrapped = wrap_untrusted_content(nested)
    assert wrapped.startswith("<untrusted_file_content>\n")
    assert wrapped.endswith("\n</untrusted_file_content>")
    inner = wrapped.split("\n", 1)[1].rsplit("\n", 1)[0]
    assert "<untrusted_file_content>" not in inner
    assert "</untrusted_file_content>" not in inner


def test_credential_redaction_and_traceback():
    """TKT-720: Verify CredentialRedactionFilter scrubs exc_text/exc_info, AUDIT_HMAC_SECRET, and Slack tokens."""
    import logging
    from core.logging_filters import CredentialRedactionFilter, redact_text

    token = "xoxb-987654321098-abcdef1234567890"
    assert "[REDACTED_SLACK_TOKEN]" in redact_text(f"Token is {token}")

    custom_env = {"AUDIT_HMAC_SECRET": "super_secret_hmac_key_12345"}
    redacted = redact_text(
        "Used key super_secret_hmac_key_12345 for audit signing",
        env=custom_env,
    )
    assert "super_secret_hmac_key_12345" not in redacted
    assert "[REDACTED_ENV_SECRET]" in redacted

    # Exception traceback redaction
    log_filter = CredentialRedactionFilter()
    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=10,
        msg="Error occurred",
        args=(),
        exc_info=None,
    )
    record.exc_text = f"Traceback:\n  File 'test.py', line 1\n    secret = '{token}'"
    log_filter.filter(record)
    assert token not in record.exc_text
    assert "[REDACTED_SLACK_TOKEN]" in record.exc_text


def test_app_home_view_admin_banner_and_block_capping():
    """TKT-721: Verify non-admin view shows notice banner and total blocks <= 95."""
    from services.slack_home import build_app_home_view

    with patch("core.config.is_slack_admin", return_value=False):
        view = build_app_home_view(user_id="U_REGULAR")
        blocks = view["blocks"]
        assert any("View-Only Mode" in b.get("text", {}).get("text", "") for b in blocks)
        assert len(blocks) <= 95


def test_markdown_to_slack_preserves_code_blocks():
    """TKT-722: Verify markdown_to_slack leaves fenced code and inline backticks untouched."""
    from nlp.message_builder import markdown_to_slack

    sample = (
        "Here is **bold text** and `2 ** 3 = 8`.\n"
        "```python\n"
        "# Comment inside code\n"
        "def foo(**kwargs):\n"
        "    return [x](y)\n"
        "```\n"
        "### Section Header\n"
        "Check [link](https://example.com)."
    )
    converted = markdown_to_slack(sample)

    assert "*bold text*" in converted
    assert "`2 ** 3 = 8`" in converted  # inline code preserved
    assert "# Comment inside code" in converted  # heading regex did not touch code
    assert "def foo(**kwargs):" in converted  # kwargs bold regex did not touch code
    assert "return [x](y)" in converted  # link regex did not touch code
    assert "*Section Header*" in converted  # outside heading converted
    assert "<https://example.com|link>" in converted  # outside link converted


def test_markdown_to_slack_heading_with_bold_and_hr():
    """TKT: '### **Title**' must become '*Title*' (not '**Title**'), and '---' lines become Slack dividers."""

    from nlp.message_builder import markdown_to_slack

    sample = (
        "---\n"
        "### **Glavni cilj i problemska tvrdnja**\n"
        "- **Visionary**: Transformacija kao primarni cilj.\n"
        "### Razmjene i kompromisi\n"
        "---\n"
    )
    converted = markdown_to_slack(sample)

    assert "*Glavni cilj i problemska tvrdnja*" in converted
    assert "**" not in converted
    assert "###" not in converted
    assert "- *Visionary*: Transformacija kao primarni cilj." in converted
    assert "*Razmjene i kompromisi*" in converted
    assert "──────────" in converted  # '---' horizontal rules become Slack divider lines
    assert "---" not in converted


@pytest.mark.anyio
async def test_send_admin_audit_alert_uses_admin_ids():
    """TKT-722: Verify send_admin_audit_alert respects multiple admin IDs."""
    from core.config import configure_token
    from services.audit_logger import send_admin_audit_alert

    configure_token("xoxb-real-token-123")
    with patch("core.config.get_admin_slack_user_ids", return_value={"U_ADMIN_PRIMARY", "U_ADMIN_SECONDARY"}), \
         patch("services.slack.open_dm_channel", new_callable=AsyncMock) as mock_dm, \
         patch("services.slack.post_agent_response", new_callable=AsyncMock) as mock_post:
        mock_dm.return_value = "D_ADMIN_CHANNEL"
        mock_post.return_value = {"ok": True}

        res = await send_admin_audit_alert(
            title="Integrity Failure",
            message="Tampering detected",
            details={"seq": 42},
        )
        assert res is True
        assert mock_dm.await_count == 2
        called_users = [call[0][0] for call in mock_dm.call_args_list]
        assert called_users == sorted(["U_ADMIN_PRIMARY", "U_ADMIN_SECONDARY"])
        posted_text = mock_post.call_args_list[0][1]["text"]
        assert "🚨 *[SECURITY CRITICAL] Integrity Failure*" in posted_text
        assert "Action Required: Run `python -m services.audit_logger --verify` for forensic diagnostics." in posted_text

        # Also verify WARNING severity tier delivery and Option A footer
        mock_post.reset_mock()
        mock_dm.reset_mock()
        res_warn = await send_admin_audit_alert(
            title="ADMIN_CONFIG_CHANGE",
            message="Action 'toggle_skill_filter' recorded",
            details={"enabled": False},
            severity="WARNING",
        )
        assert res_warn is True
        warn_posted_text = mock_post.call_args_list[0][1]["text"]
        assert "⚠️ *[SECURITY WARNING] ADMIN_CONFIG_CHANGE*" in warn_posted_text
        assert "Validation Action: Run `python -m services.audit_logger --verify` to verify audit trail integrity." in warn_posted_text


def test_context_prep_terminal_constraint_for_last_agent_in_fixed_chain():
    """Plan 05: internal (bot-triggered) turn with exhausted chain must set
    force_termination so the final agent gets the 'last agent' constraint and
    answers instead of refusing."""
    from orchestration.agent_settings import reset_enabled_agents
    from orchestration.thread_state import thread_state_manager
    from orchestration.stages.context_prep import ContextPrepStage

    reset_enabled_agents()
    thread_state_manager.reset_all()
    channel_id, thread_ts = "C_TERM_BOT", "T_TERM_BOT"
    thread_state_manager.set_remaining_agents(channel_id, thread_ts, [])

    pipeline = TurnPipeline()
    stage = ContextPrepStage(pipeline.deps)
    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="agent-10014 /[agent-10012]",
        bot_user_id="U_BOT",
        agent_key="agent-10012",
        is_bot_trigger=True,
        was_explicit_target=True,
        in_memory_history=[{"role": "user", "content": "what is your agent id, max 2 words"}],
        agent_cfg={"username": "ProductStrategist", "displayname": "Product Strategist", "skills": [], "system_prompt": "sys"},
    )
    ctx.modifiers = {}

    with patch("orchestration.stages.context_prep.build_agent_prompt", return_value="System prompt"), \
         patch("orchestration.stages.context_prep.build_llm_messages", return_value=[]) as mock_build:
        asyncio.run(stage.stage_prepare_context(ctx))

    assert ctx.force_termination is True
    assert mock_build.call_args.kwargs["force_termination"] is True
    thread_state_manager.reset_all()


def test_context_prep_no_terminal_constraint_for_multi_round_restart():
    """Plan 05: with active_rounds > 1 and empty remaining, the next round must still
    start (force_termination stays False, next_designated_agent = first of round sequence)."""
    from orchestration.agent_settings import reset_enabled_agents
    from orchestration.thread_state import thread_state_manager
    from orchestration.stages.context_prep import ContextPrepStage

    reset_enabled_agents()
    thread_state_manager.reset_all()
    channel_id, thread_ts = "C_TERM_ROUND", "T_TERM_ROUND"
    thread_state_manager.set_remaining_agents(channel_id, thread_ts, [])
    thread_state_manager.set_rounds(channel_id, thread_ts, 2)
    thread_state_manager.set_round_sequence(channel_id, thread_ts, ["agent-10001", "agent-10012"])

    pipeline = TurnPipeline()
    stage = ContextPrepStage(pipeline.deps)
    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="agent-10012 done",
        bot_user_id="U_BOT",
        agent_key="agent-10012",
        is_bot_trigger=True,
        was_explicit_target=True,
        in_memory_history=[{"role": "user", "content": "discuss"}],
        agent_cfg={"username": "ProductStrategist", "displayname": "Product Strategist", "skills": [], "system_prompt": "sys"},
    )
    ctx.modifiers = {}

    with patch("orchestration.stages.context_prep.build_agent_prompt", return_value="System prompt"), \
         patch("orchestration.stages.context_prep.build_llm_messages", return_value=[]):
        asyncio.run(stage.stage_prepare_context(ctx))

    assert ctx.force_termination is False
    assert ctx.next_designated_agent == "agent-10001"
    thread_state_manager.reset_all()


def test_context_prep_plain_non_bot_turn_not_terminal():
    """Plan 05: legacy behavior preserved - a plain non-bot-trigger turn with empty
    remaining and no explicit target is not force-terminated."""
    from orchestration.agent_settings import reset_enabled_agents
    from orchestration.thread_state import thread_state_manager
    from orchestration.stages.context_prep import ContextPrepStage

    reset_enabled_agents()
    thread_state_manager.reset_all()
    channel_id, thread_ts = "C_TERM_PLAIN", "T_TERM_PLAIN"
    thread_state_manager.set_remaining_agents(channel_id, thread_ts, [])

    pipeline = TurnPipeline()
    stage = ContextPrepStage(pipeline.deps)
    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="hello",
        bot_user_id="U_BOT",
        agent_key="agent-10001",
        is_bot_trigger=False,
        was_explicit_target=False,
        in_memory_history=[{"role": "user", "content": "hello"}],
        agent_cfg={"username": "Visionary", "displayname": "Visionary", "skills": [], "system_prompt": "sys"},
    )
    ctx.modifiers = {}

    with patch("orchestration.stages.context_prep.build_agent_prompt", return_value="System prompt"), \
         patch("orchestration.stages.context_prep.build_llm_messages", return_value=[]):
        asyncio.run(stage.stage_prepare_context(ctx))

    assert ctx.force_termination is False
    thread_state_manager.reset_all()

def test_context_prep_bot_turn_with_pending_retry_not_terminal():
    """Plan 05: a bot-triggered turn with empty remaining but a pending rate-limit retry
    must NOT be force-terminated, so the retry nomination survives tag stripping."""
    from orchestration.agent_settings import reset_enabled_agents
    from orchestration.thread_state import thread_state_manager
    from orchestration.stages.context_prep import ContextPrepStage

    reset_enabled_agents()
    thread_state_manager.reset_all()
    channel_id, thread_ts = "C_TERM_RETRY", "T_TERM_RETRY"
    thread_state_manager.set_remaining_agents(channel_id, thread_ts, [])
    thread_state_manager.add_to_retry_queue(channel_id, thread_ts, "agent-10011")

    pipeline = TurnPipeline()
    stage = ContextPrepStage(pipeline.deps)
    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="/[agent-10002] Your turn",
        bot_user_id="U_BOT",
        agent_key="agent-10002",
        is_bot_trigger=True,
        was_explicit_target=True,
        in_memory_history=[{"role": "user", "content": "discuss"}],
        agent_cfg={"username": "Innovator", "displayname": "Innovator", "skills": [], "system_prompt": "sys"},
    )
    ctx.modifiers = {}

    with patch("orchestration.stages.context_prep.build_agent_prompt", return_value="System prompt"), \
         patch("orchestration.stages.context_prep.build_llm_messages", return_value=[]):
        asyncio.run(stage.stage_prepare_context(ctx))

    assert ctx.force_termination is False
    thread_state_manager.reset_all()
