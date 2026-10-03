"""条件占比构造 + 字面量感知签名 + 表达式值软 MISS。

三条独立但同源的缺口(2026-10 实测,均以「编译成功而 SQL 错」或
「十轮 COMPILE_DRIFT 死循环」收场):

1. **条件被静默丢弃**(聚合签名):``SUM(x) FILTER (WHERE c)`` 的 Where
   挂在聚合**父节点**上,旧签名只看聚合内部 → 单条件聚合与无条件度量同
   签名,问「仅 A 的金额」答「全部金额」;
2. **占比没有可答构造**(匹配第三档):语义模型只有无条件聚合度量,
   占比题(条件聚合 / 全量 × 100)无论怎么写都对不上声明条目,只能
   软 MISS 拒绝。加了生成器产出的占比度量后,plan 的自由拼法
   (FILTER/CASE、``*100`` 位置、NULLIF 守卫)必须归一后对账;
3. **算式值被冻进骨架**(filter value):plan 把子查询写进 condition
   value,``_literal`` 保守加引号 → 权威 SQL 出现
   ``amount < ('SELECT AVG(...)')``,骨架保真校验又要求 gen 逐字复现。
"""
from trove.services.semantic_layer.compiler import (
    CompileMiss,
    CompileResult,
    PartialCompile,
    SemanticCompiler,
    _agg_entry,
    _agg_signature,
    _looks_like_expression_value,
    _share_shape,
    _sig_compatible,
)
from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticField,
    SemanticMetric,
    SemanticModel,
    SemanticRelationship,
)

COND_A = "loan.status = 'a'"  # _norm_cond 后的小写形态
COND_C = "loan.status = 'c'"

FILTER_A = "SUM(loan.amount) FILTER (WHERE loan.status = 'A') * 100.0 / SUM(loan.amount)"
CASE_A = ("SUM(CASE WHEN loan.status = 'A' THEN loan.amount ELSE 0 END)"
          " * 100.0 / NULLIF(SUM(loan.amount), 0)")
CASE_C = ("SUM(CASE WHEN loan.status = 'C' THEN loan.amount ELSE 0 END)"
          " * 100.0 / NULLIF(SUM(loan.amount), 0)")
#: CAST 变体:KB 生成器写占比时常给分子/分母补 CAST(数值安全),而计划侧候选
#: 往往不带 —— 两者必须互认(等价关系不随拼法;声明表达式始终是权威)。
CASE_A_CAST = ("SUM(CASE WHEN loan.status = 'A' THEN CAST(loan.amount AS DOUBLE) ELSE 0 END)"
               " * 100.0 / NULLIF(SUM(CAST(loan.amount AS DOUBLE)), 0)")


def _field(name, datatype=None):
    return SemanticField(name=name, expression=name, datatype=datatype)


def _entry(expr):
    """表达式里**唯一**聚合的签名条目(测试用;占比形态另测)。"""
    from sqlglot import exp, parse_one

    aggs = list(parse_one(expr).find_all(exp.AggFunc))
    assert len(aggs) == 1, f"期望单聚合: {expr}"
    return _agg_entry(aggs[0])


def _model(*metrics, datasets=None):
    return SemanticModel(
        name="share",
        datasets=datasets or [
            SemanticDataset(name="loan", primary_key=["loan_id"], fields=[
                _field("loan_id"), _field("amount"), _field("status"),
                _field("client_id"),
            ]),
            SemanticDataset(name="client", primary_key=["client_id"], fields=[
                _field("client_id"), _field("gender"),
            ]),
        ],
        relationships=[
            SemanticRelationship("loan_client", "loan", "client",
                                 from_columns=["client_id"], to_columns=["client_id"],
                                 cardinality="M:1"),
        ],
        metrics=list(metrics),
    )


def _sum_amount():
    return SemanticMetric("total_loan_amount", "SUM(loan.amount)", datasets=["loan"])


def _share_metric(expr=CASE_A, name="share of loan amount where status is A"):
    return SemanticMetric(name, expr, datasets=["loan"])


def _agg_plan(expr):
    return {"tables": ["loan"], "aggregation": expr, "answer_columns": [expr]}


def _compile(model, plan, matched=("loan",)):
    return SemanticCompiler(model).compile_detailed(plan, list(matched))


# ── 1. 条件进签名:条件不同即不同度量 ─────────────────────────


def test_agg_entry_sees_filter_condition_on_parent_node():
    """``AGG(x) FILTER (WHERE p)``:条件在聚合父节点上,必须被签名看见。"""
    assert _entry("SUM(loan.amount) FILTER (WHERE loan.status = 'A')") == (
        "sum", frozenset({"loan.amount"}), False, frozenset({COND_A}))


def test_agg_entry_case_form_condition_and_then_column():
    """CASE 形态:条件归条件集,被测列取 THEN 分支(条件列不混进列集)。"""
    assert _entry("SUM(CASE WHEN loan.status = 'A' THEN loan.amount ELSE 0 END)") == (
        "sum", frozenset({"loan.amount"}), False, frozenset({COND_A}))


def test_sum_of_ones_normalized_to_count():
    """``SUM(CASE WHEN p THEN 1 ELSE 0 END)`` = 满足 p 的行数,与 COUNT(*) FILTER 同签名。"""
    cond_count = _entry("SUM(CASE WHEN loan.status = 'A' THEN 1 ELSE 0 END)")
    filter_count = _entry("COUNT(*) FILTER (WHERE loan.status = 'A')")
    assert cond_count == filter_count
    assert cond_count[0] == "count"


def test_condition_difference_breaks_compatibility():
    """条件不同 → 两个度量(占比题按枚举值区分就靠这条)。"""
    a = _agg_signature("SUM(loan.amount) FILTER (WHERE loan.status = 'A')")
    c = _agg_signature("SUM(loan.amount) FILTER (WHERE loan.status = 'C')")
    assert not _sig_compatible(a, c)


def test_unconditional_metric_not_matched_by_conditional_candidate():
    """旧通道:条件聚合静默命中无条件度量 → 编译成功而条件被丢。现必须 MISS。"""
    model = _model(_sum_amount())
    result = _compile(model, _agg_plan(
        "SUM(loan.amount) FILTER (WHERE loan.status = 'A')"))
    assert isinstance(result, CompileMiss)
    assert result.reason == "no_metric_match"


def test_conditional_metric_matched_by_conditional_candidate():
    """声明了条件度量后,同条件候选照常命中且内联声明表达式。"""
    model = _model(_sum_amount(), _share_metric(
        "SUM(loan.amount) FILTER (WHERE loan.status = 'A')",
        name="active loan amount"))
    result = _compile(model, _agg_plan(
        "SUM(loan.amount) FILTER (WHERE loan.status = 'A')"))
    assert isinstance(result, CompileResult)
    assert "FILTER" in result.sql and "loan.status = 'A'" in result.sql


def test_case_and_filter_spellings_share_signature():
    """同一条件的 CASE/FILTER 两拼法签名相同(占比题两种写法都要互认)。"""
    assert _sig_compatible(
        _agg_signature("SUM(CASE WHEN loan.status = 'A' THEN loan.amount ELSE 0 END)"),
        _agg_signature("SUM(loan.amount) FILTER (WHERE loan.status = 'A')"),
    )


# ── 2. 占比形态归一 ──────────────────────────────────────────


def test_share_shape_filter_and_case_spellings_are_the_same():
    """FILTER / CASE 两拼法、NULLIF 守卫、``a*100/b`` 与 ``a/b*100`` 全部归一。"""
    variants = [
        FILTER_A,
        CASE_A,
        ("SUM(CASE WHEN loan.status = 'A' THEN loan.amount ELSE 0 END)"
         " / SUM(loan.amount) * 100"),
        "SUM(loan.amount) FILTER (WHERE loan.status = 'A') * 100.0 / SUM(loan.amount)",
    ]
    shapes = {_share_shape(v) for v in variants}
    assert len(shapes) == 1
    cond_e, plain_e, scale = shapes.pop()
    assert cond_e[3] == frozenset({COND_A}) and not plain_e[3]
    assert scale == "percent"


def test_share_shape_cast_variant_is_same_shape():
    """CAST 变体同形态:KB 侧 ``CAST(amount AS DOUBLE)`` 与计划侧的裸列互认。

    占比构造的 CAST 是量纲/类型噪声(SUM 的输入类型不改变"哪一部分占总量的
    百分之几"),归一化时必须穿透 CAST —— 否则 Lane B 生成的带 CAST 占比度量
    永远匹配不上计划候选(整类占比题退回软 MISS)。
    """
    assert _share_shape(CASE_A_CAST) == _share_shape(CASE_A)


def test_share_shape_distinguishes_enum_value():
    """``status='A'`` 与 ``status='C'`` 是两个占比,不互认。"""
    assert _share_shape(CASE_A) != _share_shape(CASE_C)


def test_share_shape_distinguishes_scale():
    """缺 ``*100`` 是 fraction:量纲差 100 倍,不得静默互认。"""
    fraction = _share_shape(
        "SUM(CASE WHEN loan.status = 'A' THEN loan.amount ELSE 0 END) / SUM(loan.amount)")
    assert fraction is not None and fraction[2] == "fraction"
    assert fraction != _share_shape(CASE_A)


def test_share_shape_requires_condition_on_numerator():
    """分母带条件的占比是另一个量(全量占比),不认。"""
    assert _share_shape(
        "SUM(loan.amount) / (SUM(loan.amount) FILTER (WHERE loan.status = 'A')) * 100"
    ) is None


def test_share_shape_rejects_non_share_shapes():
    """非占比表达式(异函数 / 异列比值 / 单聚合)交回常规路径。"""
    assert _share_shape("SUM(loan.amount)/SUM(loan.fee)") is None
    assert _share_shape("SUM(loan.amount)/COUNT(*) * 100") is None
    assert _share_shape("SUM(loan.amount)") is None


def test_count_share_shape_recognized():
    """计数占比(SUM-1 归一为 COUNT,列集空集通配)也进识别面。"""
    shape = _share_shape(
        "SUM(CASE WHEN loan.status = 'A' THEN 1 ELSE 0 END) * 100.0 / COUNT(*)")
    assert shape is not None
    assert shape[0][0] == "count" and shape[0][3] == frozenset({COND_A})
    assert shape[1][0] == "count" and shape[2] == "percent"


# ── 3. 占比第三档匹配(compile 级)────────────────────────────


def test_filter_spelling_matches_declared_case_metric():
    """plan 写 FILTER 拼法 → 命中声明的 CASE 形态占比度量,SQL 用声明表达式。"""
    model = _model(_sum_amount(), _share_metric())
    result = _compile(model, _agg_plan(FILTER_A))
    assert isinstance(result, CompileResult), result
    # 声明的表达式是权威:产物是 CASE 形态(与 plan 的自由拼法无关)
    assert "CASE WHEN loan.status = 'A'" in result.sql
    assert "NULLIF(SUM(loan.amount), 0)" in result.sql
    assert "/ COUNT(*)" not in result.sql  # 不得退化成计数占比


def test_cast_metric_matched_by_plain_candidate():
    """声明带 CAST 的占比度量 ← 不带 CAST 的候选命中;SQL 用声明形态(带 CAST)。"""
    model = _model(_sum_amount(), _share_metric(CASE_A_CAST, name="share cast"))
    result = _compile(model, _agg_plan(CASE_A))
    assert isinstance(result, CompileResult), result
    assert "CASE WHEN loan.status = 'A'" in result.sql
    assert "CAST(loan.amount AS DOUBLE)" in result.sql


def test_share_candidate_with_other_enum_value_does_not_match():
    """``status='C'`` 的占比不得命中 ``status='A'`` 的度量(静默错数通道)。"""
    model = _model(_sum_amount(), _share_metric())
    result = _compile(model, _agg_plan(CASE_C))
    assert isinstance(result, CompileMiss)
    assert result.reason == "no_metric_match"


def test_share_metric_not_matched_by_plain_ratio():
    """反向:声明的占比度量不得被无条件比值候选命中。"""
    model = _model(_sum_amount(), _share_metric())
    result = _compile(model, _agg_plan("SUM(loan.amount)/COUNT(*)"))
    assert isinstance(result, CompileMiss)
    assert result.reason == "no_metric_match"


# ── 4. 表达式型 filter 值 → 软 MISS(骨架不得冻结坏 SQL)────────


class TestExpressionFilterValue:
    def _trans_model(self):
        return _model(
            SemanticMetric("total_trans_amount", "SUM(trans.amount)", datasets=["trans"]),
            datasets=[SemanticDataset(name="trans", primary_key=["trans_id"], fields=[
                _field("trans_id"), _field("amount"), _field("date", "Date"),
            ])],
        )

    def _plan(self, value):
        return {
            "tables": ["trans"],
            "aggregation": "SUM(trans.amount)",
            "answer_columns": ["SUM(trans.amount)"],
            "conditions": [{"field": "trans.amount", "op": "<", "value": value}],
        }

    def test_subquery_value_yields_partial_without_frozen_sql(self):
        """0488 形状:子查询值 → 软 MISS,骨架不含被引号冻坏的子查询。"""
        result = _compile(self._trans_model(), self._plan("(SELECT AVG(amount) FROM trans)"),
                          matched=("trans",))
        assert isinstance(result, PartialCompile), result
        assert result.miss_parts == [
            {"reason": "expression_filter_value", "component": "trans.amount"}]
        assert "AVG" not in result.sql
        assert "WHERE" not in result.sql
        assert result.sql == "SELECT SUM(trans.amount)\nFROM trans"

    def test_plain_value_still_compiles_into_skeleton(self):
        """对照:普通字面量照常进骨架 WHERE(软 MISS 只针对算式值)。"""
        result = _compile(self._trans_model(), self._plan(1000), matched=("trans",))
        assert isinstance(result, CompileResult), result
        assert "WHERE trans.amount < 1000" in result.sql

    def test_having_expression_value_is_soft_miss(self):
        """HAVING 路径同规:算式值不进权威 SQL。"""
        result = _compile(
            self._trans_model(),
            {"tables": ["trans"], "aggregation": "SUM(trans.amount)",
             "answer_columns": ["SUM(trans.amount)"],
             "having": [{"metric": "total_trans_amount", "op": ">",
                         "value": "(SELECT AVG(x) FROM y)"}]},
            matched=("trans",),
        )
        assert isinstance(result, PartialCompile), result
        assert result.miss_parts == [
            {"reason": "expression_filter_value", "component": "total_trans_amount"}]
        assert "HAVING" not in result.sql


class TestLooksLikeExpressionValue:
    def test_expression_forms_detected(self):
        assert _looks_like_expression_value("(SELECT AVG(amount) FROM trans)")
        assert _looks_like_expression_value("SUM(loan.amount)")
        assert _looks_like_expression_value(" ( select 1")
        assert _looks_like_expression_value("(COUNT(*) / 2)")
        # 计划显式带引号包着算式 → 照样识别(冻进骨架比软 MISS 更坏)
        assert _looks_like_expression_value("'(SELECT 1)'")

    def test_literals_not_flagged(self):
        for value in (None, 42, 3.5, True, "", "Prague", "contract finished",
                      "1998%", "'M'", "M", "sum", "average weekly issuance"):
            assert not _looks_like_expression_value(value), value
