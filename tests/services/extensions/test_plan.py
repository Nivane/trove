"""``trove extensions plan``(E6)—— 运行时装配清单 + 信封对账。

清单是**运行时事实**:信封 × state 的投影 —— 只有 confirmed 的资产按其挂点
展开,disabled(E6 颗粒停用)整封摘除。它与信封**声明面**(``extensions
list``:全量、含未生效)是两张视图,不是同一张。

对账(R7 的运行时半边,静态半边在 ``trove validate`` 的检查器族)把两份
独立副本摆在一起:信封的 ``state`` 列,与运行时读路径**自己的读函数**
(``SkillService.list_org(confirmed_only=True)`` / ``KbService.load_decisions``
里 ``enabled`` 的规则)。今天两份由构造相等,所以这里的价值全在**漂移时
响亮** —— 两个方向各一组测试。

零 LLM、零网络。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from trove.cli.commands.extensions_cmds import main_extensions
from trove.services.extensions import (
    LIVE_STATE,
    MountCatalog,
    build_plan,
    build_decision_envelope,
    build_skill_envelope,
    clear_cache,
    plan_entries,
    reconcile_plan,
    runtime_liveness,
)

_CATALOG = MountCatalog(
    inject_nodes=("query_sketch", "gen_sql"),
    ad_nodes=("gen_sql",),
    validator_host="validate",
)


@pytest.fixture(autouse=True)
def _clear_envelope_cache():
    clear_cache()
    yield
    clear_cache()


# ── helpers ──────────────────────────────────────────────


def _skill_env(name: str, *, status: str, tier: str = "required",
               node: str | None = None, **over):
    entry = {"name": name, "tier": tier, "status": status, **over}
    if node:
        entry["triggers"] = {"node": node}
    return build_skill_envelope(entry, files=[], source="org", catalog=_CATALOG)


class _Rule:
    def __init__(self, rid="r1", *, enabled=True, **over):
        self.id = rid
        self.conditions = ["delta_pct > 20"]
        self.action = None
        self.enabled = enabled
        for k, v in over.items():
            setattr(self, k, v)


def _write_skill(root: Path, name: str, *, status: str, tier: str = "required",
                 node: str | None = None, body: str = "body") -> None:
    d = root / ".trove" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    meta: dict = {"name": name, "description": f"{name} desc",
                  "tier": tier, "status": status, "version": 1}
    if node:
        meta["triggers"] = {"node": node}
    fm = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    (d / "SKILL.md").write_text(f"---\n{fm}\n---\n\n{body}\n", encoding="utf-8")


def _write_decision(root: Path, ds: str, rid: str, *, enabled: bool = True) -> None:
    d = root / ".trove" / "kb" / ds
    d.mkdir(parents=True, exist_ok=True)
    path = d / "decisions.yml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {
        "version": 1, "rules": []}
    rule = {
        "id": rid, "name": f"{rid} name", "severity": "warning",
        "subject": {"metrics": ["total_amount"]},
        "baseline": {"kind": "literal", "value": 1.0},
        "conditions": ["delta_pct > 20"],
    }
    if not enabled:
        rule["enabled"] = False
    doc["rules"].append(rule)
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")


def _names(entries, kind: str) -> list[str]:
    """该 kind 的**唯一**资产名(一个资产可以有多行挂点)。"""
    return sorted({e.name for e in entries if e.kind == kind})


# ── plan_entries:信封 × state ────────────────────────────


class TestPlanEntries:
    def test_only_confirmed_assets_enter(self):
        """disabled / pending / rejected 三种状态一起摘 —— 判据是同一条。"""
        entries = plan_entries([
            _skill_env("live", status="confirmed"),
            _skill_env("stopped", status="disabled"),
            _skill_env("draft", status="pending"),
            _skill_env("gone", status="rejected"),
        ])
        assert _names(entries, "skill") == ["live"]

    def test_mounts_are_expanded_one_row_per_mount(self):
        entries = plan_entries([
            _skill_env("pinned", status="confirmed", node="gen_sql"),
            _skill_env("no-node-declared", status="confirmed"),  # 展开到全目录
        ])
        assert [(e.name, e.node, e.effect) for e in entries] == [
            ("no-node-declared", "gen_sql", "inject"),
            ("no-node-declared", "query_sketch", "inject"),
            ("pinned", "gen_sql", "inject"),
        ]

    def test_empty_when_nothing_is_live(self):
        assert plan_entries([_skill_env("stopped", status="disabled")]) == []

    def test_decision_disabled_is_dropped(self):
        live = build_decision_envelope(
            _Rule("on"), files=[], datasource="demo")
        off = build_decision_envelope(
            _Rule("off", enabled=False), files=[], datasource="demo")
        assert live.state == LIVE_STATE and off.state == "disabled"
        assert _names(plan_entries([live, off]), "decision") == ["on"]


# ── reconcile_plan:纯函数,两个方向 ──────────────────────


class TestReconcile:
    def test_equal_copies_diff_empty(self):
        same = {"skill": {"skill:org:a"}, "decision": {"decision:kb:demo:r1"}}
        assert reconcile_plan(same, dict(same)) == []

    def test_runtime_only_is_named_as_a_missing_filter(self):
        """运行时有、静态没有 = 停用的资产还在被投递(消费面漏了过滤)。"""
        drift = reconcile_plan({"skill": set()}, {"skill": {"skill:org:x"}})
        assert len(drift) == 1
        assert "skill:org:x" in drift[0]
        assert "漏了过滤" in drift[0]

    def test_static_only_is_named_as_drift(self):
        drift = reconcile_plan({"skill": {"skill:org:x"}}, {"skill": set()})
        assert len(drift) == 1 and "漂移" in drift[0]

    def test_kinds_do_not_cross_contaminate(self):
        """skill 与 decision 各自对账:一个 kind 的差集不替另一个背锅。"""
        drift = reconcile_plan(
            {"skill": {"s1"}, "decision": {"d1"}},
            {"skill": {"s1"}, "decision": set()},
        )
        assert len(drift) == 1 and "d1" in drift[0]


# ── build_plan:真文件端到端 ─────────────────────────────


class TestBuildPlan:
    def test_disabled_org_skill_is_dropped_and_reconciled(self, tmp_path):
        """验收面:停用 → 清单摘除;`--plan` 与信封静态视图 diff 空。"""
        _write_skill(tmp_path, "live-one", status="confirmed", node="gen_sql")
        _write_skill(tmp_path, "stopped-one", status="disabled", node="gen_sql")

        plan = build_plan(tmp_path)
        assert _names(plan.entries, "skill") == [
            "diagnose_failure", "live-one", "plan_query", "sql_construction"]
        assert "stopped-one" not in _names(plan.entries, "skill")
        # 信封声明面照旧列着它(state=disabled)—— 两张视图职责不同
        stated = {e["name"]: e["state"] for e in plan.envelopes
                  if e["kind"] == "skill" and e["source"] == "org"}
        assert stated == {"live-one": "confirmed", "stopped-one": "disabled"}
        # 对账:两边都摘掉 disabled 之后 diff 空
        assert plan.drift == []
        assert plan.ok and plan.exit_code == 0
        assert plan.static_live["skill"] == ["skill:org:live-one"]
        assert plan.runtime_live["skill"] == ["skill:org:live-one"]

    def test_enable_brings_the_asset_back(self, tmp_path):
        from trove.services.skills.service import SkillService

        _write_skill(tmp_path, "flip", status="confirmed", node="gen_sql")
        svc = SkillService(tmp_path / ".trove" / "skills")
        svc.disable("flip")
        assert "flip" not in _names(build_plan(tmp_path).entries, "skill")

        svc.enable("flip")
        plan = build_plan(tmp_path)
        assert "flip" in _names(plan.entries, "skill")
        assert plan.drift == [] and plan.exit_code == 0

    def test_disabled_decision_rule_is_dropped(self, tmp_path):
        _write_decision(tmp_path, "demo", "keep", enabled=True)
        _write_decision(tmp_path, "demo", "muted", enabled=False)

        plan = build_plan(tmp_path)
        assert "keep" in _names(plan.entries, "decision")
        assert "muted" not in _names(plan.entries, "decision")
        assert plan.drift == []
        assert plan.runtime_live["decision"] == ["decision:kb:demo:keep"]

    def test_drift_when_runtime_leaks_a_stopped_asset(self, tmp_path, monkeypatch):
        """消费面若绕过过滤(停用资产仍被投递)→ 对账响亮,退出码 1。"""
        from trove.services.extensions import plan as plan_mod

        _write_skill(tmp_path, "stopped", status="disabled", node="gen_sql")
        monkeypatch.setattr(
            plan_mod, "runtime_liveness",
            lambda root, **kw: {"skill": {"skill:org:stopped"}, "decision": set()})
        plan = build_plan(tmp_path)
        assert plan.ok is False and plan.exit_code == 1
        assert any("漏了过滤" in d for d in plan.drift)

    def test_drift_when_static_claims_but_runtime_lacks(self, tmp_path, monkeypatch):
        from trove.services.extensions import plan as plan_mod

        _write_skill(tmp_path, "claimed", status="confirmed", node="gen_sql")
        monkeypatch.setattr(
            plan_mod, "runtime_liveness",
            lambda root, **kw: {"skill": set(), "decision": set()})
        plan = build_plan(tmp_path)
        assert plan.ok is False
        assert any("漂移" in d for d in plan.drift)

    def test_reports_and_render_shape(self, tmp_path):
        _write_skill(tmp_path, "shown", status="confirmed", node="gen_sql")
        _write_skill(tmp_path, "hidden", status="disabled", node="gen_sql")
        plan = build_plan(tmp_path)
        d = plan.to_dict()
        assert set(d) >= {"entries", "envelopes", "static_live", "runtime_live",
                          "drift", "counts", "ok", "exit_code"}
        assert d["counts"]["disabled"] == 1
        text = plan.render()
        assert "运行时实际装配清单" in text
        assert "shown" in text and "hidden" not in text
        assert "一致(diff 空)" in text and "退出码 0" in text

    def test_empty_project_is_clean_not_drifting(self, tmp_path):
        """没有 org 资产 ≠ 对账失败:两边都空,diff 空。"""
        plan = build_plan(tmp_path)
        assert plan.drift == [] and plan.exit_code == 0
        assert plan.static_live == {"skill": [], "decision": []}


# ── runtime_liveness:与运行时同一份读函数 ────────────────


class TestRuntimeLiveness:
    def test_reads_through_the_services_own_readers(self, tmp_path):
        from trove.services.skills.service import SkillService

        _write_skill(tmp_path, "on", status="confirmed", node="gen_sql")
        _write_skill(tmp_path, "off", status="disabled", node="gen_sql")
        _write_decision(tmp_path, "demo", "r-on", enabled=True)
        _write_decision(tmp_path, "demo", "r-off", enabled=False)

        live = runtime_liveness(tmp_path)
        assert live["skill"] == {"skill:org:on"}
        assert live["decision"] == {"decision:kb:demo:r-on"}
        # 与直接调用服务读函数的结果逐字一致(没有第二份判据)
        assert live["skill"] == {
            f"skill:org:{e['name']}"
            for e in SkillService(tmp_path / ".trove" / "skills")
            .list_org(confirmed_only=True)
        }


# ── CLI:退出码 + 两个输出面 ─────────────────────────────


class TestCliPlan:
    def test_text_output_exit_0_when_reconciled(self, tmp_path, monkeypatch,
                                                capsys):
        _write_skill(tmp_path, "live", status="confirmed", node="gen_sql")
        _write_skill(tmp_path, "stopped", status="disabled", node="gen_sql")
        monkeypatch.chdir(tmp_path)
        code = main_extensions(["plan"])
        out = capsys.readouterr().out
        assert code == 0
        assert "运行时实际装配清单" in out
        assert "live" in out and "stopped" not in out
        assert "一致(diff 空)" in out

    def test_json_output_is_machine_readable(self, tmp_path, monkeypatch,
                                             capsys):
        import json

        _write_skill(tmp_path, "stopped", status="disabled", node="gen_sql")
        monkeypatch.chdir(tmp_path)
        assert main_extensions(["plan", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["kind"] == "plan" and payload["ok"] is True
        assert payload["drift"] == []
        assert payload["counts"]["disabled"] == 1
        # 对账可自查:两份副本都在输出里,差集 = drift(空)
        assert payload["static_live"] == payload["runtime_live"]

    def test_exit_1_on_drift(self, tmp_path, monkeypatch, capsys):
        from trove.services.extensions import plan as plan_mod

        _write_skill(tmp_path, "claimed", status="confirmed", node="gen_sql")
        monkeypatch.setattr(
            plan_mod, "runtime_liveness",
            lambda root, **kw: {"skill": set(), "decision": set()})
        monkeypatch.chdir(tmp_path)
        code = main_extensions(["plan"])
        out = capsys.readouterr().out
        assert code == 1
        assert "对账漂移" in out and "退出码 1" in out
