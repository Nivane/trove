"""Agent-facing ontology context — a targeted projection of the semantic model.

MCP already exposes the raw ``semantics.yml`` as a resource, but an agent
needs a *targeted* context: which objects exist, what their fields and links
are, which metrics are anchored to them, and which values are legal. This
module is the deterministic projection behind ``list_objects`` /
``describe_object``. It adds no persistence and no write path.

Discipline (same as :mod:`object_graph`):

* pure functions, zero I/O / zero LLM, declaration order preserved;
* absence is meaningful: optional keys are omitted rather than filled with
  defaults (the read-side fallbacks — display name / natural keys / link
  display name — stay in :func:`build_object_graph` so every surface gives
  the same answer);
* resolution never guesses: an object ref resolves by dataset name, entity
  display name, entity alias, or dataset synonym; multiple hits raise
  :class:`ObjectAmbiguous` instead of picking one.
"""
from __future__ import annotations

from typing import Any

from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticField,
    SemanticMetric,
    SemanticModel,
)
from trove.services.semantic_layer.object_graph import (
    build_object_graph,
    entity_display_name,
)
from trove.services.semantic_layer.topics import resolve_topic


class ObjectLookupError(ValueError):
    """An object reference could not be resolved deterministically."""


class ObjectNotFound(ObjectLookupError):
    """No declared dataset matches the reference."""

    def __init__(self, ref: str, available: list[str]) -> None:
        self.ref = ref
        self.available = available
        super().__init__(
            f"object not found: {ref!r} (available: {', '.join(available) or 'none'})"
        )


class ObjectAmbiguous(ObjectLookupError):
    """The reference matches more than one dataset — never pick one."""

    def __init__(self, ref: str, candidates: list[str]) -> None:
        self.ref = ref
        self.candidates = candidates
        super().__init__(
            f"object ref {ref!r} is ambiguous between: {', '.join(candidates)}"
        )


def _terms(dataset: SemanticDataset) -> set[str]:
    """All declared names that can address a dataset (normalized)."""
    entity = dataset.entity
    raw = [dataset.name, *dataset.synonyms]
    if entity is not None:
        raw.append(entity.display_name)
        raw.extend(entity.aliases)
    return {t.strip().casefold() for t in raw if t and t.strip()}


def resolve_object(model: SemanticModel, ref: str) -> SemanticDataset:
    """Resolve an object reference to exactly one dataset.

    Matching is case-insensitive over dataset name, dataset synonyms, entity
    display name and entity aliases. Ambiguity is an error, not a choice.
    """
    wanted = (ref or "").strip().casefold()
    if not wanted:
        raise ObjectNotFound(ref, [d.name for d in model.datasets])
    hits = [d for d in model.datasets if wanted in _terms(d)]
    if not hits:
        raise ObjectNotFound(ref, [d.name for d in model.datasets])
    if len(hits) > 1:
        raise ObjectAmbiguous(ref, [d.name for d in hits])
    return hits[0]


def _field_context(field: SemanticField) -> dict[str, Any]:
    """One field / property, omitting absent declarations.

    ``expression`` is included only when it differs from the field name — a
    computed attribute (``CONCAT(first, last)``) is semantic; an identity
    expression is noise the compiler already knows.
    """
    out: dict[str, Any] = {"name": field.name}
    expression = (field.expression or "").strip()
    if expression and expression.casefold() != field.name.strip().casefold():
        out["expression"] = expression
    if field.datatype:
        out["datatype"] = field.datatype
    if field.semantic_role:
        out["role"] = field.semantic_role
    if field.is_time:
        out["is_time"] = True
    if field.description:
        out["description"] = field.description
    if field.synonyms:
        out["synonyms"] = list(field.synonyms)
    if field.enum_display:
        out["enum_display"] = dict(field.enum_display)
    if field.value_aliases:
        out["value_aliases"] = {
            code: list(aliases) for code, aliases in field.value_aliases.items()
        }
    if field.values:
        out["values"] = list(field.values)
    if field.label:
        out["label"] = field.label
    if field.examples:
        out["examples"] = list(field.examples)
    if field.mask and field.mask.strip().lower() != "none":
        out["mask"] = field.mask
    return out


def _metric_context(metric: SemanticMetric, *, detail: bool) -> dict[str, Any]:
    """One metric summary; ``detail`` adds the expression and model hints."""
    out: dict[str, Any] = {"name": metric.name}
    if metric.definition:
        out["definition"] = metric.definition
    if metric.synonyms:
        out["synonyms"] = list(metric.synonyms)
    out["datasets"] = list(metric.datasets)
    if metric.metric_type:
        out["type"] = metric.metric_type
    if detail:
        out["expression"] = metric.expression
        if metric.filter:
            out["filter"] = metric.filter
        if metric.agg_time_dimension:
            out["agg_time_dimension"] = metric.agg_time_dimension
        if metric.non_additive:
            out["non_additive"] = True
        if metric.datatype:
            out["datatype"] = metric.datatype
        if metric.examples:
            out["examples"] = list(metric.examples)
    return out


def _topic_context(model: SemanticModel, topic: Any, *, detail: bool) -> dict[str, Any]:
    scope = resolve_topic(model, topic.name)
    out: dict[str, Any] = {
        "name": topic.name,
        "datasets": list(topic.datasets),
        "scope": list(scope.scope or []),
        "status": scope.status,
        "metrics": list(topic.metrics),
    }
    if topic.description:
        out["description"] = topic.description
    if topic.synonyms:
        out["synonyms"] = list(topic.synonyms)
    if detail and topic.examples:
        out["examples"] = list(topic.examples)
    return out


def build_model_overview(model: SemanticModel | None) -> dict[str, Any]:
    """Model-level ontology map for agent discovery.

    Shape (optional keys are omitted when the declaration is absent)::

        {name, version?, description?, instructions?, examples?,
         objects: [{dataset, display_name, declared, natural_keys, aliases?,
                    description?, synonyms?, metrics, field_count}],
         links: [...], hierarchies: [...],
         metrics: [{name, definition?, synonyms?, datasets, type?}],
         topics: [...], time_spine?: {field, granularity, fill}}

    ``objects`` / ``links`` / ``hierarchies`` come from
    :func:`build_object_graph`, so display names and natural-key fallbacks
    are byte-identical to the admin object view and answer cards.
    """
    if model is None:
        return {
            "objects": [], "links": [], "hierarchies": [],
            "metrics": [], "topics": [],
        }

    graph = build_object_graph(model)
    datasets = {d.name: d for d in model.datasets}
    objects: list[dict[str, Any]] = []
    for item in graph["objects"]:
        enriched = dict(item)
        dataset = datasets.get(item["dataset"])
        if dataset is not None:
            if dataset.description:
                enriched["description"] = dataset.description
            if dataset.synonyms:
                enriched["synonyms"] = list(dataset.synonyms)
        objects.append(enriched)

    out: dict[str, Any] = {}
    if model.name:
        out["name"] = model.name
    if model.version:
        out["version"] = model.version
    if model.description:
        out["description"] = model.description
    if model.instructions:
        out["instructions"] = model.instructions
    if model.examples:
        out["examples"] = list(model.examples)
    out["objects"] = objects
    out["links"] = graph["links"]
    out["hierarchies"] = graph["hierarchies"]
    out["metrics"] = [_metric_context(m, detail=False) for m in model.metrics]
    out["topics"] = [
        _topic_context(model, t, detail=False) for t in model.topics
    ]
    if model.time_spine is not None and model.time_spine.field:
        out["time_spine"] = {
            "field": model.time_spine.field,
            "granularity": model.time_spine.granularity,
            "fill": model.time_spine.fill,
        }
    return out


def build_object_detail(
    model: SemanticModel, dataset: SemanticDataset,
) -> dict[str, Any]:
    """Object-centric context: properties, metrics, links, hierarchies, topics.

    This is the agent's equivalent of opening one object in the object view:
    the same declared coverage, with the fields and anchored metrics that the
    graph node only summarizes as counts and names.
    """
    graph = build_object_graph(model)
    base = next(
        (o for o in graph["objects"] if o["dataset"] == dataset.name), None,
    )
    detail: dict[str, Any] = dict(base or {
        "dataset": dataset.name,
        "display_name": entity_display_name(dataset),
        "declared": dataset.entity is not None,
    })
    if dataset.description:
        detail["description"] = dataset.description
    if dataset.synonyms:
        detail["synonyms"] = list(dataset.synonyms)
    if dataset.primary_key:
        detail["primary_key"] = list(dataset.primary_key)
    if dataset.unique_keys:
        detail["unique_keys"] = [list(key) for key in dataset.unique_keys]
    if dataset.row_filter:
        detail["row_filter"] = dataset.row_filter
    detail["fields"] = [_field_context(f) for f in dataset.fields]
    detail["metrics"] = [
        _metric_context(m, detail=True)
        for m in model.metrics if dataset.name in m.datasets
    ]

    links: list[dict[str, Any]] = []
    for link in graph["links"]:
        if link["from"] == dataset.name:
            links.append({**link, "direction": "outgoing"})
        elif link["to"] == dataset.name:
            links.append({**link, "direction": "incoming"})
    detail["links"] = links
    detail["hierarchies"] = [
        h for h in graph["hierarchies"] if h["dataset"] == dataset.name
    ]
    detail["topics"] = [
        _topic_context(model, t, detail=True)
        for t in model.topics if dataset.name in t.datasets
    ]
    if dataset.examples:
        detail["examples"] = list(dataset.examples)
    return detail
