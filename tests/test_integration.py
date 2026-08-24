import pytest
from fastapi.testclient import TestClient
from main import app, PROCESSED_EVENTS
from agent_router import (
    ENABLED_DEBATE_AGENTS,
    LOCKED_AGENTS,
    REMAINING_AGENTS_MAP,
    STOP_REQUESTED_THREADS,
    THREAD_GENERATIONS,
    is_discussion_active,
    interrupt_thread_discussion,
    reset_enabled_agents,
)

client = TestClient(app)


async def mock_post_agent_response(*args, **kwargs):
    return {"ok": True}


async def mock_fetch_thread_history(*args, **kwargs):
    return []


from unittest.mock import AsyncMock

@pytest.fixture(autouse=True)
def patch_slack_calls(monkeypatch):
    reset_enabled_agents()
    monkeypatch.setattr("main.process_and_respond", AsyncMock(return_value={"ok": True}))
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
            "text": "I nominate [AnthrOpus_Innovator] to speak next.",
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
            "text": "[MistLar_TechLead] Please analyze this schema.",
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


def test_slack_retry_handling():
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

    # First attempt with retry header (e.g. attempt 0 was lost in network) -> should be processed!
    response1 = client.post(
        "/slack/events", json=payload, headers={"X-Slack-Retry-Num": "1"}
    )
    assert response1.status_code == 200
    assert ("C_TEST_RETRY", "123456789.0007") in PROCESSED_EVENTS

    # Second duplicate retry with same ts -> should be deduplicated
    response2 = client.post(
        "/slack/events", json=payload, headers={"X-Slack-Retry-Num": "2"}
    )
    assert response2.status_code == 200


def test_stop_command_integration(monkeypatch):
    PROCESSED_EVENTS.clear()
    STOP_REQUESTED_THREADS.clear()
    REMAINING_AGENTS_MAP.clear()

    # Pre-populate remaining agents for thread to verify they are cleared on stop
    thread_key = ("C_TEST_STOP", "123456789.0008")
    REMAINING_AGENTS_MAP[thread_key] = ["MistLar_TechLead", "AnthrOpus_Innovator"]

    mock_process = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("main.process_and_respond", mock_process)

    # 1. User sends stop via app_mention
    payload_stop_mention = {
        "type": "event_callback",
        "event": {
            "type": "app_mention",
            "channel": "C_TEST_STOP",
            "ts": "123456789.0008",
            "thread_ts": "123456789.0008",
            "text": "<@U12345> /stop",
        },
    }

    response = client.post("/slack/events", json=payload_stop_mention)
    assert response.status_code == 200
    assert thread_key in PROCESSED_EVENTS
    assert thread_key in STOP_REQUESTED_THREADS
    assert thread_key not in REMAINING_AGENTS_MAP
    # Verify process_and_respond was NOT called for stop command
    mock_process.assert_not_called()

    # 2. User sends stop via persona name mention in regular message
    STOP_REQUESTED_THREADS.clear()
    thread_key_2 = ("C_TEST_STOP", "123456789.0009")
    REMAINING_AGENTS_MAP[thread_key_2] = ["OpAiTerra_ROIStrategist"]

    payload_stop_persona = {
        "type": "event_callback",
        "event": {
            "type": "message",
            "channel": "C_TEST_STOP",
            "ts": "123456789.0009",
            "thread_ts": "123456789.0009",
            "text": "@CaiLogics Test /stop discussion",
        },
    }

    response2 = client.post("/slack/events", json=payload_stop_persona)
    assert response2.status_code == 200
    assert thread_key_2 in PROCESSED_EVENTS
    assert thread_key_2 in STOP_REQUESTED_THREADS
    assert thread_key_2 not in REMAINING_AGENTS_MAP
    mock_process.assert_not_called()


def test_app_home_opened_event(monkeypatch):
    mock_publish = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("main.publish_app_home", mock_publish)

    payload = {
        "type": "event_callback",
        "event": {
            "type": "app_home_opened",
            "user": "U_TEST_USER_1",
            "tab": "home",
        },
    }

    response = client.post("/slack/events", json=payload)
    assert response.status_code == 200
    mock_publish.assert_called_once_with(user_id="U_TEST_USER_1")


def test_slack_interactivity_radio_toggle_action(monkeypatch):
    reset_enabled_agents()
    mock_publish = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("main.publish_app_home", mock_publish)

    # 1. Turn OFF MistLar_TechLead via radio button
    payload_off = {
        "type": "block_actions",
        "user": {"id": "U_TEST_USER_2"},
        "actions": [
            {
                "action_id": "toggle_agent_radio:MistLar_TechLead",
                "selected_option": {
                    "value": "MistLar_TechLead:off",
                    "text": {"type": "plain_text", "text": "🔴 OFF"},
                },
            }
        ],
    }

    response = client.post("/slack/actions", json=payload_off)
    assert response.status_code == 200
    assert "MistLar_TechLead" not in ENABLED_DEBATE_AGENTS
    mock_publish.assert_called_with(user_id="U_TEST_USER_2")

    # 2. Turn ON MistLar_TechLead via radio button
    payload_on = {
        "type": "block_actions",
        "user": {"id": "U_TEST_USER_2"},
        "actions": [
            {
                "action_id": "toggle_agent_radio:MistLar_TechLead",
                "selected_option": {
                    "value": "MistLar_TechLead:on",
                    "text": {"type": "plain_text", "text": "🟢 ON"},
                },
            }
        ],
    }

    response_on = client.post("/slack/interactivity", json=payload_on)
    assert response_on.status_code == 200
    assert "MistLar_TechLead" in ENABLED_DEBATE_AGENTS

    # 3. Bulk disable optional (only locked agents remain ON)
    payload_bulk_off = {
        "type": "block_actions",
        "user": {"id": "U_TEST_USER_2"},
        "actions": [{"action_id": "bulk_disable_optional"}],
    }

    response_bulk_off = client.post("/slack/actions", json=payload_bulk_off)
    assert response_bulk_off.status_code == 200
    assert ENABLED_DEBATE_AGENTS == LOCKED_AGENTS

    # 4. Bulk enable all
    payload_bulk_on = {
        "type": "block_actions",
        "user": {"id": "U_TEST_USER_2"},
        "actions": [{"action_id": "bulk_enable_all"}],
    }

    response_bulk_on = client.post("/slack/actions", json=payload_bulk_on)
    assert response_bulk_on.status_code == 200
    assert len(ENABLED_DEBATE_AGENTS) == 16


def test_user_new_query_interrupts_ongoing_discussion_webhook(monkeypatch):
    PROCESSED_EVENTS.clear()
    thread_key = ("C_TEST_INTERRUPT_WEBHOOK", "123456789.2001")
    
    # 1. Simulate an ongoing discussion with remaining agents
    REMAINING_AGENTS_MAP[thread_key] = ["MistLar_TechLead", "AnthrOpus_Innovator"]
    initial_gen = THREAD_GENERATIONS.get(thread_key, 0)
    assert is_discussion_active("C_TEST_INTERRUPT_WEBHOOK", "123456789.2001") is True

    mock_process = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("main.process_and_respond", mock_process)

    # 2. User sends new query in the same thread with @app_name
    payload = {
        "type": "event_callback",
        "event": {
            "type": "app_mention",
            "channel": "C_TEST_INTERRUPT_WEBHOOK",
            "ts": "123456789.2002",
            "thread_ts": "123456789.2001",
            "text": "<@U12345> New topic prompt",
        },
    }

    response = client.post("/slack/events", json=payload)
    assert response.status_code == 200
    assert ("C_TEST_INTERRUPT_WEBHOOK", "123456789.2002") in PROCESSED_EVENTS

    # 3. Verify previous discussion state was cleared and generation was bumped
    assert thread_key not in REMAINING_AGENTS_MAP
    assert THREAD_GENERATIONS[thread_key] == initial_gen + 1

    # 4. Verify process_and_respond was dispatched for the new query
    mock_process.assert_called_once()
    call_kwargs = mock_process.call_args.kwargs
    assert call_kwargs["channel_id"] == "C_TEST_INTERRUPT_WEBHOOK"
    assert call_kwargs["thread_ts"] == "123456789.2001"
    assert call_kwargs["raw_text"] == "<@U12345> New topic prompt"
    assert call_kwargs["is_bot_trigger"] is False


def test_user_mention_syntax_variations_webhook_interruption(monkeypatch):
    PROCESSED_EVENTS.clear()
    thread_key = ("C_TEST_INTERRUPT_SYNTAX", "123456789.3001")
    
    # 1. Message with @CaiLogics Hub
    REMAINING_AGENTS_MAP[thread_key] = ["OpAiTerra_Architect"]
    mock_process = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("main.process_and_respond", mock_process)

    payload_hub = {
        "type": "event_callback",
        "event": {
            "type": "message",
            "channel": "C_TEST_INTERRUPT_SYNTAX",
            "ts": "123456789.3002",
            "thread_ts": "123456789.3001",
            "text": "@CaiLogics Hub Analyze this other query",
        },
    }

    response1 = client.post("/slack/events", json=payload_hub)
    assert response1.status_code == 200
    assert thread_key not in REMAINING_AGENTS_MAP
    assert mock_process.call_count == 1

    # 2. Message with agent bracket tag [Gemini_Visionary]
    REMAINING_AGENTS_MAP[thread_key] = ["OpAiTerra_Architect"]
    payload_tag = {
        "type": "event_callback",
        "event": {
            "type": "message",
            "channel": "C_TEST_INTERRUPT_SYNTAX",
            "ts": "123456789.3003",
            "thread_ts": "123456789.3001",
            "text": "[Gemini_Visionary] Start fresh with this",
        },
    }

    response2 = client.post("/slack/events", json=payload_tag)
    assert response2.status_code == 200
    assert thread_key not in REMAINING_AGENTS_MAP
    assert mock_process.call_count == 2


def test_webhook_with_history_limit_dispatches_properly(monkeypatch):
    PROCESSED_EVENTS.clear()
    thread_key = ("C_TEST_HIST_WEBHOOK", "123456789.4001")

    mock_process = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("main.process_and_respond", mock_process)

    payload = {
        "type": "event_callback",
        "event": {
            "type": "app_mention",
            "channel": "C_TEST_HIST_WEBHOOK",
            "ts": "123456789.4002",
            "thread_ts": "123456789.4001",
            "text": "<@U12345> Analyze the previous discussion /history 3",
        },
    }

    response = client.post("/slack/events", json=payload)
    assert response.status_code == 200
    assert mock_process.call_count == 1
    call_kwargs = mock_process.call_args.kwargs
    assert call_kwargs["channel_id"] == "C_TEST_HIST_WEBHOOK"
    assert call_kwargs["thread_ts"] == "123456789.4001"
    assert call_kwargs["raw_text"] == "<@U12345> Analyze the previous discussion /history 3"
    assert call_kwargs["is_bot_trigger"] is False




