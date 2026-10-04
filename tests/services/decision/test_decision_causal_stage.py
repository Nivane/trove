"""因果阶段(B3)端到端 —— 升级梯在真实 datasource 上的判定与降级。

数据构造(12 个历史月 + 当期):
  - 华东(处理组,subject 过滤钉住):[100,110]×6,当期 200;
  - 华北(对照组):[50,55]×6,当期 60 → 手算 ATT = (200−110)−(60−55) = 85;
  - 华南/西南(供体):[25,27.5]×6+50、[40,44]×6+88 —— 与处理组**同比**,
    合成拟合 = 0 → L3。

placebo 前窗:相邻块的 did 恒为 ±5(阈值 0.1×110 = 11)→ C3 过;
散布 se ≈ 5.77 → |85| > 2·se → 区间不跨零。

每条铁律各有专门用例:算不出 ≠ 平行、C3 不过 → L1 + unmet 带实测值、
因果绝不改 triggered、未触发/未声明零成本、预算让路记账、assumptions
永远非空。所有场景的**手算数**在各自 docstring 里给出 —— 改数据必须
同步改断言(它们就是复算凭证)。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from trove.core.types import DatasourceConfig
from trove.services.decision.rules import parse_rule
from trove.services.decision.service import DecisionService
from trove.services.kb.service import KbService

NOW = datetime(2026, 9, 27, 10, 0, 0)
CURRENT = "2026-09-15"

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
"""


def _hist_months() -> list[str]:
    """12 个历史月(2025-09 .. 2026-08),偶数下标低值、奇数下标高值。"""
    return [(f"{2025 + (8 + i) // 12}-{(8 + i) % 12 + 1:02d}-15")
            for i in range(12)]


def _series(region: str, even: float, odd: float, post: float,
            months: list[str] | None = None) -> list[tuple[str, float, str]]:
    rows = [(region, even if i % 2 == 0 else odd, m)
            for i, m in enumerate(months if months is not None else _hist_months())]
    rows.append((region, post, CURRENT))
    return rows


#: 干净场景:四个维值齐备,处理组/对照/供体如模块 docstring 所述。
CLEAN_ROWS = (_series("华东", 100, 110, 200) + _series("华北", 50, 55, 60)
              + _series("华南", 25, 27.5, 50) + _series("西南", 40, 44, 88))

RULE = {
    "id": "loan-region-move",
    "name": "华东贷款余额异动",
    "window": "本月",
    "subject": {
        "metrics": ["loan_balance"], "dimensions": [],
        "filters": [{"field": "loan.region", "op": "=", "value": "华东"}],
    },
    "baseline": {"kind": "prev_period"},
    "scope": "aggregate",
    "conditions": {"all": ["delta_pct > 0.05"]},
    "seasonal": {"lookback": 12},
    "causal": {"mode": "auto", "control": {"dim": "region", "value": "华北"}},
}


def _seed(rows: list[tuple[str, float, str]]) -> str:
    values = ", ".join(f"('{r}', {v}, '{d}')" for r, v, d in rows)
    return f"INSERT INTO loan (region, amount, date) VALUES {values}"


@pytest.fixture
async def svc(tmp_path):
    from trove.services.datasource.registry import ConnectorRegistry

    registry = ConnectorRegistry()
    config = DatasourceConfig(
        name="demo", type="sqlite",
        connection_params={"path": ":memory:"}, default=True,
    )
    adapter = await registry.register(config, set_default=True)
    await adapter.execute("CREATE TABLE loan (region TEXT, amount REAL, date DATE)")
    await adapter.execute(_seed(CLEAN_ROWS))
    kb = KbService(tmp_path / "proj")
    (kb.kb_dir / "demo").mkdir(parents=True, exist_ok=True)
    kb.semantics_path("demo").write_text(SEMANTICS, encoding="utf-8")
    yield DecisionService(registry, kb)
    await registry.close_all()


def rule(**overrides):
    return parse_rule({**RULE, **overrides})


def counting(svc, monkeypatch) -> list[str]:
    """记账包装 + 因果取数筛子(strftime = 块序列 SQL 的签名)。"""
    calls: list[str] = []
    orig = svc.connectors.execute

    async def counted(sql, datasource=None):
        calls.append(sql)
        return await orig(sql, datasource)

    monkeypatch.setattr(svc.connectors, "execute", counted)
    return calls


def _series_calls(calls: list[str]) -> list[str]:
    return [s for s in calls if "strftime" in s]


async def _reseed(svc, rows):
    """换一套种子数据(场景之间不共享 fixture 数据库状态)。

    走 adapter 直连而非 ``connectors.execute`` —— 后者带只读守卫
    (业务数据源对 Trove 永远只读),种子是测试装置不是业务写。
    """
    adapter = await svc.connectors.get("demo")
    await adapter.execute("DELETE FROM loan")
    await adapter.execute(_seed(rows))


class TestCleanLadder:
    async def test_full_pass_reaches_l3_with_hand_computed_numbers(self, svc):
        out = await svc.evaluate(rule(), "demo", NOW)
        assert out.error == "", out.error
        assert out.triggered is True

        c = out.evidence["causal"]
        assert c["rung"] == "L3" and c["unmet"] == []
        assert c["degraded"] == []
        assert c["note"]                     # 限定语必须在出口
        assert c["grain"] == "month" and c["block_mode"] == "trailing"
        assert c["lookback"] == 12
        # 块序列 = 12 历史 + 当期(因果帧必须含当期)
        assert len(c["blocks"]) == 13
        assert c["blocks"][-1] == ["2026-09-01", "2026-09-30"]
        assert c["treated"]["pre"] == 110.0 and c["treated"]["post"] == 200.0
        assert "strftime" in c["treated"]["sql"]

        # DiD:ATT = (200−110) − (60−55) = 85;placebo did 恒 ±5 → 过
        assert c["did"]["att"] == pytest.approx(85.0)
        assert c["did"]["control_delta"] == pytest.approx(5.0)
        assert c["placebo"]["passes"] is True
        assert c["placebo"]["max_abs_did"] == pytest.approx(5.0)
        assert c["placebo"]["threshold"] == pytest.approx(11.0)
        assert c["placebo"]["count"] == 4    # 取最近 4 对
        assert c["did"]["se"] == pytest.approx(5.7735, abs=1e-3)
        assert c["did"]["crosses_zero"] is False

        # 对照帧:标签 华北、处理组 华东、供体池 = 其余维值
        assert c["control"]["mode"] == "dim"
        assert c["control"]["label"] == "华北"
        assert c["control"]["treated_labels"] == ["华东"]
        assert c["control"]["donors"] == ["华南", "西南"]

        # 合成对照:同比供体 → 拟合 0;反事实 = 105×69/34.125 ≈ 212.31
        assert c["synthetic"]["fit_relative"] == pytest.approx(0.0, abs=1e-12)
        assert c["synthetic"]["fit_passes"] is True
        assert c["synthetic"]["counterfactual"] == pytest.approx(212.3077, abs=1e-3)
        assert c["synthetic"]["effect"] == pytest.approx(-12.3077, abs=1e-3)

    async def test_message_carries_the_net_effect_only_at_l2_plus(self, svc):
        out = await svc.evaluate(rule(), "demo", NOW)
        assert "净效应" in out.message
        # L3 用合成对照的效应(-12.31),不用 DiD 的 85
        assert "-12.31" in out.message and "合成对照" in out.message

    async def test_assumptions_are_nonempty_and_mark_the_untestable(self, svc):
        out = await svc.evaluate(rule(), "demo", NOW)
        assumptions = out.evidence["causal"]["assumptions"]
        assert len(assumptions) == 4
        assert all(a["text"] and a["detail"] for a in assumptions)
        assert [a["checked"] for a in assumptions].count(None) == 1
        assert any(a["checked"] is True and "max|did|" in a["detail"]
                   for a in assumptions)

    async def test_budget_counts_both_causal_queries(self, svc, monkeypatch):
        calls = counting(svc, monkeypatch)
        out = await svc.evaluate(rule(), "demo", NOW)
        assert len(_series_calls(calls)) == 2
        budget = out.evidence["budget"]
        assert budget["by_stage"]["causal"] == 2
        assert budget["yielded"] == []

    async def test_deterministic_section(self, svc):
        a = await svc.evaluate(rule(), "demo", NOW)
        b = await svc.evaluate(rule(), "demo", NOW)
        assert a.evidence["causal"] == b.evidence["causal"]

    async def test_did_mode_caps_at_l2_without_donor_fit(self, svc):
        out = await svc.evaluate(
            rule(causal={"mode": "did",
                         "control": {"dim": "region", "value": "华北"}}),
            "demo", NOW)
        c = out.evidence["causal"]
        assert c["rung"] == "L2" and c["unmet"] == []
        assert "synthetic" not in c              # did 档不装能算的
        assert "净效应" in out.message and "DiD" in out.message


class TestHonestDegradation:
    async def test_failed_placebo_stops_at_l1_with_measured_and_threshold(
            self, svc):
        """C3 核心用例:对照反向(华北 [55,50]×6)→ did 恒 ±15 > 11。

        promoted claim:降级不是一句「条件不足」—— 实测值(15)与阈值(11)
        都在 unmet 里,而且 triggered 不变(L1 只是附录)。
        """
        await _reseed(svc, (_series("华东", 100, 110, 200)
                            + _series("华北", 55, 50, 60)
                            + _series("华南", 25, 27.5, 50)
                            + _series("西南", 40, 44, 88)))
        out = await svc.evaluate(rule(), "demo", NOW)
        assert out.error == "" and out.triggered is True     # 绝不改 triggered
        c = out.evidence["causal"]
        assert c["rung"] == "L1"
        assert c["unmet"] == [{"condition": "C3",
                               "reason": "placebo_not_parallel",
                               "measured": pytest.approx(15.0),
                               "threshold": pytest.approx(11.0)}]
        # L1 不主张因果:消息不带净效应,假设清单明写「不主张因果」
        assert "净效应" not in out.message
        assert "不主张因果" in c["assumptions"][0]["text"]
        assert c["assumptions"][0]["checked"] is None

    async def test_poor_donor_fit_stops_honestly_at_l2(self, svc):
        """西南 [40,70]×6 → 拟合 ≈ 0.152 > 0.1 → C6 不过,停 L2。

        DiD 照样可用(ATT 85)且消息用它 —— 升级梯只在证据支持时升。
        """
        await _reseed(svc, (_series("华东", 100, 110, 200)
                            + _series("华北", 50, 55, 60)
                            + _series("华南", 25, 27.5, 50)
                            + _series("西南", 40, 70, 88)))
        out = await svc.evaluate(rule(), "demo", NOW)
        c = out.evidence["causal"]
        assert c["rung"] == "L2"
        unmet = c["unmet"][0]
        assert unmet["condition"] == "C6" and unmet["reason"] == "fit_too_poor"
        assert unmet["measured"] == pytest.approx(0.152381, abs=1e-5)
        assert unmet["threshold"] == pytest.approx(0.1)
        assert "净效应：+85.00" in out.message and "DiD" in out.message

    async def test_insufficient_blocks_is_c1_and_saves_the_second_query(
            self, svc, monkeypatch):
        """只种 4 个历史月 → C1 实测 4 < 8,且**不花第二条** SQL。"""
        await _reseed(svc, (_series("华东", 100, 110, 200,
                                    months=_hist_months()[-4:])
                            + _series("华北", 50, 55, 60,
                                      months=_hist_months()[-4:])))
        calls = counting(svc, monkeypatch)
        out = await svc.evaluate(rule(), "demo", NOW)
        c = out.evidence["causal"]
        assert c["rung"] == "L1"
        assert c["unmet"][0] == {"condition": "C1",
                                 "reason": "insufficient_blocks",
                                 "measured": 4, "threshold": 8}
        assert len(_series_calls(calls)) == 1

    async def test_missing_pre_block_is_c4_before_any_control_query(
            self, svc, monkeypatch):
        """yoy 基准:当期与前年同期有数,但前窗全空 → 没得减,不升级。"""
        await _reseed(svc, [("华东", 110.0, "2025-09-15"),
                            ("华东", 200.0, CURRENT)])
        calls = counting(svc, monkeypatch)
        out = await svc.evaluate(
            rule(baseline={"kind": "yoy"}), "demo", NOW)
        assert out.triggered is True
        c = out.evidence["causal"]
        assert c["rung"] == "L1"
        assert c["unmet"][0]["condition"] == "C4"
        assert c["unmet"][0]["reason"] == "no_pre_block"
        assert len(_series_calls(calls)) == 1

    async def test_control_without_history_is_c2_with_precise_reason(self, svc):
        """华北只有当期、没有历史 → 对照数据缺,原因细到 control_data_missing。"""
        await _reseed(svc, (_series("华东", 100, 110, 200)
                            + [("华北", 60.0, CURRENT)]
                            + _series("华南", 25, 27.5, 50)
                            + _series("西南", 40, 44, 88)))
        out = await svc.evaluate(rule(), "demo", NOW)
        c = out.evidence["causal"]
        assert c["rung"] == "L1"
        assert c["unmet"][0] == {"condition": "C2",
                                 "reason": "control_data_missing"}
        assert c["control"]["pre"] is None and c["control"]["post"] == 60.0
        assert "净效应" not in out.message

    async def test_filters_control_can_never_reach_l3(self, svc):
        """filters 形态对照 → 无供体池 → 诚实停 L2,原因指路 dim 形态。"""
        await _reseed(svc, (_series("华东", 100, 110, 200)
                            + _series("华北", 50, 55, 60)))
        out = await svc.evaluate(
            rule(causal={"mode": "auto", "control": {
                "filters": [{"field": "loan.region", "op": "=", "value": "华北"}]}}),
            "demo", NOW)
        c = out.evidence["causal"]
        assert c["rung"] == "L2"
        assert c["unmet"][0]["condition"] == "C6"
        assert c["unmet"][0]["reason"] == "donors_need_dim_control"
        assert c["control"]["mode"] == "filters" and c["control"]["donors"] == []

    async def test_unidentified_treated_marks_l3_unavailable(self, svc):
        """subject 没有钉住处理维 → 处理组 = 整个总体 → 供体被污染,停 L2。"""
        await _reseed(svc, (_series("华东", 100, 110, 200)
                            + _series("华北", 50, 55, 60)
                            + _series("华南", 25, 27.5, 50)
                            + _series("西南", 40, 44, 88)))
        out = await svc.evaluate(
            rule(subject={"metrics": ["loan_balance"], "dimensions": []}),
            "demo", NOW)
        assert out.triggered is True
        c = out.evidence["causal"]
        assert c["rung"] == "L2"
        assert c["unmet"][0]["reason"] == "treated_unidentified"
        assert c["control"]["treated_labels"] is None
        assert c["control"]["donors"] == []

    async def test_no_seasonal_degrades_structurally_with_zero_queries(
            self, svc, monkeypatch):
        # 手改 decisions.yml 可绕过 lint —— 运行时的结构性降级必须兜住
        raw = {**RULE}
        raw.pop("seasonal")
        calls = counting(svc, monkeypatch)
        out = await svc.evaluate(parse_rule(raw), "demo", NOW)
        assert out.triggered is True
        c = out.evidence["causal"]
        assert c["rung"] == "L1" and c["unmet"][0] == {
            "condition": "C4", "reason": "no_seasonal"}
        assert c["degraded"] == [{"stage": "causal", "reason": "no_seasonal"}]
        assert _series_calls(calls) == []

    async def test_budget_yield_is_recorded_not_silent(self, svc, monkeypatch):
        import trove.services.decision.service as service_mod
        from trove.services.decision.budget import DecisionBudget

        monkeypatch.setattr(service_mod, "DecisionBudget",
                            lambda: DecisionBudget(total=2))
        calls = counting(svc, monkeypatch)
        out = await svc.evaluate(rule(), "demo", NOW)
        assert out.error == "" and out.triggered is True    # 让路不改判定
        budget = out.evidence["budget"]
        assert budget["by_stage"]["judge"] == 2
        assert [y["stage"] for y in budget["yielded"]] == ["causal"]
        c = out.evidence["causal"]
        assert c["rung"] == "L1"
        assert c["unmet"][0] == {"condition": "C5",
                                 "reason": "query_budget_exceeded"}
        assert _series_calls(calls) == []


class TestCostDiscipline:
    async def test_not_triggered_spends_nothing_on_causal(self, svc, monkeypatch):
        calls = counting(svc, monkeypatch)
        out = await svc.evaluate(
            rule(conditions={"all": ["delta_pct > 10"]}), "demo", NOW)
        assert out.error == "" and out.triggered is False
        assert len(calls) == 2
        assert "causal" not in out.evidence
        assert "budget" not in out.evidence

    async def test_undeclared_causal_keeps_the_old_evidence_shape(
            self, svc, monkeypatch):
        """兼容安全带:声明 seasonal+significance 但未声明 causal 的规则,
        响应不出现 causal 键、也不跑因果取数。"""
        raw = {**RULE}
        raw.pop("causal")
        calls = counting(svc, monkeypatch)
        out = await svc.evaluate(
            parse_rule({**raw, "significance": {"require": ""}}), "demo", NOW)
        assert out.error == ""
        assert "causal" not in out.evidence
        # 块序列只有显著性阶段自己那条;因果一条都不跑
        assert len(_series_calls(calls)) == 1
