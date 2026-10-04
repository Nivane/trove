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
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from trove.core.logging import get_logger
from trove.services.decision.expr import DecisionExprError, as_number
from trove.services.decision.rules import DecisionRule, RuleError, compile_condition
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
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


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
        time_filters = [
            {"field": ref, "op": ">=", "value": window[0]},
            {"field": ref, "op": "<=", "value": window[1]},
        ]
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

        cur_info = await self._compile(rule, model, dialect, datasource, cur_window)
        cur_sql = cur_info["sql"]
        cur_cols = list(cur_info.get("columns") or [])
        cur_result = await self._run(cur_sql, datasource)
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
            base_map = self._to_map(
                list(base_info.get("columns") or []), base_result.rows)

        rows, triggered_dims = self._judge(rule, cond, cur_map, base_map,
                                           cur_result.row_count)
        triggered = bool(triggered_dims)

        # 分析桥(补丁 1):只在触发时跑,未触发零成本。任何失败只进
        # evidence 的 degraded —— 判定已经判完,桥是附录不是前置。
        analysis = None
        if triggered:
            analysis = await self._analysis_bridge(
                rule, datasource, dialect, rows, cur_window, base_window,
                matched=list(cur_info.get("datasets") or []))

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

        return DecisionOutcome(
            triggered=triggered,
            message=self._summarize(rule, rows, triggered_dims) if triggered else "",
            rule_id=rule.id,
            severity=rule.severity,
            evidence=evidence,
        )

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

        async def _runner(sql: str, ds: str):
            result = await self._run(sql, ds)
            return list(result.columns), list(result.rows)

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
        base_map: dict[str, float | None], row_count: int,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Per-group verdicts → (row cards, the groups that triggered)."""
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
            hit = cond.eval(scope_vars) is True
            # Only a satisfied group lists its conditions: under `all`, a
            # half-matched group would otherwise look like it fired.
            matched = ([c for c in rule.conditions if self._cond_hit(c, scope_vars)]
                       if hit else [])
            rows.append({
                "dim": dim,
                "current": _jsonable(cur),
                "baseline": _jsonable(base),
                "delta": _jsonable(delta),
                "delta_pct": _jsonable(delta_pct),
                "contribution": _jsonable(contrib_by_dim.get(dim)),
                "triggered": hit,
                "matched": matched,
            })
            if hit:
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

    @staticmethod
    def _cond_hit(text: str, scope: dict[str, Any]) -> bool:
        """Whether one condition text matched — for the `matched` audit list.
        A condition that fails to parse is already fatal upstream."""
        from trove.services.decision.expr import parse_condition

        try:
            return parse_condition(text).eval(scope) is True
        except DecisionExprError:
            return False

    @staticmethod
    def _fmt(value: Any) -> str:
        n = as_number(value)
        if n is None:
            return "—"
        return f"{n:,.2f}".rstrip("0").rstrip(".")

    def _summarize(
        self, rule: DecisionRule, rows: list[dict[str, Any]], dims: list[str],
    ) -> str:
        """One-line human summary: what fired, for which groups, on what numbers.

        ``dims`` is what the rule decided to emit (see ``_judge``), so an
        `emit: top_k` rule reports its top groups, not every group that
        happened to satisfy the condition.
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
        return f"[{rule.severity}] {head} — " + " | ".join(parts)

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
