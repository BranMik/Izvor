import os
import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--hybrid--mocked-llm",
        "--hybrid-mocked-llm",
        dest="hybrid_mocked_llm",
        action="store_true",
        default=False,
        help="Activates hybrid mocked LLM testing (default is live LLM execution for all agents)",
    )
