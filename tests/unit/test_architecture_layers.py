import ast
import functools
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]

EXCLUDED_DIRS = frozenset({
    ".venv",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "deprecated",
    "static_backup",
    "Izvor-app",
    "system_resources",
    "flat-histories",
    "__pycache__",
    "data",
    "agents_resources",
})


@functools.lru_cache(maxsize=1)
def _get_python_source_files() -> tuple[Path, ...]:
    py_files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(PROJECT_ROOT):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
        for fname in filenames:
            if fname.endswith(".py"):
                py_files.append(Path(dirpath) / fname)
    return tuple(py_files)


@functools.lru_cache(maxsize=None)
def _parse_ast(py_file: Path) -> ast.Module | None:
    try:
        return ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    except (SyntaxError, UnicodeDecodeError):
        return None


def test_core_does_not_import_adapters():
    """Verify that core/ modules never import from adapters/ layer."""
    core_dir = PROJECT_ROOT / "engine" / "core"
    violations = []

    for py_file in core_dir.glob("*.py"):
        tree = _parse_ast(py_file)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("adapters"):
                        violations.append(f"{py_file.name}:{node.lineno} imports {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith("adapters"):
                    violations.append(f"{py_file.name}:{node.lineno} imports from {node.module}")

    assert not violations, f"Layer violation: core/ cannot import from adapters/: {violations}"


def test_core_does_not_import_higher_layers():
    """
    Strict Layer Inversion: Verify that core/ modules have ZERO imports (top-level or deferred)
    from higher layers: config_store, adapters, orchestration, agents, services, pipeline, skills, prompts.
    """
    core_dir = PROJECT_ROOT / "engine" / "core"
    forbidden_layers = {
        "config_store",
        "adapters",
        "api",
        "orchestration",
        "agents",
        "services",
        "pipeline",
        "skills",
        "prompts",
    }
    violations = []

    for py_file in core_dir.glob("*.py"):
        tree = _parse_ast(py_file)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_pkg = alias.name.split(".")[0]
                    if root_pkg in forbidden_layers:
                        violations.append(f"{py_file.name}:{node.lineno} imports {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    root_pkg = node.module.split(".")[0]
                    if root_pkg in forbidden_layers:
                        violations.append(f"{py_file.name}:{node.lineno} imports from {node.module}")

    assert not violations, f"Layer violation: core/ cannot import from higher layers {forbidden_layers}: {violations}"


def test_core_top_level_does_not_import_orchestration():
    """Verify that core/ modules have zero top-level imports from orchestration/."""
    core_dir = PROJECT_ROOT / "engine" / "core"
    violations = []

    for py_file in core_dir.glob("*.py"):
        tree = _parse_ast(py_file)
        if tree is None:
            continue
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
        tree = _parse_ast(py_file)
        if tree is None:
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
        tree = _parse_ast(py_file)
        if tree is None:
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
    )

    for py_file in _get_python_source_files():
        rel_path = py_file.relative_to(PROJECT_ROOT)
        if any(rel_path.is_relative_to(p) for p in allowed_prefixes):
            continue
        tree = _parse_ast(py_file)
        if tree is None:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                if node.attr in ("chat_postMessage", "chat_update", "chat_postEphemeral"):
                    violations.append(f"{rel_path}:{node.lineno} directly calls {node.attr}")

    assert not violations, (
        "Security / Architecture violation: raw Slack chat_* calls outside services/slack/:\n"
        + "\n".join(violations)
    )


def test_strict_five_tier_dag_layer_boundaries():
    """
    TKT-1225: Enforce strict 5-tier downward DAG across engine/ and main.py:
      L1   : core
      L1.5 : config_store
      L2   : services, skills, nlp, prompts, pipeline
      L3   : orchestration, agents
      L4   : adapters, api, main
    """
    layer_tiers: dict[str, float] = {
        "core": 1.0,
        "config_store": 1.5,
        "services": 2.0,
        "skills": 2.0,
        "nlp": 2.0,
        "prompts": 2.0,
        "pipeline": 2.0,
        "orchestration": 3.0,
        "agents": 3.0,
        "adapters": 4.0,
        "api": 4.0,
    }
    engine_dir = PROJECT_ROOT / "engine"
    violations: list[str] = []

    py_files = list(engine_dir.rglob("*.py")) + [PROJECT_ROOT / "main.py"]
    for py_file in py_files:
        rel_path = py_file.relative_to(PROJECT_ROOT)
        if py_file.name == "main.py":
            source_layer = "main"
            source_tier = 4.0
        else:
            rel_engine = py_file.relative_to(engine_dir)
            source_layer = rel_engine.parts[0]
            if source_layer not in layer_tiers:
                continue
            source_tier = layer_tiers[source_layer]

        tree = _parse_ast(py_file)
        if tree is None:
            continue
        for node in ast.walk(tree):
            imported_mods: list[str] = []
            if isinstance(node, ast.Import):
                imported_mods = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported_mods = [node.module]

            for mod_name in imported_mods:
                parts = mod_name.split(".")
                target_pkg = parts[1] if parts[0] == "engine" and len(parts) > 1 else parts[0]
                if target_pkg in layer_tiers and layer_tiers[target_pkg] > source_tier:
                    violations.append(
                        f"{rel_path}:{node.lineno} (tier {source_tier} '{source_layer}') "
                        f"upwardly imports '{mod_name}' (tier {layer_tiers[target_pkg]} '{target_pkg}')"
                    )

    assert not violations, (
        "Architecture violation: upward dependency edge(s) detected across 5-tier DAG:\n"
        + "\n".join(violations)
    )


def test_api_and_adapters_do_not_cross_import():
    """
    Plan 210: Enforce lateral isolation between first-party HTTP transport (engine/api/)
    and external messaging platform adapters (engine/adapters/).
    """
    engine_dir = PROJECT_ROOT / "engine"
    boundary_pairs = (
        ("api", "adapters"),
        ("adapters", "api"),
    )
    violations: list[str] = []

    for source_pkg, forbidden_pkg in boundary_pairs:
        source_dir = engine_dir / source_pkg
        if not source_dir.exists():
            continue
        for py_file in source_dir.rglob("*.py"):
            rel_path = py_file.relative_to(PROJECT_ROOT)
            tree = _parse_ast(py_file)
        if tree is None:
            continue
            for node in ast.walk(tree):
                imported_mods: list[str] = []
                if isinstance(node, ast.Import):
                    imported_mods = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    imported_mods = [node.module]

                for mod_name in imported_mods:
                    parts = mod_name.split(".")
                    target_pkg = parts[1] if parts[0] == "engine" and len(parts) > 1 else parts[0]
                    if target_pkg == forbidden_pkg:
                        violations.append(
                            f"{rel_path}:{node.lineno} ('{source_pkg}') cross-imports '{mod_name}' ('{forbidden_pkg}')"
                        )

    assert not violations, (
        "Architecture violation: lateral cross-import between engine/api/ and engine/adapters/:\n"
        + "\n".join(violations)
    )

