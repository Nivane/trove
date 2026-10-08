"""编译回放 —— 三态 + 诚实第四态 not_applicable + 异常降 unknown 不阻断。"""
from __future__ import annotations

from pathlib import Path

import yaml

from trove.services.semantic_layer.sandbox import run_replay

BASE = {"version": "0.2.0.dev0", "semantic_model": [{
    "name": "demo",
    "datasets": [{"name": "loan", "source": "loan"}],
    "metrics": [{"name": "refund_rate", "expression": {"dialects": [
        {"dialect": "ANSI_SQL", "expression": "SUM(loan.refund) / SUM(loan.amount)"}]}}],
    "topics": [{"name": "credits", "datasets": ["loan"]}],
}]}

AFTER_NO_REFUND = {"version": "0.2.0.dev0", "semantic_model": [{
    "name": "demo",
    "datasets": [{"name": "loan", "source": "loan"}],
    "metrics": [],
    "topics": [{"name": "credits", "datasets": ["loan"]}],
}]}

RULES = {"version": 3, "rules": [
    {"id": "revenue_drop", "subject": {"metrics": ["refund_rate"]},
     "condition": "current < 0.9 * baseline"}]}


def _dump(doc: dict) -> str:
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)


def _kb(tmp_path: Path, *, rules: dict | None = RULES) -> Path:
    ds = tmp_path / "kb" / "demo"
    ds.mkdir(parents=True)
    if rules is not None:
        (ds / "decisions.yml").write_text(_dump(rules), encoding="utf-8")
    return tmp_path / "kb"


def test_regression_highlights_now_broken(tmp_path):
    out = run_replay(_kb(tmp_path), "demo", dialect="sqlite",
                     base_text=_dump(BASE), after_text=_dump(AFTER_NO_REFUND))
    assert out["now_broken"] == ["rule:revenue_drop"]
    assert out["verdict"] == "regresses"
    assert "rule:revenue_drop" in out["replayed"]


def test_improvement_was_broken_now_compiles(tmp_path):
    base = {"version": "0.2.0.dev0", "semantic_model": [{
        "name": "demo", "datasets": [{"name": "loan", "source": "loan"}],
        "metrics": []}]}
    out = run_replay(_kb(tmp_path), "demo", dialect="sqlite",
                     base_text=_dump(base), after_text=_dump(BASE))
    assert out["was_broken_now_compiles"] == ["rule:revenue_drop"]
    assert out["verdict"] == "improves"


def test_neutral_when_nothing_changes(tmp_path):
    out = run_replay(_kb(tmp_path), "demo", dialect="sqlite",
                     base_text=_dump(BASE), after_text=_dump(BASE))
    assert out["verdict"] == "neutral"
    assert out["now_broken"] == []


def test_repeated_calls_do_not_share_state(tmp_path):
    """两次调用互不串味 —— 浅拷贝模块级 _EMPTY 会让第二次带着第一次的键。"""
    kb = _kb(tmp_path)
    first = run_replay(kb, "demo", dialect="sqlite",
                       base_text=_dump(BASE), after_text=_dump(AFTER_NO_REFUND))
    second = run_replay(kb, "demo", dialect="sqlite",
                        base_text=_dump(BASE), after_text=_dump(BASE))
    assert first["now_broken"] == ["rule:revenue_drop"]
    assert second["now_broken"] == []
    assert second["verdict"] == "neutral"


def test_not_applicable_when_nothing_replayable(tmp_path):
    """KB 里一条可回放产物都没有 → 如实 not_applicable,不冒充 neutral（§5.4）。"""
    kb = _kb(tmp_path, rules=None)
    bare = {"version": "0.2.0.dev0", "semantic_model": [{
        "name": "demo", "datasets": [{"name": "loan", "source": "loan"}], "metrics": []}]}
    out = run_replay(kb, "demo", dialect="sqlite",
                     base_text=_dump(bare), after_text=_dump(bare))
    assert out["verdict"] == "not_applicable"
    assert out["not_applicable_reason"]


def test_broken_after_model_is_unknown_not_error(tmp_path):
    out = run_replay(_kb(tmp_path), "demo", dialect="sqlite",
                     base_text=_dump(BASE), after_text="{{{ not yaml")
    assert out["verdict"] == "unknown"
    assert out["reason"]


def test_broken_decisions_is_unknown(tmp_path):
    kb = _kb(tmp_path)
    (kb / "demo" / "decisions.yml").write_text("rules: 不是列表", encoding="utf-8")
    out = run_replay(kb, "demo", dialect="sqlite",
                     base_text=_dump(BASE), after_text=_dump(BASE))
    assert out["verdict"] == "unknown"


def test_topic_scope_emptied_is_broken(tmp_path):
    after = {"version": "0.2.0.dev0", "semantic_model": [{
        "name": "demo", "datasets": [], "metrics": [],
        "topics": [{"name": "credits", "datasets": ["loan"]}]}]}
    out = run_replay(_kb(tmp_path, rules=None), "demo", dialect="sqlite",
                     base_text=_dump(BASE), after_text=_dump(after))
    assert "topic:credits" in out["now_broken"]
    assert out["verdict"] == "regresses"
