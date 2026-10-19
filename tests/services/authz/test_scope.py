"""行/列 scope 引擎(确定性 SQL 重写)单测。

钉死的几条纪律(见 ``trove/services/authz/scope.py`` 模块 docstring):

- 未声明 = **逐字节不变**(不解析、不格式化);
- 行过滤 = 表引用子查询,JOIN/子查询/CTE/UNION/多别名下语义正确;
- 列拒绝 = 引用即拒绝,``SELECT *``/``t.*`` 拒绝、``COUNT(*)`` 允许;
- 任何解析不了(列限定符/谓词/SQL)→ fail-closed;
- 声明 RLS 与主体行授权合进同一个子查询,``include_declared_rls`` 由调用方
  声明(编译/快径已注入 → False;生成/工具路径未注入 → True)。
"""

from __future__ import annotations

import pytest

from trove.services.authz.enforcer import Authorizer
from trove.services.authz.policy import Principal
from trove.services.authz.scope import (
    ScopeError,
    apply_scope,
    authorize_and_scope,
)
from trove.services.semantic_layer.models import SemanticDataset, SemanticModel

DS = "fin"


def user(*, row=None, deny=None, grants=frozenset({DS})) -> Principal:
    return Principal(
        subject="7", role="user", grants=grants,
        row_grants=None if row is None else {DS: row},
        column_denials=(
            None if deny is None
            else {DS: {t: frozenset(c) for t, c in deny.items()}}
        ),
    )


def scope(sql, principal, *, model=None, include_declared_rls=True, datasource=DS):
    return apply_scope(
        sql, model=model, principal=principal, datasource=datasource,
        dialect="sqlite", include_declared_rls=include_declared_rls,
    )


def denied(sql, principal, **kwargs) -> ScopeError:
    with pytest.raises(ScopeError) as exc:
        scope(sql, principal, **kwargs)
    return exc.value


class TestUnrestricted:
    def test_no_grants_returns_sql_byte_for_byte(self):
        sql = "SELECT  *\n FROM loans   WHERE  x=1"   # 刻意的非常规空白
        out = scope(sql, user())
        assert out.sql == sql
        assert out.rewritten is False
        assert out.row_filters == ()

    def test_grants_for_another_datasource_do_not_apply(self):
        p = Principal(subject="7", row_grants={"other": {"loans": "a = 1"}})
        sql = "SELECT a FROM loans"
        assert scope(sql, p).sql == sql

    def test_datasource_match_is_case_insensitive(self):
        p = Principal(subject="7", row_grants={"FIN": {"loans": "a = 1"}})
        out = scope("SELECT a FROM loans", p)
        assert "WHERE (loans.a = 1)" in out.sql

    def test_no_principal_is_refused(self):
        with pytest.raises(ScopeError) as exc:
            apply_scope("SELECT 1", model=None, principal=None,
                        datasource=DS, dialect="sqlite")
        assert exc.value.reason == "no_principal"
        assert exc.value.error_tag() == "AUTHZ_NO_PRINCIPAL"


class TestRowFilters:
    def test_single_table_is_wrapped_with_alias_preserved(self):
        out = scope("SELECT l.amount FROM loans l", user(row={"loans": "d = 1"}))
        assert out.sql == (
            "SELECT l.amount FROM (SELECT * FROM loans "
            "WHERE (loans.d = 1)) AS l")
        assert out.row_filters == ("loans",)
        assert out.rewritten is True

    def test_table_without_alias_gets_a_synthetic_alias(self):
        out = scope("SELECT name FROM loans", user(row={"loans": "d = 1"}))
        # 派生表在 Postgres/MySQL 上强制要求别名 —— 合成一个与表名同名,
        # 原来按表名限定的列 ``loans.name`` 仍然解析得到
        assert out.sql == (
            "SELECT name FROM (SELECT * FROM loans WHERE (loans.d = 1)) AS loans")

    def test_each_alias_of_the_same_table_is_wrapped(self):
        out = scope(
            "SELECT l.id FROM loans l JOIN loans l2 ON l.id = l2.id",
            user(row={"loans": "d = 1"}))
        assert out.sql.count("SELECT * FROM loans WHERE (loans.d = 1)") == 2

    def test_join_wraps_only_the_granted_table(self):
        out = scope(
            "SELECT * FROM loans l JOIN client c ON l.cid = c.id",
            user(row={"loans": "d = 1"}))
        assert "FROM (SELECT * FROM loans WHERE (loans.d = 1)) AS l" in out.sql
        assert "JOIN client AS c" in out.sql

    def test_subquery_and_union_are_rewritten_at_each_reference(self):
        out = scope(
            "SELECT * FROM loans UNION SELECT * FROM loans",
            user(row={"loans": "d = 1"}))
        assert out.sql.count("(SELECT * FROM loans WHERE (loans.d = 1))") == 2
        out = scope(
            "SELECT x FROM (SELECT x FROM loans) q",
            user(row={"loans": "d = 1"}))
        assert "(SELECT * FROM loans WHERE (loans.d = 1)) AS loans" in out.sql

    def test_cte_body_is_rewritten_but_cte_reference_is_not_a_table(self):
        out = scope(
            "WITH q AS (SELECT x FROM loans) SELECT x FROM q",
            user(row={"loans": "d = 1"}))
        assert "FROM (SELECT * FROM loans WHERE (loans.d = 1)) AS loans" in out.sql
        assert "FROM q" in out.sql

    def test_cte_shadowing_a_physical_table_is_not_wrapped(self):
        """CTE 名遮蔽物理表:``FROM client`` 指的是 CTE,不该被行过滤/列拒绝。"""
        sql = "WITH client AS (SELECT 1 AS x) SELECT * FROM client"
        out = scope(sql, user(row={"client": "a = 1"},
                              deny={"client": ["x"]}))
        assert out.sql == sql

    def test_predicate_columns_are_table_qualified(self):
        out = scope("SELECT a FROM loans l",
                    user(row={"loans": "region = 'Prague' AND amount > 0"}))
        assert "WHERE (loans.region = 'Prague' AND loans.amount > 0)" in out.sql

    def test_schema_qualified_table_keeps_schema(self):
        out = scope("SELECT a FROM main.loans", user(row={"loans": "d = 1"}))
        assert "FROM (SELECT * FROM main.loans WHERE (loans.d = 1)) AS loans" in out.sql

    def test_unparseable_predicate_is_refused_not_dropped(self):
        err = denied("SELECT a FROM loans", user(row={"loans": "d = = 1"}))
        assert err.reason == "unresolved"
        assert err.tables == ("loans",)

    def test_predicate_with_subquery_is_refused(self):
        err = denied(
            "SELECT a FROM loans",
            user(row={"loans": "d IN (SELECT id FROM other)"}))
        assert err.reason == "unresolved"

    def test_unparseable_sql_is_refused(self):
        err = denied("SELECT FROM WHERE ((", user(row={"loans": "d = 1"}))
        assert err.reason == "unresolved"

    def test_rewrite_is_deterministic(self):
        p = user(row={"loans": "d = 1", "client": "c = 2"})
        sql = "SELECT * FROM loans l JOIN client c ON l.id = c.id"
        assert scope(sql, p).sql == scope(sql, p).sql


class TestColumnDenials:
    DENY = {"client": ["email", "phone"]}

    @pytest.mark.parametrize("sql", [
        "SELECT email FROM client",
        "SELECT CLIENT.EMAIL FROM client",
        "SELECT name FROM client WHERE email LIKE '%x%'",
        "SELECT name FROM client ORDER BY email",
        "SELECT upper(email) FROM client",
        "SELECT name FROM client GROUP BY email",
        "SELECT name FROM client HAVING count(email) > 1",
        "SELECT * FROM client",
        "SELECT client.* FROM client",
        "SELECT q.email FROM client q",
    ])
    def test_blocked_references_are_refused(self, sql):
        err = denied(sql, user(deny=self.DENY))
        assert err.reason == "column"
        assert err.error_tag() == "AUTHZ_COLUMN"
        assert any("email" in c for c in err.columns)

    @pytest.mark.parametrize("sql", [
        "SELECT name FROM client",
        "SELECT COUNT(*) FROM client",
        "SELECT count(1) FROM client",
        "SELECT id, name FROM client WHERE name = 'x'",
    ])
    def test_allowed_references_pass_through(self, sql):
        out = scope(sql, user(deny=self.DENY))
        assert out.sql == sql

    def test_unqualified_blocked_column_is_refused(self):
        """未限定列可能指向被禁列 → 从严拒绝,不猜。"""
        err = denied("SELECT email FROM client c", user(deny=self.DENY))
        assert err.reason == "column"

    def test_unqualified_column_is_checked_against_all_scopes(self):
        """相关子查询里的未限定列沿作用域链检查。"""
        err = denied(
            "SELECT name FROM client c WHERE EXISTS "
            "(SELECT 1 FROM loans l WHERE email = 'x')",
            user(deny=self.DENY))
        assert err.reason == "column"

    def test_derived_table_reads_are_checked_inside_not_outside(self):
        """``q.name`` 本身不带拒绝面;越权的东西在内层投影处被拦。"""
        ok = scope(
            "SELECT q.name FROM (SELECT name FROM client) q",
            user(deny=self.DENY))
        assert ok.sql.startswith("SELECT q.name")
        err = denied(
            "SELECT q.x FROM (SELECT email AS x FROM client) q",
            user(deny=self.DENY))
        assert err.reason == "column"

    def test_cte_body_projection_is_checked(self):
        err = denied(
            "WITH q AS (SELECT * FROM client) SELECT * FROM q",
            user(deny=self.DENY))
        assert err.reason == "column"

    def test_unresolvable_qualifier_is_refused(self):
        err = denied("SELECT ghost.email FROM client", user(deny=self.DENY))
        assert err.reason == "unresolved"

    def test_denials_apply_per_datasource(self):
        p = Principal(subject="7", column_denials={
            "other": {"client": frozenset({"email"})}})
        sql = "SELECT email FROM client"
        assert scope(sql, p).sql == sql

    def test_star_on_table_without_denials_is_allowed(self):
        out = scope("SELECT * FROM loans", user(deny={"client": ["email"]}))
        assert out.sql == "SELECT * FROM loans"


class TestDeclaredRLS:
    MODEL = SemanticModel(datasets=[
        SemanticDataset(name="loans", source="main.loans",
                        row_filter="district_id = 1"),
        SemanticDataset(name="client", source="client"),
    ])

    def test_included_when_caller_declares_not_injected(self):
        out = scope("SELECT a FROM loans", user(), model=self.MODEL)
        assert "WHERE (loans.district_id = 1)" in out.sql
        assert out.row_filters == ("loans",)

    def test_skipped_when_compiler_already_injected(self):
        sql = "SELECT a FROM loans WHERE (district_id = 1)"
        out = scope(sql, user(), model=self.MODEL,
                    include_declared_rls=False)
        assert out.sql == sql                     # 不出现第二份谓词

    def test_declared_and_grant_merge_into_one_subquery(self):
        out = scope("SELECT a FROM loans l", user(row={"loans": "amount > 0"}),
                    model=self.MODEL)
        assert out.sql.count("SELECT * FROM loans") == 1   # 只包一层
        assert ("WHERE (loans.district_id = 1) "
                "AND (loans.amount > 0)") in out.sql

    def test_declared_rls_applies_with_no_principal_grants(self):
        """生成路径此前从未注入声明 RLS —— include=True 时无 grant 也要生效。"""
        out = scope("SELECT a FROM loans", user(), model=self.MODEL)
        assert out.rewritten is True

    def test_model_none_still_applies_principal_grants(self):
        out = scope("SELECT a FROM loans", user(row={"loans": "a > 0"}),
                    model=None)
        assert "WHERE (loans.a > 0)" in out.sql


class TestAuthorizeAndScope:
    def test_authorizer_denial_surfaces_as_scope_error(self):
        authorizer = Authorizer(declared_tables=lambda ds: {"loans"})
        p = user(grants=frozenset())        # 空 grants → 只放行默认源
        with pytest.raises(ScopeError) as exc:
            authorize_and_scope(
                "SELECT * FROM loans", authorizer=authorizer, principal=p,
                model=None, datasource="other", dialect="sqlite", default="fin")
        assert exc.value.reason == "datasource"

    def test_table_denial_surfaces_tables(self):
        authorizer = Authorizer(declared_tables=lambda ds: {"loans"})
        with pytest.raises(ScopeError) as exc:
            authorize_and_scope(
                "SELECT * FROM secret", authorizer=authorizer,
                principal=user(), model=None, datasource=DS,
                dialect="sqlite")
        assert exc.value.reason == "table"
        assert exc.value.tables == ("secret",)

    def test_missing_authorizer_still_applies_scope(self):
        out = authorize_and_scope(
            "SELECT a FROM loans", authorizer=None,
            principal=user(row={"loans": "a = 1"}), model=None,
            datasource=DS, dialect="sqlite")
        assert "WHERE (loans.a = 1)" in out.sql

    def test_allowed_path_returns_scoped_sql(self):
        authorizer = Authorizer(declared_tables=lambda ds: {"loans"})
        out = authorize_and_scope(
            "SELECT a FROM loans", authorizer=authorizer, principal=user(),
            model=None, datasource=DS, dialect="sqlite")
        assert out.sql == "SELECT a FROM loans"


class TestErrorSurface:
    def test_message_carries_tag_and_column_names(self):
        err = denied("SELECT email FROM client",
                     user(deny={"client": ["email"]}))
        text = err.render()
        assert text.startswith("[ERR:AUTHZ_COLUMN]")
        assert "client.email" in text

    def test_unknown_reason_folds_to_unresolved(self):
        err = ScopeError("bogus")
        assert err.reason == "unresolved"
        assert err.error_tag() == "AUTHZ_UNRESOLVED"
