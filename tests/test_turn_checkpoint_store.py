"""
Unit tests for TurnCheckpointStore service.
Verifies ACID persistence, retrieval by various keys, status transitions, and expiration pruning.
"""

import time
import pytest
from core.domain import TurnCheckpoint
from services.turn_checkpoint_store import TurnCheckpointStore


@pytest.fixture
def checkpoint_store():
    return TurnCheckpointStore()


def test_turn_checkpoint_save_and_retrieve(checkpoint_store):
    chk = TurnCheckpoint(
        checkpoint_id="chk_test_123",
        session_id="C_TEST:1700000001.000100",
        channel_id="C_TEST",
        thread_ts="1700000001.000100",
        agent_key="agent-10002",
        pause_reason="hitl",
        messages=[{"role": "user", "content": "Execute critical operation"}],
        pending_tool_call={"id": "call_1", "function": {"name": "restricted_tool", "arguments": "{}"}},
        active_tools=[{"type": "function", "function": {"name": "restricted_tool"}}],
        skills_used=["admin_tool"],
        token_mapping={"prompt": 120, "completion": 30},
        accumulated_history=[{"speaker": "User", "text": "Execute"}],
        prompt_tokens=120,
        completion_tokens=30,
        hitl_request_id="hitl_req_999",
    )

    saved_id = checkpoint_store.save_checkpoint(chk)
    assert saved_id == "chk_test_123"

    loaded = checkpoint_store.get_checkpoint("chk_test_123")
    assert loaded is not None
    assert loaded.session_id == "C_TEST:1700000001.000100"
    assert loaded.agent_key == "agent-10002"
    assert loaded.pause_reason == "hitl"
    assert loaded.hitl_request_id == "hitl_req_999"
    assert len(loaded.messages) == 1
    assert loaded.pending_tool_call["id"] == "call_1"
    assert loaded.prompt_tokens == 120
    assert loaded.completion_tokens == 30
    assert loaded.status in ("ACTIVE", "PENDING")

    # Retrieve by HITL request id
    by_hitl = checkpoint_store.get_checkpoint_by_hitl_request("hitl_req_999")
    assert by_hitl is not None
    assert by_hitl.checkpoint_id == "chk_test_123"

    # Retrieve latest for thread
    latest = checkpoint_store.get_latest_checkpoint_for_thread("C_TEST", "1700000001.000100")
    assert latest is not None
    assert latest.checkpoint_id == "chk_test_123"

    # Status transition to RESUMED
    assert checkpoint_store.mark_resumed("chk_test_123") is True
    resumed = checkpoint_store.get_checkpoint("chk_test_123")
    assert resumed.status == "RESUMED"

    # HITL lookup should only return PENDING
    assert checkpoint_store.get_checkpoint_by_hitl_request("hitl_req_999") is None


def test_turn_checkpoint_expiration_pruning(checkpoint_store):
    expired_chk = TurnCheckpoint(
        checkpoint_id="chk_expired_01",
        session_id="C_TEST:1700000002.000200",
        channel_id="C_TEST",
        thread_ts="1700000002.000200",
        agent_key="agent-10001",
        pause_reason="circuit_breaker",
        expires_at=time.time() - 100.0,  # Already expired
    )
    checkpoint_store.save_checkpoint(expired_chk)

    active_chk = TurnCheckpoint(
        checkpoint_id="chk_active_01",
        session_id="C_TEST:1700000003.000300",
        channel_id="C_TEST",
        thread_ts="1700000003.000300",
        agent_key="agent-10001",
        pause_reason="circuit_breaker",
        expires_at=time.time() + 1000.0,
    )
    checkpoint_store.save_checkpoint(active_chk)

    pruned = checkpoint_store.prune_expired()
    assert pruned >= 1

    assert checkpoint_store.get_checkpoint("chk_expired_01") is None
    assert checkpoint_store.get_checkpoint("chk_active_01") is not None
