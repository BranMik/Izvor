from pathlib import Path

from services.thread_notes_writer import (
    extract_ts5_suffix,
    generate_notes_filename,
    sanitize_prompt_slug,
    save_thread_notes_to_disk,
)
import services.thread_notes_writer as writer_mod


def test_sanitize_prompt_slug():
    assert sanitize_prompt_slug("How to build a scalable DB?") == "how_to_build_a_scala"
    assert sanitize_prompt_slug("   ") == "thread"
    assert sanitize_prompt_slug(None) == "thread"
    assert sanitize_prompt_slug("Special @#$ chars *&^ here") == "special_chars_here"


def test_extract_ts5_suffix():
    assert extract_ts5_suffix("1712345678.123456") == "23456"
    assert extract_ts5_suffix("1700000000.000100") == "00100"
    assert extract_ts5_suffix("123") == "00123"


def test_generate_notes_filename():
    fn_live = generate_notes_filename("Design DB Architecture", "1710000000.000999", "live_notes")
    assert fn_live == "design_db_architectu_00999_live_notes.md"

    fn_conc = generate_notes_filename("Design DB Architecture", "1710000000.000999", "conclusion")
    assert fn_conc == "design_db_architectu_00999_conclusion.md"


def test_save_thread_notes_to_disk_creates_and_updates_files(tmp_path, monkeypatch):
    monkeypatch.setattr(writer_mod, "THREAD_NOTES_DIR", tmp_path)

    channel_id = "C_TEST_WRITE"
    thread_ts = "1710000000.12345"
    prompt = "Create a resilient cache architecture"
    live_text = "🎯 *Problem Statement*\nBuild cache.\n\n*Perspectives*\nRedis cluster."
    conclusion_text = "🎯 *Problem Statement*\nBuild cache.\n\n*Conclusion and Argumentation*\nConsensus on Redis."

    # 1. Initial write
    res = save_thread_notes_to_disk(
        channel_id=channel_id,
        thread_ts=thread_ts,
        initial_prompt=prompt,
        live_notes_text=live_text,
        conclusion_text=conclusion_text,
        force=True,
    )

    assert "live_notes_path" in res
    assert "conclusion_path" in res

    live_path = Path(res["live_notes_path"])
    conc_path = Path(res["conclusion_path"])

    assert live_path.exists()
    assert conc_path.exists()
    assert live_path.name == "create_a_resilient_c_12345_live_notes.md"
    assert conc_path.name == "create_a_resilient_c_12345_conclusion.md"

    live_content = live_path.read_text(encoding="utf-8")
    conc_content = conc_path.read_text(encoding="utf-8")

    assert "Live Discussion Scribe Notes" in live_content
    assert "Redis cluster" in live_content
    assert "Discussion Conclusion & Dialectical Analysis" in conc_content
    assert "Consensus on Redis" in conc_content

    # 2. Re-engagement update in-place
    updated_conc_text = "🎯 *Problem Statement*\nBuild cache.\n\n*Conclusion and Argumentation*\nUpdated consensus with Memcached fallback."
    res2 = save_thread_notes_to_disk(
        channel_id=channel_id,
        thread_ts=thread_ts,
        initial_prompt=prompt,
        live_notes_text=live_text,
        conclusion_text=updated_conc_text,
        force=True,
    )

    assert res2["conclusion_path"] == str(conc_path)
    updated_conc_content = conc_path.read_text(encoding="utf-8")
    assert "Memcached fallback" in updated_conc_content
