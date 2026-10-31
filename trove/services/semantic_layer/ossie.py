"""Apache Ossie semantic model parser (core spec, v0.2 flat root).

Reads the parts relevant to NL→SQL plus the spec's extensibility surface.
The document root is the model itself (OSSIE 0.2): `version` / `name` /
`datasets` / `relationships` / `metrics` / `custom_extensions`, plus Trove
extensions (`masking` / `topics` / `hierarchies` / `time_spine`) at the
same level. The legacy v0.1 `semantic_model: [model]` list root is
**refused** with a migration command — reading a newer/older root shape
with the wrong interpreter produces a *wrong* semantic model, not a
partially read one.

- `datasets` (source, primary_key, unique_keys, fields with datatype /
  dimension.is_time / label / synonyms) — table anchoring + the declared
  filter/group-by vocabulary;
- `relationships` (from → to, key pairs) — the declared join graph;
- `metrics` (name, multi-dialect aggregate expression, datatype,
  ai_context synonyms/examples) — business metrics;
- `custom_extensions` (vendor_name + data) — preserved/passed through;
- model-level `ai_context` (instructions / synonyms / examples) and the
  root `version` string (required, gated against `OSSIE_VERSION`);
- field-level `mask` + model-level `masking` (default_policy /
  bypass_scopes / hash_salt_ref) — the masking **declaration**; applying it
  to result sets is `authz/masking.py` (P4);
- field-level `values` (该列的实际取值,probe 的结构事实) — read as plain
  data; it never changes `semantic_role` nor feeds prompt rendering;
- dataset-level `entity` (display_name / natural_keys / aliases) and
  model-level `hierarchies` (name / display_name / dataset / levels) —
  the object-view declarations (Trove 扩展); relationship-level
  `display_name`. 全部可选,缺省 = 存量模型逐字节同前;`entity` 只认
  `datasets[].entity`,写在文档根出声但忽略。
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
#: 全部写入点(kb 结构生成 / flat↔OSSIE 桥接 / 变更暂存 / 导入落点)都经
#: :func:`stamp_ossie_version` 盖章。
OSSIE_VERSION = "0.2.0.dev0"

_ANSI = "ANSI_SQL"

#: 旧 v0.1 根形状的迁移命令(写进拒绝文案,保证可执行)。
MIGRATE_SEMANTICS_COMMAND = (
    "uv run python scripts/migrate_semantics_v02.py "
    "--kb-dir .trove/kb --all --write"
)

_LEGACY_ROOT_REFUSAL = (
    "旧版 v0.1 文档（semantic_model 列表包装）已不再支持；请先迁移："
    f"{MIGRATE_SEMANTICS_COMMAND}（或运行 /kb migrate 查看原因）"
)

#: 闭集版本比较器:数字段 + 可选 dev/a/beta/rc 阶段 + 可选数字。
#: 刻意不引入 ``packaging``(不是直接依赖),也**不猜**闭集外的写法。
_VERSION_RE = re.compile(
    r"^(?P<release>\d+(?:\.\d+)*)"
    r"(?:[.-]?(?P<stage>dev|alpha|a|beta|b|rc)[.-]?(?P<pre>\d*))?$",
    re.IGNORECASE,
)
_STAGE_RANK = {
    "dev": 0,
    "alpha": 1,
    "a": 1,
    "beta": 2,
    "b": 2,
    "rc": 3,
    None: 4,
}


class OssieFormatError(ValueError):
    """语义文档根形状/版本不可解释(读端拒绝或写端拒绝降级)。"""


def parse_version_key(text: object) -> tuple[tuple[int, ...], int, int] | None:
    """OSSIE ``version`` → 可比较键;闭集外返回 ``None``(调用方拒绝)。

    ``dev < alpha/a < beta/b < rc < release``;缺省数字段在比较时补 0,
    因此 ``0.2`` == ``0.2.0``。``0.2.0`` **新于** ``0.2.0.dev0``。
    """
    if not isinstance(text, str):
        return None
    raw = text.strip()
    m = _VERSION_RE.match(raw)
    if m is None:
        return None
    release = tuple(int(part) for part in m.group("release").split("."))
    stage = m.group("stage")
    rank = _STAGE_RANK[stage.lower() if stage else None]
    return release, rank, int(m.group("pre") or 0)


def _version_cmp(left: tuple[tuple[int, ...], int, int],
                 right: tuple[tuple[int, ...], int, int]) -> bool:
    """补零后比较,避免 ``0.2`` 被当成短于 ``0.2.0``。"""
    width = max(len(left[0]), len(right[0]))
    a = left[0] + (0,) * (width - len(left[0]))
    b = right[0] + (0,) * (width - len(right[0]))
    return (a, left[1], left[2]) > (b, right[1], right[2])


def is_legacy_document(data: object) -> bool:
    """v0.1 列表包装(``semantic_model: [...]``)检测。"""
    return isinstance(data, dict) and "semantic_model" in data


def is_newer_document(data: object) -> bool:
    """文档声明的 OSSIE 版本是否比本程序支持的新。

    新版本**永远不可改写**(``force`` 也不行):覆盖它等于用旧解释毁掉一份
    新资产。其它拒绝(legacy / 缺版本 / 坏版本)可以由人显式 ``force``
    整体替换。
    """
    if not isinstance(data, dict):
        return False
    key = parse_version_key(data.get("version"))
    current = parse_version_key(OSSIE_VERSION)
    return key is not None and current is not None and _version_cmp(key, current) > 0


def document_refusal(data: object) -> str | None:
    """语义文档能否按当前解释读取;不能则返回可操作原因。

    ``None`` = 可读。空文档(``{}`` / ``None``)是"尚未落盘"的新文档,
    写路径会盖章,读路径按空模型处理。非空文档必须带可解析的
    ``version``,且不得比 :data:`OSSIE_VERSION` 新。
    """
    if not data:
        return None
    if not isinstance(data, dict):
        return "OSSIE 文档根必须是映射（mapping）"
    if is_legacy_document(data):
        return _LEGACY_ROOT_REFUSAL
    version = data.get("version")
    if version is None or (isinstance(version, str) and not version.strip()):
        return "缺少 OSSIE version（0.2 扁平根要求非空字符串）"
    if not isinstance(version, str):
        return "OSSIE version 必须是字符串"
    key = parse_version_key(version)
    if key is None:
        return f"无法解析的 OSSIE version: {version!r}"
    if is_newer_document(data):
        return (
            f"文件声明的 OSSIE version {version} 比本程序支持的 "
            f"{OSSIE_VERSION} 更新；请升级 Trove 或回退文件版本"
        )
    return None


def stamp_ossie_version(doc: dict) -> dict:
    """写路径统一盖章:返回 ``version`` 在第一键的扁平 0.2 文档。

    缺 ``version`` 的**新构造**文档补当前版本;更老的扁平版升级到当前;
    更新版本/坏版本/legacy 包装拒绝(绝不静默降级)。
    """
    if not isinstance(doc, dict):
        raise OssieFormatError("OSSIE 文档根必须是映射（mapping）")
    if is_legacy_document(doc):
        raise OssieFormatError(_LEGACY_ROOT_REFUSAL)
    version = doc.get("version")
    if version is not None:
        if not isinstance(version, str) or not version.strip():
            raise OssieFormatError("OSSIE version 必须是非空字符串")
        key = parse_version_key(version)
        if key is None:
            raise OssieFormatError(f"无法解析的 OSSIE version: {version!r}")
        current = parse_version_key(OSSIE_VERSION)
        if current is not None and _version_cmp(key, current) > 0:
            raise OssieFormatError(
                f"不能把更新的 OSSIE version {version} 写成本程序支持的 "
                f"{OSSIE_VERSION}")
    rest = {key: value for key, value in doc.items() if key != "version"}
    return {"version": OSSIE_VERSION, **rest}

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
        text: OSSIE YAML content (spec: 0.2 flat root; the model itself).
        preferred_dialect: active adapter dialect; expression picking
            prefers it, then ANSI_SQL, then the first available.

    Raises:
        ValueError: the root is unreadable or a metric has no expression
            (structural problems, not per-metric drops).
    """
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise OssieFormatError("OSSIE document root must be a mapping")
    # 先过版本门:根形状/版本不认识的文档**一个字段都不读** —— 旧代码把
    # 不认识的文档当认识的读,产出的不是「少读几个字段」,是一个错的语义
    # 模型,它会进编译器、进权威 SQL、进答案。
    refusal = document_refusal(data)
    if refusal is not None:
        raise OssieFormatError(refusal)
    top = data
    version = str(data.get("version") or "").strip()

    # `entity` 只认**声明位置**:datasets[].entity。写在文档根静默忽略会让
    # 「对象看起来声明了、投影里却没有」成为一次无痕的失败 —— 出声,并在
    # lint 侧拦写盘(与 masking/topics 同一条双守卫先例)。
    if "entity" in data:
        logger.warning(
            "entity at document root is ignored; declare it under "
            "datasets[].entity (the dataset it belongs to)")

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

    # 业务主题域(Trove 扩展,可选段):0.2 扁平根下就在模型根。
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
