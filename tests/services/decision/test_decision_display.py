"""决策行卡显示名(FR-DEC-1,E7 决策标注)—— judge 之后的纯标注 pass。

三条钉死的纪律(N34):

1. **标注命中**:规则维度解析到声明了 ``enum_display`` / ``value_aliases`` /
   实体自然键的字段 → 行卡与显著性 ``by_dim`` 条目带可选 ``dim_display``;
2. **缺声明 → 证据逐字节不变**:不是"值换了",是键压根不出现 —— 同一份
   数据(同一 NOW)跑两遍(带声明 / 不带声明),剥掉 ``dim_display`` 后
   JSON 逐字节相同,且 message 不受标注影响;
3. **判定键一律不动**:``dim`` 仍是身份(``by_dim`` 键、``diff_verdicts``
   的分组键都读它),``triggered``/``confidence``/``gated`` 与无声明跑完全
   一致;``diff_verdicts`` 对新键视而不见 —— 加一列显示名绝不能让判定
   历史看起来"变了"。

数据构造沿用 ``test_decision_significance_stage.py`` 的小世界(12 个历史月
+ 当期,华东超带 / 华北近失),这样显著性 by_dim 的标注路径是真的被走到
的,而不是手工拼一个证据 dict。
"""

from __future__ import annotations

import copy
import json
from datetime import datetime

import pytest

from tests.services.semantic_layer.test_display import _model
from trove.core.types import DatasourceConfig
from trove.services.decision.rules import parse_rule
from trove.services.decision.service import (
    DecisionService,
    annotate_dim_display,
    label_display,
    resolve_dim_pairs,
)
from trove.services.decision.verdicts import VerdictRecord, diff_verdicts
from trove.services.kb.service import KbService

NOW = datetime(2026, 9, 27, 10, 0, 0)

#: 与显著性阶段测试同一份语义模型(region 无显示声明)。
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

#: 同一模型 + region 上的 enum_display 声明(唯一的差异)。
DISPLAY_SEMANTICS = SEMANTICS.replace(
    "            datatype: TEXT\n            semantic_role: dimension\n",
    "            datatype: TEXT\n            semantic_role: dimension\n"
    "            enum_display:\n"
    "              华东: East China\n"
    "              华北: North China\n",
    1,
)

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


def _seed_sql() -> str:
    rows: list[tuple[str, float, str]] = []
    for i, month in enumerate(HISTORY_MONTHS):
        rows.append(("华东", 960.0 if i % 2 == 0 else 1040.0, month))
        rows.append(("华北", 400.0 if i % 2 == 0 else 600.0, month))
    rows.append(("华东", 1600.0, "2026-09-15"))
    rows.append(("华北", 680.0, "2026-09-15"))
    values = ", ".join(f"('{r}', {v}, '{d}')" for r, v, d in rows)
    return f"INSERT INTO loan (region, amount, date) VALUES {values}"


def rule(**overrides):
    return parse_rule({**RULE, **overrides})


@pytest.fixture
async def make_svc(tmp_path):
    """按语义模型文本建服务(同一小世界数据两遍:带/不带显示声明)。"""
    from trove.services.datasource.registry import ConnectorRegistry

    registries = []

    async def _make(semantics: str) -> DecisionService:
        # 每次调用一个独立的 KB 项目目录:语义模型是**求值时**现读的,
        # 共用目录会让第二次写覆盖第一次(两遍跑会读到同一份声明)
        registry = ConnectorRegistry()
        config = DatasourceConfig(
            name="demo", type="sqlite",
            connection_params={"path": ":memory:"}, default=True,
        )
        adapter = await registry.register(config, set_default=True)
        await adapter.execute(
            "CREATE TABLE loan (region TEXT, amount REAL, date DATE)")
        await adapter.execute(_seed_sql())

        kb = KbService(tmp_path / f"proj{len(registries)}")
        (kb.kb_dir / "demo").mkdir(parents=True, exist_ok=True)
        kb.semantics_path("demo").write_text(semantics, encoding="utf-8")
        registries.append(registry)
        return DecisionService(registry, kb)

    yield _make
    for registry in registries:
        await registry.close_all()


def _strip_displays(evidence: dict) -> dict:
    """剥掉全部 dim_display(深拷贝,不动原证据)。"""
    out = copy.deepcopy(evidence)
    for card in out.get("rows") or []:
        if isinstance(card, dict):
            card.pop("dim_display", None)
    by_dim = ((out.get("significance") or {}).get("by_dim") or {})
    for entry in by_dim.values():
        if isinstance(entry, dict):
            entry.pop("dim_display", None)
    return out


# ---------------------------------------------------------------------------
# 单元:三个模块级纯函数
# ---------------------------------------------------------------------------


class TestResolveDimPairs:
    def test_resolves_declared_dims_through_the_compiler(self):
        m = _model()
        pairs = resolve_dim_pairs(m, ["purpose", "loan.frequency"], ["loan"])
        assert [p[0] for p in pairs] == ["loan", "loan"]
        assert [p[1].name for p in pairs] == ["purpose", "frequency"]

    def test_unresolvable_dim_is_none_not_guessed(self):
        m = _model()
        pairs = resolve_dim_pairs(m, ["purpose", "nope"], ["loan"])
        assert pairs[0] is not None and pairs[1] is None

    def test_empty_and_absent_model_are_empty(self):
        assert resolve_dim_pairs(_model(), [], ["loan"]) == []
        assert resolve_dim_pairs(None, ["purpose"], ["loan"]) == []


class TestLabelDisplay:
    def test_single_dim_enum_display_hit(self):
        m = _model()
        pairs = resolve_dim_pairs(m, ["purpose"], ["loan"])
        assert label_display(m, pairs, "M") == "monthly"

    def test_single_dim_value_aliases_hit(self):
        m = _model()
        pairs = resolve_dim_pairs(m, ["frequency"], ["loan"])
        assert label_display(m, pairs, "M") == "monthly"

    def test_single_dim_entity_natural_key(self):
        m = _model()
        pairs = resolve_dim_pairs(m, ["loan_id"], ["loan"])
        assert label_display(m, pairs, "L1") == "贷款 L1"

    def test_single_dim_unresolved_value_stays_absent(self):
        m = _model()
        pairs = resolve_dim_pairs(m, ["purpose"], ["loan"])
        assert label_display(m, pairs, "X") is None
        assert label_display(m, pairs, "") is None
        assert label_display(m, [None], "M") is None

    def test_multi_dim_splits_with_the_same_separator_as_to_map(self):
        m = _model()
        pairs = resolve_dim_pairs(m, ["purpose", "frequency"], ["loan"])
        # M/M 两段都命中;W 在 frequency 上无别名 → 该段保留原值
        assert label_display(m, pairs, "M / M") == "monthly / monthly"
        assert label_display(m, pairs, "M / W") == "monthly / W"

    def test_multi_dim_segment_count_mismatch_abandons_the_whole_group(self):
        m = _model()
        pairs = resolve_dim_pairs(m, ["purpose", "frequency"], ["loan"])
        # 值里自带分隔符 → 段数与维数对不上:整组放弃,绝不把显示名
        # 钉到错的段上
        assert label_display(m, pairs, "M / M / M") is None

    def test_multi_dim_all_unresolved_is_none(self):
        m = _model()
        pairs = resolve_dim_pairs(m, ["purpose", "frequency"], ["loan"])
        assert label_display(m, pairs, "X / Y") is None


class TestAnnotateDimDisplay:
    def test_cards_gain_display_only_when_mapped(self):
        m = _model()
        pairs = resolve_dim_pairs(m, ["purpose"], ["loan"])
        rows = [{"dim": "M", "triggered": True}, {"dim": "X", "triggered": False}]
        annotate_dim_display(rows, m, pairs)
        assert rows[0]["dim_display"] == "monthly"
        assert rows[0]["dim"] == "M" and rows[0]["triggered"] is True
        assert "dim_display" not in rows[1]

    def test_no_pairs_or_no_rows_is_a_noop(self):
        m = _model()
        rows = [{"dim": "M"}]
        annotate_dim_display(rows, m, [])
        annotate_dim_display([], m, resolve_dim_pairs(m, ["purpose"], ["loan"]))
        assert rows == [{"dim": "M"}]


# ---------------------------------------------------------------------------
# 端到端:真实 datasource 上的 evaluate_rule
# ---------------------------------------------------------------------------


class TestEvaluationAnnotation:
    async def test_rows_and_by_dim_annotated_dim_is_still_identity(self, make_svc):
        svc = await make_svc(DISPLAY_SEMANTICS)
        out = await svc.evaluate(rule(), "demo", NOW)
        assert out.error == "", out.error

        rows = {r["dim"]: r for r in out.evidence["rows"]}
        assert rows["华东"]["dim_display"] == "East China"
        assert rows["华北"]["dim_display"] == "North China"
        # dim 仍是身份:条件作用域/回评分组键读的都是它
        assert rows["华东"]["dim"] == "华东"

        by_dim = out.evidence["significance"]["by_dim"]
        assert set(by_dim) == {"华东", "华北"}          # 键不动
        assert by_dim["华东"]["dim_display"] == "East China"
        assert by_dim["华北"]["dim_display"] == "North China"
        assert by_dim["华东"]["gated"] is True          # 门位不受标注影响

    async def test_absence_of_declaration_is_byte_identical(self, make_svc):
        plain = await make_svc(SEMANTICS)
        disp = await make_svc(DISPLAY_SEMANTICS)
        plain_out = await plain.evaluate(rule(), "demo", NOW)
        disp_out = await disp.evaluate(rule(), "demo", NOW)
        assert plain_out.error == disp_out.error == ""

        # 判定键(N34):标注不参与判定 —— 两份跑出的终态必须一致
        assert disp_out.triggered == plain_out.triggered
        assert disp_out.message == plain_out.message   # message 用原始维值
        plain_rows = {r["dim"]: r for r in plain_out.evidence["rows"]}
        disp_rows = {r["dim"]: r for r in disp_out.evidence["rows"]}
        for dim in ("华东", "华北"):
            assert disp_rows[dim]["triggered"] == plain_rows[dim]["triggered"]
            assert disp_rows[dim]["confidence"] == plain_rows[dim]["confidence"]
            assert disp_rows[dim].get("gated") == plain_rows[dim].get("gated")

        # 逐字节:带声明跑剥掉 dim_display 后,与不带声明跑完全相同 ——
        # 「未声明 → 输出逐字节不变」的最强形式(唯一差异是加出来的键)
        assert "dim_display" not in json.dumps(plain_out.evidence)
        assert json.dumps(_strip_displays(disp_out.evidence), sort_keys=True) \
            == json.dumps(plain_out.evidence, sort_keys=True)


class TestDiffIgnoresDisplay:
    def _record(self, rows: list[dict]) -> VerdictRecord:
        return VerdictRecord(
            rule_id="loan-spike", datasource="demo",
            evidence={"rows": rows}, triggered=True,
        )

    def test_diff_reads_dim_and_keys_by_position_only(self):
        base = [
            {"dim": "华东", "triggered": True, "delta_pct": 0.6},
            {"dim": "华北", "triggered": False, "delta_pct": 0.13},
        ]
        annotated = [dict(r, dim_display=d) for r, d in
                     zip(base, ("East China", "North China"))]
        out = diff_verdicts(self._record(base), self._record(annotated))
        assert out["groups"] == []
        assert out["status_change"] is None

    def test_diff_still_sees_real_changes(self):
        prev = [{"dim": "华东", "triggered": False, "delta_pct": 0.01,
                 "dim_display": "East China"}]
        cur = [{"dim": "华东", "triggered": True, "delta_pct": 0.6,
                "dim_display": "East China"}]
        out = diff_verdicts(self._record(prev), self._record(cur))
        assert out["groups"] == [
            {"dim": "华东", "change": "fired", "triggered": [False, True],
             "delta_pct": [0.01, 0.6]},
        ]
