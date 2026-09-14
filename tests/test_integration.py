import pytest
from fastapi.testclient import TestClient

from main import PROCESSED_EVENTS, app
from orchestration.agent_settings import (
    ENABLED_DEBATE_AGENTS,
    LOCKED_AGENTS,
    get_circuit_breaker_threshold,
    is_circuit_breaker_enabled,
    reset_enabled_agents,
    set_circuit_breaker_enabled,
    set_circuit_breaker_threshold,
)
from orchestration.thread_state import thread_state_manager

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
    monkeypatch.setattr("orchestration.agent_router.post_agent_response", mock_post_agent_response)
    monkeypatch.setattr("orchestration.agent_router.fetch_thread_history", mock_fetch_thread_history)
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

    # 2. Bot message with nomination (should also be skipped; internal backend loop handles chaining)
    payload_with_nom = {
        "type": "event_callback",
        "event": {
            "type": "message",
            "subtype": "bot_message",
            "channel": "C_TEST_BOT",
            "ts": "123456789.0003",
            "text": "I nominate [Innovator] to speak next.",
        },
    }
    response2 = client.post("/slack/events", json=payload_with_nom)
    assert response2.status_code == 200
    # Should NOT be in cache (skipped by webhook because backend loops internally)
    assert ("C_TEST_BOT", "123456789.0003") not in PROCESSED_EVENTS


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
            "text": "[TechLead] Please analyze this schema.",
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
    thread_state_manager.reset_all()

    # Pre-populate remaining agents for thread to verify they are cleared on stop
    thread_key = ("C_TEST_STOP", "123456789.0008")
    thread_state_manager.remaining_agents_map[thread_key] = ["TechLead", "Innovator"]

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
    assert thread_key in thread_state_manager.stop_requested_threads
    assert thread_key not in thread_state_manager.remaining_agents_map
    # Verify process_and_respond was NOT called for stop command
    mock_process.assert_not_called()

    # 2. User sends stop via persona name mention in regular message
    thread_state_manager.stop_requested_threads.clear()
    thread_key_2 = ("C_TEST_STOP", "123456789.0009")
    thread_state_manager.remaining_agents_map[thread_key_2] = ["ROIStrategist"]

    payload_stop_persona = {
        "type": "event_callback",
        "event": {
            "type": "message",
            "channel": "C_TEST_STOP",
            "ts": "123456789.0009",
            "thread_ts": "123456789.0009",
            "text": "@CaiLogics Test /stop",
        },
    }

    response2 = client.post("/slack/events", json=payload_stop_persona)
    assert response2.status_code == 200
    assert thread_key_2 in PROCESSED_EVENTS
    assert thread_key_2 in thread_state_manager.stop_requested_threads
    assert thread_key_2 not in thread_state_manager.remaining_agents_map
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

    # 1. Turn OFF TechLead via radio button
    payload_off = {
        "type": "block_actions",
        "user": {"id": "U_TEST_USER_2"},
        "actions": [
            {
                "action_id": "toggle_agent_radio:TechLead",
                "selected_option": {
                    "value": "TechLead:off",
                    "text": {"type": "plain_text", "text": "🔴 OFF"},
                },
            }
        ],
    }

    response = client.post("/slack/actions", json=payload_off)
    assert response.status_code == 200
    assert "TechLead" not in ENABLED_DEBATE_AGENTS
    mock_publish.assert_called_with(user_id="U_TEST_USER_2")

    # 2. Turn ON TechLead via radio button
    payload_on = {
        "type": "block_actions",
        "user": {"id": "U_TEST_USER_2"},
        "actions": [
            {
                "action_id": "toggle_agent_radio:TechLead",
                "selected_option": {
                    "value": "TechLead:on",
                    "text": {"type": "plain_text", "text": "🟢 ON"},
                },
            }
        ],
    }

    response_on = client.post("/slack/interactivity", json=payload_on)
    assert response_on.status_code == 200
    assert "TechLead" in ENABLED_DEBATE_AGENTS

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
    assert len(ENABLED_DEBATE_AGENTS) == 17


def test_user_new_query_interrupts_ongoing_discussion_webhook(monkeypatch):
    PROCESSED_EVENTS.clear()
    thread_key = ("C_TEST_INTERRUPT_WEBHOOK", "123456789.2001")
    
    # 1. Simulate an ongoing discussion with remaining agents
    thread_state_manager.remaining_agents_map[thread_key] = ["TechLead", "Innovator"]
    initial_gen = thread_state_manager.thread_generations.get(thread_key, 0)
    assert thread_state_manager.is_discussion_active("C_TEST_INTERRUPT_WEBHOOK", "123456789.2001") is True

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
    assert thread_key not in thread_state_manager.remaining_agents_map
    assert thread_state_manager.thread_generations[thread_key] == initial_gen + 1

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
    thread_state_manager.remaining_agents_map[thread_key] = ["Architect"]
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
    assert thread_key not in thread_state_manager.remaining_agents_map
    assert mock_process.call_count == 1

    # 2. Message with agent bracket tag [Visionary]
    thread_state_manager.remaining_agents_map[thread_key] = ["Architect"]
    payload_tag = {
        "type": "event_callback",
        "event": {
            "type": "message",
            "channel": "C_TEST_INTERRUPT_SYNTAX",
            "ts": "123456789.3003",
            "thread_ts": "123456789.3001",
            "text": "[Visionary] Start fresh with this",
        },
    }

    response2 = client.post("/slack/events", json=payload_tag)
    assert response2.status_code == 200
    assert thread_key not in thread_state_manager.remaining_agents_map
    assert mock_process.call_count == 2


def test_webhook_with_history_limit_dispatches_properly(monkeypatch):
    PROCESSED_EVENTS.clear()

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


def test_slack_interactivity_circuit_breaker_toggle():
    set_circuit_breaker_enabled(False)
    assert is_circuit_breaker_enabled() is False

    payload_on = {
        "type": "block_actions",
        "user": {"id": "U_CB_TEST"},
        "actions": [
            {
                "action_id": "toggle_circuit_breaker_radio",
                "selected_option": {"value": "cb:on"},
            }
        ],
    }
    response_on = client.post("/slack/actions", json=payload_on)
    assert response_on.status_code == 200
    assert is_circuit_breaker_enabled() is True

    payload_off = {
        "type": "block_actions",
        "user": {"id": "U_CB_TEST"},
        "actions": [
            {
                "action_id": "toggle_circuit_breaker_radio",
                "selected_option": {"value": "cb:off"},
            }
        ],
    }
    response_off = client.post("/slack/actions", json=payload_off)
    assert response_off.status_code == 200
    assert is_circuit_breaker_enabled() is False
    assert get_circuit_breaker_threshold() == 0.0


def test_slack_interactivity_circuit_breaker_threshold_input():
    set_circuit_breaker_threshold(1.00)

    payload_input = {
        "type": "block_actions",
        "user": {"id": "U_CB_TEST"},
        "actions": [
            {
                "action_id": "set_circuit_breaker_threshold_input",
                "value": "2.50",
            }
        ],
    }
    response = client.post("/slack/actions", json=payload_input)
    assert response.status_code == 200
    assert get_circuit_breaker_threshold() == 2.50


def test_slack_interactivity_circuit_breaker_preset_button():
    set_circuit_breaker_threshold(1.00)

    payload_preset = {
        "type": "block_actions",
        "user": {"id": "U_CB_TEST"},
        "actions": [{"action_id": "set_circuit_breaker_preset:5.00"}],
    }
    response = client.post("/slack/actions", json=payload_preset)
    assert response.status_code == 200
    assert get_circuit_breaker_threshold() == 5.00


def test_slack_interactivity_circuit_breaker_save_button():
    set_circuit_breaker_threshold(1.00)

    payload_save = {
        "type": "block_actions",
        "user": {"id": "U_CB_TEST"},
        "actions": [{"action_id": "save_circuit_breaker_threshold_button"}],
        "view": {
            "state": {
                "values": {
                    "circuit_breaker_threshold_block": {
                        "set_circuit_breaker_threshold_input": {
                            "value": "0.75"
                        }
                    }
                }
            }
        },
    }
    response = client.post("/slack/actions", json=payload_save)
    assert response.status_code == 200
    assert get_circuit_breaker_threshold() == 0.75


def test_slack_interactivity_circuit_breaker_save_button_dynamic_block_id():
    set_circuit_breaker_threshold(1.00)

    # Dynamic block ID generated by build_app_home_view
    payload_save = {
        "type": "block_actions",
        "user": {"id": "U_CB_TEST"},
        "actions": [{"action_id": "save_circuit_breaker_threshold_button"}],
        "view": {
            "state": {
                "values": {
                    "cb_threshold_block_1.00": {
                        "set_circuit_breaker_threshold_input": {
                            "value": "3.25"
                        }
                    }
                }
            }
        },
    }
    response = client.post("/slack/actions", json=payload_save)
    assert response.status_code == 200
    assert get_circuit_breaker_threshold() == 3.25


def test_slack_interactivity_preset_updates_view():
    from services.slack_client import build_app_home_view

    set_circuit_breaker_threshold(1.00)

    # 1. Click $0.50 preset
    payload_preset = {
        "type": "block_actions",
        "user": {"id": "U_CB_TEST"},
        "actions": [{"action_id": "set_circuit_breaker_preset:0.50"}],
    }
    response = client.post("/slack/actions", json=payload_preset)
    assert response.status_code == 200
    assert get_circuit_breaker_threshold() == 0.50

    # 2. Verify build_app_home_view immediately reflects the new threshold and dynamic block_id
    view = build_app_home_view()
    threshold_blocks = [
        b for b in view["blocks"]
        if b.get("element", {}).get("action_id") == "set_circuit_breaker_threshold_input"
    ]
    assert len(threshold_blocks) == 1
    input_block = threshold_blocks[0]
    assert input_block["block_id"] == "cb_threshold_block_0.50"
    assert input_block["element"]["initial_value"] == "0.50"


def test_slack_interactivity_url_encoded_payload():
    import json
    import urllib.parse

    set_circuit_breaker_enabled(False)
    set_circuit_breaker_threshold(1.00)

    raw_payload = {
        "type": "block_actions",
        "user": {"id": "U_CB_URL_ENCODED"},
        "actions": [
            {
                "action_id": "toggle_circuit_breaker_radio",
                "selected_option": {"value": "cb:on"},
            }
        ],
    }
    encoded_body = urllib.parse.urlencode({"payload": json.dumps(raw_payload)})
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    response = client.post("/slack/actions", content=encoded_body, headers=headers)
    assert response.status_code == 200
    assert is_circuit_breaker_enabled() is True


def test_circuit_breaker_off_resets_amount_in_app_home_view():
    from services.slack_client import build_app_home_view

    set_circuit_breaker_enabled(True)
    set_circuit_breaker_threshold(3.50)
    assert get_circuit_breaker_threshold() == 3.50

    set_circuit_breaker_enabled(False)
    assert get_circuit_breaker_threshold() == 0.0

    view = build_app_home_view()
    threshold_blocks = [
        b for b in view["blocks"]
        if b.get("element", {}).get("action_id") == "set_circuit_breaker_threshold_input"
    ]
    assert len(threshold_blocks) == 1
    input_block = threshold_blocks[0]
    assert input_block["block_id"] == "cb_threshold_block_0.00"
    assert input_block["element"]["initial_value"] == "0.00"


def test_slack_interactivity_zero_threshold_turns_off_circuit_breaker():
    set_circuit_breaker_enabled(True)
    set_circuit_breaker_threshold(2.00)
    assert is_circuit_breaker_enabled() is True

    payload_zero = {
        "type": "block_actions",
        "user": {"id": "U_CB_TEST"},
        "actions": [
            {
                "action_id": "set_circuit_breaker_threshold_input",
                "value": "0",
            }
        ],
    }
    response = client.post("/slack/actions", json=payload_zero)
    assert response.status_code == 200
    assert is_circuit_breaker_enabled() is False
    assert get_circuit_breaker_threshold() == 0.0


def test_slack_interactivity_scribe_toggle():
    from orchestration.agent_settings import is_scribe_enabled, set_scribe_enabled

    try:
        set_scribe_enabled(True)
        assert is_scribe_enabled() is True

        payload_off = {
            "type": "block_actions",
            "user": {"id": "U_SCRIBE_TEST"},
            "actions": [
                {
                    "action_id": "toggle_scribe_radio",
                    "selected_option": {"value": "scribe:off"},
                }
            ],
        }
        response_off = client.post("/slack/actions", json=payload_off)
        assert response_off.status_code == 200
        assert is_scribe_enabled() is False

        payload_on = {
            "type": "block_actions",
            "user": {"id": "U_SCRIBE_TEST"},
            "actions": [
                {
                    "action_id": "toggle_scribe_radio",
                    "selected_option": {"value": "scribe:on"},
                }
            ],
        }
        response_on = client.post("/slack/actions", json=payload_on)
        assert response_on.status_code == 200
        assert is_scribe_enabled() is True
    finally:
        set_scribe_enabled(True)








