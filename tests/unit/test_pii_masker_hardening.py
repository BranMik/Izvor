"""
Tests for Prompt File mtime Invalidation and PIIMasker Hardening (TKT-932).
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
import pytest

from prompts.builder import (
    _read_prompt_file,
    get_short_output_agents,
    get_system_agents,
    is_short_output_agent,
    is_system_agent,
)
from services.security.pii_masker import PIIMasker, _STATIC_SENTENCE_START_ALLOWLIST_LOWER


def test_pii_masker_single_pass_unmask_no_recursion():
    """
    Verifies that single-pass regex unmasking avoids token collisions and recursive substitutions.
    Sequential replacement of <A> with <B> would replace <A> -> <B> -> 'resolved',
    whereas single-pass replaces <A> with '<B>' and <B> with 'resolved' without cascaded re-scanning.
    """
    masker = PIIMasker()
    token_mapping = {
        "<TOKEN_A>": "<TOKEN_B>",
        "<TOKEN_B>": "final_secret_value",
    }
    input_text = "Here is <TOKEN_A> and here is <TOKEN_B>."
    unmasked = masker.unmask(input_text, token_mapping)

    # In single-pass replacement, <TOKEN_A> maps directly to '<TOKEN_B>' and <TOKEN_B> maps to 'final_secret_value'
    assert unmasked == "Here is <TOKEN_B> and here is final_secret_value."


def test_pii_masker_preserves_bare_git_commits():
    """
    Verifies that bare 32-64 hex strings (like Git commit SHAs) are not masked into <TOKEN_HASH>,
    while contextual tokens (api_key, hash, token) ARE masked.
    """
    masker = PIIMasker()
    bare_commit = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    text = (
        f"Merged PR in commit {bare_commit} yesterday. "
        f"Server token: {bare_commit}. "
        f"API hash: {bare_commit}."
    )

    masked, mapping = masker.mask(text)

    # Bare commit SHA must be preserved
    assert f"Merged PR in commit {bare_commit}" in masked

    # Contextual occurrences must be masked
    assert "Server token: <TOKEN_HASH_" in masked or "Server token: " in masked
    assert "TOKEN_HASH" in "".join(mapping.keys())


@pytest.mark.anyio
async def test_pii_masker_async_mask():
    """Verifies that async_mask correctly offloads masking and returns identical results."""
    masker = PIIMasker()
    text = "Secret email test@example.com and key: sk_0123456789abcdef0123456789abcdef"

    sync_masked, sync_map = masker.mask(text)
    async_masked, async_map = await masker.async_mask(text)

    assert sync_masked == async_masked
    assert sync_map == async_map


def test_prompt_file_mtime_cache_invalidation():
    """
    Verifies that _read_prompt_file reloads from disk when file mtime changes,
    rather than serving stale cached content.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        prompt_file = Path(tmp_dir) / "test_persona.md"
        prompt_file.write_text("Version 1 content", encoding="utf-8")

        # Initial read
        res1 = _read_prompt_file(prompt_file)
        assert res1 == "Version 1 content"

        # Update file content and advance mtime
        time.sleep(0.05)
        prompt_file.write_text("Version 2 updated persona", encoding="utf-8")
        current_stat = prompt_file.stat()
        os.utime(prompt_file, (current_stat.st_atime + 2, current_stat.st_mtime + 2))

        # Subsequent read must immediately reflect updated content
        res2 = _read_prompt_file(prompt_file)
        assert res2 == "Version 2 updated persona"


def test_dynamic_agent_sets_and_predicates():
    """Verifies that short output and system agents resolve dynamically without disk reads."""
    assert len(get_short_output_agents()) == 4
    assert is_short_output_agent("agent-10010") is True
    assert is_short_output_agent("agent-10001") is False

    assert "orchestrator" in get_system_agents()
    assert is_system_agent("orchestrator") is True
    assert is_system_agent("agent-10001") is False


def test_pii_masker_preserves_directive_bounds_and_identifiers():
    """
    Verifies that Presidio NER does not falsely mask directive bounds (max 2 words, min 5 words),
    Slack mentions, bracketed agent tags, code identifiers, technical terms, or agent names,
    while still properly masking genuine person names and locations.
    """
    masker = PIIMasker()

    text_clean_directive = "User message: <@U0BR4EH6XMY> Test again, want to see avatars, max 2 words"
    masked_dir, map_dir = masker.mask(text_clean_directive)
    assert masked_dir == text_clean_directive
    assert map_dir == {}

    text_complex = (
        "Hello <@U123456> /[Visionary] and [agent-10001], please format in max 5 words or min 2 words. "
        "Also notify Alice in New York using JSON on Slack via int_or_off."
    )
    masked_complex, map_complex = masker.mask(text_complex)

    assert "<@U123456>" in masked_complex
    assert "/[Visionary]" in masked_complex
    assert "[agent-10001]" in masked_complex
    assert "max 5 words" in masked_complex
    assert "min 2 words" in masked_complex
    assert "JSON" in masked_complex
    assert "Slack" in masked_complex
    assert "int_or_off" in masked_complex

    # Genuine PII must still be masked
    if masker._presidio_analyzer is not None:
        assert "Alice" not in masked_complex
        assert "<PERSON_0>" in masked_complex
        assert map_complex["<PERSON_0>"] == "Alice"
        assert "New York" not in masked_complex
        assert "<LOCATION_0>" in masked_complex
        assert map_complex["<LOCATION_0>"] == "New York"


def test_sync_skill_exclusions_creates_json_and_caps_keywords(tmp_path):
    """
    Verifies that sync_skill_exclusions generates a valid JSON file with
    at most 5 sentence_start_keywords per skill, and respects idempotency.
    """
    masker = PIIMasker()
    test_exclusions_file = tmp_path / "test_skill_exclusions.json"
    masker.configure_paths(exclusions_file=test_exclusions_file)

    try:
        stats1 = masker.sync_skill_exclusions()
        assert test_exclusions_file.exists()
        assert stats1["new"] > 0
        assert stats1["total"] > 0

        # Verify JSON contents and keyword limit
        import json

        with open(test_exclusions_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        assert "skills" in data
        assert "max_words" in data["skills"]
        for s_name, s_info in data["skills"].items():
            assert len(s_info["sentence_start_keywords"]) <= 5
            assert "content_hash" in s_info
            assert "global_terms" in s_info
            # Must only include auto-detect or agent skills
            assert s_info.get("auto_detect") is True or s_info.get("scope") == "agent"

        # Non-auto-detect skills must NEVER be in presidio_skill_exclusions.json
        non_auto_skills = {
            "grant_skill", "grant_skill_for_all", "revoke_skill_for_all",
            "revoke_constraint", "rounds", "stop_discussion", "first",
            "open_debate", "list_skills", "control_plane"
        }
        for s in non_auto_skills:
            assert s not in data["skills"]

        # Second sync without changes should be a no-op
        stats2 = masker.sync_skill_exclusions()
        assert stats2["new"] == 0
        assert stats2["updated"] == 0
        assert stats2["unchanged"] == stats1["total"]
    finally:
        masker.reset_paths()


def test_manual_include_and_exclude_overrides(tmp_path):
    """
    Verifies that manual_include and manual_exclude in presidio_skill_exclusions.json
    are honored by the allowlists and preserved across automatic syncs.
    """
    masker = PIIMasker()
    test_exclusions_file = tmp_path / "test_skill_exclusions.json"
    masker.configure_paths(exclusions_file=test_exclusions_file)

    try:
        masker.sync_skill_exclusions()

        import json

        with open(test_exclusions_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Add manual overrides to max_words
        data["skills"]["max_words"]["manual_include"] = ["customdirective"]
        data["skills"]["max_words"]["manual_exclude"] = ["maxwords"]

        with open(test_exclusions_file, "w", encoding="utf-8") as f:
            json.dump(data, f)

        masker.refresh_allowlist()

        assert "customdirective" in masker._sentence_start_allowlist
        assert "maxwords" not in masker._sentence_start_allowlist

        # Re-sync must preserve manual overrides
        masker.sync_skill_exclusions()

        with open(test_exclusions_file, "r", encoding="utf-8") as f:
            data_after = json.load(f)

        assert data_after["skills"]["max_words"]["manual_include"] == ["customdirective"]
        assert data_after["skills"]["max_words"]["manual_exclude"] == ["maxwords"]
    finally:
        masker.reset_paths()


def test_static_sentence_start_allowlist_contains_zero_skill_keywords():
    """
    Verifies that _STATIC_SENTENCE_START_ALLOWLIST_LOWER contains only generic syntax tokens
    and zero skill stems, establishing presidio_skill_exclusions.json as the single source of truth.
    """
    forbidden_stems = {
        "grant", "first", "rounds", "round", "stop", "revoke",
        "max", "min", "maximum", "minimum", "words", "word",
        "history", "exclude", "include", "scribe"
    }
    assert _STATIC_SENTENCE_START_ALLOWLIST_LOWER.isdisjoint(forbidden_stems)


def test_sentence_start_boundary_filtering():
    """
    Verifies that capitalized single-word auto-detected skill keywords are skipped by Presidio
    at sentence or clause boundaries, but properly masked as PERSON mid-sentence.
    Also verifies that non-auto-detect words like 'Grant' are NOT exempted and are masked as PERSON.
    """
    masker = PIIMasker()
    masker.sync_skill_exclusions()

    # 1. At start of prompt -> Max (auto-detect directive keyword) is not masked
    text_start = "Max 5 words for summary"
    masked_start, map_start = masker.mask(text_start)
    assert "Max" in masked_start
    assert "<PERSON_" not in masked_start

    # 2. After leading Slack mention & agent tag -> Max is not masked
    text_mention = "<@U123456> /[Visionary] Max 5 words"
    masked_mention, _ = masker.mask(text_mention)
    assert "Max" in masked_mention
    assert "<PERSON_" not in masked_mention

    # 3. After sentence boundary (period / exclamation) -> Exclude (auto-detect keyword) is not masked
    text_clause = "Analysis complete. Exclude [agent-10001] immediately!"
    masked_clause, _ = masker.mask(text_clause)
    assert "Exclude" in masked_clause

    # 4. Sentence starting with 'Grant Morrison' as a person name MUST be masked (grant_skill is not auto-detected)
    if masker._presidio_analyzer is not None:
        text_grant = "Grant Morrison submitted the budget report yesterday."
        masked_grant, map_grant = masker.mask(text_grant)
        assert "Grant Morrison" not in masked_grant
        assert "<PERSON_0>" in masked_grant
        assert map_grant["<PERSON_0>"] == "Grant Morrison"

    # 5. Mid-sentence -> Grant and Max as person names ARE masked
    if masker._presidio_analyzer is not None:
        text_mid = "Please ask Grant and Max about the budget in New York."
        masked_mid, map_mid = masker.mask(text_mid)
        assert "Grant" not in masked_mid
        assert "Max" not in masked_mid
        assert "New York" not in masked_mid
        assert any("Grant" in v for v in map_mid.values())
        assert any("Max" in v for v in map_mid.values())
        assert any("New York" in v for v in map_mid.values())


def test_pii_masker_excludes_organization_entities():
    """
    Verifies that ORGANIZATION entities (such as 'The AI First Company') remain unmasked,
    avoiding synthetic placeholder tokens in business documents, while PERSON entities
    and high-risk PII remain masked. Also asserts labels_to_ignore includes ORGANIZATION and ORG.
    """
    masker = PIIMasker()
    masker._init_presidio_if_available()
    if masker._presidio_analyzer is not None:
        nlp_engine = getattr(masker._presidio_analyzer, "nlp_engine", None)
        if nlp_engine and hasattr(nlp_engine, "ner_model_configuration"):
            ner_cfg = nlp_engine.ner_model_configuration
            ignored = getattr(ner_cfg, "labels_to_ignore", None) or (
                ner_cfg.get("labels_to_ignore", []) if isinstance(ner_cfg, dict) else []
            )
            assert "ORGANIZATION" in ignored
            assert "ORG" in ignored

        text = "The AI First Company by Benjamin Simkin contains 4 foundations. Contact: secret@corp.com"
        masked, mapping = masker.mask(text)

        # Organization remains unmasked
        assert "The AI First Company" in masked
        assert "<ORGANIZATION" not in masked

        # Person name is masked
        assert "Benjamin Simkin" not in masked
        assert any("Benjamin Simkin" in v for v in mapping.values())

        # Email is strictly masked
        assert "secret@corp.com" not in masked
        assert any("secret@corp.com" in v for v in mapping.values())


@pytest.mark.anyio
async def test_execute_llm_with_retry_privacy_provider_exception_fails_closed():
    """
    Verifies that when _privacy_mode_provider() raises an exception, execute_llm_with_retry
    fails CLOSED (strict=True) and still masks PII in both messages and embedding input.
    """
    from unittest.mock import MagicMock

    from core.llm_utils import execute_llm_with_retry, set_privacy_mode_provider

    def _broken_provider() -> bool:
        raise RuntimeError("settings store corrupted")

    set_privacy_mode_provider(_broken_provider)
    try:
        captured = {}

        async def mock_provider(**call_kwargs):
            captured.update(call_kwargs)
            resp = MagicMock()
            resp.choices = []
            resp.data = [{"embedding": [0.1]}]
            return resp

        await execute_llm_with_retry(
            func=mock_provider,
            kwargs={
                "messages": [{"role": "user", "content": "Send report to alice@corp.com"}],
                "input": ["Embed bob@corp.com SSN 123-45-6789"],
            },
            agent_key="test_agent",
            thread_key=("C1", "T1"),
        )

        msg_text = captured["messages"][0]["content"]
        inp_text = captured["input"][0]
        assert "alice@corp.com" not in msg_text
        assert "<EMAIL_0>" in msg_text
        assert "bob@corp.com" not in inp_text
        assert "123-45-6789" not in inp_text
        assert "<EMAIL_" in inp_text
    finally:
        set_privacy_mode_provider(None)


def test_pii_masker_prescans_literal_placeholder_tokens_to_prevent_collision():
    """TKT-1209 (C5): Forged literal <EMAIL_0>/<EMAIL_1> tags in input must not collide with masked PII."""
    from services.security.pii_masker import PIIMasker

    masker = PIIMasker()
    text = "Injected <EMAIL_0> and <EMAIL_2> before real alice@corp.com"
    masked, mapping = masker.mask(text)

    assert "alice@corp.com" not in masked
    assert "<EMAIL_0>" in masked
    assert "<EMAIL_2>" in masked
    assert "<EMAIL_3>" in masked
    assert mapping.get("<EMAIL_3>") == "alice@corp.com"
    assert "<EMAIL_0>" not in mapping
    assert "<EMAIL_2>" not in mapping
    assert masker.unmask(masked, mapping) == text


@pytest.mark.anyio
async def test_outbound_web_tool_args_stay_masked_at_execution_call_site():
    """TKT-1209 (C5): ExecutionStage.execute_tool_loop must keep <EMAIL_N> masked when
    invoking web_search/fetch_web_page while unmasking real PII for local tools."""
    import json
    from types import SimpleNamespace
    from unittest.mock import patch

    from orchestration.thread_state import thread_state_manager
    from orchestration.turn_pipeline import TurnContext, TurnPipeline
    from services.security.pii_masker import PIIMasker

    masker = PIIMasker()
    masked, mapping = masker.mask("search for alice@corp.com")
    assert "alice@corp.com" not in masked
    assert "<EMAIL_0>" in masked

    recorded_calls: list[tuple[str, dict]] = []

    async def fake_tool_executor(fn_name: str, fn_args: dict) -> str:
        recorded_calls.append((fn_name, dict(fn_args)))
        return "tool ok"

    async def fake_llm_executor(func, completion_kwargs, **kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="Done.", tool_calls=None))],
            usage=None,
        )

    pipeline = TurnPipeline(llm_executor=fake_llm_executor, tool_executor=fake_tool_executor)
    channel_id = "C_PII_LOOP"
    thread_ts = "1700000100.0001"
    ctx = TurnContext(channel_id=channel_id, thread_ts=thread_ts, raw_text="search", user_id="U_TEST")
    ctx.thread_key = (channel_id, thread_ts)
    ctx.agent_key = "agent-10001"
    ctx.agent_cfg = {"model": "gpt-4o", "username": "Architect", "skills": []}
    ctx.turn_token_mapping = dict(mapping)
    ctx.messages = [{"role": "user", "content": masked}]

    initial_response = SimpleNamespace(
        content="",
        tool_calls=[
            SimpleNamespace(
                id="call_web_search",
                function=SimpleNamespace(name="web_search", arguments=json.dumps({"query": masked})),
            ),
            SimpleNamespace(
                id="call_fetch_web",
                function=SimpleNamespace(
                    name="fetch_web_page",
                    arguments=json.dumps({"url": f"https://example.com?q={masked}"}),
                ),
            ),
            SimpleNamespace(
                id="call_read_doc",
                function=SimpleNamespace(name="read_document", arguments=json.dumps({"filename": masked})),
            ),
        ],
    )

    try:
        with patch("services.local_tools.is_agent_authorized_for_tool", return_value=True):
            halt = await pipeline.execution_stage.execute_tool_loop(
                ctx,
                active_tools=[],
                agent_skills=[],
                expired_thread_skills=[],
                thread_ttl_hours=24.0,
                initial_response_message=initial_response,
            )

        assert halt is None
        assert len(recorded_calls) == 3
        calls_by_name = dict(recorded_calls)
        assert "alice@corp.com" not in calls_by_name["web_search"]["query"]
        assert "<EMAIL_0>" in calls_by_name["web_search"]["query"]
        assert "alice@corp.com" not in calls_by_name["fetch_web_page"]["url"]
        assert "<EMAIL_0>" in calls_by_name["fetch_web_page"]["url"]
        assert calls_by_name["read_document"]["filename"] == "search for alice@corp.com"
    finally:
        thread_state_manager.cleanup_thread(channel_id, thread_ts)


@pytest.mark.anyio
async def test_execute_llm_with_retry_into_tool_loop_keeps_web_args_and_checkpoints_masked():
    """TKT-1301: End-to-end execute_llm_with_retry -> ToolLoopRunner keeps web_search/fetch_web_page
    and saved SQLite checkpoints masked while unmasking read_document."""
    import json
    from types import SimpleNamespace
    from unittest.mock import patch

    from core.llm_utils import (
        configure_llm_completion_fn,
        execute_llm_with_retry,
        set_privacy_mode_provider,
    )
    from orchestration.thread_state import thread_state_manager
    from orchestration.turn_pipeline import TurnContext, TurnPipeline
    from services.turn_checkpoint_store import turn_checkpoint_store

    set_privacy_mode_provider(lambda: True)
    recorded_calls: list[tuple[str, dict]] = []

    async def fake_tool_executor(fn_name: str, fn_args: dict) -> str:
        recorded_calls.append((fn_name, dict(fn_args)))
        return "tool ok"

    step = {"n": 0}

    async def fake_completion(**kw):
        step["n"] += 1
        if step["n"] == 1:
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="Checking <EMAIL_0>",
                            tool_calls=[
                                SimpleNamespace(
                                    id="c_web",
                                    function=SimpleNamespace(
                                        name="web_search",
                                        arguments=json.dumps({"query": "profile <EMAIL_0>"}),
                                    ),
                                ),
                                SimpleNamespace(
                                    id="c_doc",
                                    function=SimpleNamespace(
                                        name="read_document",
                                        arguments=json.dumps({"filename": "<EMAIL_0>"}),
                                    ),
                                ),
                            ],
                        )
                    )
                ],
                usage=None,
            )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="Finished for <EMAIL_0>", tool_calls=None))],
            usage=None,
        )

    configure_llm_completion_fn(fake_completion)
    pipeline = TurnPipeline(tool_executor=fake_tool_executor)
    channel_id = "C_E2E_PII_CHK"
    thread_ts = "1700000199.0001"
    ctx = TurnContext(channel_id=channel_id, thread_ts=thread_ts, raw_text="Investigate alice@corp.com", user_id="U1")
    ctx.thread_key = (channel_id, thread_ts)
    ctx.agent_key = "agent-10001"
    ctx.agent_cfg = {"model": "gpt-4o", "username": "Architect", "skills": []}
    ctx.prompt = "Investigate alice@corp.com"
    ctx.turn_token_mapping = {}
    ctx.messages = [{"role": "user", "content": "Investigate alice@corp.com"}]

    try:
        resp1 = await execute_llm_with_retry(
            func=fake_completion,
            kwargs={"model": "gpt-4o", "messages": ctx.messages},
            agent_key=ctx.agent_key,
            thread_key=ctx.thread_key,
            turn_token_mapping=ctx.turn_token_mapping,
        )
        with patch("services.local_tools.is_agent_authorized_for_tool", return_value=True):
            halt = await pipeline.execution_stage.execute_tool_loop(
                ctx,
                active_tools=[],
                agent_skills=[],
                expired_thread_skills=[],
                thread_ttl_hours=24.0,
                initial_response_message=resp1.choices[0].message,
            )

        assert halt is None
        calls_by_name = dict(recorded_calls)
        assert calls_by_name["web_search"]["query"] == "profile <EMAIL_0>"
        assert calls_by_name["read_document"]["filename"] == "alice@corp.com"
        assert ctx.output.final_text == "Finished for alice@corp.com"

        chk_id = pipeline.execution_stage.tool_loop_runner.save_execution_checkpoint(
            ctx=ctx, pause_reason="circuit_breaker", active_tools=[]
        )
        saved = turn_checkpoint_store.get_checkpoint(chk_id)
        assert saved is not None
        assert "alice@corp.com" not in saved.prompt
        assert "alice@corp.com" not in json.dumps(saved.messages)
    finally:
        configure_llm_completion_fn(None)
        set_privacy_mode_provider(None)
        thread_state_manager.cleanup_thread(channel_id, thread_ts)
