"""对象图投影 —— 语义模型的**只读视图**(设计 §4.5,FR-GRAPH-1/2)。

对象视角没有自己的持久化、没有自己的写路径:``build_object_graph(model)``
是**纯函数**,零 I/O、零 LLM、同输入逐字节同输出 —— 对象图页、拒绝文案的
「X→Y 链接」、分析下钻选路共享这一份投影的定义,三处说的永远是同一批声明。

投影 ≡ 声明(FR-GRAPH-2 的两条往返断言,由测试钉住):

* 每个对象指回一个**数据集声明**(``objects[].dataset``);
* 每条边指回一条 **relationship 声明**(``links[].name``);
* 未声明 entity 的数据集也出现在图上(``declared: false``,弱化为「表」
  节点)——图的完整性让「声明覆盖率」成为一眼可见的缺口,而不是让未建模
  的表从视野里消失。

读侧回退都是**文档化的**(声明层保持缺失即缺失,回退只发生在这里,与
设计 §4.1/§4.2 同一条纪律):

* ``objects[].display_name``:entity.display_name 缺省 → 数据集 name;
* ``objects[].natural_keys``:entity.natural_keys 缺省 → primary_key;
* ``links[].display_name``:relationship.display_name 缺省 → 列连接串
  (``from_columns → to_columns``,设计 §4.2;列名 ``", "`` 连接,无列 →
  ``from → to`` 数据集名) —— 与前端 ``columnFallback`` 逐字节同款,两层
  回退给的是同一句话。层级 display_name 没有文档化的回退,不发明,原样透出。

``links[].fan_out`` 是**布尔派生值**(§4.5 DTO 只有布尔;原始串不出面)。
语义权威在编译器 ``compiler._classify_edges`` —— M:N 边
(``compiler._is_many_to_many`` 归一化判定,拼写无关)且豁免串不是 ``dedup``
时 True(``bridge:<dataset>`` 豁免未实现,保守仍 True);``dedup`` → False;
非 M:N(含空基数) → False。``unknown_cardinality`` 是另一回事(「无从判定」
不是「会倍增」),**不折进**这个字段。判定直接复用编译器函数,一致性由
行为表单测钉住(五种 M:N 拼写 × {空串, dedup, bridge:ds} 同输入同判定)。
"""
from __future__ import annotations

from typing import Any

from trove.services.semantic_layer.compiler import _is_many_to_many
from trove.services.semantic_layer.models import SemanticModel

_EMPTY: dict[str, list] = {"objects": [], "links": [], "hierarchies": []}


def _link_display_name(r: Any) -> str:
    """边的展示名:声明优先;缺省回退列名连接串(设计 §4.2)。

    回退串 = ``from_columns → to_columns``(列名 ``", "`` 连接;无列 →
    数据集名)—— 与前端 ``ontologyChart.columnFallback`` 同一格式:「这条边
    是哪两根键连的」在对象图页与任何后续消费面里必须是同一句话。
    """
    if r.display_name:
        return r.display_name
    from_cols = ", ".join(r.from_columns)
    to_cols = ", ".join(r.to_columns)
    if from_cols or to_cols:
        return f"{from_cols} → {to_cols}"
    return f"{r.from_} → {r.to}"


def _link_fan_out(r: Any) -> bool:
    """边的行倍增风险 → 布尔(与编译器 ``_classify_edges`` 同一判定)。

    复用 ``compiler._is_many_to_many``(拼写归一化:M:N/N:M/M2M/
    many-to-many 等全收)而不是在这里再写一遍拼写表 —— 两处判定漂移的
    后果是同一份声明在对象图页看着安全、在编译期被拒(或反过来)。
    豁免串比较照编译器:``(r.fan_out or "").strip().lower() == "dedup"``
    才是显式豁免;``bridge:<dataset>`` 的编译期豁免**未实现** → 保守 True。
    """
    if not _is_many_to_many(r.cardinality):
        return False
    return (r.fan_out or "").strip().lower() != "dedup"


def build_object_graph(model: SemanticModel | None) -> dict[str, Any]:
    """SemanticModel → 对象图 DTO(design §4.5;纯函数,绝不抛)。

    Args:
        model: 解析后的语义模型;``None``/空模型 → 空图(形状恒定:三个键
            都在 —— 空图与「读不到」由调用方(端点)用错误形状区分,
            投影自身不做 404/500 的判定)。

    Returns:
        ``{"objects": [...], "links": [...], "hierarchies": [...]}``,
        列表顺序 = 声明顺序(不排序、不去重 —— 声明即输出,顺序是文档的
        一部分,也与 PYTHONHASHSEED 无关)。
    """
    if model is None:
        return {k: list(v) for k, v in _EMPTY.items()}

    objects: list[dict[str, Any]] = []
    for d in model.datasets:
        entity = d.entity
        objects.append({
            "dataset": d.name,
            # 读侧回退(设计 §4.1):声明优先,缺省回退数据集 name。
            "display_name": (entity.display_name if entity and entity.display_name
                             else d.name),
            "declared": entity is not None,
            # 自然键缺省回退 primary_key(同一读侧纪律)。
            "natural_keys": (list(entity.natural_keys)
                             if entity and entity.natural_keys
                             else list(d.primary_key)),
            "aliases": list(entity.aliases) if entity else [],
            "metrics": [m.name for m in model.metrics if d.name in m.datasets],
            "field_count": len(d.fields),
        })

    links: list[dict[str, Any]] = []
    for r in model.relationships:
        links.append({
            "name": r.name,
            "from": r.from_,
            "to": r.to,
            "from_columns": list(r.from_columns),
            "to_columns": list(r.to_columns),
            "cardinality": r.cardinality,
            # 布尔派生值(§4.5):原始豁免串不出面,判定与编译器同源。
            "fan_out": _link_fan_out(r),
            "display_name": _link_display_name(r),
        })

    hierarchies = [
        {
            "name": h.name,
            "display_name": h.display_name,
            "dataset": h.dataset,
            "levels": list(h.levels),
        }
        for h in model.hierarchies
    ]

    return {"objects": objects, "links": links, "hierarchies": hierarchies}
