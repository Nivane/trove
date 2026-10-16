"""生成链归因纯核心的测试:L1 档位 / 事实抽取 / plan 解析 / 责任跳 / 报告。"""
from __future__ import annotations

from trove.eval.chain import Predicate, sql_facts


def _f(sql: str):
    got = sql_facts(sql, "mysql")
    assert got is not None and got.ok, sql
    return got


def test_tables_and_join_edges_direction_insensitive():
    a = _f("SELECT a.x FROM aa a JOIN bb b ON a.i = b.i")
    b = _f("SELECT a.x FROM bb b JOIN aa a ON b.i = a.i")
    assert a.tables == ("aa", "bb") == b.tables
    assert a.join_edges == b.join_edges


def test_cte_names_excluded_from_tables():
    got = _f("WITH t AS (SELECT 1 AS x FROM base) SELECT t.x FROM t")
    assert got.tables == ("base",)


def test_conds_flattened_including_subquery():
    got = _f(
        "SELECT * FROM t WHERE t.a = 1 AND t.b IN (2, 3) "
        "AND t.c = (SELECT MAX(c) FROM t2) AND t.d IS NULL"
    )
    assert Predicate((("t", "a"),), "=", ("1",)) in got.conds
    assert any(p.op == "in" and p.values == ("2", "3") for p in got.conds)
    assert any(p.op == "=" and p.values == ("(subquery)",) for p in got.conds)
    assert any(p.op == "raw" for p in got.conds)  # IS NULL 走原文兜底


def test_in_subquery_marker():
    # IN (SELECT …):sqlglot 把它存在 args["query"],不折成 (empty)——
    # 否则它与真·空 IN 列表同形,且与 `= (SELECT …)` 的标记词汇不一致
    got = _f("SELECT * FROM t WHERE t.a IN (SELECT b FROM t2)")
    assert got.conds[0].values == ("(subquery)",)
    lit = _f("SELECT * FROM t WHERE t.a IN (1, 2, 3)")
    assert lit.conds[0].values == ("1", "2", "3")


def test_join_on_is_not_a_cond():
    got = _f("SELECT a.x FROM aa a JOIN bb b ON a.i = b.i WHERE a.k = 7")
    assert len(got.conds) == 1
    assert got.conds[0].cols == (("a", "k"),)


def test_aggs_groups_order_limit_projections():
    got = _f(
        "SELECT s.region, COUNT(DISTINCT s.id) FROM sales s "
        "GROUP BY s.region ORDER BY s.region DESC LIMIT 5"
    )
    assert got.aggs == (("count_distinct", (("s", "id"),)),)
    assert got.groups == ((("s", "region"),),)
    assert got.order == (("region", "desc"),)
    assert got.has_limit is True
    assert len(got.projections) == 2


def test_count_star_is_wildcard_zero_cols():
    got = _f("SELECT COUNT(*) FROM sales")
    assert got.aggs == (("count", ()),)


def test_unparseable_returns_none():
    assert sql_facts("SELECT FROM WHERE ((", "mysql") is None
    assert sql_facts("", "mysql") is None
