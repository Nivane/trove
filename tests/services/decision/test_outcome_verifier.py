"""``make_verifier`` 契约单测:None=还没到期 / dict=测量 / error=响亮失败。

执行面在 action 包外(包只拿到一个 callable),这里用**假 DecisionService
+ 假 kb + monkeypatch 掉的取数函数**把每条分支都走一遍 —— 真取数在
``test_series_source.py`` / ``test_causal_source.py``,真判定在
``test_decision_service.py``;本文件钉的是 verifier 自己的承诺:

  - 期没滚过行动日 → ``None``(不写行、不算失败,下个 sweep 再试);
  - per_dimension 规则取不到「当时点的那组」→ ``group_unresolved``
    响亮失败,绝不拿别的总体冒充;
  - 对照不可用只降级,ITS 结论原样保留(``causal_reason`` 区分
    「帧没取到」与「帧在、值不全」);
  - 观测量里 ``rule_rev``(测量依据的版本)与 ``rule_rev_judged``
    (判定当时的版本,propose 时钉下)分列。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from trove.services.decision import outcome as oc
from trove.services.decision.rules import (
    Causal,
    CausalControl,
    DecisionRule,
    RuleError,
    Seasonal,
    Subject,
)
from trove.services.decision.series_source import BlockSeries
from trove.services.decision.service import DecisionError

WINDOW = ("2026-09-01", "2026-09-30")
#: 8 个行动前块(≥ MIN_BLOCKS)+ 当期 —— 与 series_source 的约定一致(末位=当期)
BLOCKS = [100.0, 102.0, 98.0, 101.0, 99.0, 103.0, 97.0, 101.0]


class FakeDecision:
    """仅实现 verifier 触达的那几个口(结构复用的最小面)。"""

    def __init__(self, *, window=WINDOW, window_error=None, compile_error=None,
                 compile_info=None):
        self._window = window
        self._window_error = window_error
        self._compile_error = compile_error
        self._compile_info = compile_info if compile_info is not None else {
            "time_field": "issue_date", "datasets": ["loan"]}
        self.calls: list[tuple] = []

    async def _dialect(self, datasource):
        return "mysql"

    def _model_for(self, datasource, dialect):
        return "model"

    def _provider_for(self, datasource, dialect):
        return "provider"

    def _query_runner(self, datasource):
        return "runner"

    def _resolve_window(self, rule, anchor):
        if self._window_error is not None:
            raise self._window_error
        return self._window, None

    async def _compile(self, rule, model, dialect, datasource, window):
        self.calls.append(("compile", rule.id, window))
        if self._compile_error is not None:
            raise self._compile_error
        return dict(self._compile_info)


class FakeKb:
    def __init__(self, rules=None, error=None):
        self._rules = list(rules or [])
        self._error = error

    def load_decisions(self, datasource):
        if self._error is not None:
            raise self._error
        return SimpleNamespace(rules=self._rules, digest="d")


def _rule(rule_id="loan-drop", **kw):
    base = dict(id=rule_id, name="", window="上月",
                subject=Subject(metrics=["loan_balance"]))
    base.update(kw)
    return DecisionRule(**base)


def _proposal(**kw):
    fields = dict(
        id="p-1", rule_id="loan-drop", datasource="demo",
        dispatched_at="2026-09-20T09:00:00", decided_at="2026-09-19T09:00:00",
        evidence_refs={"rule_rev": "rev-judged", "group": "华东"},
    )
    fields.update(kw)
    return SimpleNamespace(**fields)


def _series(labels=("华东",), values=None, **kw):
    vals = values if values is not None else BLOCKS + [150.0]
    return BlockSeries(sql="SELECT ...", grain="month", mode="trailing",
                       blocks=[("2026-%02d-01" % (i + 1), "2026-%02d-28" % (i + 1))
                               for i in range(len(vals))],
                       by_dim={lab: list(vals) for lab in labels}, **kw)


@pytest.fixture()
def wiring(monkeypatch):
    """默认:取数成功、无对照 —— 单个测试按需覆盖。

    默认序列的取值键是 ``""``(聚合规则测的就是它);维度规则的测试
    自己换 ``labels=("华东",)`` 的序列。
    """
    box = {"series": _series(labels=("",)), "series_calls": [], "control": None,
           "control_raises": None}

    async def fake_series(**kw):
        box["series_calls"].append(kw)
        return box["series"]

    async def fake_control(**kw):
        if box["control_raises"] is not None:
            raise box["control_raises"]
        return box["control"]

    monkeypatch.setattr(oc, "fetch_block_series", fake_series)
    monkeypatch.setattr(oc, "fetch_control_series", fake_control)
    return box


class TestContract:
    """每条失败都是 error dict(measure_due 落成一行 error outcome)。"""

    async def test_manual_proposal_has_no_rule_to_measure(self, wiring):
        verify = oc.make_verifier(FakeDecision(), kb=FakeKb())
        out = await verify(_proposal(rule_id=""))
        assert out == {"error": "no_rule"}

    async def test_no_kb(self, wiring):
        verify = oc.make_verifier(FakeDecision(), kb=None)
        assert (await verify(_proposal()))["error"] == "no_kb"

    async def test_unreadable_decisions(self, wiring):
        kb = FakeKb(error=RuleError("bad yaml"))
        verify = oc.make_verifier(FakeDecision(), kb=kb)
        assert (await verify(_proposal()))["error"].startswith(
            "decisions_unreadable:")

    async def test_rule_missing(self, wiring):
        verify = oc.make_verifier(FakeDecision(), kb=FakeKb([]))
        assert (await verify(_proposal()))["error"] == "rule_missing: loan-drop"

    async def test_rule_disabled(self, wiring):
        kb = FakeKb([_rule(enabled=False)])
        verify = oc.make_verifier(FakeDecision(), kb=kb)
        assert (await verify(_proposal()))["error"] == "rule_disabled: loan-drop"

    async def test_no_dispatch_time(self, wiring):
        verify = oc.make_verifier(FakeDecision(), kb=FakeKb([_rule()]))
        out = await verify(_proposal(dispatched_at="", decided_at=""))
        assert out == {"error": "no_dispatch_time"}

    async def test_decided_at_is_the_fallback_clock(self, wiring):
        verify = oc.make_verifier(FakeDecision(), kb=FakeKb([_rule()]))
        out = await verify(_proposal(dispatched_at=""))
        assert "error" not in out
        assert out["left_at"] == "2026-09-19T09:00:00"

    async def test_window_unresolved(self, wiring):
        dec = FakeDecision(window_error=DecisionError("没时间字段"))
        verify = oc.make_verifier(dec, kb=FakeKb([_rule()]))
        assert (await verify(_proposal()))["error"].startswith("window_unresolved:")

    async def test_no_window(self, wiring):
        dec = FakeDecision(window=None)
        verify = oc.make_verifier(dec, kb=FakeKb([_rule()]))
        assert (await verify(_proposal()))["error"] == "no_window"

    async def test_no_metric(self, wiring):
        kb = FakeKb([_rule(subject=Subject())])
        verify = oc.make_verifier(FakeDecision(), kb=kb)
        assert (await verify(_proposal()))["error"] == "no_metric"

    async def test_compile_failed(self, wiring):
        dec = FakeDecision(compile_error=DecisionError("MISS"))
        verify = oc.make_verifier(dec, kb=FakeKb([_rule()]))
        assert (await verify(_proposal()))["error"].startswith("compile_failed:")

    async def test_no_time_field(self, wiring):
        dec = FakeDecision(compile_info={"time_field": "", "datasets": []})
        verify = oc.make_verifier(dec, kb=FakeKb([_rule()]))
        assert (await verify(_proposal()))["error"] == "no_time_field"

    async def test_query_failed(self, wiring, monkeypatch):
        async def boom(**kw):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(oc, "fetch_block_series", boom)
        verify = oc.make_verifier(FakeDecision(), kb=FakeKb([_rule()]))
        out = await verify(_proposal())
        assert out["error"].startswith("query_failed:")
        assert "connection refused" in out["error"]

    async def test_series_unavailable(self, wiring):
        wiring["series"] = None
        verify = oc.make_verifier(FakeDecision(), kb=FakeKb([_rule()]))
        assert (await verify(_proposal()))["error"] == "series_unavailable"


class TestWindowGate:
    """期没滚过行动日 → None:不是失败,不写行,下个 sweep 再看。"""

    async def test_not_yet_due_returns_none(self, wiring):
        dec = FakeDecision(window=("2026-09-01", "2026-09-15"))
        verify = oc.make_verifier(dec, kb=FakeKb([_rule()]))
        assert await verify(_proposal(dispatched_at="2026-09-20T00:00:00")) is None
        assert wiring["series_calls"] == []          # 一条查询都不发

    async def test_end_equal_to_left_at_is_not_due(self, wiring):
        dec = FakeDecision(window=("2026-09-01", "2026-09-20"))
        verify = oc.make_verifier(dec, kb=FakeKb([_rule()]))
        assert await verify(_proposal(dispatched_at="2026-09-20T09:00:00")) is None

    async def test_window_rolled_past_means_measure(self, wiring):
        verify = oc.make_verifier(FakeDecision(), kb=FakeKb([_rule()]))
        out = await verify(_proposal())
        assert out is not None and "error" not in out


class TestGroupPin:
    """测「当时点的那一组」—— propose 时钉进 evidence_refs,验收读它。"""

    async def test_dimensioned_rule_without_pinned_group_is_loud(self, wiring):
        rule = _rule(subject=Subject(metrics=["m"], dimensions=["region"]))
        verify = oc.make_verifier(FakeDecision(), kb=FakeKb([rule]))
        out = await verify(_proposal(evidence_refs={}))
        assert out == {"error": "group_unresolved"}
        assert wiring["series_calls"] == []          # 花查询之前就响

    async def test_pinned_group_is_measured(self, wiring):
        wiring["series"] = _series(labels=("华东",))
        rule = _rule(subject=Subject(metrics=["m"], dimensions=["region"]))
        verify = oc.make_verifier(FakeDecision(), kb=FakeKb([rule]))
        out = await verify(_proposal(evidence_refs={"group": "华东"}))
        assert out["group"] == "华东"
        assert out["group_missing"] is False
        assert "group_missing" not in out["degraded"]
        # 取数请求确实点名了那一组(维度列表原样传下去)
        assert wiring["series_calls"][0]["dimensions"] == ["region"]

    async def test_group_absent_from_series_is_degraded(self, wiring):
        wiring["series"] = _series(labels=("华南",))
        rule = _rule(subject=Subject(metrics=["m"], dimensions=["region"]))
        verify = oc.make_verifier(FakeDecision(), kb=FakeKb([rule]))
        out = await verify(_proposal(evidence_refs={"group": "华东"}))
        assert out["group_missing"] is True
        assert "group_missing" in out["degraded"]
        assert out["outside_band"] is None           # 没有数据就没有判定

    async def test_aggregate_rule_ignores_a_stray_group(self, wiring):
        """聚合规则只看 "" 这一个键 —— refs 里混进组名也不许它改变测法。"""
        verify = oc.make_verifier(FakeDecision(), kb=FakeKb([_rule()]))
        out = await verify(_proposal(evidence_refs={"group": "华东"}))
        assert out["group"] == ""
        assert out["group_missing"] is False


class TestObserved:
    async def test_happy_path_shape(self, wiring):
        verify = oc.make_verifier(FakeDecision(), kb=FakeKb([_rule()]))
        out = await verify(_proposal())

        # 效果本体
        assert out["method"] == "its"
        assert out["delta"] == pytest.approx(150.0 - 100.5)
        assert out["outside_band"] is True
        # 审计指针
        assert out["proposal_id"] == "p-1"
        assert out["rule_id"] == "loan-drop"
        assert out["metric"] == "loan_balance"
        assert out["window"] == list(WINDOW)
        assert out["left_at"] == "2026-09-20T09:00:00"
        assert out["exposure_days"] == 10            # 09-20 → 09-30
        assert out["blocks"] == len(BLOCKS) + 1
        assert out["sql"] == "SELECT ..."
        assert out["grain"] == "month" and out["mode"] == "trailing"
        # 版本对:测量依据(当前)与判定当时(钉下的)分列
        assert out["rule_rev"] != "" and out["rule_rev_judged"] == "rev-judged"
        # 没声明因果 → 明确不可用,不是错误
        assert out["causal"] == "unavailable"
        assert out["causal_reason"] == "no_control"
        assert out["att"] is None

    async def test_no_control_ever_fetched(self, wiring, monkeypatch):
        calls = []

        async def spy(**kw):
            calls.append(kw)
            return None

        monkeypatch.setattr(oc, "fetch_control_series", spy)
        verify = oc.make_verifier(FakeDecision(), kb=FakeKb([_rule()]))
        await verify(_proposal())
        assert calls == []

    async def test_rule_rev_judged_survives_a_missing_ref(self, wiring):
        verify = oc.make_verifier(FakeDecision(), kb=FakeKb([_rule()]))
        out = await verify(_proposal(evidence_refs={}))
        assert out["rule_rev_judged"] == ""


class TestCausalLeg:
    def _rule_with_control(self):
        return [_rule(causal=Causal(control=CausalControl(dim="region",
                                                          value="华南")))]

    async def test_did_when_control_frame_available(self, wiring, monkeypatch):
        wiring["control"] = SimpleNamespace(
            series=_series(labels=("华南",), values=[200.0] * 8 + [210.0]),
            mode="dim", label="华南", treated_labels=None, donors={},
            donor_reason="")
        verify = oc.make_verifier(FakeDecision(), kb=FakeKb(self._rule_with_control()))
        out = await verify(_proposal())

        assert out["causal"] == "did"
        assert out["method"] == "its+did"
        # T: 100.5 → 150 (+49.5);C: 200 → 210 (+10) → ATT = 39.5
        assert out["att"]["att"] == pytest.approx(39.5)
        assert out["att"]["control_delta"] == pytest.approx(10.0)
        # ITS 的数字不被因果改动
        assert out["delta"] == pytest.approx(49.5)
        assert out["outside_band"] is True

    async def test_frame_missing_says_unavailable(self, wiring):
        wiring["control"] = None
        verify = oc.make_verifier(FakeDecision(), kb=FakeKb(self._rule_with_control()))
        out = await verify(_proposal())
        assert out["causal"] == "unavailable"
        assert out["causal_reason"] == "control_unavailable"
        assert out["delta"] is not None              # ITS 结论原样

    async def test_frame_without_series_says_unavailable(self, wiring):
        wiring["control"] = SimpleNamespace(series=None, mode="dim",
                                            label="华南", donors={},
                                            treated_labels=None,
                                            donor_reason="no_donors")
        verify = oc.make_verifier(FakeDecision(), kb=FakeKb(self._rule_with_control()))
        out = await verify(_proposal())
        assert out["causal_reason"] == "control_unavailable"

    async def test_frame_but_incomplete_values_says_incomplete(self, wiring):
        """帧在、控制组当期缺值 —— 与「帧取不到」是两种修法,原因要分开。"""
        wiring["control"] = SimpleNamespace(
            series=_series(labels=("华南",), values=[200.0] * 8 + [None]),
            mode="dim", label="华南", treated_labels=None, donors={},
            donor_reason="")
        verify = oc.make_verifier(FakeDecision(), kb=FakeKb(self._rule_with_control()))
        out = await verify(_proposal())
        assert out["causal"] == "unavailable"
        assert out["causal_reason"] == "control_incomplete"
        assert "causal:control_incomplete" in out["degraded"]

    async def test_control_fetch_exception_degrades_not_raises(self, wiring):
        wiring["control_raises"] = RuntimeError("对照查询炸了")
        verify = oc.make_verifier(FakeDecision(), kb=FakeKb(self._rule_with_control()))
        out = await verify(_proposal())
        assert out["causal_reason"] == "control_unavailable"
        assert out["outside_band"] is True           # 验收主体不受影响

    async def test_filters_mode_label_is_empty(self, wiring):
        """filters 口径的对照帧没有 dim 标签(供体池结构上不存在)——
        取值键是 "";这条腿不炸、结论保留。"""
        wiring["control"] = SimpleNamespace(
            series=_series(labels=("",), values=[200.0] * 8 + [210.0]),
            mode="filters", label="", treated_labels=None, donors={},
            donor_reason="")
        rule = [_rule(causal=Causal(control=CausalControl(
            filters=[{"field": "region", "op": "eq", "value": "华北"}])))]
        verify = oc.make_verifier(FakeDecision(), kb=FakeKb(rule))
        out = await verify(_proposal())
        assert out["causal"] == "did"
        assert out["att"]["control_delta"] == pytest.approx(10.0)


class TestExposureDays:
    def test_left_inside_window(self):
        assert oc._exposure_days(WINDOW, "2026-09-20T00:00:00") == 10

    def test_left_before_window_counts_whole_window(self):
        assert oc._exposure_days(WINDOW, "2026-08-01T00:00:00") == 29

    def test_garbage_is_none(self):
        assert oc._exposure_days(None, "2026-09-20") is None
        assert oc._exposure_days(WINDOW, "not-a-date") is None


class TestSeasonalPassthrough:
    async def test_seasonal_declaration_reaches_the_fetch(self, wiring):
        rule = _rule(seasonal=Seasonal(grain="month", lookback=6,
                                       mode="same_phase", k=2.0))
        verify = oc.make_verifier(FakeDecision(), kb=FakeKb([rule]))
        await verify(_proposal())
        call = wiring["series_calls"][0]
        assert call["grain"] == "month"
        assert call["lookback"] == 6
        assert call["mode"] == "same_phase"

    async def test_undeclared_seasonal_uses_defaults(self, wiring):
        verify = oc.make_verifier(FakeDecision(), kb=FakeKb([_rule()]))
        await verify(_proposal())
        call = wiring["series_calls"][0]
        assert call["grain"] == "" and call["lookback"] == 12
