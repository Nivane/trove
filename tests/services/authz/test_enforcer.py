"""执行层强制点单测:I2 / I7 与 A1–A3。

对应设计文档 ``2026-09-28-agent-identity-masking-design.md`` §5.3 / §10 / §12。

这一层是「执行 SQL 之前的最后一米」:无论 SQL 从哪来(编译器、快径、API 直执行、
job payload),落库前都要在这里过一次。所以本文件的重点不是「判定对不对」——
那是 ``policy.py`` 的事——而是**判定点的形状**:缺主体必须拒绝、基准不存在与
判定不通过必须分开、配置写错不得静默失效。
"""

from __future__ import annotations

import pytest

from trove.services.authz.enforcer import AuthzDecision, Authorizer, referenced_tables
from trove.services.authz.policy import Principal

#: 一个声明了 orders / customers 的语义模型对应的物理表名集合。
_DECLARED = {"orders", "customers"}


def _authorizer(mode: str = "enforce", declared=None) -> Authorizer:
    """默认:语义层基准是 ``_DECLARED``。"""
    if declared is None:
        declared = _DECLARED
    return Authorizer(
        declared_tables=(lambda _ds: declared) if declared is not False else None,
        mode=mode,
    )


def _user(grants=frozenset({"test_db"})) -> Principal:
    return Principal(subject="7", grants=grants)


DEFAULT = "test_db"


# ── A1:主体存在(I2 / I7)─────────────────────────────────────────


class TestNoPrincipal:
    """缺主体一律拒绝 —— 这是 I2「执行前再验一次」的入口条件。"""

    def test_missing_principal_is_denied(self):
        d = _authorizer().check(None, datasource=DEFAULT, tables=["orders"], default=DEFAULT)
        assert d.allowed is False
        assert d.reason == "no_principal"

    def test_missing_principal_is_denied_even_when_nothing_can_object(self):
        """没有语义基准、没有表、数据源也空 —— 仍然拒绝。

        「其它条件都没意见」不得被读成放行:缺主体的方向只有拒绝一个。
        """
        d = _authorizer(declared=False).check(None, datasource="", default="")
        assert d.allowed is False
        assert d.reason == "no_principal"

    def test_missing_principal_is_denied_in_warn_mode_too(self):
        """warn 只放宽 A3(表级),不放宽 A1 —— 两者不是一回事。"""
        d = _authorizer(mode="warn").check(None, datasource=DEFAULT, default=DEFAULT)
        assert d.allowed is False


# ── A2:数据源授权 ────────────────────────────────────────────────


class TestDatasourceGate:
    def test_datasource_outside_grants_is_denied(self):
        d = _authorizer().check(
            _user(frozenset({"other_db"})), datasource=DEFAULT, default=DEFAULT,
        )
        assert d.allowed is False
        assert d.reason == "datasource"

    def test_granted_datasource_passes(self):
        assert _authorizer().check(_user(), datasource=DEFAULT, default=DEFAULT).allowed

    def test_admin_passes(self):
        p = Principal(subject="1", role="admin")
        assert _authorizer().check(p, datasource="anything", default=DEFAULT).allowed

    def test_empty_grants_allow_default_only(self):
        p = Principal(subject="7", grants=frozenset())
        assert _authorizer().check(p, datasource=DEFAULT, default=DEFAULT).allowed
        assert not _authorizer().check(p, datasource="other", default=DEFAULT).allowed

    def test_no_evidence_is_denied(self):
        """``grants=None``(拿不到依据)连默认源都不放行 —— I7,与 policy 同口径。"""
        p = Principal(subject="7", grants=None)
        d = _authorizer().check(p, datasource=DEFAULT, default=DEFAULT)
        assert d.allowed is False
        assert d.reason == "datasource"

    def test_datasource_denial_does_not_depend_on_tables(self):
        """A2 先于 A3:数据源都不给,表清单是什么都不影响结论。"""
        d = _authorizer().check(
            _user(frozenset({"other"})), datasource=DEFAULT,
            tables=["orders"], default=DEFAULT,
        )
        assert d.reason == "datasource" and d.narrowed_tables == []


# ── A3:表级(语义层白名单)────────────────────────────────────────


class TestTableGate:
    """A3 的「可见数据集」= 该数据源在语义模型里声明过的数据集/物理表。

    这是 2026-09-29 定下的口径:仓库的 grants 只到数据源级,没有数据集级授权源。
    所以 A3 挡的不是「另一个用户的数据集」,而是**声明之外的表** —— 即绕过语义层
    直接摸物理表。编译路径与快径产出的 SQL 只引用声明过的表,因此不会被误伤。
    """

    def test_declared_tables_pass(self):
        d = _authorizer().check(
            _user(), datasource=DEFAULT, tables=["orders", "customers"], default=DEFAULT,
        )
        assert d.allowed is True
        assert d.narrowed_tables == []

    def test_undeclared_table_denied_in_enforce(self):
        d = _authorizer(mode="enforce").check(
            _user(), datasource=DEFAULT, tables=["orders", "salary"], default=DEFAULT,
        )
        assert d.allowed is False
        assert d.reason == "table"
        assert d.narrowed_tables == ["salary"]

    def test_undeclared_table_allowed_in_warn_but_recorded(self):
        """warn 期(§8.2)要能回答「哪些表会被拒」,所以放行也要把表名带出来。"""
        d = _authorizer(mode="warn").check(
            _user(), datasource=DEFAULT, tables=["orders", "salary"], default=DEFAULT,
        )
        assert d.allowed is True
        assert d.narrowed_tables == ["salary"]

    def test_case_and_schema_prefix_do_not_defeat_the_gate(self):
        d = _authorizer().check(
            _user(), datasource=DEFAULT,
            tables=["ORDERS", "db1.customers"], default=DEFAULT,
        )
        assert d.allowed is True, "大小写与 schema 前缀不该被读成「声明之外的表」"

    def test_no_semantic_basis_skips_a3(self):
        """**基准不存在 != 判定不通过。**

        没接语义层的部署 declared 为空,此时若判「表 ⊆ ∅」就会拒掉一切 ——
        把一次安全改进做成事故,正是 §8.2 要避免的形态。A1/A2 仍然生效。
        """
        auth = _authorizer(declared=False)
        d = auth.check(_user(), datasource=DEFAULT, tables=["anything"], default=DEFAULT)
        assert d.allowed is True
        assert d.narrowed_tables == []

    def test_empty_declared_set_also_skips_a3(self):
        auth = _authorizer(declared=set())
        assert auth.check(
            _user(), datasource=DEFAULT, tables=["anything"], default=DEFAULT,
        ).allowed

    def test_tables_resolved_from_sql_when_not_given(self):
        """调用方不给表清单时自己解析 —— 判定只有一份实现(I1 的同一诉求)。"""
        d = _authorizer().check(
            _user(), datasource=DEFAULT,
            sql="SELECT count(*) FROM orders", default=DEFAULT,
        )
        assert d.allowed is True

    def test_undeclared_table_found_by_resolving_the_sql(self):
        d = _authorizer().check(
            _user(), datasource=DEFAULT,
            sql="SELECT * FROM salary", default=DEFAULT,
        )
        assert d.allowed is False and d.narrowed_tables == ["salary"]

    def test_subquery_and_join_tables_are_all_checked(self):
        """只查 FROM 会漏 —— 子查询/联表里的表同样是「触及」。”"""
        d = _authorizer().check(
            _user(), datasource=DEFAULT,
            sql=(
                "SELECT * FROM orders o JOIN customers c ON o.cid = c.id "
                "WHERE o.amt > (SELECT avg(amt) FROM payroll)"
            ),
            default=DEFAULT,
        )
        assert d.allowed is False
        assert d.narrowed_tables == ["payroll"]

    def test_unparseable_sql_is_closed_in_enforce(self):
        """解析不了 = 无法确认触及了什么,与 §10 的 RLS 注入失败同向:
        没有下层兜底的方向必须严。"""
        d = _authorizer().check(
            _user(), datasource=DEFAULT, sql="SELECT FROM WHERE ((", default=DEFAULT,
        )
        assert d.allowed is False
        assert d.reason == "unresolved"

    def test_unparseable_sql_passes_in_warn(self):
        d = _authorizer(mode="warn").check(
            _user(), datasource=DEFAULT, sql="SELECT FROM WHERE ((", default=DEFAULT,
        )
        assert d.allowed is True

    def test_a3_is_skipped_when_caller_passes_no_sql_and_no_tables(self):
        """既没有表清单也没有 SQL → 无从判定 → 不拦(A1/A2 已过)。"""
        assert _authorizer().check(_user(), datasource=DEFAULT, default=DEFAULT).allowed


# ── CTE 别名不是表 ────────────────────────────────────────────────


class TestCteAliasIsNotATable:
    """``FROM <cte>`` 在 sqlglot 里同样是 ``exp.Table``。

    不排除 CTE 名,就会把别名当成物理表送进 A3 —— 在 enforce 下造成**与权限
    无关**的误拒(有 CTE 的合法查询一律被拦)。列限定符那一半此前已防住,
    ``FROM`` 这一半没有。同仓 ``services/sql/guard.py`` 是正确写法,两份判据
    必须同源。
    """

    def test_referenced_tables_excludes_the_cte_name(self):
        assert referenced_tables(
            "WITH selected AS (SELECT id FROM orders) SELECT * FROM selected",
            "mysql",
        ) == {"orders"}

    def test_a_legitimate_cte_query_is_not_denied(self):
        """用户可见后果:这条查询没碰任何未声明表,不该被拒。"""
        d = _authorizer().check(
            _user(), datasource=DEFAULT,
            sql=(
                "WITH big AS (SELECT * FROM orders WHERE amt > 100) "
                "SELECT count(*) FROM big"
            ),
            default=DEFAULT,
        )
        assert d.allowed is True, f"CTE 名被当成未声明表: {d.narrowed_tables}"

    def test_nested_ctes_are_excluded_too(self):
        assert referenced_tables(
            "WITH a AS (SELECT id FROM orders), "
            "b AS (SELECT id FROM a) SELECT * FROM b",
            "mysql",
        ) == {"orders"}

    def test_undeclared_table_inside_a_cte_body_is_still_caught(self):
        """排除别名不能把 CTE 体里的真表一起排掉 —— 否则这道门就成了摆设。"""
        d = _authorizer().check(
            _user(), datasource=DEFAULT,
            sql=(
                "WITH ok AS (SELECT * FROM orders) "
                "SELECT * FROM ok JOIN payroll ON ok.id = payroll.id"
            ),
            default=DEFAULT,
        )
        assert d.allowed is False
        assert d.narrowed_tables == ["payroll"]


# ── 配置 ─────────────────────────────────────────────────────────


class TestMode:
    def test_unknown_mode_is_rejected_at_construction(self):
        """写错配置不得静默降级成 warn —— 那会让 enforce 变成一个错觉。"""
        with pytest.raises(ValueError):
            Authorizer(mode="enfroce")

    def test_modes_are_case_insensitive(self):
        assert Authorizer(mode="ENFORCE").mode == "enforce"


class TestDecision:
    def test_error_tag_is_derived_from_reason(self):
        """reason → ``[ERR:...]`` 标签的映射只有一份(§10 的错误码)。"""
        assert AuthzDecision(False, "no_principal").error_tag() == "AUTHZ_NO_PRINCIPAL"
        assert AuthzDecision(False, "datasource").error_tag() == "AUTHZ_DATASOURCE"
        assert AuthzDecision(False, "table").error_tag() == "AUTHZ_TABLE"

    def test_allowed_decision_has_no_tag(self):
        assert AuthzDecision(True).error_tag() == ""

    def test_decision_is_immutable(self):
        d = AuthzDecision(True)
        with pytest.raises(Exception):
            d.allowed = False  # type: ignore[misc]


def test_the_deny_reasons_are_the_metric_label_domain():
    """拒绝原因与指标接受的标签值必须是**同一份契约**。

    链条是 ``Authorizer.check()`` → ``decision.reason`` →
    ``execute_sql`` 的 ``record_authz_deny(decision.reason)``,而
    ``record_authz_deny`` 会**静默丢弃**域外的 reason(``record_authz_deny``
    是值域的定义处,见 ``core/metrics.py``)。两侧各有一张手抄表,谁也没钉住谁。

    漂移的表现是「拦了但没计数」—— 一个从两个方向都查不出来的缺口:看板
    上这条规则的拒绝率是 0,运维以为没触发,实际是每次触发都掉在地上。
    ``error_tag()`` 也一样:未登记的 reason 退化成通用的 ``AUTHZ_DENIED``,
    连答案里的错误码都指不出是哪一道门拒的。

    (同 ``tests/services/kb/test_ledger.py`` 的台账枚举钉子。)
    """
    from trove.core import metrics as metrics_mod
    from trove.services.authz import enforcer as enforcer_mod

    assert set(enforcer_mod._ERROR_TAGS) == metrics_mod.AUTHZ_DENY_REASONS
    # 兜底码不能是某个登记过的码 —— 否则"未登记"与某个具体原因在答案里同形。
    assert "AUTHZ_DENIED" not in set(enforcer_mod._ERROR_TAGS.values())
    # 每个 reason 有自己的码(§10):两个原因共用一个码,错误码就区分不出拒在哪。
    tags = list(enforcer_mod._ERROR_TAGS.values())
    assert len(tags) == len(set(tags))
