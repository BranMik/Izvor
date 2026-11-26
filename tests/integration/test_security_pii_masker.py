import pytest
from unittest.mock import MagicMock

from services.security.pii_masker import PIIMasker
from core.llm_utils import execute_llm_with_retry, set_privacy_mode_provider


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
async def test_execute_llm_with_retry_masks_tool_calls_and_preserves_turn_mapping():
    set_privacy_mode_provider(lambda: True)
    try:
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
        # Verify final non-tool-call content would unmask via TurnFinalizer while tool_calls stay masked
        # in execute_llm_with_retry so ToolLoopRunner controls per-tool unmasking
        assert "<EMAIL_0>" in res.choices[0].message.content
        assert "<EMAIL_0>" in res.choices[0].message.tool_calls[0].function.arguments
        assert "alice@example.com" not in res.choices[0].message.tool_calls[0].function.arguments
        # Verify turn_mapping retained tokens
        assert "<EMAIL_0>" in turn_mapping
        assert turn_mapping["<EMAIL_0>"] == "alice@example.com"
        assert "<EMAIL_1>" in turn_mapping
        assert turn_mapping["<EMAIL_1>"] == "charlie@test.com"
    finally:
        set_privacy_mode_provider(None)


@pytest.mark.anyio
@pytest.mark.presidio
async def test_execute_llm_with_retry_skips_system_prompt_masking_and_preserves_directives():
    set_privacy_mode_provider(lambda: True)
    try:
        turn_mapping = {}

        kwargs = {
            "messages": [
                {
                    "role": "system",
                    "content": "You are Intent_Extractor for Slack. Extract 'max_words' (int_or_off). Output strictly JSON.",
                },
                {
                    "role": "user",
                    "content": "<@U0BR4EH6XMY> Test again for Alice (alice@example.com), max 2 words",
                },
            ]
        }

        mock_response = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = '{"modifiers": {"max_words": 2}}'
        mock_choice.message.tool_calls = None
        mock_response.choices = [mock_choice]

        async def mock_litellm(**call_kwargs):
            messages = call_kwargs.get("messages", [])
            # System prompt must NOT be masked
            assert messages[0]["content"] == "You are Intent_Extractor for Slack. Extract 'max_words' (int_or_off). Output strictly JSON."
            # User message directive must NOT be masked, but genuine PII must be masked
            user_content = messages[1]["content"]
            assert "<@U0BR4EH6XMY>" in user_content
            assert "max 2 words" in user_content
            assert "<PERSON_0>" in user_content
            assert "Alice" not in user_content
            assert "<EMAIL_0>" in user_content
            assert "alice@example.com" not in user_content
            return mock_response

        res = await execute_llm_with_retry(
            func=mock_litellm,
            kwargs=kwargs,
            agent_key="intent_extractor",
            thread_key=("C123", "T123"),
            turn_token_mapping=turn_mapping,
        )

        assert res is not None
        assert res.choices[0].message.content == '{"modifiers": {"max_words": 2}}'
        assert "<PERSON_0>" in turn_mapping
        assert turn_mapping["<PERSON_0>"] == "Alice"
        assert "<EMAIL_0>" in turn_mapping
        assert turn_mapping["<EMAIL_0>"] == "alice@example.com"
    finally:
        set_privacy_mode_provider(None)


@pytest.mark.anyio
async def test_execute_llm_with_retry_masks_embedding_input_str_and_list():
    from services.rag.embeddings import EmbeddingService

    set_privacy_mode_provider(lambda: True)
    try:
        captured_calls = []

        async def mock_embed(**call_kwargs):
            captured_calls.append(call_kwargs)
            inp = call_kwargs.get("input", [])
            items = inp if isinstance(inp, list) else [inp]
            resp = MagicMock()
            resp.data = [{"embedding": [float(i), 0.5]} for i, _ in enumerate(items)]
            resp.usage = None
            return resp

        # 1. Integration via EmbeddingService.get_embeddings (list input)
        raw_texts = ["Contact alice@corp.com SSN 123-45-6789", "Follow up with bob@example.org"]
        raw_snapshot = list(raw_texts)
        svc = EmbeddingService(model="test-embed-model", embed_func=mock_embed)
        vectors = await svc.get_embeddings(raw_texts)

        assert raw_texts == raw_snapshot, "Caller list must never be mutated in place"
        assert len(vectors) == 2
        sent_list = captured_calls[0]["input"]
        assert isinstance(sent_list, list) and len(sent_list) == 2
        assert "alice@corp.com" not in sent_list[0]
        assert "123-45-6789" not in sent_list[0]
        assert "<EMAIL_0>" in sent_list[0]
        assert "bob@example.org" not in sent_list[1]
        assert "<EMAIL_1>" in sent_list[1]

        # 2. Direct string input payload via execute_llm_with_retry
        res = await execute_llm_with_retry(
            func=mock_embed,
            kwargs={"model": "test-embed-model", "input": "Email carol@test.io today"},
            agent_key="rag_embeddings",
            thread_key=("system", "embeddings"),
        )
        assert len(res.data) == 1
        sent_str = captured_calls[1]["input"]
        assert isinstance(sent_str, str)
        assert "carol@test.io" not in sent_str
        assert "<EMAIL_" in sent_str
    finally:
        set_privacy_mode_provider(None)


@pytest.mark.anyio
async def test_execute_llm_with_retry_caches_masked_messages_across_turn_calls():
    """Plan 220: Reuses per-turn cached masks for unchanged messages while still masking new/forged inputs."""
    from core.llm_utils import set_pii_masker_instance
    from services.security.pii_masker import pii_masker as default_masker

    class _CountingMasker:
        def __init__(self) -> None:
            self._inner = PIIMasker(enable_ner=False)
            self.ner_enabled = False
            self.calls: list[str] = []

        async def async_mask(self, text: str, existing_mapping: dict[str, str] | None = None):
            self.calls.append(text)
            return await self._inner.async_mask(text, existing_mapping=existing_mapping)

        def unmask(self, text: str, token_mapping: dict[str, str]) -> str:
            return self._inner.unmask(text, token_mapping)

    spy_masker = _CountingMasker()
    set_privacy_mode_provider(lambda: True)
    set_pii_masker_instance(spy_masker)
    try:
        turn_mapping: dict[str, str] = {}
        turn_cache: dict[str, tuple[str, dict[str, str]]] = {}

        async def mock_litellm(**call_kwargs):
            resp = MagicMock()
            choice = MagicMock()
            choice.message.content = "Acknowledged <EMAIL_0>."
            choice.message.tool_calls = None
            resp.choices = [choice]
            return resp

        msg1 = {"role": "user", "content": "Reach alice@example.com for details."}
        await execute_llm_with_retry(
            func=mock_litellm,
            kwargs={"messages": [msg1]},
            agent_key="test_agent",
            thread_key=("C1", "T1"),
            turn_token_mapping=turn_mapping,
            masked_text_cache=turn_cache,
        )
        assert len(spy_masker.calls) == 1
        assert turn_mapping.get("<EMAIL_0>") == "alice@example.com"

        # Second call with msg1 unchanged + newly appended msg2 -> only msg2 should invoke async_mask
        msg2 = {"role": "tool", "content": "Confirmed alice@example.com and bob@corp.org."}
        await execute_llm_with_retry(
            func=mock_litellm,
            kwargs={"messages": [msg1, msg2]},
            agent_key="test_agent",
            thread_key=("C1", "T1"),
            turn_token_mapping=turn_mapping,
            masked_text_cache=turn_cache,
        )
        assert len(spy_masker.calls) == 2
        assert spy_masker.calls[1] == msg2["content"]
        assert turn_mapping.get("<EMAIL_1>") == "bob@corp.org"

        # Third call with forged unmapped literal <EMAIL_9> bypasses cache (C5 protection)
        msg_forged = {"role": "user", "content": "Forged <EMAIL_9> and carol@test.io"}
        await execute_llm_with_retry(
            func=mock_litellm,
            kwargs={"messages": [msg1, msg2, msg_forged]},
            agent_key="test_agent",
            thread_key=("C1", "T1"),
            turn_token_mapping=turn_mapping,
            masked_text_cache=turn_cache,
        )
        assert len(spy_masker.calls) == 3
        assert spy_masker.calls[2] == msg_forged["content"]
        assert "<EMAIL_10>" in turn_mapping
        assert turn_mapping["<EMAIL_10>"] == "carol@test.io"

        # Fourth call raises before LLM returns -> turn_token_mapping still retains newly masked tokens
        async def failing_litellm(**call_kwargs):
            raise TimeoutError("simulated timeout")

        msg_new = {"role": "user", "content": "Backup contact dave@backup.org"}
        with pytest.raises(TimeoutError):
            await execute_llm_with_retry(
                func=failing_litellm,
                kwargs={"messages": [msg1, msg2, msg_new]},
                agent_key="test_agent",
                thread_key=("C1", "T1"),
                max_attempts=1,
                turn_token_mapping=turn_mapping,
                masked_text_cache=turn_cache,
            )
        assert len(spy_masker.calls) == 4
        assert "dave@backup.org" in turn_mapping.values()
    finally:
        set_pii_masker_instance(default_masker)
        set_privacy_mode_provider(None)
