"""ExtensionEnvelope —— 推导闭包 / unresolved 响亮 / 缓存键含名(零 LLM)。

信封的全部价值是「信封说的 = 实际有的」,所以这里的断言都对着**推导**:
capabilities 不是读声明,是从 check 表达式的 identifiers 闭包 ∩ 合法域算
出来的;算不出来的引用必须进 unresolved,不许静默省略。
"""

from __future__ import annotations

import itertools

import pytest

from trove.services.decision.expr import (
    VARIABLES as DECISION_VARIABLES,
)
from trove.services.extensions import (
    DOMAIN_VERSIONS,
    Capabilities,
    MountCatalog,
    build_decision_envelope,
    build_preset_envelope,
    build_skill_envelope,
    clear_cache,
    collect_assets,
    derive_check_capabilities,
    digest_files,
)
from trove.services.skills.validators import (
    VALIDATOR_HOST,
    VALIDATOR_VARIABLES,
)

#: 测试用的静态挂点目录(不依赖 validate 的真名单 —— 推导规则本身要
#: 能在任意名单上跑,名单↔运行时的对账是 validate 那边的事)。
_CATALOG = MountCatalog(
    inject_nodes=("query_sketch", "gen_sql"),
    ad_nodes=("gen_sql",),
    validator_host=VALIDATOR_HOST,
)


@pytest.fixture(autouse=True)
def _clear_envelope_cache():
    """进程级缓存归零 —— 一个测试造的缓存不许泄给下一个。"""
    clear_cache()
    yield
    clear_cache()


# ── derive_check_capabilities:推导闭包 ────────────────────


def test_variables_are_derived_from_expression_closure():
    caps, unresolved = derive_check_capabilities(
        [{"expr": "min >= 0 and max <= 100", "severity": "blocking"}],
        VALIDATOR_VARIABLES)
    assert caps.variables == frozenset({"min", "max"})
    assert caps.effects == frozenset({"takeover"})   # blocking → takeover
    assert unresolved == ()


def test_advisory_maps_to_watch():
    caps, unresolved = derive_check_capabilities(
        [{"expr": "null_count == 0", "severity": "advisory"}],
        VALIDATOR_VARIABLES)
    assert caps.effects == frozenset({"watch"})
    assert unresolved == ()


def test_domain_external_identifier_is_loud():
    """域外变量(手写文件绕过写入面)→ unresolved,不静默丢。"""
    caps, unresolved = derive_check_capabilities(
        [{"expr": "bogus_var > 0"}], VALIDATOR_VARIABLES)
    assert caps.variables == frozenset()
    assert len(unresolved) == 1 and "bogus_var" in unresolved[0]


def test_severity_outside_vocab_is_loud():
    caps, unresolved = derive_check_capabilities(
        [{"expr": "min >= 0", "severity": "fatal"}], VALIDATOR_VARIABLES)
    assert caps.effects == frozenset()
    assert len(unresolved) == 1 and "fatal" in unresolved[0]


def test_malformed_checks_are_loud():
    _, unresolved = derive_check_capabilities(
        ["not-a-mapping", {"expr": ""}, {"severity": "blocking"}],
        VALIDATOR_VARIABLES)
    assert len(unresolved) == 3


def test_decision_domain_reads_current_baseline():
    """决策域与结果域是两套变量 —— 用错域即 unresolved。"""
    caps, unresolved = derive_check_capabilities(
        [{"expr": "current > baseline"}], DECISION_VARIABLES)
    assert caps.variables == frozenset({"current", "baseline"})
    assert unresolved == ()
    _, bad = derive_check_capabilities(
        [{"expr": "current > baseline"}], VALIDATOR_VARIABLES)
    assert bad  # current/baseline 不在结果域


# ── build_skill_envelope:挂点与档位 ───────────────────────


_SEQ = itertools.count()


def _skill(**over):
    # 名字必须唯一:缓存键 =(名, 文件摘要, 域版本),空 files 的摘要恒等 ——
    # 同名会合法命中上一个测试**同名但不同内容**的信封。
    entry = {"name": f"s{next(_SEQ)}", "tier": "required",
             "status": "confirmed"}
    entry.update(over)
    return entry


def test_required_without_declared_node_expands_to_catalog():
    env = build_skill_envelope(
        _skill(), files=[], source="org", catalog=_CATALOG)
    assert [(m.node, m.tier, m.effect) for m in env.mounts] == [
        ("query_sketch", "required", "inject"),
        ("gen_sql", "required", "inject"),
    ]
    assert env.capabilities == Capabilities(
        effects=frozenset({"inject"}), targets=frozenset({"prompt"}))
    assert env.unresolved == ()


def test_required_with_unknown_node_is_loud():
    env = build_skill_envelope(
        _skill(triggers={"node": "ghost"}), files=[], source="org",
        catalog=_CATALOG)
    assert env.mounts == ()
    assert len(env.unresolved) == 1 and "ghost" in env.unresolved[0]


def test_available_only_mounts_on_ad_node():
    ok = build_skill_envelope(
        _skill(tier="available", triggers={"node": "gen_sql"}),
        files=[], source="org", catalog=_CATALOG)
    assert [(m.node, m.effect) for m in ok.mounts] == [("gen_sql", "advertise")]
    bad = build_skill_envelope(
        _skill(tier="available", triggers={"node": "query_sketch"}),
        files=[], source="org", catalog=_CATALOG)
    assert bad.mounts == () and bad.unresolved


def test_validator_tier_mounts_on_host_with_severity_effect():
    env = build_skill_envelope(
        _skill(tier="validator", checks=[
            {"expr": "min >= 0", "severity": "blocking"}]),
        files=[], source="org", catalog=_CATALOG)
    assert [(m.node, m.tier, m.effect) for m in env.mounts] == [
        (VALIDATOR_HOST, "validator", "takeover")]
    assert env.capabilities.variables == frozenset({"min"})
    assert env.capabilities.targets == frozenset({"result"})


def test_validator_declaring_other_node_is_loud():
    env = build_skill_envelope(
        _skill(tier="validator", triggers={"node": "gen_sql"},
               checks=[{"expr": "min >= 0"}]),
        files=[], source="org", catalog=_CATALOG)
    assert env.unresolved and "结果断言只在宿主节点运行" in env.unresolved[0]


def test_tier_outside_vocab_is_loud():
    env = build_skill_envelope(
        _skill(tier="superpower"), files=[], source="org", catalog=_CATALOG)
    assert env.unresolved and "superpower" in env.unresolved[0]


def test_read_error_entry_degrades_with_error_in_unresolved():
    env = build_skill_envelope(
        _skill(error="frontmatter 缺 description"), files=[], source="org",
        catalog=_CATALOG)
    assert env.unresolved == ("frontmatter 缺 description",)


# ── build_decision_envelope ──────────────────────────────


class _Rule:
    def __init__(self, **over):
        self.id = "r1"
        self.conditions = ["delta_pct > 20"]
        self.action = None
        self.enabled = True
        for k, v in over.items():
            setattr(self, k, v)


def test_decision_effect_follows_action():
    watch = build_decision_envelope(_Rule(), files=[], datasource="demo")
    assert watch.state == "confirmed"
    assert [(m.node, m.tier, m.effect) for m in watch.mounts] == [
        ("decisions", "scheduled", "watch")]
    # 不同 id = 不同规则(同 id 会合法命中上面的缓存)。
    takeover = build_decision_envelope(
        _Rule(id="r2", action={"template": "notify"}), files=[],
        datasource="demo")
    assert takeover.mounts[0].effect == "takeover"
    assert takeover.source == "kb:demo"


def test_decision_disabled_state():
    env = build_decision_envelope(
        _Rule(enabled=False), files=[], datasource="demo")
    assert env.state == "disabled"


def test_decision_bad_condition_is_loud():
    env = build_decision_envelope(
        _Rule(conditions=["current >> baseline"]), files=[], datasource="demo")
    assert env.unresolved and env.unresolved[0].startswith("conditions[0]")


# ── build_preset_envelope ────────────────────────────────


class _Preset:
    def __init__(self, **over):
        self.name = "p1"
        self.source = "builtin"
        self.path = ""
        self.skills = []
        self.decisions = []
        self.domains = []
        self.semantics = []
        self.presentation = []
        for k, v in over.items():
            setattr(self, k, v)


def test_preset_mounts_and_item_caps_union():
    env = build_preset_envelope(_Preset(
        skills=[{"name": "draft-skill", "tier": "required"}],
        decisions=[{"name": "draft-rule", "conditions": ["delta > 1"]}],
        domains=[{"name": "d"}],
    ))
    assert [(m.node, m.tier) for m in env.mounts] == [
        ("skills", "draft"), ("decisions", "draft"), ("semantics", "draft")]
    assert env.capabilities.effects == frozenset({"inject", "watch"})
    assert env.capabilities.variables == frozenset({"delta"})
    assert env.unresolved == ()


def test_preset_empty_item_stays_clean():
    env = build_preset_envelope(_Preset())
    assert env.mounts == () and env.unresolved == ()


# ── 缓存键:名必须进键 ────────────────────────────────────


def test_cache_does_not_cross_contaminate_assets_sharing_a_file(tmp_path):
    """三个 code skill 同住一份 manifest —— 缓存只按文件摘要会互相顶掉。"""
    manifest = tmp_path / "manifest.yml"
    manifest.write_text("skills: [a, b, c]\n", encoding="utf-8")
    a = build_skill_envelope(
        _skill(name="a"), files=[manifest], source="code", catalog=_CATALOG)
    b = build_skill_envelope(
        _skill(name="b", tier="validator"), files=[manifest], source="code",
        catalog=_CATALOG)
    assert a.name == "a" and b.name == "b"
    assert a.capabilities.effects == frozenset({"inject"})
    assert b.capabilities.effects == frozenset()   # validator 无 checks


def test_cache_hits_on_identical_input(tmp_path):
    f = tmp_path / "SKILL.md"
    f.write_text("v1", encoding="utf-8")
    entry = _skill(name="hit")
    first = build_skill_envelope(
        entry, files=[f], source="org", catalog=_CATALOG)
    again = build_skill_envelope(
        entry, files=[f], source="org", catalog=_CATALOG)
    assert first is again


def test_cache_misses_when_file_content_changes(tmp_path):
    f = tmp_path / "SKILL.md"
    f.write_text("v1", encoding="utf-8")
    first = build_skill_envelope(
        _skill(), files=[f], source="org", catalog=_CATALOG)
    f.write_text("v2", encoding="utf-8")
    second = build_skill_envelope(
        _skill(), files=[f], source="org", catalog=_CATALOG)
    assert first is not second
    assert first.provenance.sha256 != second.provenance.sha256


def test_cache_key_carries_domain_version():
    """域版本进键 —— 词表变更 bump 后旧缓存不得复用(以键的形状钉住)。"""
    assert DOMAIN_VERSIONS["validator"] and DOMAIN_VERSIONS["decision"]
    caps = Capabilities(variables=frozenset({"min"}))
    assert caps.to_dict() == {
        "variables": ["min"], "effects": [], "targets": []}


def test_digest_files_is_path_and_content_sensitive(tmp_path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("same", encoding="utf-8")
    b.write_text("same", encoding="utf-8")
    assert digest_files([a]) != digest_files([b])          # 路径进摘要
    da = digest_files([a])
    a.write_text("changed", encoding="utf-8")
    assert digest_files([a]) != da                          # 内容进摘要
    assert digest_files([]) == ""


# ── collect_assets:只读聚合 ──────────────────────────────


def test_collect_assets_on_empty_project_reads_code_and_presets(tmp_path):
    """空项目 ≠ 空信封:code skills 与内置 preset 随包存在。"""
    envs = collect_assets(tmp_path)
    kinds = {e.kind for e in envs}
    assert "skill" in kinds and "preset" in kinds
    for e in envs:
        if e.kind == "skill" and e.source == "code":
            assert e.state == "confirmed"


def test_collect_assets_org_skill(tmp_path):
    d = tmp_path / ".trove" / "skills" / "no-neg"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: no-neg\ndescription: No negative values.\n"
        "tier: validator\nstatus: pending\ntargets: [result]\n"
        "triggers: {node: validate}\n"
        "checks:\n  - expr: min >= 0\n    severity: blocking\n---\n\n正文\n",
        encoding="utf-8")
    envs = collect_assets(tmp_path)
    org = next(e for e in envs if e.source == "org")
    assert org.name == "no-neg"
    assert org.state == "pending"        # 草稿不进任何面(确认门)
    assert org.mounts and org.mounts[0].node == VALIDATOR_HOST
    assert org.mounts[0].effect == "takeover"


def test_collect_assets_org_skill_disabled_state_passthrough(tmp_path):
    """E6 颗粒停用:信封照列(治理声明面),state 原样透出 disabled ——
    挂点照推导(它写的就是确认后的去向),摘除只发生在运行时装配清单。"""
    d = tmp_path / ".trove" / "skills" / "muted"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: muted\ndescription: muted skill.\n"
        "tier: required\nstatus: disabled\ntriggers: {node: gen_sql}\n---\n\n正文\n",
        encoding="utf-8")
    envs = collect_assets(tmp_path)
    org = next(e for e in envs if e.source == "org")
    assert org.name == "muted"
    assert org.state == "disabled"
    assert [(m.node, m.tier, m.effect) for m in org.mounts] == [
        ("gen_sql", "required", "inject")]


def test_collect_assets_decision_from_kb(tmp_path):
    import yaml

    ds = tmp_path / ".trove" / "kb" / "demo"
    ds.mkdir(parents=True)
    (ds / "decisions.yml").write_text(yaml.safe_dump({
        "version": 1,
        "rules": [{
            "id": "high-delta",
            "name": "delta above 20%",
            "severity": "warning",
            "subject": {"metrics": ["total_amount"]},
            "baseline": {"kind": "literal", "value": 1.0},
            "conditions": ["delta_pct > 20"],
        }],
    }, allow_unicode=True), encoding="utf-8")
    envs = collect_assets(tmp_path)
    dec = next(e for e in envs if e.kind == "decision")
    assert dec.name == "high-delta" and dec.source == "kb:demo"
    assert dec.capabilities.variables == frozenset({"delta_pct"})


def test_assets_sorted_stable(tmp_path):
    envs = collect_assets(tmp_path)
    keys = [(e.kind, e.name, e.source) for e in envs]
    assert keys == sorted(keys)


def test_to_dict_json_roundtrip(tmp_path):
    import json

    envs = collect_assets(tmp_path)
    for e in envs:
        d = e.to_dict()
        assert json.loads(json.dumps(d, ensure_ascii=False)) == d
        assert set(d) == {"kind", "name", "source", "state", "mounts",
                          "capabilities", "provenance", "unresolved"}


def test_provenance_files_recorded(tmp_path):
    f = tmp_path / "SKILL.md"
    f.write_text("x", encoding="utf-8")
    env = build_skill_envelope(
        _skill(), files=[f], source="org", catalog=_CATALOG)
    assert env.provenance.files == (str(f),)
    assert len(env.provenance.sha256) == 64


def test_mount_catalog_from_validate_matches_constants():
    from trove.services.validate.service import SKILL_NODES, AVAILABLE_AD_NODES

    cat = MountCatalog.from_validate()
    assert cat.inject_nodes == tuple(SKILL_NODES)
    assert cat.ad_nodes == tuple(AVAILABLE_AD_NODES)
    assert cat.validator_host == VALIDATOR_HOST


def test_path_objects_and_strings_digest_identically(tmp_path):
    f = tmp_path / "SKILL.md"
    f.write_text("x", encoding="utf-8")
    assert digest_files([f]) == digest_files([str(f)])
