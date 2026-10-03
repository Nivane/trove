"""驱动器树骨架 + 比率识别单测(纯函数,只读模型,零 I/O)。

树的三条纪律在此钉死:
  - Add/Sub → decomposable=True(恒等式链);
  - Mul/Div/SafeDivide/Avg → decomposable=False(不硬拆);
  - 环 / 超深 / 不可解析 → 显式 note,绝不无限展开。
"""


from trove.services.analysis.expr_tree import (
    collect_components,
    metric_components,
    metric_ratio_parts,
)
from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticField,
    SemanticMetric,
    SemanticModel,
)


def _model() -> SemanticModel:
    return SemanticModel(
        name="m",
        datasets=[
            SemanticDataset(
                name="sales",
                source="sales",
                fields=[
                    SemanticField(name="amount", expression="sales.amount", semantic_role="measure"),
                    SemanticField(name="cost", expression="sales.cost", semantic_role="measure"),
                    SemanticField(name="region", expression="sales.region", semantic_role="dimension"),
                    SemanticField(name="day", expression="sales.day", datatype="Date", is_time=True),
                ],
            )
        ],
        metrics=[
            SemanticMetric(name="revenue", expression="SUM(sales.amount)", datasets=["sales"]),
            SemanticMetric(name="cost", expression="SUM(sales.cost)", datasets=["sales"]),
            SemanticMetric(name="profit", expression="revenue - cost", datasets=["sales"],
                           metric_type="derived"),
            SemanticMetric(name="margin", expression="revenue / cost", datasets=["sales"],
                           metric_type="ratio"),
            SemanticMetric(name="avg_amount", expression="AVG(sales.amount)", datasets=["sales"]),
            SemanticMetric(name="mixed", expression="(revenue + cost) * revenue", datasets=["sales"],
                           metric_type="derived"),
            # 环:a = b + SUM;b = a + SUM
            SemanticMetric(name="cyc_a", expression="cyc_b + SUM(sales.amount)", datasets=["sales"]),
            SemanticMetric(name="cyc_b", expression="cyc_a + SUM(sales.amount)", datasets=["sales"]),
            # 深链:d7 → d6 → … → d1(每层 + SUM)
            SemanticMetric(name="d1", expression="SUM(sales.amount)", datasets=["sales"]),
            SemanticMetric(name="d2", expression="d1 + SUM(sales.amount)", datasets=["sales"]),
            SemanticMetric(name="d3", expression="d2 + SUM(sales.amount)", datasets=["sales"]),
            SemanticMetric(name="d4", expression="d3 + SUM(sales.amount)", datasets=["sales"]),
            SemanticMetric(name="d5", expression="d4 + SUM(sales.amount)", datasets=["sales"]),
            SemanticMetric(name="d6", expression="d5 + SUM(sales.amount)", datasets=["sales"]),
            SemanticMetric(name="d7", expression="d6 + SUM(sales.amount)", datasets=["sales"]),
        ],
    )


def _by_name(model: SemanticModel, name: str) -> SemanticMetric:
    return next(m for m in model.metrics if m.name == name)


class TestMetricRatioParts:
    def test_avg_becomes_sum_over_count(self):
        m = _model()
        assert metric_ratio_parts(_by_name(m, "avg_amount")) == (
            "SUM(sales.amount)", "COUNT(sales.amount)")

    def test_div(self):
        m = _model()
        assert metric_ratio_parts(_by_name(m, "margin")) == ("revenue", "cost")

    def test_additive_is_none(self):
        m = _model()
        assert metric_ratio_parts(_by_name(m, "profit")) is None


class TestMetricComponents:
    def test_additive_chain_is_decomposable(self):
        m = _model()
        root = metric_components(_by_name(m, "profit"), m)
        assert root["kind"] == "derived" and root["op"] == "-"
        assert root["decomposable"] is True
        assert root["metric"] == "profit" and root["candidate"] == "profit"
        kids = root["children"]
        assert [k["name"] for k in kids] == ["revenue", "cost"]
        assert all(k["metric"] == k["name"] and k["candidate"] == k["name"] for k in kids)
        # 引用被展开:叶子是 SUM 表达式
        assert "SUM(sales.amount)" in kids[0]["expression"]

    def test_division_is_not_decomposable(self):
        m = _model()
        root = metric_components(_by_name(m, "margin"), m)
        assert root["kind"] == "ratio" and root["op"] == "/"
        assert root["decomposable"] is False
        assert [k["candidate"] for k in root["children"]] == ["revenue", "cost"]

    def test_avg_children_are_sum_count(self):
        m = _model()
        root = metric_components(_by_name(m, "avg_amount"), m)
        assert root["kind"] == "ratio" and root["op"] == "avg"
        assert root["decomposable"] is False
        assert [k["candidate"] for k in root["children"]] == [
            "SUM(sales.amount)", "COUNT(sales.amount)"]

    def test_mixed_mul_chain_not_decomposable(self):
        m = _model()
        root = metric_components(_by_name(m, "mixed"), m)
        assert root["op"] == "*" and root["decomposable"] is False
        # 内层加法自身仍是恒等式(性质不继承)
        inner = root["children"][0]
        assert inner["op"] == "+" and inner["decomposable"] is True

    def test_cycle_stops_with_note(self):
        m = _model()
        root = metric_components(_by_name(m, "cyc_a"), m)
        # cyc_a → cyc_b → (cyc_a = 环) + SUM
        b = root["children"][0]
        assert b["metric"] == "cyc_b"
        back = b["children"][0]
        assert back["metric"] == "cyc_a" and back["note"] == "cycle"
        assert back["candidate"] == "cyc_a"

    def test_depth_cap(self):
        m = _model()
        root = metric_components(_by_name(m, "d7"), m, max_depth=3)
        # 逐层展开:d7→d6→d5→d4,第 4 层的引用被深度截停
        node = root
        seen = []
        while node.get("children"):
            node = node["children"][0]
            seen.append(node.get("note"))
        assert "depth" in seen

    def test_unparseable_expression_degrades_to_leaf(self):
        m = _model()
        broken = SemanticMetric(name="broken", expression="SUM(", datasets=["sales"])
        root = metric_components(broken, m)
        assert root["kind"] == "leaf" and root["note"] == "unparseable"
        assert root["candidate"] == "broken"

    def test_plain_metric_is_single_leaf(self):
        m = _model()
        root = metric_components(_by_name(m, "revenue"), m)
        assert root["kind"] == "leaf" and root["children"] == []
        assert root["candidate"] == "revenue"


class TestCollectComponents:
    def test_preorder_dedupe_and_root_exclusion(self):
        m = _model()
        root = metric_components(_by_name(m, "profit"), m)
        comps, truncated = collect_components(root, max_components=4, exclude="profit")
        assert [c["candidate"] for c in comps] == ["revenue", "cost"]
        assert truncated == 0

    def test_truncation_accounted(self):
        m = _model()
        root = metric_components(_by_name(m, "d7"), m)
        comps, truncated = collect_components(root, max_components=2, exclude="d7")
        assert len(comps) == 2
        assert truncated >= 1

    def test_unresolvable_children_skipped(self):
        m = _model()
        root = metric_components(_by_name(m, "mixed"), m)
        comps, truncated = collect_components(root, max_components=8, exclude="mixed")
        # (revenue + cost) 的叶子是度量引用 → 可执行;乘法链本身候选为 None
        assert set(c["candidate"] for c in comps) == {"revenue", "cost"}
        assert truncated == 0
