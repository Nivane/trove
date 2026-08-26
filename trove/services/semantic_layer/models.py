"""Semantic layer models shared by parsers and consumers.

Models mirror the Apache Ossie core spec subset we consume: datasets
(with fields/primary keys), relationships (the declared join graph) and
metrics (business phrase → aggregate SQL expression). Metrics map 1:1
onto TermHit for retrieval (see kb.service.search_terms); datasets and
relationships are the deterministic structure layer that later stages
compile queries from (see services/kb/semantic_gen.py).
"""
from dataclasses import dataclass, field


@dataclass
class SemanticMetric:
    """A business metric with synonyms, SQL expression and source datasets.

    datasets: logical dataset names referenced by the expression
        (`dataset.field`); empty means table-agnostic (no anchoring).

    metric_type: OSSIE ``type`` — "" | "simple" | "derived" | "ratio"
        (ratio 按 derived 处理)。derived/ratio 的表达式可引用其他已声明
        metric 名(MetricFlow 风格),编译期递归内联。
    """

    name: str
    expression: str
    synonyms: list[str] = field(default_factory=list)
    datasets: list[str] = field(default_factory=list)
    definition: str = ""
    metric_type: str = ""
    # metric 级行级过滤(measure filter):如 ``status = 'A'``。编译期并入
    # WHERE(列须解析到本 metric 锚定数据集的已声明字段,否则保守 MISS)。
    filter: str = ""
    # 聚合时间维度:该 metric 按哪个时间字段聚合(``loan.date`` 或裸列名)。
    # 时间范围注入时优先用它,解决"matched 内多时间字段无法判定"的覆盖损失。
    agg_time_dimension: str = ""
    # 非加性标记:count distinct / ratio 等不可再加总的度量。lint 用于
    # 警告"被其他度量再聚合",本身不阻断编译。
    non_additive: bool = False


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
        "POPLATEK MESICNE" → "monthly")。
    """

    name: str
    expression: str
    datatype: str | None = None
    is_time: bool = False
    description: str = ""
    synonyms: list[str] = field(default_factory=list)
    semantic_role: str = ""  # identifier | measure | dimension | enum | time
    enum_display: dict[str, str] = field(default_factory=dict)


@dataclass
class SemanticRelationship:
    """A declared join edge: ``from_`` = many side, ``to`` = one side.

    from_columns/to_columns are ordered key pairs (composite joins
    supported); the many→one direction maps onto the OSSIE
    ``relationships`` block and the MetricFlow convention that avoids
    fan-out joins.

    cardinality: 从 ``to`` 侧看 ``from`` 侧(ER 惯例)——
        "1:N"=一个 to 对应多个 from(默认,由 many→one 构造推断)、
        "1:1"、显式 "M:N" 表示多对多:编译器将拒绝经它编译(编译期拒
        fan-out,交回 LLM 通道 + 规则链兜底)。
    """

    name: str
    from_: str
    to: str
    from_columns: list[str] = field(default_factory=list)
    to_columns: list[str] = field(default_factory=list)
    cardinality: str = ""  # ""(安全,默认) | "1:N" | "1:1" | "M:N"


@dataclass
class SemanticDataset:
    """A logical dataset: physical table + declared fields + keys."""

    name: str
    source: str = ""  # physical table reference (schema.table)
    primary_key: list[str] = field(default_factory=list)
    description: str = ""
    synonyms: list[str] = field(default_factory=list)
    fields: list[SemanticField] = field(default_factory=list)


@dataclass
class SemanticModel:
    """One parsed semantic model (OSSIE `semantic_model` entry)."""

    name: str = ""
    description: str = ""
    instructions: str = ""  # model-level ai_context.instructions
    metrics: list[SemanticMetric] = field(default_factory=list)
    datasets: list[SemanticDataset] = field(default_factory=list)
    relationships: list[SemanticRelationship] = field(default_factory=list)
