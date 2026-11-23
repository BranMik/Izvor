
from core import paths
from config_store.system_settings import (
    SystemSettingsModel,
    system_settings_manager,
    get_max_tool_iterations,
    get_tool_call_num_retries,
    get_word_limit_grace_buffer,
    get_output_constraints,
    get_guardrail_embedding_cache_size,
    get_thread_cleanup_interval_seconds,
    get_audit_integrity_check_interval_seconds,
    get_pricing_cache_ttl_days,
    get_pricing_cache_ttl_seconds,
    get_slack_file_download_timeout_seconds,
    get_web_search_timeout_seconds,
    get_web_fetch_timeout_seconds,
    get_db_busy_timeout_seconds,
    get_llm_max_retries,
    get_slack_rate_limit_per_second,
    get_slack_rate_limit_burst,
    get_pricing_benchmark_url,
    get_web_fetch_user_agent,
    get_web_security,
    get_security_burst_limits,
)
from prompts.builder import load_system_prompt


def test_core_paths_rag_anchors():
    """Verifies that RAG and vector directories are centralized in core.paths."""
    assert paths.RAG_KNOWLEDGE_DIR == paths.CONFIG_DIR / "rag_knowledge"
    assert paths.RAG_CACHE_DIR == paths.DATA_DIR / ".rag_cache"
    assert paths.LANCEDB_DIR == paths.RAG_CACHE_DIR / "lancedb"
    assert paths.MANIFEST_PATH == paths.RAG_CACHE_DIR / "file_manifest.json"
    assert "RAG_KNOWLEDGE_DIR" in paths.__all__
    assert "RAG_CACHE_DIR" in paths.__all__
    assert "LANCEDB_DIR" in paths.__all__
    assert "MANIFEST_PATH" in paths.__all__


def test_load_system_prompt_files():
    """Verifies that extracted system prompts exist in prompts/system_agents/ and load correctly."""
    prompt_files = [
        "Discussion_Scribe_Live",
        "Discussion_Scribe_Conclusion",
        "Intent_Extractor",
        "Language_Detector",
    ]
    for prompt_name in prompt_files:
        file_path = paths.SYSTEM_AGENTS_DIR / f"{prompt_name}.md"
        assert file_path.exists(), f"Missing prompt file: {file_path}"
        content = load_system_prompt(prompt_name, fallback="FALLBACK")
        assert content != "FALLBACK", f"Failed to read prompt content for {prompt_name}"
        assert len(content) > 20, f"Prompt content suspiciously short for {prompt_name}"


def test_load_system_prompt_fallback(tmp_path):
    """Verifies that load_system_prompt falls back gracefully when file does not exist."""
    fallback_text = "Custom fallback instructions"
    loaded = load_system_prompt("non_existent_prompt_xyz_123", fallback=fallback_text)
    assert loaded == fallback_text


def test_system_settings_extracted_constants_defaults():
    """Verifies all extracted technical settings defaults loaded into SystemSettingsManager."""
    system_settings_manager.load()

    assert get_max_tool_iterations() == 3
    assert get_tool_call_num_retries() == 2
    assert get_word_limit_grace_buffer() == 50
    assert SystemSettingsModel().output_constraints == {"default_max_words": 200, "short_max_words": 100}
    assert get_output_constraints() == {"default_max_words": 150, "short_max_words": 70}
    assert get_guardrail_embedding_cache_size() == 500
    assert get_thread_cleanup_interval_seconds() == 600.0
    assert get_audit_integrity_check_interval_seconds() == 43200.0
    assert get_pricing_cache_ttl_days() == 7
    assert get_pricing_cache_ttl_seconds() == 7 * 86400
    assert get_slack_file_download_timeout_seconds() == 30.0
    assert get_web_search_timeout_seconds() == 10.0
    assert get_web_fetch_timeout_seconds() == 12.0
    assert get_db_busy_timeout_seconds() == 5.0
    assert get_llm_max_retries() == 3
    assert get_slack_rate_limit_per_second() == 10.0
    assert get_slack_rate_limit_burst() == 15.0
    assert "artificialanalysis.ai" in get_pricing_benchmark_url()
    assert "Mozilla/5.0" in get_web_fetch_user_agent()

    sec = get_web_security()
    assert sec["max_url_length"] == 2048
    assert sec["max_query_length"] == 1024
    assert sec["max_redirects"] == 5
    assert sec["max_body_bytes"] == 2097152
    assert sec["max_fetches_per_turn"] == 5

    sbl = get_security_burst_limits()
    assert sbl["auth_failure_window_seconds"] == 60.0
    assert sbl["auth_failure_burst_limit"] == 5
    assert sbl["global_auth_failure_burst_limit"] == 25
    assert sbl["max_tracker_entries"] == 1000


def test_apply_mechanical_word_limit_with_grace_buffer():
    """Verifies apply_mechanical_word_limit uses grace_buffer for >=20 words and 0 buffer for <20 words."""
    from orchestration.stages.post_record import apply_mechanical_word_limit

    # >= 20 words target uses default 50 grace buffer -> cutoff is 75 words
    text_70 = " ".join(["word"] * 70)
    assert apply_mechanical_word_limit(text_70, max_words=25) == text_70

    text_90 = " ".join(["word"] * 90)
    truncated_over = apply_mechanical_word_limit(text_90, max_words=25)
    assert len(truncated_over.split()) == 75

    # < 20 words target defaults to 0 grace buffer -> cutoff is exact max_words
    text_15 = " ".join(["word"] * 15)
    truncated_strict = apply_mechanical_word_limit(text_15, max_words=10)
    assert len(truncated_strict.split()) == 10

    # Explicit custom grace buffer override
    truncated_custom = apply_mechanical_word_limit(text_70, max_words=10, grace_buffer=5)
    assert len(truncated_custom.split()) == 15


def test_sync_output_constraints_refreshes_cached_agent_prompts():
    import json
    import os
    from config_store.agent_settings import settings_manager

    # Ensure initial sync matches active configured output_constraints
    constraints = get_output_constraints()
    settings_manager.sync_output_constraints_if_changed()
    assert f"strictly NOT exceed {constraints['default_max_words']} words" in settings_manager.agents_config["agent-10008"]["system_prompt"]
    assert f"strictly NOT exceed {constraints['short_max_words']} words" in settings_manager.agents_config["agent-10010"]["system_prompt"]
    assert settings_manager.sync_output_constraints_if_changed() is False

    # Simulate external runtime edit to system_settings.json on disk
    sys_file = system_settings_manager.active_settings_file
    data = json.loads(sys_file.read_text(encoding="utf-8"))
    data["output_constraints"] = {"default_max_words": 260, "short_max_words": 50}
    sys_file.write_text(json.dumps(data), encoding="utf-8")
    # Bump mtime cleanly to guarantee detection even on coarse filesystem timestamp granularity
    stat = sys_file.stat()
    os.utime(sys_file, (stat.st_atime + 2.0, stat.st_mtime + 2.0))

    assert settings_manager.sync_output_constraints_if_changed() is True
    assert "strictly NOT exceed 260 words" in settings_manager.agents_config["agent-10008"]["system_prompt"]
    assert "strictly NOT exceed 50 words" in settings_manager.agents_config["agent-10010"]["system_prompt"]
    assert "Compact Override:" in settings_manager.agents_config["agent-10010"]["system_prompt"]
    assert "<output_contract>" not in settings_manager.agents_config["agent-10010"]["system_prompt"]
    assert "strictly NOT exceed 260 words" in settings_manager.system_agents_config["Challenger"]["system_prompt"]
    assert settings_manager.sync_output_constraints_if_changed() is False

