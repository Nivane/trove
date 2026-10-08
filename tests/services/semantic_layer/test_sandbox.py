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


def test_still_compiles_lists_survivors(tmp_path):
    """A8：三态里的 `still_compiles` 如实列出「改完仍然编得过」的产物（§5.4 输出形状）。"""
    out = run_replay(_kb(tmp_path), "demo", dialect="sqlite",
                     base_text=_dump(BASE), after_text=_dump(BASE))
    assert out["still_compiles"] == ["rule:revenue_drop", "topic:credits"]


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


def test_topic_scope_restored_is_improvement(tmp_path):
    """域过期 → 域恢复 = 真「曾经编不出」,记 improves（「新增主题不算修复」的正向半边）。"""
    base = {"version": "0.2.0.dev0", "semantic_model": [{
        "name": "demo", "datasets": [], "metrics": [],
        "topics": [{"name": "credits", "datasets": ["credit_card"]}]}]}
    after = {"version": "0.2.0.dev0", "semantic_model": [{
        "name": "demo", "datasets": [{"name": "credit_card", "source": "credit_card"}],
        "metrics": [],
        "topics": [{"name": "credits", "datasets": ["credit_card"]}]}]}
    out = run_replay(_kb(tmp_path, rules=None), "demo", dialect="sqlite",
                     base_text=_dump(base), after_text=_dump(after))
    assert out["was_broken_now_compiles"] == ["topic:credits"]
    assert out["verdict"] == "improves"


def test_block_pick_follows_dialect_parameter(tmp_path):
    """``_parse`` 用调用方的 dialect 挑表达式块 —— mysql 挑到编不出的块,sqlite 回退 ANSI。

    MYSQL 块 ``SUM(1)`` 不含 ``dataset.field`` 引用 → 度量锚定为空 →
    ``build_and_compile`` 抛「cannot determine anchor datasets」;ANSI 块引用
    ``loan.refund`` → 锚定 loan → 能编。块选择若写死 sqlite,mysql 那次会误挑
    ANSI 把坏块编译成功 —— 这条断言即红。
    """
    doc = {"version": "0.2.0.dev0", "semantic_model": [{
        "name": "demo",
        "datasets": [{"name": "loan", "source": "loan"}],
        "metrics": [{"name": "refund_rate", "expression": {"dialects": [
            {"dialect": "MYSQL", "expression": "SUM(1)"},
            {"dialect": "ANSI_SQL", "expression": "SUM(loan.refund)"}]}}],
    }]}
    kb = _kb(tmp_path)  # RULES 的 subject 正是 refund_rate

    mysql_out = run_replay(kb, "demo", dialect="mysql",
                           base_text=_dump(doc), after_text=_dump(doc))
    assert "rule:revenue_drop" in mysql_out["now_broken"]
    sqlite_out = run_replay(kb, "demo", dialect="sqlite",
                            base_text=_dump(doc), after_text=_dump(doc))
    assert "rule:revenue_drop" not in sqlite_out["now_broken"]


def test_unexpected_exception_degrades_to_unknown(tmp_path, monkeypatch):
    """运行期任何异常 → verdict=unknown + reason,不阻断（模块契约）。"""
    from trove.services.semantic_layer import sandbox

    def boom(model, rule, dialect):
        raise RuntimeError("boom")

    monkeypatch.setattr(sandbox, "_rule_compiles", boom)
    out = run_replay(_kb(tmp_path), "demo", dialect="sqlite",
                     base_text=_dump(BASE), after_text=_dump(BASE))
    assert out["verdict"] == "unknown"
    assert "boom" in out["reason"]
    assert out["now_broken"] == []
