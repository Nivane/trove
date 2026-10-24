"""目标指标名解析(``resolve_metric_name``)—— 确定性、零 LLM、绝不猜。

覆盖四级:精确 → 归一 → 整段表达式 → 词边界包含;以及两个失败态
(``unknown`` / ``ambiguous``,后者带候选)。
"""

from types import SimpleNamespace

from trove.services.analysis.engine import (
    MetricNameResolution,
    resolve_metric_name,
)


def _provider(*metrics):
    """``[(name, expression), ...]`` → 最小语义层 provider。"""
    model = SimpleNamespace(metrics=[
        SimpleNamespace(name=name, expression=expr)
        for name, expr in metrics
    ])
    return SimpleNamespace(model=lambda: model)


_FIN = _provider(
    ("average Loan amount", "AVG(loan.amount)"),
    ("total Loan amount", "SUM(loan.amount)"),
    ("total Payment amount", "SUM(order.amount)"),
    ("max_loan_amount", "MAX(loan.amount)"),
)


class TestResolveMetricName:
    def test_exact_case_insensitive(self):
        r = resolve_metric_name(_FIN, "Average Loan Amount")
        assert r.name == "average Loan amount"
        assert r.strategy == "exact"

    def test_normalized_underscores_and_spacing(self):
        # 声明名是下划线写法,planner 写成空格版本 —— 归一后相等。
        r = resolve_metric_name(_FIN, "max loan amount")
        assert r.name == "max_loan_amount"
        assert r.strategy == "normalized"

    def test_normalized_collapses_whitespace_and_quotes(self):
        r = resolve_metric_name(_FIN, "`average   loan amount`")
        assert r.name == "average Loan amount"
        assert r.strategy == "normalized"

    def test_expression_match(self):
        r = resolve_metric_name(_FIN, "AVG( loan.amount )")
        assert r.name == "average Loan amount"
        assert r.strategy == "expression"

    def test_containment_unique_longer_raw(self):
        # 声明名是 raw 的真子串(官方口径比 planner 写的更长)。
        r = resolve_metric_name(_FIN, "average Loan amount in monetary units")
        assert r.name == "average Loan amount"
        assert r.strategy == "containment"

    def test_containment_ambiguous_is_not_guessed(self):
        # 「loan amount」同时命中 average / total(以及两个 share 口径类的
        # 名字)—— 不同聚合口径之间绝不替模型选。
        r = resolve_metric_name(_FIN, "loan amount")
        assert r.name is None
        assert r.reason == "ambiguous"
        assert len(r.candidates) >= 2
        assert "average Loan amount" in r.candidates
        assert "total Loan amount" in r.candidates

    def test_unknown(self):
        r = resolve_metric_name(_FIN, "nonexistent metric")
        assert r.name is None
        assert r.reason == "unknown"
        assert r.candidates == ()

    def test_ascii_word_boundary_blocks_plural(self):
        # amount ≠ amounts:包含匹配必须按词边界,不能把复数当同一口径。
        provider = _provider(("total Loan amount", "SUM(loan.amount)"))
        r = resolve_metric_name(provider, "loan amounts")
        assert r.name is None
        assert r.reason == "unknown"

    def test_cjk_substring_is_word_bounded(self):
        # CJK 不算 ASCII 词字符:「贷款金额」应能在「平均贷款金额」里命中。
        provider = _provider(("平均贷款金额", "AVG(loan.amount)"))
        r = resolve_metric_name(provider, "贷款金额")
        assert r.name == "平均贷款金额"
        assert r.strategy == "containment"

    def test_empty_metric_name_and_missing_model(self):
        assert resolve_metric_name(_FIN, "  ") == MetricNameResolution(
            name=None, reason="unknown")
        assert resolve_metric_name(None, "loan").reason == "unknown"

    def test_model_lookup_failure_is_unknown(self):
        def boom():
            raise RuntimeError("kb down")

        r = resolve_metric_name(SimpleNamespace(model=boom), "loan amount")
        assert r.name is None
        assert r.reason == "unknown"
