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

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from trove.services.analysis.stats import MIN_BLOCKS, ROBUST_Z_THRESHOLD
from trove.services.decision.expr import (
    DecisionExprError,
    condition_variables,
    parse_condition,
)

SEVERITIES = ("info", "warning", "critical")
BASELINE_KINDS = ("prev_period", "yoy", "literal", "none")
SCOPES = ("aggregate", "per_dimension")
EMITS = ("any", "all", "top_k")

#: ``seasonal.grain`` —— 空 = 由窗口形状推导(``derive_grain``);非空 =
#: 作者声明(判定侧按硬性对齐处理:声明与窗口形状不一致时拒绝确认)。
SEASONAL_GRAINS = ("", "day", "week", "month")
#: ``seasonal.mode`` —— 近期连续块 | 同相位(去年同期)。
SEASONAL_MODES = ("trailing", "same_phase")
#: ``significance.require`` 的闭集 —— 空 = 只记录不拦;``outside_band`` =
#: 触发必须再通过「超出历史噪声带」这道门。
SIGNIFICANCE_REQUIREMENTS = ("", "outside_band")

#: ``action.autonomy`` — v1 ships no dispatch either way; the two values are
#: the contract the action pillar (P3) builds its proposal gate on:
#: ``notify_only`` never leaves the system, ``propose`` may create a proposal
#: that a human still has to approve. There is deliberately no "auto" value.
AUTONOMIES = ("notify_only", "propose")

#: ``priority`` 的闭区间 —— 0 = 常规。四个档位够区分「今天要做」与「知会一声」,
#: 不做无上界的自由数字:它进通知、进排序、进治理待办,排序语义必须可解释。
PRIORITY_MAX = 3

#: Schema version this code writes and understands. A file with a *higher*
#: version was written by a newer Trove and may carry fields this reader
#: would drop on the next save — see ``parse_document``.
SCHEMA_VERSION = 3

#: Variables that only exist when the rule groups by a dimension — a
#: condition referencing them on an aggregate rule would silently see Unknown.
_DIMENSION_ONLY = frozenset({"contribution", "dim"})

#: 只在声明 ``significance`` 后才有值的变量 —— 未声明时它在条件里
#: 恒为 Unknown(规则静默不响),所以 lint 在写入时直接拒掉。
_SIGNIFICANCE_ONLY = frozenset({"confidence"})


class RuleError(ValueError):
    """A rule document that must not be persisted or executed."""


@dataclass
class Baseline:
    kind: str = "none"
    value: float | None = None


@dataclass
class ActionRef:
    """A rule's pointer at an action template (P3) — a *reference*, not an action.

    The rule says "when this fires, the org's response is template X"; whether
    anything leaves the system is decided by the template's confirmation gate
    and the proposal's approval step, never here. ``params`` carries the
    closed-set variables the template may interpolate — arbitrary expressions
    are deliberately impossible.
    """

    template: str = ""
    autonomy: str = "notify_only"    # AUTONOMIES
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Subject:
    """The semantic query body — mirrors ``services.semantic_layer.query.SemanticQuery``."""

    metrics: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    filters: list[dict[str, Any]] = field(default_factory=list)
    time_grain: dict[str, str] | None = None
    limit: int | None = None


@dataclass
class Seasonal:
    """季节性声明(schema v3)—— 噪声带的**块规格**,不是第二个窗口。

    语义 = 「这条规则的历史基线由哪些同粒度块构成」:
    ``grain`` 空 → 由窗口形状推导(月首月尾→month 等);``lookback``
    只计**历史**块(被测窗口不参与 —— 混入会自我稀释);``mode`` 选
    近期连续块(trailing)或同相位(same_phase,去年同期);``k`` 是
    噪声带宽(稳健 z 单位)。呈现形态是**分布**而非点值:决策要回答
    「超出噪声带了吗」,把分布压成「上期是多少」会丢掉它唯一的价值。
    """

    grain: str = ""
    lookback: int = 12
    mode: str = "trailing"
    k: float = ROBUST_Z_THRESHOLD


@dataclass
class Significance:
    """显著性门(schema v3)—— 声明的存在与否都有语义。

    声明即要证据:触发后计算噪声带、给行卡附 ``z/confidence/gated``;
    ``require`` 非空则是**门**:触发必须超出噪声带才成立。``""`` 只
    记录不拦。``min_confidence`` 是位置分数下限(仅 require 时生效)。
    """

    require: str = ""
    min_confidence: float = 0.0

    def required(self) -> bool:
        return bool(self.require)


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
    # ── schema v2 ────────────────────────────────────────
    #: What a human should do about it. **Human-authored only** — it is never
    #: generated, because a recommendation is a promise the org makes, not a
    #: model's guess; an LLM-written "建议" that reads as authoritative is the
    #: exact failure mode this field exists to avoid.
    recommendation: str = ""
    #: 0 = 常规, PRIORITY_MAX = 最紧急. Orders the governance to-do list.
    priority: int = 0
    #: Optional response template (P3). None = notify only, which is also
    #: what every rule written before v2 means.
    action: ActionRef | None = None
    #: The dimension the analysis bridge decomposes along when the rule fires
    #: (default: the rule's own grouping — see ``decision/bridge.py``).
    driver_dimension: str = ""
    # ── schema v3 ────────────────────────────────────────
    #: 噪声带的块规格。``None`` = 未声明(输出逐字节与 v2 相同)。
    seasonal: Seasonal | None = None
    #: 显著性门。``None`` = 未声明;存在即要证据,``require`` 非空即要拦。
    significance: Significance | None = None

    def describe(self) -> str:
        return self.name or self.id


@dataclass
class DecisionDoc:
    rules: list[DecisionRule] = field(default_factory=list)
    version: int = 1
    digest: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


# ── parsing ──────────────────────────────────────────────────

def _as_int(raw: Any, *, default: int) -> int:
    """Strict int coercion for hand-written YAML, with ``None`` = absent.

    Garbage raises (like ``top_k``): a typo'd ``priority: high`` must not
    quietly become 0 — "regular priority" reads exactly like a deliberate
    choice. The *range* is lint's business, not the parser's.
    """
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        raise RuleError(f"expected an integer, got {raw!r}")
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise RuleError(f"expected an integer, got {raw!r}")


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


def _parse_action(raw: Any) -> ActionRef | None:
    """``action`` block → ``ActionRef``; ``None`` when the rule has no action.

    Structure only — whether ``template`` names a confirmed template is a
    question for ``lint_rule_assets``, which owns the asset registries.
    """
    if raw is None or raw == "":
        return None
    if not isinstance(raw, dict):
        raise RuleError(f"'action' must be a mapping, got {type(raw).__name__}")
    params = raw.get("params")
    return ActionRef(
        template=str(raw.get("template") or "").strip(),
        autonomy=str(raw.get("autonomy") or "notify_only").strip().lower(),
        params=dict(params) if isinstance(params, dict) else {},
    )


def _parse_seasonal(raw: Any) -> Seasonal | None:
    """``seasonal`` 块 → ``Seasonal``;``None``/空 = 未声明(保持缺省)。

    **出现即声明**:与缺省值相同的块也会被记下(``lookback: 12`` 显式
    写出 ≠ 没写)—— ``significance`` 的 lint 要求 seasonal 存在,存在性
    必须能表达,不能靠"值不等于缺省"来猜。
    """
    if raw is None or raw == "":
        return None
    if not isinstance(raw, dict):
        raise RuleError(f"'seasonal' must be a mapping, got {type(raw).__name__}")
    k_raw = raw.get("k")
    if k_raw is None or k_raw == "":
        k = float(ROBUST_Z_THRESHOLD)
    elif isinstance(k_raw, bool):
        raise RuleError(f"seasonal.k must be a number, got {k_raw!r}")
    else:
        try:
            k = float(k_raw)
        except (TypeError, ValueError):
            raise RuleError(f"seasonal.k must be a number, got {k_raw!r}")
    return Seasonal(
        grain=str(raw.get("grain") or "").strip().lower(),
        lookback=_as_int(raw.get("lookback"), default=12),
        mode=str(raw.get("mode") or "trailing").strip().lower(),
        k=k,
    )


def _parse_significance(raw: Any) -> Significance | None:
    """``significance`` 块 → ``Significance``;``None``/空 = 未声明。"""
    if raw is None or raw == "":
        return None
    if not isinstance(raw, dict):
        raise RuleError(f"'significance' must be a mapping, got {type(raw).__name__}")
    mc_raw = raw.get("min_confidence")
    if mc_raw is None or mc_raw == "":
        mc = 0.0
    elif isinstance(mc_raw, bool):
        raise RuleError(f"significance.min_confidence must be a number, got {mc_raw!r}")
    else:
        try:
            mc = float(mc_raw)
        except (TypeError, ValueError):
            raise RuleError(
                f"significance.min_confidence must be a number, got {mc_raw!r}")
    return Significance(
        require=str(raw.get("require") or "").strip().lower(),
        min_confidence=mc,
    )


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
        recommendation=str(raw.get("recommendation") or "").strip(),
        priority=_as_int(raw.get("priority"), default=0),
        action=_parse_action(raw.get("action")),
        driver_dimension=str(raw.get("driver_dimension") or "").strip(),
        seasonal=_parse_seasonal(raw.get("seasonal")),
        significance=_parse_significance(raw.get("significance")),
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
    if version > SCHEMA_VERSION:
        # Same philosophy as ``StorageSchemaTooNew``: a newer file may carry
        # fields this reader does not know, and the failure is *silent* —
        # ``rule_to_dict`` would drop them on the next save through the UI,
        # so a whole document PUT would quietly delete a newer Trove's rules.
        # Refusing to read beats silently having read less.
        raise RuleError(
            f"decisions.yml schema version {version} is newer than this Trove "
            f"understands (max {SCHEMA_VERSION}) — refusing to read it, since "
            "saving through this version would drop fields it does not know")
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

    # ── schema v2 ────────────────────────────────────────
    if not isinstance(rule.priority, int) or not (0 <= rule.priority <= PRIORITY_MAX):
        issues.append(
            f"{where}: priority must be an integer in [0, {PRIORITY_MAX}] "
            f"(got {rule.priority!r})")
    if rule.action is not None:
        if not rule.action.template:
            issues.append(f"{where}: action.template must not be empty")
        if rule.action.autonomy not in AUTONOMIES:
            issues.append(
                f"{where}: action.autonomy must be one of "
                f"{', '.join(AUTONOMIES)} (got {rule.action.autonomy!r})")

    # ── schema v3 ────────────────────────────────────────
    if rule.seasonal is not None:
        if rule.seasonal.grain not in SEASONAL_GRAINS:
            issues.append(
                f"{where}: seasonal.grain must be one of "
                f"{[g or '(derive)' for g in SEASONAL_GRAINS]} "
                f"(got {rule.seasonal.grain!r})")
        if rule.seasonal.mode not in SEASONAL_MODES:
            issues.append(
                f"{where}: seasonal.mode must be one of "
                f"{', '.join(SEASONAL_MODES)} (got {rule.seasonal.mode!r})")
        if rule.seasonal.lookback < 1:
            issues.append(f"{where}: seasonal.lookback must be >= 1 "
                          f"(got {rule.seasonal.lookback!r})")
        if not (rule.seasonal.k > 0):
            issues.append(f"{where}: seasonal.k must be > 0 "
                          f"(got {rule.seasonal.k!r})")
    if rule.significance is not None:
        if rule.seasonal is None:
            # 噪声带由历史块构建 —— 没有块规格,门永远判不了(而
            # 「判不了」在 require 下是 error run:一条每次都报错的
            # 规则不该被写进文件)。
            issues.append(
                f"{where}: 'significance' requires a 'seasonal' block — the "
                "noise band is built from historical blocks; declare "
                "seasonal (grain/lookback/mode) or drop significance")
        if rule.significance.require not in SIGNIFICANCE_REQUIREMENTS:
            issues.append(
                f"{where}: significance.require must be one of "
                f"{[r or '(record only)' for r in SIGNIFICANCE_REQUIREMENTS]} "
                f"(got {rule.significance.require!r})")
        if not (0.0 <= rule.significance.min_confidence <= 1.0):
            issues.append(
                f"{where}: significance.min_confidence must be in [0, 1] "
                f"(got {rule.significance.min_confidence!r})")
        if rule.seasonal is not None and rule.significance.required() \
                and rule.seasonal.lookback < MIN_BLOCKS:
            # MIN_BLOCKS 是统计侧的硬门(块数不足时显著性成噪声放大器):
            # 声明了门却给不够块,等于一条永远 error 的规则 —— 挡在写入口。
            issues.append(
                f"{where}: seasonal.lookback {rule.seasonal.lookback} < "
                f"{MIN_BLOCKS} (MIN_BLOCKS) — significance can never confirm "
                "a trigger with fewer blocks")

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
        sig_only = sorted(used & _SIGNIFICANCE_ONLY)
        if sig_only and rule.significance is None:
            issues.append(
                f"{where}: condition {cond!r} uses {', '.join(sig_only)}, which "
                "only exists when 'significance' is declared")
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


def lint_advisories(doc: DecisionDoc) -> list[str]:
    """Non-blocking hints — shown by the admin UI, **never** refused by a save.

    The blocking/advisory split is the same one the Skills validator tier
    uses: a rule that is structurally sound but under-specified should still
    be writable (the author may be mid-draft), while one that would never
    fire must not reach disk at all.
    """
    out: list[str] = []
    for rule in doc.rules:
        where = f"rule {rule.id!r}"
        if rule.action is not None and not rule.recommendation:
            # A proposal with no stated recommendation asks a human to approve
            # *something* without saying why. Not fatal — the action payload
            # still carries the evidence — but the approver deserves a reason.
            out.append(
                f"{where}: declares an action but no 'recommendation' — the "
                "approver will see the template with no stated reason")
        if rule.action is not None and rule.action.autonomy == "propose" \
                and rule.scope == "aggregate":
            out.append(
                f"{where}: 'propose' on an aggregate rule fires without a "
                "group label — consider a driver_dimension so the proposal "
                "names what moved")
        if rule.significance is not None and rule.significance.min_confidence \
                and not rule.significance.required():
            out.append(
                f"{where}: significance.min_confidence is only enforced with "
                "require: outside_band — without it the value is recorded "
                "but not gated on")
    return out


def lint_rule_assets(
    rule: DecisionRule, *, templates: set[str] | None = None,
    confirmed: set[str] | None = None, dimensions: set[str] | None = None,
) -> list[str]:
    """Checks that need asset registries rather than the rule alone.

    Every input is optional and ``None`` means *"that registry is not
    available here, so this check cannot run"* — a caller without a template
    service should not be told the rule is bad. Passing an empty set is
    different and means "the registry is here and it is empty", which does
    flag every reference. The distinction matters at P3 wiring time: an
    unwired service must skip the check, an empty one must fail it.
    """
    issues: list[str] = []
    where = f"rule {rule.id!r}"
    if rule.action is not None and rule.action.template:
        name = rule.action.template
        if templates is not None and name not in templates:
            issues.append(f"{where}: action.template {name!r} is not declared")
        elif confirmed is not None and name not in confirmed:
            # Declared but not confirmed: the template gate (draft → admin
            # confirm) exists so unreviewed org responses cannot be wired to
            # a rule that fires on its own.
            issues.append(
                f"{where}: action.template {name!r} is not confirmed yet — "
                "confirm it (or pick a confirmed template) before referencing it")
    if rule.driver_dimension and dimensions is not None:
        if rule.driver_dimension not in dimensions:
            issues.append(
                f"{where}: driver_dimension {rule.driver_dimension!r} is not "
                "declared in the semantic model")
    return issues


def lint_document_assets(
    doc: DecisionDoc, *, templates: set[str] | None = None,
    confirmed: set[str] | None = None, dimensions: set[str] | None = None,
) -> list[str]:
    """``lint_rule_assets`` flattened across the document (see ``lint_document``)."""
    issues: list[str] = []
    for rule in doc.rules:
        issues.extend(lint_rule_assets(
            rule, templates=templates, confirmed=confirmed,
            dimensions=dimensions))
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
    # Schema v2 — every field parse_rule reads must be written back, or a save
    # through the UI silently drops it (round-trip test pins this).
    if rule.recommendation:
        out["recommendation"] = rule.recommendation
    if rule.priority:
        out["priority"] = rule.priority
    if rule.action is not None:
        action: dict[str, Any] = {
            "template": rule.action.template,
            "autonomy": rule.action.autonomy,
        }
        if rule.action.params:
            action["params"] = dict(rule.action.params)
        out["action"] = action
    if rule.driver_dimension:
        out["driver_dimension"] = rule.driver_dimension
    # Schema v3 —— 未声明的块**不写出**:v2 规则的字节表示与历史完全一致,
    # 整份文件的 digest 与每条规则的 rev 因此保持连续(条件序列化是
    # N2 修复成立的前提:回评分桶认 rev,而 rev 认这份规范形状)。
    if rule.seasonal is not None:
        out["seasonal"] = {
            "grain": rule.seasonal.grain,
            "lookback": rule.seasonal.lookback,
            "mode": rule.seasonal.mode,
            "k": rule.seasonal.k,
        }
    if rule.significance is not None:
        sig: dict[str, Any] = {"require": rule.significance.require}
        if rule.significance.min_confidence:
            sig["min_confidence"] = rule.significance.min_confidence
        out["significance"] = sig
    return out


def rule_rev(rule: DecisionRule) -> str:
    """单条规则的内容版本号(规范序列化 sha256 前 16 位)。

    N2 修复:``rule_digest`` 是整份 ``decisions.yml`` 的字节 sha256 ——
    改 B 规则会让 A 规则的回评分桶(``(rule_id, rule_rev)``)整段错位。
    rev 只认这一条规则:``rule_to_dict`` 条件序列化 → 与文件里其他规则、
    键序、缩进、注释都无关;同一规则内容 ⇒ 同一 rev(跨进程稳定)。
    """
    payload = json.dumps(rule_to_dict(rule), sort_keys=True,
                         ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


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
