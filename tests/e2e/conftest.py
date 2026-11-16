import shutil

import pytest

from core.paths import AGENTS_RESOURCES_DIR as REAL_AGENTS_RESOURCES_DIR, PROJECT_ROOT
from services.local_tools import configure_paths as configure_local_tools_paths, reset_paths as reset_local_tools_paths


@pytest.fixture(autouse=True)
def isolate_local_tools_for_tests(tmp_path):
    """
    Isolates local_tools document authoring/reading inside a temporary agents_resources
    directory during pytest E2E runs so tests never dirty the repository workspace.
    """
    temp_resources = tmp_path / "_e2e_iso" / "agents_resources"
    temp_resources.mkdir(parents=True, exist_ok=True)

    sys_spec = REAL_AGENTS_RESOURCES_DIR / "sys_spec.txt"
    if sys_spec.exists():
        shutil.copy2(sys_spec, temp_resources / "sys_spec.txt")

    configure_local_tools_paths(
        workspace_dir=PROJECT_ROOT,
        agents_resources_dir=temp_resources,
    )
    try:
        yield temp_resources
    finally:
        reset_local_tools_paths()


def pytest_addoption(parser):
    parser.addoption(
        "--mock",
        "--offline",
        dest="mock",
        action="store_true",
        default=False,
        help="Runs offline in-memory simulation for E2E scenarios (default is live LLM execution)",
    )
    parser.addoption(
        "--hybrid--mocked-llm",
        "--hybrid-mocked-llm",
        dest="hybrid_mocked_llm",
        action="store_true",
        default=False,
        help="Activates hybrid mocked LLM testing (default is live LLM execution for all agents)",
    )
