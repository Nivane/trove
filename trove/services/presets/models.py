"""预设包(preset)的契约 + 结果形状 —— 接入模板的声明面。

一个 preset 说清「接一类数据源时,组织希望先摆上哪些**方法论骨架**」:
skills(技能骨架/引用)、decisions(规则模板/引用)、domains(主题域骨架/引用)、
semantics(口径提示)、presentation(展示偏好提示)。它**只装跨源方法论与
骨架** —— 数据源事实(表名、字段、枚举、gold SQL)一律归 KB,preset 里出现
具体数据源事实就是把「接入即建模」变成「接入即作弊」。

闭键集:每一层的键都在这里闭死,未知键**拒绝并列出合法键名**。理由与
``skills.service`` 的 ``create`` 同款:YAML 往返会把未知键静默保留,写错了
没人告诉你 —— 而这份配置从任何外部面看都与"写对了"一样。

两个来源(见 ``service.py``):内置只读 ``trove/presets/<name>/preset.yml``
随代码分发;组织 ``.trove/presets/<name>/preset.yml`` 由 admin 管理。同名时
**组织版遮蔽内置版**(确定性;validate 会把它报出来)。

本模块是纯数据 + 解析:读取、校验、结果形状。套用动作在 ``service.py``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── 闭键集 ────────────────────────────────────────────────

#: 顶层。``version`` 是 preset 自己的修订号(与 org skill 的 version 同义:
#: 内容变更就 +1),不是 schema 版本 —— 契约本身靠闭键集保证。
PRESET_KEYS = (
    "name", "version", "description", "author",
    "skills", "decisions", "domains", "semantics", "presentation",
)

#: 「引用既有资产」形态:只允许点名键。`skills: [报表口径]` 与
#: `skills: [{name: 报表口径}]` 等价(前者是后者的糖)。
REF_KEYS = ("name", "id")

#: 技能骨架 —— 直接喂 ``SkillService.create``(它自己做更严的闭键校验)。
SKILL_TEMPLATE_KEYS = (
    "name", "description", "tier", "triggers", "lang", "body",
    "mode", "severity", "targets", "checks",
)

#: 决策规则模板 —— 键集 = ``decision.rules`` 的规则字段(``rule_to_dict``
#: 的往返形状),不多不少:模板是规则,不是另一种东西。
DECISION_TEMPLATE_KEYS = (
    "id", "name", "enabled", "severity", "owner_role", "window", "subject",
    "baseline", "scope", "emit", "top_k", "conditions", "condition_mode",
    "recommendation", "priority", "action", "driver_dimension",
)

#: 主题域骨架 —— 喂 ``SemanticManager.create_draft(kind="topic")``。
DOMAIN_TEMPLATE_KEYS = (
    "name", "datasets", "description", "synonyms", "metrics", "examples",
)

#: 提示段:只校验形状,不产任何生效内容(apply 报告里逐条 ``skipped``)。
SEMANTICS_KEYS = ("notes", "metrics", "time_grains")
PRESENTATION_KEYS = ("notes", "chart", "drill_down")

#: 套用结果的三种状态。
#: ``drafted`` = 落了 pending 草稿;``skipped`` = 无需动作(引用已就位 / 提示段);
#: ``unresolved`` = 引用的资产在目标数据源上解析不到,什么都没落。
STATUSES = ("drafted", "skipped", "unresolved")

#: preset 来源。
SOURCES = ("builtin", "org")


class PresetError(ValueError):
    """一份不符合契约的 preset(拒绝读取/写入)。"""


def reject_unknown(raw: Any, legal: tuple[str, ...], where: str) -> None:
    """未知键 → 报错并**列出合法键名**。"""
    if not isinstance(raw, dict):
        raise PresetError(f"{where} 必须是映射,得到 {type(raw).__name__}")
    unknown = sorted(str(k) for k in raw if k not in legal)
    if unknown:
        raise PresetError(
            f"{where}: 未知键 {', '.join(unknown)};合法键: {', '.join(legal)}")


def _as_section_list(raw: Any, where: str) -> list[Any]:
    if raw is None or raw == "":
        return []
    if not isinstance(raw, list):
        raise PresetError(f"{where} 必须是列表")
    return list(raw)


# ── 解析 ──────────────────────────────────────────────────


@dataclass
class Preset:
    """一份解析通过的 preset(结构已校验,引用**未**解析 —— 引用要靠
    目标数据源才判得了,属于套用/校验阶段的事)。"""

    name: str
    version: int = 1
    description: str = ""
    author: str = ""
    skills: list[Any] = field(default_factory=list)
    decisions: list[Any] = field(default_factory=list)
    domains: list[Any] = field(default_factory=list)
    semantics: dict[str, Any] = field(default_factory=dict)
    presentation: dict[str, Any] = field(default_factory=dict)
    #: ``builtin`` | ``org`` —— 同名遮蔽后**生效的那个来源**。
    source: str = "builtin"
    path: str = ""

    @property
    def counts(self) -> dict[str, int]:
        return {
            "skills": len(self.skills),
            "decisions": len(self.decisions),
            "domains": len(self.domains),
            "semantics": len(self.semantics),
            "presentation": len(self.presentation),
        }


def _parse_skill_entry(entry: Any, i: int) -> Any:
    where = f"skills[{i}]"
    if isinstance(entry, str):
        if not entry.strip():
            raise PresetError(f"{where}: 技能引用不能为空")
        return entry.strip()
    if isinstance(entry, dict) and set(entry) <= set(REF_KEYS):
        name = str(entry.get("name") or "").strip()
        if not name:
            raise PresetError(f"{where}: 技能引用缺 name")
        return name
    reject_unknown(entry, SKILL_TEMPLATE_KEYS, where)
    if not str(entry.get("name") or "").strip():
        raise PresetError(f"{where}: name 必填")
    return dict(entry)


def _parse_decision_entry(entry: Any, i: int) -> Any:
    where = f"decisions[{i}]"
    if isinstance(entry, str):
        if not entry.strip():
            raise PresetError(f"{where}: 规则引用不能为空")
        return entry.strip()
    if isinstance(entry, dict) and set(entry) <= set(REF_KEYS):
        rid = str(entry.get("id") or "").strip()
        if not rid:
            raise PresetError(f"{where}: 规则引用缺 id")
        return rid
    reject_unknown(entry, DECISION_TEMPLATE_KEYS, where)
    if not str(entry.get("id") or "").strip():
        raise PresetError(f"{where}: id 必填")
    return dict(entry)


def _parse_domain_entry(entry: Any, i: int) -> Any:
    where = f"domains[{i}]"
    if isinstance(entry, str):
        if not entry.strip():
            raise PresetError(f"{where}: 主题域引用不能为空")
        return entry.strip()
    if isinstance(entry, dict) and set(entry) <= set(REF_KEYS):
        name = str(entry.get("name") or "").strip()
        if not name:
            raise PresetError(f"{where}: 主题域引用缺 name")
        return name
    reject_unknown(entry, DOMAIN_TEMPLATE_KEYS, where)
    if not str(entry.get("name") or "").strip():
        raise PresetError(f"{where}: name 必填")
    return dict(entry)


def parse_preset(data: Any, *, source: str = "builtin",
                 path: str | Path = "", name_hint: str = "") -> Preset:
    """``preset.yml`` → :class:`Preset`。结构不过一律抛 :class:`PresetError`。

    契约(spec 的硬要求):顶层闭键集;``name`` 必填且与目录名一致(不一致
    时套用按哪个名字走是二义的 —— 与 org skill 的目录名/frontmatter 名同一
    条纪律);``description`` 必填(一份没有说明的预设无从判断该不该套);
    三个资产段每项要么是「引用」(字符串 / 只带 name|id 的映射)要么是
    「骨架」(完整映射,闭键集)。
    """
    if data is None:
        data = {}
    reject_unknown(data, PRESET_KEYS, "preset")
    name = str(data.get("name") or "").strip()
    if not name:
        raise PresetError("preset: name 必填")
    if name_hint and name != name_hint:
        raise PresetError(
            f"preset: name {name!r} 与目录名 {name_hint!r} 不一致"
            "(套用按名字解析,两个名字会让「套哪一份」变成二义)")
    description = str(data.get("description") or "").strip()
    if not description:
        raise PresetError("preset: description 必填(说明这份预设装了什么、"
                          "适合哪类数据源)")
    raw_version = data.get("version", 1)
    if isinstance(raw_version, bool) or not isinstance(raw_version, int) or raw_version < 1:
        raise PresetError(f"preset: version 必须是 >= 1 的整数,得到 {raw_version!r}")

    semantics = data.get("semantics") or {}
    reject_unknown(semantics, SEMANTICS_KEYS, "semantics")
    presentation = data.get("presentation") or {}
    reject_unknown(presentation, PRESENTATION_KEYS, "presentation")

    return Preset(
        name=name,
        version=raw_version,
        description=description,
        author=str(data.get("author") or "").strip(),
        skills=[_parse_skill_entry(e, i)
                for i, e in enumerate(_as_section_list(data.get("skills"), "skills"))],
        decisions=[_parse_decision_entry(e, i)
                   for i, e in enumerate(_as_section_list(data.get("decisions"), "decisions"))],
        domains=[_parse_domain_entry(e, i)
                 for i, e in enumerate(_as_section_list(data.get("domains"), "domains"))],
        semantics=dict(semantics),
        presentation=dict(presentation),
        source=source,
        path=str(path),
    )


def preset_to_dict(preset: Preset) -> dict[str, Any]:
    """:class:`Preset` → 可 YAML 化的映射(admin 编辑面往返用)。"""
    out: dict[str, Any] = {
        "name": preset.name,
        "version": preset.version,
        "description": preset.description,
    }
    if preset.author:
        out["author"] = preset.author
    if preset.skills:
        out["skills"] = [dict(e) if isinstance(e, dict) else e for e in preset.skills]
    if preset.decisions:
        out["decisions"] = [dict(e) if isinstance(e, dict) else e for e in preset.decisions]
    if preset.domains:
        out["domains"] = [dict(e) if isinstance(e, dict) else e for e in preset.domains]
    if preset.semantics:
        out["semantics"] = dict(preset.semantics)
    if preset.presentation:
        out["presentation"] = dict(preset.presentation)
    return out


def brief(preset: Preset, *, shadowed: bool = False) -> dict[str, Any]:
    """列表视图:契约摘要 + 来源 + 是否遮蔽了同名内置版。"""
    return {
        "name": preset.name,
        "version": preset.version,
        "description": preset.description,
        "author": preset.author,
        "source": preset.source,
        "path": preset.path,
        "counts": preset.counts,
        "shadowed": shadowed,
    }


# ── 套用结果 ──────────────────────────────────────────────


@dataclass
class ApplyItem:
    """一条套用结果。``reason`` 永远非空 —— 一条"为什么"说不清的记录,
    读的人只能去猜,而猜出来的结论往往比"没做"更糟。"""

    section: str          # skills | decisions | domains | semantics | presentation
    item: str
    status: str           # STATUSES
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "item": self.item,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass
class ApplyReport:
    """一次套用的账本。``items`` 逐条可读,``counts`` 供页面/CLI 打一行摘要。"""

    preset: str = ""
    datasource: str = ""
    source: str = ""
    items: list[ApplyItem] = field(default_factory=list)

    def add(self, section: str, item: str, status: str, reason: str = "") -> ApplyItem:
        if status not in STATUSES:
            raise ValueError(f"unknown apply status: {status!r}")
        entry = ApplyItem(section=section, item=str(item), status=status,
                          reason=reason)
        self.items.append(entry)
        return entry

    @property
    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in STATUSES}
        for i in self.items:
            out[i.status] = out.get(i.status, 0) + 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "preset": self.preset,
            "datasource": self.datasource,
            "source": self.source,
            "counts": self.counts,
            "items": [i.to_dict() for i in self.items],
        }

    def render(self) -> str:
        """CLI/REPL 的一屏文本(与 ``validate`` 的报告渲染同一取向)。"""
        lines = [
            f"套用 preset {self.preset!r} → 数据源 {self.datasource!r}"
            f"（来源: {self.source or '未知'}）",
        ]
        status_label = {"drafted": "落草稿", "skipped": "跳过", "unresolved": "未解析"}
        for i in self.items:
            lines.append(f"  [{status_label.get(i.status, i.status)}] "
                         f"{i.section}/{i.item}: {i.reason}")
        c = self.counts
        lines.append(
            f"合计: {c.get('drafted', 0)} 条落 pending 草稿 · "
            f"{c.get('skipped', 0)} 条跳过 · {c.get('unresolved', 0)} 条未解析"
            "（草稿不会生效,逐条确认后才生效）")
        return "\n".join(lines)
