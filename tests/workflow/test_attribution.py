"""Tests for the attribution/root-cause analysis extension.

Covers:
  - deterministic contribution math (`_contribution`)
  - base-period derivation (`_base_period`)
  - waterfall chart payload + ASCII fallback renderer
  - attribution intent detection (`has_attribution_signal` + verify/classify)
  - attribution node: share / prev_period baselines, depth=2 drilldown,
    graceful degradation (disabled / no plan / no connectors)
  - graph link: reflect OK → attribution → insights → output
"""

import json

import pytest

from trove.core.config import AgentConfig
from trove.workflow.graphs import GraphServices, build_graphs
from trove.workflow.intent import (
    Intent,
    classify_intent,
    has_attribution_signal,
    verify_intent,
)
from trove.workflow.nodes.attribution import (
    _base_period,
    _contribution,
    _waterfall_chart,
    make_attribution,
)
from trove.workflow.nodes.query_sketch import make_query_sketch
from trove.workflow.nodes.output import output
from trove.workflow.state import WorkflowState
from trove.services.viz.spark import render_waterfall_ascii


# ── deterministic contribution math ──────────────────────────

class TestContribution:
    def test_signed_contribution_sums_to_abs(self):
        """delta_i/total_abs:正负项共存,按 |contribution| 降序。"""
        rows = _contribution({"a": 100, "b": 200}, {"a": 150, "b": 160})
        assert len(rows) == 2
        # delta: a=50(正贡献), b=-40(负贡献);total_abs=90
        assert rows[0]["dim"] == "a"
        assert rows[0]["contribution"] == pytest.approx(50 / 90)
        assert rows[1]["dim"] == "b"
        assert rows[1]["contribution"] == pytest.approx(-40 / 90)
        # 降序
        assert abs(rows[0]["contribution"]) >= abs(rows[1]["contribution"])

    def test_zero_total_falls_back_to_share(self):
        """total_abs == 0(无任何变化)→ 退化为占比归因 cur/total。"""
        rows = _contribution({"a": 100, "b": 100}, {"a": 100, "b": 100})
        assert rows[0]["contribution"] == pytest.approx(100 / 200)
        assert rows[1]["contribution"] == pytest.approx(100 / 200)

    def test_missing_dimension_treated_as_zero(self):
        """基期/当前缺失的维度按 0 计(新出现/消失的分项)。"""
        rows = _contribution({"a": 100}, {"a": 110, "b": 90})
        by_dim = {r["dim"]: r for r in rows}
        assert by_dim["b"]["base"] == 0.0
        assert by_dim["b"]["delta"] == 90.0

    def test_empty_inputs(self):
        assert _contribution({}, {}) == []
        # 只有基期、无当前 → delta=-base,贡献 -1.0(变化全部为消失项)
        assert _contribution({"a": 1.0}, {})[0]["contribution"] == -1.0

    def test_numeric_parse_tolerance(self):
        """字符串数值(带 %/货币符号)可解析。"""
        rows = _contribution({"a": "100"}, {"a": "150"})
        assert rows[0]["delta"] == pytest.approx(50.0)


# ── base-period derivation ───────────────────────────────────

class TestBasePeriod:
    def test_prev_period_shifts_equal_window(self):
        assert _base_period("2024-01-01 ~ 2024-01-31", "prev_period") == (
            ("2024-01-01", "2024-01-31"),
            ("2023-12-01", "2023-12-31"),
        )

    def test_prev_period_single_day(self):
        assert _base_period("2024-03-15 ~ 2024-03-15", "prev_period") == (
            ("2024-03-15", "2024-03-15"),
            ("2024-03-14", "2024-03-14"),
        )

    def test_yoy_shifts_one_year(self):
        assert _base_period("2024-03-01 ~ 2024-03-31", "yoy") == (
            ("2024-03-01", "2024-03-31"),
            ("2023-03-01", "2023-03-31"),
        )

    def test_share_has_no_base(self):
        assert _base_period("2024-01-01 ~ 2024-01-31", "share") is None

    def test_invalid_format_returns_none(self):
        assert _base_period("", "prev_period") is None
        assert _base_period("not a range", "prev_period") is None
        assert _base_period("2024-13-99 ~ x", "prev_period") is None


# ── waterfall chart payload + ASCII fallback ────────────────

class TestWaterfallChart:
    TABLE = [
        {"dim": "a", "base": 100, "current": 150, "delta": 50, "contribution": 0.5},
        {"dim": "b", "base": 200, "current": 160, "delta": -40, "contribution": -0.5},
    ]

    def test_payload_shape(self):
        chart = _waterfall_chart("为什么下降", "基期", 300, 310, self.TABLE, "zh")
        assert chart["type"] == "waterfall"
        assert chart["categories"] == ["基期", "a", "b", "当前"]
        assert chart["series"][0]["data"] == [300, 50, -40, 310]
        assert chart["measures"] == ["delta"]

    def test_empty_table_returns_none(self):
        assert _waterfall_chart("q", "基期", 0, 0, [], "zh") is None

    def test_ascii_renderer(self):
        chart = _waterfall_chart("为什么下降", "基期", 300, 310, self.TABLE, "zh")
        text = render_waterfall_ascii(chart, "zh")
        assert "基期" in text and "当前" in text
        assert "+" in text and "-" in text  # 正负 Δ 段都画出
        assert "300" in text and "310" in text

    def test_ascii_ignores_non_waterfall(self):
        assert render_waterfall_ascii({"type": "bar"}, "zh") == ""


# ── attribution intent detection ─────────────────────────────

class TestAttributionIntent:
    def test_strong_attribution_questions(self):
        for q in [
            "为什么营收下降了？",
            "为什么上季度利润下滑？",
            "利润下滑主要来自哪个地区？",
            "哪个地区对收入下降贡献最大？",
            "为什么华东地区交易量下降",
            "Why did revenue drop last quarter?",
            "which region contributed most to the decline?",
            "什么原因导致贷款违约增加？",
        ]:
            assert has_attribution_signal(q), q
            assert classify_intent(q) == Intent.ATTRIBUTION, q

    def test_chitchat_why_not_attribution(self):
        """「为什么天是蓝的」无数据词 → 不误判归因。"""
        assert not has_attribution_signal("为什么天是蓝的？")
        assert not has_attribution_signal("Why is the sky blue?")

    def test_plain_query_not_attribution(self):
        """普通取数(无触发词)→ 不误判。"""
        for q in [
            "哪个地区的平均贷款金额最高？",
            "how many accounts have loans",
            "列出各地区的贷款总额",
            "收入是多少？",
        ]:
            assert not has_attribution_signal(q), q

    def test_verify_intent_overrides_to_attribution(self):
        """LLM 判 query,但归因信号命中 → ATTRIBUTION(metadata 优先于它)。"""
        assert verify_intent(
            Intent.QUERY, data_signal=True, attribution_signal=True,
        ) == Intent.ATTRIBUTION
        # 无归因信号 → 原逻辑(数据题 query)
        assert verify_intent(Intent.QUERY, data_signal=True) == Intent.QUERY
        # metadata 仍优先于归因
        assert verify_intent(
            Intent.METADATA, attribution_signal=True,
        ) == Intent.METADATA

    def test_classify_priority_write_metadata_win(self):
        """写意图与 metadata 强信号优先级高于归因。"""
        assert classify_intent("删除为什么会导致数据下降的表") == Intent.WRITE
        assert classify_intent("口径的定义是什么") == Intent.METADATA


# ── attribution node ─────────────────────────────────────────

class RecordingLLM:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def chat(self, model, messages, **kwargs):
        self.calls.append(messages)
        return self._responses.pop(0)

    async def chat_full(self, model, messages, tools=None, **kwargs):
        self.calls.append(messages)
        return {"content": self._responses.pop(0), "tool_calls": []}


@pytest.fixture
async def attr_registry(tmp_path):
    """带时间字段的测试库(students + enrolled DATE),挂确定性语义模型。"""
    from trove.core.types import DatasourceConfig
    from trove.services.datasource.registry import ConnectorRegistry
    from tests.conftest import make_test_semantic_provider

    registry = ConnectorRegistry()
    config = DatasourceConfig(
        name="test_db", type="sqlite",
        connection_params={"path": ":memory:"}, default=True,
    )
    adapter = await registry.register(config, set_default=True)
    await adapter.execute(
        "CREATE TABLE students (id INTEGER PRIMARY KEY, name TEXT, "
        "grade INTEGER, county TEXT, enrolled DATE)"
    )
    await adapter.execute(
        "INSERT INTO students (name, grade, county, enrolled) VALUES "
        "('A',95,'Alameda','2024-01-01'), "
        "('B',88,'Alameda','2024-02-01'), "
        "('C',92,'Orange','2024-01-15'), "
        "('D',75,'Orange','2024-03-01'), "
        "('E',99,'Los Angeles','2024-01-01')"
    )
    registry._test_semantic_provider = await make_test_semantic_provider(
        registry, tmp_path / "kb")
    yield registry
    await registry.close_all()


def on_config(**kwargs):
    cfg = dict(target="mock/model", insights=True, conclusion=True)
    cfg.update(kwargs)
    return AgentConfig(**cfg)


def make_attr_state(**kwargs):
    defaults = {
        "session_id": "s1",
        "run_id": "r1",
        "question": "为什么成绩下降",
        "lang": "zh",
        "matched_tables": ["students"],
        "dialect": "sqlite",
        "datasource": "test_db",
        "attribution_plan": {
            "target_metric": "sum of students.grade",
            "dimensions": ["students.county"],
            "baseline": "share",
            "depth": 1,
        },
    }
    defaults.update(kwargs)
    return WorkflowState(**defaults)


class TestAttributionNode:
    async def test_share_baseline_produces_table_and_narrative(self, attr_registry):
        llm = RecordingLLM(["平均成绩主要来自 Alameda 地区"])
        node = make_attribution(
            llm, on_config(), connectors=attr_registry,
            semantic_layer=attr_registry._test_semantic_provider,
        )
        out = await node(make_attr_state())
        attr = out["attribution"]
        assert attr["baseline"] == "share"
        assert attr["total_delta"] == 449.0  # 全部成绩之和
        assert len(attr["table"]) == 3
        # 叙事来自脚本
        assert attr["narrative"] == "平均成绩主要来自 Alameda 地区"
        assert attr["chart"]["type"] == "waterfall"
        assert out["attribution_hops"]

    async def test_prev_period_baseline(self, attr_registry):
        llm = RecordingLLM(["整体无变化,占比归因"])
        node = make_attribution(
            llm, on_config(), connectors=attr_registry,
            semantic_layer=attr_registry._test_semantic_provider,
        )
        out = await node(make_attr_state(
            attribution_plan={
                "target_metric": "sum of students.grade",
                "dimensions": ["students.county"],
                "baseline": "prev_period",
                "depth": 1,
            },
            time_context="2024-01-01 ~ 2024-03-31",
        ))
        attr = out["attribution"]
        assert attr["baseline"] == "prev_period"
        # 基期为 2023-12-01~31,库内无该期数据 → base=0,delta=current
        assert attr["table"][0]["base"] == 0.0
        assert attr["table"][0]["delta"] == attr["table"][0]["current"]

    async def test_depth2_drilldown(self, attr_registry):
        llm = RecordingLLM(["top 地区内 A 贡献最大"])
        node = make_attribution(
            llm, on_config(), connectors=attr_registry,
            semantic_layer=attr_registry._test_semantic_provider,
        )
        out = await node(make_attr_state(
            attribution_plan={
                "target_metric": "sum of students.grade",
                "dimensions": ["students.county", "students.name"],
                "baseline": "share",
                "depth": 2,
            },
        ))
        attr = out["attribution"]
        assert "drilldown" in attr
        assert attr["drilldown"]["dimension"] == "students.name"
        assert attr["drilldown"]["table"]
        # hop 列表含下钻跳
        assert any(h["hop"] == 2 for h in attr["hops"])

    async def test_disabled_passes_through(self, attr_registry):
        llm = RecordingLLM([])
        node = make_attribution(
            llm, on_config(attribution=type(AgentConfig().attribution)(enabled=False)),
            connectors=attr_registry,
            semantic_layer=attr_registry._test_semantic_provider,
        )
        out = await node(make_attr_state())
        assert out == {}
        assert len(llm.calls) == 0

    async def test_no_plan_passes_through(self, attr_registry):
        llm = RecordingLLM([])
        node = make_attribution(
            llm, on_config(), connectors=attr_registry,
            semantic_layer=attr_registry._test_semantic_provider,
        )
        out = await node(make_attr_state(attribution_plan=None))
        assert out == {}
        assert len(llm.calls) == 0

    async def test_no_connectors_passes_through(self, attr_registry):
        llm = RecordingLLM([])
        node = make_attribution(llm, on_config(), connectors=None,
                                semantic_layer=attr_registry._test_semantic_provider)
        out = await node(make_attr_state())
        assert out == {}

    async def test_unresolvable_metric_degrades(self, attr_registry):
        llm = RecordingLLM([])
        node = make_attribution(
            llm, on_config(), connectors=attr_registry,
            semantic_layer=attr_registry._test_semantic_provider,
        )
        out = await node(make_attr_state(
            attribution_plan={
                "target_metric": "nonexistent metric",
                "dimensions": ["students.county"],
                "baseline": "share",
                "depth": 1,
            },
        ))
        assert out == {}

    async def test_narrative_failure_degrades_with_table(self, attr_registry):
        class Boom:
            async def chat(self, model, messages, **kwargs):
                raise RuntimeError("llm down")

        node = make_attribution(
            Boom(), on_config(), connectors=attr_registry,
            semantic_layer=attr_registry._test_semantic_provider,
        )
        out = await node(make_attr_state())
        attr = out["attribution"]
        assert attr["narrative"] == ""  # 叙事失败不阻断归因表
        assert attr["table"]


# ── output rendering ─────────────────────────────────────────

class TestOutputAttribution:
    async def test_renders_attribution_section(self):
        state = WorkflowState(
            session_id="s1",
            question="为什么营收下降",
            attribution={
                "narrative": "主要是华东地区贡献",
                "baseline": "prev_period",
                "table": [
                    {"dim": "华东", "base": 100, "current": 80,
                     "delta": -20, "contribution": -0.6},
                    {"dim": "华北", "base": 50, "current": 60,
                     "delta": 10, "contribution": 0.4},
                ],
                "chart": {
                    "type": "waterfall",
                    "categories": ["基期", "华东", "华北", "当前"],
                    "series": [{"data": [100, -20, 10, 60]}],
                },
            },
        )
        out = await output(state)
        assert "归因分析" in out["final_response"]
        assert "主要是华东地区贡献" in out["final_response"]
        assert "华东" in out["final_response"] and "-20" in out["final_response"]
        assert "-60.0%" in out["final_response"]

    async def test_no_attribution_no_section(self):
        state = WorkflowState(session_id="s1", question="q")
        out = await output(state)
        assert "归因分析" not in out["final_response"]


# ── query_sketch attribution plan extraction ────────────────

class TestQuerySketchAttribution:
    async def test_attribution_intent_extracts_plan(self, attr_registry):
        """归因 intent → query_sketch 系统提示含归因指导,plan_json 里的
        attribution 块带出到 state.attribution_plan。"""
        llm = RecordingLLM([json.dumps({
            "tables": ["students"],
            "aggregation": "sum(students.grade)",
            "answer_columns": ["students.county", "sum(students.grade)"],
            "attribution": {
                "target_metric": "sum of students.grade",
                "dimensions": ["students.county", "students.name"],
                "baseline": "prev_period",
                "depth": 2,
            },
        })])
        node = make_query_sketch(
            llm, on_config(), connectors=attr_registry,
            semantic_layer=attr_registry._test_semantic_provider,
        )
        state = WorkflowState(
            session_id="s1",
            question="为什么成绩下降",
            lang="zh",
            intent="attribution",
            matched_tables=["students"],
            schema_context="students dataset",
        )
        out = await node(state)
        assert out["attribution_plan"] == {
            "target_metric": "sum of students.grade",
            "dimensions": ["students.county", "students.name"],
            "baseline": "prev_period",
            "depth": 2,
        }
        # 系统提示注入归因指导
        sys_prompt = llm.calls[0][0]["content"]
        assert "attribution" in sys_prompt

    async def test_query_intent_without_block_has_no_plan(self, attr_registry):
        """非归因 intent → 即使 LLM 意外给了 attribution 块也不带出(仅归因意图)。"""
        llm = RecordingLLM([json.dumps({
            "tables": ["students"],
            "aggregation": "sum of students.grade",
            "answer_columns": ["students.county", "sum of students.grade"],
            "attribution": {"target_metric": "x"},
        })])
        node = make_query_sketch(
            llm, on_config(), connectors=attr_registry,
            semantic_layer=attr_registry._test_semantic_provider,
        )
        state = WorkflowState(
            session_id="s1",
            question="平均成绩",
            lang="zh",
            intent="query",
            matched_tables=["students"],
            schema_context="students dataset",
        )
        out = await node(state)
        assert "attribution_plan" not in out


# ── graph link: reflect OK → attribution → insights → output ─

class TestGraphAttributionFlow:
    async def test_attribution_runs_after_reflect_ok(self, attr_registry):
        """attribution_plan 置位 → reflect OK 后进 attribution 节点。

        断言:最终状态带 attribution(归因表 + 叙事),且走通 insights→output。
        """
        llm = RecordingLLM([
            "query",  # route_intent
            "```sql\nSELECT county, SUM(grade) FROM students GROUP BY county;\n```",
            "OK",  # reflect
            "- 华东贡献最大",  # attribution narrative (node)
            "- 总体平稳",  # insights
            "总体平稳",  # conclusion
        ])
        graph = build_graphs(
            GraphServices(
                llm=llm,
                connectors=attr_registry,
                semantic_layer=attr_registry._test_semantic_provider,
                config=on_config(insights=True, conclusion=True),
            ),
            multi_candidate=False, query_sketch=False, agentic=False,
        )["reflection"]
        result = await graph.ainvoke(make_attr_state())
        assert result["verdict"] == "OK"
        assert result["attribution"] is not None
        assert result["attribution"]["table"]
        assert result["insights"] == ["总体平稳"]
        assert "归因分析" in result["final_response"]

    async def test_no_plan_skips_attribution(self, attr_registry):
        """无 attribution_plan → 跳过 attribution,原路径 insights→output。"""
        llm = RecordingLLM([
            "query",
            "```sql\nSELECT county, SUM(grade) FROM students GROUP BY county;\n```",
            "OK",
            "- 共 5 名学生",  # insights
            "共 5 名",  # conclusion
        ])
        graph = build_graphs(
            GraphServices(
                llm=llm,
                connectors=attr_registry,
                semantic_layer=attr_registry._test_semantic_provider,
                config=on_config(insights=True, conclusion=True),
            ),
            multi_candidate=False, query_sketch=False, agentic=False,
        )["reflection"]
        result = await graph.ainvoke(
            make_attr_state(attribution_plan=None, question="学生人数"))
        assert result.get("attribution") is None
        assert "归因分析" not in result["final_response"]
        assert result["insights"] == ["共 5 名学生"]

    async def test_attribution_disabled_skips(self, attr_registry):
        llm = RecordingLLM([
            "query",
            "```sql\nSELECT county, SUM(grade) FROM students GROUP BY county;\n```",
            "OK",
            "- 共 5 名学生",
            "共 5 名",
        ])
        graph = build_graphs(
            GraphServices(
                llm=llm,
                connectors=attr_registry,
                semantic_layer=attr_registry._test_semantic_provider,
                config=on_config(
                    attribution=type(AgentConfig().attribution)(enabled=False),
                ),
            ),
            multi_candidate=False, query_sketch=False, agentic=False,
        )["reflection"]
        result = await graph.ainvoke(make_attr_state())
        assert result.get("attribution") is None
