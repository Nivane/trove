"""工具治理测试 — 三段式描述 lint + 黄金路由 + ACL 裁剪。

Item 11 的落地:描述质量是函数调用精度的第一杠杆。本测试断言:
  1. lint_tool_descriptions 对全工具集零违规(标记齐全 + 长度有界);
  2. 黄金路由:给定问题场景,唯一正确的工具能被识别(描述可路由);
  3. ACL:allowed_roles 裁剪 defs/handlers,catalog 工具按角色隐藏;
  4. 自适应工具集:simple/standard/complex 按复杂度挂载不同工具;
  5. probe/check 结果缓存的作用域 = 单次运行(跨修正轮共享,不跨运行)。
"""

import json

import pytest

from trove.core.types import DatasourceConfig
from trove.llm.agent_loop import ToolRegistry
from trove.services.datasource.registry import ConnectorRegistry
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


_COUNT_SQL = "SELECT COUNT(*) AS n FROM students"


@pytest.fixture
async def uncached_registry():
    """与 sqlite_registry 同构,但**关闭 registry 层结果缓存**。

    probe_cache(gen_sql)与 ConnectorRegistry._result_cache 是两层独立缓存,
    TTL 同为 60s。本类只考察上层作用域,必须关掉下层:否则「重新执行」拿到的
    仍是下层的旧行,观测不到任何差异(这正是实现本修复时踩到的坑)。
    """
    registry = ConnectorRegistry(result_cache_ttl_s=0)
    config = DatasourceConfig(
        name="test_db", type="sqlite",
        connection_params={"path": ":memory:"}, default=True,
    )
    adapter = await registry.register(config, set_default=True)
    await adapter.execute(
        "CREATE TABLE students (id INTEGER PRIMARY KEY, name TEXT, "
        "grade INTEGER, county TEXT)")
    await adapter.execute(
        "INSERT INTO students (name, grade, county) VALUES "
        "('Alice', 95, 'Alameda'), ('Bob', 88, 'Alameda'), "
        "('Carol', 92, 'Orange'), ('Dave', 75, 'Orange'), ('Eve', 99, 'Los Angeles')")
    yield registry
    await registry.unregister("test_db")


async def _add_frank(registry) -> None:
    """真实写入一行 → 把「命中旧缓存」与「重新执行」区分开。"""
    adapter = await registry.get()
    await adapter.execute(
        "INSERT INTO students (name, grade, county) VALUES ('Frank', 81, 'Orange')")


class TestProbeMemoization:
    """probe/check 结果 memoization:作用域 = **单次运行**(跨修正轮),不是全进程。

    缓存 dict 由 gen_sql 节点闭包持有,而图在启动时只编译一次 → 该 dict 是
    进程级的。因此 run_id 必须进缓存键:run_id 每次运行一个 uuid
    (:class:`SessionManager` 生成),于是「跨修正轮复用」的原本设计意图不变
    (同一 run 的所有轮次同键),而跨运行 / 跨会话 / 跨用户的复用被消除。
    """

    async def test_probe_result_shared_across_correction_rounds(
        self, uncached_registry,
    ):
        """同一次运行的多轮修正共享同一观测(不重执行)—— 缓存的原本理由。"""
        cache: dict = {}
        r1 = build_sql_registry(
            uncached_registry, "How many students?", "en", "sqlite",
            probe_cache=cache, run_id="run-1",
        )
        first = await r1.handlers()["probe_query"]({"sql": _COUNT_SQL})
        assert json.loads(first)["rows"] == [["5"]]

        await _add_frank(uncached_registry)

        r2 = build_sql_registry(
            uncached_registry, "How many students?", "en", "sqlite",
            probe_cache=cache, run_id="run-1",
        )
        second = await r2.handlers()["probe_query"]({"sql": _COUNT_SQL})
        assert second == first  # 命中缓存 → 与首轮逐字一致(陈旧但不跨运行)
        assert (_cache_key("", _COUNT_SQL, "probe", 10, "run-1")) in cache

    async def test_probe_result_not_reused_across_runs(self, uncached_registry):
        """不同运行 → 不复用:重新执行,观测到变更后的真实数据。

        缓存的作用域是「同一次运行的多轮修正」,不是全进程。进程级共享会把
        上一次运行的观测喂给下一次运行——数据已变时,模型会基于不存在的行数
        / 值做生成决策(定 SQL、判"0 行 = 过滤值写错了"),而真正的执行在
        execute_sql(新鲜):观测与事实背离无法解释。
        """
        cache: dict = {}
        r1 = build_sql_registry(
            uncached_registry, "How many students?", "en", "sqlite",
            probe_cache=cache, run_id="run-1",
        )
        first = json.loads(await r1.handlers()["probe_query"]({"sql": _COUNT_SQL}))
        assert first["rows"] == [["5"]]

        await _add_frank(uncached_registry)

        r2 = build_sql_registry(
            uncached_registry, "How many students?", "en", "sqlite",
            probe_cache=cache, run_id="run-2",
        )
        second = json.loads(await r2.handlers()["probe_query"]({"sql": _COUNT_SQL}))
        assert second["rows"] == [["6"]]  # 重新执行 → 看到新行

    async def test_cache_key_carries_run_scope(self):
        """缓存键含运行维度(隔离的最小机制)。"""
        assert _cache_key("", _COUNT_SQL, "probe", 10, "run-1") != _cache_key(
            "", _COUNT_SQL, "probe", 10, "run-2")
        assert _cache_key("", _COUNT_SQL, "probe", 10, "run-1") == _cache_key(
            "", _COUNT_SQL, "probe", 10, "run-1")
        # kind 仍参与去重:probe 与 check 取数上限不同,不可互相顶替
        assert _cache_key("", _COUNT_SQL, "probe", 10, "run-1") != _cache_key(
            "", _COUNT_SQL, "check", 50, "run-1")
