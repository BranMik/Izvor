import ast
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from core.database import init_db
from nlp.intent_extractor import IntentExtractor
from nlp.language_detector import detect_language, detect_language_llm
from services.telemetry import (
    _TRACE_BUFFER,
    _TRACE_BUFFER_LOCK,
    clear_turn_traces,
    flush_turn_traces,
    get_turn_traces,
    record_turn_trace,
)


@pytest.fixture(autouse=True)
def setup_db():
    init_db()
    clear_turn_traces()
    yield
    clear_turn_traces()


def test_zero_litellm_completion_in_production_code():
    """
    AST analysis asserting zero calls to synchronous litellm.completion across all production modules.
    """
    root_dir = Path(__file__).resolve().parents[2]
    production_dirs = [
        "config_store",
        "nlp",
        "orchestration",
        "agents",
        "services",
        "skills",
        "prompts",
        "adapters",
        "core",
        "pipeline",
    ]
    violations = []

    for pdir in production_dirs:
        dir_path = root_dir / "engine" / pdir
        if not dir_path.exists():
            continue
        for py_file in dir_path.rglob("*.py"):
            source = py_file.read_text(encoding="utf-8")
            try:
                tree = ast.parse(source, filename=str(py_file))
            except SyntaxError:
                continue

            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute):
                    if node.attr == "completion":
                        # Check if base is 'litellm'
                        if isinstance(node.value, ast.Name) and node.value.id == "litellm":
                            violations.append(f"{py_file.relative_to(root_dir)}:L{node.lineno}")

    assert violations == [], f"Found synchronous litellm.completion usages in production code: {violations}"


@pytest.mark.anyio
async def test_detect_language_llm_uses_acompletion():
    """Verifies that detect_language_llm dispatches via litellm.acompletion."""
    mock_choice = MagicMock()
    mock_choice.message.content = "Croatian"
    mock_resp = MagicMock()
    mock_resp.choices = [mock_choice]
    mock_resp.usage = None

    with patch("nlp.language_detector.litellm.acompletion", AsyncMock(return_value=mock_resp)) as mock_acomp:
        res = await detect_language_llm("Ovo je test na hrvatskom jeziku.")
        assert res == "Croatian"
        assert mock_acomp.called


def test_detect_language_sync_uses_fast_heuristic():
    """Verifies that synchronous detect_language works deterministically without LLM calls."""
    assert detect_language("Koji je ovo jezik i kako funkcionira?") == "Croatian"
    assert detect_language("Das ist ein deutscher Satz für den Test.") == "German"
    assert detect_language("This is a standard English query.") == "English"


@pytest.mark.anyio
async def test_intent_extractor_uses_acompletion():
    """Verifies that IntentExtractor dispatches via litellm.acompletion."""
    extractor = IntentExtractor()
    mock_choice = MagicMock()
    mock_choice.message.content = json.dumps({"actions": [], "modifiers": {}})
    mock_resp = MagicMock()
    mock_resp.choices = [mock_choice]
    mock_resp.usage = None

    with patch("nlp.intent_extractor.litellm.acompletion", AsyncMock(return_value=mock_resp)) as mock_acomp:
        res = await extractor.extract_intent("Summarize this document")
        assert res is not None
        assert mock_acomp.called


def test_telemetry_write_behind_buffer_and_batch_flush():
    """Verifies that record_turn_trace queues traces and flush_turn_traces batch-inserts them."""
    session_id = "test_telemetry_sess"
    # Intercept flush to keep traces in queue initially
    with patch("services.telemetry.flush_turn_traces", return_value=0):
        t1 = record_turn_trace(
            session_id=session_id,
            channel_id="C1",
            thread_ts="T1",
            agent_key="agent-1",
            stage_name="validation",
            duration_ms=45.2,
        )
        t2 = record_turn_trace(
            session_id=session_id,
            channel_id="C1",
            thread_ts="T1",
            agent_key="agent-1",
            stage_name="execution",
            duration_ms=120.5,
        )
        with _TRACE_BUFFER_LOCK:
            assert len(_TRACE_BUFFER) == 2

    # Now flush for real
    inserted = flush_turn_traces()
    assert inserted == 2
    with _TRACE_BUFFER_LOCK:
        assert len(_TRACE_BUFFER) == 0

    # Verify database read through get_turn_traces
    traces = get_turn_traces(session_id=session_id)
    assert len(traces) == 2
    assert traces[0]["trace_id"] == t1
    assert traces[1]["trace_id"] == t2
    assert traces[0]["stage_name"] == "validation"
    assert traces[1]["stage_name"] == "execution"
