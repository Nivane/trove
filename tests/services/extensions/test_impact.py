"""影响面回放(E4,``--impact``)—— 四桶差分 + 证据 + 隐私 + 确定性,零 LLM 硬门。

分节对应交付面:候选集加载(包/资产目录;坏包坏输入响亮 → 退出码 2)·
装配合并(同名替换)· **四桶差分**(新增拦截/新放行/无变化/**无法判定**)
· 逐题证据(触发资产名 / 表达式 / 变量实际取值)· 退出码三分支 ·
gate 消费面(metrics 直接喂 ``eval_gate``,同一份报告判"无回归")。

``_llm_forbidden`` 把「零 LLM」钉成机制:回放路径上任何一次 LLM 调用都会让
测试直接炸 —— 这不是纪律(靠人记得),是电路(靠 fixture 断)。
"""

from __future__ import annotations

import json

import pytest

from trove.services.extensions.dryrun import (
    Assembly,
    CorpusItem,
    GuardTier,
    Judgment,
)
from trove.services.extensions.impact import (
    BUCKETS,
    CandidateAsset,
    ImpactReport,
    ImpactRow,
    classify,
    diff_assembly,
    filter_candidates,
    load_candidates,
    merge_assembly,
)


@pytest.fixture(autouse=True)
def _llm_forbidden(monkeypatch):
    """零 LLM 硬门:回放路径上任何一次 LLM 调用 = 测试直接失败。"""
    from trove.llm.gateway import LLMGateway

    def _boom(*args, **kwargs):
        raise AssertionError("impact 路径不允许调用 LLM")

    monkeypatch.setattr(LLMGateway, "chat", _boom, raising=False)
    monkeypatch.setattr(LLMGateway, "chat_full", _boom, raising=False)
    monkeypatch.setattr(LLMGateway, "chat_stream", _boom, raising=False)


# ── helpers ──────────────────────────────────────────────


def _item(**kw) -> CorpusItem:
    base = dict(question="q", sql="SELECT 1", dialect="sqlite")
    base.update(kw)
    return CorpusItem(**base)


def _result_item(rows=None, columns=None, **kw) -> CorpusItem:
    return _item(
        rows=[[3]] if rows is None else rows,
        columns=["loan_count"] if columns is None else columns,
        **kw,
    )


def _guard_entry(name="no-naked-select", *, checks=None) -> dict:
    """真实读路径形状的 guard 条目(``read_skill`` 的投影)。"""
    return {
        "name": name, "tier": "guard", "status": "pending",
        "guard": {"targets": ["sql"], "checks": checks or [
            {"name": f"c-{name}", "severity": "blocking",
             "expr": "has_limit == 1", "reason": f"{name} 违反"}]},
    }


def _validator_entry(name="must-catch", *, expr="min >= 100",
                     columns=("loan_count",), severity="blocking") -> dict:
    """真实读路径形状的 validator 条目。"""
    return {
        "name": name, "tier": "validator", "status": "confirmed",
        "severity": severity, "targets": ["result"],
        "checks": [{"expr": expr, "columns": list(columns), "message": "违反"}],
    }


def _candidate(name, entry, *, source="pack:files/skills/x/SKILL.md",
               status="pending") -> CandidateAsset:
    return CandidateAsset(name=name, tier=str(entry.get("tier") or ""),
                          entry=entry, source=source, status=status)


def _runner_for(payload):
    """命中表由 payload 决定;``specs`` 空 = 没装 → 无命中(判定内核的约定)。"""
    def runner(specs, *, sql, dialect="", lang="zh"):
        if not specs:
            return []
        return list(payload)

    return runner


def _real_runner():
    """真实守卫判定(E2 的 ``run_guards``)—— 差分测试走真表达式,不摆假命中。"""
    from trove.services.skills.guards import run_guards

    def runner(specs, *, sql, dialect="", lang="zh"):
        return run_guards(specs, sql=sql, dialect=dialect, lang=lang)

    return runner


def _tier(runner, specs=None) -> GuardTier:
    return GuardTier(available=True, specs=list(specs or []), runner=runner)


def _report(rows, **kw) -> ImpactReport:
    base = dict(
        target="cand", datasource="demo", candidates=[_candidate(
            "x", _guard_entry("x"))],
        before={"validator": [], "guard": []},
        after={"validator": [], "guard": ["x"]},
        rows=list(rows),
    )
    base.update(kw)
    return ImpactReport(**base)


def _write_skill(root, name, text):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(text, encoding="utf-8")
    return d / "SKILL.md"


_GUARD_MD = """---
name: {name}
description: 一条守卫
tier: guard
status: {status}
guard:
  targets: [sql]
  checks:
    - name: {check}
      severity: blocking
      expr: has_limit == 1
      reason: 缺 LIMIT
      hint_zh: 加上 LIMIT
      hint_en: add a LIMIT
---

人类可读说明
"""

_VALIDATOR_MD = """---
name: {name}
description: 一条断言
tier: validator
status: {status}
severity: blocking
targets: [result]
checks:
  - expr: min >= 100
    columns: [loan_count]
    message: 违反
---

人类可读说明
"""


# ── 候选集加载(包 / 资产目录)─────────────────────────────


def test_load_candidates_from_plain_asset_dir(tmp_path):
    _write_skill(tmp_path / "assets", "no-naked-select",
                 _GUARD_MD.format(name="no-naked-select", status="pending",
                                  check="no-limit"))
    cands, errors, notes = load_candidates(tmp_path / "assets")
    assert errors == [] and len(cands) == 1
    c = cands[0]
    assert c.name == "no-naked-select" and c.tier == "guard"
    assert c.replayable and c.status == "pending"
    assert c.source.endswith("no-naked-select/SKILL.md")
    assert c.entry["guard"]["checks"][0]["expr"] == "has_limit == 1"


def test_load_candidates_from_pack_runs_tamper_check(tmp_path, monkeypatch):
    """包路径先过 ``read_pack`` 的逐文件 sha256 —— 改过的包回放的是别人改过的东西。"""
    from trove.services.extensions.pack import write_pack

    files = {"skills/no-naked-select/SKILL.md": _GUARD_MD.format(
        name="no-naked-select", status="pending", check="no-limit").encode()}
    write_pack(tmp_path / "p", name="demo-pack", files=files)
    cands, errors, notes = load_candidates(tmp_path / "p")
    assert errors == [] and len(cands) == 1
    assert any("sha256" in n or "校验" in n for n in notes)

    (tmp_path / "p" / "files" / "skills" / "no-naked-select" / "SKILL.md").write_text(
        "改过的内容", encoding="utf-8")
    cands, errors, notes = load_candidates(tmp_path / "p")
    assert cands == [] and len(errors) == 1
    assert "校验失败" in errors[0]


def test_load_candidates_missing_path_is_error(tmp_path):
    cands, errors, notes = load_candidates(tmp_path / "ghost")
    assert cands == [] and len(errors) == 1 and "不存在" in errors[0]


def test_load_candidates_file_path_is_error(tmp_path):
    p = tmp_path / "f.yml"
    p.write_text("x", encoding="utf-8")
    cands, errors, _ = load_candidates(p)
    assert cands == [] and "必须是目录" in errors[0]


def test_load_candidates_empty_target_is_error():
    cands, errors, _ = load_candidates("")
    assert cands == [] and "需要候选集路径" in errors[0]


def test_load_candidates_broken_skill_md_is_error(tmp_path):
    """frontmatter 读不出 = 连它属于哪一档都不知道 → 响亮(退出码 2)。"""
    root = tmp_path / "assets"
    _write_skill(root, "broken", "没有 frontmatter 的正文\n")
    cands, errors, _ = load_candidates(root)
    assert cands == [] and "无法解析" in errors[0]


def test_load_candidates_orphan_dir_is_noted(tmp_path):
    root = tmp_path / "assets"
    (root / "half").mkdir(parents=True)          # 没有 SKILL.md
    _write_skill(root, "ok", _GUARD_MD.format(
        name="ok", status="pending", check="c"))
    cands, errors, notes = load_candidates(root)
    assert errors == [] and [c.name for c in cands] == ["ok"]
    assert any("half" in n for n in notes)


def test_load_candidates_non_replayable_tier_is_noted(tmp_path):
    root = tmp_path / "assets"
    _write_skill(root, "method", """---
name: method
tier: available
status: pending
---

方法论
""")
    cands, errors, notes = load_candidates(root)
    assert errors == [] and len(cands) == 1 and not cands[0].replayable
    assert "不可回放" in notes[0] and "method" in notes[0]


def test_load_candidates_order_is_deterministic(tmp_path):
    root = tmp_path / "assets"
    for n in ("zeta", "alpha", "mid"):
        _write_skill(root, n, _GUARD_MD.format(
            name=n, status="pending", check=f"c-{n}"))
    names = [c.name for c in load_candidates(root)[0]]
    assert names == sorted(names)


# ── 装配合并(现状 → 现状+候选)────────────────────────────


def test_merge_same_name_replaces_in_place():
    base = Assembly(validator=[_validator_entry("keep"), _validator_entry("swap")])
    cand = _candidate("swap", _validator_entry("swap", expr="min >= 0"))
    merged = merge_assembly(base, [cand])
    assert [v["name"] for v in merged.validator] == ["keep", "swap"]
    assert merged.validator[1]["checks"][0]["expr"] == "min >= 0"
    assert base.validator[1]["checks"][0]["expr"] == "min >= 100"   # 不就地改


def test_merge_new_name_appends():
    base = Assembly(guard=[_guard_entry("a")], guard_tier=_tier(_runner_for([])))
    merged = merge_assembly(base, [_candidate("b", _guard_entry("b"))])
    assert [g["name"] for g in merged.guard] == ["a", "b"]


def test_merge_keeps_tiers_apart():
    base = Assembly(validator=[_validator_entry("v")], guard=[_guard_entry("g")],
                    guard_tier=_tier(_runner_for([])))
    merged = merge_assembly(base, [
        _candidate("v2", _validator_entry("v2")),
        _candidate("g2", _guard_entry("g2")),
    ])
    assert [v["name"] for v in merged.validator] == ["v", "v2"]
    assert [g["name"] for g in merged.guard] == ["g", "g2"]


def test_merge_inherits_guard_availability():
    """候选改变的是"装了什么",不是"引擎在不在" —— 不可用就整档不可用。"""
    base = Assembly(guard_tier=GuardTier(available=False, reason="guards_module_absent"))
    merged = merge_assembly(base, [_candidate("b", _guard_entry("b"))])
    assert merged.guard_available is False
    assert merged.guard_tier.reason == "guards_module_absent"


def test_merge_inherits_not_selected():
    base = Assembly(not_selected=["narrowed"])
    assert merge_assembly(base, []).not_selected == ["narrowed"]


def test_merge_skips_non_replayable_tiers():
    base = Assembly()
    merged = merge_assembly(base, [_candidate("m", {"name": "m", "tier": "available"})])
    assert merged.validator == [] and merged.guard == []


# ── 四桶差分 ─────────────────────────────────────────────


def test_install_blocking_guard_counts_newly_blocked_exactly():
    """验收口径:装必拦守卫 → 新增拦截计数正确(且只数该数的那条)。

    走**真实** ``run_guards``(表达式 + SQL 特征),命中与否由 SQL 决定 ——
    这正是验收里「装必拦守卫差分计数正确」的那条路。
    """
    items = [
        _item(question="q1", sql="SELECT a FROM t"),            # 无 LIMIT → 拦
        _item(question="q2", sql="SELECT a FROM t LIMIT 5"),    # 有 LIMIT → 放
    ]
    entry = _guard_entry("no-naked-select")
    before = Assembly(guard_tier=_tier(_real_runner()))
    after = merge_assembly(before, [_candidate("no-naked-select", entry)])
    rows = diff_assembly(items, before=before, after=after)
    guard_rows = [r for r in rows if r.tier == "guard"]
    assert [r.bucket for r in guard_rows] == ["newly_blocked", "unchanged"]
    assert guard_rows[0].newly_blocked_by == ("c-no-naked-select",)
    assert guard_rows[0].pre == "pass" and guard_rows[0].post == "violated"
    assert guard_rows[1].newly_blocked_by == ()
    assert guard_rows[0].evidence[0]["values"] == {"has_limit": 0}
    assert guard_rows[1].evidence == ()


def test_install_pass_only_guard_changes_nothing():
    items = [_result_item(question="q1"), _result_item(question="q2")]
    before = Assembly(guard_tier=_tier(_runner_for([
        {"name": "g", "verdict": True, "severity": "blocking"}])))
    after = merge_assembly(before, [_candidate("g", _guard_entry("g"))])
    rows = diff_assembly(items, before=before, after=after)
    assert {r.bucket for r in rows} == {"unchanged"}
    rep = _report(rows, candidates=[_candidate("g", _guard_entry("g"))])
    assert rep.exit_code == 0 and rep.counts()["newly_blocked"] == 0


def test_looser_candidate_guard_is_newly_released():
    """候选把守卫放宽(同名替换)→ 装前拦、装后不拦 = 新放行(门必须能红)。"""
    items = [_item(question="q1", sql="SELECT a FROM t")]
    strict = _guard_entry("no-naked-select", checks=[
        {"name": "c-no-naked-select", "severity": "blocking",
         "expr": "has_limit == 1", "reason": "缺 LIMIT"}])
    looser = _guard_entry("no-naked-select", checks=[
        {"name": "c-no-naked-select", "severity": "blocking",
         "expr": "has_limit >= 0", "reason": "随便"}])
    before = Assembly(guard=[strict], guard_tier=_tier(_real_runner(), [strict]))
    after = merge_assembly(before, [_candidate("no-naked-select", looser)])
    rows = [r for r in diff_assembly(items, before=before, after=after)
            if r.tier == "guard"]
    assert rows[0].pre == "violated" and rows[0].post == "pass"
    assert rows[0].bucket == "newly_released"
    assert rows[0].newly_released_by == ("c-no-naked-select",)
    assert _report(rows).exit_code == 1


def test_looser_candidate_validator_is_newly_released():
    items = [_result_item(rows=[[3]])]
    before = Assembly(validator=[_validator_entry("must-catch", expr="min >= 100")])
    after = merge_assembly(before, [_candidate(
        "must-catch", _validator_entry("must-catch", expr="min >= 0"))])
    rows = [r for r in diff_assembly(items, before=before, after=after)
            if r.tier == "validator"]
    assert rows[0].pre == "violated" and rows[0].post == "pass"
    assert rows[0].bucket == "newly_released"


def test_missing_result_rows_is_undecided_not_unchanged():
    """诚实边界:validator 档判不了 → **无法判定**,绝不许默默算成「无变化」。"""
    before = Assembly(guard_tier=_tier(_runner_for([])))
    after = merge_assembly(before, [_candidate("g", _guard_entry("g"))])
    rows = diff_assembly([_item(question="q1", sql="", rows=None, columns=None)],
                         before=before, after=after)
    val = [r for r in rows if r.tier == "validator"][0]
    assert val.bucket == "undecided" and val.state == "skipped"
    assert val.reason == "no_result_rows"


def test_unjudged_one_side_is_undecided():
    """一侧 unjudged(判不了)不是"没变化" —— 三值内核的 None 在这里有归宿。"""
    j = Judgment(state="covered", pre="pass", post="unjudged")
    bucket, why = classify(j)
    assert bucket == "undecided" and why == "unjudged"


def test_runner_error_is_undecided_and_exit_2():
    def boom(specs, *, sql, dialect="", lang="zh"):
        if specs:
            raise RuntimeError("crash")
        return []

    before = Assembly(guard_tier=_tier(boom))
    after = merge_assembly(before, [_candidate("g", _guard_entry("g"))])
    rows = [r for r in diff_assembly([_item()], before=before, after=after)
            if r.tier == "guard"]
    assert rows[0].bucket == "undecided" and rows[0].state == "errored"
    assert rows[0].reason == "runner_error"
    assert _report(rows).exit_code == 2


def test_guard_tier_unavailable_is_skipped_loudly():
    """guard 档不可用 → 整档 skipped(附理由码),不是"没有差异"。"""
    before = Assembly(guard_tier=GuardTier(available=False, reason="guards_module_absent"))
    after = merge_assembly(before, [_candidate("g", _guard_entry("g"))])
    rows = [r for r in diff_assembly([_item()], before=before, after=after)
            if r.tier == "guard"]
    assert rows[0].state == "skipped" and rows[0].reason == "guards_module_absent"
    assert rows[0].bucket == "undecided"


def test_every_item_times_every_tier_gets_a_row():
    items = [_item(question="q1"), _item(question="q2"), _item(question="q3")]
    before = Assembly(guard_tier=_tier(_runner_for([])))
    after = merge_assembly(before, [_candidate("g", _guard_entry("g"))])
    rows = diff_assembly(items, before=before, after=after)
    assert len(rows) == 6
    assert [(r.index, r.tier) for r in rows] == [
        (0, "validator"), (0, "guard"),
        (1, "validator"), (1, "guard"),
        (2, "validator"), (2, "guard"),
    ]


def test_passing_hits_are_not_evidence():
    before = Assembly(guard_tier=_tier(_runner_for([
        {"name": "g", "verdict": True, "severity": "blocking"}])))
    after = merge_assembly(before, [_candidate("g", _guard_entry("g"))])
    rows = [r for r in diff_assembly([_item()], before=before, after=after)
            if r.tier == "guard"]
    assert rows[0].bucket == "unchanged" and rows[0].evidence == ()


# ── 逐题证据(触发资产名 / 表达式 / 变量实际取值)──────────


def test_evidence_carries_guard_name_expression_and_values():
    """每条数字都指得回原始行:守卫名 + 表达式 + **变量实际取值**。"""
    items = [_item(question="q1", sql="SELECT a FROM t")]
    before = Assembly(guard_tier=_tier(_runner_for([
        {"name": "no-limit", "verdict": False, "severity": "blocking"}])))
    after = merge_assembly(before, [_candidate("no-naked-select", _guard_entry(
        "no-naked-select", checks=[{"name": "no-limit", "severity": "blocking",
                                    "expr": "has_limit == 1", "reason": "缺 LIMIT"}]))])
    rows = [r for r in diff_assembly(items, before=before, after=after)
            if r.tier == "guard"]
    ev = rows[0].evidence
    assert len(ev) == 1
    e = ev[0]
    assert e["side"] == "post" and e["tier"] == "guard"
    assert e["name"] == "no-limit" and e["asset"] == "no-naked-select"
    assert e["expr"] == "has_limit == 1"
    assert e["verdict"] is False
    assert e["values"] == {"has_limit": 0}          # 与内核同一份作用域
    assert rows[0].to_dict()["evidence"][0]["values"] == {"has_limit": 0}


def test_evidence_lists_only_referenced_variables():
    items = [_item(sql="SELECT a FROM t LIMIT 3")]
    before = Assembly(guard_tier=_tier(_runner_for([
        {"name": "join-heavy", "verdict": False, "severity": "advisory"}])))
    after = merge_assembly(before, [_candidate("x", _guard_entry("x", checks=[
        {"name": "join-heavy", "severity": "advisory",
         "expr": "table_count >= 1", "reason": "多表"}]))])
    rows = [r for r in diff_assembly(items, before=before, after=after)
            if r.tier == "guard"]
    assert set(rows[0].evidence[0]["values"]) == {"table_count"}


def test_evidence_carries_validator_scope_values():
    items = [_result_item(rows=[[3], [7]], columns=["loan_count"])]
    before = Assembly()
    after = merge_assembly(before, [_candidate(
        "must-catch", _validator_entry("must-catch", expr="min >= 100"))])
    rows = [r for r in diff_assembly(items, before=before, after=after)
            if r.tier == "validator"]
    ev = rows[0].evidence
    assert len(ev) == 1
    assert ev[0]["expr"] == "min >= 100" and ev[0]["values"] == {"min": 3.0}
    assert ev[0]["side"] == "post" and ev[0]["asset"] == "must-catch"


def test_evidence_unknown_variable_is_explicit():
    """缺列 → 值域里是 UNKNOWN —— 证据里必须是 ``"unknown"``,不是 null/0。"""
    items = [_result_item(rows=[[3]], columns=["other_col"])]
    before = Assembly()
    after = merge_assembly(before, [_candidate(
        "must-catch", _validator_entry("must-catch", expr="min >= 100"))])
    rows = [r for r in diff_assembly(items, before=before, after=after)
            if r.tier == "validator"]
    assert rows[0].bucket == "undecided"        # 判不了,进第四桶
    assert rows[0].evidence[0]["values"] == {"min": "unknown"}
    json.dumps(rows[0].to_dict())               # JSON 安全


def test_evidence_for_both_sides_is_labelled():
    """新放行场景两侧都有证据 —— side 字段区分装前/装后。"""
    items = [_result_item(rows=[[3]])]
    before = Assembly(validator=[_validator_entry("must-catch", expr="min >= 100")])
    after = merge_assembly(before, [_candidate(
        "must-catch", _validator_entry("must-catch", expr="min >= 200"))])
    rows = [r for r in diff_assembly(items, before=before, after=after)
            if r.tier == "validator"]
    assert {e["side"] for e in rows[0].evidence} == {"pre", "post"}


def test_evidence_is_deterministically_ordered():
    items = [_item()]
    before = Assembly(guard_tier=_tier(_runner_for([
        {"name": "b", "verdict": False, "severity": "blocking"},
        {"name": "a", "verdict": None, "severity": "advisory"},
    ])))
    after = merge_assembly(before, [_candidate("x", _guard_entry("x", checks=[
        {"name": "b", "severity": "blocking", "expr": "has_limit == 1",
         "reason": "b"},
        {"name": "a", "severity": "advisory", "expr": "has_limit == 1",
         "reason": "a"}]))])
    rows = [r for r in diff_assembly(items, before=before, after=after)
            if r.tier == "guard"]
    assert [e["name"] for e in rows[0].evidence] == ["a", "b"]


# ── 触发筛(候选只筛可知维度)──────────────────────────────


def test_filter_candidates_drops_other_datasource():
    other = _guard_entry("scoped")
    other["triggers"] = {"datasource": "other"}
    kept, notes = filter_candidates([_candidate("scoped", other)], "demo")
    assert kept == [] and "不适用于数据源 demo" in notes[0]


def test_filter_candidates_keeps_matching_datasource():
    scoped = _guard_entry("scoped")
    scoped["triggers"] = {"datasource": "demo"}
    kept, notes = filter_candidates([_candidate("scoped", scoped)], "demo")
    assert [c.name for c in kept] == ["scoped"] and notes == []


def test_filter_candidates_notes_unknown_dims_and_keeps():
    """回放时不可知的维度(lang/role/…)不收窄,但**必须说出来**。"""
    trig = _guard_entry("lang-scoped")
    trig["triggers"] = {"lang": "en", "datasource": "demo"}
    kept, notes = filter_candidates([_candidate("lang-scoped", trig)], "demo")
    assert [c.name for c in kept] == ["lang-scoped"]
    assert any("不可知" in n and "lang" in n for n in notes)


def test_filter_candidates_without_datasource_keeps_all():
    kept, notes = filter_candidates([_candidate("x", _guard_entry("x"))], "")
    assert [c.name for c in kept] == ["x"] and notes == []


# ── 退出码三分支 ─────────────────────────────────────────


def _covered(buckets):
    """桶名 → 判定行(直接构行,便于把退出码/metrics 场景摆到最简)。"""
    out = []
    for i, b in enumerate(buckets):
        if b == "undecided":       # 无法判定行 = 跳过态(双态至少一侧没判成)
            out.append(ImpactRow(
                index=i, question_id="abcd1234", source="fixtures:x", tier="guard",
                state="skipped", reason="no_sql", bucket="undecided"))
            continue
        out.append(ImpactRow(
            index=i, question_id="abcd1234", source="fixtures:x", tier="guard",
            state="covered",
            pre="violated" if b == "newly_released" else "pass",
            post="violated" if b == "newly_blocked" else "pass",
            pre_blocked_by=("g",) if b == "newly_released" else (),
            post_blocked_by=("g",) if b == "newly_blocked" else (),
            bucket=b))
    return out


def test_exit_1_on_newly_blocked():
    assert _report(_covered(["newly_blocked"])).exit_code == 1


def test_exit_1_on_newly_released():
    assert _report(_covered(["newly_released"])).exit_code == 1


def test_exit_0_when_nothing_changed():
    assert _report(_covered(["unchanged"])).exit_code == 0


def test_exit_2_when_no_candidates():
    """「没有差异」不许等于「没有验」:没有可回放的候选 → 2,不是 0。"""
    rep = _report(_covered(["unchanged"]), candidates=[])
    assert rep.exit_code == 2
    assert "候选集为空" in rep.render()


def test_exit_2_when_errors_present():
    rep = _report(_covered(["unchanged"]), errors=["候选集路径不存在: x"])
    assert rep.exit_code == 2


def test_exit_2_when_no_rows():
    assert _report([], errors=[]).exit_code == 2


def test_exit_2_when_nothing_was_actually_judged():
    """全 skipped → 一条都没真判过 = 无法回放(绝不显示为 0)。"""
    before = Assembly(guard_tier=_tier(_runner_for([])))
    after = merge_assembly(before, [_candidate("g", _guard_entry("g"))])
    rows = diff_assembly([_item(question="q", sql="", rows=None, columns=None)],
                         before=before, after=after)
    rep = _report(rows)
    assert rep.decided == 0 and rep.exit_code == 2
    assert "一条判定都没真判过" in rep.render()


def test_exit_2_on_runner_error():
    def boom(specs, *, sql, dialect="", lang="zh"):
        raise RuntimeError("x")

    rows = diff_assembly([_item()], before=Assembly(guard_tier=_tier(boom)),
                         after=Assembly(guard_tier=_tier(boom)))
    assert _report(rows).exit_code == 2


# ── metrics(gate 消费面)────────────────────────────────────


def test_metrics_keys_and_directions():
    """指标名里带方向关键词:``fail`` = 更低更好(``gate._direction_of``)。"""
    from trove.eval.gate import _direction_of

    m = _report(_covered(["newly_blocked", "unchanged"])).metrics
    assert set(m) >= {"coverage", "blocking_fail_rate", "newly_blocked_fail_rate",
                      "newly_released_fail_rate", "unjudged_fail_rate",
                      "n", "n_judged"}
    for name in ("blocking_fail_rate", "newly_blocked_fail_rate",
                 "newly_released_fail_rate", "unjudged_fail_rate"):
        assert _direction_of(name) == "lower"
    assert _direction_of("coverage") == "higher"


def test_metrics_values_track_counts():
    rep = _report(_covered(["newly_blocked", "unchanged", "unchanged", "unchanged"]))
    m = rep.metrics
    assert m["n"] == 4 and m["n_judged"] == 4
    assert m["coverage"] == 1.0
    assert m["newly_blocked_fail_rate"] == 0.25
    assert m["newly_released_fail_rate"] == 0.0
    assert m["blocking_fail_rate"] == 0.25
    assert m["unjudged_fail_rate"] == 0.0


def test_gate_reports_no_regression_for_identical_reports():
    """验收口径(过 gate **无误报**):同一份报告 → 一个指标都不回归。"""
    from trove.eval.gate import compare_metrics

    m = _report(_covered(["newly_blocked", "unchanged"])).metrics
    verdict = compare_metrics(m, dict(m))
    assert [r.metric for r in verdict.metrics if not r.ok] == []
    assert verdict.metrics                                  # 确实比过(不是空比)


def test_gate_flags_worse_impact():
    """门必须能红:拦截率上升 = 回归(否则报告过门是假绿)。"""
    from trove.eval.gate import compare_metrics

    good = _report(_covered(["unchanged", "unchanged", "unchanged", "unchanged"]))
    bad = _report(_covered(["newly_blocked", "newly_blocked", "unchanged", "unchanged"]))
    verdict = compare_metrics(good.metrics, bad.metrics)
    regressed = {r.metric for r in verdict.metrics if not r.ok}
    assert {"newly_blocked_fail_rate", "blocking_fail_rate"} <= regressed


# ── 隐私(R5)与确定性 ─────────────────────────────────────


def test_question_hashed_by_default():
    rep = _report(_covered(["unchanged"]))
    data = rep.to_dict()
    assert "question" not in data["items"][0]
    assert data["include_questions"] is False
    assert data["items"][0]["question_id"]                  # 短码在(能对上号)


def test_question_shown_only_with_flag():
    items = [_item(question="How many loans?"),
             CorpusItem(question="How many loans?", sql="SELECT 1", dialect="sqlite")]
    before = Assembly(guard_tier=_tier(_runner_for([])))
    after = merge_assembly(before, [_candidate("g", _guard_entry("g"))])
    masked = diff_assembly(items, before=before, after=after)
    shown = diff_assembly(items, before=before, after=after, include_questions=True)
    assert masked[0].question == "" and shown[0].question == "How many loans?"
    assert "How many loans?" not in str(_report(masked).to_dict())
    assert "How many loans?" in _report(shown).render()
    # 短码两侧一致(默认出的是它,两份报告的问题能对上)
    assert masked[0].question_id == shown[0].question_id
    assert len(masked[0].question_id) == 8


def test_diff_is_byte_stable_across_calls():
    """同输入两次 → to_dict 逐字节一致(无时间戳/无集合迭代序)。"""
    items = [_item(question="q1"), _item(question="q2")]
    before = Assembly(guard_tier=_tier(_runner_for([
        {"name": "g", "verdict": False, "severity": "blocking"}])))
    after = merge_assembly(before, [_candidate("g", _guard_entry("g"))])
    a = [r.to_dict() for r in diff_assembly(items, before=before, after=after)]
    b = [r.to_dict() for r in diff_assembly(items, before=before, after=after)]
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_report_render_and_dict_agree_on_buckets():
    rep = _report(_covered(["newly_blocked", "newly_released", "undecided"]))
    text = rep.render()
    for bucket in BUCKETS:
        assert f"{rep.counts()[bucket]}" in text
    assert "退出码 1" in text
    assert rep.to_dict()["counts"] == rep.counts()


def test_report_renders_all_four_buckets():
    rep = _report(_covered(["newly_blocked", "newly_released", "unchanged",
                            "undecided"]))
    text = rep.render()
    for label in ("新增拦截 1", "新放行 1", "无变化 1", "无法判定 1"):
        assert label in text
