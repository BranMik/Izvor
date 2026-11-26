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
    root_dir = Path(__file__).resolve().parents[2]
    targets = [
        "engine",
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

    # Optional Ruff full check if ruff is installed in the current environment
    try:
        ruff_check = subprocess.run(
            [sys.executable, "-m", "ruff", "--version"],
            cwd=root_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        if ruff_check.returncode == 0:
            res = subprocess.run(
                [sys.executable, "-m", "ruff", "check", "."],
                cwd=root_dir,
                capture_output=True,
                text=True,
                check=False,
            )
            assert res.returncode == 0, f"Ruff check failed:\n{res.stdout}\n{res.stderr}"
    except FileNotFoundError:
        pass


def test_all_docs_markdown_file_links_exist_on_disk():
    """
    Scans all Markdown documentation files under docs/ and root README.md and verifies that every
    local file link (file:///... and relative file references) resolves
    to an existing file on disk.
    """
    root_dir = Path(__file__).resolve().parents[2]
    docs_dir = root_dir / "docs"
    assert docs_dir.exists(), "docs/ directory does not exist!"

    md_files = list(docs_dir.rglob("*.md"))
    readme_file = root_dir / "README.md"
    if readme_file.exists():
        md_files.append(readme_file)
    assert len(md_files) > 0, "No markdown files found under docs/!"

    link_pattern = re.compile(
        r"\[.*?\]\((file:///[^#\s\)]+|[^:\s\)]+\.(?:md|py|json|sql|txt|sh|csv|yaml|yml|png|jpg|jpeg|svg))(?:#[^)]*)?\)"
    )

    broken_links = []
    absolute_uri_links = []
    links_checked = 0

    for doc_file in md_files:
        content = doc_file.read_text(encoding="utf-8")
        for match in link_pattern.finditer(content):
            target = match.group(1).strip()
            if target.startswith(("http://", "https://", "mailto:")):
                continue

            if re.match(r"^file:///[a-zA-Z]:", target):
                absolute_uri_links.append(f"{doc_file.relative_to(root_dir)} -> {target}")

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
    assert not absolute_uri_links, (
        f"Found {len(absolute_uri_links)} absolute machine-specific file:/// URI(s) in documentation "
        "(use repo-relative links like file:///path instead):\n" + "\n".join(absolute_uri_links)
    )
    assert links_checked > 0, "No markdown file links were checked!"


def test_no_unittest_mock_introspection_in_production_code():
    """
    Ensures production modules do not inspect test runner mocks (assert_called, mock_calls)
    or probe sys.modules for test monkeypatches.
    """
    root_dir = Path(__file__).resolve().parents[2]
    prod_dirs = [
        root_dir / "engine" / "orchestration",
        root_dir / "engine" / "pipeline",
        root_dir / "engine" / "services",
        root_dir / "engine" / "core",
        root_dir / "engine" / "config_store",
        root_dir / "engine" / "agents",
        root_dir / "engine" / "nlp",
        root_dir / "engine" / "skills",
        root_dir / "engine" / "adapters",
        root_dir / "engine" / "api",
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

    root_dir = Path(__file__).resolve().parents[2]
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
        "WORKFLOWS_DIR",
        "ENGINE_WORKFLOWS_DIR",
        "THREAD_NOTES_DIR",
        "AGENTS_RESOURCES_DIR",
        "WORKSPACE_DIR",
        "SLACK_BOT_TOKEN",
        "SLACK_SIGNING_SECRET",
        "ADMIN_USERNAME",
        "ADMIN_PASSWORD",
        "AUDIT_HMAC_SECRET",
        "system_settings_manager",
        "app_settings_manager",
        "workflow_manager",
        "db_manager",
        "usage_tracker",
        "pricing_service",
        "thread_notes_writer",
        "flat_history_recorder",
        "local_tools",
        "mcp_client_manager",
        "store_file",
        "thread_skills_manager",
        "spend_governance",
        "audit_logger",
        "slack_async_client",
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
        + "\nUse manager.configure_paths() or dependency injection overrides instead."
    )


def test_no_monkeypatch_setenv_or_delenv():
    """
    Ensures that test files and fixtures do not use monkeypatch.setenv or monkeypatch.delenv,
    enforcing clean parameter injection or manager configure/override APIs instead.
    """
    import ast

    root_dir = Path(__file__).resolve().parents[2]
    tests_dir = root_dir / "tests"

    violations = []

    for py_file in tests_dir.rglob("*.py"):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr in ("setenv", "delenv"):
                    if isinstance(func.value, ast.Name) and func.value.id == "monkeypatch":
                        violations.append(
                            f"{py_file.relative_to(root_dir)}:{node.lineno} uses forbidden monkeypatch.{func.attr}"
                        )

    assert not violations, (
        f"Found {len(violations)} forbidden monkeypatch.setenv/delenv calls in test suite:\n"
        + "\n".join(violations)
        + "\nUse manager configure_paths(), parameter injection, or config override functions instead."
    )


def test_zero_monkeypatch_usage_in_test_suite():
    """
    Enforces repository-wide prohibition of pytest monkeypatch fixture and monkeypatch calls.
    Tests must use dependency injection, manager configure_paths(), IZVOR_* overrides,
    or unittest.mock.patch instead.
    """
    import ast

    root_dir = Path(__file__).resolve().parents[2]
    tests_dir = root_dir / "tests"

    violations = []

    for py_file in tests_dir.rglob("*.py"):
        if py_file.name == "test_code_hygiene.py":
            continue

        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            # Check function definitions for 'monkeypatch' parameter
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for arg in node.args.args + node.args.kwonlyargs:
                    if arg.arg == "monkeypatch":
                        violations.append(
                            f"{py_file.relative_to(root_dir)}:{node.lineno} function '{node.name}' requests 'monkeypatch' fixture parameter"
                        )

            # Check calls on monkeypatch identifier
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    if isinstance(func.value, ast.Name) and func.value.id == "monkeypatch":
                        violations.append(
                            f"{py_file.relative_to(root_dir)}:{node.lineno} calls forbidden 'monkeypatch.{func.attr}'"
                        )

    assert not violations, (
        f"Found {len(violations)} forbidden monkeypatch usages in test suite:\n"
        + "\n".join(violations)
        + "\nUse manager configure_paths(), dependency injection, or unittest.mock.patch instead."
    )


def test_no_duplicate_module_constants_in_models():
    """
    Enforces that module-level constants in engine/core/models.py are never defined
    more than once (e.g. REQUIRED_DEFAULT_WORKFLOW_IDS).
    """
    root_dir = Path(__file__).resolve().parents[2]
    models_file = root_dir / "engine" / "core" / "models.py"
    assert models_file.exists(), "engine/core/models.py does not exist!"

    tree = ast.parse(models_file.read_text(encoding="utf-8"), filename=str(models_file))
    top_level_targets: dict[str, list[int]] = {}

    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    top_level_targets.setdefault(target.id, []).append(node.lineno)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                top_level_targets.setdefault(node.target.id, []).append(node.lineno)

    duplicates = {name: lines for name, lines in top_level_targets.items() if len(lines) > 1}
    assert not duplicates, (
        "Found duplicate top-level constant assignment(s) in engine/core/models.py:\n"
        + "\n".join(f"- {name} at lines {lines}" for name, lines in duplicates.items())
    )


def test_consistent_fastapi_testclient_imports():
    """
    Guarantees that test modules import TestClient from fastapi.testclient,
    preventing inconsistent starlette.testclient imports.
    """
    root_dir = Path(__file__).resolve().parents[2]
    tests_dir = root_dir / "tests"

    starlette_testclient_usages: list[str] = []

    for py_file in tests_dir.rglob("*.py"):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "starlette.testclient":
                for alias in node.names:
                    if alias.name == "TestClient":
                        starlette_testclient_usages.append(
                            f"{py_file.relative_to(root_dir)}:{node.lineno} imports TestClient from starlette.testclient"
                        )

    assert not starlette_testclient_usages, (
        f"Found {len(starlette_testclient_usages)} starlette.testclient TestClient import(s):\n"
        + "\n".join(starlette_testclient_usages)
        + "\nImport from fastapi.testclient instead."
    )


def test_zero_patch_object_and_patch_dict_in_test_suite():
    """
    Plan 173 AST Hygiene Guardrail:
    Enforces repository-wide prohibition of `patch.object` and `patch.dict`.
    Tests must use pure dependency injection, constructor parameters, manager configure_paths(),
    or config override APIs instead.
    """
    root_dir = Path(__file__).resolve().parents[2]
    tests_dir = root_dir / "tests"

    violations: list[str] = []

    for py_file in tests_dir.rglob("*.py"):
        if py_file.name == "test_code_hygiene.py":
            continue

        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                # Detect patch.object or patch.dict or mock.patch.object / mock.patch.dict
                if isinstance(func, ast.Attribute) and func.attr in ("object", "dict"):
                    val = func.value
                    is_patch_call = False
                    if isinstance(val, ast.Name) and val.id == "patch":
                        is_patch_call = True
                    elif isinstance(val, ast.Attribute) and val.attr == "patch":
                        is_patch_call = True

                    if is_patch_call:
                        violations.append(
                            f"{py_file.relative_to(root_dir)}:{node.lineno} uses forbidden 'patch.{func.attr}'"
                        )

    assert not violations, (
        f"Found {len(violations)} forbidden patch.object/patch.dict call(s) in test suite:\n"
        + "\n".join(violations)
        + "\nReplace with pure dependency injection, manager configure_paths(), or configure_* overrides."
    )


def test_no_patch_on_module_paths_or_managers():
    """
    Plan 173 AST Hygiene Guardrail:
    Guarantees that `patch(...)` is never used to target module-level path constants,
    security constants, or manager/service singletons. Tests must use manager configure_paths(),
    configure_* overrides, or pure dependency injection instead.
    """
    root_dir = Path(__file__).resolve().parents[2]
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
        "WORKFLOWS_DIR",
        "ENGINE_WORKFLOWS_DIR",
        "THREAD_NOTES_DIR",
        "AGENTS_RESOURCES_DIR",
        "WORKSPACE_DIR",
        "SLACK_BOT_TOKEN",
        "SLACK_SIGNING_SECRET",
        "ADMIN_USERNAME",
        "ADMIN_PASSWORD",
        "AUDIT_HMAC_SECRET",
        "system_settings_manager",
        "app_settings_manager",
        "workflow_manager",
        "db_manager",
        "usage_tracker",
        "thread_notes_writer",
        "flat_history_recorder",
        "mcp_client_manager",
        "thread_skills_manager",
        "spend_governance",
        "audit_logger",
        "slack_async_client",
    }

    violations: list[str] = []

    for py_file in tests_dir.rglob("*.py"):
        if py_file.name == "test_code_hygiene.py":
            continue

        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                is_patch = False
                if isinstance(func, ast.Name) and func.id == "patch":
                    is_patch = True
                elif isinstance(func, ast.Attribute) and func.attr == "patch":
                    is_patch = True

                if is_patch and node.args:
                    first_arg = node.args[0]
                    target_str = None
                    if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                        target_str = first_arg.value

                    if target_str:
                        leaf = target_str.split(".")[-1]
                        if leaf in forbidden_targets:
                            violations.append(
                                f"{py_file.relative_to(root_dir)}:{node.lineno} uses forbidden patch('{target_str}')"
                            )

    assert not violations, (
        f"Found {len(violations)} forbidden patch() calls on path constants or singletons:\n"
        + "\n".join(violations)
        + "\nUse manager configure_paths(), parameter injection, or configure_* overrides instead."
    )


def _has_future_annotations(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )


def _collect_annotation_nodes(tree: ast.Module) -> list[ast.AST]:
    annotations: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
            for arg in args:
                if arg.annotation is not None:
                    annotations.append(arg.annotation)
            if node.args.vararg and node.args.vararg.annotation is not None:
                annotations.append(node.args.vararg.annotation)
            if node.args.kwarg and node.args.kwarg.annotation is not None:
                annotations.append(node.args.kwarg.annotation)
            if node.returns is not None:
                annotations.append(node.returns)
        elif isinstance(node, ast.AnnAssign) and node.annotation is not None:
            annotations.append(node.annotation)
    return annotations


def test_no_eager_string_union_annotations_without_future():
    """
    Ensures Python 3.11-3.13 compatibility when developing on Python 3.14+ (PEP 649):
    forbids `X | "Y"` / `"X" | Y` union annotations unless `from __future__ import annotations`
    is present at module level.
    """
    root_dir = Path(__file__).resolve().parents[2]
    targets = ["engine", "tests", "config/skills", "main.py"]
    violations: list[str] = []

    for target_name in targets:
        target_path = root_dir / target_name
        if not target_path.exists():
            continue
        files = [target_path] if target_path.is_file() else list(target_path.rglob("*.py"))
        for py_file in files:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            if _has_future_annotations(tree):
                continue
            for ann in _collect_annotation_nodes(tree):
                for sub in ast.walk(ann):
                    if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr):
                        if any(isinstance(side, ast.Constant) and isinstance(side.value, str) for side in (sub.left, sub.right)):
                            violations.append(
                                f"{py_file.relative_to(root_dir)}:{ann.lineno} -> {ast.unparse(ann)}"
                            )
                            break

    assert not violations, (
        "Found union annotations mixing `|` with string literals without "
        "`from __future__ import annotations` (raises TypeError on Python 3.11-3.13):\n"
        + "\n".join(violations)
    )



