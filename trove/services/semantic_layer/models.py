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
    """

    name: str
    expression: str
    synonyms: list[str] = field(default_factory=list)
    datasets: list[str] = field(default_factory=list)
    definition: str = ""


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
    """

    name: str
    from_: str
    to: str
    from_columns: list[str] = field(default_factory=list)
    to_columns: list[str] = field(default_factory=list)


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
