"""候选收件箱:软 MISS → 确定性语义候选(pending,永不自确认)。

软 MISS 的语义是「回答照常交付,但计划里有用例模型**没声明**的组件」——
这正是语义层该生长的地方。本模块把这类缺口翻译成 pending 语义草稿
(复用 ``SemanticManager`` 草稿机),管理员在收件箱
(``/v1/admin/todos`` 的 semantic_draft 源,条目 summary 即草稿 note)
里确认或拒绝。

三条硬约束(与仓库既有纪律同源):

1. **零 LLM**:捕获跑在每次查询的收尾路径上(``SessionManager``),LLM
   成本不可接受;所有翻译都是对 ``compile_misses``(reason/component)
   的确定性改写。
2. **只落 pending**:``create_draft`` 只写 semantic_drafts.yml,不碰
   semantics.yml —— 自动内容绝不绕过管理员确认(与 memory 的
   auto-write 同一条纪律)。
3. **只针对模型里没有的声明**:名字已声明的 metric/field 一律跳过 ——
   候选若诱导管理员确认出一个错误/重复的声明,比没有候选更糟
   (草稿无编辑端点,pending 要么确认要么拒绝)。

证据门槛(为什么只收这两类 reason):

- 字段类 = ``_resolve_field`` **解析失败**的理由(未声明字段/时间字段
  未声明/分析分区列未解析)——「模型里缺这个声明」就是缺口本身;
  值类(enum_value_unresolved 等)字段已解析,缺的是值词表,而
  component 只带 field_ref 不带值,确定性补不出来 → 不产生候选。
- 指标类 = ``no_metric_match``:计划给了聚合候选但没匹配上声明度量。
  表达式的原文就是权威定义(编译器的对账就是按表达式/签名做的),
  可原样落为 metric 的 expression。

锚定规则(保守,宁缺勿错):只收**全列表限定**的引用 ——
``dataset.field`` 形式的字段 ref、全部列都带表前缀且锚定唯一已声明
数据集的聚合表达式。裸列名(如 ``SUM(amount)``)在模型里无法确定性
定位到数据集,交人工,不猜。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# 单轮捕获上限:一次查询最多落这么多条草稿(每条一次 git commit),
# 防止一个多缺口计划把收件箱刷屏。
MAX_CANDIDATES_PER_RUN = 3

# 「字段解析失败」类软 MISS 理由 → field 候选。字段已解析的值/口径类
# 理由(enum_value_unresolved / time_field_not_temporal / missing_filter_value
# / expression_filter_value)刻意不在列:缺口不在声明,或在值域,见模块头。
_FIELD_REASONS = frozenset({
    "unresolved_filter_field",
    "unresolved_answer_column",
    "time_field_not_declared",
    "analysis_partition_unresolved",
    "analysis_order_unresolved",
})

# 指标候选理由:聚合候选有签名但未匹配声明度量。
_METRIC_REASON = "no_metric_match"


def _column_ref(text: str) -> tuple[str, str] | None:
    """文本 → ``(dataset, field)``;非单一 ``dataset.field`` 列引用 → None。

    走 sqlglot 解析(与编译器同源):``loan.amount`` 是 Column;
    ``loan.amount * 2`` / ``SUM(amount)`` / 自然语言都不是 → 不猜。
    """
    from sqlglot import exp, parse_one

    try:
        tree = parse_one(text)
    except Exception:
        return None
    if not isinstance(tree, exp.Column) or not tree.table or not tree.name:
        return None
    ds, col = str(tree.table), str(tree.name)
    if "." in ds or "." in col:
        return None  # 多点/异常标识符 → 拒绝歧义切分
    return ds, col


def _metric_spec(component: str, model: Any) -> dict[str, Any] | None:
    """聚合表达式 → metric 候选规格;推导不出正确声明 → None。

    条件:单一聚合函数、所有列都表限定、锚定唯一**已声明**数据集、
    推导名 ``{聚合}_{列尾}`` 未被既有声明占用(占用即跳过,绝不覆盖)。
    """
    from sqlglot import exp, parse_one
    from sqlglot.errors import ErrorLevel

    try:
        tree = parse_one(component, error_level=ErrorLevel.RAISE)
    except Exception:
        return None
    funcs = {f.sql_name().lower() for f in tree.find_all(exp.AggFunc)}
    if len(funcs) != 1:
        return None  # 非聚合 / 多聚合算式(比值型):名字推导有歧义,交人工
    all_cols = list(tree.find_all(exp.Column))
    cols = [c for c in all_cols if c.table and c.name]
    if not cols or len(cols) != len(all_cols):
        return None  # 无列(COUNT(*))或有裸列 → 无法确定性锚定数据集

    declared = {d.name.lower(): d.name for d in model.datasets}
    tables: list[str] = []
    for c in cols:
        name = declared.get(str(c.table).lower())
        if name is None:
            return None  # 引用了未声明数据集 → 锚不住(_apply_metric 会拒)
        if name not in tables:
            tables.append(name)
    if len(tables) != 1:
        return None  # 多数据集聚合:口径归人工

    metric_name = f"{next(iter(funcs))}_{str(cols[-1].name).lower()}"
    if metric_name in {m.name.lower() for m in model.metrics}:
        return None  # 名字已被占用:不建会覆盖既有声明的候选
    return {
        "kind": "metric",
        "name": metric_name,
        "payload": {"expression": component, "datasets": tables},
        "reason": _METRIC_REASON,
    }


def _field_spec(reason: str, component: str, model: Any) -> dict[str, Any] | None:
    """``dataset.field`` ref → field 候选规格;锚不住/已声明 → None。"""
    ref = _column_ref(component)
    if ref is None:
        return None
    ds_name, field_name = ref
    ds = next(
        (d for d in model.datasets if d.name.lower() == ds_name.lower()), None)
    if ds is None:
        return None  # 数据集未声明:字段无处挂靠(声明数据集是另一件事)
    if field_name.lower() in {f.name.lower() for f in ds.fields}:
        return None  # 已声明 → 缺口另有原因(值/口径),不重复建
    payload: dict[str, Any] = {"expression": f"{ds.name}.{field_name}"}
    if reason == "time_field_not_declared":
        # 计划把它当时间轴用(时间分桶的 field 解析失败)→ 按时间字段起草
        payload["is_time"] = True
    return {
        "kind": "field",
        "name": f"{ds.name}.{field_name}",
        "payload": payload,
        "reason": reason,
    }


def candidate_specs(misses: list[Any], model: Any) -> list[dict[str, Any]]:
    """软 MISS 列表 + 当前模型 → 候选规格(纯函数,零 IO;metric 优先)。

    输出顺序 = 捕获优先级:先 metric(缺口更大)后 field;同轮按
    (kind, name) 去重(同一缺口的多个 reason 只建一条)。
    """
    if model is None:
        return []
    metrics: list[dict[str, Any]] = []
    fields: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for miss in misses or []:
        if not isinstance(miss, dict):
            continue
        reason = str(miss.get("reason") or "")
        component = str(miss.get("component") or "").strip()
        if not component:
            continue
        if reason == _METRIC_REASON:
            spec = _metric_spec(component, model)
            bucket = metrics
        elif reason in _FIELD_REASONS:
            spec = _field_spec(reason, component, model)
            bucket = fields
        else:
            continue
        if spec is None:
            continue
        key = (spec["kind"], spec["name"])
        if key in seen:
            continue
        seen.add(key)
        bucket.append(spec)
    return metrics + fields


async def capture_candidates(
    kb: Any,
    datasource: str,
    question: str,
    misses: list[Any],
    *,
    max_candidates: int = MAX_CANDIDATES_PER_RUN,
) -> list[dict[str, Any]]:
    """软 MISS → pending 草稿(best-effort;返回实际落库的草稿条目)。

    幂等:``(kind, name)`` 已在 pending 队列 → 跳过(同题重跑不刷屏;
    create_draft 自身无去重,这里是唯一的闸门)。单条失败不清空整批 ——
    第一次写成功、第二条炸掉,第一条的候选不该被连坐。
    """
    if kb is None or not datasource or not misses:
        return []
    from trove.services.semantic_layer.manage import SemanticManager

    try:
        manager = SemanticManager(kb)
        model = manager.model(datasource)
    except Exception as e:
        logger.warning("Candidate model lookup failed (%s): %s", datasource, e)
        return []
    specs = candidate_specs(misses, model)
    if not specs:
        return []
    try:
        pending = {
            (str(d.get("kind") or ""), str(d.get("name") or ""))
            for d in manager.drafts(datasource).get("pending", [])
        }
    except Exception:
        pending = set()  # 读队列失败不挡写入:宁可多一条去重由人看

    created: list[dict[str, Any]] = []
    for spec in specs:
        if len(created) >= max_candidates:
            break
        if (spec["kind"], spec["name"]) in pending:
            continue
        try:
            entry = await manager.create_draft(
                datasource, spec["kind"], "upsert", spec["name"],
                payload=spec["payload"],
                note=f"auto:{spec['reason']}:{str(question or '')[:100]}",
            )
        except Exception as e:
            logger.warning(
                "Candidate draft write failed (%s, %s): %s",
                datasource, spec["name"], e)
            continue
        created.append(entry)
    return created
