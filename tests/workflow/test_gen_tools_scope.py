"""gen 工具的授权门(设计 §2.3):probe / check / search 是取数路径。

生成阶段的工具会**真实执行**模型写的 SQL、**真实读值**(search_values)。
它们不经过 ``execute_sql``,所以必须自己过一次同一道门(Authorizer A1–A3 +
行/列 scope),否则「执行前授权」只覆盖了交付那条 SQL,生成阶段就是旁路。

这里钉住四件事:

1. 行 grant 在执行前注入 —— probe/check/search 看到的是**同一批行**;
2. 列拒绝在**执行前**挡住 —— probe/check 返回 ``[ERR:AUTHZ_COLUMN]`` 观测,
   search 显式请求被拒、全列扫描跳过被禁列且只列名字(决策 3:名可见值不可);
3. 表级 A3 与 A1 同样适用(工具不是 A3 的例外);
4. 未装配鉴权面(authorizer 与 principal 都缺席)逐字节不变。
"""

from __future__ import annotations

import pytest

from trove.services.authz.enforcer import Authorizer
from trove.services.authz.policy import Principal, principal_to_wire

DB = "test_db"


def _principal(*, row: dict | None = None, deny: dict | None = None) -> Principal:
    return Principal(
        subject="7",
        role="user",
        grants=frozenset({DB}),
        row_grants=None if row is None else {DB: row},
        column_denials=(
            None if deny is None
            else {DB: {t: frozenset(cols) for t, cols in deny.items()}}
        ),
    )


def _registry(
    connectors,
    *,
    principal: Principal | None = None,
    authorizer: Authorizer | None = None,
    declared: set[str] | None = None,
    question: str = "how many students per county",
):
    """建注册表;``declared`` 给了就装 A3 基准(否则 A3 跳过)。"""
    from trove.workflow.nodes.gen_sql import build_sql_registry

    if declared is not None and authorizer is None:
        authorizer = Authorizer(declared_tables=lambda _ds: set(declared))
    registry = build_sql_registry(
        connectors,
        question,
        "en",
        "sqlite",
        datasource=DB,
        complexity="complex",
        roles=["analyst"],
        authorizer=authorizer,
        principal=principal_to_wire(principal) if principal is not None else None,
    )
    registry.activate_lazy("search_values")
    return registry


def _handler(registry, name: str):
    return registry.handlers()[name]


class TestProbe:
    async def test_row_grant_applies_before_execution(self, sqlite_registry):
        """probe 观测的是 scope 后的行 —— 5 名学生里只剩 Alameda 的 2 个。"""
        registry = _registry(
            sqlite_registry,
            principal=_principal(row={"students": "county = 'Alameda'"}),
            authorizer=Authorizer(),
        )
        obs = await _handler(registry, "probe_query")(
            {"sql": "SELECT COUNT(*) FROM students"})
        assert obs["ok"] is True, obs
        assert obs["rows"] == [["2"]], obs

    async def test_blocked_column_is_refused_without_rows(self, sqlite_registry):
        """列拒绝 = 执行前拒绝;生成期可换列重试,所以是错误观测不是终态。"""
        registry = _registry(
            sqlite_registry,
            principal=_principal(deny={"students": ["county"]}),
            authorizer=Authorizer(),
        )
        obs = await _handler(registry, "probe_query")(
            {"sql": "SELECT county, COUNT(*) FROM students GROUP BY county"})
        assert obs["ok"] is False
        assert "[ERR:AUTHZ_COLUMN]" in obs["error"]
        assert "county" in obs["error"]
        assert "rows" not in obs

    async def test_star_projection_refused_but_count_allowed(self, sqlite_registry):
        registry = _registry(
            sqlite_registry,
            principal=_principal(deny={"students": ["county"]}),
            authorizer=Authorizer(),
        )
        probe = _handler(registry, "probe_query")
        star = await probe({"sql": "SELECT * FROM students"})
        assert star["ok"] is False
        assert "[ERR:AUTHZ_COLUMN]" in star["error"]

        counted = await probe({"sql": "SELECT COUNT(*) FROM students"})
        assert counted["ok"] is True, counted
        assert counted["rows"] == [["5"]]

    async def test_a3_declared_tables_still_binds(self, sqlite_registry):
        """工具不是 A3 的例外:声明之外的表在 probe 前就被拒。"""
        registry = _registry(
            sqlite_registry,
            principal=_principal(),
            declared={"students"},
        )
        obs = await _handler(registry, "probe_query")(
            {"sql": "SELECT COUNT(*) FROM loans"})
        assert obs["ok"] is False
        assert "[ERR:AUTHZ_TABLE]" in obs["error"]

    async def test_missing_principal_denied_when_gate_wired(self, sqlite_registry):
        """装了鉴权面但主体缺失 = A1 拒绝(与 execute_sql 同一条)。"""
        registry = _registry(
            sqlite_registry, principal=None, authorizer=Authorizer(),
        )
        obs = await _handler(registry, "probe_query")(
            {"sql": "SELECT COUNT(*) FROM students"})
        assert obs["ok"] is False
        assert "[ERR:AUTHZ_NO_PRINCIPAL]" in obs["error"]

    async def test_ungated_deployment_unchanged(self, sqlite_registry):
        """authorizer 与 principal 都缺席 = 嵌入部署,老行为逐字节不变。"""
        registry = _registry(sqlite_registry)
        obs = await _handler(registry, "probe_query")(
            {"sql": "SELECT COUNT(*) FROM students"})
        assert obs["ok"] is True, obs
        assert obs["rows"] == [["5"]]


class TestCheckResult:
    async def test_blocked_column_returns_error_not_violation(self, sqlite_registry):
        """拒绝是「不可执行」,不是规则违规 —— 不加分、不产 hits。"""
        registry = _registry(
            sqlite_registry,
            principal=_principal(deny={"students": ["county"]}),
            authorizer=Authorizer(),
        )
        text = await _handler(registry, "check_result")(
            {"sql": "SELECT county FROM students"})
        assert text.startswith("ERROR:")
        assert "[ERR:AUTHZ_COLUMN]" in text
        assert registry.check_hits == []
        assert registry.check_passed == 0

    async def test_row_grant_shapes_the_checked_result(self, sqlite_registry):
        """规则校验执行的是 scope 后的 SQL:行数按可见范围算。"""
        registry = _registry(
            sqlite_registry,
            principal=_principal(row={"students": "county = 'Alameda'"}),
            authorizer=Authorizer(),
            question="list the student names",
        )
        text = await _handler(registry, "check_result")(
            {"sql": "SELECT name FROM students"})
        assert text == "OK (2 rows)", text


class TestSearchValues:
    async def test_row_grant_filters_scanned_values(self, sqlite_registry):
        """值检索与执行面看到同一批行:Carol/Dave 不在可见范围里。"""
        registry = _registry(
            sqlite_registry,
            principal=_principal(row={"students": "county = 'Alameda'"}),
            authorizer=Authorizer(),
        )
        out = await _handler(registry, "search_values")(
            {"table": "students", "keyword": "a"})
        assert out["ok"] is True, out
        assert out["hits"]["name"] == ["Alice"], out["hits"]

    async def test_scan_skips_blocked_column_but_names_it(self, sqlite_registry):
        """被禁列不扫、值不出现,但**名字**如实列出(决策 3)。"""
        registry = _registry(
            sqlite_registry,
            principal=_principal(deny={"students": ["county"]}),
            authorizer=Authorizer(),
        )
        out = await _handler(registry, "search_values")(
            {"table": "students", "keyword": "a"})
        assert out["ok"] is True, out
        assert "county" not in out["hits"]          # 值不出现
        assert out["blocked_columns"] == ["county"]  # 名字可见
        assert out["hits"]["name"] == ["Alice", "Carol", "Dave"]

    async def test_explicit_blocked_column_is_refused(self, sqlite_registry):
        registry = _registry(
            sqlite_registry,
            principal=_principal(deny={"students": ["county"]}),
            authorizer=Authorizer(),
        )
        out = await _handler(registry, "search_values")(
            {"table": "students", "keyword": "Ala", "column": "county"})
        assert out["ok"] is False
        assert "[ERR:AUTHZ_COLUMN]" in out["error"]
        assert "values" not in out

    async def test_all_columns_blocked_says_permitted_not_no_match(
        self, sqlite_registry,
    ):
        """全部跳过的列不能读成「没有匹配」—— 那会把「没查」谎报成「查过了」。"""
        registry = _registry(
            sqlite_registry,
            principal=_principal(
                deny={"students": ["id", "name", "grade", "county"]}),
            authorizer=Authorizer(),
        )
        out = await _handler(registry, "search_values")(
            {"table": "students", "keyword": "a"})
        assert out["ok"] is True, out
        assert out["hits"] == {}
        assert out["blocked_columns"] == ["county", "grade", "id", "name"]
        assert "no permitted column" in out["note"]

    async def test_table_level_gate_applies(self, sqlite_registry):
        registry = _registry(
            sqlite_registry,
            principal=_principal(),
            declared={"students"},
        )
        out = await _handler(registry, "search_values")(
            {"table": "loans", "keyword": "x"})
        assert out["ok"] is False
        assert "[ERR:AUTHZ_TABLE]" in out["error"]

    async def test_ungated_deployment_unchanged(self, sqlite_registry):
        registry = _registry(sqlite_registry)
        out = await _handler(registry, "search_values")(
            {"table": "students", "keyword": "a"})
        assert out["ok"] is True, out
        assert out["hits"]["name"] == ["Alice", "Carol", "Dave"]
        assert "blocked_columns" not in out


@pytest.mark.parametrize("sql,reason", [
    ("SELECT county FROM students", "column"),
    ("SELECT * FROM students", "column"),
])
def test_scope_error_tags_are_the_shared_closed_set(sql, reason):
    """工具拒绝与 execute_sql 用同一套 ``[ERR:AUTHZ_*]`` 词汇(闭集)。"""
    from trove.services.authz.scope import ScopeError, apply_scope

    with pytest.raises(ScopeError) as exc:
        apply_scope(
            sql, model=None,
            principal=_principal(deny={"students": ["county"]}),
            datasource=DB, dialect="sqlite", include_declared_rls=False,
        )
    assert exc.value.reason == reason
    assert exc.value.error_tag() == "AUTHZ_COLUMN"
