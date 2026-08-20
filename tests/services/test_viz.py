"""Chart inference + ECharts/ASCII output tests (deterministic, zero LLM)."""

from __future__ import annotations

from trove.services.viz.echarts import build_echarts_option
from trove.services.viz.infer import build_chart, infer_chart, is_numeric_column
from trove.services.viz.spark import render_ascii_bar
from trove.workflow.nodes.chart import make_chart
from trove.workflow.state import WorkflowState


class TestInferChart:
    def test_time_dimension_infers_line(self):
        spec = infer_chart(
            ["month", "amount"],
            [["2024-01", 100], ["2024-02", 150], ["2024-03", 80]],
        )
        assert spec.chart_type == "line"
        assert spec.dimension == "month"
        assert spec.measures == ["amount"]

    def test_categorical_single_measure_is_bar(self):
        spec = infer_chart(
            ["region", "count"],
            [["east", 10], ["west", 20], ["north", 30]],
        )
        assert spec.chart_type == "bar"
        assert spec.measures == ["count"]

    def test_proportional_slices_to_pie(self):
        spec = infer_chart(
            ["category", "shares"],
            [["a", 0.4], ["b", 0.35], ["c", 0.25]],
        )
        assert spec.chart_type == "pie"

    def test_no_numeric_measure_means_no_chart(self):
        spec = infer_chart(["name", "grade"], [["A", "high"], ["B", "low"]])
        assert spec.chart_type == "none"

    def test_empty_rows_no_chart(self):
        spec = infer_chart(["a", "b"], [])
        assert spec.chart_type == "none"

    def test_multi_measure_bar(self):
        spec = infer_chart(
            ["region", "jan", "feb"],
            [["east", 10, 12], ["west", 20, 22]],
        )
        assert spec.chart_type == "bar"
        assert spec.measures == ["jan", "feb"]

    def test_is_numeric_column(self):
        assert is_numeric_column([1, 2, 3, "4"])
        assert not is_numeric_column(["a", "b", "c", 1])
        assert not is_numeric_column([])

    def test_build_chart_payload_shape(self):
        payload = build_chart(
            ["region", "count"], [["east", 10], ["west", 20]],
            infer_chart(["region", "count"], [["east", 10], ["west", 20]]),
            "区域统计",
        )
        assert payload["type"] == "bar"
        assert payload["categories"] == ["east", "west"]
        assert payload["series"][0]["data"] == [10.0, 20.0]
        assert payload["title"] == "区域统计"

    def test_build_chart_none_when_not_chartable(self):
        assert build_chart(["a", "b"], [["x", "y"]], infer_chart(["a", "b"], [["x", "y"]])) is None


class TestECharts:
    def test_option_for_bar(self):
        chart = build_chart(
            ["region", "count"], [["east", 10], ["west", 20]],
            infer_chart(["region", "count"], [["east", 10], ["west", 20]]),
        )
        option = build_echarts_option(chart)
        assert option["xAxis"]["data"] == ["east", "west"]
        assert option["series"][0]["type"] == "bar"

    def test_option_for_pie_flattens_series(self):
        chart = build_chart(
            ["category", "s"], [["a", 0.4], ["b", 0.6]],
            infer_chart(["category", "s"], [["a", 0.4], ["b", 0.6]]),
        )
        option = build_echarts_option(chart)
        assert option["series"][0]["type"] == "pie"
        assert [p["name"] for p in option["series"][0]["data"]] == ["a", "b"]

    def test_none_for_no_chart(self):
        assert build_echarts_option(None) is None


class TestAsciiRender:
    def test_bar_render(self):
        chart = build_chart(
            ["region", "count"], [["east", 10], ["west", 20]],
            infer_chart(["region", "count"], [["east", 10], ["west", 20]]),
            "区域",
        )
        out = render_ascii_bar(chart)
        assert "east" in out and "west" in out and "10" in out
        assert "█" in out

    def test_pie_or_multiseries_falls_back_to_empty(self):
        chart = build_chart(
            ["category", "s"], [["a", 0.4], ["b", 0.6]],
            infer_chart(["category", "s"], [["a", 0.4], ["b", 0.6]]),
        )
        assert render_ascii_bar(chart) == ""


class TestChartNode:
    async def test_chart_node_sets_payload(self):
        node = make_chart()
        state = WorkflowState(
            session_id="s1", question="每个地区贷款金额",
            columns=["region", "amount"],
            rows=[["east", 100], ["west", 200]],
            row_count=2,
        )
        out = await node(state)
        assert out["chart"]["type"] == "bar"
        assert out["chart"]["categories"] == ["east", "west"]

    async def test_chart_node_error_clears_chart(self):
        node = make_chart()
        state = WorkflowState(session_id="s1", question="q", error="boom")
        assert await node(state) == {"chart": None}

    async def test_chart_node_zero_rows_clears(self):
        node = make_chart()
        state = WorkflowState(
            session_id="s1", question="q",
            columns=["a", "b"], rows=[], row_count=0,
        )
        assert await node(state) == {"chart": None}