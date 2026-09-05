"""Semantic layer models shared by parsers and consumers.

Models mirror the Apache Ossie core spec (v0.2.0.dev0) we consume:
datasets (with fields/primary keys/unique keys), relationships (the
declared join graph), metrics (business phrase → aggregate SQL
expression), plus the extensibility/context surface (custom_extensions,
ai_context.examples, labels). Metrics map 1:1 onto TermHit for retrieval
(see kb.service.search_terms); datasets and relationships are the
deterministic structure layer that later stages compile queries from
(see services/kb/semantic_gen.py).
"""
from dataclasses import dataclass, field


def _clean_extensions(raw: list | None) -> list[dict]:
    """custom_extensions 净化:只保留 {vendor_name, data} 形式且 vendor_name
    非空的条目(空 vendor_name 是坏条目,lint 也会标)。"""
    out: list[dict] = []
    for e in raw or []:
        if not isinstance(e, dict):
            continue
        vendor = str(e.get("vendor_name") or "").strip()
        if not vendor:
            continue
        out.append({"vendor_name": vendor, "data": e.get("data", "")})
    return out


@dataclass
class SemanticMetric:
    """A business metric with synonyms, SQL expression and source datasets.

    datasets: logical dataset names referenced by the expression
        (`dataset.field`); empty means table-agnostic (no anchoring).

    metric_type: OSSIE ``type`` — "" | "simple" | "derived" | "ratio"
        (ratio 按 derived 处理)。derived/ratio 的表达式可引用其他已声明
        metric 名(MetricFlow 风格),编译期递归内联。

    datatype: OSSIE DataType of the metric's value (Decimal/Float/...).
    examples: OSSIE ai_context.examples — 该度量的示例问句(AI 上下文)。
    custom_extensions: OSSIE vendor 扩展(vendor_name + data,透传保留)。
    """

    name: str
    expression: str
    synonyms: list[str] = field(default_factory=list)
    datasets: list[str] = field(default_factory=list)
    definition: str = ""
    metric_type: str = ""
    # metric 级行级过滤(measure filter):如 ``status = 'A'``。编译期并入
    # WHERE(列须解析到本 metric 锚定数据集的已声明字段,否则保守 MISS)。
    # 建模约束:对已声明 enum 字段的等值过滤应走维度过滤(lint 拦截)。
    filter: str = ""
    # 聚合时间维度:该 metric 按哪个时间字段聚合(``loan.date`` 或裸列名)。
    # 时间范围注入时优先用它,解决"matched 内多时间字段无法判定"的覆盖损失。
    agg_time_dimension: str = ""
    # 非加性标记:count distinct / ratio 等不可再加总的度量。lint 用于
    # 警告"被其他度量再聚合",本身不阻断编译。
    non_additive: bool = False
    datatype: str | None = None
    examples: list[str] = field(default_factory=list)
    custom_extensions: list[dict] = field(default_factory=list)


@dataclass
class SemanticField:
    """A row-level attribute (dimension / filter) on a dataset.

    expression: scalar (non-aggregate) SQL expression, dialect-picked.
    is_time: OSSIE temporal-role flag — defaults to True for temporal
        datatypes (Date/Time/DateTime/DateTimeTz) unless overridden.
    semantic_role: Palantir 风格属性角色 —— identifier / measure /
        dimension / enum / time。让字段候选检索与编译器不用猜"这列
        能不能聚合/该不该分组"(P5.1)。
    enum_display: 枚举列的 ``code → 人类可读词`` 字典(过滤值锚定用,
        "POPLATEK MESICNE" → "monthly")。运行时把人类值归一成 code。
    label: OSSIE ``label`` — 分类标签(AI/UI 归类用,透传)。
    examples: OSSIE ai_context.examples — 该字段的示例问句。
    custom_extensions: OSSIE vendor 扩展(透传保留)。
    """

    name: str
    expression: str
    datatype: str | None = None
    is_time: bool = False
    description: str = ""
    synonyms: list[str] = field(default_factory=list)
    semantic_role: str = ""  # identifier | measure | dimension | enum | time
    enum_display: dict[str, str] = field(default_factory=dict)
    label: str = ""
    examples: list[str] = field(default_factory=list)
    custom_extensions: list[dict] = field(default_factory=list)


@dataclass
class SemanticRelationship:
    """A declared join edge: ``from_`` = many side, ``to`` = one side.

    from_columns/to_columns are ordered key pairs (composite joins
    supported); the many→one direction maps onto the OSSIE
    ``relationships`` block and the MetricFlow convention that avoids
    fan-out joins.

    cardinality: 从 ``to`` 侧看 ``from`` 侧(ER 惯例)——
        "1:N"=一个 to 对应多个 from(默认,由 many→one 构造推断)、
        "1:1"、显式 "M:N" 表示多对多。

    fan_out: M:N 边的显式豁免方式——空(默认)= 编译期拒 fan-out
        (行倍增,回交 LLM + 规则链兜底);``dedup`` = 编译期把 from
        侧包成 ``SELECT DISTINCT *`` 子查询,消除关联/桥接表里的整行
        重复(精确重复行是唯一安全、免额外建模的去重情形);
        ``bridge:<dataset>`` 保留(未来:走已预聚合桥表,支持同键多行)。
        genuinely 多对多(同键多行且每行语义不同)应建模为 1:N + 中间
        表(编译器完全支持),而不是豁免。
    """

    name: str
    from_: str
    to: str
    from_columns: list[str] = field(default_factory=list)
    to_columns: list[str] = field(default_factory=list)
    cardinality: str = ""  # ""(安全,默认) | "1:N" | "1:1" | "M:N"
    fan_out: str = ""  # "" | "dedup" | "bridge:<dataset>"
    examples: list[str] = field(default_factory=list)
    custom_extensions: list[dict] = field(default_factory=list)


@dataclass
class SemanticDataset:
    """A logical dataset: physical table + declared fields + keys.

    unique_keys: OSSIE ``unique_keys`` — 数组的数组,每个都是唯一键
        (单列或复合)。与 primary_key 同构,消费端可作为联表/去重依据。
    """

    name: str
    source: str = ""  # physical table reference (schema.table)
    primary_key: list[str] = field(default_factory=list)
    unique_keys: list[list[str]] = field(default_factory=list)
    description: str = ""
    synonyms: list[str] = field(default_factory=list)
    fields: list[SemanticField] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    custom_extensions: list[dict] = field(default_factory=list)


@dataclass
class TimeSpine:
    """模型级时间轴声明(OSSIE ``time_spine``):空档补全的时间序列。

    field: 声明的时间字段(``dataset.field`` 或裸列名)。
    granularity: 序列粒度(year/quarter/month/week/day)。
    fill: 缺期填充策略——"none"(默认,显示 NULL) | "0" | "previous"。
    """

    field: str = ""
    granularity: str = "month"
    fill: str = "none"


@dataclass
class SemanticModel:
    """One parsed semantic model (OSSIE `semantic_model` entry).

    version: OSSIE 文档级 ``version``(semantic_model 上方,透传)。
    examples: model 级 ai_context.examples(示例问句)。
    custom_extensions: model 级 vendor 扩展(透传保留)。
    time_spine: 模型级时间轴声明(见 TimeSpine);空 = 不启用空档填充。
    """

    name: str = ""
    description: str = ""
    instructions: str = ""  # model-level ai_context.instructions
    metrics: list[SemanticMetric] = field(default_factory=list)
    datasets: list[SemanticDataset] = field(default_factory=list)
    relationships: list[SemanticRelationship] = field(default_factory=list)
    version: str = ""
    examples: list[str] = field(default_factory=list)
    custom_extensions: list[dict] = field(default_factory=list)
    time_spine: TimeSpine | None = None
