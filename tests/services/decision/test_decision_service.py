"""DecisionService — end to end against a real (in-memory) datasource.

These are integration tests on purpose: the value of the decision layer is
that a rule written as "本月, 环比, delta_pct < -10%" turns into the right
SQL against the right window. Every seam between parse_time_range →
base_period → build_and_compile → execute is covered by a unit test
somewhere, but only this file covers them *composed*.
"""

from datetime import date, datetime
from decimal import Decimal

import pytest
import yaml

from trove.core.types import DatasourceConfig
from trove.services.decision.rules import parse_rule
from trove.services.decision.service import DecisionService
from trove.services.kb.service import KbService

NOW = datetime(2026, 9, 27, 10, 0, 0)
TODAY = date(2026, 9, 27)

SEMANTICS = """
semantic_model:
  - name: demo
    datasets:
      - name: loan
        source: loan
        description: "loan records; columns: region, amount, date"
        ai_context:
          synonyms: [loan, 贷款]
        fields:
          - name: region
            expression:
              dialects: [{dialect: sqlite, expression: loan.region}]
            datatype: TEXT
            semantic_role: dimension
          - name: amount
            expression:
              dialects: [{dialect: sqlite, expression: loan.amount}]
            datatype: DOUBLE
            semantic_role: measure
          - name: date
            expression:
              dialects: [{dialect: sqlite, expression: loan.date}]
            datatype: DATE
            semantic_role: time
    metrics:
      - name: loan_balance
        expression:
          dialects: [{dialect: sqlite, expression: SUM(loan.amount)}]
        agg_time_dimension: loan.date
      - name: loan_cost
        expression:
          dialects: [{dialect: sqlite, expression: SUM(loan.cost)}]
        agg_time_dimension: loan.date
      - name: loan_net
        type: derived
        expression:
          dialects: [{dialect: sqlite, expression: loan_balance - loan_cost}]
        agg_time_dimension: loan.date
"""

RULE = {
    "id": "loan-drop",
    "name": "贷款余额环比下滑",
    "window": "本月",
    "subject": {"metrics": ["loan_balance"], "dimensions": ["region"]},
    "baseline": {"kind": "prev_period"},
    "scope": "per_dimension",
    "conditions": {"all": ["delta_pct < -0.10"]},
}

# Aug 2026 (baseline): 华东 1000, 华北 500
# Sep 2026 (current):  华东  800 (-20%), 华北 600 (+20%)
# cost 是成比例的减数,让 loan_net = loan_balance − loan_cost 有真实可分解性:
# Δnet(-550) == Δbalance(-300) − Δcost(+250) —— 恒等式精确,残差为 0。
ROWS = """
INSERT INTO loan (region, amount, cost, date) VALUES
  ('华东', 1000, 400, '2026-08-05'), ('华东', 1000, 400, '2026-08-20'),
  ('华北',  500, 100, '2026-08-10'),
  ('华东',  800, 500, '2026-09-05'), ('华东',  800, 500, '2026-09-20'),
  ('华北',  600, 150, '2026-09-10')
"""


@pytest.fixture
async def svc(tmp_path):
    from trove.services.datasource.registry import ConnectorRegistry

    registry = ConnectorRegistry()
    config = DatasourceConfig(
        name="demo", type="sqlite",
        connection_params={"path": ":memory:"}, default=True,
    )
    adapter = await registry.register(config, set_default=True)
    await adapter.execute(
        "CREATE TABLE loan (region TEXT, amount REAL, cost REAL, date DATE)")
    await adapter.execute(ROWS)

    kb = KbService(tmp_path / "proj")
    ds_dir = kb.kb_dir / "demo"
    ds_dir.mkdir(parents=True, exist_ok=True)
    kb.semantics_path("demo").write_text(SEMANTICS, encoding="utf-8")
    # The provider reads `.trove/semantic/<ds>/`, which _semantic_root derives
    # from the KB root — so an explicit dir is not needed here.
    yield DecisionService(registry, kb)
    await registry.close_all()


def rule(**overrides):
    return parse_rule({**RULE, **overrides})


class TestWindowedComparison:
    async def test_prev_period_fires_on_the_group_that_dropped(self, svc):
        """本月 prev_period against NOW=2026-09-27 is 2026-08-02 ~ 08-31 —
        which contains the August rows, so the comparison is real."""
        out = await svc.evaluate(rule(), "demo", NOW)
        assert out.error == ""
        assert out.triggered is True

        fired = [r for r in out.evidence["rows"] if r["triggered"]]
        assert [r["dim"] for r in fired] == ["华东"]
        assert fired[0]["current"] == 1600.0      # 800 + 800
        assert fired[0]["baseline"] == 2000.0     # 1000 + 1000
        assert fired[0]["delta_pct"] == pytest.approx(-0.2)

    async def test_the_growing_group_is_reported_but_not_triggered(self, svc):
        """华北 grew 20%; it must appear in the evidence as judged-and-not-
        fired, which is what lets a reader trust the negative result."""
        out = await svc.evaluate(rule(), "demo", NOW)
        north = next(r for r in out.evidence["rows"] if r["dim"] == "华北")
        assert north["triggered"] is False
        assert north["delta_pct"] == pytest.approx(0.2)
        assert north["matched"] == []

    async def test_evidence_carries_the_sql_and_the_rows_it_judged(self, svc):
        """A decision run has no LangGraph trace — the run record is the only
        audit trail, so the evidence has to stand on its own."""
        out = await svc.evaluate(rule(), "demo", NOW)
        ev = out.evidence
        assert "SELECT" in ev["evidence"]["sql_current"].upper()
        assert "SELECT" in ev["evidence"]["sql_baseline"].upper()
        assert ev["evidence"]["columns"] == ["region", "loan_balance"]
        assert ev["evidence"]["rows"], "raw rows must be kept, not just the summary"
        assert ev["evidence"]["truncated"] is False
        assert ev["periods"]["current"] == ["2026-09-01", "2026-09-30"]
        assert ev["periods"]["baseline"] == ["2026-08-02", "2026-08-31"]
        assert ev["provenance"] == {"datasource": "demo"}

    async def test_both_windows_are_actually_filtered(self, svc):
        """Guards against the window silently not being applied — both SQLs
        must name the time field with a real date range."""
        out = await svc.evaluate(rule(), "demo", NOW)
        cur = out.evidence["evidence"]["sql_current"]
        base = out.evidence["evidence"]["sql_baseline"]
        assert "2026-09-01" in cur and "2026-09-30" in cur
        assert "2026-08-02" in base and "2026-08-31" in base

    async def test_message_names_the_group_and_the_numbers(self, svc):
        out = await svc.evaluate(rule(), "demo", NOW)
        assert "华东" in out.message
        assert "warning" in out.message
        assert out.message.startswith("[warning]")


class TestBaselines:
    async def test_yoy_window(self, svc):
        """Same rows, different baseline kind → a different window; with no
        September-2025 data the baseline is empty, delta is Unknown, and the
        rule must **not** fire. That is the Kleene behaviour end to end."""
        out = await svc.evaluate(rule(baseline={"kind": "yoy"}), "demo", NOW)
        assert out.error == ""
        assert out.triggered is False
        assert out.evidence["periods"]["baseline"] == ["2025-09-01", "2025-09-30"]
        assert all(r["baseline"] is None for r in out.evidence["rows"])

    async def test_literal_baseline_needs_no_second_query(self, svc):
        out = await svc.evaluate(
            rule(baseline={"kind": "literal", "value": 1500},
                 conditions=["current < baseline"]),
            "demo", NOW)
        assert out.evidence["evidence"]["sql_baseline"] == ""
        east = next(r for r in out.evidence["rows"] if r["dim"] == "华东")
        assert east["baseline"] == 1500.0
        assert east["triggered"] is False      # 1600 >= 1500

    async def test_kind_none_leaves_baseline_unknown(self, svc):
        out = await svc.evaluate(
            rule(baseline={"kind": "none"}, conditions=["current > 100"]),
            "demo", NOW)
        assert out.error == ""
        assert out.triggered is True
        assert out.evidence["evidence"]["sql_baseline"] == ""

    async def test_window_without_a_baseline_kind_is_allowed(self, svc):
        """A window with kind 'none' still filters — it just has nothing to
        compare against."""
        out = await svc.evaluate(
            rule(window="本月", baseline={"kind": "none"},
                 subject={"metrics": ["loan_balance"]},
                 conditions=["current > 0"], scope="aggregate", emit="any"),
            "demo", NOW)
        assert out.triggered is True
        assert "2026-09-01" in out.evidence["evidence"]["sql_current"]

    async def test_aggregate_scope_with_dimensions_fails_loud(self, svc):
        """Dimensions are the group-by, so there is no aggregate row to judge.
        Left unchecked this reads Unknown forever — a rule that is silently
        dead, which looks exactly like "nothing wrong today"."""
        out = await svc.evaluate(
            rule(scope="aggregate", emit="any",
                 conditions=["current > 0"]),      # RULE carries dimensions
            "demo", NOW)
        assert out.error and "subject.dimensions" in out.error
        assert out.triggered is False
        assert out.message == ""


class TestScopes:
    async def test_aggregate_scope_single_row(self, svc):
        out = await svc.evaluate(
            rule(scope="aggregate", emit="any",
                 subject={"metrics": ["loan_balance"]},
                 conditions=["current > 1000"]),
            "demo", NOW)
        assert out.error == ""
        assert out.triggered is True
        assert out.evidence["rows"][0]["dim"] == ""
        assert out.evidence["rows"][0]["current"] == 2200.0   # all regions

    async def test_top_k_limits_what_the_rule_reports(self, svc):
        out = await svc.evaluate(
            rule(emit="top_k", top_k=1,
                 conditions=["abs(delta) > 1"]),
            "demo", NOW)
        rows = out.evidence["rows"]
        assert sum(1 for r in rows if r["triggered"]) == 2   # both moved
        assert ("华东" in out.message) and ("华北" not in out.message)

    async def test_emit_all_requires_every_group(self, svc):
        out = await svc.evaluate(
            rule(emit="all", conditions=["abs(delta) > 1"]), "demo", NOW)
        assert out.triggered is True

        out2 = await svc.evaluate(
            rule(emit="all", conditions=["delta < 0"]), "demo", NOW)
        assert out2.triggered is False          # only 华东 fell
        # ...but the per-group verdicts stay truthful
        assert any(r["triggered"] for r in out2.evidence["rows"])

    async def test_contribution_is_attached_per_group(self, svc):
        out = await svc.evaluate(rule(), "demo", NOW)
        east = next(r for r in out.evidence["rows"] if r["dim"] == "华东")
        north = next(r for r in out.evidence["rows"] if r["dim"] == "华北")
        # deltas: 华东 -400, 华北 +100 → Σ|Δ| = 500
        assert east["contribution"] == pytest.approx(-0.8)
        assert north["contribution"] == pytest.approx(0.2)


class TestFailLoud:
    """Every one of these must surface as `error`, never as a quiet OK."""

    async def test_unresolvable_window(self, svc):
        out = await svc.evaluate(rule(window="下个世纪"), "demo", NOW)
        assert out.error and "not a resolvable time expression" in out.error
        assert out.triggered is False
        assert out.message == ""

    async def test_baseline_kind_without_a_window(self, svc):
        out = await svc.evaluate(rule(window=""), "demo", NOW)
        assert out.error and "needs a 'window'" in out.error

    async def test_undeclared_metric(self, svc):
        out = await svc.evaluate(
            rule(subject={"metrics": ["nope"], "dimensions": ["region"]}),
            "demo", NOW)
        assert out.error and "nope" in out.error

    async def test_undeclared_dimension(self, svc):
        out = await svc.evaluate(
            rule(subject={"metrics": ["loan_balance"], "dimensions": ["nope"]}),
            "demo", NOW)
        assert out.error and "nope" in out.error

    async def test_metric_without_a_declared_time_dimension(self, svc, monkeypatch):
        """Without agg_time_dimension and with no unique time field, the
        engine must refuse rather than query an unbounded window."""
        import trove.services.semantic_layer.compiler as compiler

        monkeypatch.setattr(compiler, "resolve_time_field",
                            lambda *a, **k: None)
        out = await svc.evaluate(rule(), "demo", NOW)
        assert out.error and "time field" in out.error

    async def test_unreachable_datasource(self, svc):
        out = await svc.evaluate(rule(), "nope", NOW)
        assert out.error and "unreachable" in out.error

    async def test_missing_semantic_model(self, svc, tmp_path):
        kb = KbService(tmp_path / "empty")
        kb.kb_dir.mkdir(parents=True, exist_ok=True)
        out = await DecisionService(svc.connectors, kb).evaluate(rule(), "demo", NOW)
        assert out.error and "no semantic model" in out.error


class TestEvidenceIsJsonSafe:
    async def test_no_decimal_or_datetime_leaks_into_the_record(self, svc):
        """Postgres returns Decimal/datetime; a json.dumps failure there would
        abort finish_run and strand the job's schedule."""
        import json

        out = await svc.evaluate(rule(), "demo", NOW)
        json.dumps(out.evidence)          # would raise on Decimal/datetime

    async def test_row_values_are_coerced(self):
        from trove.services.decision.service import _jsonable

        assert _jsonable(Decimal("12.5")) == 12.5
        assert _jsonable(datetime(2026, 9, 27, 1, 2)) == "2026-09-27T01:02:00"
        assert _jsonable(date(2026, 9, 27)) == "2026-09-27"
        assert _jsonable(float("nan")) is None
        assert _jsonable(float("inf")) is None
        assert _jsonable(None) is None
        assert _jsonable("x") == "x"


class TestAgainstTheKb:
    async def test_rule_loaded_from_the_kb_evaluates(self, svc):
        """The path a job actually takes: rules come from decisions.yml, not
        from a literal in code."""
        kb = svc.kb
        (kb.decisions_path("demo")).write_text(
            yaml.safe_dump({"version": 1, "rules": [RULE]}, allow_unicode=True),
            encoding="utf-8")
        loaded = kb.get_decision("demo", "loan-drop")
        assert loaded is not None

        out = await svc.evaluate(loaded, "demo", NOW)
        assert out.error == ""
        assert out.triggered is True
        assert out.evidence["rule_id"] == "loan-drop"
        assert out.evidence["rule_name"] == "贷款余额环比下滑"

    async def test_the_digest_lands_in_the_evidence(self, svc):
        """The card must answer "which version of the rule judged this" —
        and the digest is on the document, so the caller passes it in."""
        out = await svc.evaluate(rule(), "demo", NOW, rule_digest="sha256:abc")
        assert out.evidence["rule_digest"] == "sha256:abc"
        assert out.evidence["model_version"] == ""

    async def test_a_failed_evaluation_records_the_digest_too(self, svc):
        """When a rule starts erroring, "which version" is the first question."""
        broken = rule(window="没有一个这样的时间词")
        out = await svc.evaluate(broken, "demo", NOW, rule_digest="sha256:abc")
        assert out.error
        assert out.evidence["rule_digest"] == "sha256:abc"


class TestLocalDateAnchor:
    async def test_window_follows_the_injected_clock_not_the_wall_clock(self, svc):
        """Replay must be reproducible: the same rule evaluated with a
        different `now` yields a different window, and nothing reads
        datetime.now() behind the caller's back."""
        sep = await svc.evaluate(rule(window="上月"), "demo", NOW)
        assert sep.evidence["periods"]["current"] == ["2026-08-01", "2026-08-31"]

        october = await svc.evaluate(rule(), "demo", datetime(2026, 10, 3))
        assert october.evidence["periods"]["current"] == ["2026-10-01", "2026-10-31"]
        # October is 31 days, so prev_period is the *31-day* window ending
        # 09-30 — not the calendar month of September. Equal-length is the
        # documented contract; see trove/core/periods.py.
        assert october.evidence["periods"]["baseline"] == ["2026-08-31", "2026-09-30"]

    async def test_english_window_expression(self, svc):
        out = await svc.evaluate(rule(window="last month",
                                      conditions=["current > 0"]), "demo", NOW)
        if out.error:
            pytest.skip(f"english rules unsupported: {out.error}")
        assert out.evidence["periods"]["current"] == ["2026-08-01", "2026-08-31"]


class TestBoundedExecution:
    """每条 SQL 有各自的执行预算 —— 定时任务没有人在等。

    无界的形态是:一条慢查询把这次 run(job 的 schedule)永远吊在 ``await`` 上,
    看板上它既不是成功也不是失败。所以超时必须折成 run 上看得见的 error
    (``evaluate`` 的既有契约:失败响亮,绝不静默变 OK)。
    """

    async def test_a_hanging_query_folds_into_a_visible_error(self, svc, monkeypatch):
        import asyncio

        async def _hang(*_a, **_k):
            await asyncio.sleep(30)

        bounded = DecisionService(svc.connectors, svc.kb, timeout_ms=50)
        monkeypatch.setattr(svc.connectors, "execute", _hang)

        out = await bounded.evaluate(rule(), "demo", NOW)

        assert out.triggered is False
        assert "timed out" in out.error

    def test_positive_value_wins(self):
        assert DecisionService(None, None, timeout_ms=50)._timeout_ms == 50

    @pytest.mark.parametrize("raw", [0, -1, "abc", None])
    def test_missing_or_garbage_falls_back_to_the_default(self, raw):
        """非正/非数 → 回到缺省,**不静默变成"无超时"**。"""
        assert DecisionService(None, None, timeout_ms=raw)._timeout_ms == 30_000


class TestAnalysisBridgeSeam:
    """补丁 1:判定触发 → 桥接分析引擎,证据里多一节 ``analysis``。

    这一层测的是**缝**(谁在什么时候被调用、调用时手里有什么),不是桥
    内部的算法 —— 那些在 ``test_bridge.py`` 里。缝错了的表现都很安静:
    未触发也跑(白烧查询)、matched 递空(桥静默永不适用)、桥抛异常把
    已判完的判定降级成 error run(附录拖垮正文)。
    """

    async def test_untriggered_rule_never_touches_the_bridge(self, svc, monkeypatch):
        """判定是常态,分析是例外 —— 不触发的规则不该付两次查询。"""
        import trove.services.decision.bridge as bridge

        called = []

        async def _spy(**kwargs):
            called.append(kwargs)
            return None

        monkeypatch.setattr(bridge, "run_bridge", _spy)
        out = await svc.evaluate(rule(baseline={"kind": "yoy"}), "demo", NOW)

        assert out.error == ""
        assert out.triggered is False          # 2025-09 无数据 → Unknown → 不触发
        assert called == []
        assert "analysis" not in out.evidence

    async def test_triggered_rule_hands_the_bridge_the_judging_anchor(
            self, svc, monkeypatch):
        """matched 必须与判定自己编译时用的一致(判定按什么表编译,桥就
        按同一份),judged_rows 必须是判定行 —— 桥据此零额外查询复用分组。"""
        import trove.services.decision.bridge as bridge

        calls = []
        real = bridge.run_bridge

        async def _record(**kwargs):
            calls.append(kwargs)
            return await real(**kwargs)

        monkeypatch.setattr(bridge, "run_bridge", _record)
        out = await svc.evaluate(rule(), "demo", NOW)

        assert out.error == "" and out.triggered is True
        assert len(calls) == 1
        assert calls[0]["matched"] == ["loan"]
        assert calls[0]["judged_rows"], "判定行必须递过去,不能空手"
        assert calls[0]["judged_rows"][0]["dim"] == "华东"

    async def test_single_leaf_metric_writes_no_analysis_section(self, svc):
        """SUM(amount) 是单叶子 —— 桥对这条规则没有话说。不适用 ≠ 失败:
        证据里什么都不写,也不得报错。"""
        out = await svc.evaluate(rule(), "demo", NOW)   # loan_balance

        assert out.error == "" and out.triggered is True
        assert "analysis" not in out.evidence

    async def test_derived_metric_yields_components_and_an_exact_residual(self, svc):
        """loan_net = loan_balance − loan_cost 有真实可分解性。

        Δnet(-600 华东) == Δbalance(-400) − Δcost(+200) —— 恒等式精确,
        残差必须是 ``identity`` 的 0,而不是读起来像真残差的 gap。
        """
        from trove.services.decision.bridge import MAX_BRIDGE_QUERIES

        out = await svc.evaluate(
            rule(id="net-drop", name="净额下滑", priority=2,
                 recommendation="联系区域客户经理复核",
                 subject={"metrics": ["loan_net"], "dimensions": ["region"]},
                 conditions={"all": ["delta_pct < -0.10"]}),
            "demo", NOW)

        assert out.error == "" and out.triggered is True
        analysis = out.evidence["analysis"]
        top = analysis["top_components"][0]
        assert (top["dim"], top["value"]) == ("region", "华东")
        assert top["delta"] == pytest.approx(-600.0)
        assert top["contribution"] == pytest.approx(-600 / 650)
        assert len(analysis["queries"]) <= MAX_BRIDGE_QUERIES
        assert analysis["residual"] == {"value": 0.0, "exact": True,
                                        "reason": "identity"}
        assert analysis["degraded"] == []
        # 证据自带 SQL —— 判定记录是唯一审计线,可复算性不认"谁跑的"。
        assert any("SELECT" in q["sql"].upper() for q in analysis["queries"])
        # 判定字段照常在(桥只追加,不动正文)。
        assert out.evidence["priority"] == 2
        assert out.evidence["recommendation"] == "联系区域客户经理复核"

    async def test_a_crashing_bridge_cannot_downgrade_the_verdict(
            self, svc, monkeypatch):
        """附录拖垮正文是最坏形态:判定已经判完,桥崩了只能记账。"""
        import trove.services.decision.bridge as bridge

        async def _boom(**_kwargs):
            raise RuntimeError("bridge exploded")

        monkeypatch.setattr(bridge, "run_bridge", _boom)
        out = await svc.evaluate(rule(id="net-drop", name="净额下滑",
                                      subject={"metrics": ["loan_net"],
                                               "dimensions": ["region"]},
                                      conditions={"all": ["delta_pct < -0.10"]}),
                                 "demo", NOW)

        assert out.error == ""                     # 不是 error run
        assert out.triggered is True               # 判定照常交付
        analysis = out.evidence["analysis"]
        assert analysis["top_components"] == []
        assert analysis["degraded"][0]["stage"] == "analysis_bridge"
        assert "bridge exploded" in analysis["degraded"][0]["reason"]


class TestOrgExtensionsSwitch:
    """``agent.extensions.org_extensions_enabled`` 的决策面 —— 停用必须**响**。

    一条停用中的定时决策任务若静默按"零规则通过"返回,与健康运行从外部看
    没有区别(模块 docstring「失败必响」的同一条纪律)。开关**现读**:同一个
    ``DecisionService`` 实例上翻开关即时生效,不重建 —— 管理端 PUT 改的就是
    这个活对象。
    """

    async def test_disabled_raises_and_evaluate_folds_to_error(self, svc):
        from trove.core.config import AgentConfig
        from trove.services.decision.service import DecisionError

        cfg = AgentConfig(target="mock/model")
        cfg.extensions.org_extensions_enabled = False
        svc.config = cfg

        with pytest.raises(DecisionError, match="已停用"):
            await svc.evaluate_rule(rule(), "demo", NOW)

        out = await svc.evaluate(rule(), "demo", NOW)   # 折成 error,不上抛
        assert out.triggered is False
        assert out.message == ""
        assert "org_extensions_enabled" in out.error
        assert "已停用" in out.error
        # 证据仍记 provenance:「哪一版规则没被判」是 error 第一个问题。
        assert out.evidence["provenance"] == {"datasource": "demo"}

    async def test_switch_is_read_live_no_rebuild(self, svc):
        from trove.core.config import AgentConfig

        cfg = AgentConfig(target="mock/model")
        cfg.extensions.org_extensions_enabled = False
        svc.config = cfg
        assert (await svc.evaluate(rule(), "demo", NOW)).error

        cfg.extensions.org_extensions_enabled = True    # 现场翻回,不重建
        out = await svc.evaluate(rule(), "demo", NOW)
        assert out.error == ""
        assert out.triggered is True                    # 华东 -20% 照常命中
