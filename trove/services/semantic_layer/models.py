"""Semantic layer models shared by parsers and consumers.

Semantic metrics are the live, external counterpart to KB terms: a
business phrase → SQL mapping injected into the gen_sql prompt as
context. Fields mirror the Apache Ossie core spec subset we consume
and map 1:1 onto TermHit for retrieval (see kb.service.search_terms).
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
class SemanticModel:
    """One parsed semantic model (OSSIE `semantic_model` entry)."""

    name: str = ""
    description: str = ""
    instructions: str = ""  # model-level ai_context.instructions
    metrics: list[SemanticMetric] = field(default_factory=list)
