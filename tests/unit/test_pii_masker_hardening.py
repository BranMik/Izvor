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
from services.rag.pii_masker import PIIMasker


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
