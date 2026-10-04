"""显著性阶段(B2)端到端 —— 真实 datasource 上的噪声带门。

数据构造(12 个历史月 + 当期):
  - 华东:历史 960/1040 交替(median 1000、MAD 40、带 ≈ [792, 1208]),
    当期 1600 —— 条件触发且**超出噪声带**;
  - 华北:历史 400/600 交替(median 500、MAD 100、带 ≈ [-19, 1019]),
    当期 680 —— 条件触发但**在带内**(近失)。

这一对是本批的核心语义:同一个条件、两种命运,而两者都在证据里
可见(华北的 matched 保留 —— 审计要看得见近失)。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from trove.core.types import DatasourceConfig
from trove.services.decision.rules import parse_rule, rule_rev
from trove.services.decision.service import DecisionService
from trove.services.kb.service import KbService

NOW = datetime(2026, 9, 27, 10, 0, 0)

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

#: 12 个历史月(2025-09 .. 2026-08)+ 当期(2026-09);偶数下标低值、
#: 奇数下标高值 —— 6/6 均衡让 median 落在两级之间(MAD 非零)。
HISTORY_MONTHS = [
    (f"{2025 + (8 + i) // 12}-{(8 + i) % 12 + 1:02d}-15") for i in range(12)
]

RULE = {
    "id": "loan-spike",
    "name": "贷款余额异动",
    "window": "本月",
    "subject": {"metrics": ["loan_balance"], "dimensions": ["region"]},
    "baseline": {"kind": "prev_period"},
    "scope": "per_dimension",
    "conditions": {"all": ["delta_pct > 0.05"]},
    "seasonal": {"lookback": 12},
    "significance": {"require": "outside_band"},
}

#: 无 v3 声明的老规则(兼容安全带用):条件永不触发。
PLAIN_RULE = {
    "id": "plain",
    "name": "普通规则",
    "window": "本月",
    "subject": {"metrics": ["loan_balance"], "dimensions": ["region"]},
    "baseline": {"kind": "prev_period"},
    "scope": "per_dimension",
    "conditions": {"all": ["delta_pct > 10"]},
}


def _seed_sql() -> str:
    rows: list[tuple[str, float, str]] = []
    for i, month in enumerate(HISTORY_MONTHS):
        rows.append(("华东", 960.0 if i % 2 == 0 else 1040.0, month))
        rows.append(("华北", 400.0 if i % 2 == 0 else 600.0, month))
    rows.append(("华东", 1600.0, "2026-09-15"))
    rows.append(("华北", 680.0, "2026-09-15"))
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
    await adapter.execute(_seed_sql())

    kb = KbService(tmp_path / "proj")
    (kb.kb_dir / "demo").mkdir(parents=True, exist_ok=True)
    kb.semantics_path("demo").write_text(SEMANTICS, encoding="utf-8")
    yield DecisionService(registry, kb)
    await registry.close_all()


def rule(**overrides):
    return parse_rule({**RULE, **overrides})


def counting(svc, monkeypatch) -> list[str]:
    """把 registry.execute 换成记账包装(与结果缓存无关,只数调用)。"""
    calls: list[str] = []
    orig = svc.connectors.execute

    async def counted(sql, datasource=None):
        calls.append(sql)
        return await orig(sql, datasource)

    monkeypatch.setattr(svc.connectors, "execute", counted)
    return calls


class TestRequireGate:
    async def test_only_the_group_outside_the_band_triggers(self, svc):
        out = await svc.evaluate(rule(), "demo", NOW)
        assert out.error == "", out.error
        assert out.triggered is True

        rows = {r["dim"]: r for r in out.evidence["rows"]}
        east, north = rows["华东"], rows["华北"]
        assert east["triggered"] is True and east["gated"] is True
        assert east["confidence"] == 1.0          # |z| ≈ 10 → 位置分数封顶
        # 华北条件满足但在带内 —— 近失必须可见,而不是被静默吞掉
        assert north["triggered"] is False and north["gated"] is False
        assert north["matched"] == ["delta_pct > 0.05"]
        assert north["contribution"] is not None

        sig = out.evidence["significance"]
        assert sig["required"] is True
        assert sig["by_dim"]["华东"]["reason"] == ""
        assert sig["by_dim"]["华北"]["reason"] == "within_band"
        assert sig["by_dim"]["华北"]["confidence"] == 0.0
        assert sig["seasonal"] == {"grain": "", "lookback": 12,
                                   "mode": "trailing", "k": 3.5}

        assert "华东" in out.message and "华北" not in out.message

    async def test_band_is_built_from_the_twelve_historical_blocks(self, svc):
        out = await svc.evaluate(rule(), "demo", NOW)
        sig = out.evidence["significance"]
        assert len(sig["blocks"]) == 12
        assert sig["blocks"][0] == ["2025-09-01", "2025-09-30"]
        assert sig["blocks"][-1] == ["2026-08-01", "2026-08-31"]   # 不含当期
        band = sig["by_dim"]["华东"]["band"]
        assert band["center"] == pytest.approx(1000.0)
        assert band["n"] == 12
        assert band["degraded"] == []
        assert sig["by_dim"]["华东"]["low_n"] is False
        assert "sql" in sig and "GROUP BY" in sig["sql"]

    async def test_band_line_rides_in_the_evidence_for_notifications(self, svc):
        from trove.services.decision.significance import band_line

        out = await svc.evaluate(rule(), "demo", NOW)
        line = band_line(out.evidence["significance"])
        assert line.startswith("噪声带：") and "华东" in line

    async def test_declared_without_require_records_but_does_not_gate(self, svc):
        out = await svc.evaluate(
            rule(significance={"require": ""}), "demo", NOW)
        assert out.error == "" and out.triggered is True
        rows = {r["dim"]: r for r in out.evidence["rows"]}
        assert rows["华北"]["triggered"] is True    # 门只看条件
        assert rows["华北"]["gated"] is False       # 带照记
        assert out.evidence["significance"]["required"] is False

    async def test_confidence_is_usable_in_conditions(self, svc):
        out = await svc.evaluate(
            rule(significance={"require": ""},
                 conditions={"all": ["delta_pct > 0.05 and confidence > 0.5"]}),
            "demo", NOW)
        assert out.error == ""
        rows = {r["dim"]: r for r in out.evidence["rows"]}
        assert rows["华东"]["triggered"] is True     # 位置分数 1.0 > 0.5
        assert rows["华北"]["triggered"] is False    # 位置分数 0.0

    async def test_min_confidence_raises_the_bar(self, svc):
        out = await svc.evaluate(
            rule(significance={"require": "outside_band",
                               "min_confidence": 0.99}),
            "demo", NOW)
        # 华东 |z|≈10.1 → 位置 1.0 ≥ 0.99,仍过门
        assert out.triggered is True
        assert out.evidence["significance"]["by_dim"]["华东"]["gated"] is True


class TestFailLoud:
    async def test_require_errors_when_blocks_cannot_confirm(self, svc):
        """块数不足(< MIN_BLOCKS)时**不确认** —— require 下这是 error run,
        不是静默「未触发」(算不出 ≠ 在带内)。"""
        out = await svc.evaluate(
            rule(seasonal={"lookback": 4}), "demo", NOW)
        assert out.error and "significance requires" in out.error
        assert "insufficient_n" in out.error
        assert out.triggered is False and out.message == ""

    async def test_same_case_without_require_is_a_clean_verdict(self, svc):
        out = await svc.evaluate(
            rule(seasonal={"lookback": 4}, significance={"require": ""}),
            "demo", NOW)
        assert out.error == ""
        sig = out.evidence["significance"]
        assert sig["by_dim"]["华东"]["reason"] == "insufficient_n"
        assert sig["by_dim"]["华东"]["gated"] is False

    async def test_require_errors_without_a_resolvable_time_field(self, svc):
        # 字面基准 + 无窗口:条件仍可触发(current > 500),但历史块无处可取
        # —— 没有时间场的规则在 require 下必须报错,不能拿不出带就放行。
        out = await svc.evaluate(
            rule(window="", baseline={"kind": "literal", "value": 100},
                 conditions={"all": ["current > 500"]}),
            "demo", NOW)
        assert out.error and "no_time_field" in out.error
        assert out.triggered is False

    async def test_budget_yield_is_loud_under_require(self, svc, monkeypatch):
        import trove.services.decision.service as service_mod
        from trove.services.decision.budget import DecisionBudget

        # 判定自己花 2 条 → 显著性阶段拿不到额度 → 让路(而非静默跳过)
        monkeypatch.setattr(service_mod, "DecisionBudget",
                            lambda: DecisionBudget(total=2))
        out = await svc.evaluate(rule(), "demo", NOW)
        assert out.error and "query_budget_exceeded" in out.error

    async def test_budget_yield_is_recorded_when_not_required(self, svc, monkeypatch):
        import trove.services.decision.service as service_mod
        from trove.services.decision.budget import DecisionBudget

        monkeypatch.setattr(service_mod, "DecisionBudget",
                            lambda: DecisionBudget(total=2))
        out = await svc.evaluate(
            rule(significance={"require": ""}), "demo", NOW)
        assert out.error == ""
        budget = out.evidence["budget"]
        assert budget["limit"] == 2
        assert budget["by_stage"]["judge"] == 2
        assert [y["stage"] for y in budget["yielded"]] == ["significance"]
        assert out.evidence["significance"]["by_dim"]["华东"]["reason"] \
            == "query_budget_exceeded"


class TestSamePhase:
    async def test_same_phase_blocks_shift_by_whole_years(self, svc):
        out = await svc.evaluate(
            rule(seasonal={"mode": "same_phase", "lookback": 12},
                 significance={"require": ""}),
            "demo", NOW)
        assert out.error == ""
        sig = out.evidence["significance"]
        assert sig["mode"] == "same_phase"
        assert sig["blocks"][0] == ["2014-09-01", "2014-09-30"]
        assert sig["blocks"][-1] == ["2025-09-01", "2025-09-30"]
        # 只有 2025-09 有数据 → 单点历史:散布为 0,带算不出(响亮地记账)
        assert sig["by_dim"]["华东"]["band"]["n"] == 1
        assert sig["by_dim"]["华东"]["reason"] == "zero_scale"


class TestBackwardCompatibility:
    async def test_undeclared_rule_keeps_the_old_evidence_shape(self, svc):
        """兼容安全带:没有 v3 声明的规则,证据除 rule_rev 外逐键不变
        (rule_rev 是有意的加性例外 —— N2 修复,每条 verdict 都带)。"""
        out = await svc.evaluate(parse_rule(PLAIN_RULE), "demo", NOW)
        assert out.error == ""
        base_keys = {
            "rule_id", "rule_digest", "rule_name", "severity", "priority",
            "recommendation", "owner_role", "model_version", "window_expr",
            "periods", "times", "rows", "evidence", "provenance", "rule_rev",
        }
        assert set(out.evidence) == base_keys
        assert out.evidence["rule_rev"] == rule_rev(parse_rule(PLAIN_RULE))
        for row in out.evidence["rows"]:
            assert set(row) == {"dim", "current", "baseline", "delta",
                                "delta_pct", "contribution", "triggered",
                                "matched"}

    async def test_not_triggered_declared_rule_spends_only_the_judge_queries(
            self, svc, monkeypatch):
        """成本纪律(R3):未触发时显著性阶段不跑 —— 声明了也一样。"""
        calls = counting(svc, monkeypatch)
        out = await svc.evaluate(
            rule(conditions={"all": ["delta_pct > 10"]}), "demo", NOW)
        assert out.error == "" and out.triggered is False
        assert len(calls) == 2
        assert "significance" not in out.evidence
        assert "budget" not in out.evidence

    async def test_undeclared_rule_never_runs_the_stage(self, svc, monkeypatch):
        calls = counting(svc, monkeypatch)
        out = await svc.evaluate(parse_rule(PLAIN_RULE), "demo", NOW)
        assert out.error == "" and out.triggered is False
        assert len(calls) == 2


class TestBudgetAccounting:
    async def test_ledger_snapshot_counts_every_stage(self, svc):
        out = await svc.evaluate(rule(), "demo", NOW)
        budget = out.evidence["budget"]
        assert budget["limit"] == 12
        assert budget["by_stage"]["judge"] == 2
        assert budget["by_stage"]["significance"] == 1
        assert budget["yielded"] == []
        assert budget["used"] == 2 + 1 + budget["by_stage"].get("bridge", 0)

    async def test_rule_rev_is_pinned_per_rule(self, svc):
        out = await svc.evaluate(rule(), "demo", NOW)
        assert out.evidence["rule_rev"] == rule_rev(rule())
        # 同一条规则的再次判定 → 同一 rev(回评分桶的键)
        out2 = await svc.evaluate(rule(), "demo", datetime(2026, 9, 27, 23, 0))
        assert out2.evidence["rule_rev"] == out.evidence["rule_rev"]
