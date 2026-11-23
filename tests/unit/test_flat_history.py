from unittest.mock import AsyncMock, MagicMock

import pytest

from core.llm_utils import configure_llm_completion_fn
from orchestration.agent_router import (
    configure_fetch_thread_history,
    configure_post_agent_response,
    configure_router_sleep_fn,
    process_and_respond,
)
from config_store.agent_settings import reset_enabled_agents, set_orchestration_mode
from orchestration.thread_state import thread_state_manager
from services.flat_history_recorder import (
    configure_paths,
    format_messages_to_markdown,
    generate_flat_history_filename,
    get_flat_history_filepath,
    record_flat_history,
    reset_paths,
)


@pytest.fixture(autouse=True)
def setup_flat_history_test_environment(tmp_path):
    reset_enabled_agents()
    set_orchestration_mode("sandwich")
    thread_state_manager.reset_all()
    configure_router_sleep_fn(AsyncMock(return_value=None))

    # Point flat histories to tmp_path during unit test isolation
    test_dir = tmp_path / "flat-histories"
    test_dir.mkdir(parents=True, exist_ok=True)
    configure_paths(test_dir)
    yield test_dir
    reset_paths()


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
    """Verify that get_flat_history_filepath reuses existing file for the same channel+thread."""
    tmp_path / "flat-histories"
    thread_ts = "1712345678.555"
    fpath1 = get_flat_history_filepath("C123", thread_ts, "Kako napraviti web")
    fpath1.write_text(
        f"# Thread Conversation History\n- **Channel ID:** `C123`\n- **Thread TS:** `{thread_ts}`\n",
        encoding="utf-8",
    )

    # Second call with different prompt in same channel+thread must find and return existing file
    fpath2 = get_flat_history_filepath("C123", thread_ts, "Nastavi sa detaljima")
    assert fpath1 == fpath2
    assert fpath2.name == "kako_napra_C123_1712345678.md"

    # Same thread_ts in a DIFFERENT channel must produce a distinct file (channel-scoped isolation)
    fpath_other = get_flat_history_filepath("C999", thread_ts, "Kako napraviti web")
    assert fpath_other != fpath1
    assert fpath_other.name == "kako_napra_C999_1712345678.md"


def test_format_messages_to_markdown():
    """Verify markdown output formatting with bold speaker tags and metadata."""
    messages = [
        {"role": "user", "content": "Kako optimizirati bazu podataka?"},
        {"role": "assistant", "content": "[agent-10001]: Preporucujem skaliranje i indeksiranje."},
        {"role": "assistant", "content": "[agent-10011]: Slazem se, a mozemo dodati i Redis cache."},
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
    assert "### **[agent-10001]**\n\nPreporucujem skaliranje i indeksiranje." in md
    assert "### **[agent-10011]**\n\nSlazem se, a mozemo dodati i Redis cache." in md


def test_record_flat_history_atomic_write(tmp_path):
    """Verify record_flat_history creates the file cleanly on disk."""
    thread_ts = "1712345678.900"
    channel_id = "C_TEST"
    messages = [
        {"role": "user", "content": "Plan projekta"},
        {"role": "assistant", "content": "[agent-10001]: Definirajmo ciljeve."},
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
    assert "### **[agent-10001]**" in content
    assert "Definirajmo ciljeve." in content


@pytest.mark.anyio
async def test_process_and_respond_writes_flat_history(tmp_path):
    """Verify end-to-end that process_and_respond updates flat-histories after agent turn."""
    channel_id = "C_INTEG"
    thread_ts = "1712345678.888"

    mock_llm_response = MagicMock()
    mock_llm_response.choices = [
        MagicMock(message=MagicMock(content="Ovo je rjesenje [agent-10011]."))
    ]
    mock_llm_response.usage = {"prompt_tokens": 50, "completion_tokens": 20}

    async def fake_execute_llm(*args, **kwargs):
        return mock_llm_response

    async def fake_post_agent_response(*args, **kwargs):
        return {"ok": True, "ts": "1712345679.000", "message": kwargs}

    async def fake_fetch_history(*args, **kwargs):
        return [{"role": "user", "content": "Kako podici Docker container?"}]

    configure_llm_completion_fn(fake_execute_llm)
    configure_post_agent_response(fake_post_agent_response)
    configure_fetch_thread_history(fake_fetch_history)

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


def test_configure_paths_isolation(tmp_path):
    """Verify that configure_paths properly routes writes to custom isolated directories."""
    custom_dir = tmp_path / "custom_flat"
    custom_dir.mkdir(parents=True, exist_ok=True)
    configure_paths(custom_dir)
    try:
        res = record_flat_history(
            channel_id="C_CUSTOM",
            thread_ts="1712345678.999",
            initial_prompt="Custom dir test write",
            messages=[{"role": "user", "content": "hello custom"}],
        )
        assert res is not None
        assert res.exists()
        assert res.parent.resolve() == custom_dir.resolve()
    finally:
        reset_paths()

