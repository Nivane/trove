"""影响面 resolver —— reference（结构化引用）与 mention（词面）逐类标注。"""
from __future__ import annotations

from pathlib import Path

import yaml

from trove.services.drift.impact import resolve_impact
from trove.services.drift.models import ImpactSet


def _write(kb_dir: Path, ds: str, name: str, doc: dict) -> None:
    d = kb_dir / ds
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")


DECISIONS = {"version": 3, "rules": [
    {"id": "revenue_drop", "subject": {"metrics": ["refund_rate"]},
     "condition": "current < 0.9 * baseline"},
    {"id": "region_shift", "subject": {"metrics": ["total_loan"], "dimensions": ["loan.region"]},
     "condition": "current < baseline"},
]}

EXAMPLES = {"examples": [
    {"question": "华东区的退款率是多少", "sql": "SELECT refund_rate FROM loan WHERE region='east'",
     "tags": ["refund", "loan"], "confirmed": True},
    {"question": "每月贷款笔数", "sql": "SELECT COUNT(*) FROM loan", "tags": ["loan"]},
]}

LESSONS = {"lessons": [
    {"pattern": "refund_rate 口径含冲正", "note": "退款率分母用 loan.amount", "confirmed": True},
    {"pattern": "日期比较注意事项", "note": "无关教训", "confirmed": True},
]}


def _kb(tmp_path: Path) -> Path:
    kb = tmp_path / "kb"
    _write(kb, "demo", "decisions.yml", DECISIONS)
    _write(kb, "demo", "examples.yml", EXAMPLES)
    _write(kb, "demo", "lessons.yml", LESSONS)
    _write(kb, "demo", "semantics.yml", {"version": "0.2.0.dev0", "semantic_model": [{
        "name": "demo", "datasets": [{"name": "loan", "source": "loan"}],
        "metrics": [{"name": "refund_rate", "expression": {"dialects": [
            {"dialect": "ANSI_SQL", "expression": "SUM(loan.refund) / SUM(loan.amount)"}]}}],
        "topics": [{"name": "credits", "datasets": ["loan"], "metrics": ["refund_rate"]}],
    }]})
    return kb


def test_metric_subject_hits_rules_and_topics_as_reference(tmp_path):
    imp = resolve_impact(_kb(tmp_path), "demo", {"metric:refund_rate"})
    assert "revenue_drop" in imp.rules
    assert "credits" in imp.topics
    assert imp.basis["revenue_drop"] == "reference"
    assert imp.basis["credits"] == "reference"


def test_field_subject_hits_rule_dimension(tmp_path):
    imp = resolve_impact(_kb(tmp_path), "demo", {"field:loan.region"})
    assert "region_shift" in imp.rules
    assert imp.basis["region_shift"] == "reference"


def test_mention_basis_for_examples_and_lessons(tmp_path):
    imp = resolve_impact(_kb(tmp_path), "demo", {"metric:refund_rate"})
    # 恰好命中「华东区的退款率是多少」这一条,且**靠的是它的 SQL 文本** ——
    # 该示例的问句与 tags 都不含 `refund_rate`(问句/tags 单独匹配均不命中)。
    # 标签取问句(问句缺失才回退 SQL),所以「英文词面命中」体现在命中的是哪条,
    # 而不体现在标签文本里。
    assert imp.examples == ["华东区的退款率是多少"]
    assert any("refund_rate" in p for p in imp.lessons)
    assert all(imp.basis.get(x) == "mention"
               for x in imp.examples if x not in imp.rules)
    assert imp.basis[next(p for p in imp.lessons if "refund_rate" in p)] == "mention"


def test_dataset_subject(tmp_path):
    imp = resolve_impact(_kb(tmp_path), "demo", {"dataset:loan"})
    assert "credits" in imp.topics
    assert "revenue_drop" not in imp.rules or imp.basis["revenue_drop"] == "reference"


def test_unknown_subject_is_empty_not_error(tmp_path):
    imp = resolve_impact(_kb(tmp_path), "demo", {"metric:nonexistent"})
    assert imp.is_empty() or imp.topics == []


def test_missing_files_are_empty(tmp_path):
    imp = resolve_impact(tmp_path / "kb", "nosuch", {"metric:x"})
    assert isinstance(imp, ImpactSet)
    assert imp.is_empty()


def test_broken_decisions_raises_for_caller(tmp_path):
    """本模块不吞结构性错误：坏 decisions.yml 抛给调用方折 degraded（I8 同款）。"""
    kb = _kb(tmp_path)
    (kb / "demo" / "decisions.yml").write_text("rules: 不是列表", encoding="utf-8")
    try:
        resolve_impact(kb, "demo", {"metric:refund_rate"})
    except Exception:
        return
    raise AssertionError("坏 decisions.yml 应当抛出")
