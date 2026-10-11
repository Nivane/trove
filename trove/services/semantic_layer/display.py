"""声明 → 显示名映射 —— 消费面的**唯一一份**(E7 §5.4(g) / FR-DEC-1)。

分析载荷(行卡 ``dim_display``、标签 ``primary_dimension_display`` /
``dimensions_display``)与判定行卡、噪声带 ``by_dim`` 条目共用这一份映射:
同一份声明在两个消费面必须给出同一句话,两处各写一遍迟早漂移。

值 → 显示名(优先序固定,逐级回退):

1. ``field.enum_display[值]``(声明里的 code → 人类可读词);
2. ``field.value_aliases[值]`` 的首个别名(与 enum_display 同源的业务词);
3. 字段是该数据集**已声明实体**的自然键时 → ``"{实体显示名} {值}"`` ——
   自然键缺省回退 ``primary_key``、实体显示名缺省回退数据集 name,都是
   **读侧**口径(与 ``object_graph`` 投影逐字同一句;声明层保持缺失即
   缺失,回退只发生在这里)。

级别字段 → 层级显示名:只取层级声明自己的 ``display_name`` —— 未声明就
是**没有**(层级 display_name 没有文档化的回退,**不发明**,与
object_graph 同一条纪律)。

值匹配大小写敏感优先、不中再大小写无关(与 ``candidates._value_spec``
的证据门同款);歧义(同一级别字段落在多条层级里)同样是 ``None``。

纯函数、零 I/O、零 LLM;解析不了 → ``None``(调用方按**缺席容忍**处理:
键不出现、旧输出逐字节不变,绝不猜)。
"""
from __future__ import annotations

from typing import Any


def _norm(text: Any) -> str:
    return str(text or "").strip()


def resolve_ref(model: Any, ref: str) -> tuple[Any, Any] | None:
    """``dataset.field`` 引用 → (数据集对象, 字段对象);解析不了 → None。

    前缀匹配数据集**声明名**(声明序),剩余段必须无点且命中该数据集的
    字段 —— 不走 ``split(".")``:数据集名允许含点(云仓
    ``project.dataset``),字段名不允许,从长到短的声明名前缀才是唯一
    不会猜错的方向。
    """
    text = _norm(ref)
    if not text or model is None:
        return None
    for ds in (getattr(model, "datasets", None) or []):
        name = _norm(getattr(ds, "name", ""))
        if not name or not text.lower().startswith(name.lower() + "."):
            continue
        remainder = text[len(name) + 1:]
        if not remainder or "." in remainder:
            continue
        for fld in (getattr(ds, "fields", None) or []):
            if _norm(getattr(fld, "name", "")).lower() == remainder.lower():
                return ds, fld
    return None


def _dataset_of(model: Any, dataset_name: str) -> Any | None:
    name = _norm(dataset_name)
    if not name or model is None:
        return None
    for ds in (getattr(model, "datasets", None) or []):
        if _norm(getattr(ds, "name", "")).lower() == name.lower():
            return ds
    return None


def _field_of(dataset: Any, field_name: str) -> Any | None:
    name = _norm(field_name)
    if not name or dataset is None:
        return None
    for fld in (getattr(dataset, "fields", None) or []):
        if _norm(getattr(fld, "name", "")).lower() == name.lower():
            return fld
    return None


def _value_display(field: Any, value: Any) -> str | None:
    """字段级映射(不含实体回退):enum_display → value_aliases 首别名。"""
    text = _norm(value)
    if not text:
        return None
    enum_display = getattr(field, "enum_display", None) or {}
    shown = enum_display.get(text)
    if not _norm(shown):  # 大小写无关兜底(存储值可能被驱动归一过拼写)
        for code, label in enum_display.items():
            if _norm(code).lower() == text.lower() and _norm(label):
                shown = label
                break
    if _norm(shown):
        return _norm(shown)
    aliases = getattr(field, "value_aliases", None) or {}
    for code, values in aliases.items():
        if _norm(code).lower() != text.lower():
            continue
        for alias in (values or []):
            if _norm(alias):
                return _norm(alias)
    return None


def value_display(model: Any, dataset_name: str, field_name: str, value: Any) -> str | None:
    """值 → 显示名(三级回退,见模块 docstring);无声明映射 → None。"""
    dataset = _dataset_of(model, dataset_name)
    field = _field_of(dataset, field_name)
    if field is None:
        return None
    shown = _value_display(field, value)
    if shown is not None:
        return shown
    entity = getattr(dataset, "entity", None)
    if entity is None:
        return None
    text = _norm(value)
    keys = [
        _norm(k) for k in (getattr(entity, "natural_keys", None) or []) if _norm(k)
    ] or [
        _norm(k) for k in (getattr(dataset, "primary_key", None) or []) if _norm(k)
    ]
    if not text or field.name not in keys:
        return None
    label = _norm(getattr(entity, "display_name", "")) or _norm(getattr(dataset, "name", ""))
    if not label:
        return None
    return f"{label} {text}"


def ref_value_display(model: Any, ref: str, value: Any) -> str | None:
    """``dataset.field`` 引用版 ``value_display``;引用解析不了 → None。"""
    resolved = resolve_ref(model, ref)
    if resolved is None:
        return None
    ds, fld = resolved
    return value_display(model, ds.name, fld.name, value)


def hierarchy_at(model: Any, dataset_name: str, field_name: str) -> tuple[int, int, Any] | None:
    """级别字段 → (层级声明序, 级别序, 层级对象)。

    不属于任何层级 / 同一字段落在多条层级里(歧义)→ ``None`` —— 选路与
    显示都不猜。
    """
    ds_name = _norm(dataset_name).lower()
    field = _norm(field_name).lower()
    if not ds_name or not field or model is None:
        return None
    hits: list[tuple[int, int, Any]] = []
    for hidx, h in enumerate(getattr(model, "hierarchies", None) or []):
        if _norm(getattr(h, "dataset", "")).lower() != ds_name:
            continue
        for lidx, level in enumerate(getattr(h, "levels", None) or []):
            if _norm(level).lower() == field:
                hits.append((hidx, lidx, h))
    if len(hits) != 1:
        return None
    return hits[0]


def field_display(model: Any, dataset_name: str, field_name: str) -> str | None:
    """级别字段 → 层级 display_name;不是级别/未声明显示名 → None(不发明)。"""
    hit = hierarchy_at(model, dataset_name, field_name)
    if hit is None:
        return None
    return _norm(getattr(hit[2], "display_name", "")) or None


def ref_field_display(model: Any, ref: str) -> str | None:
    """``dataset.field`` 引用版 ``field_display``。"""
    resolved = resolve_ref(model, ref)
    if resolved is None:
        return None
    ds, fld = resolved
    return field_display(model, ds.name, fld.name)


def next_level_ref(model: Any, dataset_name: str, field_name: str) -> str | None:
    """级别字段 → 同层级下一级(细一级)的 ``dataset.field`` 引用。

    已是末级 / 不在任何层级 / 歧义 → ``None``(调用方回退原行为)。
    引用按层级声明的数据集名与级别字段名原样构造,保证可被编译器解析。
    """
    hit = hierarchy_at(model, dataset_name, field_name)
    if hit is None:
        return None
    _, lidx, hierarchy = hit
    levels = list(getattr(hierarchy, "levels", None) or [])
    if lidx + 1 >= len(levels):
        return None
    return f"{_norm(getattr(hierarchy, 'dataset', ''))}.{_norm(levels[lidx + 1])}"


def ref_next_level(model: Any, ref: str) -> str | None:
    """``dataset.field`` 引用版 ``next_level_ref``。"""
    resolved = resolve_ref(model, ref)
    if resolved is None:
        return None
    ds, fld = resolved
    return next_level_ref(model, ds.name, fld.name)
