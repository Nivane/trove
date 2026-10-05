"""DecisionService — deterministic rule evaluation against a live datasource.

The pipeline, once per rule:

    window (natural language)  →  parse_time_range / format_time_range
    window + baseline kind     →  base_period  →  (current, baseline) date ranges
    subject + time filter      →  build_and_compile  →  authoritative SQL
    SQL                        →  connectors.execute  →  rows
    rows × condition AST       →  DecisionOutcome (with evidence)

Zero LLM. Every step is a pure function or a read-only query, so a verdict is
reproducible from its inputs alone — which is what makes it defensible when a
rule fires and somebody asks why.

**Failure is always loud.** The sibling ``jobs/alerts.py`` DSL can afford to
treat "couldn't tell" as "no alert", because its inputs are already in hand
and the worst case is a missed notification. Here a rule that cannot be
evaluated (unresolvable window, undeclared metric, no declared time field)
must surface as an error on the run, never as a quiet "OK" — a silently
never-firing scheduled job is indistinguishable from a healthy one.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from trove.core.logging import get_logger
from trove.core.serialize import json_safe
from trove.services.analysis.engine import time_conds
from trove.services.analysis.stats import MIN_BLOCKS
from trove.services.decision.budget import DecisionBudget
from trove.services.decision.causal import causal_line
from trove.services.decision.expr import (
    DecisionExprError,
    as_number,
    condition_variables,
)
from trove.services.decision.rules import (
    Causal,
    DecisionRule,
    RuleError,
    Seasonal,
    Significance,
    compile_condition,
    rule_rev,
)
from trove.services.semantic_layer.query import (
    SemanticQuery,
    SemanticQueryError,
    build_and_compile,
)

logger = get_logger(__name__)

#: Evidence rows kept in the run record. The full result set can be large and
#: this lands in the jobs database, so the card carries a bounded sample
#: plus an explicit `truncated` flag rather than silently dropping rows.
MAX_EVIDENCE_ROWS = 200

_CJK_RE = re.compile(r"[一-鿿]")


class DecisionError(RuntimeError):
    """A rule could not be evaluated. Never downgraded to "no trigger"."""


@dataclass
class DecisionOutcome:
    """One rule evaluation.

    Mirrors ``jobs.alerts.AlertVerdict``'s ``triggered`` + ``message`` so the
    existing dispatch/cooldown path is reused unchanged; ``error`` is the
    addition that path needs to tell "judged, no trigger" from "could not judge".
    """

    triggered: bool
    message: str
    rule_id: str = ""
    severity: str = "warning"
    error: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


def _lang_of(text: str) -> str:
    """Regex rules are per-language; the rule's own window text decides."""
    return "zh" if _CJK_RE.search(text or "") else "en"


def _jsonable(value: Any) -> Any:
    """DB-typed value → something ``json.dumps`` accepts.

    Postgres hands back ``Decimal`` and ``datetime``; a decision run stores
    its evidence as JSON, and a serialization failure there would abort
    ``finish_run`` and (before the runner guard) strand the job's schedule.

    实现已升格共享(``core.serialize.json_safe``):分析柱的证据记录走同一条
    序列化边界 —— 2026-10-05 归因答案在 UI 上「流中断」的根因就是同一个
    未安全化的 ``Decimal``。此处保留旧名,调用点与测试按 ``_jsonable`` 读。
    """
    return json_safe(value)


def _contribution(
    current: dict[str, float], baseline: dict[str, float],
) -> list[dict[str, Any]]:
    """维度贡献率表 —— 与 ``attribution._contribution`` 同语义。

    delta_i = cur_i - base_i;total_abs = Σ|delta_i|;contribution_i =
    delta_i / total_abs(带符号),按 |contribution| 降序。total_abs == 0 时
    退化为占比 —— 这里可以安全退化(它只是展示用的归因列),因为**触发判定
    不读它**:判据是 current/baseline/delta,与归因列无关。
    """
    keys = list(current) + [k for k in baseline if k not in current]
    items: list[dict[str, Any]] = []
    for k in keys:
        c = as_number(current.get(k))
        b = as_number(baseline.get(k))
        items.append({"dim": k, "current": c, "baseline": b})
    deltas = [
        (it["current"] - it["baseline"])
        if it["current"] is not None and it["baseline"] is not None else None
        for it in items
    ]
    total_abs = sum(abs(d) for d in deltas if d is not None)
    total_cur = sum(it["current"] for it in items if it["current"] is not None)
    for it, d in zip(items, deltas):
        if d is None:
            it["contribution"] = None
        elif total_abs:
            it["contribution"] = d / total_abs
        elif total_cur:
            it["contribution"] = (it["current"] or 0.0) / total_cur
        else:
            it["contribution"] = None
    items.sort(key=lambda x: abs(x["contribution"] or 0.0), reverse=True)
    return items


def cond_hit(text: str, scope: dict[str, Any]) -> bool:
    """Whether one condition text matched — for the `matched` audit list.
    A condition that fails to parse is already fatal upstream."""
    from trove.services.decision.expr import parse_condition

    try:
        return parse_condition(text).eval(scope) is True
    except DecisionExprError:
        return False


def judge(
    rule: DecisionRule, cond: Any, cur_map: dict[str, float | None],
    base_map: dict[str, float | None], row_count: int, *,
    confidence_by_dim: dict[str, float | None] | None = None,
    gated_by_dim: dict[str, bool] | None = None,
    require_gate: bool = False,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Per-group verdicts → (row cards, the groups that triggered).

    Module-level and **pure** (B4 提取):同样的输入永远给同样的行卡 ——
    what-if 模拟要在假想的 current/baseline 上重跑同一条内核,判定与
    模拟共用这一个函数就是它们不漂移的全部保证(``_contribution`` /
    ``_jsonable`` 同样零 I/O)。

    ``confidence_by_dim`` is the significance stage's product: ``None``
    (the default — no ``significance`` on the rule) leaves every row card
    byte-identical to the pre-B2 shape. When provided, rows gain
    ``confidence``/``gated`` and — for a rule that ``require``s the gate —
    a satisfied condition only triggers when the group is also *out of
    the noise band*. A group that fired but could not be confirmed stays
    visible as ``triggered: false, gated: false`` with its ``matched``
    conditions intact (the audit trail must show the near-miss, not hide
    it), and ``confidence`` joins the condition scope so a rule can test
    it directly.
    """
    contrib = _contribution(cur_map, base_map) if rule.scope == "per_dimension" \
        else []
    contrib_by_dim = {c["dim"]: c["contribution"] for c in contrib}
    dims = [c["dim"] for c in contrib] if contrib else list(cur_map)

    rows: list[dict[str, Any]] = []
    hits: list[tuple[str, float | None, list[str]]] = []
    if rule.scope != "per_dimension":
        dims = [""]

    for dim in dims:
        cur = cur_map.get(dim)
        base = base_map.get(dim)
        delta = (cur - base) if cur is not None and base is not None else None
        delta_pct = (delta / base) if delta is not None and base else None
        scope_vars = {
            "current": cur,
            "baseline": base,
            "delta": delta,
            "delta_pct": delta_pct,
            "contribution": contrib_by_dim.get(dim),
            "row_count": row_count,
            "dim": dim,
        }
        if confidence_by_dim is not None:
            scope_vars["confidence"] = confidence_by_dim.get(dim)
        hit = cond.eval(scope_vars) is True
        # Only a satisfied group lists its conditions: under `all`, a
        # half-matched group would otherwise look like it fired.
        matched = ([c for c in rule.conditions if cond_hit(c, scope_vars)]
                   if hit else [])
        card: dict[str, Any] = {
            "dim": dim,
            "current": _jsonable(cur),
            "baseline": _jsonable(base),
            "delta": _jsonable(delta),
            "delta_pct": _jsonable(delta_pct),
            "contribution": _jsonable(contrib_by_dim.get(dim)),
            "triggered": hit,
            "matched": matched,
        }
        if confidence_by_dim is not None:
            gated = bool(gated_by_dim.get(dim)) if gated_by_dim is not None else False
            card["triggered"] = bool(hit and (gated if require_gate else True))
            card["confidence"] = _jsonable(confidence_by_dim.get(dim))
            card["gated"] = gated
        rows.append(card)
        if card["triggered"]:
            hits.append((dim, cur, matched))

    # `emit` decides which of the triggered groups the *rule* reports. The
    # per-row verdicts stay as judged — for `all` you specifically want to
    # see which groups did not fire, and rewriting them would hide that.
    if rule.scope == "per_dimension":
        if rule.emit == "all":
            fired = bool(rows) and all(r["triggered"] for r in rows)
            hits = hits if fired else []
        elif rule.emit == "top_k":
            hits = sorted(
                hits, key=lambda h: abs(contrib_by_dim.get(h[0]) or 0.0),
                reverse=True)[:max(1, rule.top_k)]

    return rows, [h[0] for h in hits]


class DecisionService:
    """Evaluates decision rules for a datasource.

    ``connectors`` + ``kb`` are the only dependencies: the semantic model is
    resolved **per datasource at evaluation time** rather than injected as a
    provider. A ``Job`` carries its own ``datasource``, and a provider built
    once at startup is bound to the default one — using it would silently
    compile the wrong model's SQL.
    """

    def __init__(
        self, connectors, kb, semantic_dir: str | Path | None = None,
        *, timeout_ms: int = 30_000, config: Any = None,
    ):
        self.connectors = connectors
        self.kb = kb
        self._semantic_dir = Path(semantic_dir) if semantic_dir is not None else None
        # 扩展面治理开关的宿主(``AgentConfig``,与 SkillService 同一把)。
        # 这里存的是**活对象引用**:管理端改开关就地生效,评估时现读 ——
        # 不做构造期快照。不传(旧构造点/单测)→ 视为开启。
        self.config = config
        # 单条 SQL 的执行预算(毫秒),与交互管线同一个 ``budget.timeout_ms``。
        # 定时任务没有人在等,一条无界的查询会把这次 run(job 的 schedule)永远
        # 吊在那里;写错/非正数 → 回到缺省,不静默变成"无超时"。
        try:
            ms = int(timeout_ms)
        except (TypeError, ValueError):
            ms = 0
        self._timeout_ms = ms if ms > 0 else 30_000

    def _org_enabled(self) -> bool:
        """组织扩展总开关 —— **每次评估现读**(热生效,无缓存)。

        ``agent.extensions.org_extensions_enabled``(默认 true)同管
        org skills 与决策规则:规则是组织扩展的一个消费面,停用即不执行。
        """
        ext = getattr(self.config, "extensions", None)
        if ext is None:
            return True
        return bool(getattr(ext, "org_extensions_enabled", True))

    # ── model / dialect resolution ────────────────────────

    def _semantic_root(self) -> Path:
        """``.trove/semantic`` — derived from the KB's root when available.

        Deriving from the KB (rather than ``Path.cwd()``) keeps the rules and
        the semantic model they compile against coming from the same project
        tree; pointing them at different roots is how a job ends up referring
        to a rule that the scheduler cannot see.
        """
        if self._semantic_dir is not None:
            return self._semantic_dir
        if self.kb is not None:
            return Path(self.kb.kb_dir).parent / "semantic"
        return Path.cwd() / ".trove" / "semantic"

    async def _dialect(self, datasource: str) -> str:
        """Real dialect or a hard failure.

        ``api/routers/semantic_query.py`` falls back to ``"sqlite"`` when the
        adapter cannot be reached — acceptable there, because a bad compile
        surfaces immediately as a 422 to the human who asked. A scheduled
        decision run has no such reader: a SQLite-dialect query sent to MySQL
        is a wrong answer nobody sees.
        """
        try:
            adapter = await self.connectors.get(datasource)
        except Exception as e:
            raise DecisionError(f"datasource unreachable: {datasource!r}: {e}") from e
        dialect = ""
        try:
            dialect = adapter.dialect() or ""
        except Exception:
            dialect = ""
        if not dialect:
            raise DecisionError(
                f"cannot determine SQL dialect for datasource {datasource!r} "
                "(refusing to guess — a wrong dialect compiles wrong SQL)")
        return dialect

    def _provider_for(self, datasource: str, dialect: str):
        """Per-datasource semantic provider — same path as the public query API.

        Returned as the *provider* (not just its model) because the analysis
        bridge hands it to ``AnalysisEngine``, which calls ``.model()`` and
        the resolver helpers on it. One construction path for both keeps the
        bridge compiling against exactly the model the verdict was judged on.
        """
        from trove.services.semantic_layer.provider import SemanticLayerProvider

        kb_path = None
        if self.kb is not None:
            try:
                kb_path = self.kb.semantics_path(datasource)
            except Exception:
                kb_path = None
        return SemanticLayerProvider(
            directory=self._semantic_root() / datasource,
            datasource=datasource,
            dialect=dialect,
            kb_semantics_path=kb_path,
        )

    def _model_for(self, datasource: str, dialect: str):
        model = self._provider_for(datasource, dialect).model()
        if model is None:
            raise DecisionError(
                f"no semantic model for datasource {datasource!r} "
                "— decision rules compile against the semantic layer only")
        return model

    # ── query building ────────────────────────────────────

    @staticmethod
    def _resolve_window(rule: DecisionRule, anchor: date) -> tuple[
            tuple[str, str] | None, tuple[str, str] | None]:
        """``rule.window`` → (current range, baseline range), both ISO strings.

        The window is written in the same natural language the NL pipeline
        parses, and resolved through the same public entry point — so "本月"
        means the same span whether a user asks a question or a rule fires.
        """
        from trove.core.periods import base_period
        from trove.workflow.nodes.parse_date import format_time_range, parse_time_range

        kind = rule.baseline.kind
        if not rule.window:
            if kind in ("prev_period", "yoy"):
                raise DecisionError(
                    f"rule {rule.id!r}: baseline.kind {kind!r} needs a 'window' "
                    "to derive the baseline from")
            return None, None

        rng = parse_time_range(rule.window, reference_date=anchor,
                               lang=_lang_of(rule.window))
        if rng is None:
            raise DecisionError(
                f"rule {rule.id!r}: window {rule.window!r} is not a resolvable "
                f"time expression (reference date {anchor.isoformat()})")
        ctx = format_time_range(*rng)
        if kind in ("prev_period", "yoy"):
            periods = base_period(ctx, kind)
            if periods is None:  # unreachable for these kinds, but never guess
                raise DecisionError(
                    f"rule {rule.id!r}: cannot derive a {kind} baseline from "
                    f"window {ctx!r}")
            return periods
        return (rng[0].isoformat(), rng[1].isoformat()), None

    async def _compile(
        self, rule: DecisionRule, model, dialect: str, datasource: str,
        window: tuple[str, str] | None,
    ) -> dict[str, Any]:
        """subject (+ window) → compiled SQL, columns and anchor datasets."""
        base_filters = [dict(f) for f in rule.subject.filters]

        def _query(filters: list[dict[str, Any]]) -> SemanticQuery:
            return SemanticQuery(
                metrics=list(rule.subject.metrics),
                dimensions=list(rule.subject.dimensions),
                time_grain=rule.subject.time_grain,
                filters=filters,
                limit=rule.subject.limit,
            )

        try:
            # Pass 1 resolves the anchor datasets; the time field is looked up
            # against *those*, and the anchor does not depend on the filters.
            # Two pure compiles beat re-deriving build_and_compile's anchoring
            # rules here, where they would be free to drift.
            anchor_info = build_and_compile(model, _query(base_filters), dialect)
        except SemanticQueryError as e:
            raise DecisionError(f"rule {rule.id!r}: {e}") from e

        if window is None:
            return anchor_info

        from trove.services.semantic_layer.compiler import resolve_time_field

        anchor = list(anchor_info.get("datasets") or [])
        preferred = self._metric_time_dimension(model, rule.subject.metrics[0])
        resolved = resolve_time_field(model, anchor, preferred=preferred)
        if resolved is None:
            raise DecisionError(
                f"rule {rule.id!r}: cannot resolve a time field for window "
                f"{rule.window!r} — declare the metric's 'agg_time_dimension' "
                "in the semantic model")
        ds, fld = resolved
        ref = f"{ds}.{fld.name}"
        # 窗口条件与分析引擎/桥走同一份 ``time_conds``(半开 ``< end+1 天``),
        # 不再内联闭区间:闭区间在 timestamp 列上丢窗口最后一天,而触发后的
        # 分析桥是半开 —— 判定和它自己的附录必须对同一窗口看到同一批天数。
        time_filters = time_conds(ref, window, dialect=dialect)
        try:
            info = build_and_compile(model, _query(time_filters + base_filters),
                                     dialect)
        except SemanticQueryError as e:
            raise DecisionError(f"rule {rule.id!r}: {e}") from e
        info["time_field"] = ref
        return info

    @staticmethod
    def _metric_time_dimension(model, metric_name: str) -> str:
        for m in model.metrics:
            if m.name.strip().lower() == str(metric_name).strip().lower():
                return m.agg_time_dimension or ""
        return ""

    # ── row shaping ───────────────────────────────────────

    @staticmethod
    def _to_map(columns: list[str], rows: list[list[Any]]) -> dict[str, float | None]:
        """Rows → ``{dim label: metric value}``.

        Single metric per rule (lint enforces it), so the metric is the last
        column and everything before it identifies the group. Multiple
        dimensions join into one label — the comparison is per group, and the
        label is what a human reads in the alert.
        """
        if not columns:
            return {}
        n_dims = max(0, len(columns) - 1)
        out: dict[str, float | None] = {}
        for row in rows:
            if not row:
                continue
            label = " / ".join(
                str(row[i]) for i in range(min(n_dims, len(row)))) if n_dims else ""
            out[label] = as_number(row[n_dims]) if len(row) > n_dims else None
        return out

    # ── evaluation ────────────────────────────────────────

    async def evaluate_rule(
        self, rule: DecisionRule, datasource: str, now: datetime | None = None,
        *, rule_digest: str = "",
    ) -> DecisionOutcome:
        """Evaluate one rule. Raises ``DecisionError`` when it cannot be judged.

        ``rule_digest`` is the ``decisions.yml`` byte digest the rule was read
        from — the caller has it (it loaded the document) and the evidence
        needs it, because for a decision run the evidence *is* the audit
        trail: without it, "this fired on the 10% threshold" is unanswerable
        once the threshold is edited.

        **总开关停用时抛 ``DecisionError``(响)**,绝不返回"零规则通过":
        ``evaluate`` 把它折成 ``error``,runner 记 status=error —— 一条停用
        中的定时决策任务必须报「已停用」,静默的绿色 run 与健康运行从外部
        看没有区别(与模块 docstring 的"失败必响"同一条纪律)。
        """
        if not self._org_enabled():
            raise DecisionError(
                "org extensions are disabled "
                "(agent.extensions.org_extensions_enabled=false) — "
                "组织扩展已停用,decision rules 不执行(这不是一条通过的判定)。"
            )
        # Lint blocks this at the write time, but a hand-edited decisions.yml
        # reaches here un-linted, and the failure mode is a rule that reads
        # Unknown forever — indistinguishable from "nothing wrong today".
        if rule.scope == "aggregate" and rule.subject.dimensions:
            raise DecisionError(
                "scope 'aggregate' cannot be combined with subject.dimensions "
                f"({', '.join(rule.subject.dimensions)}) — the query groups, so "
                "there is no aggregate row to judge")
        anchor = (now or datetime.now()).date()
        dialect = await self._dialect(datasource)
        model = self._model_for(datasource, dialect)
        cur_window, base_window = self._resolve_window(rule, anchor)

        cond = compile_condition(rule)

        # 查询账本(B2):judge 最先花、永远花得起;显著性/桥在它后面,
        # 预算不足时让路的永远是后者。无 significance 声明的规则账本不
        # 进证据(见下面 evidence 的写法)—— 老输出保持原样。
        ledger = DecisionBudget().allocate()

        cur_info = await self._compile(rule, model, dialect, datasource, cur_window)
        cur_sql = cur_info["sql"]
        cur_cols = list(cur_info.get("columns") or [])
        cur_result = await self._run(cur_sql, datasource)
        ledger.record("judge")
        cur_map = self._to_map(cur_cols, cur_result.rows)

        base_sql = ""
        base_map: dict[str, float | None] = {}
        if rule.baseline.kind == "literal":
            base_map = dict.fromkeys(cur_map, rule.baseline.value)
        elif base_window is not None:
            base_info = await self._compile(rule, model, dialect, datasource,
                                            base_window)
            base_sql = base_info["sql"]
            base_result = await self._run(base_sql, datasource)
            ledger.record("judge")
            base_map = self._to_map(
                list(base_info.get("columns") or []), base_result.rows)

        rows, triggered_dims = self._judge(rule, cond, cur_map, base_map,
                                           cur_result.row_count)
        triggered = bool(triggered_dims)

        # 显著性门(B2):_judge 之后、桥之前。只在「声明了 significance
        # 且有事要判」时跑(未触发的规则零成本;条件引用 confidence 的
        # 规则必须跑 —— 那是它的判据)。require 下判不了 → DecisionError。
        significance = None
        if self._needs_significance(rule, triggered):
            rows, triggered_dims, significance = await self._significance_stage(
                rule, datasource, dialect, rows, cur_map, base_map,
                cur_result.row_count, cur_window, cond=cond, ledger=ledger,
                matched=list(cur_info.get("datasets") or []),
                time_field=str(cur_info.get("time_field") or ""))
            triggered = bool(triggered_dims)

        # 因果升级梯(B3):显著性之后、桥之前。只在「声明了 causal 且
        # 触发(含显著性门判定后的终态)」时跑。附录纪律:任何失败只
        # 进 degraded/unmet,绝不上抛、绝不改 triggered(见 _causal_stage)。
        causal = None
        if self._needs_causal(rule, triggered):
            causal = await self._causal_stage(
                rule, datasource, dialect, cur_window, ledger=ledger,
                matched=list(cur_info.get("datasets") or []),
                time_field=str(cur_info.get("time_field") or ""))

        # 分析桥(补丁 1):只在触发时跑,未触发零成本。任何失败只进
        # evidence 的 degraded —— 判定已经判完,桥是附录不是前置。
        analysis = None
        if triggered:
            analysis = await self._analysis_bridge(
                rule, datasource, dialect, rows, cur_window, base_window,
                matched=list(cur_info.get("datasets") or []))
            # 桥的查询事后入账(桥自带 4 条硬门,这里只求账目完整):
            # evidence.budget 回答「这次判定一共花了几条、花在哪」。
            ledger.record("bridge", len((analysis or {}).get("queries") or []))

        evidence = {
            "rule_id": rule.id,
            "rule_digest": rule_digest,
            "rule_name": rule.describe(),
            "severity": rule.severity,
            "priority": rule.priority,
            "recommendation": rule.recommendation,
            "owner_role": rule.owner_role,
            "model_version": cur_info.get("version", ""),
            "window_expr": rule.window,
            "periods": {
                "current": list(cur_window) if cur_window else None,
                "baseline": list(base_window) if base_window else None,
                "baseline_kind": rule.baseline.kind,
            },
            "times": {
                "anchor_date": anchor.isoformat(),
                "evaluated_at": (now or datetime.now()).isoformat(),
            },
            "rows": rows,
            "evidence": {
                "sql_current": cur_sql,
                "sql_baseline": base_sql,
                "columns": cur_cols,
                "time_field": cur_info.get("time_field", ""),
                "rows": [[_jsonable(c) for c in r] for r in
                         cur_result.rows[:MAX_EVIDENCE_ROWS]],
                "row_count": cur_result.row_count,
                "truncated": cur_result.row_count > MAX_EVIDENCE_ROWS,
            },
            "provenance": {"datasource": datasource},
        }
        if analysis is not None:
            evidence["analysis"] = analysis
        # 声明了对应块的规则才有这些节(未声明 → 响应逐字节与旧版相同):
        # significance = 噪声带证据(含每维 band/z/confidence/gated 与
        # 判不了的原因);causal = 升级梯证据(rung/unmet/assumptions/
        # did/placebo/synthetic);budget = 本次判定的查询账目(限额/实花/
        # 分阶段/让路)。
        if significance is not None:
            evidence["significance"] = significance
        if causal is not None:
            evidence["causal"] = causal
        if significance is not None or causal is not None:
            evidence["budget"] = ledger.snapshot()
        # rule_rev = 单条规则的内容版本(N2):rule_digest 是整份文件的
        # 字节 sha256,任何一条无关规则被编辑都会污染所有回评分桶。
        # 无条件写入 —— 从本版起每条 verdict 都钉在自己被判定时的规则
        # 内容上;更老的 verdict 没有它,回评按 rev_unknown 分桶。
        evidence["rule_rev"] = rule_rev(rule)

        return DecisionOutcome(
            triggered=triggered,
            message=(self._summarize(rule, rows, triggered_dims,
                                     net=causal_line(causal))
                     if triggered else ""),
            rule_id=rule.id,
            severity=rule.severity,
            evidence=evidence,
        )

    @staticmethod
    def _needs_causal(rule: DecisionRule, triggered: bool) -> bool:
        """因果阶段跑不跑 —— 声明即尝试,且只在触发后(未触发零成本)。

        与显著性不同:条件词汇里没有「净效应」变量(因果**绝不参与
        判定**,连读都不读),所以不存在「未触发也要跑」的情形 ——
        附录只跟在真触发后面。显著性门抑制掉的触发到这里已经是
        ``triggered=False``:规则没成立,估计它「为什么变化」没有意义。
        """
        return rule.causal is not None and triggered

    @staticmethod
    def _needs_significance(rule: DecisionRule, triggered: bool) -> bool:
        """显著性阶段跑不跑(成本纪律:未触发且无 confidence 条件 → 不跑)。

        声明即要证据(触发了就给带),``require`` 是拦不拦的问题,不是
        跑不跑的问题 —— 拦的道理与记的道理是同一个带。条件引用
        ``confidence`` 的规则必须跑:那是它的判据,不跑恒 Unknown。
        """
        uses_confidence = any(
            "confidence" in condition_variables(c) for c in rule.conditions)
        if uses_confidence:
            return True
        return rule.significance is not None and triggered

    async def _significance_stage(
        self, rule: DecisionRule, datasource: str, dialect: str,
        rows: list[dict[str, Any]], cur_map: dict[str, float | None],
        base_map: dict[str, float | None], row_count: int,
        cur_window: tuple[str, str] | None, *, cond: Any, ledger: Any,
        matched: list[str] | None = None, time_field: str = "",
    ) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
        """触发后的噪声带门(至多 1 条 SQL)→ (重判的行卡, 触发的组, 证据节)。

        与引擎的 series 阶段同款块语义(block_windows / same_phase_blocks
        / compile_series_hop),三处判定侧特有语义:

          - **按维仍是一条 SQL**(answer_columns = 维度… + 度量,见
            series_source)—— 预算纪律要求块序列至多花一条;
          - **require 下判不了 = error run**:触发但无法确认(粒度不对齐、
            块数不足、无数据、SQL 失败、预算让路)的组不静默变「未触发」;
          - **重判**:条件可引用 ``confidence``,带算出来后要用完整作用域
            重算一遍行卡与触发组(未引用 confidence 的行结果不变)。
        """
        from trove.services.decision.series_source import (
            fetch_block_series,
            plan_reason,
        )
        from trove.services.decision.significance import (
            CONFIDENCE_NOTE,
            build_band_payload,
            gate,
        )

        sig = rule.significance or Significance()
        seasonal = rule.seasonal or Seasonal()
        degraded: list[dict[str, Any]] = []
        section: dict[str, Any] = {
            "required": sig.required(),
            "min_confidence": sig.min_confidence,
            "seasonal": {
                "grain": seasonal.grain, "lookback": seasonal.lookback,
                "mode": seasonal.mode, "k": seasonal.k,
            },
            "note": CONFIDENCE_NOTE,
            "by_dim": {},
            "degraded": degraded,
        }

        series = None
        reason = ""
        if not time_field:
            reason = "no_time_field"
        else:
            reason = plan_reason(cur_window, grain=seasonal.grain,
                                 mode=seasonal.mode, lookback=seasonal.lookback)
        if not reason and not ledger.can(1):
            entry = ledger.yield_("significance", needed=1)
            degraded.append({"stage": "significance", **entry})
            reason = str(entry.get("reason") or "query_budget_exceeded")
        if not reason:
            try:
                series = await fetch_block_series(
                    semantic_layer=self._provider_for(datasource, dialect),
                    runner=self._query_runner(datasource),
                    datasource=datasource, dialect=dialect,
                    metric_name=(rule.subject.metrics[0]
                                 if rule.subject.metrics else ""),
                    matched=list(matched or []), filters=rule.subject.filters,
                    window=cur_window, time_field=time_field,
                    grain=seasonal.grain, mode=seasonal.mode,
                    lookback=seasonal.lookback,
                    dimensions=list(rule.subject.dimensions),
                )
            except Exception as e:
                reason = f"series_query_failed:{str(e)[:120]}"
            if series is not None:
                ledger.record("significance")
            elif not reason:
                reason = "series_unavailable"
        if reason and series is None:
            degraded.append({"stage": "significance", "reason": reason})
        if series is not None:
            section["grain"] = series.grain
            section["mode"] = series.mode
            section["blocks"] = [[b[0], b[1]] for b in series.blocks]
            section["span"] = list(series.span() or ["", ""])
            section["sql"] = series.sql
            if series.unmatched:
                # 桶标签与块窗口对不上 = 粒度/时区假设错了:记账,
                # 外面看得见(不静默丢桶)。
                degraded.append({"stage": "significance",
                                 "reason": f"unmatched_buckets:{series.unmatched}"})

        conf_by_dim: dict[str, float | None] = {}
        gated_by_dim: dict[str, bool] = {}
        for row in rows:
            dim = str(row.get("dim") or "")
            values = series.values(dim) if series is not None else []
            if series is not None:
                payload = build_band_payload(
                    values, k=seasonal.k,
                    seed_material=(f"{datasource}|{rule.id}|{dim}|"
                                   f"{series.blocks[0][0] if series.blocks else ''}"))
                outcome = gate(values, cur_map.get(dim), payload,
                               min_confidence=sig.min_confidence)
            else:
                payload = {}
                outcome = {"z": None, "outside": None, "confidence": None,
                           "gated": False, "reason": reason or "series_unavailable"}
            section["by_dim"][dim] = {**payload, **outcome}
            conf_by_dim[dim] = outcome["confidence"]
            gated_by_dim[dim] = bool(outcome["gated"])

        if sig.required():
            # 「判了在带内」是诚实的未触发;「判不了」不是 —— 触发过但
            # 无法确认的组必须把整条规则升级成 error run,绝不静默放行。
            hard: list[str] = []
            for row in rows:
                dim = str(row.get("dim") or "")
                if not row.get("triggered") or gated_by_dim.get(dim):
                    continue
                why = str(section["by_dim"].get(dim, {}).get("reason") or "")
                if why in ("within_band", "below_min_confidence"):
                    continue
                hard.append(f"{dim or '(aggregate)'}: {why or 'unconfirmable'}")
            if hard:
                raise DecisionError(
                    f"rule {rule.id!r}: significance requires 'outside_band' "
                    "but the noise band could not be computed for a triggered "
                    f"group — {'; '.join(hard)} (refusing to pass silently: a "
                    "trigger that cannot be confirmed is not a confirmed trigger)")

        rows, triggered_dims = self._judge(
            rule, cond, cur_map, base_map, row_count,
            confidence_by_dim=conf_by_dim, gated_by_dim=gated_by_dim,
            require_gate=sig.required())
        return rows, triggered_dims, section

    async def _causal_stage(
        self, rule: DecisionRule, datasource: str, dialect: str,
        cur_window: tuple[str, str] | None, *, ledger: Any,
        matched: list[str] | None = None, time_field: str = "",
    ) -> dict[str, Any]:
        """触发后的反事实估计(升级梯 L1→L3;至多 2 条 SQL)。

        三条不变量(与 ``decision/causal.py`` 的模块纪律一一对应):

          - **绝不上抛**:编译 MISS、SQL 报错、预算让路、数据不足 ——
            全部只写 ``degraded`` / ``unmet``,证据节照常返回;附录
            拖垮正文是明确禁止的;
          - **绝不改 triggered**:因果不参与判定(条件词汇里没有它),
            只是「为什么变化」的补充估计;
          - **条件不足诚实降级**:``ladder_decision`` 逐条给实测值与
            阈值(C3 的 ``max|did|``/阈值走验收要求),``assumptions``
            永远非空且把「无法检验」的条目写出来。

        取数两条:处理组(subject 口径,含当期块)+ 对照帧(dim 口径
        按维分组,供体池在同一帧里;filters 口径窄序列)。两条都成功
        才 ``ledger.record``;结构性前置(C4/C1)不满足时不花第二条。
        """
        from trove.services.decision.causal import (
            CAUSAL_NOTE,
            assumptions_for,
            crosses_zero,
            did_2x2,
            did_se,
            donor_counterfactual,
            ladder_decision,
            parallel_trends,
            placebo_pairs,
        )
        from trove.services.decision.causal_source import (
            causal_lookback,
            fetch_control_series,
            fetch_treated_series,
            split_post,
        )
        from trove.services.decision.series_source import plan_reason

        causal = rule.causal or Causal()
        seasonal = rule.seasonal or Seasonal()
        lookback = causal_lookback(seasonal, causal)
        degraded: list[dict[str, Any]] = []
        section: dict[str, Any] = {
            "mode": causal.mode,
            "placebo_blocks": causal.placebo_blocks,
            "tolerance": causal.tolerance,
            "control_declared": (
                {"dim": causal.control.dim, "value": causal.control.value}
                if causal.control is not None and causal.control.by_dim()
                else {"filters": [dict(f) for f in causal.control.filters]}
                if causal.control is not None
                else {}),
            "note": CAUSAL_NOTE,
            "rung": "L1",
            "unmet": [],
            "assumptions": [],
            "degraded": degraded,
        }

        def _stop(condition: str, reason: str, **fields: Any) -> dict[str, Any]:
            section["unmet"] = [{"condition": condition, "reason": reason,
                                 **fields}]
            section["assumptions"] = assumptions_for(
                "L1", unmet=section["unmet"],
                context=f"粒度 {seasonal.grain or '派生'}·{seasonal.mode}")
            return section

        # ── 结构性前置(C4):不花任何查询 ─────────────────
        if rule.seasonal is None:
            degraded.append({"stage": "causal", "reason": "no_seasonal"})
            return _stop("C4", "no_seasonal")
        if not time_field:
            degraded.append({"stage": "causal", "reason": "no_time_field"})
            return _stop("C4", "no_time_field")
        structural = plan_reason(cur_window, grain=seasonal.grain,
                                 mode=seasonal.mode, lookback=lookback)
        if structural:
            degraded.append({"stage": "causal", "reason": structural})
            return _stop("C4", structural)

        # ── C5 预算:两条取数要么都花得起,要么让路记账 ────
        if not ledger.can(2):
            entry = ledger.yield_("causal", needed=2)
            degraded.append({"stage": "causal", **entry})
            return _stop("C5", str(entry.get("reason") or "query_budget_exceeded"))

        metric = rule.subject.metrics[0] if rule.subject.metrics else ""
        # ── 第一条:处理组序列(subject 口径) ─────────────
        try:
            treated = await fetch_treated_series(
                semantic_layer=self._provider_for(datasource, dialect),
                runner=self._query_runner(datasource),
                datasource=datasource, dialect=dialect, metric_name=metric,
                matched=list(matched or []), filters=rule.subject.filters,
                window=cur_window, time_field=time_field,
                grain=seasonal.grain, mode=seasonal.mode, lookback=lookback)
        except Exception as e:
            reason = f"series_query_failed:{str(e)[:120]}"
            degraded.append({"stage": "causal", "reason": reason})
            return _stop("C4", reason)
        if treated is None:
            degraded.append({"stage": "causal",
                             "reason": "treated_series_unavailable"})
            return _stop("C4", "treated_series_unavailable")
        ledger.record("causal")
        if treated.unmatched:
            degraded.append({"stage": "causal",
                             "reason": f"unmatched_buckets:{treated.unmatched}"})

        t_hist, t_post = split_post(treated.values(""))
        if not t_hist or t_hist[-1] is None:
            # 前窗最后一格没值 = 没有「干预前水平」——DiD 的减数不存在。
            return _stop("C4", "no_pre_block")
        n_blocks = sum(1 for v in t_hist if v is not None)
        if n_blocks < MIN_BLOCKS:
            # C1 在 C2 之前:块数都不够就不花第二条查询(梯子顺序即
            # 取数顺序,记账与判定共用同一条序)。
            return _stop("C1", "insufficient_blocks",
                         measured=n_blocks, threshold=MIN_BLOCKS)
        t_pre = t_hist[-1]

        # ── 第二条:对照帧(dim 口径含供体池 / filters 窄序列) ──
        ctrl = None
        c_reason = ""
        if causal.control is None:      # 手改文件可绕过 lint
            c_reason = "no_control_declared"
        else:
            try:
                ctrl = await fetch_control_series(
                    semantic_layer=self._provider_for(datasource, dialect),
                    runner=self._query_runner(datasource),
                    datasource=datasource, dialect=dialect, metric_name=metric,
                    matched=list(matched or []),
                    subject_filters=rule.subject.filters,
                    control=causal.control, window=cur_window,
                    time_field=time_field, grain=seasonal.grain,
                    mode=seasonal.mode, lookback=lookback)
            except Exception as e:
                c_reason = f"control_query_failed:{str(e)[:120]}"
            if ctrl is not None and ctrl.series is not None:
                ledger.record("causal")
                if ctrl.series.unmatched:
                    degraded.append(
                        {"stage": "causal",
                         "reason": f"unmatched_buckets:{ctrl.series.unmatched}"})
            elif ctrl is not None:
                c_reason = "control_series_unavailable"
            if ctrl is not None and ctrl.donor_reason:
                degraded.append({"stage": "causal",
                                 "reason": f"donors:{ctrl.donor_reason}"})

        c_hist: list[float | None] = []
        c_post: float | None = None
        if ctrl is not None and ctrl.series is not None:
            label = ctrl.label if ctrl.mode == "dim" else ""
            c_hist, c_post = split_post(ctrl.series.values(label))
            if not c_hist or c_hist[-1] is None or c_post is None:
                c_reason = c_reason or "control_data_missing"
        has_control = not c_reason

        # ── 估计:DiD(手算)+ placebo 前窗 + 合成对照(可选) ──
        pairs: list[dict[str, Any]] = []
        placebo: dict[str, Any] | None = None
        synthetic: dict[str, Any] | None = None
        if has_control:
            c_pre = c_hist[-1]
            att = did_2x2(t_pre, t_post, c_pre, c_post)
            # 块窗口两帧同规格(同 grain/mode/lookback/窗口),取处理组的
            # 历史窗口做 placebo 标签 —— 两条 SQL 的块网格逐格对齐。
            pairs = placebo_pairs(t_hist, c_hist, blocks=treated.blocks[:-1],
                                  count=causal.placebo_blocks)
            se = did_se(pairs)
            if att is not None:
                section["did"] = {**att, "se": se,
                                  "crosses_zero": crosses_zero(att["att"], se)}
            placebo = parallel_trends(pairs, scale=max(abs(t_pre), abs(c_pre)),
                                      tolerance=causal.tolerance)
            if causal.mode == "auto" and ctrl is not None and ctrl.donors:
                synthetic = donor_counterfactual(
                    treated.values(""), ctrl.donors,
                    tolerance=causal.tolerance)
                synthetic["se"] = se
                synthetic["crosses_zero"] = crosses_zero(
                    synthetic.get("effect"), se)

        # ── 梯子判定(唯一一次;所有条件已实测) ────────────
        ladder = ladder_decision(
            seasonal_declared=True, n_blocks=n_blocks, has_pre_block=True,
            has_control=has_control, budget_ok=True, placebo=placebo,
            mode=causal.mode, synthetic=synthetic)
        if synthetic is None and ctrl is not None and ctrl.donor_reason:
            # C6 的原因细化为结构性原因(treated_unidentified /
            # donors_need_dim_control 的修法各自不同,不塌成一句
            # synthetic_unavailable)。
            for u in ladder["unmet"]:
                if u.get("condition") == "C6" \
                        and u.get("reason") == "synthetic_unavailable":
                    u["reason"] = ctrl.donor_reason
        if not has_control and c_reason:
            for u in ladder["unmet"]:
                if u.get("condition") == "C2":
                    u["reason"] = c_reason

        section.update({
            "rung": ladder["rung"],
            "unmet": ladder["unmet"],
            "grain": treated.grain,
            "block_mode": treated.mode,
            "lookback": lookback,
            "blocks": [[b[0], b[1]] for b in treated.blocks],
            "treated": {"label": "", "blocks_with_data": n_blocks,
                        "pre": t_pre, "post": t_post, "sql": treated.sql},
            "assumptions": assumptions_for(
                ladder["rung"], placebo=placebo, synthetic=synthetic,
                unmet=ladder["unmet"],
                context=f"粒度 {treated.grain}·{treated.mode}·前窗 {n_blocks} 块"),
        })
        if ctrl is not None:
            section["control"] = {
                "mode": ctrl.mode,
                "label": ctrl.label,
                "treated_labels": ctrl.treated_labels,
                "donors": sorted(ctrl.donors),
                "pre": c_hist[-1] if c_hist else None,
                "post": c_post,
                "sql": ctrl.series.sql if ctrl.series is not None else "",
            }
        if placebo is not None:
            section["placebo"] = placebo
        if synthetic is not None:
            section["synthetic"] = synthetic
        return section

    def _query_runner(self, datasource: str):
        """桥与签名带共用的 runner 契约:``(sql, ds) -> (columns, rows)``。

        异常原样上抛(桥与显著性阶段各自按自己的语义处理:桥吞成
        degraded,显著性按 require 决定降级还是 error run)。
        """
        async def _runner(sql: str, ds: str):
            result = await self._run(sql, ds or datasource)
            return list(result.columns), list(result.rows)
        return _runner

    async def _analysis_bridge(
        self, rule: DecisionRule, datasource: str, dialect: str,
        rows: list[dict[str, Any]],
        cur_window: tuple[str, str] | None,
        base_window: tuple[str, str] | None,
        matched: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """触发后的确定性"为什么"(补丁 1)。绝不上抛 —— 见 bridge 模块。

        ``None`` = 桥对这条规则不适用(指标不可分解/无时间字段),证据里
        就不写这一节;该做而没做成的情况由桥自己记 ``degraded``。

        ``matched`` 从判定自己的编译产物里原样传递:桥再推一遍锚定,就多
        了一处能与判定漂移的推论 —— 判定按什么表编译的,桥就得按同一份。
        """
        from trove.services.decision.bridge import run_bridge

        try:
            provider = self._provider_for(datasource, dialect)
        except Exception as e:
            logger.warning("analysis bridge: no semantic provider for %s: %s",
                           datasource, e)
            return {
                "top_components": [], "tree": None,
                "residual": {"value": None, "exact": False,
                             "reason": "no_semantic_provider"},
                "queries": [],
                "degraded": [{"stage": "analysis_bridge",
                              "reason": str(e)[:200]}],
            }

        _runner = self._query_runner(datasource)

        try:
            return await run_bridge(
                semantic_layer=provider, runner=_runner, rule=rule,
                datasource=datasource, dialect=dialect,
                cur_period=cur_window, base_period=base_window,
                judged_rows=rows, matched=list(matched or []),
            )
        except Exception as e:
            # 第二层保险:``run_bridge`` 自己已保证不上抛,但如果没有这层,
            # 桥里将来某个未预料的异常形态会顺着 ``evaluate`` 的兜底把一次
            # **已经判完的判定**降级成 error run —— 附录拖垮正文。记账,
            # 判定照常交付(与上面 provider 缺失同一形状)。
            logger.exception("analysis bridge raised for rule %s", rule.id)
            return {
                "top_components": [], "tree": None,
                "residual": {"value": None, "exact": False,
                             "reason": "bridge_error"},
                "queries": [],
                "degraded": [{"stage": "analysis_bridge",
                              "reason": str(e)[:200]}],
            }

    async def _run(self, sql: str, datasource: str):
        """Read-only execution through the registry's guarded channel.

        ``ConnectorRegistry.execute`` rejects anything that is not a SELECT,
        so a decision rule cannot write to a business datasource even if the
        compiler were somehow talked into emitting DML.

        有界:每次执行一条各自的预算(不是整条规则共享)—— 规则最多跑两次
        (当前期 + 基准期),一次卡死不该把另一次的额度也吃掉。超时折成
        ``DecisionError``:run 上显式报错,绝不静默变 OK(见模块 docstring)。
        """
        try:
            return await asyncio.wait_for(
                self.connectors.execute(sql, datasource),
                timeout=self._timeout_ms / 1000.0,
            )
        except asyncio.TimeoutError:
            raise DecisionError(
                f"query timed out after {self._timeout_ms}ms on {datasource!r}"
            )
        except Exception as e:
            raise DecisionError(f"query failed on {datasource!r}: {e}") from e

    def _judge(
        self, rule: DecisionRule, cond, cur_map: dict[str, float | None],
        base_map: dict[str, float | None], row_count: int, *,
        confidence_by_dim: dict[str, float | None] | None = None,
        gated_by_dim: dict[str, bool] | None = None,
        require_gate: bool = False,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Thin shell → module-level :func:`judge`(B4 提取;签名不变)。"""
        return judge(rule, cond, cur_map, base_map, row_count,
                     confidence_by_dim=confidence_by_dim,
                     gated_by_dim=gated_by_dim, require_gate=require_gate)

    @staticmethod
    def _cond_hit(text: str, scope: dict[str, Any]) -> bool:
        """Thin shell → module-level :func:`cond_hit`(B4 提取)。"""
        return cond_hit(text, scope)

    @staticmethod
    def _fmt(value: Any) -> str:
        n = as_number(value)
        if n is None:
            return "—"
        return f"{n:,.2f}".rstrip("0").rstrip(".")

    def _summarize(
        self, rule: DecisionRule, rows: list[dict[str, Any]], dims: list[str],
        *, net: str = "",
    ) -> str:
        """One-line human summary: what fired, for which groups, on what numbers.

        ``dims`` is what the rule decided to emit (see ``_judge``), so an
        `emit: top_k` rule reports its top groups, not every group that
        happened to satisfy the condition.

        ``net`` = 因果梯的一行缀(``causal_line`` 的产物;**仅 rung
        L2/L3 且有效应时非空**)—— L1 明说「不主张因果」,拒绝主张的
        效应不该上通知。缀在末尾而不是替换正文:触发的事实与反事实的
        估计是两层信息,不能互相顶掉。
        """
        head = rule.describe()
        emitted = set(dims)
        parts = []
        for r in rows:
            if not r["triggered"] or r["dim"] not in emitted:
                continue
            label = f"{r['dim']}: " if r["dim"] else ""
            bits = [f"当期 {self._fmt(r['current'])}"]
            if r["baseline"] is not None:
                bits.append(f"基期 {self._fmt(r['baseline'])}")
            if r["delta_pct"] is not None:
                bits.append(f"变化 {r['delta_pct'] * 100:+.1f}%")
            why = "; ".join(r["matched"]) or "; ".join(rule.conditions)
            parts.append(f"{label}{', '.join(bits)} 〔{why}〕")
        line = f"[{rule.severity}] {head} — " + " | ".join(parts)
        return f"{line} {net}" if net else line

    # ── entry point used by the runner ────────────────────

    async def evaluate(
        self, rule: DecisionRule, datasource: str, now: datetime | None = None,
        *, rule_digest: str = "",
    ) -> DecisionOutcome:
        """``evaluate_rule`` with every failure folded into ``error``.

        The runner must never see an exception here: an unhandled one would
        abandon the run record. A failure becomes an outcome with ``error``
        set and ``triggered`` False, which the runner stores as status
        ``error`` — visible, and never a notification.
        """
        try:
            return await self.evaluate_rule(rule, datasource, now,
                                            rule_digest=rule_digest)
        except (DecisionError, RuleError, DecisionExprError) as e:
            logger.warning("decision rule %s failed: %s", rule.id, e)
            return self._failed(rule, datasource, e, rule_digest)
        except Exception as e:  # pragma: no cover - defensive
            logger.exception("decision rule %s crashed", rule.id)
            return self._failed(rule, datasource, e, rule_digest)

    @staticmethod
    def _failed(
        rule: DecisionRule, datasource: str, e: Exception, rule_digest: str,
    ) -> DecisionOutcome:
        # The digest is recorded on failures too: "which version of the rule
        # could not be judged" is the first question when one starts erroring.
        return DecisionOutcome(
            triggered=False, message="", rule_id=rule.id,
            severity=rule.severity, error=str(e)[:300],
            evidence={"rule_id": rule.id, "rule_digest": rule_digest,
                      "error": str(e)[:300],
                      "provenance": {"datasource": datasource}},
        )
