import os
import shutil
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from orchestration import agent_router
from orchestration.agent_router import process_and_respond
from orchestration.agent_settings import reset_enabled_agents, set_orchestration_mode
from orchestration.thread_state import thread_state_manager
from services.flat_history_recorder import (
    DEFAULT_FLAT_HISTORIES_DIR,
    FLAT_HISTORIES_DIR,
    format_messages_to_markdown,
    generate_flat_history_filename,
    get_flat_history_filepath,
    is_test_thread,
    record_flat_history,
)


@pytest.fixture(autouse=True)
def setup_flat_history_test_environment(monkeypatch, tmp_path):
    reset_enabled_agents()
    set_orchestration_mode("sandwich")
    thread_state_manager.reset_all()
    monkeypatch.setattr(agent_router.asyncio, "sleep", AsyncMock(return_value=None))

    # Point FLAT_HISTORIES_DIR to tmp_path during unit test isolation
    test_dir = tmp_path / "flat-histories"
    test_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "services.flat_history_recorder.FLAT_HISTORIES_DIR", test_dir
    )
    yield test_dir


def test_generate_flat_history_filename():
    """Verify filename format starts with 10 chars of prompt and 10 chars of thread_ts."""
    # User's exact example: initial prompt starts thread, @app ignored
    prompt = "@app kako napraviti web stranicu"
    thread_ts = "1712345678.123456"
    filename = generate_flat_history_filename(prompt, thread_ts)
    assert filename == "kako_napra_1712345678.md"
    assert filename.startswith("kako_napra")
    assert "1712345678" in filename
    assert filename.endswith(".md")


def test_generate_flat_history_filename_variations():
    """Verify Slack mention tags, slash commands, and special chars are stripped properly."""
    # Slack mention format <@U012345ABC>
    p1 = "<@U012345ABC> Objavi clanak o umjetnoj inteligenciji /rounds 2"
    f1 = generate_flat_history_filename(p1, "1700000001.999")
    assert f1.startswith("objavi_cla")
    assert "1700000001" in f1

    # Short prompt
    p2 = "Test!"
    f2 = generate_flat_history_filename(p2, "1700000002.111")
    assert f2 == "test_1700000002.md"

    # Only mention prompt fallback
    p3 = "@AgentBot"
    f3 = generate_flat_history_filename(p3, "1700000003.222")
    assert f3 == "thread_1700000003.md"


def test_one_thread_one_file_reused(tmp_path):
    """Verify that get_flat_history_filepath reuses existing file for the same thread."""
    test_dir = tmp_path / "flat-histories"
    thread_ts = "1712345678.555"
    fpath1 = get_flat_history_filepath("C123", thread_ts, "Kako napraviti web")
    fpath1.write_text(f"# Thread Conversation History\n- **Thread TS:** `{thread_ts}`\n", encoding="utf-8")

    # Second call with different prompt in same thread must find and return existing file
    fpath2 = get_flat_history_filepath("C123", thread_ts, "Nastavi sa detaljima")
    assert fpath1 == fpath2
    assert fpath2.name == "kako_napra_1712345678.md"


def test_format_messages_to_markdown():
    """Verify markdown output formatting with bold speaker tags and metadata."""
    messages = [
        {"role": "user", "content": "Kako optimizirati bazu podataka?"},
        {"role": "assistant", "content": "[Visionary]: Preporucujem skaliranje i indeksiranje."},
        {"role": "assistant", "content": "[TechLead]: Slazem se, a mozemo dodati i Redis cache."},
    ]
    md = format_messages_to_markdown(
        messages=messages,
        channel_id="C_TEST",
        thread_ts="1712345678.100",
        initial_prompt="Kako optimizirati bazu podataka?",
    )
    assert "# Thread Conversation History" in md
    assert "- **Thread TS:** `1712345678.100`" in md
    assert "- **Channel ID:** `C_TEST`" in md
    assert "- **Initial Prompt:** Kako optimizirati bazu podataka?" in md
    assert "### **[User]**\n\nKako optimizirati bazu podataka?" in md
    assert "### **[Visionary]**\n\nPreporucujem skaliranje i indeksiranje." in md
    assert "### **[TechLead]**\n\nSlazem se, a mozemo dodati i Redis cache." in md


def test_record_flat_history_atomic_write(tmp_path):
    """Verify record_flat_history creates the file cleanly on disk."""
    thread_ts = "1712345678.900"
    channel_id = "C_TEST"
    messages = [
        {"role": "user", "content": "Plan projekta"},
        {"role": "assistant", "content": "[Visionary]: Definirajmo ciljeve."},
    ]
    path = record_flat_history(
        channel_id=channel_id,
        thread_ts=thread_ts,
        initial_prompt="Plan projekta",
        messages=messages,
    )
    assert path is not None
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "### **[User]**" in content
    assert "### **[Visionary]**" in content
    assert "Definirajmo ciljeve." in content


@pytest.mark.anyio
async def test_process_and_respond_writes_flat_history(monkeypatch, tmp_path):
    """Verify end-to-end that process_and_respond updates flat-histories after agent turn."""
    channel_id = "C_INTEG"
    thread_ts = "1712345678.888"

    mock_llm_response = MagicMock()
    mock_llm_response.choices = [
        MagicMock(message=MagicMock(content="Ovo je rjesenje [TechLead]."))
    ]
    mock_llm_response.usage = {"prompt_tokens": 50, "completion_tokens": 20}

    async def fake_execute_llm(*args, **kwargs):
        return mock_llm_response

    async def fake_post_agent_response(**kwargs):
        return {"ok": True, "ts": "1712345679.000", "message": kwargs}

    async def fake_fetch_history(*args, **kwargs):
        return [{"role": "user", "content": "Kako podici Docker container?"}]

    monkeypatch.setattr(agent_router, "execute_llm_with_retry", fake_execute_llm)
    monkeypatch.setattr(agent_router, "post_agent_response", fake_post_agent_response)
    monkeypatch.setattr(agent_router, "fetch_thread_history", fake_fetch_history)

    # Initial prompt stored in ThreadStateManager
    thread_state_manager.set_initial_prompt(
        channel_id, thread_ts, "Kako podici Docker container?"
    )

    res = await process_and_respond(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="Kako podici Docker container?",
    )

    assert res.get("ok") is True
    test_dir = tmp_path / "flat-histories"
    files = list(test_dir.glob("*_1712345678.md"))
    assert len(files) == 1
    hist_file = files[0]
    assert hist_file.name.startswith("kako_podic")
    content = hist_file.read_text(encoding="utf-8")
    assert "### **[User]**" in content
    assert "Kako podici Docker container?" in content
    assert "Ovo je rjesenje" in content


def test_is_test_thread_detection(monkeypatch):
    """Verify that is_test_thread properly classifies testing vs real production threads."""
    # When FLAT_HISTORIES_DIR is pointed to default project directory
    monkeypatch.setattr(
        "services.flat_history_recorder.FLAT_HISTORIES_DIR", DEFAULT_FLAT_HISTORIES_DIR
    )

    # 1. Channels starting with test prefixes or test IDs
    assert is_test_thread(channel_id="C_TEST_01", thread_ts="1789000001.0000") is True
    assert is_test_thread(channel_id="C_TEST", thread_ts="1789000001.0000") is True
    assert is_test_thread(channel_id="test_channel", thread_ts="1789000001.0000") is True
    assert is_test_thread(channel_id="C123", thread_ts="1789000001.0000") is True
    assert is_test_thread(channel_id="C_FILE_TEST", thread_ts="1789000001.0000") is True

    # 2. Test thread timestamps (e.g. E2E T_xx or mock synthetic timestamps)
    assert is_test_thread(channel_id="C0123REAL", thread_ts="T_08") is True
    assert is_test_thread(channel_id="C0123REAL", thread_ts="T_15") is True
    assert is_test_thread(channel_id="C0123REAL", thread_ts="1700000001.0000") is True
    assert is_test_thread(channel_id="C0123REAL", thread_ts="123450001") is True
    assert is_test_thread(channel_id="C0123REAL", thread_ts="888880001") is True

    # 3. Environment flags
    monkeypatch.setenv("E2E_TESTING", "1")
    assert is_test_thread(channel_id="C0123REAL", thread_ts="1789123456.789012") is True
    monkeypatch.delenv("E2E_TESTING")

    # 4. When redirected to isolated tmp_path, is_test_thread returns False to allow test writes
    monkeypatch.setattr(
        "services.flat_history_recorder.FLAT_HISTORIES_DIR", Path("/tmp/isolated_test")
    )
    assert is_test_thread(channel_id="C_TEST_01", thread_ts="T_15") is False


def test_record_flat_history_skips_testing_thread(monkeypatch, tmp_path):
    """Verify that record_flat_history returns None and creates NO files for testing threads."""
    # Point FLAT_HISTORIES_DIR to default to test real behavior
    monkeypatch.setattr(
        "services.flat_history_recorder.FLAT_HISTORIES_DIR", DEFAULT_FLAT_HISTORIES_DIR
    )

    res = record_flat_history(
        channel_id="C_TEST_01",
        thread_ts="T_01",
        initial_prompt="Should skip this",
        messages=[{"role": "user", "content": "hello"}],
    )
    assert res is None

    # But with force=True and a custom dir, it can write
    custom_dir = tmp_path / "custom_flat"
    custom_dir.mkdir()
    monkeypatch.setattr(
        "services.flat_history_recorder.FLAT_HISTORIES_DIR", custom_dir
    )
    res_forced = record_flat_history(
        channel_id="C_TEST_01",
        thread_ts="1712345678.999",
        initial_prompt="Forced test write",
        messages=[{"role": "user", "content": "hello forced"}],
        force=True,
    )
    assert res_forced is not None
    assert res_forced.exists()

