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
version: "0.2.0.dev0"
name: shop
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

    def test_one_to_n_has_no_fanout_edge_but_declares_the_join(self):
        """1:N 不构成行倍增(F5-a 静默),但连接键仍然是被声明的路径
        (F5-b 用它)—— 两份视图同源、判据不同。"""
        ctx = build_object_context(_model_with(_rel(cardinality="1:N")))
        assert ctx is not None
        assert ctx.fanout_edges == ()
        assert [j.name for j in ctx.declared_joins] == ["r"]

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

    def test_dedup_exemption_silences_fanout_only(self):
        """显式 dedup 豁免 → 不是行倍增边(与编译器同款豁免串比较),
        但键的声明仍在(F5-b 视图照收)。"""
        ctx = build_object_context(_model_with(_rel(fan_out="dedup")))
        assert ctx is not None
        assert ctx.fanout_edges == ()
        assert len(ctx.declared_joins) == 1

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
        """1:N 边:不构成行倍增(F5-a 静默);按声明键连接(F5-b 放行)。"""
        ctx = build_object_context(_model_with(_rel(cardinality="1:N")))
        assert ctx is not None
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


# ── F5-b 声明键守卫 ─────────────────────────────────────────

#: 声明对:trans.account_id → disp_tbl.client_id(跨名,任何方向都合法)。
DECLARED_EQ = "ON t.account_id = d.client_id"


def _same_name_ctx() -> ObjectContext:
    """同名列声明(account_id → account_id):USING 判定用。"""
    ctx = build_object_context(
        _model_with(_rel(from_columns=["account_id"], to_columns=["account_id"]))
    )
    assert ctx is not None
    return ctx


class TestDeclaredKeyGuard:
    """F5-b:有声明关系的两张表按**非声明键**连接 → 拦(未声明的连接路径)。

    编译期对位:``ambiguous_join_path`` 硬 MISS —— 生成侧自由 SQL 没有
    骨架保真兜底,这里补上。合法模式(塌缩连接)的前提是两张表**没有**
    直接声明;有声明时,声明就是唯一被认可的路径。
    """

    def _hit(self, sql, ctx=None, lang="zh"):
        return check_object_sql(sql, ctx or _ctx(), lang=lang, dialect="sqlite")

    def test_wrong_key_join_hits(self):
        hits = self._hit(
            "SELECT t.trans_id FROM trans t JOIN disp_tbl d ON t.bank = d.region"
        )
        assert [h["name"] for h in hits] == ["F5-b"]
        assert "[F5-b]" in hits[0]["reason"]
        assert "trans.account_id" in hits[0]["reason"]  # 声明键进报文

    def test_declared_key_join_passes(self):
        assert self._hit(
            f"SELECT t.trans_id FROM trans t JOIN disp_tbl d {DECLARED_EQ}"
        ) == []

    def test_reversed_orientation_passes(self):
        assert self._hit(
            "SELECT d.disp_id FROM disp_tbl d JOIN trans t "
            "ON d.client_id = t.account_id"
        ) == []

    def test_declared_key_plus_extra_condition_passes(self):
        """声明键在场 + 额外非键条件(t.date = d.opened) → 放行。"""
        assert self._hit(
            f"SELECT t.trans_id FROM trans t JOIN disp_tbl d {DECLARED_EQ} "
            "AND t.date = d.opened"
        ) == []

    def test_where_form_join(self):
        assert self._hit(
            "SELECT t.trans_id FROM trans t, disp_tbl d "
            "WHERE t.account_id = d.client_id"
        ) == []
        hits = self._hit(
            "SELECT t.trans_id FROM trans t, disp_tbl d WHERE t.bank = d.region"
        )
        assert [h["name"] for h in hits] == ["F5-b"]

    def test_one_sided_declared_column_still_hits(self):
        """一端用了声明列、另一端没有 → 键对不成对,不是声明路径。"""
        hits = self._hit(
            "SELECT t.trans_id FROM trans t JOIN disp_tbl d "
            "ON t.account_id = d.region"
        )
        assert [h["name"] for h in hits] == ["F5-b"]

    def test_composite_keys_pair_positionally(self):
        """zip 位置配对(与编译器同款):b=y 命中;a=y 不成对。"""
        ctx = build_object_context(_model_with(_rel(
            from_columns=["account_id", "client_id"],
            to_columns=["region", "branch"],
        )))
        assert ctx is not None
        assert self._hit(
            "SELECT t.trans_id FROM trans t JOIN disp_tbl d "
            "ON t.client_id = d.branch", ctx,
        ) == []
        hits = self._hit(
            "SELECT t.trans_id FROM trans t JOIN disp_tbl d "
            "ON t.account_id = d.branch", ctx,
        )
        assert [h["name"] for h in hits] == ["F5-b"]

    def test_using_declared_same_name_passes(self):
        assert self._hit(
            "SELECT t.trans_id FROM trans t JOIN disp_tbl d USING (account_id)",
            _same_name_ctx(),
        ) == []

    def test_collapsed_join_without_declaration_passes(self):
        """塌缩连接(trans ↔ disp 经共有的 account_id,两张表间**没有**
        声明)→ 不在守卫范围内,放行 —— 这正是设计要避开的那类误报。"""
        model = _mn_model()  # 声明只连 trans ↔ disp_tbl
        ctx = build_object_context(model)
        assert ctx is not None
        assert self._hit(
            "SELECT t.trans_id FROM trans t JOIN other o ON t.account_id = o.account_id",
            ctx,
        ) == []

    def test_second_table_pair_without_declaration_passes(self):
        hits = self._hit(
            "SELECT o.x FROM other o JOIN another n ON o.y = n.y"
        )
        assert hits == []

    def test_no_linking_equality_passes(self):
        """交叉连接 / 范围连接:没有等值 → 不判(漏判不误拦)。"""
        assert self._hit(
            "SELECT t.trans_id FROM trans t, disp_tbl d"
        ) == []
        assert self._hit(
            "SELECT t.trans_id FROM trans t JOIN disp_tbl d "
            "ON t.date BETWEEN d.start AND d.end"
        ) == []
        assert self._hit(
            "SELECT t.trans_id FROM trans t NATURAL JOIN disp_tbl d"
        ) == []

    def test_or_subtree_not_counted(self):
        """OR 下的等值不保证成立 → 不下钻;无其他连接等值 → 不判。"""
        assert self._hit(
            "SELECT t.trans_id FROM trans t JOIN disp_tbl d "
            "ON t.bank = d.region OR t.account_id = d.client_id"
        ) == []

    def test_unqualified_columns_not_attributed(self):
        """无限定符的列归属不明 → 不参与认定(偏漏判一侧)。"""
        assert self._hit(
            "SELECT t.trans_id FROM trans t JOIN disp_tbl d "
            "ON t.bank = region"
        ) == []

    def test_aliases_resolve_to_physical_tables(self):
        hits = self._hit(
            "SELECT x.trans_id FROM trans AS x JOIN disp_tbl AS y "
            "ON x.bank = y.region"
        )
        assert [h["name"] for h in hits] == ["F5-b"]

    def test_english_message(self):
        hits = self._hit(
            "SELECT t.trans_id FROM trans t JOIN disp_tbl d ON t.bank = d.region",
            lang="en",
        )
        assert "undeclared" in hits[0]["reason"]

    def test_fanout_takes_precedence_in_same_layer(self):
        """同层两个守卫都成立 → 报 F5-a(静默放大优先)。"""
        hits = self._hit(
            "SELECT SUM(t.amount) FROM trans t JOIN disp_tbl d "
            "ON t.bank = d.region"
        )
        assert [h["name"] for h in hits] == ["F5-a"]


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
