"""候选收件箱:软 MISS → 确定性语义候选(pending,零 LLM)。

覆盖两层:
- ``candidate_specs`` 纯映射(锚定/跳过规则 —— 宁缺勿错的那一半);
- ``capture_candidates`` 在真实 KB 目录上的落库行为(幂等/上限/绝不
  自应用),并证明候选**可被确认**(payload 就是可应用的声明,不是
  诱导管理员确认出错误模型的半成品)。
"""

from __future__ import annotations

import yaml
import pytest

from trove.services.kb.service import KbService
from trove.services.semantic_layer.candidates import (
    MAX_CANDIDATES_PER_RUN,
    candidate_specs,
    capture_candidates,
)
from trove.services.semantic_layer.manage import SemanticManager
from trove.services.semantic_layer.ossie import parse_ossie


def _doc(metrics: list[dict] | None = None) -> dict:
    return {
        "version": "0.2.0.dev0",
        "semantic_model": [{
            "name": "demo",
            "datasets": [
                {
                    "name": "loan", "source": "loan",
                    "primary_key": ["loan_id"],
                    "fields": [
                        {"name": "loan_id", "datatype": "Integer",
                         "expression": {"dialects": [{
                             "dialect": "ANSI_SQL", "expression": "loan_id"}]}},
                        {"name": "amount", "datatype": "Decimal",
                         "expression": {"dialects": [{
                             "dialect": "ANSI_SQL", "expression": "amount"}]}},
                        {"name": "status", "datatype": "String",
                         "expression": {"dialects": [{
                             "dialect": "ANSI_SQL", "expression": "status"}]}},
                    ],
                },
                {
                    "name": "district", "source": "district",
                    "primary_key": ["district_id"],
                    "fields": [
                        {"name": "A3", "datatype": "String",
                         "expression": {"dialects": [{
                             "dialect": "ANSI_SQL", "expression": "A3"}]}},
                    ],
                },
            ],
            "metrics": metrics or [],
        }],
    }


def _model(metrics: list[dict] | None = None):
    return parse_ossie(yaml.safe_dump(_doc(metrics), allow_unicode=True))


@pytest.fixture
def kb(tmp_path):
    k = KbService(tmp_path / "proj")
    ds_dir = k.kb_dir / "demo"
    ds_dir.mkdir(parents=True)
    (ds_dir / "semantics.yml").write_text(
        yaml.safe_dump(_doc(), allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    return k


# ── 纯映射 ──────────────────────────────────────────────


class TestCandidateSpecs:
    def test_metric_from_qualified_aggregate(self):
        specs = candidate_specs(
            [{"reason": "no_metric_match", "component": "AVG(loan.amount)"}],
            _model())
        assert specs == [{
            "kind": "metric", "name": "avg_amount",
            "payload": {"expression": "AVG(loan.amount)", "datasets": ["loan"]},
            "reason": "no_metric_match",
        }]

    def test_metric_skips_unanchorable_forms(self):
        model = _model()
        for component in (
            "SUM(amount)",                     # 裸列:锚不住数据集
            "SUM(ghost.amount)",               # 未声明数据集
            "SUM(loan.amount + district.A3)",  # 多数据集聚合
            "SUM(loan.amount) / COUNT(loan.loan_id)",  # 多聚合算式
            "COUNT(*)",                        # 无列
            "平均贷款金额",                      # 非 SQL
        ):
            assert candidate_specs(
                [{"reason": "no_metric_match", "component": component}],
                model) == [], component

    def test_metric_skips_taken_name(self):
        """推导名已声明 → 跳过:候选绝不诱导覆盖既有声明。"""
        model = _model(metrics=[{
            "name": "avg_amount",
            "expression": {"dialects": [{
                "dialect": "ANSI_SQL", "expression": "AVG(loan.amount)"}]},
        }])
        assert candidate_specs(
            [{"reason": "no_metric_match", "component": "AVG(loan.amount)"}],
            model) == []

    def test_field_from_dotted_ref(self):
        specs = candidate_specs(
            [{"reason": "unresolved_filter_field", "component": "loan.grade"}],
            _model())
        assert specs == [{
            "kind": "field", "name": "loan.grade",
            "payload": {"expression": "loan.grade"},
            "reason": "unresolved_filter_field",
        }]

    def test_time_field_reason_marks_is_time(self):
        """时间分桶的 field 解析失败 → 候选带上 is_time(按时间轴起草)。"""
        specs = candidate_specs(
            [{"reason": "time_field_not_declared",
              "component": "loan.issue_date"}],
            _model())
        assert specs[0]["payload"] == {
            "expression": "loan.issue_date", "is_time": True}

    def test_field_skips_declared_undeclared_and_unanchorable(self):
        model = _model()
        for component in (
            "loan.status",     # 已声明 → 缺口另有原因
            "ghost.x",         # 数据集未声明
            "amount",          # 裸列:锚不住
            "loan.amount * 2",  # 非列引用
        ):
            assert candidate_specs(
                [{"reason": "unresolved_filter_field", "component": component}],
                model) == [], component

    def test_non_candidate_reasons_and_empty_components_ignored(self):
        model = _model()
        # 两键枚举缺口(无 value 第三键)不产生候选:老形状输入 → 候选集
        # 与今天逐字节相同(值类候选另有 TestValueCandidates,证据门控)。
        for miss in (
            {"reason": "enum_value_unresolved", "component": "loan.status"},
            {"reason": "invalid_op", "component": "="},
            {"reason": "having_without_aggregation", "component": ""},
            {"reason": "no_metric_match", "component": ""},
        ):
            assert candidate_specs([miss], model) == [], miss

    def test_metric_priority_and_dedupe(self):
        specs = candidate_specs([
            {"reason": "unresolved_filter_field", "component": "loan.grade"},
            {"reason": "no_metric_match", "component": "AVG(loan.amount)"},
            {"reason": "unresolved_answer_column", "component": "loan.grade"},
        ], _model())
        assert [s["name"] for s in specs] == ["avg_amount", "loan.grade"]

    def test_no_model_yields_no_specs(self):
        assert candidate_specs(
            [{"reason": "no_metric_match", "component": "AVG(loan.amount)"}],
            None) == []


def _model_with_values(values: list[str] | None, aliases: dict | None = None):
    """status 字段带实测取值(probe 结构事实)+ 可选既有值别名。"""
    doc = _doc()
    status = doc["semantic_model"][0]["datasets"][0]["fields"][2]
    if values is not None:
        status["values"] = values
    if aliases is not None:
        status.setdefault("ai_context", {})["value_aliases"] = aliases
    return parse_ossie(yaml.safe_dump(doc, allow_unicode=True))


class TestValueCandidates:
    """值类缺口候选(enum_value_unresolved + 第三键 value):证据门控。

    唯一可以落候选的情形 = 提问字面量与**实测取值**大小写无关地相等
    (库里确实这么存)。翻译缺口(男性/月结 → M/monthly)机器定不了目标码
    ——自映射会把错字面量冻结进骨架,聚合题的 0 行过滤没守卫兜底 → 不猜。
    """

    def test_value_hit_lands_alias_candidate(self):
        specs = candidate_specs(
            [{"reason": "enum_value_unresolved", "component": "loan.status",
              "value": "Z"}],
            _model_with_values(["A", "B", "Z"]))
        assert specs == [{
            "kind": "field", "name": "loan.status",
            "payload": {"expression": "status", "value_aliases": {"Z": ["Z"]}},
            "reason": "enum_value_unresolved",
            "evidence": "loan.status=Z",
        }]

    def test_value_match_is_case_insensitive_stored_spelling_wins(self):
        specs = candidate_specs(
            [{"reason": "enum_value_unresolved", "component": "loan.status",
              "value": " z "}],
            _model_with_values(["A", "Z"]))
        assert specs[0]["payload"]["value_aliases"] == {"Z": ["Z"]}

    def test_value_misses_without_probe_evidence_skipped(self):
        # 值不在实测取值里(翻译缺口)= 主导失败模式:不产生候选
        assert candidate_specs(
            [{"reason": "enum_value_unresolved", "component": "loan.status",
              "value": "男性"}],
            _model_with_values(["M", "F"])) == []
        # 未探测(values 缺席)= 没有证据:同样不猜
        assert candidate_specs(
            [{"reason": "enum_value_unresolved", "component": "loan.status",
              "value": "Z"}],
            _model_with_values(None)) == []

    def test_value_merges_existing_aliases_not_replaces(self):
        """payload 整体替换 —— 既有别名必须全量并入,否则确认即丢失。"""
        specs = candidate_specs(
            [{"reason": "enum_value_unresolved", "component": "loan.status",
              "value": "Z"}],
            _model_with_values(["A", "Z"], aliases={"A": ["active"]}))
        assert specs[0]["payload"]["value_aliases"] == {
            "A": ["active"], "Z": ["Z"]}

    def test_value_alias_deduped_when_already_declared(self):
        specs = candidate_specs(
            [{"reason": "enum_value_unresolved", "component": "loan.status",
              "value": "z"}],
            _model_with_values(["Z"], aliases={"Z": ["z"]}))
        assert specs[0]["payload"]["value_aliases"] == {"Z": ["z"]}

    def test_value_junk_is_never_echoed(self):
        """提问字面量当不可信输入:只有命中实测取值的输入才进 payload,
        且落进去的是**探测到的存储写法**(probe 已净化),不是用户原文。"""
        for junk in ("Z\n- evil: yaml", "Z'", "Z" * 500, "", "  "):
            assert candidate_specs(
                [{"reason": "enum_value_unresolved", "component": "loan.status",
                  "value": junk}],
                _model_with_values(["A", "B"])) == [], repr(junk)

    def test_value_spec_deduped_against_field_gap_same_name(self):
        """同一字段的声明缺口与值缺口同轮出现 → (kind, name) 去重只留一条。"""
        specs = candidate_specs([
            {"reason": "unresolved_filter_field", "component": "loan.grade"},
            {"reason": "enum_value_unresolved", "component": "loan.status",
             "value": "Z"},
        ], _model_with_values(["Z"]))
        assert [s["name"] for s in specs] == ["loan.grade", "loan.status"]


# ── 落库(真实 KB 目录) ──────────────────────────────────


class TestCaptureCandidates:
    async def test_capture_creates_pending_only_and_keeps_semantics_intact(self, kb):
        before = (kb.kb_dir / "demo" / "semantics.yml").read_text(encoding="utf-8")
        created = await capture_candidates(kb, "demo", "哪个地区贷款金额最高?", [
            {"reason": "no_metric_match", "component": "AVG(loan.amount)"},
            {"reason": "unresolved_filter_field", "component": "loan.grade"},
        ])
        assert {c["kind"] for c in created} == {"metric", "field"}
        pending = SemanticManager(kb).drafts("demo")["pending"]
        assert len(pending) == 2
        assert pending[0]["note"] == "auto:no_metric_match:哪个地区贷款金额最高?"
        assert all(d["status"] == "pending" for d in pending)
        # 候选绝不自动应用:semantics.yml 逐字节未动
        assert (kb.kb_dir / "demo" / "semantics.yml").read_text(
            encoding="utf-8") == before

    async def test_capture_dedupes_against_pending_queue(self, kb):
        misses = [{"reason": "no_metric_match", "component": "AVG(loan.amount)"}]
        first = await capture_candidates(kb, "demo", "q", misses)
        assert len(first) == 1
        # 同题重跑:create_draft 自身无去重,幂等闸门在这里
        assert await capture_candidates(kb, "demo", "q", misses) == []
        assert len(SemanticManager(kb).drafts("demo")["pending"]) == 1

    async def test_capture_caps_per_run(self, kb):
        misses = [
            {"reason": "unresolved_filter_field", "component": f"loan.f{i}"}
            for i in range(5)
        ]
        created = await capture_candidates(kb, "demo", "q", misses)
        assert len(created) == MAX_CANDIDATES_PER_RUN

    async def test_capture_without_model_or_kb_is_noop(self, kb, tmp_path):
        bare = KbService(tmp_path / "bare")
        assert await capture_candidates(
            bare, "demo", "q",
            [{"reason": "no_metric_match", "component": "AVG(loan.amount)"}]) == []
        assert await capture_candidates(
            None, "demo", "q",
            [{"reason": "no_metric_match", "component": "AVG(loan.amount)"}]) == []

    async def test_candidate_is_confirmable(self, kb):
        """候选 payload 必须是**可应用的正确声明** —— 确认后模型真的长出来。"""
        created = await capture_candidates(kb, "demo", "q", [
            {"reason": "no_metric_match", "component": "AVG(loan.amount)"},
            {"reason": "unresolved_filter_field", "component": "loan.grade"},
        ])
        manager = SemanticManager(kb)
        for entry in created:
            await manager.confirm_draft("demo", entry["id"], dialect="sqlite")

        model = manager.model("demo")
        assert "avg_amount" in {m.name for m in model.metrics}
        loan = next(d for d in model.datasets if d.name == "loan")
        assert "grade" in {f.name for f in loan.fields}
        assert manager.drafts("demo")["pending"] == []
