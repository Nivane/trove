"""``PresetService.export_pack / import_pack`` —— 包与来源的资产策略。

三条验收主线(实施稿 §04 E5 / §07):

1. **往返保真**:导出物 = 文件原样;导入后(源本已 pending 时)逐字节相同。
2. **导入一律 pending**:技能不在投递面(``confirmed_only`` 看不见)、决策
   只落 ``decision_drafts.yml``(``decisions.yml`` 里没有)、预设落盘但不被
   消费 —— 自动资产不绕管理员确认门(红线回归门)。
3. **冲撞保守**:默认拒载点名(什么都不写)、``--force`` 覆盖为 pending 形态、
   生效规则永不被覆盖、幂等重放报 skipped。

零 LLM / 零网络:纯 tmp 文件系统。
"""

from __future__ import annotations

import pytest
import yaml

from trove.services.decision.drafts import DecisionDraftStore
from trove.services.extensions.pack import (
    FILES_DIR,
    MANIFEST_FILE,
    PackError,
    PackSchemaTooNew,
    PackTampered,
    read_pack,
    write_pack,
)
from trove.services.kb.service import KbService
from trove.services.presets.service import PresetService
from trove.services.skills.service import SkillService

SKILL_TEXT = (
    "---\n"
    "name: loop-x\n"
    "description: Loop discipline.\n"
    "triggers:\n  node: gen_sql\n"
    "tier: required\n"
    "status: pending\n"
    "source: admin\n"
    "---\n"
    "\n"
    "1. Step one.\n"
    "2. Step two.\n"
)

RULE = {
    "version": 1,
    "rules": [{
        "id": "loan-high",
        "name": "loan total above baseline",
        "severity": "warning",
        "subject": {"metrics": ["total_amount"]},
        "baseline": {"kind": "literal", "value": 100000.0},
        "conditions": ["delta > 0"],
    }],
}

PRESET = {"name": "starter", "description": "接入模板",
          "skills": ["loop-x"]}


def _services(root, *, builtin_root=None):
    kb = KbService(root, git_kb=False)
    skills = SkillService(root / ".trove" / "skills", git_enabled=False)
    presets = PresetService(root / ".trove" / "presets", kb=kb, skills=skills,
                            builtin_root=builtin_root, git_enabled=False)
    return kb, skills, presets


def _write_skill(root, name="loop-x", text=SKILL_TEXT, override=None):
    d = root / ".trove" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(text, encoding="utf-8")
    if override is not None:
        (d / f"SKILL.{override[0]}.md").write_text(override[1], encoding="utf-8")
    return d


def _write_decisions(root, datasource="mini", data=RULE):
    p = root / ".trove" / "kb" / datasource / "decisions.yml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                 encoding="utf-8")
    return p


def _write_preset(root, name="starter", data=PRESET):
    p = root / ".trove" / "presets" / name / "preset.yml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                 encoding="utf-8")
    return p


def _project(tmp_path, name="proj"):
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    return root


# ── 导出:收什么,不收什么 ────────────────────────────────


def test_export_collects_org_assets_verbatim(tmp_path):
    """导出物 = 文件原样(逐字节);技能覆盖文件一并进包。"""
    root = _project(tmp_path)
    _write_skill(root, override=("zh", "中文覆盖\n"))
    _write_decisions(root)
    _write_preset(root)
    _kb, _skills, presets = _services(root)

    dest = tmp_path / "pack"
    report = presets.export_pack("demo-pack", dest)
    assert report.action == "export" and report.refused == 0
    loaded = read_pack(dest)
    assert set(loaded.files) == {
        "skills/loop-x/SKILL.md", "skills/loop-x/SKILL.zh.md",
        "kb/mini/decisions.yml", "presets/starter/preset.yml",
    }
    for rel in loaded.files:
        assert loaded.files[rel] == _flat(root, rel)
    assert loaded.manifest.kinds == ("decisions", "presets", "skills")
    assert sorted(i.item for i in report.items) == ["loop-x", "mini", "starter"]


def _flat(root, rel: str) -> bytes:
    """包的逻辑路径 → 源工程里的真实路径(原样读)。"""
    base = {"skills": "skills", "kb": "kb", "presets": "presets"}
    head, _, tail = rel.partition("/")
    return (root / ".trove" / base[head] / tail).read_bytes()


def test_export_excludes_builtin_and_code_assets(tmp_path):
    """内置 / 代码资产不进包 —— 它们随代码分发,打包会造第二份事实源。"""
    root = _project(tmp_path)
    builtin = tmp_path / "builtin"
    _write_preset(builtin, name="builtin-only")
    _write_preset(root, name="org-only")
    _kb, _skills, presets = _services(root, builtin_root=builtin)

    report = presets.export_pack("demo-pack", tmp_path / "pack")
    loaded = read_pack(tmp_path / "pack")
    assert list(loaded.files) == ["presets/org-only/preset.yml"]
    assert [i.item for i in report.items] == ["org-only"]


def test_export_excludes_decision_drafts(tmp_path):
    """``decision_drafts.yml`` 是待审状态不是资产 —— 不进包。"""
    root = _project(tmp_path)
    _write_decisions(root)
    kb, _skills, presets = _services(root)
    # 另一条 id(不与 decisions.yml 里那条撞 id)才是真草稿
    DecisionDraftStore(kb).add(
        "mini", _parse_rule(dict(RULE["rules"][0], id="loan-low")))
    assert (root / ".trove" / "kb" / "mini" / "decision_drafts.yml").is_file()

    presets.export_pack("demo-pack", tmp_path / "pack")
    assert list(read_pack(tmp_path / "pack").files) == ["kb/mini/decisions.yml"]


def _parse_rule(raw):
    from trove.services.decision.rules import parse_rule

    return parse_rule(raw)


def test_export_refuses_when_nothing_to_pack(tmp_path):
    """空包拒载:没有资产时"成功导出一个空包"比失败更糟。"""
    root = _project(tmp_path)
    _kb, _skills, presets = _services(root)
    with pytest.raises(PackError, match="空"):
        presets.export_pack("demo-pack", tmp_path / "pack")
    assert not (tmp_path / "pack").exists()


def test_export_origin_and_provenance_roundtrip(tmp_path):
    root = _project(tmp_path)
    _write_preset(root)
    _kb, _skills, presets = _services(root)
    presets.export_pack("demo-pack", tmp_path / "pack",
                        origin="export:upstream-pack")
    manifest = read_pack(tmp_path / "pack").manifest
    assert manifest.origin == "export:upstream-pack"
    assert manifest.tool_version          # 如实填;取不到才空
    assert manifest.exported_at


# ── 导入:一律 pending(红线) ────────────────────────────


async def test_import_lands_everything_pending(tmp_path):
    """三样资产全落 pending:技能被归一、决策落草稿、预设落盘不被消费。"""
    src = _project(tmp_path, "src")
    _write_skill(src)
    _write_decisions(src)
    _write_preset(src)
    _kb, _skills, origin_presets = _services(src)
    origin_presets.export_pack("demo-pack", tmp_path / "pack")

    dst = _project(tmp_path, "dst")
    kb, skills, presets = _services(dst)
    report = await presets.import_pack(tmp_path / "pack")
    assert report.refused == 0

    skill = skills.read_skill("loop-x")
    assert skill is not None and skill["status"] == "pending"
    assert skills.list_org(confirmed_only=True) == []      # 不在投递面

    assert DecisionDraftStore(kb).find_rule("mini", "loan-high") is not None
    assert not (dst / ".trove" / "kb" / "mini" / "decisions.yml").is_file()

    assert (dst / ".trove" / "presets" / "starter" / "preset.yml").is_file()


async def test_import_pending_skill_is_byte_identical(tmp_path):
    """源本已 pending → 导入后与源文件逐字节相同(往返保真的可断言形态)。"""
    src = _project(tmp_path, "src")
    _write_skill(src, override=("zh", "中文覆盖\n"))
    _kb, _skills, origin_presets = _services(src)
    origin_presets.export_pack("demo-pack", tmp_path / "pack")

    dst = _project(tmp_path, "dst")
    _kb2, _skills2, presets = _services(dst)
    await presets.import_pack(tmp_path / "pack")

    for rel in ("SKILL.md", "SKILL.zh.md"):
        assert ((dst / ".trove" / "skills" / "loop-x" / rel).read_bytes()
                == (src / ".trove" / "skills" / "loop-x" / rel).read_bytes())


async def test_import_confirmed_skill_is_normalized_to_pending(tmp_path):
    """已确认(confirmed)的技能进包后导入 → 只改 status 那一行,落 pending。"""
    src = _project(tmp_path, "src")
    _write_skill(src, text=SKILL_TEXT.replace("status: pending",
                                              "status: confirmed"))
    _kb, _skills, origin_presets = _services(src)
    origin_presets.export_pack("demo-pack", tmp_path / "pack")

    dst = _project(tmp_path, "dst")
    _kb2, skills2, presets = _services(dst)
    report = await presets.import_pack(tmp_path / "pack")

    landed = (dst / ".trove" / "skills" / "loop-x" / "SKILL.md").read_text(
        encoding="utf-8")
    source = (src / ".trove" / "skills" / "loop-x" / "SKILL.md").read_text(
        encoding="utf-8")
    assert landed == source.replace("status: confirmed", "status: pending")
    assert "归一为 pending" in report.render()
    assert skills2.read_skill("loop-x")["status"] == "pending"
    assert skills2.list_org(confirmed_only=True) == []


async def test_import_without_confirmation_field_lands_pending(tmp_path):
    """源文件没写 status → 读路径缺省是 pending,导入不额外插一行。"""
    root = _project(tmp_path, "src")
    _write_skill(root, text=SKILL_TEXT.replace("status: pending\n", ""))
    _kb, _skills, origin_presets = _services(root)
    origin_presets.export_pack("demo-pack", tmp_path / "pack")

    dst = _project(tmp_path, "dst")
    _kb2, _skills2, presets = _services(dst)
    await presets.import_pack(tmp_path / "pack")
    assert ((dst / ".trove" / "skills" / "loop-x" / "SKILL.md").read_bytes()
            == (root / ".trove" / "skills" / "loop-x" / "SKILL.md").read_bytes())


async def test_import_decisions_never_touch_decisions_yml(tmp_path):
    """决策导入的落点是草稿文件 —— 生效面(``decisions.yml``)一个字节不动。"""
    src = _project(tmp_path, "src")
    _write_decisions(src, data={"version": 1, "rules": [
        dict(RULE["rules"][0]), dict(RULE["rules"][0], id="loan-low")]})
    _kb, _skills, origin_presets = _services(src)
    origin_presets.export_pack("demo-pack", tmp_path / "pack")

    dst = _project(tmp_path, "dst")
    kb, _skills2, presets = _services(dst)
    report = await presets.import_pack(tmp_path / "pack")
    assert report.counts["imported"] == 2
    assert not (dst / ".trove" / "kb" / "mini" / "decisions.yml").exists()
    store = DecisionDraftStore(kb)
    assert store.find_rule("mini", "loan-high") is not None
    assert store.find_rule("mini", "loan-low") is not None


async def test_import_is_idempotent(tmp_path):
    """重放同一个包:全部 skipped,不产生第二份草稿、不报冲突。"""
    src = _project(tmp_path, "src")
    _write_skill(src)
    _write_decisions(src)
    _write_preset(src)
    _kb, _skills, origin_presets = _services(src)
    origin_presets.export_pack("demo-pack", tmp_path / "pack")

    dst = _project(tmp_path, "dst")
    kb, _skills2, presets = _services(dst)
    await presets.import_pack(tmp_path / "pack")
    again = await presets.import_pack(tmp_path / "pack")
    assert again.counts["skipped"] == 3
    assert again.refused == 0
    drafts = yaml.safe_load(
        (dst / ".trove" / "kb" / "mini" / "decision_drafts.yml").read_text(
            encoding="utf-8"))
    assert len(drafts["drafts"]) == 1


# ── 冲撞策略:默认拒载点名, --force 覆盖为 pending ───────


async def test_import_conflict_refuses_and_names_target(tmp_path):
    """同名技能已存在且内容不同 → 拒载点名,该资产一个字节不写;其余照常落。"""
    src = _project(tmp_path, "src")
    _write_skill(src)
    _write_preset(src)
    _kb, _skills, origin_presets = _services(src)
    origin_presets.export_pack("demo-pack", tmp_path / "pack")

    dst = _project(tmp_path, "dst")
    _write_skill(dst, text=SKILL_TEXT.replace("Step one", "a different body"))
    _kb2, skills2, presets = _services(dst)
    before = (dst / ".trove" / "skills" / "loop-x" / "SKILL.md").read_bytes()

    report = await presets.import_pack(tmp_path / "pack")
    assert report.counts["conflict"] == 1
    assert report.refused == 1
    assert (dst / ".trove" / "skills" / "loop-x" / "SKILL.md").read_bytes() \
        == before
    assert "同名 org skill 已存在" in report.render()
    # 其余资产照常导入
    assert (dst / ".trove" / "presets" / "starter" / "preset.yml").is_file()
    assert skills2.list_org(confirmed_only=True) == []


async def test_import_force_overwrites_skill_as_pending(tmp_path):
    """``--force``:同名覆盖,但覆盖物仍是 pending(绝不产出生效状态)。"""
    src = _project(tmp_path, "src")
    _write_skill(src)
    _kb, _skills, origin_presets = _services(src)
    origin_presets.export_pack("demo-pack", tmp_path / "pack")

    dst = _project(tmp_path, "dst")
    _write_skill(dst, text=SKILL_TEXT.replace("Step one", "old body"),
                 override=("zh", "旧覆盖\n"))
    _kb2, skills2, presets = _services(dst)

    report = await presets.import_pack(tmp_path / "pack", force=True)
    assert report.counts["overwritten"] == 1
    assert (dst / ".trove" / "skills" / "loop-x" / "SKILL.md").read_text(
        encoding="utf-8") == SKILL_TEXT
    assert skills2.read_skill("loop-x")["status"] == "pending"
    # 包内没有的旧覆盖文件:保留 + 报告点名(覆盖不是删除)
    assert (dst / ".trove" / "skills" / "loop-x" / "SKILL.zh.md").is_file()
    assert "未删除" in report.render()


async def test_import_conflict_on_preset_names_target(tmp_path):
    src = _project(tmp_path, "src")
    _write_preset(src)
    _kb, _skills, origin_presets = _services(src)
    origin_presets.export_pack("demo-pack", tmp_path / "pack")

    dst = _project(tmp_path, "dst")
    _write_preset(dst, data=dict(PRESET, description="本地的另一份"))
    before = (dst / ".trove" / "presets" / "starter" / "preset.yml").read_bytes()
    _kb2, _skills2, presets = _services(dst)

    report = await presets.import_pack(tmp_path / "pack")
    assert report.counts["conflict"] == 1
    assert (dst / ".trove" / "presets" / "starter" / "preset.yml").read_bytes() \
        == before
    forced = await presets.import_pack(tmp_path / "pack", force=True)
    assert forced.counts["overwritten"] == 1
    assert (dst / ".trove" / "presets" / "starter" / "preset.yml").read_bytes() \
        == (src / ".trove" / "presets" / "starter" / "preset.yml").read_bytes()


async def test_import_force_replaces_differing_pending_draft(tmp_path):
    """同 id 的 pending 草稿内容不同:默认拒载;``--force`` 驳回旧的再落新。"""
    src = _project(tmp_path, "src")
    _write_decisions(src)
    _kb, _skills, origin_presets = _services(src)
    origin_presets.export_pack("demo-pack", tmp_path / "pack")

    dst = _project(tmp_path, "dst")
    kb, _skills2, presets = _services(dst)
    store = DecisionDraftStore(kb)
    other = dict(RULE["rules"][0], conditions=["delta_pct > 0.5"])
    store.add("mini", _parse_rule(other), source="admin")

    report = await presets.import_pack(tmp_path / "pack")
    assert report.counts["conflict"] == 1
    assert store.find_rule("mini", "loan-high")["rule"]["conditions"] == \
        ["delta_pct > 0.5"]

    forced = await presets.import_pack(tmp_path / "pack", force=True)
    assert forced.counts["overwritten"] == 1
    current = store.find_rule("mini", "loan-high")
    assert current["rule"]["conditions"] == ["delta > 0"]
    drafts = yaml.safe_load(
        (dst / ".trove" / "kb" / "mini" / "decision_drafts.yml").read_text(
            encoding="utf-8"))
    assert [d["status"] for d in drafts["drafts"]] == ["rejected", "pending"]


async def test_live_decision_rule_never_overridden_even_with_force(tmp_path):
    """生效规则(``decisions.yml`` 里)永不被包覆盖 —— ``--force`` 也不行。"""
    src = _project(tmp_path, "src")
    _write_decisions(src)
    _kb, _skills, origin_presets = _services(src)
    origin_presets.export_pack("demo-pack", tmp_path / "pack")

    dst = _project(tmp_path, "dst")
    live = dict(RULE["rules"][0], conditions=["delta < 0"])
    live_path = _write_decisions(dst, data={"version": 1, "rules": [live]})
    before = live_path.read_bytes()
    _kb2, _skills2, presets = _services(dst)

    report = await presets.import_pack(tmp_path / "pack", force=True)
    assert report.counts["conflict"] == 1
    assert live_path.read_bytes() == before
    assert "生效规则永不被包覆盖" in report.render()


# ── 包级失败:一条资产都不落 ─────────────────────────────


async def test_tampered_pack_lands_nothing(tmp_path):
    src = _project(tmp_path, "src")
    _write_skill(src)
    _write_preset(src)
    _kb, _skills, origin_presets = _services(src)
    origin_presets.export_pack("demo-pack", tmp_path / "pack")
    target = tmp_path / "pack" / FILES_DIR / "skills" / "loop-x" / "SKILL.md"
    target.write_bytes(b"tampered\n")

    dst = _project(tmp_path, "dst")
    _kb2, _skills2, presets = _services(dst)
    with pytest.raises(PackTampered, match="skills/loop-x/SKILL.md"):
        await presets.import_pack(tmp_path / "pack")
    assert not (dst / ".trove" / "skills").exists()
    assert not (dst / ".trove" / "presets").exists()


async def test_newer_pack_schema_lands_nothing(tmp_path):
    src = _project(tmp_path, "src")
    _write_skill(src)
    _kb, _skills, origin_presets = _services(src)
    origin_presets.export_pack("demo-pack", tmp_path / "pack")
    manifest_path = tmp_path / "pack" / MANIFEST_FILE
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    data["pack_schema"] = 2
    manifest_path.write_text(yaml.safe_dump(data, allow_unicode=True,
                                            sort_keys=False), encoding="utf-8")

    dst = _project(tmp_path, "dst")
    _kb2, _skills2, presets = _services(dst)
    with pytest.raises(PackSchemaTooNew, match="pack_schema 2"):
        await presets.import_pack(tmp_path / "pack")
    assert not (dst / ".trove" / "skills").exists()


async def test_import_requires_wired_services(tmp_path):
    """裸 PresetService(没接 KB/技能服务)导不了 —— 落不了草稿就不装作能落。"""
    root = _project(tmp_path)
    presets = PresetService(root / ".trove" / "presets", git_enabled=False)
    with pytest.raises(PackError, match="接线"):
        await presets.import_pack(tmp_path / "pack")


# ── 条目级拒载:坏资产不入库 ─────────────────────────────


async def test_invalid_skill_is_refused_by_item(tmp_path):
    """技能过不了写入面形状校验 → 该条拒载,其余照常落。"""
    _flat_pack(tmp_path, {
        "skills/bad-tier/SKILL.md":
            SKILL_TEXT.replace("tier: required", "tier: turbo")
            .replace("name: loop-x", "name: bad-tier"),
        "presets/starter/preset.yml": yaml.safe_dump(PRESET, allow_unicode=True),
    })
    dst = _project(tmp_path, "dst")
    _kb, _skills2, presets = _services(dst)
    report = await presets.import_pack(tmp_path / "pack")
    assert report.counts["invalid"] == 1
    assert not (dst / ".trove" / "skills" / "bad-tier").exists()
    assert (dst / ".trove" / "presets" / "starter" / "preset.yml").is_file()


async def test_unknown_path_kind_is_refused_by_item(tmp_path):
    _flat_pack(tmp_path, {"kb/mini/examples.yml": b"[]\n"})
    dst = _project(tmp_path, "dst")
    _kb, _skills2, presets = _services(dst)
    report = await presets.import_pack(tmp_path / "pack")
    assert report.counts["invalid"] == 1
    assert not (dst / ".trove" / "kb").exists()


async def test_extra_files_are_reported_skipped(tmp_path):
    """包内未列出的多余文件:报 skipped,不导入。"""
    src = _project(tmp_path, "src")
    _write_preset(src)
    _kb, _skills, origin_presets = _services(src)
    origin_presets.export_pack("demo-pack", tmp_path / "pack")
    stray = tmp_path / "pack" / FILES_DIR / "skills" / "sneaky" / "SKILL.md"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"not part of the pack")

    dst = _project(tmp_path, "dst")
    _kb2, _skills2, presets = _services(dst)
    report = await presets.import_pack(tmp_path / "pack")
    assert report.counts["skipped"] == 1
    assert "skills/sneaky/SKILL.md" in report.render()
    assert not (dst / ".trove" / "skills").exists()


async def test_import_reports_datasource_kb_dir_missing(tmp_path):
    """目标还没有该数据源的 KB 目录:草稿照落,报告点名(不静默)。"""
    src = _project(tmp_path, "src")
    _write_decisions(src)
    _kb, _skills, origin_presets = _services(src)
    origin_presets.export_pack("demo-pack", tmp_path / "pack")

    dst = _project(tmp_path, "dst")
    _kb2, _skills2, presets = _services(dst)
    report = await presets.import_pack(tmp_path / "pack")
    assert report.counts["imported"] == 1
    assert "等待 /kb init" in report.render()


# ── 工具:手写小包(条目级拒载样本) ─────────────────────


def _flat_pack(root, files: dict[str, bytes]) -> None:
    """手写一个通过包校验、但资产内容有问题的包。"""
    write_pack(root / "pack", name="demo-pack",
               files={k: (v if isinstance(v, bytes) else v.encode("utf-8"))
                      for k, v in files.items()})
