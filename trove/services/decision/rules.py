"""Decision-rule schema + lint — the declarative half of the decision layer.

A decision rule says *what to watch* in the semantic model's own vocabulary
(metric / dimension / filter, the same shapes ``/v1/semantic/query`` accepts)
and *when it counts as a problem* as a small boolean expression over the
comparison the engine computes (current vs baseline).

The rule body deliberately reuses ``SemanticQuery``'s shape rather than
inventing a parallel DSL: an author can paste the ``subject`` block into
``POST /v1/semantic/query`` and see whether it compiles and what it returns
before wiring it to a schedule. Lint's hard gate is exactly that test.

Rules live in ``.trove/kb/<datasource>/decisions.yml`` — inside the KB tree
that is already git-tracked, so every rule change is a reviewable commit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trove.services.decision.expr import (
    DecisionExprError,
    condition_variables,
    parse_condition,
)

SEVERITIES = ("info", "warning", "critical")
BASELINE_KINDS = ("prev_period", "yoy", "literal", "none")
SCOPES = ("aggregate", "per_dimension")
EMITS = ("any", "all", "top_k")

#: Variables that only exist when the rule groups by a dimension — a
#: condition referencing them on an aggregate rule would silently see Unknown.
_DIMENSION_ONLY = frozenset({"contribution", "dim"})


class RuleError(ValueError):
    """A rule document that must not be persisted or executed."""


@dataclass
class Baseline:
    kind: str = "none"
    value: float | None = None


@dataclass
class Subject:
    """The semantic query body — mirrors ``services.semantic_layer.query.SemanticQuery``."""

    metrics: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    filters: list[dict[str, Any]] = field(default_factory=list)
    time_grain: dict[str, str] | None = None
    limit: int | None = None


@dataclass
class DecisionRule:
    id: str
    name: str = ""
    enabled: bool = True
    severity: str = "warning"
    owner_role: str = ""
    window: str = ""
    subject: Subject = field(default_factory=Subject)
    baseline: Baseline = field(default_factory=Baseline)
    scope: str = "aggregate"
    emit: str = "any"
    top_k: int = 3
    conditions: list[str] = field(default_factory=list)
    condition_mode: str = "all"        # "all" (AND) | "any" (OR)

    def describe(self) -> str:
        return self.name or self.id


@dataclass
class DecisionDoc:
    rules: list[DecisionRule] = field(default_factory=list)
    version: int = 1
    digest: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


# ── parsing ──────────────────────────────────────────────────

def _as_float(raw: Any) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _parse_conditions(raw: Any) -> tuple[list[str], str]:
    """``conditions`` is either a list (AND) or ``{all: [...]}`` / ``{any: [...]}``."""
    if raw is None or raw == "":
        return [], "all"
    if isinstance(raw, list):
        return [str(c) for c in raw], "all"
    if isinstance(raw, dict):
        keys = [k for k in raw if k in ("all", "any")]
        if len(keys) != 1:
            raise RuleError(
                "conditions object must have exactly one of 'all' / 'any'")
        key = keys[0]
        items = raw.get(key) or []
        if not isinstance(items, list):
            raise RuleError(f"conditions.{key} must be a list")
        return [str(c) for c in items], key
    raise RuleError("conditions must be a list or an {all|any} object")


def parse_rule(raw: dict[str, Any]) -> DecisionRule:
    """One YAML rule mapping → ``DecisionRule`` (structure only, no lint)."""
    if not isinstance(raw, dict):
        raise RuleError(f"rule must be a mapping, got {type(raw).__name__}")
    rid = str(raw.get("id") or "").strip()
    if not rid:
        raise RuleError("rule is missing an 'id'")

    subject_raw = raw.get("subject") or {}
    if not isinstance(subject_raw, dict):
        raise RuleError(f"rule {rid}: 'subject' must be a mapping")
    tg = subject_raw.get("time_grain")
    subject = Subject(
        metrics=[str(m) for m in (subject_raw.get("metrics") or [])],
        dimensions=[str(d) for d in (subject_raw.get("dimensions") or [])],
        filters=[f for f in (subject_raw.get("filters") or []) if isinstance(f, dict)],
        time_grain=tg if isinstance(tg, dict) else None,
        limit=int(subject_raw["limit"]) if subject_raw.get("limit") is not None else None,
    )

    base_raw = raw.get("baseline") or {}
    if not isinstance(base_raw, dict):
        raise RuleError(f"rule {rid}: 'baseline' must be a mapping")
    baseline = Baseline(
        kind=str(base_raw.get("kind") or "none").strip().lower(),
        value=_as_float(base_raw.get("value")),
    )

    conditions, mode = _parse_conditions(raw.get("conditions"))

    return DecisionRule(
        id=rid,
        name=str(raw.get("name") or "").strip(),
        enabled=bool(raw.get("enabled", True)),
        severity=str(raw.get("severity") or "warning").strip().lower(),
        owner_role=str(raw.get("owner_role") or "").strip(),
        window=str(raw.get("window") or "").strip(),
        subject=subject,
        baseline=baseline,
        scope=str(raw.get("scope") or "aggregate").strip().lower(),
        emit=str(raw.get("emit") or "any").strip().lower(),
        top_k=int(raw.get("top_k") or 3),
        conditions=conditions,
        condition_mode=mode,
    )


def parse_document(data: dict[str, Any] | None) -> DecisionDoc:
    """Whole ``decisions.yml`` → ``DecisionDoc``. Duplicate ids are an error:
    a job references a rule by id, so a duplicate would make that reference
    ambiguous."""
    data = data or {}
    if not isinstance(data, dict):
        raise RuleError("decisions.yml must be a mapping")
    raw_rules = data.get("rules") or []
    if not isinstance(raw_rules, list):
        raise RuleError("'rules' must be a list")
    rules: list[DecisionRule] = []
    seen: set[str] = set()
    for raw in raw_rules:
        rule = parse_rule(raw)
        if rule.id in seen:
            raise RuleError(f"duplicate rule id: {rule.id!r}")
        seen.add(rule.id)
        rules.append(rule)
    try:
        version = int(data.get("version") or 1)
    except (TypeError, ValueError):
        raise RuleError("'version' must be an integer")
    return DecisionDoc(rules=rules, version=version)


# ── lint ─────────────────────────────────────────────────────

def lint_rule(rule: DecisionRule) -> list[str]:
    """Structural problems that must block a write or a job reference.

    Only checks what can be judged *without* the semantic model — whether the
    subject actually compiles is checked by the service, which owns the model.
    """
    issues: list[str] = []
    where = f"rule {rule.id!r}"

    if not rule.subject.metrics:
        issues.append(f"{where}: subject.metrics must name a metric")
    elif len(rule.subject.metrics) > 1:
        # The condition vocabulary is singular (`current` / `baseline` /
        # `delta`), so a second metric would have nowhere to bind. Rejecting
        # beats silently judging only the first.
        issues.append(
            f"{where}: exactly one metric per rule (got "
            f"{len(rule.subject.metrics)}) — 'current'/'delta' are singular; "
            "split into separate rules instead")
    if rule.severity not in SEVERITIES:
        issues.append(
            f"{where}: severity must be one of {', '.join(SEVERITIES)} "
            f"(got {rule.severity!r})")
    if rule.baseline.kind not in BASELINE_KINDS:
        issues.append(
            f"{where}: baseline.kind must be one of {', '.join(BASELINE_KINDS)} "
            f"(got {rule.baseline.kind!r})")
    if rule.baseline.kind == "literal" and rule.baseline.value is None:
        issues.append(f"{where}: baseline.kind 'literal' requires a numeric value")
    if rule.baseline.kind != "literal" and rule.baseline.value is not None:
        issues.append(
            f"{where}: baseline.value is only meaningful with kind 'literal'")
    if rule.scope not in SCOPES:
        issues.append(
            f"{where}: scope must be one of {', '.join(SCOPES)} (got {rule.scope!r})")
    if rule.emit not in EMITS:
        issues.append(
            f"{where}: emit must be one of {', '.join(EMITS)} (got {rule.emit!r})")
    if rule.emit != "top_k" and rule.top_k != 3:
        issues.append(f"{where}: top_k is only meaningful with emit 'top_k'")
    if rule.emit == "top_k" and rule.top_k < 1:
        issues.append(f"{where}: top_k must be >= 1")
    # `window` is a natural-language time expression resolved by the service
    # through parse_date; an unresolvable one is a runtime hard failure there,
    # not a lint issue, because it depends on the reference date.
    if rule.scope == "per_dimension" and not rule.subject.dimensions:
        issues.append(
            f"{where}: scope 'per_dimension' requires at least one subject dimension")
    if rule.scope == "aggregate" and rule.emit != "any":
        issues.append(
            f"{where}: emit is ignored when scope is 'aggregate' "
            "(a single row is either triggered or not)")
    if rule.scope == "aggregate" and rule.subject.dimensions:
        # The subject's dimensions *are* the group-by, so the query returns
        # one row per group while an aggregate rule looks up the single
        # ``""`` label — every variable reads Unknown and the rule can never
        # fire. Silently dead is the one outcome worth blocking here.
        issues.append(
            f"{where}: scope 'aggregate' judges one row but "
            f"subject.dimensions ({', '.join(rule.subject.dimensions)}) makes "
            "the query group — drop them, filter instead, or use scope "
            "'per_dimension'")

    if not rule.conditions:
        issues.append(f"{where}: at least one condition is required")

    for cond in rule.conditions:
        try:
            used = condition_variables(cond)
        except DecisionExprError as e:
            issues.append(f"{where}: bad condition {cond!r}: {e}")
            continue
        missing = sorted(used & _DIMENSION_ONLY)
        if missing and rule.scope != "per_dimension":
            issues.append(
                f"{where}: condition {cond!r} uses {', '.join(missing)}, which "
                "only exists when scope is 'per_dimension'")
        if rule.baseline.kind in ("none",) and "baseline" in used:
            issues.append(
                f"{where}: condition {cond!r} uses 'baseline' but "
                "baseline.kind is 'none'")
        if rule.baseline.kind in ("none",) and any(
                v in used for v in ("delta", "delta_pct")):
            issues.append(
                f"{where}: condition {cond!r} uses a delta but "
                "baseline.kind is 'none'")

    return issues


def rule_to_dict(rule: DecisionRule) -> dict[str, Any]:
    """``DecisionRule`` → the YAML mapping it was parsed from.

    Round-trips through ``parse_rule``: the API accepts rule bodies as JSON
    and writes them back as YAML, so the two directions must agree on the
    shape or every save through the UI would reshape the file.
    """
    out: dict[str, Any] = {"id": rule.id}
    if rule.name:
        out["name"] = rule.name
    if not rule.enabled:
        out["enabled"] = False
    out["severity"] = rule.severity
    if rule.owner_role:
        out["owner_role"] = rule.owner_role
    if rule.window:
        out["window"] = rule.window
    subject: dict[str, Any] = {"metrics": list(rule.subject.metrics)}
    if rule.subject.dimensions:
        subject["dimensions"] = list(rule.subject.dimensions)
    if rule.subject.filters:
        subject["filters"] = [dict(f) for f in rule.subject.filters]
    if rule.subject.time_grain:
        subject["time_grain"] = dict(rule.subject.time_grain)
    if rule.subject.limit is not None:
        subject["limit"] = rule.subject.limit
    out["subject"] = subject
    baseline: dict[str, Any] = {"kind": rule.baseline.kind}
    if rule.baseline.value is not None:
        baseline["value"] = rule.baseline.value
    out["baseline"] = baseline
    if rule.scope != "aggregate":
        out["scope"] = rule.scope
    if rule.emit != "any":
        out["emit"] = rule.emit
    if rule.emit == "top_k":
        out["top_k"] = rule.top_k
    conds = list(rule.conditions)
    out["conditions"] = {rule.condition_mode: conds} if rule.condition_mode != "all" \
        else conds
    return out


def lint_document(doc: DecisionDoc) -> list[str]:
    """Every issue that must block a write, flattened across the document.

    A document with no rules is *not* an issue: it is the state of a file
    ``kb init`` has only laid down, and — since a whole-document PUT is the
    only way to remove a rule — the only way to delete the last one. Callers
    that want to point emptiness out (the admin list does) say so themselves.
    """
    issues: list[str] = []
    for rule in doc.rules:
        issues.extend(lint_rule(rule))
    return issues


def compile_condition(rule: DecisionRule) -> Any:
    """The rule's conditions → one AST (``mode`` picks AND vs OR).

    Raises ``DecisionExprError`` on a malformed condition; callers on the
    execution path treat that as a hard failure, never as "no trigger".
    """
    nodes = [parse_condition(c) for c in rule.conditions]
    from trove.services.decision.expr import And, Or

    if not nodes:
        raise DecisionExprError(f"rule {rule.id!r} has no conditions")
    if len(nodes) == 1:
        return nodes[0]
    return Or(tuple(nodes)) if rule.condition_mode == "any" else And(tuple(nodes))
