"""实体引用(entity_ref)的键位解析 —— 对象视图在行动面的只读投影(设计 §5.6)。

行动提案在创建时把「消息点名的那一组」冻结进 ``evidence_refs["group"]``
(B7);本模块回答随之而来的第二个问题:**这一组是不是一个已声明实体上
的键?** 是 → 同一个时点再钉一份
``evidence_refs["entity_ref"] = {"type": <实体显示名>, "keys": {<键字段>: <值>}}``
(设计 §5.6),行动台据此把「针对 loan L-456」渲染成人能读的对象名;不是
(未声明 / 引用解析不了 / 聚合规则)→ **整键缺席**:宁缺不猜(N37)——
一份猜出来的对象引用比没有引用更糟,它会被展示成一条像模像样的业务
事实,而且和 ``group`` 一样在创建时冻结,错了没有第二次机会纠正。

**为什么解析器在这里(与 ``outcome.py`` 同款姿态)**:action 包构造上
够不到语义层(姿态守卫 AST 钉死),解析要读语义模型,所以执行面留在
包外,由 ``ActionService`` 以**可注入 callable** 的形式接收
(``entity_lookup``,装配在 ``main.py``)。包拿到的是一次调用,不是任何
可以自己伸向语义层的东西 —— 与 B7 ``verifier`` 的例外条款同一条理由、
同一个装配点。

**与对象图页同一定义(反漂移)**:「哪些字段是实体的键」在**本仓只有
一处定义** —— ``semantic_layer/object_graph.py`` 的投影:``declared``
只看 entity 块在不在,``natural_keys`` 空则回退 ``primary_key``、显示名
空则回退数据集名(D1 读侧回退)。这里直接消费 ``build_object_graph``
的输出,而不是把回退规则再写一遍 —— 两处各写一遍,迟早漂到「对象图页
说是键、行动引用不认」。

**解析与编译器同一把尺**:dim 引用(``loan.id`` 或裸列名)交给
``semantic_layer/query.py::_resolve_field`` —— 规则作者写在
``subject.dimensions`` 里的引用怎么解析,编译期与这里永远是同一个答案
(裸列名跨数据集不唯一 → 两侧同样拒绝)。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from trove.core.logging import get_logger
from trove.services.semantic_layer.object_graph import build_object_graph
from trove.services.semantic_layer.query import SemanticQueryError, _resolve_field

logger = get_logger(__name__)

#: 解析器契约:``async (datasource, dim 引用) -> spec | None``。
#: spec = ``{"type": <实体显示名>, "field": <键字段名>}``;
#: 解析不了(数据源够不到 / 无语义模型 / 引用不解析 / 不是键)→ ``None``。
EntityLookup = Callable[[str, str], Awaitable[dict[str, str] | None]]


def entity_key_index(model: Any) -> dict[str, dict[str, str]]:
    """已声明实体的键字段索引:``"<dataset>.<field>"`` → 引用规格。

    spec = ``{"type": 实体显示名(D1 回退后), "field": 键字段名}`` ——
    ``type`` 是**读侧回退后**的展示名(与对象图页同一份定义),``field``
    是键字段的裸名(前端把它原样括注,不编业务标签)。

    只有 ``declared`` 的对象进索引(未声明 = 不是对象,它的主键只是
    物理约束,不是业务键);键取自投影的 ``natural_keys``(已含回退到
    ``primary_key`` 的 D1 纪律)。``setdefault`` = 声明顺序先到先得,
    输出与 ``PYTHONHASHSEED`` 无关;``model=None`` → 空索引(缺席,不抛)。
    """
    index: dict[str, dict[str, str]] = {}
    for obj in build_object_graph(model)["objects"]:
        if not obj.get("declared"):
            continue
        etype = str(obj.get("display_name") or "").strip()
        if not etype:
            continue
        dataset = str(obj.get("dataset") or "")
        for key in obj.get("natural_keys") or []:
            name = str(key or "").strip()
            if not name:
                continue
            index.setdefault(f"{dataset}.{name}", {"type": etype, "field": name})
    return index


def entity_spec_for(model: Any, dim_ref: str) -> dict[str, str] | None:
    """dim 字段引用 → 键字段规格;未声明 / 解析不了 → ``None``(宁缺不猜)。

    解析复用编译器自己的 ``query._resolve_field``(裸列名须跨数据集唯一),
    解析结果落回索引的规范名 ``"<dataset>.<field>"``(索引按**声明**的
    大小写建,``_resolve_field`` 给回的正是声明对象,所以逐字对上)。
    """
    if model is None:
        return None
    try:
        ds_name, field = _resolve_field(model, str(dim_ref or ""))
    except SemanticQueryError:
        return None
    spec = entity_key_index(model).get(f"{ds_name}.{field.name}")
    return dict(spec) if spec else None


def make_entity_lookup(decision: Any) -> EntityLookup:
    """装配解析器 —— ``decision`` = ``DecisionService``(语义模型/方言解析
    都从它复用),与 ``outcome.make_verifier`` 同款包外执行面。

    复用 ``decision._dialect`` / ``decision._model_for`` 而不是另开一条
    模型加载路径:派生看到的声明必须与判定编译看到的是同一份(判定的
    维度编译过、提案才存在 —— 两处读到不同模型,引用会指到一个判定
    从未看过的对象上)。

    **一切失败 → None**:数据源够不到、无语义模型、引用不解析、投影坏形
    —— 引用是展示与审计元数据,缺席是合法结局(宁缺不猜),绝不抛回
    提案创建;失败原因记 info 日志(诊断用),不吃掉异常本身。
    """
    async def lookup(datasource: str, dim_ref: str) -> dict[str, str] | None:
        try:
            dialect = await decision._dialect(datasource)
            model = decision._model_for(datasource, dialect)
            return entity_spec_for(model, dim_ref)
        except Exception as e:  # noqa: BLE001 — 缺席是合法结局，见 docstring
            logger.info("entity_ref: lookup failed for %r / %r: %s",
                        datasource, dim_ref, e)
            return None

    return lookup
