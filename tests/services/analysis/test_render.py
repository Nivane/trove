"""瀑布图 payload 渲染单测(纯函数)。

形状契约:categories 首=基期、末=当前,中间=各维度项;series.data 与
categories 等长;measures 标出"增量"语义(前端 ECharts 靠它堆叠)。
"""

import pytest

from trove.services.analysis.render import ratio_waterfall_chart, waterfall_chart

def _table() -> list[dict]:
    return [
        {"dim": "East", "base": 36.0, "current": 45.0, "delta": 9.0, "contribution": 0.9},
        {"dim": "West", "base": 24.0, "current": 25.0, "delta": 1.0, "contribution": 0.1},
    ]


class TestWaterfallChart:
    def test_shape_and_order(self):
        chart = waterfall_chart("为什么涨?", "上期", 60.0, 70.0, _table(), "zh")
        assert chart["type"] == "waterfall"
        assert chart["categories"] == ["上期", "East", "West", "当前"]
        assert chart["series"][0]["data"] == [60.0, 9.0, 1.0, 70.0]
        assert chart["measures"] == ["delta"]
        assert chart["title"] == "为什么涨?"

    def test_en_labels(self):
        chart = waterfall_chart("why?", "prev", 60.0, 70.0, _table(), "en")
        assert chart["categories"][-1] == "Current"
        assert chart["dimension"] == "dimension contribution"

    def test_empty_table_returns_none(self):
        assert waterfall_chart("q", "上期", 0.0, 1.0, [], "zh") is None

    def test_title_truncated(self):
        chart = waterfall_chart("问" * 100, "上期", 1.0, 2.0, _table(), "zh")
        assert chart["title"] == "问" * 60


class TestRatioWaterfallChart:
    def test_three_effects_between_base_and_current(self):
        effects = {"within": 0.2, "composition": -0.1, "interaction": 0.05}
        chart = ratio_waterfall_chart("率为何变?", "zh", 0.5, effects, 0.65)
        assert chart["categories"] == ["基期", "本征效应", "结构效应", "交叉效应", "当前"]
        assert chart["series"][0]["data"] == [0.5, 0.2, -0.1, 0.05, 0.65]
        # 与 shift-share 恒等式同构:基期 + 三效应 == 当前
        assert 0.5 + 0.2 - 0.1 + 0.05 == pytest.approx(0.65)

    def test_en_labels(self):
        chart = ratio_waterfall_chart("why?", "en", 0.5, {"within": 0.1, "composition": 0.0, "interaction": 0.0}, 0.6)
        assert chart["categories"][1] == "Within"

    def test_no_effects_returns_none(self):
        assert ratio_waterfall_chart("q", "zh", 0.5, {}, 0.6) is None
