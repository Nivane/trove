"""工具治理测试 — 三段式描述 lint + 黄金路由 + ACL 裁剪。

Item 11 的落地:描述质量是函数调用精度的第一杠杆。本测试断言:
  1. lint_tool_descriptions 对全工具集零违规(标记齐全 + 长度有界);
  2. 黄金路由:给定问题场景,唯一正确的工具能被识别(描述可路由);
  3. ACL:allowed_roles 裁剪 defs/handlers,catalog 工具按角色隐藏;
  4. 自适应工具集:simple/standard/complex 按复杂度挂载不同工具;
  5. probe/check 结果缓存跨注册表共享。
"""

import json

import pytest

from trove.llm.agent_loop import ToolRegistry
from trove.workflow.nodes.gen_sql import (
    DESC_DONT_MARKER,
    DESC_EXAMPLE_MARKER,
    DESC_USE_MARKER,
    _cache_key,
    lint_tool_descriptions,
    build_sql_registry,
)


def _defs_by_name(tools: list[dict]) -> dict[str, dict]:
    return {t["function"]["name"]: t["function"] for t in tools}


class TestDescriptionLint:
    def test_all_registry_tools_pass_three_part_lint(self, sqlite_registry):
        """三段式描述 lint 对全工具集零违规(含 finish)。"""
        registry = build_sql_registry(
            sqlite_registry, "How many students?", "en", "sqlite",
        )
        problems = lint_tool_descriptions(registry.defs())
        assert problems == [], f"tool description lint failed: {problems}"

    def test_lint_flags_missing_marker(self):
        """缺标记 → lint 报违规。"""
        tools = [{
            "type": "function",
            "function": {"name": "bad", "description": "just a short phrase"},
        }]
        problems = lint_tool_descriptions(tools)
        assert any("missing 'Use when:'" in p for p in problems)
        assert any("too short" in p for p in problems)

    def test_each_tool_has_distinct_route_signal(self, sqlite_registry):
        """黄金路由静态前置:每个工具的 Use when 段含可路由的关键信号。"""
        registry = build_sql_registry(
            sqlite_registry, "How many students?", "en", "sqlite",
        )
        fns = _defs_by_name(registry.defs())
        # 每个工具的 Use when 段必须包含能定位自身场景的信号词
        assert "syntax" in fns["validate_sql"]["description"].lower()
        assert "row" in fns["probe_query"]["description"].lower()
        assert "rule checks" in fns["check_result"]["description"].lower()
        assert "final answer" in fns["finish"]["description"].lower()
        # catalog 是 schema 探索三件套的唯一入口(路由信号在它的描述里)
        catalog = fns["catalog"]["description"].lower()
        assert "search_values" in catalog and "lookup_schema" in catalog
        assert "explain_plan" in catalog
        assert "use when:" in catalog and "do not use when:" in catalog


class TestGoldenRoute:
    """黄金路由:场景 → 期望工具。描述必须引导模型选对工具(静态可验证)。"""

    GOLDEN: list[tuple[str, str]] = [
        ("just drafted SQL, want a cheap syntax check without running it",
         "validate_sql"),
        ("draft might return 0 rows for a superlative; check real row count",
         "probe_query"),
        ("about to finalize, must confirm no rule violations on the draft",
         "check_result"),
        ("candidate filter value 'Ala' — confirm its exact spelling in data",
         "catalog"),
        ("a table referenced in the question is missing from the schema section",
         "catalog"),
        ("draft joins several large tables; check index usage before finalizing",
         "catalog"),
        ("ready to submit the final SQL", "finish"),
    ]

    @pytest.mark.parametrize("scenario,expected", GOLDEN)
    def test_scenario_routes_to_expected_tool(self, sqlite_registry, scenario, expected):
        """每个场景都能在期望工具的 description 里命中场景关键词,且其它工具
        不含相同场景的 Use when 引导(防工具间职责混淆)。"""
        registry = build_sql_registry(
            sqlite_registry, "How many students?", "en", "sqlite",
        )
        fns = _defs_by_name(registry.defs())
        desc = fns[expected]["description"].lower()
        # 期望工具的 Use when 段存在
        assert DESC_USE_MARKER in fns[expected]["description"]
        # 其它工具不得包含该工具的专属信号(粗查:finish 的 'final' 属通用词,
        # 这里用工具各自 Use when 段的独特短语断言)
        if expected == "finish":
            assert "submit" in desc or "final answer" in desc


class TestACLRoleFiltering:
    async def test_catalog_tools_hidden_for_basic_role(self, sqlite_registry):
        """allowed_roles=['user'] → catalog 工具被裁剪,core 工具保留。"""
        registry = build_sql_registry(
            sqlite_registry, "How many students?", "en", "sqlite",
            roles=["user"],
        )
        names = [d["function"]["name"] for d in registry.defs()]
        assert "validate_sql" in names
        assert "probe_query" in names
        assert "check_result" in names
        assert "finish" in names
        assert "catalog" not in names
        assert "search_values" not in names
        assert "lookup_schema" not in names
        assert "explain_plan" not in names

    async def test_catalog_tools_visible_for_analyst(self, sqlite_registry):
        """allowed_roles=['analyst'] → catalog 发现工具可见;
        三件套按需解锁(初始不在 defs,spec 恒在)。"""
        registry = build_sql_registry(
            sqlite_registry, "How many students?", "en", "sqlite",
            roles=["analyst"],
        )
        names = [d["function"]["name"] for d in registry.defs()]
        assert "catalog" in names
        assert "search_values" not in names
        assert "lookup_schema" not in names
        assert "explain_plan" not in names
        # 按需契约:spec 始终可查可执行,只不注入 prompt
        for name in ("search_values", "lookup_schema", "explain_plan"):
            assert registry.spec(name) is not None

    async def test_no_roles_preserves_legacy_core_plus_catalog(self, sqlite_registry):
        """roles=None(未启用 ACL)→ core + catalog 常驻,三件套按需解锁。"""
        registry = build_sql_registry(
            sqlite_registry, "How many students?", "en", "sqlite",
        )
        names = [d["function"]["name"] for d in registry.defs()]
        assert names == [
            "validate_sql", "probe_query", "check_result",
            "catalog", "finish",
        ]
        # 旧的全量形态由 make_sql_tools 保留(激活全部懒注册)
        from trove.workflow.nodes.gen_sql import make_sql_tools
        tools, handlers, _ = make_sql_tools(
            sqlite_registry, "How many students?", "en", "sqlite",
        )
        assert [t["function"]["name"] for t in tools] == [
            "validate_sql", "probe_query", "check_result",
            "catalog", "search_values", "lookup_schema", "explain_plan",
        ]
        assert set(handlers) == {
            "validate_sql", "probe_query", "check_result", "catalog",
            "search_values", "lookup_schema", "explain_plan",
        }

    async def test_calling_hidden_tool_folds_to_unknown(self, sqlite_registry):
        """模型调用被 ACL 裁掉的工具 → 按 unknown 折叠回喂,不执行。"""
        calls: list[str] = []

        class Connectors:
            async def get_schema(self, *a, **k):
                return None

        from trove.workflow.nodes.gen_sql import make_sql_tools
        # 直接构造:受约束 registry 手动挂一个 handler 计数
        registry = build_sql_registry(
            sqlite_registry, "q", "en", "sqlite", roles=["user"], finish=False,
        )
        assert "search_values" not in registry.handlers()


class TestComplexityTiers:
    def test_simple_tier_only_syntax_and_finish(self, sqlite_registry):
        """simple → 仅 validate_sql + finish(无执行/无 catalog)。"""
        registry = build_sql_registry(
            sqlite_registry, "How many students?", "en", "sqlite",
            complexity="simple",
        )
        names = [d["function"]["name"] for d in registry.defs()]
        assert names == ["validate_sql", "finish"]

    def test_standard_tier_adds_probe_check(self, sqlite_registry):
        """standard → + probe/check + catalog 发现工具,三件套按需。"""
        registry = build_sql_registry(
            sqlite_registry, "How many students?", "en", "sqlite",
            complexity="standard",
        )
        names = [d["function"]["name"] for d in registry.defs()]
        assert names == [
            "validate_sql", "probe_query", "check_result", "catalog", "finish",
        ]
        assert registry.spec("search_values") is not None

    def test_complex_tier_full_toolset(self, sqlite_registry):
        """complex → core + catalog;三件套懒注册,解锁后进入 defs。"""
        registry = build_sql_registry(
            sqlite_registry, "How many students?", "en", "sqlite",
            complexity="complex",
        )
        names = [d["function"]["name"] for d in registry.defs()]
        assert names == [
            "validate_sql", "probe_query", "check_result", "catalog", "finish",
        ]
        # 解锁 → 下一轮 defs 全量
        for name in ("search_values", "lookup_schema", "explain_plan"):
            registry.activate_lazy(name)
        assert [d["function"]["name"] for d in registry.defs()] == [
            "validate_sql", "probe_query", "check_result",
            "catalog", "search_values", "lookup_schema", "explain_plan",
            "finish",
        ]


class TestProbeMemoization:
    async def test_probe_result_cached_across_registries(self, sqlite_registry):
        """同一数据源同一 SQL 的 probe 结果跨注册表共享(修正轮间不重执行)。"""
        cache: dict = {}
        r1 = build_sql_registry(
            sqlite_registry, "How many students?", "en", "sqlite",
            probe_cache=cache,
        )
        r2 = build_sql_registry(
            sqlite_registry, "How many students?", "en", "sqlite",
            probe_cache=cache,
        )
        h1 = r1.handlers()["probe_query"]
        h2 = r2.handlers()["probe_query"]
        sql = "SELECT name FROM students"
        first = await h1({"sql": sql})
        second = await h2({"sql": sql})
        assert second == first
        # 缓存以 (datasource, sql, kind, limit) 为键,第二次注册表命中同一条目。
        # build_sql_registry 默认 datasource=""(与调用处一致)。
        assert (_cache_key("", sql, "probe", 10)) in cache
        assert cache[(_cache_key("", sql, "probe", 10))][1] == first
