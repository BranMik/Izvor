import ast
import re
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
        "engine",
        "e2e_testing_harness",
        "tests",
        "config/skills",
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
    assert not failed_files, f"AST syntax/parsing errors found in {len(failed_files)} file(s):\n" + "\n".join(
        failed_files
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
            assert res.returncode == 0, f"Ruff F821 undefined variable check failed:\n{res.stdout}\n{res.stderr}"
    except FileNotFoundError:
        pass


def test_all_docs_markdown_file_links_exist_on_disk():
    """
    Scans all Markdown documentation files under docs/ and verifies that every
    local file link (file:///... and relative file references) resolves
    to an existing file on disk.
    """
    root_dir = Path(__file__).resolve().parent.parent
    docs_dir = root_dir / "docs"
    assert docs_dir.exists(), "docs/ directory does not exist!"

    md_files = list(docs_dir.rglob("*.md"))
    assert len(md_files) > 0, "No markdown files found under docs/!"

    link_pattern = re.compile(
        r"\[.*?\]\((file:///[^#\s\)]+|[^:\s\)]+\.(?:md|py|json|sql|txt|sh|csv|yaml|yml))(?:#[^)]*)?\)"
    )

    broken_links = []
    links_checked = 0

    for doc_file in md_files:
        content = doc_file.read_text(encoding="utf-8")
        for match in link_pattern.finditer(content):
            target = match.group(1).strip()
            if target.startswith(("http://", "https://", "mailto:")):
                continue

            links_checked += 1
            if target.startswith("file:///"):
                raw_path = target[8:]
                if re.match(r"^[a-zA-Z]:/", raw_path):
                    target_path = Path(raw_path)
                else:
                    target_path = root_dir / raw_path.lstrip("/")
            else:
                target_path = (doc_file.parent / target).resolve()

            if not target_path.exists():
                broken_links.append(f"{doc_file.relative_to(root_dir)} -> {target}")

    assert not broken_links, f"Found {len(broken_links)} broken file link(s) in documentation:\n" + "\n".join(
        broken_links
    )
    assert links_checked > 0, "No markdown file links were checked!"


def test_no_unittest_mock_introspection_in_production_code():
    """
    Ensures production modules do not inspect test runner mocks (assert_called, mock_calls)
    or probe sys.modules for test monkeypatches.
    """
    root_dir = Path(__file__).resolve().parent.parent
    prod_dirs = [
        root_dir / "engine" / "orchestration",
        root_dir / "engine" / "pipeline",
        root_dir / "engine" / "services",
        root_dir / "engine" / "core",
        root_dir / "engine" / "agents",
        root_dir / "engine" / "nlp",
        root_dir / "engine" / "skills",
        root_dir / "engine" / "adapters",
        root_dir / "config" / "skills",
    ]

    violations = []
    forbidden_tokens = ["assert_called", "mock_calls"]

    for p_dir in prod_dirs:
        if not p_dir.exists():
            continue
        for py_file in p_dir.rglob("*.py"):
            text = py_file.read_text(encoding="utf-8")
            for token in forbidden_tokens:
                if token in text:
                    violations.append(
                        f"{py_file.relative_to(root_dir)} contains forbidden test introspection token '{token}'"
                    )

    assert not violations, "\n".join(violations)


def test_no_monkeypatch_setattr_on_module_paths_or_managers():
    """
    Ensures test files and conftest.py do not use monkeypatch.setattr on module-level path constants
    or manager singletons, enforcing dependency injection and IZVOR_* environment overrides instead.
    """
    import ast

    root_dir = Path(__file__).resolve().parent.parent
    tests_dir = root_dir / "tests"

    forbidden_targets = {
        "DOWNLOADS_DIR",
        "SETTINGS_FILE",
        "SYSTEM_SETTINGS_FILE",
        "SYSTEM_BACKUP_FILE",
        "SYSTEM_AGENT_SETTINGS_FILE",
        "SYSTEM_AGENT_BACKUP_FILE",
        "APP_SETTINGS_FILE",
        "APP_BACKUP_FILE",
        "PRICING_CACHE_FILE",
        "USAGE_STORE_FILE",
        "DEFAULT_DB_PATH",
        "system_settings_manager",
        "app_settings_manager",
    }

    violations = []

    for py_file in tests_dir.rglob("*.py"):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                is_setattr = False
                if isinstance(func, ast.Attribute) and func.attr == "setattr":
                    if isinstance(func.value, ast.Name) and func.value.id == "monkeypatch":
                        is_setattr = True

                if is_setattr and len(node.args) >= 2:
                    target_attr = None
                    arg1 = node.args[1]
                    if isinstance(arg1, ast.Constant) and isinstance(arg1.value, str):
                        target_attr = arg1.value.split(".")[-1]
                    elif isinstance(arg1, ast.Name):
                        target_attr = arg1.id

                    arg0 = node.args[0]
                    if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                        leaf = arg0.value.split(".")[-1]
                        if leaf in forbidden_targets:
                            target_attr = leaf

                    if target_attr and target_attr in forbidden_targets:
                        violations.append(
                            f"{py_file.relative_to(root_dir)}:{node.lineno} uses forbidden monkeypatch.setattr on '{target_attr}'"
                        )

    assert not violations, (
        f"Found {len(violations)} forbidden monkeypatch.setattr calls on path constants/managers:\n"
        + "\n".join(violations)
        + "\nUse manager.configure_paths() or monkeypatch.setenv('IZVOR_*', ...) instead."
    )
