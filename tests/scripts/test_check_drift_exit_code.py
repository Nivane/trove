"""``check_drift.py`` 退出码测试 —— 钉住「未检查 ≠ 干净」。

这组测试存在的直接原因:该脚本此前**没有任何测试**,而它的退出码正是
CI 判断依据。于是「catalog 连不上 → 报告全空 → dirty=False → exit 0」
这条路径长期存在且无人发现 —— **数据库连不上的时候,漂移门禁是最绿的。**
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from trove.services.drift import collect


def _load_script():
    """按路径加载 scripts/check_drift.py(它不是包的一部分)。"""
    path = Path(__file__).resolve().parents[2] / "scripts" / "check_drift.py"
    spec = importlib.util.spec_from_file_location("check_drift_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cd():
    return _load_script()


def _l1(**over):
    base = {"datasource": "demo", "new_tables": [], "gone_tables": [],
            "column_changes": {}, "status": "ok", "skip_reason": None}
    base.update(over)
    return base


def _rep(kb_report, semantic=None, name="demo"):
    return {"kb": kb_report, "semantic": semantic,
            "drift": collect(kb_report, semantic, name)}


# ── 修掉的那条 ──────────────────────────────────────────────────────

def test_catalog_unreachable_exits_2_not_0(cd):
    """核心回归:连不上库必须 exit 2,不能 exit 0。"""
    _, code = cd.decide(
        {"demo": _rep(_l1(status="skipped", skip_reason="catalog_unreachable"))},
        errors=[],
    )
    assert code == 2


def test_missing_kb_exits_2(cd):
    _, code = cd.decide(
        {"demo": _rep(_l1(status="skipped", skip_reason="kb_missing"))},
        errors=[],
    )
    assert code == 2


def test_incomplete_is_listed_in_payload(cd):
    payload, _ = cd.decide(
        {"demo": _rep(_l1(status="skipped", skip_reason="catalog_unreachable"))},
        errors=[],
    )
    assert payload["incomplete"] == {"demo": "catalog_unreachable"}


def test_semantic_skipped_makes_whole_check_incomplete(cd):
    """语义层查不成 → 该数据源整体未完成(即使 L1 查成了)。"""
    sem = {"stale": False, "gone_tables": [], "missing_fields": {},
           "missing_keys": {}, "relationship_breaks": [],
           "status": "skipped", "skip_reason": "no_catalog"}
    payload, code = cd.decide({"demo": _rep(_l1(), sem)}, errors=[])
    assert code == 2
    assert payload["incomplete"] == {"demo": "no_catalog"}


# ── 正常路径不受影响 ────────────────────────────────────────────────

def test_clean_exits_0(cd):
    payload, code = cd.decide({"demo": _rep(_l1())}, errors=[])
    assert code == 0
    assert "incomplete" not in payload


def test_drift_found_exits_1(cd):
    _, code = cd.decide({"demo": _rep(_l1(gone_tables=["legacy"]))}, errors=[])
    assert code == 1


def test_semantic_drift_alone_exits_1(cd):
    sem = {"stale": True, "gone_tables": ["sales"], "missing_fields": {},
           "missing_keys": {}, "relationship_breaks": [],
           "status": "ok", "skip_reason": None}
    _, code = cd.decide({"demo": _rep(_l1(), sem)}, errors=[])
    assert code == 1


def test_info_only_drift_still_exits_1(cd):
    """L1 新增表是 info 级,但仍是「有漂移」→ 1。

    门禁的**阻断**判定走 DriftGate(只拦 warning/critical);CLI 的退出码
    是「有没有发现」,两件事,不要混。
    """
    _, code = cd.decide({"demo": _rep(_l1(new_tables=["brand_new"]))}, errors=[])
    assert code == 1


def test_errors_exit_2(cd):
    _, code = cd.decide({}, errors=["demo: connection refused"])
    assert code == 2


def test_one_bad_datasource_does_not_hide_a_good_one(cd):
    """一个数据源未完成,另一个干净 → 仍 exit 2,但干净的仍出现在 payload。"""
    reports = {
        "bad": _rep(_l1(status="skipped", skip_reason="catalog_unreachable"), name="bad"),
        "good": _rep(_l1(), name="good"),
    }
    payload, code = cd.decide(reports, errors=[])
    assert code == 2
    assert set(payload["datasources"]) == {"bad", "good"}
    assert payload["incomplete"] == {"bad": "catalog_unreachable"}


# ── 人类可读输出 ────────────────────────────────────────────────────

def test_render_human_warns_before_showing_ok(cd, capsys):
    """未完成的 KB drift 不得渲染成 OK。"""
    cd._render_human(cd.decide(
        {"demo": _rep(_l1(status="skipped", skip_reason="catalog_unreachable"))},
        errors=[])[0])
    out, err = capsys.readouterr()
    assert "未检查" in out
    assert "KB drift: OK" not in out
    assert "catalog_unreachable" in err


def test_render_human_clean_path_unchanged(cd, capsys):
    cd._render_human(cd.decide({"demo": _rep(_l1())}, errors=[])[0])
    out, _ = capsys.readouterr()
    assert "KB drift: OK" in out
    assert "Semantic drift: n/a (no semantic layer)" in out
