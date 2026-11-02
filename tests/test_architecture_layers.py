import ast
from pathlib import Path



PROJECT_ROOT = Path(__file__).resolve().parent.parent

EXCLUDED_DIRS = {
    ".venv",
    ".git",
    "deprecated",
    "static_backup",
    "Izvor-app",
    "system_resources",
    "flat-histories",
    "__pycache__",
}


def _get_python_source_files() -> list[Path]:
    py_files = []
    for path in PROJECT_ROOT.rglob("*.py"):
        if any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        py_files.append(path)
    return py_files


def test_core_does_not_import_adapters():
    """Verify that core/ modules never import from adapters/ layer."""
    core_dir = PROJECT_ROOT / "engine" / "core"
    violations = []

    for py_file in core_dir.glob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("adapters"):
                        violations.append(f"{py_file.name}:{node.lineno} imports {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith("adapters"):
                    violations.append(f"{py_file.name}:{node.lineno} imports from {node.module}")

    assert not violations, f"Layer violation: core/ cannot import from adapters/: {violations}"


def test_core_top_level_does_not_import_orchestration():
    """Verify that core/ modules have zero top-level imports from orchestration/."""
    core_dir = PROJECT_ROOT / "engine" / "core"
    violations = []

    for py_file in core_dir.glob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        # Inspect only top-level statements
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("orchestration"):
                        violations.append(f"{py_file.name}:{node.lineno} top-level imports {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith("orchestration"):
                    violations.append(f"{py_file.name}:{node.lineno} top-level imports from {node.module}")

    assert not violations, f"Layer violation: core/ has top-level orchestration imports: {violations}"


def test_no_await_inside_sync_db_transactions():
    """
    Verify that no `await` expression appears inside a `with get_db_transaction(...)` block.
    Mixing await inside synchronous SQLite transactions risks connection lock deadlocks.
    """
    violations = []

    for py_file in _get_python_source_files():
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.With):
                # Check if with items reference get_db_transaction
                is_db_tx = False
                for item in node.items:
                    expr = item.context_expr
                    if isinstance(expr, ast.Call):
                        func = expr.func
                        func_name = getattr(func, "id", None) or getattr(func, "attr", None)
                        if func_name == "get_db_transaction":
                            is_db_tx = True
                            break

                if is_db_tx:
                    for child in ast.walk(node):
                        if isinstance(child, ast.Await):
                            rel_path = py_file.relative_to(PROJECT_ROOT)
                            violations.append(
                                f"{rel_path}:{child.lineno} contains 'await' inside get_db_transaction"
                            )

    assert not violations, "Concurrency violation: await inside synchronous DB transaction:\n" + "\n".join(violations)


def test_no_bare_except_pass():
    """
    Verify that there are no bare `except:` statements without exception type across all source files.
    Bare `except:` catches BaseException, interrupting SystemExit and KeyboardInterrupt.
    """
    violations = []

    for py_file in _get_python_source_files():
        if "tests" in py_file.parts:
            continue
        try:
            content = py_file.read_text(encoding="utf-8")
            tree = ast.parse(content, filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError):
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                if node.type is None:
                    rel_path = py_file.relative_to(PROJECT_ROOT)
                    violations.append(
                        f"{rel_path}:{node.lineno} bare 'except:' without exception type"
                    )

    assert not violations, (
        "Code hygiene violation: bare 'except:' without exception type:\n"
        + "\n".join(violations[:20])
    )


def test_slack_client_chat_not_called_outside_allowed_modules():
    """
    TKT-1009: Outbound Slack chat messages (chat_postMessage, chat_update, chat_postEphemeral)
    must always pass through sanitized wrappers in services/slack/ and never be called directly
    on AsyncWebClient / raw client instances across orchestration, adapters, or agents.
    """
    violations = []
    allowed_prefixes = (
        Path("engine") / "services" / "slack",
        Path("tests"),
        Path("e2e_testing_harness"),
    )

    for py_file in _get_python_source_files():
        rel_path = py_file.relative_to(PROJECT_ROOT)
        if any(rel_path.is_relative_to(p) for p in allowed_prefixes):
            continue
        try:
            content = py_file.read_text(encoding="utf-8")
            tree = ast.parse(content, filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError):
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                if node.attr in ("chat_postMessage", "chat_update", "chat_postEphemeral"):
                    violations.append(f"{rel_path}:{node.lineno} directly calls {node.attr}")

    assert not violations, (
        "Security / Architecture violation: raw Slack chat_* calls outside services/slack/:\n"
        + "\n".join(violations)
    )

