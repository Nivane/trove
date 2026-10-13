"""对象约束检查单测(F5-a 扇出守卫)。

钉住五件事:

1. **上下文派生**:M:N 且无 ``dedup`` 豁免的声明 → FanoutEdge;1:N / 空模型 /
   dedup → ``None``(无声明部署逐字节静默);数据集名映射到物理表名
   (与 RLS/漂移报告同一份 ``physical_table`` 口径);
2. **触发面 = 三条同时成立**:同层直接源含 M:N 两端 + 非 DISTINCT 非窗口
   SUM/AVG + 未按声明键分组;任一不成立 → 零命中;
3. **刻意收窄的边界**:COUNT/MIN/MAX 不放大、DISTINCT/窗口聚合不算、
   预聚合子查询天然免疫(外层直接源是子查询)、CTE 名不是物理表、
   子查询里的聚合只归那一层判、解析失败不拦;
4. **hits 形状与规则链一致**(``[{name, reason}]``,reason 带 ``[F5-a]``
   前缀)——下游 validation_hits / 评测归因零分支;
5. **确定性**:同输入逐字节同输出;``ctx=None`` / 坏 SQL 绝不抛。
"""
from __future__ import annotations

import dataclasses

import pytest

from trove.services.semantic_layer.models import (
    SemanticModel,
    SemanticRelationship,
)
from trove.services.semantic_layer.object_check import (
    ObjectContext,
    build_object_context,
    check_object_sql,
    context_from_provider,
)
from trove.services.semantic_layer.ossie import parse_ossie

# from_columns=[account_id] / to_columns=[client_id] 是**故意不同名**的:
# 按声明键分组的豁免测试要能区分"分了哪一端的键"。
MN_YAML = """
semantic_model:
  - name: shop
    datasets:
      - name: trans
        source: trans
        primary_key: [trans_id]
      - name: disp
        source: disp_tbl
        primary_key: [disp_id]
    relationships:
      - name: trans_disp
        from: trans
        to: disp
        from_columns: [account_id]
        to_columns: [client_id]
        cardinality: M:N
"""


def _mn_model():
    return parse_ossie(MN_YAML, preferred_dialect="sqlite")


def _ctx() -> ObjectContext:
    ctx = build_object_context(_mn_model())
    assert ctx is not None
    return ctx


def _rel(**kw) -> SemanticRelationship:
    base = dict(
        name="r", from_="trans", to="disp",
        from_columns=["account_id"], to_columns=["client_id"],
        cardinality="M:N",
    )
    base.update(kw)
    return SemanticRelationship(**base)


def _model_with(*rels: SemanticRelationship) -> SemanticModel:
    return dataclasses.replace(_mn_model(), relationships=list(rels))


# ── 上下文派生 ──────────────────────────────────────────────


class TestBuildContext:
    def test_none_model_is_none(self):
        assert build_object_context(None) is None

    def test_no_relationships_is_none(self):
        assert build_object_context(_model_with()) is None

    def test_one_to_n_only_is_none(self):
        """1:N 不构成行倍增 —— 只声明 1:N 的部署检查整体静默。"""
        assert build_object_context(_model_with(_rel(cardinality="1:N"))) is None

    def test_mn_edge_extracted_with_physical_tables(self):
        ctx = _ctx()
        assert len(ctx.fanout_edges) == 1
        edge = ctx.fanout_edges[0]
        assert edge.name == "trans_disp"
        # disp 数据集 source=disp_tbl → 物理名映射(与 RLS 同一口径)
        assert edge.from_table == "trans"
        assert edge.to_table == "disp_tbl"
        assert edge.keys == {"account_id", "client_id"}

    @pytest.mark.parametrize("cardinality", ["M:N", "N:M", "m2m", "many-to-many"])
    def test_mn_spellings_all_extracted(self, cardinality):
        """拼写归一化判定与编译器/对象图同源(复用 _link_fan_out)。"""
        ctx = build_object_context(_model_with(_rel(cardinality=cardinality)))
        assert ctx is not None and len(ctx.fanout_edges) == 1

    def test_dedup_exemption_silences(self):
        """显式 dedup 豁免 → 不是行倍增边(与编译器同款豁免串比较)。"""
        assert build_object_context(
            _model_with(_rel(fan_out="dedup"))
        ) is None

    def test_empty_model_is_none(self):
        assert build_object_context(SemanticModel(name="empty")) is None

    def test_context_from_provider_swallows(self):
        class _Boom:
            def model(self):
                raise RuntimeError("boom")

        assert context_from_provider(_Boom()) is None
        assert context_from_provider(None) is None

    def test_context_from_provider_ok(self):
        class _P:
            def model(self):
                return _mn_model()

        ctx = context_from_provider(_P())
        assert ctx is not None and len(ctx.fanout_edges) == 1


# ── F5-a 触发面 ─────────────────────────────────────────────


class TestFanoutHit:
    def test_scalar_sum_over_mn_join_hits(self):
        hits = check_object_sql(
            "SELECT SUM(t.amount) FROM trans t JOIN disp_tbl d ON t.account_id = d.client_id",
            _ctx(), lang="zh", dialect="sqlite",
        )
        assert len(hits) == 1
        assert hits[0]["name"] == "F5-a"
        assert hits[0]["reason"].startswith("[F5-a]")
        assert "trans_disp" in hits[0]["reason"]
        assert "account_id" in hits[0]["reason"]

    def test_avg_also_hits(self):
        hits = check_object_sql(
            "SELECT AVG(t.amount) FROM trans t JOIN disp_tbl d ON t.account_id = d.client_id",
            _ctx(), dialect="sqlite",
        )
        assert [h["name"] for h in hits] == ["F5-a"]

    def test_group_by_other_column_still_hits(self):
        """按非声明键分组:组内仍是倍增行,拦。"""
        hits = check_object_sql(
            "SELECT d.region, SUM(t.amount) FROM trans t "
            "JOIN disp_tbl d ON t.account_id = d.client_id GROUP BY d.region",
            _ctx(), dialect="sqlite",
        )
        assert [h["name"] for h in hits] == ["F5-a"]

    def test_group_by_declared_key_skips(self):
        """按声明键(任一端)分组 → 组内口径不受整行倍增影响。"""
        for col in ("client_id", "account_id"):
            hits = check_object_sql(
                f"SELECT d.{col}, SUM(t.amount) FROM trans t "
                f"JOIN disp_tbl d ON t.account_id = d.client_id "
                f"GROUP BY d.{col}",
                _ctx(), dialect="sqlite",
            )
            assert hits == [], col

    def test_hit_shape_matches_rules_verify(self):
        hits = check_object_sql(
            "SELECT SUM(t.amount) FROM trans t JOIN disp_tbl d ON t.account_id = d.client_id",
            _ctx(), dialect="sqlite",
        )
        assert set(hits[0]) == {"name", "reason"}

    def test_english_message(self):
        hits = check_object_sql(
            "SELECT SUM(t.amount) FROM trans t JOIN disp_tbl d ON t.account_id = d.client_id",
            _ctx(), lang="en", dialect="sqlite",
        )
        assert "many-to-many" in hits[0]["reason"]

    def test_first_hit_wins_single_element(self):
        """两层都命中也只回一条(与规则链第一条命中即止同纪律)。"""
        hits = check_object_sql(
            "SELECT SUM(t.amount) FROM trans t JOIN disp_tbl d ON t.account_id = d.client_id "
            "UNION ALL "
            "SELECT SUM(t.amount) FROM trans t JOIN disp_tbl d ON t.account_id = d.client_id",
            _ctx(), dialect="sqlite",
        )
        assert len(hits) == 1


# ── 刻意收窄:不拦的写法(遗漏只是不拦,误拦才是成本)───────


class TestNoFalsePositives:
    def test_count_min_max_do_not_hit(self):
        """COUNT/MIN/MAX 不因整行复制而放大。"""
        for agg in ("COUNT(t.trans_id)", "MIN(t.amount)", "MAX(t.amount)"):
            hits = check_object_sql(
                f"SELECT {agg} FROM trans t JOIN disp_tbl d ON t.account_id = d.client_id",
                _ctx(), dialect="sqlite",
            )
            assert hits == [], agg

    def test_distinct_sum_skips(self):
        hits = check_object_sql(
            "SELECT SUM(DISTINCT t.amount) FROM trans t "
            "JOIN disp_tbl d ON t.account_id = d.client_id",
            _ctx(), dialect="sqlite",
        )
        assert hits == []

    def test_window_sum_skips(self):
        """窗口聚合不折叠行 —— 'SUM(...) OVER (...) 会倍增'是错的。"""
        hits = check_object_sql(
            "SELECT SUM(t.amount) OVER (PARTITION BY d.client_id) "
            "FROM trans t JOIN disp_tbl d ON t.account_id = d.client_id",
            _ctx(), dialect="sqlite",
        )
        assert hits == []

    def test_pre_aggregated_subquery_immune(self):
        """先在子查询里聚合,再连接 —— 行倍增只影响已聚合的行。"""
        hits = check_object_sql(
            "SELECT SUM(s.total) FROM "
            "(SELECT account_id, SUM(amount) AS total FROM trans GROUP BY account_id) s "
            "JOIN disp_tbl d ON s.account_id = d.client_id",
            _ctx(), dialect="sqlite",
        )
        assert hits == []

    def test_inner_subquery_agg_judged_by_its_own_layer(self):
        """子查询里的聚合属于那一层;那一层只有一张直接源表 → 不构成边。"""
        hits = check_object_sql(
            "SELECT t.trans_id FROM trans t JOIN disp_tbl d ON t.account_id = d.client_id "
            "WHERE t.amount > (SELECT AVG(amount) FROM trans)",
            _ctx(), dialect="sqlite",
        )
        assert hits == []

    def test_cte_name_is_not_a_physical_table(self):
        """CTE 引用不是物理表 —— FROM trans 指向 CTE 时不构成连接边。"""
        hits = check_object_sql(
            "WITH trans AS (SELECT * FROM trans WHERE amount > 0) "
            "SELECT SUM(t.amount) FROM trans t "
            "JOIN disp_tbl d ON t.account_id = d.client_id",
            _ctx(), dialect="sqlite",
        )
        assert hits == []

    def test_single_table_no_join(self):
        hits = check_object_sql(
            "SELECT SUM(amount) FROM trans", _ctx(), dialect="sqlite",
        )
        assert hits == []

    def test_only_one_endpoint_present(self):
        """只连接了边的一端(另一端缺席)→ 那次行倍增没有发生。"""
        hits = check_object_sql(
            "SELECT SUM(t.amount) FROM trans t JOIN other o ON t.x = o.x",
            _ctx(), dialect="sqlite",
        )
        assert hits == []

    def test_one_to_n_declaration_does_not_hit(self):
        ctx = build_object_context(_model_with(_rel(cardinality="1:N")))
        assert ctx is None  # 无行倍增边 → 上下文整体静默
        assert check_object_sql(
            "SELECT SUM(t.amount) FROM trans t JOIN disp_tbl d ON t.account_id = d.client_id",
            ctx, dialect="sqlite",
        ) == []

    def test_parse_failure_is_silent(self):
        assert check_object_sql("SELECT FROM WHERE (((", _ctx()) == []

    def test_empty_sql_is_silent(self):
        assert check_object_sql("", _ctx()) == []

    def test_none_ctx_is_silent(self):
        assert check_object_sql("SELECT SUM(a) FROM trans t JOIN disp_tbl d ON 1=1", None) == []

    def test_qualified_table_names_resolve(self):
        """库名前缀剥掉(exp.Table.name 口径)—— 限定名同一张表。"""
        hits = check_object_sql(
            "SELECT SUM(t.amount) FROM shop.trans t JOIN shop.disp_tbl d ON t.account_id = d.client_id",
            _ctx(), dialect="sqlite",
        )
        assert [h["name"] for h in hits] == ["F5-a"]

    def test_self_edge_declared_but_never_fires(self):
        """from=to 的声明照收(上下文是声明的忠实快照),判定时跳过。"""
        ctx = build_object_context(_model_with(_rel(from_="trans", to="trans")))
        assert ctx is not None  # 提取不筛,判定才筛
        assert check_object_sql(
            "SELECT SUM(a.amount) FROM trans a JOIN trans b ON a.account_id = b.account_id",
            ctx, dialect="sqlite",
        ) == []

    def test_self_join_edge_skipped(self):
        """直接构造含自连接边的上下文 → 判定跳过,不拦。"""
        model = _mn_model()
        ctx = build_object_context(model)
        assert ctx is not None
        edge = ctx.fanout_edges[0]
        from dataclasses import replace

        self_ctx = ObjectContext(fanout_edges=(replace(edge, to_table="trans"),))
        hits = check_object_sql(
            "SELECT SUM(a.amount) FROM trans a JOIN trans b ON a.account_id = b.account_id",
            self_ctx, dialect="sqlite",
        )
        assert hits == []


# ── 确定性 ──────────────────────────────────────────────────


class TestDeterminism:
    def test_same_input_same_output(self):
        sql = "SELECT SUM(t.amount) FROM trans t JOIN disp_tbl d ON t.account_id = d.client_id"
        first = check_object_sql(sql, _ctx(), dialect="sqlite")
        for _ in range(3):
            assert check_object_sql(sql, _ctx(), dialect="sqlite") == first

    def test_multi_edge_first_declared_wins(self):
        """多条边都成立 → 按声明顺序取第一条(输出不含集合迭代序)。"""
        model = _mn_model()
        ctx = build_object_context(model)
        assert ctx is not None
        edge = ctx.fanout_edges[0]
        from dataclasses import replace

        second = replace(edge, name="second_edge")
        two = ObjectContext(fanout_edges=(edge, second))
        hits = check_object_sql(
            "SELECT SUM(t.amount) FROM trans t JOIN disp_tbl d ON t.account_id = d.client_id",
            two, dialect="sqlite",
        )
        assert "trans_disp" in hits[0]["reason"]
        assert "second_edge" not in hits[0]["reason"]
