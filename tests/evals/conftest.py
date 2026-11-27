from __future__ import annotations

import pytest

from core.llm_utils import reset_default_llm_guard, reset_llm_completion_fn
from core.model_catalog import reset_env_provider
from services.rag.embeddings import EmbeddingService


@pytest.fixture(autouse=True)
def guard_unmocked_llm_calls_for_tests():
    """
    Shadows tests/conftest.py's fail-closed LLM guard so live AI evaluation suites
    in tests/evals/ can invoke real LLM APIs cleanly.
    """
    reset_default_llm_guard()
    reset_llm_completion_fn()
    reset_env_provider()
    yield []
    reset_default_llm_guard()


@pytest.fixture(autouse=True)
def mock_embedding_service_for_tests():
    """
    Shadows tests/conftest.py's dummy zero-vector embedding override so live RAG
    and guardrail evaluation suites generate real vector embeddings.
    """
    EmbeddingService.reset_embeddings_fn()
    yield
    EmbeddingService.reset_embeddings_fn()


def pytest_addoption(parser):
    parser.addoption(
        "--eval-case",
        dest="eval_case",
        action="store",
        default=None,
        help="Optional comma-separated eval case IDs (e.g. RAG-01,GRD-01)",
    )
    parser.addoption(
        "--eval-max-cost-usd",
        dest="eval_max_cost_usd",
        action="store",
        type=float,
        default=1.00,
        help="Maximum USD spend cap for pytest eval run (default: 1.00)",
    )
    parser.addoption(
        "--eval-judge-model",
        dest="eval_judge_model",
        action="store",
        default=None,
        help="Optional override model for LLM-as-judge",
    )

