"""模型级变更 diff —— 实体集合比对（反射驱动）+ 草稿级 diff 适配。

不新写字段级 diff 引擎：``manage._draft_diff`` 已是服务端计算、带 carryover
语义的实现（三处消费）。本模块做两件事（设计 §5.3）：

- ``entities``：base / after 两份**解析后模型**按 dataclass 反射做实体级集合
  比对——``dataclasses.asdict`` 拿全字段，任何字段变化都落进 ``modified``，
  不靠手写字段清单（R5 的防漏改机制，逐字段扰动测试钉住）；
- ``details``：逐 payload 透传 ``_draft_diff`` 的 before/after/fields。

坏文档不抛：实体层退化空比对，``details`` 照常（评审辅助,不是门禁）。
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any

from trove.services.semantic_layer.models import SemanticModel
from trove.services.semantic_layer.ossie import parse_ossie

_SECTIONS = ("datasets", "metrics", "relationships", "topics")


@dataclass(frozen=True)
class EntityDiff:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.modified)

    def to_dict(self) -> dict[str, list[str]]:
        return {"added": list(self.added), "removed": list(self.removed),
                "modified": list(self.modified)}

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "EntityDiff":
        d = d or {}
        return cls(added=list(d.get("added") or []),
                   removed=list(d.get("removed") or []),
                   modified=list(d.get("modified") or []))


@dataclass(frozen=True)
class ChangeDiff:
    entities: dict[str, EntityDiff] = field(default_factory=dict)
    details: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"entities": {k: v.to_dict() for k, v in self.entities.items()},
                "details": [dict(d) for d in self.details]}

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "ChangeDiff":
        d = d or {}
        return cls(
            entities={k: EntityDiff.from_dict(v)
                      for k, v in (d.get("entities") or {}).items()},
            details=[dict(x) for x in (d.get("details") or [])])


def _entity_rows(model: SemanticModel) -> dict[str, dict[str, dict[str, Any]]]:
    """模型 → {section: {实体名: 属性字典}}。fields 展平为 ``dataset.field``。

    datasets 一行的 ``fields`` 键**替换为字段名单**：字段级细节由 fields 节
    承担，否则改一个字段的 datatype 会把 dataset 与 field 两处都标 modified
    （重复计数，评审噪声）。
    """
    rows: dict[str, dict[str, dict[str, Any]]] = {s: {} for s in _SECTIONS + ("fields",)}
    for m in model.metrics:
        rows["metrics"][m.name] = dataclasses.asdict(m)
    for d in model.datasets:
        row = dataclasses.asdict(d)
        row["fields"] = [f.name for f in d.fields]
        rows["datasets"][d.name] = row
        for f in d.fields:
            rows["fields"][f"{d.name}.{f.name}"] = dataclasses.asdict(f)
    for r in model.relationships:
        rows["relationships"][r.name] = dataclasses.asdict(r)
    for t in model.topics:
        rows["topics"][t.name] = dataclasses.asdict(t)
    return rows


def _diff_rows(base_rows: dict, after_rows: dict, details: list[dict]) -> ChangeDiff:
    """两份实体表 → 逐节集合比对。两边都**没有行** = 没得可比（两份文档
    都没解析出来），``entities`` 退化为空字典而不是五节全空 —— 节全空会被
    读成「比过了，什么都没变」，而事实是**没比**。"""
    if not base_rows and not after_rows:
        return ChangeDiff(entities={}, details=details)
    entities: dict[str, EntityDiff] = {}
    for section in _SECTIONS + ("fields",):
        b = base_rows.get(section, {})
        a = after_rows.get(section, {})
        entities[section] = EntityDiff(
            added=sorted(set(a) - set(b)),
            removed=sorted(set(b) - set(a)),
            modified=sorted(n for n in set(a) & set(b) if a[n] != b[n]),
        )
    return ChangeDiff(entities=entities, details=details)


def _parse(doc: dict[str, Any] | None) -> SemanticModel | None:
    if not doc:
        return None
    try:
        import yaml

        return parse_ossie(
            yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
            preferred_dialect="sqlite")
    except Exception:
        return None


def build_change_diff(
    base_doc: dict[str, Any] | None,
    after_doc: dict[str, Any] | None,
    payloads: list[dict[str, Any]],
    dialect: str | None = None,
) -> ChangeDiff:
    from trove.services.semantic_layer.manage import _draft_diff

    base_model = _parse(base_doc)
    after_model = _parse(after_doc)
    base_rows = _entity_rows(base_model) if base_model is not None else {}
    after_rows = _entity_rows(after_model) if after_model is not None else {}
    details = [_draft_diff(base_doc or {}, p, dialect) for p in payloads]
    return _diff_rows(base_rows, after_rows, details)
