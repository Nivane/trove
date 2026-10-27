"""Read-only AST helpers for the architecture contracts (fitness functions).

Everything here is a pure function over source text: no trove imports, no
network, no filesystem writes. The detectors built on these helpers are
**ratchets, not sandboxes** — they catch a new "proper" path into a forbidden
layer or a new writer of a frozen artifact. Dynamic imports, ``getattr``
tricks and path strings assembled at runtime are documented blind spots.

`resolve_imports` is the one piece the older posture tests lack: it resolves
relative imports to absolute module names, so ``from ...llm import gateway``
cannot slip past a prefix check.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterable, Mapping

ROOT = Path(__file__).resolve().parents[2]
TROVE = ROOT / "trove"
SCRIPTS = ROOT / "scripts"

Sources = Mapping[str, str]


def py_sources(*prefixes: str) -> dict[str, str]:
    """Return ``{repo-relative path: source}`` for Python files under roots.

    ``prefixes`` are repo-relative files or directories. With no arguments the
    whole ``trove/`` and ``scripts/`` trees are read (tests are excluded: they
    are allowed to construct anything).
    """
    roots = prefixes or ("trove", "scripts")
    out: dict[str, str] = {}
    for prefix in roots:
        base = ROOT / prefix
        paths = [base] if base.is_file() else sorted(base.rglob("*.py"))
        for path in paths:
            rel = path.relative_to(ROOT).as_posix()
            out[rel] = path.read_text(encoding="utf-8")
    return out


def under(rel: str, prefix: str) -> bool:
    """True when ``rel`` is ``prefix`` itself or lives under it."""
    return rel == prefix or rel.startswith(prefix.rstrip("/") + "/")


def module_name(rel: str) -> str:
    """``trove/foo/bar.py`` → ``trove.foo.bar``; ``__init__.py`` → its package."""
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def package_of(rel: str) -> str:
    if rel.endswith("__init__.py"):
        return module_name(rel)
    return module_name(rel).rpartition(".")[0]


def resolve_import_targets(rel: str, node: ast.AST) -> set[str]:
    """Absolute module names named by one ``Import`` / ``ImportFrom`` node.

    Relative imports are resolved against ``rel``'s package; a multi-alias
    statement yields one entry per module and per imported name (``*``
    excluded), matching :func:`resolve_imports`.
    """
    if isinstance(node, ast.Import):
        return {alias.name for alias in node.names}
    if not isinstance(node, ast.ImportFrom):
        return set()

    package = package_of(rel)
    out: set[str] = set()
    if node.level:
        base = package
        for _ in range(node.level - 1):
            base = base.rpartition(".")[0]
        module = f"{base}.{node.module}" if node.module else base
    else:
        module = node.module or ""
    if module:
        out.add(module)
    for alias in node.names:
        if alias.name != "*" and module:
            out.add(f"{module}.{alias.name}")
    return out


def resolve_imports(rel: str, tree: ast.Module) -> set[str]:
    """Absolute module names imported by ``tree``, relative imports resolved.

    ``from ..llm import gateway`` (level 2) from ``trove/services/decision/x.py``
    resolves to ``trove.services.llm.gateway`` — the module that a one-dot
    absolute-prefix check would miss. ``from . import y`` resolves to
    ``<package>.y``.
    """
    out: set[str] = set()
    for node in ast.walk(tree):
        out |= resolve_import_targets(rel, node)
    return out


def import_bindings(rel: str, tree: ast.Module) -> dict[str, str]:
    """Local name → absolute target for imports (classes and modules)."""
    bound: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package_of(rel)
                for _ in range(node.level - 1):
                    base = base.rpartition(".")[0]
                module = f"{base}.{node.module}" if node.module else base
            else:
                module = node.module or ""
            for alias in node.names:
                if alias.name != "*" and module:
                    bound[alias.asname or alias.name] = f"{module}.{alias.name}"
    return bound


def constructor_targets(rel: str, tree: ast.Module) -> set[str]:
    """Qualified names of classes instantiated in this module.

    Only the shapes the repo actually uses are resolved: ``Store(...)``,
    ``alias.Store(...)`` and ``Store.attr(...)`` where the head is imported.
    Anything else (dynamic dispatch) is a documented blind spot.
    """
    bound = import_bindings(rel, tree)
    local = {
        node.name for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    module = module_name(rel)
    targets: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            targets.add(bound.get(func.id, f"{module}.{func.id}" if func.id in local else func.id))
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            head = bound.get(func.value.id, func.value.id)
            targets.add(f"{head}.{func.attr}")
    return targets


def canonical_class(
    target: str, definitions: Mapping[str, str], sources: Sources
) -> str | None:
    """Resolve a re-exported class name to its defining qualified name.

    ``from trove.services.action import ActionStore`` binds the package-level
    name; the class is defined in ``trove.services.action.store``. One hop of
    ``__init__.py`` re-export is followed (recursively); anything else is not
    guessed.
    """
    if target in definitions:
        return target
    module, _, name = target.rpartition(".")
    init_rel = module.replace(".", "/") + "/__init__.py"
    source = sources.get(init_rel)
    if source is None:
        return None
    next_target = import_bindings(init_rel, ast.parse(source)).get(name)
    if not next_target or next_target == target:
        return None
    if next_target in definitions:
        return next_target
    return canonical_class(next_target, definitions, sources)


def class_definitions(sources: Sources, suffix: str = "Store") -> dict[str, str]:
    """``{qualified class name: defining module}`` for classes named ``*suffix``."""
    found: dict[str, str] = {}
    for rel, source in sources.items():
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover - a broken file fails elsewhere
            continue
        module = module_name(rel)
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name.endswith(suffix):
                found[f"{module}.{node.name}"] = module
    return found


def diff(expected: Iterable[str], actual: Iterable[str]) -> tuple[list[str], list[str]]:
    e, a = set(expected), set(actual)
    return sorted(a - e), sorted(e - a)


def assert_frozen(expected: Iterable[str], actual: Iterable[str], label: str) -> None:
    """Bidirectional frozen-set assertion; message names both directions."""
    extra, missing = diff(expected, actual)
    assert not missing and not extra, (
        f"{label} drifted — missing={missing} extra={extra}"
    )


def node_exists(test_rel: str, class_name: str | None, func_name: str) -> bool:
    """True when ``test_rel`` defines ``func_name`` (optionally in a class)."""
    path = ROOT / test_rel
    if not path.is_file():
        return False
    tree = ast.parse(path.read_text(encoding="utf-8"))
    if class_name is None:
        return _defines(tree.body, func_name)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return _defines(node.body, func_name)
    return False


def _defines(nodes: list[ast.stmt], name: str) -> bool:
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        for node in nodes
    )


def parse_node_id(node_id: str) -> tuple[str, str | None, str]:
    """``tests/x.py::Class::test_y`` → ``(file, Class, test_y)``."""
    parts = node_id.split("::")
    if len(parts) == 2:
        return parts[0], None, parts[1]
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    raise AssertionError(f"unrecognised pytest node id: {node_id}")
