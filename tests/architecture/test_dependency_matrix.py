"""Top-level dependency-direction matrix (layers + frozen reverse edges).

The audit found six bidirectional top-level pairs, with the high-churn
``graphs``/``session`` code sitting on a cycle. This module turns the intended
direction into a global rule instead of a handful of special-case tests:

* components are ranked bottom-up (``core`` first, ``main`` last); an import is
  allowed only when the target ranks *below* the source;
* every remaining upward import is frozen as an exact count per package pair;
* sibling cycles (depth-2 nodes inside one top-level package) are frozen as a
  set of cycle memberships.

The ratchet only forbids growth. Removing a frozen import requires shrinking the
manifest in the same commit (the same discipline as ``assert_frozen``).

**These are ratchets, not sandboxes.** Imports assembled at runtime
(``importlib`` / ``__import__`` / ``getattr``) have no AST shape and slip
through; ``scripts/`` and ``tests/`` are outside the matrix (entry points above
``main``). What the ratchet catches is the ordinary way a new edge appears:
someone writes ``from trove.services...`` in ``trove/core``.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from collections.abc import Mapping

import pytest

from tests.helpers import arch

Sources = Mapping[str, str]
Trees = Mapping[str, ast.Module]

#: Bottom-up order. ``rank(dst) < rank(src)`` is the only legal direction.
LAYERS: tuple[str, ...] = (
    "core",
    "storage",
    "demo",
    "tracing",
    "llm",
    "prompts",
    "services",
    "workflow",
    "agent",
    "eval",
    "api",
    "mcp",
    "cli",
    "main",
)
RANK: dict[str, int] = {name: rank for rank, name in enumerate(LAYERS)}

#: Upward imports that exist today, frozen to an exact statement count. The
#: manifest records only the upward side of each bidirectional pair; downward
#: imports are allowed by the rank rule and need no entry.
#:
#: Reasons:
#:   core -> services       config.py needs MemoryConfig (moved to core in C2)
#:   llm -> services        deterministic error classification (services.errors)
#:   tracing -> llm         pure cache-field helpers (moved to core in C2)
#:   workflow -> agent      output.py renders agent answer-source/confidence
#:   services -> workflow   coverage replay / time parsing / context score
#:   cli -> main            entry points re-enter the composition root
FROZEN_UPWARD_IMPORTS: dict[tuple[str, str], int] = {
    ("core", "services"): 1,
    ("llm", "services"): 2,
    ("tracing", "llm"): 1,
    ("workflow", "agent"): 4,
    ("services", "workflow"): 13,
    ("cli", "main"): 5,
}

#: Depth-2 sibling cycles (a set of node names per cycle). Only services has
#: cycles today; any new cycle or membership change is red.
FROZEN_SIBLING_CYCLES: frozenset[str] = frozenset({
    "services: analysis, authz, datasource, decision, drift, kb, memory, "
    "retrieval, semantic_layer, sql",
    "services: extensions, presets, validate",
})

REAL_TREES: Trees = {
    rel: ast.parse(source) for rel, source in arch.py_sources("trove").items()
}


def _trees(files: dict[str, str]) -> Trees:
    return {rel: ast.parse(source) for rel, source in files.items()}


def _top_component(rel: str) -> str | None:
    """``trove/core/config.py`` → ``core``; ``trove/main.py`` → ``main``.

    ``trove/__init__.py`` is the package root, not a component. Data-only
    directories (``trove/presets``) have no ``.py`` files, so they never appear.
    """
    parts = rel.split("/")
    if len(parts) < 2 or parts[0] != "trove":
        return None
    if len(parts) == 2:
        name = parts[1]
        return None if name == "__init__.py" else name[:-3]
    return parts[1]


def _discover_components(trees: Trees) -> set[str]:
    return {name for rel in trees if (name := _top_component(rel)) is not None}


def _target_components(rel: str, node: ast.AST) -> set[str]:
    """Top-level components named by one import statement (deduped)."""
    out: set[str] = set()
    for target in arch.resolve_import_targets(rel, node):
        parts = target.split(".")
        if len(parts) >= 2 and parts[0] == "trove" and parts[1] in RANK:
            out.add(parts[1])
    return out


def detect_upward_imports(trees: Trees) -> dict[tuple[str, str], list[str]]:
    """``{(src, dst): ["file:line", ...]}`` for every upward import statement.

    One count per (statement, target package): a statement touching two target
    packages contributes one site to each; aliases of the same package count
    once. Function-local and ``TYPE_CHECKING`` imports are counted too — the
    matrix governs namespace coupling, not import timing.
    """
    found: dict[tuple[str, str], list[str]] = defaultdict(list)
    for rel, tree in trees.items():
        src = _top_component(rel)
        if src is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for dst in _target_components(rel, node):
                if dst != src and RANK[dst] > RANK[src]:
                    found[(src, dst)].append(f"{rel}:{node.lineno}")
    return dict(found)


def _depth2(module: str) -> str | None:
    """``trove.services.kb.service`` → ``trove.services.kb``; roots → None.

    A two-part module (``trove.services`` / ``trove.main``) is a package root or
    top-level module, not a depth-2 sibling node, so it never enters the graph.
    """
    parts = module.split(".")
    if len(parts) < 3 or parts[0] != "trove":
        return None
    return ".".join(parts[:3])


def _cycle_key(nodes: set[str]) -> str:
    tops = {node.split(".")[1] for node in nodes}
    assert len(tops) == 1, f"cycle spans components: {sorted(nodes)}"
    top = tops.pop()
    short = sorted(node.split(".", 2)[2] for node in nodes)
    return f"{top}: {', '.join(short)}"


def _tarjan(graph: Mapping[str, set[str]]) -> list[set[str]]:
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    out: list[set[str]] = []

    def visit(node: str) -> None:
        index[node] = low[node] = len(index)
        stack.append(node)
        on_stack.add(node)
        for nxt in sorted(graph.get(node, ())):
            if nxt not in index:
                visit(nxt)
                low[node] = min(low[node], low[nxt])
            elif nxt in on_stack:
                low[node] = min(low[node], index[nxt])
        if low[node] == index[node]:
            component: set[str] = set()
            while True:
                member = stack.pop()
                on_stack.discard(member)
                component.add(member)
                if member == node:
                    break
            if len(component) > 1:
                out.append(component)

    for node in sorted(graph):
        if node not in index:
            visit(node)
    return out


def detect_sibling_cycles(trees: Trees) -> set[str]:
    """Depth-2 cycles inside each top-level package, as display strings."""
    graph: dict[str, set[str]] = defaultdict(set)
    for rel, tree in trees.items():
        src = _top_component(rel)
        if src is None:
            continue
        src_node = _depth2(arch.module_name(rel))
        if src_node is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for target in arch.resolve_import_targets(rel, node):
                parts = target.split(".")
                if len(parts) < 2 or parts[0] != "trove" or parts[1] != src:
                    continue
                dst_node = _depth2(target)
                if dst_node and dst_node != src_node:
                    graph[src_node].add(dst_node)
    return {_cycle_key(component) for component in _tarjan(graph)}


# ── the real tree ─────────────────────────────────────────────────────────


def test_layers_cover_every_top_level_component() -> None:
    discovered = _discover_components(REAL_TREES)
    assert discovered == set(LAYERS), (
        "top-level trove components changed; register each in LAYERS with a "
        f"rank. discovered={sorted(discovered)}, registered={sorted(LAYERS)}"
    )


def test_package_root_imports_nothing() -> None:
    imports = [
        node
        for node in ast.walk(REAL_TREES["trove/__init__.py"])
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert not imports, "trove/__init__.py must stay import-free"


def test_frozen_manifest_is_well_formed() -> None:
    for (src, dst), count in FROZEN_UPWARD_IMPORTS.items():
        assert src in RANK and dst in RANK, f"unregistered manifest pair: {(src, dst)}"
        assert RANK[dst] > RANK[src], f"manifest pair is not upward: {src} -> {dst}"
        assert count > 0, f"zero-count manifest entry: {src} -> {dst}"


def test_no_new_upward_imports() -> None:
    actual = detect_upward_imports(REAL_TREES)
    problems: list[str] = []
    for pair in sorted(set(actual) | set(FROZEN_UPWARD_IMPORTS)):
        src, dst = pair
        got = len(actual.get(pair, []))
        want = FROZEN_UPWARD_IMPORTS.get(pair, 0)
        if got > want:
            kind = "NEW" if pair not in FROZEN_UPWARD_IMPORTS else "UP"
            problems.append(
                f"{kind} {src} -> {dst}: {got} > {want}\n    "
                + "\n    ".join(actual[pair])
            )
        elif got < want:
            problems.append(
                f"DOWN {src} -> {dst}: {got} < {want}; shrink "
                "FROZEN_UPWARD_IMPORTS in the same commit"
            )
    assert not problems, "dependency-direction ratchet:\n" + "\n".join(problems)


def test_no_new_sibling_cycles() -> None:
    actual = detect_sibling_cycles(REAL_TREES)
    assert actual == FROZEN_SIBLING_CYCLES, (
        "depth-2 sibling cycles changed; update FROZEN_SIBLING_CYCLES only "
        "after the cycle is understood.\n"
        f"unexpected={sorted(actual - FROZEN_SIBLING_CYCLES)}\n"
        f"missing={sorted(FROZEN_SIBLING_CYCLES - actual)}"
    )


# ── detector self-tests (synthetic trees) ─────────────────────────────────


def test_detects_upward_import() -> None:
    found = detect_upward_imports(_trees({
        "trove/core/x.py": "from trove.services.kb import KbService\n",
    }))
    assert found == {("core", "services"): ["trove/core/x.py:1"]}


def test_allows_downward_import() -> None:
    found = detect_upward_imports(_trees({
        "trove/services/x.py": "from trove.core.config import AgentConfig\n",
    }))
    assert found == {}


def test_resolves_relative_upward_import() -> None:
    found = detect_upward_imports(_trees({
        "trove/core/x.py": "from ..services.kb import KbService\n",
    }))
    assert found == {("core", "services"): ["trove/core/x.py:1"]}


def test_counts_one_per_statement_per_target_package() -> None:
    found = detect_upward_imports(_trees({
        "trove/core/x.py": "from trove import services, workflow\n",
    }))
    assert set(found) == {("core", "services"), ("core", "workflow")}
    assert all(len(sites) == 1 for sites in found.values())


def test_counts_function_local_and_type_checking_imports() -> None:
    found = detect_upward_imports(_trees({
        "trove/core/x.py": (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from trove.services.kb import KbService\n"
            "def load():\n"
            "    from trove.services.kb import KbService\n"
        ),
    }))
    assert len(found[("core", "services")]) == 2


def test_ignores_version_import_from_package_root() -> None:
    found = detect_upward_imports(_trees({
        "trove/core/x.py": "from trove import __version__\n",
    }))
    assert found == {}


def test_flags_unregistered_component() -> None:
    discovered = _discover_components(_trees({"trove/foo/bar.py": ""}))
    assert discovered == {"foo"}
    assert "foo" not in RANK


def test_detects_synthetic_sibling_cycle() -> None:
    found = detect_sibling_cycles(_trees({
        "trove/services/a/x.py": "from trove.services.b.y import Y\n",
        "trove/services/b/y.py": "from trove.services.a.x import X\n",
    }))
    assert found == {"services: a, b"}


def test_ignores_one_way_sibling_edges() -> None:
    found = detect_sibling_cycles(_trees({
        "trove/services/a/x.py": "from trove.services.b.y import Y\n",
        "trove/services/b/y.py": "from trove.core.clock import utc_now\n",
    }))
    assert found == set()


def test_scc_baseline_is_well_formed() -> None:
    for cycle in FROZEN_SIBLING_CYCLES:
        top, _, members = cycle.partition(": ")
        assert top in RANK, f"unregistered cycle component: {cycle}"
        assert members, f"empty cycle membership: {cycle}"
        assert len(members.split(", ")) > 1, f"singleton is not a cycle: {cycle}"


if __name__ == "__main__":  # pragma: no cover - direct-run convenience
    raise SystemExit(pytest.main([__file__]))
