"""Chart inference + ECharts/ASCII output tests (deterministic, zero LLM)."""

from __future__ import annotations

from trove.core.config import AgentConfig
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

    def test_bare_year_column_infers_line(self):
        """4 位整数年份值会被数值判定吞掉,必须仍当作维度 → 有图。"""
        rows = [["2020", 120], ["2021", 135], ["2022", 99]]
        spec = infer_chart(["year", "loan_count"], rows)
        assert spec.chart_type == "line"
        assert spec.dimension == "year"
        assert spec.measures == ["loan_count"]

    def test_year_like_values_infer_line_even_with_expression_alias(self):
        rows = [["2020", 120], ["2021", 135], ["2022", 99]]
        spec = infer_chart(["strftime('%Y', issue_date)", "loan_count"], rows)
        assert spec.chart_type == "line"
        assert spec.measures == ["loan_count"]

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


class TestDuplicateCategoryAlignment:
    """重复分类值必须聚合对齐——修复前会错位(north 被画成 30 而非 40)。"""

    def test_duplicate_categories_are_summed_and_aligned(self):
        rows = [["east", 10], ["west", 20], ["east", 30], ["north", 40]]
        spec = infer_chart(["region", "amount"], rows)
        payload = build_chart(["region", "amount"], rows, spec, "t")
        assert payload["categories"] == ["east", "west", "north"]
        assert payload["series"][0]["data"] == [40.0, 20.0, 40.0]

    def test_duplicate_categories_multi_measure(self):
        rows = [["a", 1, 2], ["b", 3, 4], ["a", 5, 6]]
        spec = infer_chart(["g", "x", "y"], rows)
        payload = build_chart(["g", "x", "y"], rows, spec, "t")
        assert payload["series"][0]["data"] == [6.0, 3.0]
        assert payload["series"][1]["data"] == [8.0, 4.0]

    def test_pie_with_duplicate_categories(self):
        rows = [["a", 0.4], ["b", 0.35], ["a", 0.05], ["c", 0.2]]
        spec = infer_chart(["cat", "share"], rows)
        assert spec.chart_type == "pie"
        payload = build_chart(["cat", "share"], rows, spec, "t")
        assert payload["categories"] == ["a", "b", "c"]
        assert payload["series"][0]["data"] == [0.45, 0.35, 0.2]


class TestTimeSorting:
    def test_unsorted_months_are_sorted_chronologically(self):
        rows = [["2024-03", 100], ["2024-01", 120], ["2024-02", 80]]
        spec = infer_chart(["month", "v"], rows)
        assert spec.is_time
        payload = build_chart(["month", "v"], rows, spec, "t")
        assert payload["categories"] == ["2024-01", "2024-02", "2024-03"]
        assert payload["series"][0]["data"] == [120.0, 80.0, 100.0]

    def test_quarter_codes_sort(self):
        rows = [["2024Q2", 120], ["2023Q4", 90], ["2024Q1", 100]]
        spec = infer_chart(["period", "v"], rows)
        assert spec.chart_type == "line"
        payload = build_chart(["period", "v"], rows, spec, "t")
        assert payload["categories"] == ["2023Q4", "2024Q1", "2024Q2"]

    def test_bare_quarters_sort(self):
        rows = [["Q3", 80], ["Q1", 100], ["Q2", 120]]
        spec = infer_chart(["quarter", "v"], rows)
        assert spec.chart_type == "line"
        payload = build_chart(["quarter", "v"], rows, spec, "t")
        assert payload["categories"] == ["Q1", "Q2", "Q3"]

    def test_unparseable_dimension_keeps_row_order(self):
        """非时间维度(即使列名带 month)解析失败 → 保行序,不瞎排。"""
        rows = [["b", 1], ["a", 2]]
        spec = infer_chart(["region", "v"], rows)
        payload = build_chart(["region", "v"], rows, spec, "t")
        assert payload["categories"] == ["b", "a"]


class TestAdvancedTimeDetection:
    def test_epoch_timestamp_is_time_dimension(self):
        rows = [[1700000000, 100], [1700086400, 120]]
        spec = infer_chart(["created_at", "v"], rows)
        assert spec.chart_type == "line"
        assert spec.is_time

    def test_compact_yyyymm_is_time_dimension(self):
        rows = [["202401", 100], ["202402", 120]]
        spec = infer_chart(["period", "v"], rows)
        assert spec.chart_type == "line"
        assert spec.is_time

    def test_percentage_strings_are_measures(self):
        rows = [["east", "78%"], ["west", "22%"]]
        spec = infer_chart(["region", "share"], rows)
        assert spec.chart_type == "pie"
        assert spec.measures == ["share"]
        payload = build_chart(["region", "share"], rows, spec, "t")
        assert payload["series"][0]["data"] == [78.0, 22.0]

    def test_mixed_percent_and_fraction_column(self):
        rows = [["a", 0.5], ["b", "30%"]]
        spec = infer_chart(["g", "p"], rows)
        assert spec.chart_type == "pie"


class TestSemanticHints:
    def test_hint_turns_obscure_period_column_into_time(self):
        """语义模型声明为时间的字段(值形态无法被正则识别)→ hint 兜底。"""
        rows = [["P1", 100], ["P2", 120]]
        # 无 hint:name 无时间词、值非时间形态 → bar
        plain = infer_chart(["bucket", "v"], rows)
        assert plain.chart_type == "bar"
        # 有 hint:声明为时间 → line
        hinted = infer_chart(["bucket", "v"], rows, {"time_columns": ["bucket"]})
        assert hinted.chart_type == "line"
        assert hinted.is_time

    def test_hint_matches_table_qualified_tail(self):
        spec = infer_chart(
            ["loan.date", "v"], [["2024-01", 1]],
            {"time_columns": ["date"]},
        )
        assert spec.is_time


class TestTruncationFlag:
    def test_more_than_four_measures_truncates(self):
        cols = ["g", "m0", "m1", "m2", "m3", "m4", "m5"]
        rows = [["r"] + [float(i) for i in range(6)]]
        spec = infer_chart(cols, rows)
        assert spec.truncated is True
        assert len(spec.measures) == 4

    def test_category_cap_sets_truncated(self):
        rows = [[f"c{i}", float(i)] for i in range(210)]
        spec = infer_chart(["g", "v"], rows)
        payload = build_chart(["g", "v"], rows, spec, "t")
        assert payload["truncated"] is True
        assert len(payload["categories"]) == 200

    def test_under_caps_not_truncated(self):
        spec = infer_chart(["g", "v"], [["a", 1], ["b", 2]])
        payload = build_chart(["g", "v"], [["a", 1], ["b", 2]], spec, "t")
        assert payload["truncated"] is False


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


class TestChartNodeSemanticHints:
    """图表节点接入语义层时间字段提示:值形态无法识别的声明时间列 → 折线。"""

    async def test_semantic_time_hint_steers_line(self):
        from trove.services.semantic_layer.models import (
            SemanticDataset,
            SemanticField,
            SemanticMetric,
            SemanticModel,
        )

        class _FakeLayer:
            def model(self):
                return SemanticModel(
                    name="m",
                    datasets=[
                        SemanticDataset(name="loan", fields=[
                            SemanticField(name="period", expression="period",
                                          is_time=True, datatype="String"),
                            SemanticField(name="amount", expression="amount",
                                          semantic_role="measure"),
                        ]),
                    ],
                    metrics=[
                        SemanticMetric("total", "SUM(loan.amount)", datasets=["loan"]),
                    ],
                )

        node = make_chart(semantic_layer=_FakeLayer())
        # period 列:名字无时间词、值 P1/P2 无法被正则识别为时间
        state = WorkflowState(
            session_id="s1", question="按期间趋势",
            matched_tables=["loan"],
            columns=["period", "total"],
            rows=[["P1", 100], ["P2", 120]],
            row_count=2,
        )
        out = await node(state)
        assert out["chart"]["type"] == "line"

    async def test_no_semantic_layer_falls_back_to_heuristics(self):
        node = make_chart(semantic_layer=None)
        state = WorkflowState(
            session_id="s1", question="按期间",
            matched_tables=["loan"],
            columns=["bucket", "total"],
            rows=[["P1", 100], ["P2", 120]],
            row_count=2,
        )
        out = await node(state)
        assert out["chart"]["type"] == "bar"


class _ChartToolGateway:
    """LLM gateway mock that answers a chat_full tool call with plot_chart args."""

    def __init__(self, decision: dict):
        self.decision = decision
        self.calls = []

    async def chat(self, model, messages, **kwargs):
        return "OK"

    async def chat_full(self, model, messages, tools=None, **kwargs):
        import json as _json

        self.calls.append({"model": model, "tools": tools, "kwargs": kwargs})
        return {
            "content": "",
            "tool_calls": [
                {"id": "1", "name": "plot_chart", "arguments": _json.dumps(self.decision)},
            ],
        }


class TestChartLLMDecision:
    """LLM 判定图表:是否画图 + 类型 + 维度/度量列,失败回退确定性推断。"""

    async def _node(self, gateway, chart_llm=True):
        config = AgentConfig(target="mock/model")
        config.chart_llm = chart_llm
        return make_chart(llm=gateway, config=config, semantic_layer=None)

    async def test_llm_bar_decision_builds_payload(self):
        gateway = _ChartToolGateway({
            "chartable": True, "chart_type": "bar",
            "dimension": "region", "measures": ["amount"],
        })
        node = await self._node(gateway)
        state = WorkflowState(
            session_id="s1", question="各地区贷款金额",
            columns=["region", "amount"],
            rows=[["east", 100], ["west", 200]],
            row_count=2, complexity="simple",
        )
        out = await node(state)
        assert out["chart"]["type"] == "bar"
        assert out["chart"]["categories"] == ["east", "west"]
        assert out["chart"]["series"][0]["data"] == [100.0, 200.0]
        assert gateway.calls  # 走了一次工具调用

    async def test_llm_line_decision_with_time_hint(self):
        gateway = _ChartToolGateway({
            "chartable": True, "chart_type": "line",
            "dimension": "bucket", "measures": ["v"],
        })
        node = await self._node(gateway)
        state = WorkflowState(
            session_id="s1", question="趋势",
            columns=["bucket", "v"],
            rows=[["P1", 10], ["P2", 20]],
            row_count=2, complexity="standard",
        )
        out = await node(state)
        assert out["chart"]["type"] == "line"

    async def test_llm_no_chart_clears(self):
        gateway = _ChartToolGateway({"chartable": False})
        node = await self._node(gateway)
        state = WorkflowState(
            session_id="s1", question="平均贷款金额",
            columns=["avg"], rows=[[100]], row_count=1,
        )
        out = await node(state)
        assert out["chart"] is None

    async def test_llm_invalid_dimension_falls_back(self):
        """LLM 判定用了不存在的列 → 校验失败 → 回退确定性推断。"""
        gateway = _ChartToolGateway({
            "chartable": True, "chart_type": "bar",
            "dimension": "nope", "measures": ["amount"],
        })
        node = await self._node(gateway)
        state = WorkflowState(
            session_id="s1", question="各地区贷款金额",
            columns=["region", "amount"],
            rows=[["east", 100], ["west", 200]],
            row_count=2,
        )
        out = await node(state)
        assert out["chart"]["type"] == "bar"  # 确定性回退

    async def test_llm_exception_falls_back(self):
        """LLM 调用异常 → 确定性推断兜底,不阻断链路。"""
        class _Boom:
            async def chat_full(self, model, messages, tools=None, **kwargs):
                raise RuntimeError("boom")

        node = make_chart(
            llm=_Boom(), config=AgentConfig(target="mock/model", chart_llm=True),
        )
        state = WorkflowState(
            session_id="s1", question="各地区贷款金额",
            columns=["region", "amount"],
            rows=[["east", 100], ["west", 200]],
            row_count=2,
        )
        out = await node(state)
        assert out["chart"]["type"] == "bar"

    async def test_chart_llm_disabled_uses_deterministic(self):
        """chart_llm=False → 纯确定性推断,零 LLM 调用。"""
        gateway = _ChartToolGateway({"chartable": False})
        node = await self._node(gateway, chart_llm=False)
        state = WorkflowState(
            session_id="s1", question="各地区贷款金额",
            columns=["region", "amount"],
            rows=[["east", 100], ["west", 200]],
            row_count=2,
        )
        out = await node(state)
        assert out["chart"]["type"] == "bar"
        assert not gateway.calls

    async def test_no_llm_uses_deterministic(self):
        node = make_chart(llm=None)
        state = WorkflowState(
            session_id="s1", question="各地区贷款金额",
            columns=["region", "amount"],
            rows=[["east", 100], ["west", 200]],
            row_count=2,
        )
        out = await node(state)
        assert out["chart"]["type"] == "bar"


class TestChartTool:
    """plot_chart 工具:注册 + 校验 + 载荷组装。"""

    def test_chart_from_decision_valid(self):
        from trove.services.viz.tool import chart_from_decision

        payload, err = chart_from_decision(
            ["region", "amount"],
            [["east", 100], ["west", 200]],
            {"chartable": True, "chart_type": "bar", "dimension": "region", "measures": ["amount"]},
            title="各地区",
        )
        assert err == ""
        assert payload["type"] == "bar"
        assert payload["categories"] == ["east", "west"]

    def test_chart_from_decision_no_chart(self):
        from trove.services.viz.tool import chart_from_decision

        payload, err = chart_from_decision(
            ["avg"], [[100]],
            {"chartable": False},
        )
        assert payload is None and err == ""

    def test_chart_from_decision_invalid_dimension(self):
        from trove.services.viz.tool import chart_from_decision

        payload, err = chart_from_decision(
            ["region", "amount"],
            [["east", 100]],
            {"chartable": True, "chart_type": "bar", "dimension": "nope", "measures": ["amount"]},
        )
        assert payload is None and "not a result column" in err

    def test_chart_from_decision_invalid_type(self):
        from trove.services.viz.tool import chart_from_decision

        payload, err = chart_from_decision(
            ["region", "amount"],
            [["east", 100]],
            {"chartable": True, "chart_type": "scatter", "dimension": "region", "measures": ["amount"]},
        )
        assert payload is None and "chart_type" in err

    def test_chart_from_decision_non_numeric_measure(self):
        from trove.services.viz.tool import chart_from_decision

        payload, err = chart_from_decision(
            ["region", "name"],
            [["east", "bob"], ["west", "amy"]],
            {"chartable": True, "chart_type": "bar", "dimension": "region", "measures": ["name"]},
        )
        assert payload is None and "not numeric" in err

    async def test_registry_handler_sets_payload(self):
        from trove.services.viz.tool import build_chart_registry

        registry = build_chart_registry(
            ["region", "amount"],
            [["east", 100], ["west", 200]],
            title="各地区",
        )
        handler = registry.handlers()["plot_chart"]
        obs = await handler({
            "chartable": True, "chart_type": "bar",
            "dimension": "region", "measures": ["amount"],
        })
        assert obs.startswith("OK ")
        assert registry.chart_payload["type"] == "bar"

    async def test_registry_handler_no_chart_sets_none(self):
        from trove.services.viz.tool import build_chart_registry

        registry = build_chart_registry(["region", "amount"], [["east", 100]])
        handler = registry.handlers()["plot_chart"]
        assert await handler({"chartable": False}) == "NO_CHART"
        assert registry.chart_payload is None