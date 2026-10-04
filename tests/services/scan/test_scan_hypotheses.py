"""``scan.hypotheses`` —— LLM 只起草,裁决权在确定性一侧。

这个文件的三个重点是:

  1. **引用不过 → 零查询**(``validate`` 在解析期拦下,一次执行都不花);
  2. **每条假设恰好一行 outcome**,四态闭集,顺序 = 输入序 —— 判不了也
     要把"判不了"写下来;
  3. **缺失 ≠ 0**:某一侧没有行是 ``no_data``(查询跑了、行是空的),
     不是 ``refuted``,更不是 0。
"""

from __future__ import annotations

import json

import pytest

from trove.services.analysis.budget import QueryLedger
from trove.services.scan import hypotheses as hyp
from trove.services.scan.models import Hypothesis
from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticField,
    SemanticMetric,
    SemanticModel,
)

WINDOW = ("2026-09-01", "2026-09-30")
BASE_WINDOW = ("2026-08-01", "2026-08-31")


def _model() -> SemanticModel:
    return SemanticModel(
        name="m",
        datasets=[SemanticDataset(
            name="loan", source="loan",
            fields=[
                SemanticField(name="amount", expression="loan.amount",
                              semantic_role="measure"),
                SemanticField(name="region", expression="loan.region",
                              semantic_role="dimension"),
                SemanticField(name="date", expression="loan.date",
                              datatype="DATE", is_time=True),
            ],
        )],
        metrics=[SemanticMetric(
            name="loan_balance", expression="SUM(loan.amount)", datasets=["loan"],
            agg_time_dimension="loan.date")],
    )


class _SL:
    def __init__(self, model: SemanticModel | None = None) -> None:
        self._model = model if model is not None else _model()

    def model(self) -> SemanticModel:
        return self._model


class _Runner:
    """按窗口给数:``values`` 是 {窗口起点: 值};缺的窗口返回空行。"""

    def __init__(self, values: dict[str, float], error: Exception | None = None):
        self.values, self.error = values, error
        self.sqls: list[str] = []

    async def __call__(self, sql: str, datasource: str):
        self.sqls.append(sql)
        if self.error is not None:
            raise self.error
        for start, value in self.values.items():
            # 半开窗口的基线 SQL 里**也**含当前窗口的起点(作为开区间上界),
            # 所以匹配要钉在 ">= '起点'" 上,不能裸包含。
            if f">= '{start}'" in sql:
                return ["loan_balance"], [[value]]
        return ["loan_balance"], []


def _h(**over) -> Hypothesis:
    base = {"claim": "c", "metric": "loan_balance", "direction": "up"}
    base.update(over)
    return Hypothesis.from_dict(base)


class _LLM:
    """脚本化 LLM:记录 prompts,按脚本出响应。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.prompts: list[str] = []
        self.models: list[str] = []

    async def chat(self, model, messages, **kwargs):
        self.models.append(model)
        self.prompts.append(messages[-1]["content"])
        return self._responses.pop(0)


# ── 解析 ─────────────────────────────────────────────


class TestParseCandidates:
    def test_plain_json(self):
        accepted, rejected = hyp.parse_candidates(json.dumps({
            "hypotheses": [
                {"claim": "a", "metric": "m", "direction": "up", "min_pct": 0.1},
                {"claim": "b", "metric": "m", "direction": "down"},
            ],
        }))
        assert [h.claim for h in accepted] == ["a", "b"]
        assert rejected == []

    def test_fenced_json_is_tolerated(self):
        accepted, _ = hyp.parse_candidates(
            '闲聊```json\n{"hypotheses": [{"claim": "a", "metric": "m", '
            '"direction": "up"}]}\n```尾巴')
        assert [h.claim for h in accepted] == ["a"]

    def test_one_bad_candidate_does_not_sink_the_batch(self):
        accepted, rejected = hyp.parse_candidates(json.dumps({
            "hypotheses": [
                {"claim": "good", "metric": "m", "direction": "up"},
                {"claim": "bad", "metric": "m", "direction": "sideways"},
            ],
        }))
        assert [h.claim for h in accepted] == ["good"]
        assert rejected[0]["claim"] == "bad"
        assert rejected[0]["reason"].startswith("structure:")

    def test_unparseable_response_is_recorded_not_silent(self):
        accepted, rejected = hyp.parse_candidates("对不起,我无法回答")
        assert accepted == []
        assert rejected == [{"claim": "", "reason": "unparseable_response"}]

    def test_dict_without_a_list(self):
        _, rejected = hyp.parse_candidates({"hypotheses": "none"})
        assert rejected[0]["reason"] == "no_hypothesis_list"


class TestValidate:
    def test_reference_ok_costs_nothing(self):
        accepted, rejected = hyp.validate(_SL(), [_h()])
        assert [h.claim for h in accepted] == ["c"] and rejected == []

    def test_unknown_metric_is_rejected_zero_query(self):
        accepted, rejected = hyp.validate(_SL(), [_h(metric="nope")])
        assert accepted == []
        assert "unknown_metric" in rejected[0]["reason"]

    def test_unknown_dimension_is_rejected_zero_query(self):
        accepted, rejected = hyp.validate(_SL(), [_h(dimension="nope")])
        assert accepted == []
        assert "unknown_dimension" in rejected[0]["reason"]

    def test_no_anchor_dataset(self):
        model = _model()
        model.metrics[0].datasets = []
        _, rejected = hyp.validate(_SL(model), [_h()])
        assert rejected[0]["reason"] == "no_anchor_dataset"


class TestPropose:
    async def test_accepts_and_rejects_with_limits(self):
        llm = _LLM([json.dumps({"hypotheses": [
            {"claim": "ok", "metric": "loan_balance", "direction": "up"},
            {"claim": "over", "metric": "loan_balance", "direction": "up"},
        ]})])
        accepted, rejected = await hyp.propose(
            llm, semantic_layer=_SL(), data="- x", limit=1)
        assert [h.claim for h in accepted] == ["ok"]
        assert rejected == [{"claim": "over", "reason": "over_limit"}]

    async def test_model_and_intro_reach_the_prompt(self):
        llm = _LLM([json.dumps({"hypotheses": []})])
        await hyp.propose(
            llm, semantic_layer=_SL(), data="DATA-BLOCK",
            model="openai/gpt-x", intro="INTRO-TEXT")
        assert llm.models == ["openai/gpt-x"]
        assert "INTRO-TEXT" in llm.prompts[0]
        assert "DATA-BLOCK" in llm.prompts[0]
        # 归因路径的开场白替换了扫描路径的默认开场(给模型的前言不许撒谎)
        assert "噪声带" not in llm.prompts[0]

    async def test_scan_path_keeps_the_noise_band_intro(self):
        llm = _LLM([json.dumps({"hypotheses": []})])
        await hyp.propose(llm, semantic_layer=_SL(), data="- x")
        assert "噪声带" in llm.prompts[0]

    async def test_no_llm_is_loud(self):
        from trove.services.scan.models import ScanError

        with pytest.raises(ScanError):
            await hyp.propose(None, semantic_layer=_SL(), data="")


# ── 裁决 ─────────────────────────────────────────────


async def _verify(hs, runner, *, ledger=None, window=WINDOW,
                  base_window=BASE_WINDOW):
    evidence: list[dict] = []
    out = await hyp.verify(
        hs, semantic_layer=_SL(), runner=runner, datasource="demo",
        dialect="sqlite", window=window, base_window=base_window,
        ledger=ledger or QueryLedger(), evidence=evidence)
    return out, evidence


class TestVerify:
    async def test_supported(self):
        runner = _Runner({"2026-09-01": 200.0, "2026-08-01": 100.0})
        out, evidence = await _verify([_h(min_pct=0.5)], runner)
        assert len(out) == 1
        row = out[0]
        assert row.status == "supported"
        assert row.observed["pct_change"] == pytest.approx(1.0)
        assert row.observed["window"] == list(WINDOW)
        assert row.observed["base_window"] == list(BASE_WINDOW)
        assert row.queries == 2
        # 两条查询都留了证据(当期 + 基线),完整 SQL 可复算
        assert [e["purpose"] for e in evidence] == [
            "hypothesis_current", "hypothesis_baseline"]
        assert all(e["sql"] for e in evidence)

    async def test_refuted_when_the_move_is_too_small(self):
        runner = _Runner({"2026-09-01": 105.0, "2026-08-01": 100.0})
        out, _ = await _verify([_h(direction="up", min_pct=0.2)], runner)
        assert out[0].status == "refuted"
        assert "observed +5.0%" in out[0].reason
        assert "up ≥ 20%" in out[0].reason

    async def test_down_direction(self):
        runner = _Runner({"2026-09-01": 50.0, "2026-08-01": 100.0})
        out, _ = await _verify([_h(direction="down", min_pct=0.4)], runner)
        assert out[0].status == "supported"

    async def test_no_data_when_one_side_has_no_rows(self):
        """基线没有行 = 数据缺失,不是「基线是 0」,更不是被推翻。"""
        runner = _Runner({"2026-09-01": 200.0})
        out, _ = await _verify([_h(min_pct=0.1)], runner)
        assert out[0].status == "no_data"
        assert out[0].observed == {}
        assert "no rows in 2026-08-01..2026-08-31" in out[0].reason

    async def test_baseline_zero_with_min_pct_is_unverifiable(self):
        """基线是 0 而假设声明了最小幅度 → 相对变化无定义,判不了。"""
        runner = _Runner({"2026-09-01": 200.0, "2026-08-01": 0.0})
        out, _ = await _verify([_h(min_pct=0.1)], runner)
        assert out[0].status == "unverifiable"
        assert "baseline_zero" in out[0].reason

    async def test_baseline_zero_without_min_pct_still_decides_direction(self):
        runner = _Runner({"2026-09-01": 5.0, "2026-08-01": 0.0})
        out, _ = await _verify([_h(min_pct=0.0)], runner)
        assert out[0].status == "supported"
        assert out[0].observed["pct_change"] is None

    async def test_unknown_metric_is_one_unverifiable_row_zero_query(self):
        runner = _Runner({})
        out, evidence = await _verify([_h(metric="nope")], runner)
        assert [r.status for r in out] == ["unverifiable"]
        assert out[0].reason.startswith("unknown_metric")
        assert runner.sqls == [] and evidence == []
        assert out[0].queries == 0

    async def test_no_baseline_window(self):
        runner = _Runner({})
        out, _ = await _verify([_h()], runner, base_window=None)
        assert out[0].reason == "no_baseline_window"
        assert runner.sqls == []

    async def test_budget_yields_instead_of_overspending(self):
        runner = _Runner({"2026-09-01": 200.0, "2026-08-01": 100.0})
        ledger = QueryLedger(total=1)          # 一条假设要 2 次,不够
        out, _ = await _verify([_h()], runner, ledger=ledger)
        assert out[0].status == "unverifiable"
        assert out[0].reason == "query_budget_exceeded"
        assert ledger.used == 0
        assert ledger.yielded[0]["stage"] == "hypothesis"

    async def test_query_failure_is_a_verdict_not_a_crash(self):
        runner = _Runner({}, error=RuntimeError("boom"))
        out, _ = await _verify([_h()], runner)
        assert out[0].status == "unverifiable"
        assert out[0].reason.startswith("query_failed: boom")
        assert out[0].queries == 1

    async def test_every_hypothesis_gets_exactly_one_row_in_order(self):
        runner = _Runner({"2026-09-01": 200.0, "2026-08-01": 100.0})
        hs = [_h(claim="a"), _h(claim="b", metric="nope"),
              _h(claim="c", direction="down")]
        out, _ = await _verify(hs, runner)
        assert [r.hypothesis.claim for r in out] == ["a", "b", "c"]
        assert [r.status for r in out] == ["supported", "unverifiable", "refuted"]

    async def test_dimension_value_lands_in_the_sql(self):
        runner = _Runner({"2026-09-01": 200.0, "2026-08-01": 100.0})
        await _verify([_h(dimension="region", value="华东")], runner)
        assert "华东" in runner.sqls[0]


class TestValue:
    def test_missing_rows_are_none_not_zero(self):
        assert hyp._value([]) is None
        assert hyp._value(None) is None
        assert hyp._value([[]]) is None
        assert hyp._value([[None]]) is None

    def test_numeric_rows(self):
        assert hyp._value([[42]]) == 42.0
        assert hyp._value([[1], [2.5]]) == 2.5      # 取最后一行(单值聚合)

    def test_string_numbers_tolerate_decoration(self):
        assert hyp._value([["1,234.5"]]) == 1234.5
        assert hyp._value([["¥100"]]) == 100.0
        assert hyp._value([["12%"]]) == 12.0

    def test_bool_is_not_a_number(self):
        assert hyp._value([[True]]) is None

    def test_unparseable_is_none(self):
        assert hyp._value([["很多"]]) is None


class TestFindingsBlock:
    def test_renders_one_line_per_finding(self):
        from trove.services.scan.models import Finding

        f = Finding(
            metric="loan_balance", dimension="region", value="华东",
            window="本月", current=500.0, z=26.98, n=4,
            band={"lo": 48.0, "hi": 152.0})
        text = hyp.findings_block([f])
        assert "loan_balance [region=华东]" in text
        assert "500" in text and "26.98" in text and "n=4" in text

    def test_missing_values_render_as_dash(self):
        from trove.services.scan.models import Finding

        text = hyp.findings_block([Finding(metric="m", kind="anomaly")])
        assert "—" in text
