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
        token_mapping={"<PERSON_0>": "Alice Smith"},
        accumulated_history=[{"speaker": "User", "text": "Execute"}],
        prompt_tokens=120,
        completion_tokens=30,
        hitl_request_id="hitl_req_999",
        my_generation=3,
    )

    saved_id = checkpoint_store.save_checkpoint(chk)
    assert saved_id == "chk_test_123"
    assert checkpoint_store.has_active_checkpoint("C_TEST", "1700000001.000100") is True

    loaded = checkpoint_store.get_checkpoint("chk_test_123")
    assert loaded is not None
    assert loaded.session_id == "C_TEST:1700000001.000100"
    assert loaded.agent_key == "agent-10002"
    assert loaded.pause_reason == "hitl"
    assert loaded.hitl_request_id == "hitl_req_999"
    assert loaded.token_mapping == {"<PERSON_0>": "Alice Smith"}
    assert loaded.my_generation == 3
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


def test_turn_checkpoint_claim_for_resume_atomic_and_prompt_redaction(checkpoint_store):
    """TKT-1201 & TKT-1210: Verify prompt persistence/redaction, CAS claim_for_resume, expiry rejection, and mark_failed_resume."""
    chk = TurnCheckpoint(
        checkpoint_id="chk_claim_01",
        session_id="C_CLAIM:1700000009.0001",
        channel_id="C_CLAIM",
        thread_ts="1700000009.0001",
        agent_key="agent-10001",
        pause_reason="circuit_breaker",
        prompt="Analyze architectural bottlenecks",
        messages=[{"role": "user", "content": "Analyze"}],
        status="ACTIVE",
        expires_at=time.time() + 1000.0,
    )
    checkpoint_store.save_checkpoint(chk)

    loaded = checkpoint_store.get_checkpoint("chk_claim_01")
    assert loaded is not None
    assert loaded.prompt == "Analyze architectural bottlenecks"

    # First CAS claim succeeds -> status becomes RESUMING; second claim returns False
    assert checkpoint_store.claim_for_resume("chk_claim_01") is True
    assert checkpoint_store.claim_for_resume("chk_claim_01") is False
    mid = checkpoint_store.get_checkpoint("chk_claim_01")
    assert mid is not None and mid.status == "RESUMING"

    # mark_resumed succeeds from RESUMING and redacts prompt + payloads
    assert checkpoint_store.mark_resumed("chk_claim_01") is True
    assert checkpoint_store.mark_resumed("chk_claim_01") is False
    done = checkpoint_store.get_checkpoint("chk_claim_01")
    assert done is not None and done.status == "RESUMED" and done.prompt == ""

    # Expired checkpoint cannot be claimed
    expired = TurnCheckpoint(
        checkpoint_id="chk_claim_exp",
        session_id="C_CLAIM:1700000009.0002",
        channel_id="C_CLAIM",
        thread_ts="1700000009.0002",
        agent_key="agent-10001",
        pause_reason="hitl",
        prompt="Expired",
        expires_at=time.time() - 5.0,
    )
    checkpoint_store.save_checkpoint(expired)
    assert checkpoint_store.claim_for_resume("chk_claim_exp") is False

    # Failed resume moves RESUMING -> ABANDONED and redacts prompt
    failed = TurnCheckpoint(
        checkpoint_id="chk_claim_fail",
        session_id="C_CLAIM:1700000009.0003",
        channel_id="C_CLAIM",
        thread_ts="1700000009.0003",
        agent_key="agent-10001",
        pause_reason="hitl",
        prompt="Confidential secret prompt",
        expires_at=time.time() + 500.0,
    )
    checkpoint_store.save_checkpoint(failed)
    assert checkpoint_store.claim_for_resume("chk_claim_fail") is True
    assert checkpoint_store.mark_failed_resume("chk_claim_fail") is True
    abandoned = checkpoint_store.get_checkpoint("chk_claim_fail")
    assert abandoned is not None and abandoned.status == "ABANDONED" and abandoned.prompt == ""

    # release_claim restores RESUMING -> ACTIVE without redacting payload; abandon_thread_checkpoints bulk-abandons
    rel = TurnCheckpoint(
        checkpoint_id="chk_rel_01",
        session_id="C_REL:1700000010.0001",
        channel_id="C_REL",
        thread_ts="1700000010.0001",
        agent_key="agent-10002",
        pause_reason="hitl",
        prompt="Keep me during release",
        my_generation=2,
        expires_at=time.time() + 500.0,
    )
    checkpoint_store.save_checkpoint(rel)
    assert checkpoint_store.claim_for_resume("chk_rel_01") is True
    assert checkpoint_store.release_claim("chk_rel_01") is True
    re_loaded = checkpoint_store.get_checkpoint("chk_rel_01")
    assert re_loaded is not None and re_loaded.status == "ACTIVE" and re_loaded.prompt == "Keep me during release"
    assert checkpoint_store.has_active_checkpoint("C_REL", "1700000010.0001") is True
    abandoned_count = checkpoint_store.abandon_thread_checkpoints("C_REL", "1700000010.0001")
    assert abandoned_count >= 1
    assert checkpoint_store.has_active_checkpoint("C_REL", "1700000010.0001") is False
