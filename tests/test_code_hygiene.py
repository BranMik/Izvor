import ast
import subprocess
import sys
from pathlib import Path


def test_python_ast_compilation_and_hygiene():
    """
    Parses all repository Python files using the standard library `ast.parse`
    to guarantee zero syntax errors, malformed tokens, or invalid indentations.
    Additionally runs `ruff check --select F821` if ruff is available in the environment.
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
        "main.py",
    ]

    failed_files: list[str] = []
    py_files_checked = 0

    for target_name in targets:
        target_path = root_dir / target_name
        if not target_path.exists():
            continue
        if target_path.is_file() and target_path.suffix == ".py":
            files = [target_path]
        else:
            files = list(target_path.rglob("*.py"))

        for py_file in files:
            py_files_checked += 1
            try:
                content = py_file.read_text(encoding="utf-8")
                ast.parse(content, filename=str(py_file))
            except Exception as e:
                failed_files.append(f"{py_file.relative_to(root_dir)}: {e}")

    assert py_files_checked > 0, "No python files found to check!"
    assert not failed_files, (
        f"AST syntax/parsing errors found in {len(failed_files)} file(s):\n"
        + "\n".join(failed_files)
    )

    # Optional Ruff F821 check if ruff is installed in the current environment
    try:
        ruff_check = subprocess.run(
            [sys.executable, "-m", "ruff", "--version"],
            cwd=root_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        if ruff_check.returncode == 0:
            existing_targets = [t for t in targets if (root_dir / t).exists()]
            res = subprocess.run(
                [sys.executable, "-m", "ruff", "check", "--select", "F821", *existing_targets],
                cwd=root_dir,
                capture_output=True,
                text=True,
                check=False,
            )
            assert res.returncode == 0, (
                f"Ruff F821 undefined variable check failed:\n{res.stdout}\n{res.stderr}"
            )
    except FileNotFoundError:
        pass
