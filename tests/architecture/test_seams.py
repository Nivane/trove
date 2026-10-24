"""Executable contracts for the seams listed in CLAUDE.md's 接缝盘点.

A seam that cannot be checked by a machine is a convention, not a seam. Each
test below freezes one seam **in both directions** — adding a provider or
deleting a consumer is red, and the assertion message names what moved:

1. MCP HTTP auth — one construction site, wired with the live AuthService;
2. admin settings — the key table maps onto fields that exist, with the
   declared types;
3. component teardown — every factory component is classified (disposed or
   transient), so a new store cannot leak an aiosqlite worker silently;
4. datasource adapters — one registry write path, all built-in dialects
   resolve, direct adapter construction only in the two justified places;
5. tool + graph inventories — the model-visible tool sets per tier/role and
   the graph node sets under the **real** defaults.
"""

from __future__ import annotations

import ast
import copy
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _module_ast(module) -> ast.Module:
    return ast.parse(Path(inspect.getsourcefile(module)).read_text(encoding="utf-8"))


def _find_func(tree: ast.Module, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found")


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    return [
        call for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == name
    ]


def _ids(nodes) -> set[int]:
    return {id(node) for node in nodes}


# ── 1. MCP HTTP auth ──────────────────────────────────────────
#
# The gate shipped hollow for a month because nothing pinned the wiring: the
# builder returned None for every token and no test referenced it. The seam is
# "one builder → one kwargs constructor → one call site".


def test_mcp_auth_middleware_has_one_construction_site():
    import trove.main

    tree = _module_ast(trove.main)
    builders = _calls(tree, "_build_mcp_auth_middleware")
    assert len(builders) == 1, (
        f"_build_mcp_auth_middleware has {len(builders)} call sites — the gate "
        "must be built in exactly one place (_mcp_http_kwargs)"
    )

    kwargs_fn = _find_func(tree, "_mcp_http_kwargs")
    inside = _ids(_calls(kwargs_fn, "_build_mcp_auth_middleware"))
    assert id(builders[0]) in inside, (
        "the only _build_mcp_auth_middleware call lives outside _mcp_http_kwargs"
    )


def test_mcp_http_main_wires_the_live_auth_service():
    import trove.main

    tree = _module_ast(trove.main)
    fn = _find_func(tree, "async_main_mcp")

    kwargs_calls = _calls(fn, "_mcp_http_kwargs")
    assert len(kwargs_calls) == 1, (
        f"async_main_mcp calls _mcp_http_kwargs {len(kwargs_calls)} times — "
        "exactly one call keeps the middleware and the transport in sync"
    )
    call = kwargs_calls[0]
    assert any(
        isinstance(arg, ast.Call)
        and isinstance(arg.func, ast.Attribute)
        and arg.func.attr == "get"
        and isinstance(arg.func.value, ast.Name)
        and arg.func.value.id == "components"
        and len(arg.args) == 1
        and isinstance(arg.args[0], ast.Constant)
        and arg.args[0].value == "auth"
        for arg in call.args
    ), "the http kwargs are built without components.get('auth') — no per-request resolution"

    http_runs = [
        c for c in ast.walk(fn)
        if isinstance(c, ast.Call)
        and isinstance(c.func, ast.Attribute)
        and c.func.attr == "run_http_async"
    ]
    assert len(http_runs) == 1, "expected exactly one run_http_async call"
    run = http_runs[0]
    assert run.args == [], "run_http_async must receive only **http_kwargs"
    assert [kw.arg for kw in run.keywords] == [None], (
        "run_http_async must be called with **http_kwargs (a direct kwarg "
        "would bypass the builder)"
    )

    # No bypass: the auth class/middleware must not be reached from the entry
    # point directly.
    assert not _calls(fn, "Middleware"), "async_main_mcp builds a Middleware directly"
    assert not _calls(fn, "MCPBearerAuthMiddleware"), (
        "async_main_mcp instantiates the gate directly — that bypasses the seam"
    )


def test_mcp_server_build_receives_the_request_identity_switch():
    import trove.main

    fn = _find_func(_module_ast(trove.main), "async_main_mcp")
    builds = _calls(fn, "build_mcp_server")
    assert len(builds) == 1
    keywords = {kw.arg: kw.value for kw in builds[0].keywords}
    assert "require_request_identity" in keywords, (
        "build_mcp_server is called without require_request_identity — a "
        "token-protected request could fall back to the startup identity"
    )


# ── 2. Admin settings map onto live AgentConfig fields ────────

_KINDS = {"str", "path", "bool", "int", "range", "enum", "strlist", "providers"}

# key -> (config path, kind, constraint). Frozen on purpose: a new key must be
# classified here (and documented) as well as added to the service.
FROZEN_SETTINGS: dict[str, tuple[str, str, object]] = {
    "llm.default_model": ("target", "str", None),
    "llm.fast_model": ("model_fast", "str", None),
    "llm.draft_model": ("model_draft", "path", None),
    "llm.providers": ("providers", "providers", None),
    "egress.enabled": ("egress.enabled", "bool", None),
    "egress.redact_tool_results": ("egress.redact_tool_results", "bool", None),
    "egress.allow_providers": ("egress.allow_providers", "strlist", None),
    "app.language": ("language", "enum", ("zh", "en")),
    "app.semantic_layer_path": ("semantic_layer_path", "path", None),
    "app.date_parser": ("date_parser", "bool", None),
    "app.explain_semantics": ("explain_semantics", "bool", None),
    "app.fast_path": ("fast_path", "bool", None),
    "app.reflect_skip": ("reflect_skip", "enum", ("simple", "standard", "all", "off")),
    "app.hitl": ("hitl", "bool", None),
    "app.insights": ("insights", "bool", None),
    "app.conclusion": ("conclusion", "bool", None),
    "app.result_cache": ("result_cache", "bool", None),
    "app.decompose_llm_judge": ("decompose_llm_judge", "bool", None),
    "retention.max_sessions_per_user": ("retention.max_sessions_per_user", "int", 0),
    "retention.active_grace_min": ("retention.active_grace_min", "int", 0),
    "retention.max_checkpoints_per_thread": ("retention.max_checkpoints_per_thread", "int", 0),
    "retention.sweep_interval_hours": ("retention.sweep_interval_hours", "int", 0),
    "app.result_display_rows": ("result_display_rows", "range", (1, 500)),
    "app.result_max_rows": ("result_max_rows", "range", (1, 50000)),
    "app.api_rate_per_minute": ("api_rate_per_minute", "range", (0, 1000)),
    "app.api_daily_quota": ("api_daily_quota", "range", (0, 100000)),
    "extensions.org_extensions_enabled": ("extensions.org_extensions_enabled", "bool", None),
}


def _resolve(config, path: str):
    obj = config
    for part in path.split("."):
        obj = getattr(obj, part)  # AttributeError = the path does not exist
    return obj


def _check_schema(schema) -> None:
    """Every path resolves on a fresh AgentConfig and matches its declared kind."""
    from trove.core.config import AgentConfig

    config = AgentConfig()
    for key, (path, kind, constraint) in schema.items():
        assert kind in _KINDS, f"{key}: unknown kind {kind!r}"
        value = _resolve(config, path)
        if kind in ("str", "path", "enum"):
            assert isinstance(value, str), f"{key} → {path} is {type(value).__name__}, not str"
        elif kind == "bool":
            assert isinstance(value, bool), f"{key} → {path} is not a bool"
        elif kind == "int":
            assert type(value) is int, f"{key} → {path} is not an int"
        elif kind == "range":
            assert type(value) is int, f"{key} → {path} is not an int"
            assert isinstance(constraint, tuple) and len(constraint) == 2, (
                f"{key}: range needs a (lo, hi) constraint"
            )
        elif kind == "strlist":
            assert isinstance(value, list), f"{key} → {path} is not a list"
            assert all(isinstance(item, str) for item in value), (
                f"{key} → {path} holds non-strings"
            )
        elif kind == "providers":
            assert isinstance(value, list), f"{key} → {path} is not a list"
        if kind == "enum":
            assert value in constraint, (
                f"{key}: default {value!r} is outside the declared enum {constraint!r}"
            )


def test_settings_schema_is_frozen():
    from trove.services.admin_settings.service import SETTINGS_SCHEMA

    assert SETTINGS_SCHEMA == FROZEN_SETTINGS, (
        "SETTINGS_SCHEMA changed — classify the new/removed key here and in "
        "docs/reference/config.html"
    )


def test_settings_paths_and_types_match_agent_config():
    from trove.services.admin_settings.service import SETTINGS_SCHEMA

    _check_schema(SETTINGS_SCHEMA)


def test_settings_checker_rejects_a_nonexistent_path():
    bad = copy.deepcopy(FROZEN_SETTINGS)
    bad["fake.missing"] = ("no_such_section.no_such_field", "str", None)
    with pytest.raises(AttributeError):
        _check_schema(bad)


def test_settings_checker_rejects_a_mistyped_kind():
    bad = copy.deepcopy(FROZEN_SETTINGS)
    bad["fake.mistyped"] = ("target", "bool", None)  # target is a str
    with pytest.raises(AssertionError):
        _check_schema(bad)


def _sample_value(kind: str, constraint, current):
    if kind == "str":
        return "sample-model"
    if kind == "path":
        return "/tmp/trove-sample"
    if kind == "bool":
        return not current
    if kind == "int":
        return 7
    if kind == "range":
        return constraint[0]
    if kind == "enum":
        return constraint[0]
    if kind == "strlist":
        return ["sample"]
    raise AssertionError(f"no sample for kind {kind}")


def test_settings_apply_and_effective_round_trip():
    from trove.core.config import AgentConfig
    from trove.services.admin_settings.service import (
        SETTINGS_SCHEMA,
        apply_overrides,
        effective_values,
    )

    config = AgentConfig()
    before = effective_values(config)
    samples = {
        key: _sample_value(kind, constraint, before[key])
        for key, (_path, kind, constraint) in SETTINGS_SCHEMA.items()
        if kind != "providers"
    }

    apply_overrides(config, samples)
    after = effective_values(config)
    for key, expected in samples.items():
        assert after[key] == expected, f"{key} did not round-trip through the live config"


def test_settings_providers_round_trip_masks_secrets():
    from trove.core.config import AgentConfig
    from trove.services.admin_settings.service import apply_overrides, effective_values

    config = AgentConfig()
    apply_overrides(config, {"llm.providers": [{
        "name": "openai", "litellm_params": {"api_base": "http://x", "api_key": "sk-secret"},
    }]})
    rendered = effective_values(config)["llm.providers"]
    assert [p["name"] for p in rendered] == ["openai"]
    assert rendered[0]["has_api_key"] is True
    assert rendered[0]["litellm_params"]["api_key"] != "sk-secret"


# ── 3. Component teardown coverage ───────────────────────────
#
# aiosqlite worker threads are non-daemon: one store left undisposed keeps the
# process alive after asyncio.run returns. Every key the factory returns must
# be classified — disposed (holds a connection/background task) or transient
# (does not). A new component that is neither is a silent process hang.

DISPOSED_KEYS = {
    "question_coverage", "connector_registry", "jobs", "actions", "oidc",
    "verdicts", "memory", "user_facts", "session_store", "settings", "auth",
}

TRANSIENT_KEYS = {
    # Pure values / config.
    "config", "bootstrap_admin_password",
    # Stateless services (no connection of their own).
    "llm_gateway", "catalog_service", "kb", "rate_limiter", "indexer",
    "hybrid_store", "lineage", "skills", "presets", "graphs", "scheduler",
    "decision", "scans", "subscriptions", "action_templates", "maintenance",
    # `config_store` is YAML-only (`datasources.yml`):每读写一处都临时开文件,
    # 没有常驻句柄。
    "config_store",
    # `session_manager` shares the same backend as `session_store`
    # (TaskStore is constructed from `session_store.backend()`), so disposing
    # the store covers it.
    "session_manager",
    # `checkpointer` is opened with `async with build_checkpointer(...)` and
    # closed by that context manager, not by the component plan.
    "checkpointer",
}


def _factory_return_keys() -> list[str]:
    import trove.main

    fn = _find_func(_module_ast(trove.main), "create_app_components")
    dict_returns = [
        node for node in ast.walk(fn)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)
    ]
    assert len(dict_returns) == 1, (
        f"create_app_components has {len(dict_returns)} dict returns — the "
        "coverage check reads exactly one"
    )
    keys = []
    for key in dict_returns[0].value.keys:
        assert isinstance(key, ast.Constant) and isinstance(key.value, str), (
            "component keys must be string literals for the seam check"
        )
        keys.append(key.value)
    return keys


def test_every_factory_component_is_classified():
    import trove.main

    factory_keys = set(_factory_return_keys())
    disposed = {name for name, _ in trove.main.DISPOSE_PLAN}
    assert disposed == DISPOSED_KEYS, (
        f"DISPOSE_PLAN drifted: missing={sorted(DISPOSED_KEYS - disposed)} "
        f"extra={sorted(disposed - DISPOSED_KEYS)}"
    )
    overlap = DISPOSED_KEYS & TRANSIENT_KEYS
    assert not overlap, f"components classified twice: {sorted(overlap)}"
    unclassified = factory_keys - DISPOSED_KEYS - TRANSIENT_KEYS
    assert not unclassified, (
        f"unclassified components (new connection? classify in DISPOSE_PLAN / "
        f"TRANSIENT_KEYS): {sorted(unclassified)}"
    )
    stale = (DISPOSED_KEYS | TRANSIENT_KEYS) - factory_keys
    assert not stale, f"classified but no longer returned by the factory: {sorted(stale)}"


def test_dispose_order_keeps_load_bearing_relations():
    from trove.main import DISPOSE_PLAN

    order = [name for name, _ in DISPOSE_PLAN]
    assert order.index("question_coverage") < order.index("session_store"), (
        "background maintenance must stop before its storage is released"
    )
    assert order[-1] == "auth", (
        "auth is disposed last so audit writes during teardown still resolve"
    )
    for name, method in DISPOSE_PLAN:
        assert name in DISPOSED_KEYS
        assert method and not method.startswith(".")


async def test_dispose_isolates_a_failing_component():
    from trove.main import DISPOSE_PLAN, _dispose_components

    calls: list[str] = []

    def _stub(path: str, name: str):
        parts = path.split(".")
        failure = name == DISPOSE_PLAN[0][0]

        async def hook() -> None:
            calls.append(name)
            if failure:
                raise RuntimeError(f"{name} boom")

        obj = SimpleNamespace(**{parts[-1]: hook})
        for part in reversed(parts[:-1]):
            obj = SimpleNamespace(**{part: obj})
        return obj

    components = {name: _stub(method, name) for name, method in DISPOSE_PLAN}
    await _dispose_components(components)

    assert calls == [name for name, _ in DISPOSE_PLAN], (
        "one failing component stopped the rest of the teardown"
    )


# ── 4. Datasource adapter registry ───────────────────────────

BUILTIN_DIALECTS = {
    "sqlite", "mysql", "doris", "postgres", "clickhouse", "duckdb",
    "snowflake", "bigquery",
}

# Direct constructors are allowed only where there is no registry to register
# into (or no reason to): both sites build a throwaway SQLite database before
# the datasource exists, and neither writes the registry.
DIRECT_SQLITE_WHITELIST = {
    "trove/services/datasource/demo_setup.py",
    "scripts/backfill_values.py",
}


def _registry_writes_in(tree: ast.Module) -> list[ast.AST]:
    writes = []
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        for target in targets:
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == "_ADAPTER_REGISTRY"
            ):
                writes.append(node)
    return writes


def test_adapter_registry_has_one_write_path():
    import trove.services.datasource.registry as registry

    tree = _module_ast(registry)
    writes = _registry_writes_in(tree)
    assert len(writes) == 1, (
        f"_ADAPTER_REGISTRY has {len(writes)} write sites — register_adapter() "
        "is the only entry point"
    )
    register = _find_func(tree, "register_adapter")
    assert id(writes[0]) in _ids(ast.walk(register)), (
        "the only _ADAPTER_REGISTRY write is outside register_adapter()"
    )


def test_builtin_dialects_are_registered():
    import trove.services.datasource.registry as registry
    from trove.services.datasource.adapters.base import DatabaseAdapter

    registered = set(registry._ADAPTER_REGISTRY)
    assert registered == BUILTIN_DIALECTS, (
        f"built-in dialects drifted: missing={sorted(BUILTIN_DIALECTS - registered)} "
        f"extra={sorted(registered - BUILTIN_DIALECTS)}"
    )
    for dialect, cls in registry._ADAPTER_REGISTRY.items():
        assert issubclass(cls, DatabaseAdapter), f"{dialect} → {cls} is not an adapter"


@pytest.mark.parametrize("dialect", sorted(BUILTIN_DIALECTS))
async def test_prepare_resolves_every_builtin_dialect(dialect, monkeypatch):
    import trove.services.datasource.registry as registry
    from trove.core.types import DatasourceConfig

    cls = registry._ADAPTER_REGISTRY[dialect]
    connected: list[str] = []

    async def fake_connect(self):
        connected.append(self.name)

    monkeypatch.setattr(cls, "connect", fake_connect)
    connector_registry = registry.ConnectorRegistry()
    adapter = await connector_registry.prepare(
        DatasourceConfig(name="seam-probe", type=dialect),
    )
    assert isinstance(adapter, cls)
    assert connected == ["seam-probe"]


def test_direct_sqlite_construction_is_whitelisted():
    found: set[str] = set()
    for base in ("trove", "scripts"):
        for path in (ROOT / base).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            if any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "SQLiteAdapter"
                for node in ast.walk(tree)
            ):
                found.add(path.relative_to(ROOT).as_posix())
    assert found == DIRECT_SQLITE_WHITELIST, (
        f"direct SQLiteAdapter construction drifted: "
        f"new={sorted(found - DIRECT_SQLITE_WHITELIST)} "
        f"stale={sorted(DIRECT_SQLITE_WHITELIST - found)} — go through "
        "ConnectorRegistry.prepare(), or extend the whitelist with a reason"
    )


# ── 5. Tool and graph inventories ────────────────────────────

_LAZY_CATALOG = {"search_values", "lookup_schema", "explain_plan"}

# (complexity, roles) -> model-visible tool names from registry.defs().
FROZEN_TOOLS: dict[tuple[str, tuple[str, ...] | None], set[str]] = {
    ("simple", None): {"validate_sql", "finish"},
    ("simple", ("user",)): {"validate_sql", "finish"},
    ("simple", ("analyst",)): {"validate_sql", "finish"},
    ("simple", ("admin",)): {"validate_sql", "finish"},
    ("standard", None): {"validate_sql", "probe_query", "check_result", "catalog", "finish"},
    ("standard", ("user",)): {"validate_sql", "probe_query", "check_result", "finish"},
    ("standard", ("analyst",)): {"validate_sql", "probe_query", "check_result", "catalog", "finish"},
    ("standard", ("admin",)): {"validate_sql", "probe_query", "check_result", "catalog", "finish"},
    ("complex", None): {"validate_sql", "probe_query", "check_result", "catalog", "finish"},
    ("complex", ("user",)): {"validate_sql", "probe_query", "check_result", "finish"},
    ("complex", ("analyst",)): {"validate_sql", "probe_query", "check_result", "catalog", "finish"},
    ("complex", ("admin",)): {"validate_sql", "probe_query", "check_result", "catalog", "finish"},
}


def _tool_names(registry) -> set[str]:
    return {spec["function"]["name"] for spec in registry.defs()}


@pytest.mark.parametrize("key", sorted(FROZEN_TOOLS, key=str))
def test_sql_tool_inventory_by_tier_and_role(key):
    from trove.services.datasource.registry import ConnectorRegistry
    from trove.workflow.nodes.gen_sql import build_sql_registry

    complexity, roles = key
    registry = build_sql_registry(
        ConnectorRegistry(), "q", "en", "sqlite",
        complexity=complexity, roles=list(roles) if roles else None,
    )
    assert _tool_names(registry) == FROZEN_TOOLS[key], (
        f"tool set changed for complexity={complexity} roles={roles}: "
        f"missing={sorted(FROZEN_TOOLS[key] - _tool_names(registry))} "
        f"extra={sorted(_tool_names(registry) - FROZEN_TOOLS[key])}"
    )


def test_lazy_catalog_tools_exist_only_on_standard_and_complex_tiers():
    from trove.services.datasource.registry import ConnectorRegistry
    from trove.workflow.nodes.gen_sql import build_sql_registry

    for complexity in ("standard", "complex"):
        registry = build_sql_registry(
            ConnectorRegistry(), "q", "en", "sqlite", complexity=complexity,
        )
        missing = sorted(n for n in _LAZY_CATALOG if registry.spec(n) is None)
        assert not missing, f"{complexity}: lazy tools not registered: {missing}"
        # 懒注册 = 不在常驻 defs(),只能按名字取到
        assert not (_LAZY_CATALOG & _tool_names(registry))

    simple = build_sql_registry(
        ConnectorRegistry(), "q", "en", "sqlite", complexity="simple",
    )
    assert all(simple.spec(n) is None for n in _LAZY_CATALOG)


def test_without_connectors_only_syntax_tools_remain():
    from trove.workflow.nodes.gen_sql import build_sql_registry

    registry = build_sql_registry(None, "q", "en", "sqlite", complexity="complex")
    assert _tool_names(registry) == {"validate_sql", "finish"}
    assert all(registry.spec(n) is None for n in _LAZY_CATALOG)


def test_load_skill_is_conditional_on_available_skills():
    from trove.services.datasource.registry import ConnectorRegistry
    from trove.workflow.nodes.gen_sql import build_sql_registry

    class _Skills:
        def __init__(self, available: bool) -> None:
            self.available = available

        def has_available_for(self, node: str) -> bool:
            return self.available

        def load_skill_content(self, name, lang, *, node, skill_ctx) -> str:
            return "body"

    kwargs = dict(complexity="standard")
    with_skills = build_sql_registry(
        ConnectorRegistry(), "q", "en", "sqlite", skills=_Skills(True), **kwargs,
    )
    without = build_sql_registry(
        ConnectorRegistry(), "q", "en", "sqlite", skills=_Skills(False), **kwargs,
    )
    assert "load_skill" in _tool_names(with_skills)
    assert "load_skill" not in _tool_names(without)


def _graph_node_sets() -> dict[str, set[str]]:
    from trove.core.config import AgentConfig
    from trove.llm.gateway import LLMGateway
    from trove.workflow.graphs import GraphServices, build_graphs

    services = GraphServices(
        llm=LLMGateway(mock_response="x"),
        config=AgentConfig(),
    )
    graphs = build_graphs(services)  # real defaults, not the test fixture flags
    return {
        name: set(graph.nodes) - {"__start__"}
        for name, graph in graphs.items()
    }


FROZEN_GRAPH_NODES: dict[str, set[str]] = {
    "reflection": {
        "analyze_error", "answer_chitchat", "answer_correction", "answer_metadata",
        "answer_reject", "attribution", "chart", "conclusion", "confirm_draft",
        "execute_sql", "fast_match", "gen_assemble", "gen_generate", "gen_retrieve",
        "hitl", "insights", "masking", "metadata_check", "output", "parse_date",
        "query_sketch", "reflect", "refuse", "route_intent", "schema_linking",
        "select", "semantics", "validate",
    },
    "fixed": {
        "answer_chitchat", "answer_correction", "answer_metadata", "answer_reject",
        "attribution", "chart", "conclusion", "confirm_draft", "execute_sql",
        "gen_assemble", "gen_generate", "gen_retrieve", "hitl", "insights",
        "masking", "metadata_check", "output", "parse_date", "refuse",
        "route_intent", "schema_linking", "semantics", "validate",
    },
    "empty": {"output"},
}


def test_graph_node_inventory_under_real_defaults():
    actual = _graph_node_sets()
    assert set(actual) == set(FROZEN_GRAPH_NODES), (
        f"workflows changed: missing={sorted(set(FROZEN_GRAPH_NODES) - set(actual))} "
        f"extra={sorted(set(actual) - set(FROZEN_GRAPH_NODES))}"
    )
    for name, expected in FROZEN_GRAPH_NODES.items():
        assert actual[name] == expected, (
            f"{name} graph nodes changed: "
            f"missing={sorted(expected - actual[name])} "
            f"extra={sorted(actual[name] - expected)}"
        )
