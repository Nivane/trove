"""``check_drift.py`` 退出码测试 —— 钉住「未检查 ≠ 干净」。

这组测试存在的直接原因:该脚本此前**没有任何测试**,而它的退出码正是
CI 判断依据。于是「catalog 连不上 → 报告全空 → dirty=False → exit 0」
这条路径长期存在且无人发现 —— **数据库连不上的时候,漂移门禁是最绿的。**
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
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
    """``decide`` 的输入形状 —— 走真的 ``collect``,不手搓 DriftReport。

    手搓会让这些测试与合流逻辑脱钩:哪天 ``collect`` 改了判定,测试仍然绿。
    """
    return {"drift": collect(kb_report, semantic, name)}


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


def test_render_human_lists_both_levels_from_the_unified_items(cd, capsys):
    """渲染的是**合流后**的证据流 —— 两条路的发现出现在同一段输出里。"""
    kb = _l1(new_tables=["brand_new"], gone_tables=["legacy"],
             column_changes={"orders": {"added": ["amount"], "removed": ["old"]}})
    sem = {"stale": True, "gone_tables": ["sales"],
           "missing_fields": {"orders": ["region"]},
           "missing_keys": {"orders": ["id"]},
           "relationship_breaks": [{"name": "r1", "detail": "orders gone"}],
           "status": "ok", "skip_reason": None}
    cd._render_human(cd.decide({"demo": _rep(kb, sem)}, errors=[])[0])
    out, _ = capsys.readouterr()

    assert "  + new table: brand_new" in out
    assert "  - gone table: legacy" in out
    assert "  + orders.amount added" in out
    assert "  - orders.old removed" in out
    assert "  - gone dataset: sales" in out
    assert "  - orders missing field: region" in out
    assert "  - orders missing key: id" in out
    assert "  - relationship r1: orders gone" in out
    assert "KB drift: DRIFT" in out and "Semantic drift: DRIFT" in out


def test_render_item_never_silently_drops_an_unknown_kind(cd):
    """检测器将来新增 kind 时,输出里仍要看得见 —— 静默丢弃 = 发现消失。"""
    line = cd._render_item({"level": "L3", "kind": "value_not_anchorable",
                            "subject": "orders.status", "severity": "warning",
                            "detail": {}})
    assert "orders.status" in line and "value_not_anchorable" in line


# ── payload 契约 ────────────────────────────────────────────────────

def test_payload_carries_levels_verified(cd):
    """门禁的输入。

    ``status=ok`` 只说「整体跑完了」,说不了「哪几级真验证过」—— 一个没查
    L2 的 ok 报告若只暴露 status,消费方会读成「L2 干净」。
    """
    payload, _ = cd.decide({"demo": _rep(_l1())}, errors=[])
    drift = payload["datasources"]["demo"]["drift"]
    assert drift["status"] == "ok"
    assert drift["levels_verified"] == ["L1"], "没有语义层 → L2 未验证"


def test_payload_marks_l1_l2_verified_when_both_ran(cd):
    sem = {"stale": False, "gone_tables": [], "missing_fields": {},
           "missing_keys": {}, "relationship_breaks": [],
           "status": "ok", "skip_reason": None}
    payload, _ = cd.decide({"demo": _rep(_l1(), sem)}, errors=[])
    assert payload["datasources"]["demo"]["drift"]["levels_verified"] == ["L1", "L2"]


def test_payload_is_json_serializable(cd):
    """``--json`` 直接 ``json.dumps`` 它;塞进去一个 dataclass 会在运行时炸。"""
    kb = _l1(gone_tables=["legacy"])
    payload, _ = cd.decide({"demo": _rep(kb, name="demo")}, errors=[])
    json.dumps(payload)  # 不抛即通过


# ── 选源:注册表为空(全新 clone / CI)时内置 demo 仍要能查 ──────────────
#
# 上面所有测试都从 ``decide`` 起跳,绕过了 ``_run`` 的选源那一段 —— 而这一段
# 正是唯一需要 ``.trove/datasources.yml`` 的地方,也是唯一在 CI 上会出问题的
# 地方。这组测试补的就是这个缺口。

def _ns(**over) -> argparse.Namespace:
    base = {"datasource": "demo", "kb_dir": None, "json": False, "verbose": False}
    base.update(over)
    return argparse.Namespace(**base)


def _stub_check(cd, seen: list):
    """把 ``_check_datasource`` 换成记录器 —— 选源是这里要测的,真建 demo 库不是。

    记的是**整个 cfg** 而不是 (name, type):后者对「注册表来的」与「兜底造的」
    完全一样,断言它就等于断言了一个两种路径都能满足的条件 —— 兜底即使越权,
    那条测试也照样绿。
    """
    async def fake(name, cfg, kb):
        seen.append(cfg)
        return {"drift": collect(_l1(), None, name)}
    return fake


def test_demo_resolves_without_a_registry(cd, monkeypatch, tmp_path):
    """没有 ``.trove/datasources.yml`` 时,``--datasource demo`` 仍要能跑。

    CI 恰好就是这种环境:该文件被 gitignore,只有跑过 serve / 后台注册才会有。
    在补上兜底之前,这里返回 exit 2「datasource not found」—— 于是漂移门在 CI 上
    **永远红**,而红的原因(选源失败)看起来像数据出了问题,门很快会被当成噪声。
    一条永远红的门等于没有门,只是更吵。
    """
    monkeypatch.chdir(tmp_path)
    assert not (tmp_path / ".trove" / "datasources.yml").exists()

    seen: list = []
    monkeypatch.setattr(cd, "_check_datasource", _stub_check(cd, seen))

    payload, code = asyncio.run(cd._run(_ns()))

    assert [(c.name, c.type) for c in seen] == [("demo", "demo")], "内置 demo 没被认出来"
    assert code == 0


def test_registered_demo_still_wins_over_the_fallback(cd, monkeypatch, tmp_path):
    """注册表里有 demo 时用登记的那份(带持久化的连接信息),兜底不越权。

    ``demo`` 在 ``naming.RESERVED_NAMES`` 里,用户注册不了这个名字,所以
    注册表里的 demo 只可能是内置的那个 —— 这条测的是**优先级**,不是冲突。

    断言特意落在 ``retrieval_backend`` 上:它是那种**只有登记的那份才有**的
    字段(兜底造出来的 cfg 用的是类默认值)。只断言 name/type 的话,两条路径
    给出的答案一模一样,测试就失去了它名字里说的那个能力。
    """
    monkeypatch.chdir(tmp_path)
    store = tmp_path / ".trove"
    store.mkdir()
    (store / "datasources.yml").write_text(
        "datasources:\n"
        "- name: demo\n"
        "  type: demo\n"
        "  connection: {}\n"
        "  credentials: {}\n"
        "  retrieval_backend: hybrid\n",
        encoding="utf-8",
    )

    seen: list = []
    monkeypatch.setattr(cd, "_check_datasource", _stub_check(cd, seen))

    _, code = asyncio.run(cd._run(_ns()))

    assert code == 0
    assert seen[0].retrieval_backend == "hybrid", "兜底把登记的 demo 顶掉了"


def test_unknown_name_still_exits_2(cd, monkeypatch, tmp_path):
    """兜底只给内置 demo,不能顺手把任何拼错的名字都放行。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cd, "_check_datasource", _stub_check(cd, []))

    payload, code = asyncio.run(cd._run(_ns(datasource="nosuchsrc")))

    assert code == 2
    assert "not found" in payload["error"]
