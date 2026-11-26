"""Static hygiene tests for reproducible packaging (uv.lock) and GitHub Actions CI."""

from __future__ import annotations

from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_uv_lockfile_exists_and_nonempty() -> None:
    lock_file = PROJECT_ROOT / "uv.lock"
    assert lock_file.exists(), "uv.lock is missing at repository root."
    content = lock_file.read_text(encoding="utf-8")
    assert "version =" in content
    assert "izvor" in content


def test_github_actions_ci_workflow_is_valid_and_complete() -> None:
    ci_file = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
    assert ci_file.exists(), ".github/workflows/ci.yml is missing."

    raw_text = ci_file.read_text(encoding="utf-8")
    data = yaml.safe_load(raw_text)
    assert isinstance(data, dict)
    assert "jobs" in data and isinstance(data["jobs"], dict)

    jobs = data["jobs"]
    for required_job in ("lint-and-typecheck", "dependency-audit", "test"):
        assert required_job in jobs, f"CI workflow missing required job '{required_job}'."

    assert "ruff check ." in raw_text
    assert "mypy engine/core engine/orchestration/protocols.py" in raw_text
    assert "pip-audit" in raw_text
    assert "pytest -n auto --dist loadscope" in raw_text

