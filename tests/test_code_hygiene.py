import subprocess
import sys
from pathlib import Path


def test_no_undefined_variables_or_imports():
    """
    Enforces that all modules in the codebase have valid imports and no undefined names (F821).
    This prevents missing imports (like 're', 'json', etc.) from slipping into runtime.
    """
    root_dir = Path(__file__).resolve().parent.parent
    targets = [
        "services",
        "orchestration",
        "adapters",
        "core",
        "agents",
        "nlp",
        "pipeline",
        "skills",
        "e2e_testing_harness",
        "tests",
    ]

    existing_targets = [t for t in targets if (root_dir / t).exists()]

    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--select", "F821", *existing_targets],
        cwd=root_dir,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, (
        f"Undefined variables or missing imports detected (F821):\n{result.stdout}\n{result.stderr}"
    )
