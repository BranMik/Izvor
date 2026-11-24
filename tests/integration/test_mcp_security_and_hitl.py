"""
Security and Human-In-The-Loop (HITL) integration tests for Model Context Protocol (MCP).

Verifies Roadmap R-04 risk controls:
1. Unauthorized agent principal calling external MCP tool fails closed with SECURITY_POLICY_VIOLATION.
2. Outbound secret/PII masking prevents credential exfiltration in MCP arguments.
3. Inbound secret scanning blocks leaked credentials in MCP responses.
4. Mutating MCP tools trigger mandatory HITL approval.
"""

from contextlib import asynccontextmanager
import json
from unittest.mock import AsyncMock, MagicMock
import pytest

from core.domain import ToolPrincipal
from config_store.system_settings import set_mcp_servers
from services.audit_logger import audit_logger
from services.mcp.client_manager import McpClientManager
from services.mcp.security_bridge import execute_external_mcp_tool
from skills.registry import SkillDefinition, SkillRegistry


class DummyContent:
    def __init__(self, text: str):
        self.text = text


class DummyCallToolResult:
    def __init__(self, text: str, is_error: bool = False):
        self.content = [DummyContent(text)]
        self.isError = is_error


@pytest.fixture
def secure_mcp_environment():
    """Sets up an MCP server with security and mutating tool configurations."""
    set_mcp_servers({
        "internal_db": {
            "enabled": True,
            "transport": "stdio",
            "command": "python",
            "mutating_tools": ["drop_table", "insert_record"],
            "timeout_seconds": 5.0,
            "outbound_pii_masking": True,
        }
    }, save=False)

    mock_session = MagicMock()
    mock_session.initialize = AsyncMock()

    recorded_calls = []

    async def _mock_call(name, args):
        recorded_calls.append({"name": name, "args": dict(args)})
        if name == "drop_table":
            return DummyCallToolResult("Table dropped successfully")
        elif name == "leak_test":
            # Response containing a fake AWS key
            return DummyCallToolResult("Connected with key AKIAIOSFODNN7EXAMPLE")
        return DummyCallToolResult("OK")

    mock_session.call_tool = AsyncMock(side_effect=_mock_call)

    @asynccontextmanager
    async def mock_transport_factory(server_name, server_cfg):
        yield mock_session

    mgr = McpClientManager()
    mgr.configure_transport_factory(mock_transport_factory)

    yield {
        "session": mock_session,
        "manager": mgr,
        "recorded_calls": recorded_calls,
    }

    mgr.reset_transport_factory()


@pytest.mark.anyio
async def test_r04_unauthorized_agent_principal_fails_closed(secure_mcp_environment):
    """
    R-04 Control 1: Agent principal without authorized skill cannot call MCP tool.
    Must fail closed and log SECURITY_POLICY_VIOLATION.
    """
    mgr = secure_mcp_environment["manager"]

    # Unauthorized agent (no skills granted)
    principal = ToolPrincipal(kind="agent", id="unauthorized_agent")

    res = await execute_external_mcp_tool(
        namespaced_tool_name="mcp__internal_db__drop_table",
        arguments={"table": "users"},
        agent_name=principal,
        authorized_skills=[],  # No skills authorized
        skill_registry=SkillRegistry(),
        client_manager=mgr,
    )

    data = json.loads(res)
    assert "Permission denied" in data["error"]
    assert "unauthorized_agent" in data["error"]

    # Verify mock session was NEVER called
    assert len(secure_mcp_environment["recorded_calls"]) == 0

    # Verify SECURITY_POLICY_VIOLATION was logged to audit
    from services.audit_logger import get_audit_trail

    audit_events = get_audit_trail(audit_dir=audit_logger.audit_dir)
    violations = [e for e in audit_events if e.get("event_type") == "SECURITY_POLICY_VIOLATION"]
    assert len(violations) > 0
    assert any("mcp__internal_db__drop_table" in str(v.get("target")) for v in violations)


@pytest.mark.anyio
async def test_r04_outbound_secret_masking(secure_mcp_environment):
    """
    R-04 Control 2: Outbound arguments containing secrets or PII are masked
    before being sent to the external MCP server transport.
    """
    mgr = secure_mcp_environment["manager"]

    # Authorized principal with database skill
    principal = ToolPrincipal(kind="agent", id="db_admin")
    registry = SkillRegistry()
    registry._skills["db_skill"] = SkillDefinition(
        name="db_skill",
        display_name="DB",
        description="desc",
        tools=["mcp__internal_db__insert_record"],
    )

    # Argument contains a private key and a Slack token
    sensitive_args = {
        "user_email": "john.doe@example.com",
        "raw_note": "Here is the key xoxb-123456789012-123456789012-abcdefghijklmnopqrstuvwx",
    }

    await execute_external_mcp_tool(
        namespaced_tool_name="mcp__internal_db__insert_record",
        arguments=sensitive_args,
        agent_name=principal,
        authorized_skills=["db_skill"],
        skill_registry=registry,
        client_manager=mgr,
    )

    assert len(secure_mcp_environment["recorded_calls"]) == 1
    sent_args = secure_mcp_environment["recorded_calls"][0]["args"]

    # Confirm Slack token was redacted before reaching MCP server
    assert "xoxb-123456789012" not in sent_args["raw_note"]
    assert "<SLACK_TOKEN" in sent_args["raw_note"]


@pytest.mark.anyio
async def test_r04_inbound_secret_leak_blocked(secure_mcp_environment):
    """
    R-04 Control 3: Inbound response from MCP server containing leaked secrets
    is blocked and replaced with security policy error.
    """
    mgr = secure_mcp_environment["manager"]

    principal = ToolPrincipal(kind="agent", id="db_admin")
    registry = SkillRegistry()
    registry._skills["db_skill"] = SkillDefinition(
        name="db_skill",
        display_name="DB",
        description="desc",
        tools=["mcp__internal_db__leak_test"],
    )

    res = await execute_external_mcp_tool(
        namespaced_tool_name="mcp__internal_db__leak_test",
        arguments={},
        agent_name=principal,
        authorized_skills=["db_skill"],
        skill_registry=registry,
        client_manager=mgr,
    )

    # Inbound response had an AWS key -> must be blocked
    data = json.loads(res)
    assert data.get("isError") is True
    assert "security policy" in data.get("error", "").lower()
    assert "AKIAIOSFODNN7EXAMPLE" not in res


def test_r04_mutating_mcp_tool_requires_hitl():
    """
    R-04 Control 4: Mutating MCP tool requires HITL approval.
    """
    mgr = McpClientManager()
    set_mcp_servers({
        "internal_db": {
            "enabled": True,
            "transport": "stdio",
            "command": "python",
            "mutating_tools": ["drop_table"],
        }
    }, save=False)

    # drop_table is mutating -> HITL required
    assert mgr.is_tool_mutating("mcp__internal_db__drop_table") is True

    # select_query is read-only -> HITL not required
    assert mgr.is_tool_mutating("mcp__internal_db__select_query") is False


@pytest.mark.anyio
async def test_r04_mcp_post_execution_audit_statuses():
    """TKT-1223: TOOL_EXECUTE_MCP is emitted after execution with SUCCESS, FAILURE, or BLOCKED status."""
    from services.audit_logger import get_audit_trail

    set_mcp_servers({
        "audit_srv": {
            "enabled": True,
            "transport": "stdio",
            "command": "python",
            "timeout_seconds": 5.0,
            "outbound_pii_masking": True,
        }
    }, save=False)

    mock_session = MagicMock()
    mock_session.initialize = AsyncMock()

    async def _mock_call(name, args):
        if name == "ok_tool":
            return DummyCallToolResult("healthy response")
        if name == "fail_tool":
            return DummyCallToolResult("upstream crash", is_error=True)
        if name == "leak_tool":
            return DummyCallToolResult("leaked AKIAIOSFODNN7EXAMPLE")
        return DummyCallToolResult("OK")

    mock_session.call_tool = AsyncMock(side_effect=_mock_call)

    @asynccontextmanager
    async def transport_factory(server_name, server_cfg):
        yield mock_session

    mgr = McpClientManager()
    mgr.configure_transport_factory(transport_factory)

    principal = ToolPrincipal(kind="agent", id="audit_agent")
    registry = SkillRegistry()
    registry._skills["audit_skill"] = SkillDefinition(
        name="audit_skill",
        display_name="Audit",
        description="desc",
        tools=[
            "mcp__audit_srv__ok_tool",
            "mcp__audit_srv__fail_tool",
            "mcp__audit_srv__leak_tool",
        ],
    )

    try:
        res_ok = await execute_external_mcp_tool(
            namespaced_tool_name="mcp__audit_srv__ok_tool",
            arguments={},
            agent_name=principal,
            authorized_skills=["audit_skill"],
            skill_registry=registry,
            client_manager=mgr,
        )
        assert res_ok == "healthy response"

        res_fail = await execute_external_mcp_tool(
            namespaced_tool_name="mcp__audit_srv__fail_tool",
            arguments={},
            agent_name=principal,
            authorized_skills=["audit_skill"],
            skill_registry=registry,
            client_manager=mgr,
        )
        assert json.loads(res_fail)["isError"] is True

        res_leak = await execute_external_mcp_tool(
            namespaced_tool_name="mcp__audit_srv__leak_tool",
            arguments={},
            agent_name=principal,
            authorized_skills=["audit_skill"],
            skill_registry=registry,
            client_manager=mgr,
        )
        assert "security policy" in json.loads(res_leak)["error"].lower()

        events = [
            e
            for e in get_audit_trail(audit_dir=audit_logger.audit_dir)
            if e.get("event_type") == "TOOL_EXECUTE_MCP"
        ]
        by_action = {e.get("action"): e.get("status") for e in events}
        assert by_action.get("mcp__audit_srv__ok_tool") == "SUCCESS"
        assert by_action.get("mcp__audit_srv__fail_tool") == "FAILURE"
        assert by_action.get("mcp__audit_srv__leak_tool") == "BLOCKED"
    finally:
        await mgr.shutdown()


@pytest.mark.anyio
async def test_mcp_inbound_sanitization_preserves_existing_turn_token_mapping(secure_mcp_environment):
    """TKT-1302: Inbound MCP PII masking reuses and updates turn_token_mapping to prevent index collisions."""
    from services.mcp.security_bridge import async_sanitize_inbound_mcp_result

    turn_mapping = {"<EMAIL_0>": "existing.user@corp.com"}
    res1 = await async_sanitize_inbound_mcp_result(
        "Escalate to new.contact@vendor.com and existing.user@corp.com",
        token_mapping=turn_mapping,
    )
    assert res1 is not None
    assert "<EMAIL_0>" in res1
    assert "<EMAIL_1>" in res1
    assert "new.contact@vendor.com" not in res1
    assert turn_mapping["<EMAIL_0>"] == "existing.user@corp.com"
    assert turn_mapping["<EMAIL_1>"] == "new.contact@vendor.com"


@pytest.mark.anyio
async def test_hitl_strict_privacy_threads_token_mapping_and_masks_stored_args(secure_mcp_environment):
    """
    TKT-1402 (CB-06): When llm_strict_privacy is enabled and an HITL-gated MCP tool is paused,
    stored hitl_approvals.fn_args contains masked placeholders (no plaintext PII), while
    _execute_and_resume_hitl_approval threads chk.token_mapping so inbound PII tokens do not collide.
    """
    from core.config import configure_token, reset_token
    from core.database import get_connection
    from core.llm_utils import configure_llm_completion_fn, reset_llm_completion_fn
    from config_store.system_settings import system_settings_manager
    from adapters.slack.actions.hitl_actions import _execute_and_resume_hitl_approval
    from orchestration.thread_state import thread_state_manager
    from orchestration.turn_pipeline import TurnContext, TurnPipeline
    from services.hitl import approve_hitl_request
    from services.mcp.client_manager import get_mcp_client_manager
    from services.thread_skills_store import thread_skills_manager
    from services.turn_checkpoint_store import turn_checkpoint_store
    from services.slack.messages import configure_post_agent_response, reset_post_agent_response
    from skills.registry import skill_registry as global_skill_registry

    global_mgr = get_mcp_client_manager()
    orig_strict = system_settings_manager.is_strict_privacy_mode_enabled()
    channel_id = "C_HITL_PII"
    thread_ts = "88888.0001"
    thread_state_manager.clear_state(channel_id, thread_ts)
    thread_state_manager.set_remaining_agents(channel_id, thread_ts, [])
    thread_skills_manager.grant_skills(channel_id, thread_ts, ["db_skill_pii_e2e"])

    test_skill = SkillDefinition(
        name="db_skill_pii_e2e",
        display_name="DB PII",
        description="desc",
        tools=["mcp__internal_db__insert_record"],
        requires_human_ITL=True,
    )
    global_skill_registry._skills["db_skill_pii_e2e"] = test_skill

    mock_session = MagicMock()
    mock_session.initialize = AsyncMock()
    mock_session.call_tool = AsyncMock(
        return_value=DummyCallToolResult("Updated user alice.orig@corp.com and notified bob.new@vendor.com")
    )

    @asynccontextmanager
    async def pii_transport_factory(server_name, server_cfg):
        yield mock_session

    global_mgr.configure_transport_factory(pii_transport_factory)

    posted_msgs: list[str] = []

    async def capture_post(cid, tts, text, username, icon_url=None, blocks=None):
        posted_msgs.append(text)
        return {"ok": True, "ts": "88888.0002"}

    async def mock_followup_llm(*args, **kwargs):
        return MagicMock(
            choices=[MagicMock(message=MagicMock(content="Processed <EMAIL_0> and notified <EMAIL_1>.", tool_calls=None))],
            usage=None,
        )

    configure_token("xoxb-your-bot-user-oauth-token")
    configure_post_agent_response(capture_post)
    configure_llm_completion_fn(mock_followup_llm)
    system_settings_manager.set_strict_privacy_mode_enabled(True, save=False)
    try:
        ctx = TurnContext(channel_id=channel_id, thread_ts=thread_ts, raw_text="Insert <EMAIL_0>")
        ctx.agent_key = "agent-10001"
        ctx.agent_cfg = {"model": "test-model", "username": "Visionary", "skills": ["db_skill_pii_e2e"]}
        ctx.turn_token_mapping = {"<EMAIL_0>": "alice.orig@corp.com"}
        ctx.messages = [{"role": "user", "content": "Insert <EMAIL_0>"}]

        tc = MagicMock()
        tc.id = "call_hitl_pii"
        tc.function.name = "mcp__internal_db__insert_record"
        tc.function.arguments = json.dumps({"email": "alice.orig@corp.com"})

        first_msg = MagicMock(content="Waiting for approval.", tool_calls=[tc])
        first_msg.model_dump.return_value = {"role": "assistant", "content": ""}

        pause_ack = MagicMock(
            choices=[MagicMock(message=MagicMock(content="Approval requested.", tool_calls=None))],
            usage=None,
        )
        pipeline = TurnPipeline(
            skill_registry=global_skill_registry,
            llm_executor=AsyncMock(return_value=pause_ack),
            slack_poster=AsyncMock(return_value={"ok": True, "ts": "88888.0001"}),
        )
        await pipeline.execution_stage.execute_tool_loop(
            ctx,
            active_tools=[],
            agent_skills=["db_skill_pii_e2e"],
            expired_thread_skills=[],
            thread_ttl_hours=1.0,
            initial_response_message=first_msg,
        )
        assert ctx.output.waiting_hitl is True
        assert ctx.output.halted is True

        chk = turn_checkpoint_store.get_checkpoint(ctx.output.checkpoint_id)
        assert chk is not None and chk.hitl_request_id

        # Verify SQLite hitl_approvals stores masked <EMAIL_0>, NOT plaintext alice.orig@corp.com
        with get_connection() as conn:
            row = conn.execute(
                "SELECT fn_args FROM hitl_approvals WHERE request_id = ?;",
                (chk.hitl_request_id,),
            ).fetchone()
            assert row is not None
            assert "<EMAIL_0>" in row["fn_args"]
            assert "alice.orig@corp.com" not in row["fn_args"]

        approved_req = approve_hitl_request(chk.hitl_request_id)
        assert approved_req is not None
        assert approved_req.fn_args["email"] == "<EMAIL_0>"

        # Execute approved tool & resume through TurnPipeline
        await _execute_and_resume_hitl_approval(approved_req, "U_ADMIN")
        active_task = thread_state_manager.get_active_task(channel_id, thread_ts)
        if active_task is not None:
            await active_task

        # Both Alice and Bob must be unmasked cleanly without <EMAIL_0> index collision
        combined_posted = "\n".join(posted_msgs)
        assert "alice.orig@corp.com" in combined_posted
        assert "bob.new@vendor.com" in combined_posted
    finally:
        global_skill_registry._skills.pop("db_skill_pii_e2e", None)
        global_mgr.reset_transport_factory()
        system_settings_manager.set_strict_privacy_mode_enabled(orig_strict, save=False)
        reset_llm_completion_fn()
        reset_post_agent_response()
        reset_token()
