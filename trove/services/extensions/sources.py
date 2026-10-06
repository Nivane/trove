"""只读聚合:项目里的扩展资产 → ExtensionEnvelope 列表。

汇四个来源,每个都走**其服务自己的读函数** —— 与运行时同一份解析,
绝不复写(复写的版本迟早与写入面漂移,而信封的价值正是「说的 = 有的」):

- org skills   ``.trove/skills/<name>/SKILL.md``   → ``SkillService.read_skill``
- code skills  ``trove/prompts/skills/manifest.yml`` → ``SkillService.list_code_skills``
- 决策规则     ``.trove/kb/<ds>/decisions.yml``      → ``KbService.load_decisions``
- preset       内置(trove/presets/)+ ``.trove/presets/`` → ``PresetService._sources``

坏文件不在这里判死:validate 的既有检查器负责点名;本模块对读不出的
资产**降级构建**(错误消息进 ``unresolved``)或跳过(决策文档解析失败),
绝不让聚合层自身崩掉 —— 它是展示面,不是判定面。

零 LLM、零网络;文件访问只读。
"""

from __future__ import annotations

from pathlib import Path

from trove.services.extensions.envelope import (
    ExtensionEnvelope,
    MountCatalog,
    build_decision_envelope,
    build_preset_envelope,
    build_skill_envelope,
)
from trove.services.kb.service import KbService
from trove.services.presets.service import PresetService
from trove.services.skills.service import SkillService


def _code_manifest() -> Path:
    import trove

    return Path(trove.__file__).parent / "prompts" / "skills" / "manifest.yml"


def collect_skill_envelopes(
    project_root: Path, catalog: MountCatalog,
) -> list[ExtensionEnvelope]:
    """code + org skills。code 档的文件=随包分发的 manifest(来源链在此)。"""
    out: list[ExtensionEnvelope] = []
    manifest = _code_manifest()
    svc = SkillService(project_root / ".trove" / "skills")

    for entry in svc.list_code_skills():
        out.append(build_skill_envelope(
            entry, files=[manifest], source="code", catalog=catalog))

    skills_root = project_root / ".trove" / "skills"
    if not skills_root.is_dir():
        return out
    for d in sorted(skills_root.iterdir()):
        if not d.is_dir() or not (d / "SKILL.md").exists():
            continue
        entry = svc.read_skill(d.name)
        if entry is None:
            continue
        out.append(build_skill_envelope(
            {**entry, "name": entry.get("name") or d.name},
            files=[d / "SKILL.md"], source="org", catalog=catalog))
    return out


def collect_decision_envelopes(project_root: Path) -> list[ExtensionEnvelope]:
    """每个数据源 KB 的 decisions.yml,逐规则一封。"""
    kb_dir = project_root / ".trove" / "kb"
    if not kb_dir.is_dir():
        return []
    kb = KbService(project_root, git_kb=False)
    out: list[ExtensionEnvelope] = []
    for ds_dir in sorted(kb_dir.iterdir()):
        doc_path = ds_dir / "decisions.yml"
        if not ds_dir.is_dir() or not doc_path.exists():
            continue
        try:
            doc = kb.load_decisions(ds_dir.name)
        except Exception:  # noqa: BLE001 — 解析失败由 decision.schema 检查器点名
            continue
        for rule in doc.rules:
            out.append(build_decision_envelope(
                rule, files=[doc_path], datasource=ds_dir.name))
    return out


def collect_preset_envelopes(project_root: Path) -> list[ExtensionEnvelope]:
    """生效的 preset(org 遮蔽 builtin)——影子副本不单独列:信封是**生效面**。

    ``_sources`` 是 PresetService 的同一份双源装载(validate 也用私有读取
    面,见其对 ``_builtin_root`` 的引用);``git_enabled=False`` 因为这里
    只读不写。
    """
    svc = PresetService(
        project_root / ".trove" / "presets", git_enabled=False)
    org, builtin = svc._sources()
    out = [build_preset_envelope(p) for _, p in sorted(org.items())]
    out += [build_preset_envelope(p) for name, p in sorted(builtin.items())
            if name not in org]
    return out


def collect_assets(
    project_root: str | Path = ".", *,
    catalog: MountCatalog | None = None,
) -> list[ExtensionEnvelope]:
    """全量资产信封(kind, name, source 排序)—— 只读、零 LLM。"""
    root = Path(project_root)
    cat = catalog if catalog is not None else MountCatalog.from_validate()
    out: list[ExtensionEnvelope] = []
    out += collect_skill_envelopes(root, cat)
    out += collect_decision_envelopes(root)
    out += collect_preset_envelopes(root)
    return sorted(out, key=lambda e: (e.kind, e.name, e.source))
