"""RLS 注入 —— 编译路径与快径共用实现的单元测试。

核心回归:``fast_match`` 不过编译器,声明层 row_filter 必须由快径自己补注入
(见 ``trove/services/semantic_layer/rls.py`` 模块 docstring)。
"""

import pytest

from trove.services.semantic_layer import rls
from trove.services.semantic_layer.models import SemanticDataset, SemanticModel


def ds(name="orders", source="", row_filter=""):
    return SemanticDataset(name=name, source=source, row_filter=row_filter)


def model(*datasets):
    return SemanticModel(name="m", datasets=list(datasets))


# ── render_row_filter ─────────────────────────────────────


class TestRenderRowFilter:
    def test_no_dataset_no_predicate(self):
        assert rls.render_row_filter(None, "sqlite", "orders") is None

    def test_empty_row_filter_no_predicate(self):
        assert rls.render_row_filter(ds(), "sqlite", "orders") is None

    def test_bare_column_qualified_to_table(self):
        p = rls.render_row_filter(ds(row_filter="region = 'EU'"), "sqlite", "orders")
        assert p == "(orders.region = 'EU')"

    def test_already_qualified_column_kept(self):
        p = rls.render_row_filter(ds(row_filter="orders.region = 'EU'"), "sqlite", "orders")
        assert p == "(orders.region = 'EU')"

    def test_qualifier_follows_alias(self):
        p = rls.render_row_filter(ds(row_filter="region = 'EU'"), "sqlite", "o")
        assert p == "(o.region = 'EU')"

    def test_unparseable_predicate_injected_raw(self):
        """坏谓词原样注入 —— 宁可执行期报错,不可少一个安全条件。"""
        p = rls.render_row_filter(ds(row_filter="region = 'EU' AND ("), "sqlite", "orders")
        assert p == "(region = 'EU' AND ()"


# ── inject_row_filters ────────────────────────────────────


class TestInjectRowFilters:
    def test_no_rls_declared_returns_sql_untouched(self):
        sql = "SELECT COUNT(*) FROM orders"
        assert rls.inject_row_filters(sql, model(ds()), "sqlite") == sql

    def test_none_model_returns_sql_untouched(self):
        sql = "SELECT COUNT(*) FROM orders"
        assert rls.inject_row_filters(sql, None, "sqlite") == sql

    def test_predicate_injected_into_bare_select(self):
        out = rls.inject_row_filters(
            "SELECT COUNT(*) FROM orders", model(ds(row_filter="region = 'EU'")), "sqlite",
        )
        assert "region = 'EU'" in out
        assert "WHERE" in out.upper()

    def test_existing_where_is_anded(self):
        out = rls.inject_row_filters(
            "SELECT COUNT(*) FROM orders WHERE amount > 0",
            model(ds(row_filter="region = 'EU'")), "sqlite",
        )
        assert "amount > 0" in out
        assert "region = 'EU'" in out

    def test_idempotent(self):
        m = model(ds(row_filter="region = 'EU'"))
        once = rls.inject_row_filters("SELECT COUNT(*) FROM orders", m, "sqlite")
        twice = rls.inject_row_filters(once, m, "sqlite")
        assert twice.count("region = 'EU'") == 1

    def test_matches_by_physical_table_when_name_differs(self):
        """数据集名 ≠ 物理表名时,模板 SQL 用的是物理表名。"""
        m = model(ds(name="orders_ds", source="public.orders", row_filter="region = 'EU'"))
        out = rls.inject_row_filters("SELECT COUNT(*) FROM orders", m, "sqlite")
        assert "region = 'EU'" in out

    def test_alias_used_as_qualifier(self):
        m = model(ds(row_filter="region = 'EU'"))
        out = rls.inject_row_filters("SELECT COUNT(*) FROM orders o", m, "sqlite")
        assert "o.region = 'EU'" in out

    def test_unparseable_sql_raises(self):
        with pytest.raises(rls.RLSInjectionError):
            rls.inject_row_filters("SELECT FROM WHERE ((", model(ds(row_filter="x = 1")), "sqlite")

    def test_unrelated_table_passes_through(self):
        """SQL 未引用声明了 RLS 的数据集 → 无可注入,原样返回(不是错误)。"""
        m = model(ds(name="orders", row_filter="region = 'EU'"))
        sql = "SELECT COUNT(*) FROM something_else"
        assert rls.inject_row_filters(sql, m, "sqlite") == sql

    def test_mixed_model_only_filters_declared_dataset(self):
        """混合模型:只对有 row_filter 的数据集注入,不误伤其余。

        回归:曾把"一条都没注入"当失败,导致模型里只要有一个数据集声明了
        RLS,其余数据集的查询就全部无法走快径。
        """
        m = model(
            ds(name="customers", row_filter="region = 'EU'"),
            ds(name="transactions"),
        )
        out = rls.inject_row_filters("SELECT COUNT(*) FROM transactions", m, "sqlite")
        assert out == "SELECT COUNT(*) FROM transactions"
        out2 = rls.inject_row_filters("SELECT COUNT(*) FROM customers", m, "sqlite")
        assert "region = 'EU'" in out2

    def test_empty_sql_passthrough(self):
        assert rls.inject_row_filters("", model(ds(row_filter="x = 1")), "sqlite") == ""


# ── physical_table ────────────────────────────────────────


class TestPhysicalTable:
    def test_strips_schema(self):
        assert rls.physical_table(ds(name="orders", source="public.orders")) == "orders"

    def test_falls_back_to_name(self):
        assert rls.physical_table(ds(name="Orders")) == "orders"


# ── declared_rls ──────────────────────────────────────────


class TestDeclaredRls:
    def test_lists_only_declared(self):
        m = model(ds(name="a"), ds(name="b", row_filter="x = 1"))
        assert [d.name for d in rls.declared_rls(m)] == ["b"]

    def test_none_model(self):
        assert rls.declared_rls(None) == []
