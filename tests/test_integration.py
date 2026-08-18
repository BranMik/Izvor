import pytest
from fastapi.testclient import TestClient
from main import app, PROCESSED_EVENTS
from agent_router import REMAINING_AGENTS_MAP

client = TestClient(app)


async def mock_post_agent_response(*args, **kwargs):
    return {"ok": True}


async def mock_fetch_thread_history(*args, **kwargs):
    return []


@pytest.fixture(autouse=True)
def patch_slack_calls(monkeypatch):
    monkeypatch.setattr("agent_router.post_agent_response", mock_post_agent_response)
    monkeypatch.setattr("agent_router.fetch_thread_history", mock_fetch_thread_history)
    monkeypatch.setattr("main.verify_slack_signature", lambda body, headers: True)


def test_slack_events_deduplication():
    # Clear caches
    PROCESSED_EVENTS.clear()

    payload = {
        "type": "event_callback",
        "event": {
            "type": "app_mention",
            "channel": "C_TEST_DEDUP",
            "ts": "123456789.0001",
            "text": "<@U12345> Hello bot!",
        },
    }

    # First request should be accepted and added to cache
    response = client.post("/slack/events", json=payload)
    assert response.status_code == 200
    assert ("C_TEST_DEDUP", "123456789.0001") in PROCESSED_EVENTS

    # Second request with the same channel and ts should be deduplicated
    response_dup = client.post("/slack/events", json=payload)
    assert response_dup.status_code == 200


def test_bot_message_filtering():
    PROCESSED_EVENTS.clear()

    # 1. Bot message without nomination (should be skipped)
    payload_no_nom = {
        "type": "event_callback",
        "event": {
            "type": "message",
            "subtype": "bot_message",
            "channel": "C_TEST_BOT",
            "ts": "123456789.0002",
            "text": "This is a regular summary statement from Architect.",
        },
    }
    response1 = client.post("/slack/events", json=payload_no_nom)
    assert response1.status_code == 200
    # Should NOT be in cache (skipped)
    assert ("C_TEST_BOT", "123456789.0002") not in PROCESSED_EVENTS

    # 2. Bot message with nomination (should be processed)
    payload_with_nom = {
        "type": "event_callback",
        "event": {
            "type": "message",
            "subtype": "bot_message",
            "channel": "C_TEST_BOT",
            "ts": "123456789.0003",
            "text": "I nominate [Mistral_Innovator] to speak next.",
        },
    }
    response2 = client.post("/slack/events", json=payload_with_nom)
    assert response2.status_code == 200
    # Should be in cache (processed)
    assert ("C_TEST_BOT", "123456789.0003") in PROCESSED_EVENTS


def test_user_message_filtering():
    PROCESSED_EVENTS.clear()

    # 1. Plain user message without mention or tag (should be skipped)
    payload_plain = {
        "type": "event_callback",
        "event": {
            "type": "message",
            "channel": "C_TEST_USER",
            "ts": "123456789.0004",
            "text": "Just talking to my team about coding.",
        },
    }
    response1 = client.post("/slack/events", json=payload_plain)
    assert response1.status_code == 200
    assert ("C_TEST_USER", "123456789.0004") not in PROCESSED_EVENTS

    # 2. User message with tag (should be processed)
    payload_tag = {
        "type": "event_callback",
        "event": {
            "type": "message",
            "channel": "C_TEST_USER",
            "ts": "123456789.0005",
            "text": "[Mistral_TechLead] Please analyze this schema.",
        },
    }
    response2 = client.post("/slack/events", json=payload_tag)
    assert response2.status_code == 200
    assert ("C_TEST_USER", "123456789.0005") in PROCESSED_EVENTS

    # 3. User app_mention event (should be processed)
    payload_mention = {
        "type": "event_callback",
        "event": {
            "type": "app_mention",
            "channel": "C_TEST_USER",
            "ts": "123456789.0006",
            "text": "<@U12345> Hello bot!",
        },
    }
    response3 = client.post("/slack/events", json=payload_mention)
    assert response3.status_code == 200
    assert ("C_TEST_USER", "123456789.0006") in PROCESSED_EVENTS


def test_slack_retry_header_ignored():
    PROCESSED_EVENTS.clear()

    payload = {
        "type": "event_callback",
        "event": {
            "type": "app_mention",
            "channel": "C_TEST_RETRY",
            "ts": "123456789.0007",
            "text": "<@U12345> Retry message",
        },
    }

    # Request with retry header should return 200 OK immediately and NOT be added to PROCESSED_EVENTS
    response = client.post(
        "/slack/events", json=payload, headers={"X-Slack-Retry-Num": "1"}
    )
    assert response.status_code == 200
    assert ("C_TEST_RETRY", "123456789.0007") not in PROCESSED_EVENTS
