"""Deterministic join resolution for the semantic layer.

JoinResolver turns the declared relationship graph (OSSIE ``relationships``)
plus data-verified naming fallback edges into the authoritative ON-clause
list for a question's matched tables — mirroring how MetricFlow resolves
the join graph instead of letting the LLM invent join keys.

Properties:
- declared many→one relationships win over naming-convention edges for the
  same table pair;
- connected component built by BFS from the anchor (best-scored) table,
  possibly routing through intermediate tables the question never names
  (e.g. loan + district join through account);
- output is deterministic for a given input, so the rendered block stays
  byte-identical across a question's correction rounds (schema-budget cache
  stability).
"""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticMetric,
    SemanticModel,
)

# 已验证 hint 文本(可带 "(N/M match)" 重叠后缀):src.col → dst.col
_HINT_RE = re.compile(r"^(\S+)\.(\S+) → (\S+)\.(\S+)(?:\s+\(.*\))?$")


@dataclass(frozen=True)
class JoinEdge:
    """One directed join key pair: ``from_`` (many, FK owner) → ``to`` (one)."""

    from_: str
    to: str
    from_column: str
    to_column: str
    declared: bool


@dataclass
class JoinResolution:
    """Result of resolving joins for a matched table set.

    clauses: BFS 树序遍历的 ON 子句字符串(左深 FROM root JOIN(树序) 合法)。
    tree_edges: 与 clauses 一一对应的有向边(编译 JOIN 目标用,免字符串解析)。
    extra_tables: intermediate tables used by the join tree but not named
        in the matched set — schema blocks for them must be published too.
    """

    clauses: list[str] = field(default_factory=list)
    tree_edges: list["JoinEdge"] = field(default_factory=list)
    extra_tables: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.clauses


def _edge_from_hint(hint: str) -> JoinEdge | None:
    m = _HINT_RE.match(hint)
    if not m:
        return None
    src_t, src_c, dst_t, dst_c = m.groups()
    return JoinEdge(src_t, dst_t, src_c, dst_c, declared=False)


class JoinResolver:
    """Resolve authoritative join clauses over the declared + verified graph."""

    def __init__(self, model: SemanticModel | None = None):
        self._model = model

    # ── Edge sources ───────────────────────────────────────

    def _declared_edges(self, known: set[str]) -> list[JoinEdge]:
        """声明图里的边:两个端点都是模型数据集即取(含中间表跨联)。"""
        edges: list[JoinEdge] = []
        if self._model is None:
            return edges
        for r in self._model.relationships:
            if r.from_ not in known or r.to not in known:
                continue
            if not r.from_columns or not r.to_columns:
                continue
            for fc, tc in zip(r.from_columns, r.to_columns):
                edges.append(JoinEdge(r.from_, r.to, fc, tc, declared=True))
        return edges

    @staticmethod
    def _verified_edges(
        verified_by_table: dict[str, list[str]],
        known: set[str],
        seen_pairs: set[frozenset[str]],
    ) -> list[JoinEdge]:
        """数据级已验证的命名约定边(复用 schema_linking 的采样验证)。"""
        edges: list[JoinEdge] = []
        for table, hints in (verified_by_table or {}).items():
            for hint in hints:
                edge = _edge_from_hint(hint)
                if edge is None:
                    continue
                if edge.from_ not in known or edge.to not in known:
                    continue
                pair = frozenset((edge.from_, edge.to))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                edges.append(edge)
        return edges

    # ── Resolution ─────────────────────────────────────────

    def resolve(
        self,
        tables: list[str],
        verified_by_table: dict[str, list[str]] | None = None,
        root: str | None = None,
    ) -> JoinResolution:
        """ON 子句 + 中间表集合(声明优先,样本验证命名边回退,锚表 BFS)。

        边图 = 全量声明关系 ∪ 已验证命名边;从锚表(root,默认 matched[0])
        出发 BFS,子图连所有可达表——中间表(不在 matched 里的联表)也算,
        这正是 ''question 只点名 loan+district、实际要经 account 联'' 的场景。

        clauses 保持 BFS 树遍历序:对左深 FROM root JOIN(树序) 恒合法
        (每条树边的双亲先于孩子被访问)。确定性由 matched 顺序保证。
        """
        matched = list(tables or [])
        if len(matched) < 2:
            return JoinResolution()
        matched_set = set(matched)
        root = root or matched[0]

        declared = set()
        rel_tables: set[str] = set()
        if self._model is not None:
            declared = {d.name for d in self._model.datasets}
            for r in self._model.relationships:
                rel_tables.add(r.from_)
                rel_tables.add(r.to)
        # 端点表也计入 known:即使 datasets 块不全,关系图的节点也算数
        known = matched_set | declared | rel_tables | set(verified_by_table or {})

        edges = self._declared_edges(known)
        seen_pairs = {frozenset((e.from_, e.to)) for e in edges}
        edges.extend(self._verified_edges(verified_by_table or {}, known, seen_pairs))

        adjacency: dict[str, list[JoinEdge]] = {}
        for e in edges:
            adjacency.setdefault(e.from_, []).append(e)
            adjacency.setdefault(e.to, []).append(e)

        visited = {root} if root in adjacency or root in matched_set else set()
        queue: deque[str] = deque([root])
        tree: list[JoinEdge] = []
        while queue:
            table = queue.popleft()
            for edge in adjacency.get(table, []):
                other = edge.to if table == edge.from_ else edge.from_
                if other in visited:
                    continue
                visited.add(other)
                tree.append(edge)
                queue.append(other)

        clauses = [
            f"{e.from_}.{e.from_column} = {e.to}.{e.to_column}"
            for e in tree
        ]
        extra = sorted(visited - matched_set)
        return JoinResolution(clauses=clauses, tree_edges=tree, extra_tables=extra)

    @staticmethod
    def render(resolution: JoinResolution) -> str:
        """编译结果 → 注入 gen_sql 提示词的文本块(权威连线)。"""
        if resolution.empty:
            return ""
        lines = ["Relationships:"]
        lines += [f"- {c}" for c in resolution.clauses]
        if resolution.extra_tables:
            lines.append(
                "[join keeps these tables reachable: "
                + ", ".join(resolution.extra_tables)
                + "]"
            )
        return "\n".join(lines)


# ── Constrained-selection SQL compilation ────────────────────
#
# 对应 Snowflake Cortex Analyst 的「逻辑宇宙」/ MetricFlow 编译:LLM 只
# 输出构件级成分(metric / group_by / filters),编译器把每个成分落到已
# 声明的模型条目上(metric.expression / field / relationship),拼出权威
# SQL。任何成分无法映射到声明即严格 MISS(降级现有 LLM 通道)——不做
# 联表/列/过滤值的发明。

_COMPILE_OPS = {
    "=", "!=", "<>", "<", ">", "<=", ">=", "like", "ilike", "in",
}

_TEMPORAL_DTYPES = {"date", "time", "datetime", "datetimetz"}


@dataclass
class CompileResult:
    """编译产物:权威 SQL + 渲染给 gen_sql 的提示块。"""

    sql: str
    block: str  # ``Compiled SQL (authoritative)`` 段


def _qualified(tbl: str, expr: str, force_qualify: bool = True) -> str:
    """给字段投影加表限定(未限定才加)。"""
    ex = expr.strip()
    if "." in ex or not force_qualify:
        return ex
    return f"{tbl}.{ex}"


def _literal(value: Any) -> str:
    """WHERE 值字面量:字符串加单引号并转义,数值原样。"""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    return "'" + s.replace("'", "''") + "'"


def _agg_signature(expr_text: str) -> tuple[str, frozenset[str]] | None:
    """聚合表达式 → (函数名, 全限定列引用集合) 签名,用于 metric 对账。

    列引用带表前缀(``loan.amount``):只按裸列名匹配会把 trans.amount
    误认成 loan.amount。空列集(COUNT(*))是通配(见 _sig_compatible)。
    """
    from sqlglot import exp, parse_one

    try:
        tree = parse_one(expr_text)
    except Exception:
        return None
    funcs = list(tree.find_all(exp.AggFunc))
    if not funcs:
        return None
    f = funcs[0]
    name = f.sql().split("(", 1)[0].strip().lower()
    cols = frozenset(
        (f"{c.table}.{c.name}" if c.table else c.name).lower()
        for c in f.find_all(exp.Column) if c.name
    )
    return name, cols


def _sig_compatible(a: tuple[str, frozenset[str]], b: tuple[str, frozenset[str]]) -> bool:
    """函数名相同;列集一侧为空(COUNT(*) 通配)即视为兼容。"""
    if a[0] != b[0]:
        return False
    acols, bcols = a[1], b[1]
    if not acols or not bcols:
        return True
    return bool(acols & bcols)


class SemanticCompiler:
    """把 plan 的构件级成分编译成权威 SQL(只认已声明模型条目)。

    严格模式:metric/group_by/filter 每项都必须解析到模型里的 metric/
    field/relationship,任一项解析失败整体 MISS(返回 None)→ 管线降级
    现有 LLM 生成通道。这保证编译通过的 SQL 永远落在「逻辑宇宙」内。
    """

    def __init__(self, model: SemanticModel):
        self._model = model
        self._fields: dict[tuple[str, str], Any] = {}  # (dataset, field) → field
        self._datasets: dict[str, SemanticDataset] = {}
        for d in model.datasets:
            self._datasets[d.name] = d
            for f in d.fields:
                self._fields[(d.name, f.name)] = f

    # ── component resolution ─────────────────────────────

    def metrics(self) -> list[SemanticMetric]:
        return list(self._model.metrics)

    def _match_metric(self, plan: dict[str, Any]) -> SemanticMetric | None:
        """找与 plan 聚合意图签名一致的声明 metric;找不到(不在宇宙内)→ 严格 MISS。"""
        candidates = [str(plan.get("aggregation") or "").strip()]
        candidates += [
            str(a).strip() for a in (plan.get("answer_columns") or [])
            if "(" in str(a)
        ]
        plan_sigs = [s for s in (_agg_signature(c) for c in candidates) if s is not None]
        if not plan_sigs:
            # 没有任何聚合意图 → 列表问题,无 metric
            return None
        for m in self._model.metrics:
            msig = _agg_signature(m.expression)
            if msig is None:
                continue
            if any(_sig_compatible(plan_sig, msig) for plan_sig in plan_sigs):
                return m
        return None

    def _resolve_field(self, ref: str, matched: set[str]) -> tuple[str, Any] | None:
        """列引用(``col`` / ``table.col``)→ (dataset, field);找不到 → None。"""
        ref = (ref or "").strip()
        if not ref or ref == "*" or "(" in ref:
            return None
        if "." in ref:
            tbl, col = ref.split(".", 1)
            hit = self._fields.get((tbl, col))
            return (tbl, hit) if hit is not None else None
        # 无表限定:在 matched 数据集里找唯一命中
        hits = [
            (d.name, f) for (d, f) in self._fields.items()
            if d in matched and f.name == ref
        ]
        if len(hits) != 1:
            return None
        return hits[0]

    # ── compile ────────────────────────────────────────────

    def compile_from_plan(
        self,
        plan: dict[str, Any] | None,
        matched: list[str],
        verified_by_table: dict[str, list[str]] | None = None,
        force_dialect: str = "sqlite",
    ) -> CompileResult | None:
        """plan(构件级/extensible)→ 权威 SQL;任何成分 MISS → None。

        聚合问题(plan 声明 aggregation 或 answer_columns 含聚合表达式):
        必须有声明的 metric 匹配,非聚合 answer 列 = GROUP BY 维度;
        列表问题:answer 列 = 直接投影。两类都要求列在声明 field 内。
        """
        if not plan or not matched:
            return None
        matched_set = {str(t) for t in matched}

        agg_declared = str(plan.get("aggregation") or "").strip().lower() not in ("", "none")
        metric = self._match_metric(plan)
        if agg_declared and metric is None:
            return None  # 聚合问题但模型无对应 metric → 严格 MISS
        is_agg = metric is not None
        if metric is not None and metric.datasets and metric.datasets[0] not in matched_set:
            # metric 锚定表不在 matched → 生成的 SQL 会引用未覆盖表 → 严格 MISS
            return None

        out_cols: list[tuple[str, Any]] = []
        for ac in plan.get("answer_columns") or []:
            ac = str(ac).strip()
            if not ac or ac == "*" or "(" in ac:
                continue
            resolved = self._resolve_field(ac, matched_set)
            if resolved is None:
                if is_agg:
                    return None  # 聚合题的分组维度不在声明 field 内 → MISS
                # 列表题的非字段投影(如表达式)编译器无法拼 → 交给 LLM 通道
                return None
            out_cols.append(resolved)

        filters: list[tuple[str, Any, str, Any]] = []
        for cond in plan.get("conditions") or []:
            if not isinstance(cond, dict):
                return None
            field_ref = str(cond.get("field") or "").strip()
            op = str(cond.get("op") or "=").strip().lower()
            value = cond.get("value")
            resolved = self._resolve_field(field_ref, matched_set)
            if resolved is None:
                return None  # 过滤列不在声明 field 内 → 严格 MISS
            if op not in _COMPILE_OPS:
                return None
            if value is None:
                return None
            filters.append((resolved[0], resolved[1], op, value))

        if metric is None and not out_cols and not filters:
            return None  # 无可编译成分(简单问题由 fast_match/普通通道覆盖)

        # FROM:anchor 表 = metric 锚定表(须在 matched),否则 matched[0];
        # BFS 从此表起,树序 JOIN 恒为合法左深序列
        anchor = metric.datasets[0] if metric and metric.datasets else matched[0]
        if anchor not in matched_set:
            anchor = matched[0]

        resolution = JoinResolver(self._model).resolve(
            list(matched), verified_by_table or {}, root=anchor)
        joins = resolution.tree_edges if (not resolution.empty and resolution.tree_edges) else []

        projections: list[str] = []
        if is_agg:
            projections += [_qualified(t, f.expression) for t, f in out_cols]
            projections.append(metric.expression)
        else:
            projections += [_qualified(t, f.expression) for t, f in out_cols]

        where_parts = [
            f"{_qualified(tbl, f.expression)} {op.upper()} {_literal(value)}"
            for tbl, f, op, value in filters
        ]

        sql = "SELECT " + ", ".join(projections) + f"\nFROM {anchor}"
        joined = {anchor}
        for edge in joins:
            # BFS 树序:每条边连接一个已入表与一个新表 → JOIN 目标 = 未入者
            new_t = edge.to if edge.from_ in joined else edge.from_
            if new_t in joined:
                continue  # 防御:理论上不触发
            clause = f"{edge.from_}.{edge.from_column} = {edge.to}.{edge.to_column}"
            sql += f"\nJOIN {new_t} ON {clause}"
            joined.add(new_t)
        if where_parts:
            sql += "\nWHERE " + " AND ".join(where_parts)
        if is_agg and out_cols:
            gb = ", ".join(_qualified(t, f.expression) for t, f in out_cols)
            sql += f"\nGROUP BY {gb}"

        block = (
            "Compiled SQL (authoritative — generate exactly this SQL; only "
            "fix dialect or formatting if the schema demands it):\n"
            f"```sql\n{sql}\n```"
        )
        return CompileResult(sql=sql, block=block)


def validate_compiled_sql(
    sql: str, model: SemanticModel, matched: list[str],
) -> list[str]:
    """guardrail:编译产物只允许引用 matched ∪ 声明数据集,可解析检查。

    越界 → 违规列表(非空);编译器自身只产已声明成分,这是兜底网。
    """
    from sqlglot import exp, parse_one

    try:
        tree = parse_one(sql)
    except Exception as e:
        return [f"compiled SQL unparseable: {e}"]

    declared = {d.name for d in model.datasets}
    allowed = {t.lower() for t in (matched or [])} | {t.lower() for t in declared}

    tables = [t.name for t in tree.find_all(exp.Table)]
    actual = {t.lower() for t in tables if t}
    unknown = actual - allowed
    if unknown:
        return [f"compiled SQL references undeclared tables: {sorted(unknown)}"]
    return []