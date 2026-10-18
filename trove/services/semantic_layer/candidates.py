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

证据门槛(为什么只收这些 reason):

- 字段类 = ``_resolve_field`` **解析失败**的理由(未声明字段/时间字段
  未声明/分析分区列未解析)——「模型里缺这个声明」就是缺口本身;
- 指标类 = ``no_metric_match``:计划给了聚合候选但没匹配上声明度量。
  表达式的原文就是权威定义(编译器的对账就是按表达式/签名做的),
  可原样落为 metric 的 expression。
- 值类 = ``enum_value_unresolved`` **仅当字段带实测取值**(``Field.values``,
  probe 的结构事实)且提问字面量命中其一:命中即"库里确实这么存",
  落 ``value_aliases`` 别名的证据是硬的。**不命中/未探测一律不猜**——
  主导失败模式是翻译缺口(用户说「男性/月结」,库里存 ``M/monthly``),
  机器无法从缺口中推出目标码;自映射(``{V: [V]}``)会把错字面量冻结进
  骨架,聚合题的 0 行过滤没有守卫兜底,静默算错比拒绝更糟。

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
# 理由(time_field_not_temporal / missing_filter_value /
# expression_filter_value)刻意不在列:缺口不在声明,或在值域——值域里
# 只有 enum_value_unresolved 走下面的证据门控 _value_spec。
_FIELD_REASONS = frozenset({
    "unresolved_filter_field",
    "unresolved_answer_column",
    "time_field_not_declared",
    "analysis_partition_unresolved",
    "analysis_order_unresolved",
})

# 指标候选理由:聚合候选有签名但未匹配声明度量。
_METRIC_REASON = "no_metric_match"

# 值候选理由:字段已解析、值词表归一失败。值拿得到(``miss["value"]``,
# 编译器第三键),但落候选必须过实测取值证据门槛(见 _value_spec)。
_VALUE_REASONS = frozenset({"enum_value_unresolved"})

# 实体命名候选理由:问题确定性地用到了某数据集的列,而该数据集还没有
# 实体声明 —— 对象视图的门面(投影里的 declared 节点、词法锚定)就长在
# 这里。触发面刻意窄:只从字段形状的缺口(见 _entity_spec)。
_ENTITY_REASON = "entity_naming"


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


def _value_spec(component: str, value: str, model: Any) -> dict[str, Any] | None:
    """值类缺口(字段 ref + 提问字面量)→ field 候选;证据不足 → None。

    证据门槛:字段的实测取值(``Field.values``,probe 的结构事实)里存在
    与提问字面量大小写无关的相等项 —— 命中即"库里确实这么存",落一条
    ``value_aliases[stored] += [stored]`` 让该字面量今后可被归一。合并
    必须全量(baseline 别名 + 新条目):``_apply_field`` 的 payload 是
    整体替换,不是增量。

    未命中/字段未探测(``values`` 空)→ None:翻译缺口(用户说「男性」,
    库里存 ``M``)的目标码机器推不出,自映射会把错字面量冻结进骨架
    (聚合题 0 行过滤没有守卫兜底)——不猜,见模块头。
    """
    ref = _column_ref(component)
    if ref is None:
        return None
    ds_name, field_name = ref
    ds = next(
        (d for d in model.datasets if d.name.lower() == ds_name.lower()), None)
    if ds is None:
        return None
    fld = next(
        (f for f in ds.fields if f.name.lower() == field_name.lower()), None)
    if fld is None:
        return None  # 字段未声明:缺口在声明,归 _field_spec 那条路
    probe = str(value or "").strip()
    if not probe:
        return None
    stored = next(
        (str(v) for v in (fld.values or [])
         if str(v).strip().lower() == probe.lower()),
        None,
    )
    if stored is None or not stored.strip():
        return None
    merged = {
        str(code): [str(a) for a in (labels or [])]
        for code, labels in (fld.value_aliases or {}).items()
    }
    aliases = merged.setdefault(stored, [])
    if not any(a.strip().lower() == stored.lower() for a in aliases):
        aliases.append(stored)
    return {
        "kind": "field",
        "name": f"{ds.name}.{fld.name}",
        "payload": {"expression": fld.expression, "value_aliases": merged},
        "reason": "enum_value_unresolved",
        "evidence": f"{ds.name}.{fld.name}={stored}",
    }


def _entity_spec(component: str, model: Any) -> dict[str, Any] | None:
    """``dataset.field`` ref → 实体命名候选;已声明/锚不住/无可声明内容 → None。

    与 ``_field_spec`` 信任同一个锚(单一 ``dataset.field`` 引用,见
    ``_column_ref``)——不重做指标侧的多表推断,也不额外猜数据集。

    payload 只写**模型里读得到的**事实(主键 → 自然键;数据集描述 → 显示
    名,规则与 kb init 后置起草共用 ``entity_display_name``):schema notes /
    terms 是 kb init 现场才有的原料,捕获路径拿不到 **也不去猜**;那份更富
    的初始化版本由 init 后置起草负责,两个生产者以 (kind, name) 幂等收口。

    自然键空且推不出显示名 → 不落草稿:``entity: {}`` 在解析器里等于未声明,
    一条什么都声明不了的实体草稿只会让「确认过了」变成一句空话。
    """
    ref = _column_ref(component)
    if ref is None:
        return None
    ds_name = ref[0]
    ds = next((d for d in model.datasets
               if str(d.name).lower() == ds_name.lower()), None)
    if ds is None or getattr(ds, "entity", None) is not None:
        return None
    from trove.services.kb.deterministic_gen import entity_display_name

    payload: dict[str, Any] = {"dataset": ds.name}
    label = entity_display_name(str(getattr(ds, "description", "") or ""), ds.name)
    if label:
        payload["display_name"] = label
    keys = [str(k) for k in (getattr(ds, "primary_key", None) or []) if str(k).strip()]
    if keys:
        payload["natural_keys"] = keys
    if len(payload) <= 1:
        return None
    evidence = ds.name + (f" natural_keys={','.join(keys)}" if keys else "")
    return {
        "kind": "entity",
        "name": ds.name,
        "payload": payload,
        "reason": _ENTITY_REASON,
        "evidence": evidence,
    }


def candidate_specs(misses: list[Any], model: Any) -> list[dict[str, Any]]:
    """软 MISS 列表 + 当前模型 → 候选规格(纯函数,零 IO;metric 优先)。

    输出顺序 = 捕获优先级:先 metric(缺口更大)后 field,最后 entity
    (对象声明是元一级的,单轮上限下让位给能直接解锁本题的声明);同轮按
    (kind, name) 去重(同一缺口的多个 reason 只建一条)。实体候选只从
    字段形状的缺口发射(见 ``_entity_spec``)。
    """
    if model is None:
        return []
    metrics: list[dict[str, Any]] = []
    fields: list[dict[str, Any]] = []
    entities: list[dict[str, Any]] = []
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
        elif reason in _VALUE_REASONS:
            # 值类只在 miss 带第三键(提问字面量)时才有证据可用;
            # 两键老 miss(值缺席)→ 逐字节与今天相同:不产生候选。
            if miss.get("value") is None:
                continue
            spec = _value_spec(component, str(miss.get("value")), model)
            bucket = fields
        else:
            continue
        if spec is not None:
            key = (spec["kind"], spec["name"])
            if key not in seen:
                seen.add(key)
                bucket.append(spec)
        # 实体候选挂在「字段形状的缺口」上:能确定性地定位到某数据集的列,
        # 就是「这个问题用到了它」的硬证据(未声明实体才发射,见 _entity_spec)。
        if reason in _FIELD_REASONS or reason in _VALUE_REASONS:
            entity = _entity_spec(component, model)
            if entity is not None:
                ekey = (entity["kind"], entity["name"])
                if ekey not in seen:
                    seen.add(ekey)
                    entities.append(entity)
    return metrics + fields + entities


def _outcome(
    spec: dict[str, Any],
    status: str,
    *,
    draft_id: str = "",
    errors: list[str] | None = None,
    entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """规格 → 逐条处理结果(``created|exists|invalid`` 闭集)。

    ``entry`` 是内部键(新建草稿的完整条目),``capture_candidates`` 用它
    保持旧返回形状;对外的 read 路径(覆盖率体检)只消费前六个键。
    """
    row: dict[str, Any] = {
        "kind": str(spec.get("kind") or ""),
        "name": str(spec.get("name") or ""),
        "reason": str(spec.get("reason") or ""),
        "status": status,
        "draft_id": draft_id,
        "errors": list(errors or []),
    }
    if entry is not None:
        row["entry"] = entry
    return row


async def create_specs(
    kb: Any,
    datasource: str,
    specs: list[dict[str, Any]],
    *,
    actor: str = "",
    note_prefix: str = "auto",
    max_candidates: int = MAX_CANDIDATES_PER_RUN,
    validate: bool = False,
    dialect: str | None = None,
) -> list[dict[str, Any]]:
    """候选规格 → pending 草稿;返回**逐条**结果,单条失败不连坐。

    这是「规格 → 草稿」的唯一写路径:``capture_candidates``(软 MISS 收尾)
    与覆盖率体检的「生成草稿」共用它,去重语义因此只有一份。

    - **只写 pending**:``create_draft`` 只落 ``semantic_drafts.yml``;本函数
      从不调用 ``confirm_draft``(确认门在人手里)。
    - 幂等 = ``(kind, name)``:metric/field 按 pending 去重(已确认/已拒绝的
      不进 pending 桶,允许重新起草);entity 按**全状态**去重(命名定过
      一次就不再问)。命中 → ``exists``(带既有草稿 id)。
    - ``validate=True`` 时逐条跑 ``validate_draft``(与 confirm 同一条应用
      路径的干跑):不过门禁 → ``invalid`` + 错误文案,**不落一条注定
      确认不了的草稿**。``capture_candidates`` 保持 ``validate=False``
      ——老行为逐字节不变。
    - ``spec["question"]``(可选)进 note:``{prefix}:{reason}:{evidence}:{q[:100]}``。
    """
    if kb is None or not datasource or not specs:
        return []
    from trove.services.semantic_layer.manage import SemanticManager

    try:
        manager = SemanticManager(kb)
    except Exception as e:  # 路径都构不出来:每条都如实报 invalid,不假装成功
        logger.warning("Candidate manager unavailable (%s): %s", datasource, e)
        return [_outcome(s, "invalid", errors=[f"semantic layer unavailable: {e}"])
                for s in specs]

    try:
        entries = manager.drafts(datasource)
    except Exception:
        entries = {"pending": []}  # 读队列失败不挡写入:宁可多一条由人看
    pending_ids: dict[tuple[str, str], str] = {}
    decided_entities: dict[tuple[str, str], str] = {}
    for status, bucket in entries.items():
        for d in bucket:
            key = (str(d.get("kind") or ""), str(d.get("name") or ""))
            draft_id = str(d.get("id") or "")
            if status == "pending":
                pending_ids.setdefault(key, draft_id)
            elif key[0] == "entity":
                decided_entities.setdefault(key, draft_id)

    document: dict[str, Any] | None = None
    if validate:
        try:
            document = manager.document(datasource)
        except Exception:
            document = {}
        from trove.services.semantic_layer.issues import validate_draft

    outcomes: list[dict[str, Any]] = []
    created = 0
    for spec in specs:
        if created >= max_candidates:
            break
        key = (str(spec.get("kind") or ""), str(spec.get("name") or ""))
        if key in pending_ids:
            outcomes.append(_outcome(spec, "exists", draft_id=pending_ids[key]))
            continue
        if key[0] == "entity" and key in decided_entities:
            outcomes.append(
                _outcome(spec, "exists", draft_id=decided_entities[key]))
            continue
        if validate:
            check = validate_draft(
                document, kind=key[0], action="upsert", name=key[1],
                payload=spec.get("payload") or {}, dialect=dialect,
            )
            if not check.get("ok"):
                errors = [
                    str(item.get("message") or item)
                    for item in (check.get("errors") or [])
                ]
                errors += [
                    str(item.get("message") or item)
                    for item in (check.get("warnings") or [])
                ]
                outcomes.append(_outcome(spec, "invalid", errors=errors[:10]))
                continue
        evidence = str(spec.get("evidence") or "")
        note_head = f"{evidence}:" if evidence else ""
        question = str(spec.get("question") or "")
        try:
            entry = await manager.create_draft(
                datasource, key[0], "upsert", key[1],
                payload=spec.get("payload"),
                note=f"{note_prefix}:{spec.get('reason', '')}:"
                     f"{note_head}{question[:100]}",
                actor=actor,
            )
        except Exception as e:
            logger.warning(
                "Candidate draft write failed (%s, %s): %s",
                datasource, key[1], e)
            outcomes.append(_outcome(spec, "invalid", errors=[str(e)[:300]]))
            continue
        created += 1
        outcomes.append(_outcome(
            spec, "created", draft_id=str(entry.get("id") or ""), entry=entry))
    return outcomes


async def capture_candidates(
    kb: Any,
    datasource: str,
    question: str,
    misses: list[Any],
    *,
    max_candidates: int = MAX_CANDIDATES_PER_RUN,
) -> list[dict[str, Any]]:
    """软 MISS → pending 草稿(best-effort;返回实际落库的草稿条目)。

    幂等与去重语义全在 :func:`create_specs`(唯一写路径);本函数只负责
    「当前模型 → 候选规格」与把问题带进 note(``auto:{reason}:...``,
    与抽取前逐字节一致)。
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
    tagged = [{**spec, "question": question} for spec in specs]
    outcomes = await create_specs(
        kb, datasource, tagged, max_candidates=max_candidates)
    return [o["entry"] for o in outcomes
            if o.get("status") == "created" and o.get("entry")]
