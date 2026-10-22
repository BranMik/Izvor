import pytest
from unittest.mock import MagicMock

from services.rag.pii_masker import PIIMasker
from core.llm_utils import execute_llm_with_retry


def test_multi_message_and_tool_call_pii_masking_no_collision():
    masker = PIIMasker()
    msg1 = "Hello, my email is alice@example.com."
    masked1, mapping1 = masker.mask(msg1)

    assert "<EMAIL_0>" in masked1
    assert mapping1["<EMAIL_0>"] == "alice@example.com"

    # Second message with identical email should reuse <EMAIL_0>
    msg2 = "Just confirming alice@example.com again."
    masked2, mapping2 = masker.mask(msg2, existing_mapping=mapping1)
    assert "<EMAIL_0>" in masked2
    assert mapping2["<EMAIL_0>"] == "alice@example.com"
    assert "<EMAIL_1>" not in mapping2

    # Third message with a new email should assign <EMAIL_1> without collision
    msg3 = "Contact bob@company.org instead."
    masked3, mapping3 = masker.mask(msg3, existing_mapping=mapping2)
    assert "<EMAIL_1>" in masked3
    assert mapping3["<EMAIL_1>"] == "bob@company.org"
    assert mapping3["<EMAIL_0>"] == "alice@example.com"


@pytest.mark.anyio
async def test_execute_llm_with_retry_masks_tool_calls_and_preserves_turn_mapping(monkeypatch):
    monkeypatch.setattr("orchestration.system_settings.is_strict_privacy_mode_enabled", lambda: True)

    turn_mapping = {}

    kwargs = {
        "messages": [
            {"role": "user", "content": "Contact alice@example.com"},
            {
                "role": "assistant",
                "content": "Calling tool",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "send_email",
                            "arguments": '{"to": "alice@example.com", "cc": "charlie@test.com"}',
                        },
                    }
                ],
            },
        ]
    }

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "Sent message to <EMAIL_0> and <EMAIL_1>"
    mock_tc = MagicMock()
    mock_tc.function.arguments = '{"recipient": "<EMAIL_0>"}'
    mock_choice.message.tool_calls = [mock_tc]
    mock_response.choices = [mock_choice]

    async def mock_litellm(**call_kwargs):
        # Verify prompt messages sent to LLM were masked
        messages = call_kwargs.get("messages", [])
        assert "<EMAIL_0>" in messages[0]["content"]
        assert "alice@example.com" not in messages[0]["content"]

        tc_args = messages[1]["tool_calls"][0]["function"]["arguments"]
        assert "<EMAIL_0>" in tc_args
        assert "<EMAIL_1>" in tc_args
        assert "alice@example.com" not in tc_args
        assert "charlie@test.com" not in tc_args
        return mock_response

    res = await execute_llm_with_retry(
        func=mock_litellm,
        kwargs=kwargs,
        agent_key="test_agent",
        thread_key=("C123", "T123"),
        turn_token_mapping=turn_mapping,
    )

    assert res is not None
    # Verify unmasking worked
    assert "alice@example.com" in res.choices[0].message.content
    assert "charlie@test.com" in res.choices[0].message.content
    assert "alice@example.com" in res.choices[0].message.tool_calls[0].function.arguments
    # Verify turn_mapping retained tokens
    assert "<EMAIL_0>" in turn_mapping
    assert turn_mapping["<EMAIL_0>"] == "alice@example.com"
    assert "<EMAIL_1>" in turn_mapping
    assert turn_mapping["<EMAIL_1>"] == "charlie@test.com"
