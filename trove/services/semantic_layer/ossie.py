"""Apache Ossie semantic model parser (core spec, v0.2.0.dev0).

Reads the parts relevant to NL→SQL plus the spec's extensibility surface:
- `semantic_model → datasets` (source, primary_key, unique_keys, fields
  with datatype / dimension.is_time / label / synonyms) — table anchoring
  + the declared filter/group-by vocabulary;
- `relationships` (from → to, key pairs) — the declared join graph;
- `metrics` (name, multi-dialect aggregate expression, datatype,
  ai_context synonyms/examples) — business metrics;
- `custom_extensions` (vendor_name + data) — preserved/passed through;
- model-level `ai_context` (instructions / synonyms / examples) and the
  top-level `version` string;
- field-level `mask` + model-level `masking` (default_policy /
  bypass_scopes / hash_salt_ref) — the masking **declaration**; applying it
  to result sets is `authz/masking.py` (P4);
- field-level `values` (该列的实际取值,probe 的结构事实) — read as plain
  data; it never changes `semantic_role` nor feeds prompt rendering;
- dataset-level `entity` (display_name / natural_keys / aliases) and
  model-level `hierarchies` (name / display_name / dataset / levels) —
  the object-view declarations (Trove 扩展); relationship-level
  `display_name`. 全部可选,缺省 = 存量模型逐字节同前;放错层(文档根)
  出声但忽略,与 masking/topics 同一条告警先例。
"""
import logging
import re

import yaml
from sqlglot import exp, parse_one

from trove.services.semantic_layer.models import (
    Hierarchy,
    MaskingPolicy,
    SemanticDataset,
    SemanticEntity,
    SemanticField,
    SemanticMetric,
    SemanticModel,
    SemanticRelationship,
    TimeSpine,
    TopicDomain,
    _clean_extensions,
    _clean_names,
    _clean_values,
)

logger = logging.getLogger(__name__)

#: 我们写出/消费的 OSSIE core spec 版本(文档级 ``version`` 的唯一权威)。
#: 三个写入点(kb 结构生成 / flat↔OSSIE 桥接 / 变更暂存补写)都引用这里。
OSSIE_VERSION = "0.2.0.dev0"

_ANSI = "ANSI_SQL"

# 正则回退:表达式 SQLGlot 解析失败时提取 数据集.字段 引用
_DATASET_REF_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)")

# OSSIE is_time 默认:时态 datatype 未显式覆盖时视为时间维度
_TEMPORAL_TYPES = {"date", "time", "datetime", "datetimetz"}


def _pick_expression(dialects: list[dict], preferred: str) -> str:
    """从方言列表中选表达式:adapter 方言 → ANSI_SQL → 第一个可用。

    Raises:
        ValueError: 没有任何可用表达式(结构性问题,provider 层会
            last-known-good 兜底)。
    """
    by_dialect = {
        d.get("dialect", "").upper(): d.get("expression", "")
        for d in dialects if d.get("expression")
    }
    for key in (preferred.upper(), _ANSI):
        if key in by_dialect:
            return by_dialect[key]
    if not by_dialect:
        raise ValueError("metric expression has no dialects")
    return next(iter(by_dialect.values()))


def _expression_of(entity: dict, preferred: str) -> str:
    """实体(dataset 下 field / metric)的 expression.dialects 抽取。"""
    expr = (entity.get("expression") or {}).get("dialects", []) or []
    if not expr:
        raise ValueError("expression has no dialects")
    return _pick_expression(expr, preferred)


def _dataset_refs(expression: str, declared: set[str]) -> list[str]:
    """表达式里引用到的数据集名(只信声明过的数据集,防误报)。"""
    found: set[str] = set()
    try:
        for col in parse_one(expression, read="sqlite").find_all(exp.Column):
            if col.table in declared:
                found.add(col.table)
    except Exception:  # 片段解析失败 → 正则回退
        for m in _DATASET_REF_RE.finditer(expression):
            if m.group(1) in declared:
                found.add(m.group(1))
    return sorted(found)


def _resolve_is_time(dimension: object, datatype: str | None) -> bool:
    """dimension.is_time 显式值优先;未设置时按 datatype 时态默认。"""
    if isinstance(dimension, dict) and "is_time" in dimension:
        return bool(dimension["is_time"])
    if datatype and datatype.lower() in _TEMPORAL_TYPES:
        return True
    return False


def _ai_context(entity: dict) -> tuple[str, list[str], list[str]]:
    """实体/模型的 ai_context → (instructions, synonyms, examples)。

    兼容旧形态:ai_context 为字符串 → 视为 synonyms。
    """
    ai = entity.get("ai_context")
    if isinstance(ai, str):
        return "", [ai], []
    if isinstance(ai, dict):
        return (
            str(ai.get("instructions", "") or ""),
            list(ai.get("synonyms") or []),
            list(ai.get("examples") or []),
        )
    return "", [], []


def _masking_policy(raw: object) -> MaskingPolicy:
    """模型级 ``masking`` 块 → MaskingPolicy;坏形状一律退回缺省。

    这是**声明**的解析,不是安全判定点:坏的 ``default_policy`` 回落
    ``apply``(= 脱敏,取安全的一侧,与 time_spine 的坏 granularity 回落
    month 同法);坏形状不抛 —— 抛出去会让 provider 丢掉 last-known-good,
    把一处笔误放大成整个语义模型消失。
    """
    mk = raw if isinstance(raw, dict) else {}
    policy = str(mk.get("default_policy") or "apply").strip().lower()
    if policy not in {"apply", "bypass"}:
        logger.warning(
            "masking.default_policy %r invalid; defaulting to apply", policy)
        policy = "apply"
    raw_scopes = mk.get("bypass_scopes") or []
    if isinstance(raw_scopes, str):
        # 裸标量 ``bypass_scopes: pii`` 是**一个** scope,不是 ['p','i','i']
        raw_scopes = [raw_scopes]
    if not isinstance(raw_scopes, (list, tuple)):
        raw_scopes = []
    return MaskingPolicy(
        default_policy=policy,
        bypass_scopes=[str(s).strip() for s in raw_scopes if str(s).strip()],
        hash_salt_ref=str(mk.get("hash_salt_ref") or "").strip(),
    )


def _entity_declaration(raw: object) -> SemanticEntity | None:
    """数据集 ``entity`` 子块 → SemanticEntity;坏形状/空块 → ``None``。

    与 :func:`_masking_policy` 同一姿态:这是**声明**的解析,不是判定点。
    映射/嵌套容器忽略而不是猜(猜会让一份写错的文件静默变成一份能跑的
    模型);三键全空(``entity: {}``)与缺席同义 —— 都归一为未声明,
    投影的 ``declared`` 只看「有没有实体声明」这一件事。
    """
    if not isinstance(raw, dict):
        return None
    entity = SemanticEntity(
        display_name=str(raw.get("display_name") or "").strip(),
        natural_keys=_clean_names(raw.get("natural_keys")),
        aliases=_clean_names(raw.get("aliases")),
    )
    if not (entity.display_name or entity.natural_keys or entity.aliases):
        return None
    return entity


def parse_ossie(text: str, preferred_dialect: str = "ansi_sql") -> SemanticModel:
    """Parse an Ossie semantic model YAML into SemanticModel.

    Args:
        text: OSSIE YAML content (spec: top-level `semantic_model` list).
        preferred_dialect: active adapter dialect; expression picking
            prefers it, then ANSI_SQL, then the first available.

    Raises:
        ValueError: no `semantic_model` entry or a metric has no
            expression (structural problems, not per-metric drops).
    """
    data = yaml.safe_load(text) or {}
    models = data.get("semantic_model") or []
    if not models:
        raise ValueError("no semantic_model found in OSSIE YAML")
    if len(models) > 1:
        logger.warning("OSSIE file has %d semantic models; using the first", len(models))
    top = models[0]
    if not isinstance(top, dict):
        raise ValueError("semantic_model entry must be a mapping")
    version = str(data.get("version") or "").strip()

    # 脱敏策略挂在**模型入口**下(与 time_spine / ai_context 同层):文档顶层
    # 是被忽略的,而静默忽略 = 静默不脱敏 —— 出声,并在 lint 侧拦写盘。
    if "masking" in data and "masking" not in top:
        logger.warning(
            "masking at document top level is ignored; declare it under "
            "semantic_model[0] (same level as time_spine)")

    # 对象视图声明同样只认**声明位置**:entity 在 dataset 子块下(不是模型级、
    # 更不是文档根),hierarchies 在 semantic_model[0] 下。放错层静默忽略会
    # 让「对象看起来声明了、投影里却没有」成为一次无痕的失败 —— 出声,并在
    # lint 侧拦写盘(与 masking/topics 同一条双守卫先例)。
    if "entity" in data and "entity" not in top:
        logger.warning(
            "entity at document top level is ignored; declare it under the "
            "dataset it belongs to (semantic_model[0].datasets[].entity)")
    if "hierarchies" in data and "hierarchies" not in top:
        logger.warning(
            "hierarchies at document top level is ignored; declare it under "
            "semantic_model[0] (same level as time_spine)")

    declared = {d.get("name") for d in top.get("datasets", []) or [] if d.get("name")}

    datasets: list[SemanticDataset] = []
    for d in top.get("datasets", []) or []:
        if not isinstance(d, dict) or not d.get("name"):
            continue
        name = d["name"]
        _, ds_synonyms, ds_examples = _ai_context(d)
        fields: list[SemanticField] = []
        for f in d.get("fields", []) or []:
            if not isinstance(f, dict) or not f.get("name"):
                continue
            try:
                expr = _expression_of(f, preferred_dialect)
            except ValueError as e:
                logger.warning(
                    "Dropping field %s.%s: %s", name, f.get("name"), e)
                continue
            dt = f.get("datatype") or None
            dim = f.get("dimension")
            _, f_synonyms, f_examples = _ai_context(f)
            # P5.1:语义角色 + 枚举 display 字典(均可选;缺省由生成/消费端推导)
            role = str(f.get("semantic_role") or "").strip().lower()
            display = f.get("enum_display") or {}
            enum_display = {
                str(k): str(v) for k, v in display.items()
                if isinstance(display, dict)
            } if isinstance(display, dict) else {}
            # 值语义字典(多标签):ai_context.value_aliases → {code: [别名]}。
            # 兼容 "code: 别名,别名" 的紧凑形式与已解析的 list 形式。
            raw_aliases = {}
            ai = f.get("ai_context") or {}
            if isinstance(ai, dict):
                raw_aliases = ai.get("value_aliases") or {}
            value_aliases: dict[str, list[str]] = {}
            for code, labels in raw_aliases.items():
                if not isinstance(labels, (list, tuple, set)):
                    labels = re.split(r"[,，;]", str(labels))
                cleaned = [str(ln).strip() for ln in labels if str(ln).strip()]
                if cleaned:
                    value_aliases[str(code)] = cleaned
            fields.append(SemanticField(
                name=f["name"],
                expression=expr,
                datatype=(str(dt) if dt else None),
                is_time=_resolve_is_time(dim, dt),
                description=f.get("description", "") or "",
                synonyms=f_synonyms,
                semantic_role=role,
                enum_display=enum_display,
                value_aliases=value_aliases,
                # 列的实际取值(probe 的结构事实):缺省/坏形状 → 空列表,
                # 存量模型(无该键)一字不变。它只是数据,不参与角色推导。
                values=_clean_values(f.get("values")),
                label=str(f.get("label", "") or "").strip(),
                examples=f_examples,
                custom_extensions=_clean_extensions(f.get("custom_extensions")),
                # 值域归一化到小写:解析结果只有**一种**拼写,比较/展示/往返
                # 都无歧义;门禁按同一规则判(大小写不敏感),不会出现「门禁
                # 放过、运行时看不懂」这种两处不一致。
                # 裸 ``mask: null`` 是 None → 空串(不脱敏);猜成 null 模式会
                # 让「作者本来没写」也变成最强脱敏,由 lint 拦下而不是猜。
                mask=str(f.get("mask") or "").strip().lower(),
            ))
        datasets.append(SemanticDataset(
            name=name,
            source=str(d.get("source") or ""),
            primary_key=[str(pk) for pk in (d.get("primary_key") or [])],
            unique_keys=[
                [str(k) for k in keys]
                for keys in (d.get("unique_keys") or [])
                if isinstance(keys, list) and keys
            ],
            row_filter=str(d.get("row_filter") or "").strip(),
            description=d.get("description", "") or "",
            synonyms=ds_synonyms,
            fields=fields,
            examples=ds_examples,
            custom_extensions=_clean_extensions(d.get("custom_extensions")),
            # 业务实体声明(设计 §4.1):缺省/坏形状 → None(未声明)。
            entity=_entity_declaration(d.get("entity")),
        ))

    relationships: list[SemanticRelationship] = []
    for r in top.get("relationships", []) or []:
        if not isinstance(r, dict) or not r.get("name"):
            continue
        from_cols = [str(c) for c in (r.get("from_columns") or [])]
        to_cols = [str(c) for c in (r.get("to_columns") or [])]
        if r.get("from") not in declared or r.get("to") not in declared:
            logger.warning(
                "Dropping relationship %s: endpoint not in declared datasets",
                r.get("name"),
            )
            continue
        if not from_cols or len(from_cols) != len(to_cols):
            logger.warning(
                "Dropping relationship %s: from_columns/to_columns mismatch",
                r.get("name"),
            )
            continue
        _, r_examples, _ = _ai_context(r)
        relationships.append(SemanticRelationship(
            name=r["name"],
            from_=str(r["from"]),
            to=str(r["to"]),
            from_columns=from_cols,
            to_columns=to_cols,
            cardinality=str(r.get("cardinality") or "").strip().upper(),
            fan_out=str(r.get("fan_out") or "").strip().lower(),
            examples=r_examples,
            custom_extensions=_clean_extensions(r.get("custom_extensions")),
            # 边显示名(设计 §4.2):纯展示字段,缺省空串 = 未声明
            # (读侧回退列名连接串)。
            display_name=str(r.get("display_name") or "").strip(),
        ))

    ai = top.get("ai_context") or {}
    instructions_txt = ai.get("instructions", "") if isinstance(ai, dict) else ""
    model_examples = ai.get("examples", []) if isinstance(ai, dict) else []

    spine = None
    ts = top.get("time_spine") or {}
    if isinstance(ts, dict) and ts.get("field"):
        grain = str(ts.get("granularity") or "month").strip().lower()
        if grain not in {"year", "quarter", "month", "week", "day"}:
            logger.warning("time_spine.granularity %r invalid; defaulting to month", grain)
            grain = "month"
        fill = str(ts.get("fill") or "none").strip().lower()
        if fill not in {"none", "0", "previous"}:
            fill = "none"
        spine = TimeSpine(
            field=str(ts["field"]).strip(),
            granularity=grain,
            fill=fill,
        )

    # 模型级脱敏策略(缺省 = 惰性:A11 存量模型行为不变)
    masking = _masking_policy(top.get("masking"))

    # 业务主题域(Trove 扩展,可选段)。与 masking 同层:`semantic_model[0]`
    # 下。写在文档顶层会被忽略 —— 忽略一个**收敛边界**比忽略一个展示字段
    # 更危险(主题看起来生效了,范围其实没收),所以出声 + lint 拦写盘。
    if "topics" in data and "topics" not in top:
        logger.warning(
            "topics at document top level is ignored; declare it under "
            "semantic_model[0] (same level as time_spine)")
    topics: list[TopicDomain] = []
    for t in top.get("topics", []) or []:
        if not isinstance(t, dict) or not str(t.get("name") or "").strip():
            continue
        topics.append(TopicDomain(
            name=str(t["name"]).strip(),
            description=str(t.get("description") or ""),
            synonyms=_clean_names(t.get("synonyms")),
            datasets=_clean_names(t.get("datasets")),
            metrics=_clean_names(t.get("metrics")),
            examples=_clean_names(t.get("examples")),
            custom_extensions=_clean_extensions(t.get("custom_extensions")),
        ))

    # 钻取层级(设计 §4.3):模型级有序块,形态模板同 topics —— 可选段、
    # 空名条目丢弃、坏形状不猜(levels 走 _clean_names 保序去重)。级别字段
    # 的解析性(须在 dataset 的 fields 里)是 lint 的硬错,不在解析层判。
    hierarchies: list[Hierarchy] = []
    for h in top.get("hierarchies", []) or []:
        if not isinstance(h, dict) or not str(h.get("name") or "").strip():
            continue
        hierarchies.append(Hierarchy(
            name=str(h["name"]).strip(),
            display_name=str(h.get("display_name") or "").strip(),
            dataset=str(h.get("dataset") or "").strip(),
            levels=_clean_names(h.get("levels")),
        ))

    metrics: list[SemanticMetric] = []
    for m in top.get("metrics", []) or []:
        # 与原实现一致:metric 缺可用的 expression 是结构性问题 → 抛错
        # (provider 层 last-known-good 兜底);非法/坏结构的表达式才逐条丢弃。
        expression = _expression_of(m, preferred_dialect)
        _, synonyms, examples = _ai_context(m)
        m_datatype = m.get("datatype") or None
        metrics.append(SemanticMetric(
            name=m.get("name", ""),
            expression=expression,
            synonyms=synonyms,
            datasets=_dataset_refs(expression, declared),
            definition=m.get("description", "") or "",
            metric_type=str(m.get("type") or "").strip().lower(),
            filter=str(m.get("filter") or "").strip(),
            agg_time_dimension=str(m.get("agg_time_dimension") or "").strip(),
            non_additive=bool(m.get("non_additive") or False),
            datatype=(str(m_datatype) if m_datatype else None),
            examples=examples,
            custom_extensions=_clean_extensions(m.get("custom_extensions")),
        ))

    return SemanticModel(
        name=top.get("name", "") or "",
        description=top.get("description", "") or "",
        instructions=instructions_txt,
        metrics=metrics,
        datasets=datasets,
        relationships=relationships,
        version=version,
        examples=model_examples,
        custom_extensions=_clean_extensions(top.get("custom_extensions")),
        time_spine=spine,
        masking=masking,
        topics=topics,
        hierarchies=hierarchies,
    )
