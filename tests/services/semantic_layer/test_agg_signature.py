"""聚合签名对账:候选表达式必须**整式**匹配声明 metric。

签名曾是「取第一个聚合函数 + 列集相交」,三条静默错数通道:

- ``SUM(a)/COUNT(*)`` 只取首函数 ``SUM(a)`` → 比值题静默退化成求和
  (丢弃其余聚合 = 丢弃算式本身),编译成功且 SQL 是错的;
- ``COUNT(DISTINCT x)`` 与 ``COUNT(x)`` 同签名 → 去重计数被普通计数顶替;
- ``SUM(a + b)`` 与 ``SUM(a)`` 列集相交 → 不同度量被判为同一个。

修复后签名是表达式里**全部**聚合函数的有序 ``(函数名, 列集, DISTINCT)`` 元组,
逐项相等才算命中。``COUNT(*)`` 空列集通配是有意保留的放宽。
"""
import pytest

from trove.services.semantic_layer.compiler import (
    CompileMiss,
    SemanticCompiler,
)
from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticField,
    SemanticMetric,
    SemanticModel,
    SemanticRelationship,
)


def _field(name):
    return SemanticField(name=name, expression=name)


def _model(*metrics):
    return SemanticModel(
        name="agg_sig",
        datasets=[
            SemanticDataset(name="loan", primary_key=["loan_id"], fields=[
                _field("loan_id"), _field("client_id"),
                _field("amount"), _field("fee"),
            ]),
            SemanticDataset(name="client", primary_key=["client_id"], fields=[
                _field("client_id"), _field("name"),
            ]),
        ],
        relationships=[
            SemanticRelationship(
                "loan_client", "loan", "client",
                from_columns=["client_id"], to_columns=["client_id"],
                cardinality="M:1",
            ),
        ],
        metrics=list(metrics),
    )


def _summary(amount_expr, *, name="total_loan", **kw):
    return SemanticMetric(name, amount_expr, datasets=["loan"], **kw)


def _compile(model, plan, matched=("loan",)):
    return SemanticCompiler(model).compile_detailed(plan, list(matched))


def _ratio_plan(expr):
    return {
        "tables": ["loan"],
        "aggregation": expr,
        "answer_columns": [expr],
    }


# ── 曾经静默错数的三条通道 ─────────────────────────────────────

def test_ratio_expression_not_reduced_to_its_first_aggregate():
    """SUM(a)/COUNT(*) 不得命中声明的 SUM(a) —— 比值题不能退化成求和。"""
    model = _model(_summary("SUM(loan.amount)"))
    result = _compile(model, _ratio_plan("SUM(loan.amount)/COUNT(*)"))
    assert isinstance(result, CompileMiss), (
        f"比值表达式被静默降级成求和: {getattr(result, 'sql', result)}"
    )
    assert result.reason == "no_metric_match"


def test_distinct_count_not_matched_by_plain_count():
    """COUNT(DISTINCT x) 与 COUNT(x) 不是同一个度量。"""
    model = _model(SemanticMetric(
        "loan_rows", "COUNT(loan.loan_id)", datasets=["loan"]))
    result = _compile(model, _ratio_plan("COUNT(DISTINCT loan.loan_id)"))
    assert isinstance(result, CompileMiss)
    assert result.reason == "no_metric_match"


def test_sum_over_expression_not_matched_by_column_subset():
    """SUM(a + b) 与 SUM(a) 列集相交但不等 —— 不是同一个度量。"""
    model = _model(_summary("SUM(loan.amount)"))
    result = _compile(model, _ratio_plan("SUM(loan.amount + loan.fee)"))
    assert isinstance(result, CompileMiss)
    assert result.reason == "no_metric_match"


# ── 不得过度拒绝 ──────────────────────────────────────────────

def test_count_star_wildcard_still_matches_column_count():
    """COUNT(*) 空列集通配是有意放宽:仍命中声明 COUNT(col)。"""
    model = _model(SemanticMetric(
        "loan_rows", "COUNT(loan.loan_id)", datasets=["loan"]))
    result = _compile(model, _ratio_plan("COUNT(*)"))
    assert not isinstance(result, CompileMiss), result
    assert "COUNT(loan.loan_id)" in result.sql


def test_ratio_metric_matched_by_its_own_expression():
    """声明的 ratio 度量被同式候选命中 → 编译通过且内联**整条**表达式。"""
    model = _model(SemanticMetric(
        "avg_loan", "SUM(loan.amount)/COUNT(loan.loan_id)",
        datasets=["loan"], metric_type="ratio"))
    result = _compile(model, _ratio_plan("SUM(loan.amount)/COUNT(loan.loan_id)"))
    assert not isinstance(result, CompileMiss), result
    # ratio 内联会补 REAL 转型保证整除;整条算式(分子与分母)必须都在
    assert "SUM(loan.amount)" in result.sql
    assert "COUNT(loan.loan_id)" in result.sql
    assert "/" in result.sql


def test_ratio_metric_not_matched_by_bare_sum():
    """反向:声明 ratio 度量不得被裸 SUM 候选命中(分子当整体)。"""
    model = _model(SemanticMetric(
        "avg_loan", "SUM(loan.amount)/COUNT(loan.loan_id)",
        datasets=["loan"], metric_type="ratio"))
    result = _compile(model, _ratio_plan("SUM(loan.amount)"))
    assert isinstance(result, CompileMiss)
    assert result.reason == "no_metric_match"


def test_exact_single_aggregate_still_matches():
    """原样表达式照常命中(防过度拒绝回归)。"""
    model = _model(_summary("SUM(loan.amount)"))
    result = _compile(model, _ratio_plan("SUM(loan.amount)"))
    assert not isinstance(result, CompileMiss), result
    assert "SUM(loan.amount)" in result.sql


@pytest.mark.parametrize("expr", ["SUM(loan.amount)", "MIN(loan.amount)", "AVG(loan.amount)"])
def test_same_shaped_aggregates_match(expr):
    """同形单聚合(函数名+列集一致)照常命中。"""
    model = _model(_summary(expr))
    result = _compile(model, _ratio_plan(expr))
    assert not isinstance(result, CompileMiss), result
