"""答案卡对象列:答案列 ↔ 已声明实体字段的**唯一精确匹配**(设计 §5.7 / FR-GRAPH-5)。

``build_object_columns(model, columns)`` 是**纯函数**:零 I/O、零 LLM、
同输入逐字节同输出 —— agent 侧答案收尾处(``SessionManager``)拿它把执行
SQL 的答案列对**已声明实体**(``entity`` 非空的 dataset)的字段名做精确
全等匹配,唯一命中才建 ``{列名: {"entity": 显示名, "field": 字段名}}``。

三条纪律:

* **唯一命中才建映射**(宁缺不猜):零命中(列名对不上/该列不属于任何
  已声明实体)或**多实体同名字段撞车** → 该列不进入结果 —— 猜错的表头
  增强比没有增强坏得多:它把「这列是谁的字段」写在了最显眼的位置上。
* **显示名回退与对象图 D1 同一份代码**(``object_graph.entity_display_name``,
  entity.display_name 缺省 → 数据集 name)—— 两个消费面(对象图页、答案卡
  表头)说的永远是同一个名字。
* **只加映射,不动值**:产物是展示标注;rows/columns 一字不改(消费端
  DataTable 的表头增强同样只动展示,过滤/排序/复制/下载继续用原始列名)。

未声明实体 / 无可建映射 → 空 dict;载荷侧由生产者决定**键缺席**(不是
``null``/``{}``)——「键在不在」是前端唯一判据(缺席渲染逐字节不变)。
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from trove.services.semantic_layer.models import SemanticModel
from trove.services.semantic_layer.object_graph import entity_display_name


def build_object_columns(
    model: SemanticModel | None,
    columns: Iterable[Any] | None,
) -> dict[str, dict[str, str]]:
    """执行答案列 → 对象列映射(唯一精确命中;见模块 docstring)。

    候选全集 = ``entity`` 非空的数据集的字段(未声明数据集不参与 ——
    「对象列」描述的是**对象**字段,未声明表没有对象)。匹配是列名与
    字段名的**精确全等**:不做大小写折叠/前缀/别名(别名锚定是 E4 的
    检索面,不是这里的展示面)。

    Args:
        model: 语义模型;``None``(无模型/读不到)→ ``{}``。
        columns: 答案列名(执行 SQL 的列序);非 str/空串跳过。

    Returns:
        ``{列名: {"entity": <显示名>, "field": <字段名>}}``,键序 = 入参
        列序(不排序、不补列);无可建映射 → ``{}``(绝不返回半猜的映射)。
    """
    if model is None:
        return {}
    declared = [d for d in model.datasets if d.entity is not None]
    if not declared:
        return {}
    out: dict[str, dict[str, str]] = {}
    for col in columns or []:
        if not isinstance(col, str) or not col:
            continue
        hits = [(d, f) for d in declared for f in d.fields if f.name == col]
        if len(hits) != 1:
            continue  # 零命中 / 多实体同名字段 → 整列不建映射(宁缺不猜)
        dataset, field = hits[0]
        out[col] = {
            "entity": entity_display_name(dataset),
            "field": field.name,
        }
    return out
