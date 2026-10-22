import ast
from pathlib import Path


def _is_transaction_cm(node: ast.AST) -> bool:
    """Checks if an AST node represents get_db_transaction(...) or .transaction(...)."""
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id == "get_db_transaction":
            return True
        if isinstance(func, ast.Attribute) and func.attr == "transaction":
            return True
    return False


def test_zero_await_inside_database_transactions():
    """
    AST Static Analysis:
    Asserts that NO async 'await' expressions are executed inside SQLite transaction blocks.
    Awaits inside SQLite transactions block the event loop, cause deadlock with concurrent writers,
    and hold open WAL write locks while waiting on I/O.
    """
    project_root = Path(__file__).resolve().parent.parent
    source_dirs = ["core", "orchestration", "services", "adapters", "agents", "pipeline"]

    violations: list[str] = []

    for src_dir in source_dirs:
        dir_path = project_root / src_dir
        if not dir_path.is_dir():
            continue
        for py_file in dir_path.rglob("*.py"):
            try:
                tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            except Exception:
                continue

            for node in ast.walk(tree):
                if isinstance(node, (ast.With, ast.AsyncWith)):
                    is_tx = any(_is_transaction_cm(item.context_expr) for item in node.items)
                    if is_tx:
                        # Check body for await expressions
                        for child in ast.walk(node):
                            if isinstance(child, ast.Await):
                                rel_path = py_file.relative_to(project_root)
                                violations.append(f"{rel_path}:{child.lineno}")

    assert not violations, (
        f"Found {len(violations)} illegal 'await' statement(s) inside SQLite transaction blocks:\n"
        + "\n".join(f"  - {v}" for v in violations)
    )
