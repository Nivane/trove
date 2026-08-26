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
from trove.services.semantic_layer.plan import GRAINS, parse_ordering
from trove.services.semantic_layer.timegrain import date_trunc


def _is_many_to_many(cardinality: str) -> bool:
    """基数归一化判定:任意 M:N 拼写(``M:N``/``many-to-many``/``M2M`` 等)一律
    视为多对多(编译期拒 fan-out)。格式耦合曾导致 ``MANY-TO-MANY`` 漏检,
    行倍增 SQL 静默编译通过——归一化后只有真正声明 many→one 才放行。"""
    c = (cardinality or "").strip().upper().replace("_", " ").replace("-", " ")
    c = "".join(c.split())  # 去全部空白("M:N" 保留冒号)
    return c in {"MN", "M:N", "N:M", "M2M", "MANYTOMANY", "MANY:MANY"}


def _has_ambiguous_path(
    edges: list["JoinEdge"],
    subgraph: set[str],
    root: str,
    matched: set[str],
) -> bool:
    """相关子图内 root→任一 matched 表是否有多条简单路径(节点级去重)。

    边先按无序表对去重(复合键/同对重复声明算一条,不误伤),再做有限 DFS
    (每个目标最多找 2 条路径即提前返回,图规模小,成本可控)。
    图有环(如三角形)或双路由时:同一表对间存在两条不同节点序列 → 二义。
    """
    pair_adj: dict[str, set[str]] = {}
    for e in edges:
        if e.from_ not in subgraph or e.to not in subgraph:
            continue
        pair_adj.setdefault(e.from_, set()).add(e.to)
        pair_adj.setdefault(e.to, set()).add(e.from_)

    for target in matched:
        if target == root:
            continue
        count = 0
        stack = [(root, frozenset({root}))]
        while stack and count < 2:
            node, visited = stack.pop()
            if node == target:
                count += 1
                continue
            for nxt in pair_adj.get(node, ()):
                if nxt in visited:
                    continue
                stack.append((nxt, visited | {nxt}))
        if count > 1:
            return True
    return False


@dataclass(frozen=True)
class JoinEdge:
    """One directed join key pair: ``from_`` (many, FK owner) → ``to`` (one)."""

    from_: str
    to: str
    from_column: str
    to_column: str
    declared: bool
    cardinality: str = ""  # 空 = 安全(many→one);"M:N" = 编译期拒 fan-out


@dataclass
class JoinResolution:
    """Result of resolving joins for a matched table set.

    clauses: BFS 树序遍历的 ON 子句字符串(左深 FROM root JOIN(树序) 合法)。
    tree_edges: 与 clauses 一一对应的有向边(编译 JOIN 目标用,免字符串解析)。
    extra_tables: intermediate tables used by the join tree but not named
        in the matched set — schema blocks for them must be published too.
    fan_out: 树中使用了 M:N 边(P5.2,编译期拒) → 消费方应严格 MISS,
        交回 LLM 通道 + 规则链(fan-out 重复行)兜底,而不是产出行倍增 SQL。
    unknown_cardinality: 树上有边的基数未声明(空)——many→one 无从判定,
        消费方保守 MISS(宁可交 LLM,不赌安全)。
    ambiguous: 相关子图里 root→某 matched 表存在 >1 条简单路径——BFS
        先到先得不可审计,消费方严格 MISS(MetricFlow 式:二义在建模期暴露)。
    """

    clauses: list[str] = field(default_factory=list)
    tree_edges: list["JoinEdge"] = field(default_factory=list)
    extra_tables: list[str] = field(default_factory=list)
    fan_out: bool = False
    unknown_cardinality: bool = False
    ambiguous: bool = False

    @property
    def empty(self) -> bool:
        return not self.clauses


class JoinResolver:
    """Resolve authoritative join clauses over the declared join graph."""

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
                edges.append(JoinEdge(
                    r.from_, r.to, fc, tc, declared=True,
                    cardinality=(r.cardinality or "").upper(),
                ))
        return edges

    # ── Resolution ─────────────────────────────────────────

    def resolve(
        self,
        tables: list[str],
        root: str | None = None,
    ) -> JoinResolution:
        """ON 子句 + 中间表集合(纯声明关系图,锚表 BFS)。

        边图 = 全量声明关系;从锚表(root,默认 matched[0])出发 BFS,子图连
        所有可达表——中间表(不在 matched 里的联表)也算,这正是 ''question
        只点名 loan+district、实际要经 account 联'' 的场景。

        命名约定边不再有运行时回退通道(Phase B 移除 catalog 探测):join 图
        完全来自 KB 声明的 relationships,kb init 已确定性生成(含基数)。
        运行时路径绝无「有回退」的错觉。

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
        known = matched_set | declared | rel_tables

        edges = self._declared_edges(known)

        adjacency: dict[str, list[JoinEdge]] = {}
        for e in edges:
            adjacency.setdefault(e.from_, []).append(e)
            adjacency.setdefault(e.to, []).append(e)

        visited = {root} if root in adjacency or root in matched_set else set()
        queue: deque[str] = deque([root])
        parent_edge: dict[str, tuple[str, JoinEdge]] = {}  # child → (parent, edge)
        children: dict[str, list[str]] = {}
        while queue:
            table = queue.popleft()
            for edge in adjacency.get(table, []):
                other = edge.to if table == edge.from_ else edge.from_
                if other in visited:
                    continue
                visited.add(other)
                parent_edge[other] = (table, edge)
                children.setdefault(table, []).append(other)
                queue.append(other)

        # 保留"根→matched 路径上"的边;纯多余叶子(子树不含任何 matched 表)
        # 剪掉——否则无关的 M:N 边会误触发 fan-out,挡住合法编译。
        memo: dict[str, bool] = {}

        def leads_to_matched(node: str) -> bool:
            if node in memo:
                return memo[node]
            if node in matched_set:
                memo[node] = True
                return True
            memo[node] = any(leads_to_matched(c) for c in children.get(node, []))
            return memo[node]

        tree: list[JoinEdge] = []
        fan_out = False
        unknown_card = False
        for child, (parent, edge) in parent_edge.items():
            if not leads_to_matched(child):
                continue
            tree.append(edge)
            if _is_many_to_many(edge.cardinality):
                # P5.2:多对多经此边联(在 matched 路径上)→ 编译期拒 fan-out
                fan_out = True
            elif not (edge.cardinality or "").strip():
                # 边在联路径上但基数未声明 → many→one 无从判定,保守 MISS
                unknown_card = True

        # P2 路径二义性:相关子图里 root→任一 matched 表存在 >1 条简单路径。
        # BFS 先到先得选边不可审计(图有环/双路由时可能选到语义错误路径),
        # MetricFlow 式做法是把二义暴露在建模期——运行时发现即严格 MISS。
        # 相关子图按「root 可达 ∩ 可到 matched」在**图**上算,不能只依赖 BFS
        # 树:菱形里 client 的 district 被 account 先占,树里像死叶子,图上却是
        # 第二路由。边按无序表对去重后计路径(复合键/重复声明不算二义)。
        graph_edges = [e for e in edges if e.from_ in visited and e.to in visited]
        pair_adj: dict[str, set[str]] = {}
        for e in graph_edges:
            pair_adj.setdefault(e.from_, set()).add(e.to)
            pair_adj.setdefault(e.to, set()).add(e.from_)
        to_matched: set[str] = set()
        stack = list(matched_set)
        while stack:
            n = stack.pop()
            if n in to_matched:
                continue
            to_matched.add(n)
            for nb in pair_adj.get(n, ()):
                if nb not in to_matched:
                    stack.append(nb)
        relevant_graph = visited & to_matched
        ambiguous = _has_ambiguous_path(graph_edges, relevant_graph, root, matched_set)

        clauses = [
            f"{e.from_}.{e.from_column} = {e.to}.{e.to_column}"
            for e in tree
        ]
        extra = sorted(({e.from_ for e in tree} | {e.to for e in tree}) - matched_set - {root})
        return JoinResolution(
            clauses=clauses, tree_edges=tree, extra_tables=extra,
            fan_out=fan_out, unknown_cardinality=unknown_card,
            ambiguous=ambiguous)

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


def _is_time_field(f: Any) -> bool:
    """字段是否声明时间维度:is_time 标志 / semantic_role=time / 时态 datatype。"""
    if getattr(f, "is_time", False):
        return True
    if str(getattr(f, "semantic_role", "") or "").strip().lower() == "time":
        return True
    return str(getattr(f, "datatype", "") or "").lower() in _TEMPORAL_DTYPES


def resolve_time_field(
    model: "SemanticModel | None", matched: list[str],
    preferred: str | None = None,
) -> tuple[str, Any] | None:
    """matched 数据集里的时间字段 → (dataset, field)。

    ``preferred``(metric 的 agg_time_dimension,``loan.date`` 或裸列名):
    显式声明优先——即使 matched 内多个时间字段也能判定(解决"多时间字段
    无法注入时间过滤"的覆盖损失)。无 preferred 时仍要求 matched 内
    **唯一**的声明时间字段,否则 None 不猜。
    """
    if model is None:
        return None
    matched_set = {str(t) for t in (matched or [])}
    if preferred:
        ref = (preferred or "").strip()
        tbl = ref.split(".", 1)[0] if "." in ref else ""
        col = ref.split(".", 1)[1] if "." in ref else ref
        for d in model.datasets:
            if tbl and d.name != tbl:
                continue
            if d.name not in matched_set:
                continue
            for f in d.fields:
                if f.name == col and _is_time_field(f):
                    return (d.name, f)
        return None
    cands: list[tuple[str, Any]] = []
    for d in model.datasets:
        if d.name not in matched_set:
            continue
        for f in d.fields:
            if _is_time_field(f):
                cands.append((d.name, f))
    return cands[0] if len(cands) == 1 else None


@dataclass
class CompileResult:
    """编译产物:权威 SQL + 渲染给 gen_sql 的提示块。"""

    sql: str
    block: str  # ``Compiled SQL (authoritative)`` 段


@dataclass
class CompileMiss:
    """编译失败的结构化分因(eval hit-rate 归因用)。

    reason: 稳定 slug(见 MISS_REASONS);component: 人类可读的失败组件。
    消费方(planner/eval)用 reason 聚合;``compile_from_plan`` 旧契约
    将其映射回 None(字节级向后兼容)。
    """

    reason: str
    component: str = ""


# 编译失败原因全集(新增 MISS 分支必须进此集合,保证 eval 归因闭合)
MISS_REASONS = frozenset({
    "no_plan_or_matched",
    "no_metric_match",
    "metric_anchor_unmatched",
    "unresolved_answer_column",
    "unresolved_filter_field",
    "invalid_op",
    "missing_filter_value",
    "nothing_compilable",
    "fan_out",
    "unknown_cardinality",
    "unreachable_table",
    "ambiguous_join_path",
    "derived_cycle",
    "derived_depth",
    "derived_unresolved",
    "time_field_not_declared",
    "time_field_not_temporal",
    "bad_time_grain",
    "time_grain_without_aggregation",
    "having_metric_unknown",
    "having_without_aggregation",
    "guardrail_rejected",
})


def _qualified(tbl: str, expr: str, force_qualify: bool = True) -> str:
    """给字段投影加表限定(未限定才加)。"""
    ex = expr.strip()
    if "." in ex or not force_qualify:
        return ex
    return f"{tbl}.{ex}"


_ALREADY_LITERAL_RE = re.compile(
    r"^\s*(?:'[^']*'|\"[^\"]*\"|[+-]?\d+(?:\.\d+)?|\([^)]*\)|NULL|TRUE|FALSE|true|false)\s*$",
    re.I,
)


def _literal(value: Any) -> str:
    """WHERE 值字面量:字符串加单引号并转义,数值原样。

    plan 的 condition value 可能已带引号(``'C'``)、是值列表(``('C', 'D')``)
    或数字——此时原样透传,避免二次加引号(``'('C', 'D')'`` 会把 IN 列表
    变成字符串字面量,SQL 语义错误)。
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    if _ALREADY_LITERAL_RE.match(s):
        return s
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
        # 每次 compile 调用开头赋值(force_dialect / matched):派生内联与
        # 时间分桶的方言渲染、裸列解析锚定都依赖这两个会话态。
        self._dialect: str = "sqlite"
        self._matched_set: set[str] = set()

    # ── component resolution ─────────────────────────────

    def metrics(self) -> list[SemanticMetric]:
        return list(self._model.metrics)

    def _metric_by_name(self, name: str | None) -> SemanticMetric | None:
        """按度量名精确匹配(大小写不敏感)。派生度量名引用/裸名候选共用。"""
        n = (name or "").strip().lower()
        for m in self._model.metrics:
            if m.name.strip().lower() == n:
                return m
        return None

    @staticmethod
    def _agg_candidates(plan: dict[str, Any]) -> list[str]:
        """聚合候选(按 plan 顺序):aggregation 字段 + 每个含 "(" 的 answer 列。"""
        candidates: list[str] = []
        agg = str(plan.get("aggregation") or "").strip()
        if agg:
            candidates.append(agg)
        for ac in plan.get("answer_columns") or []:
            ac = str(ac).strip()
            if "(" in ac:
                candidates.append(ac)
        return candidates

    def _match_candidate(self, cand: str) -> SemanticMetric | None:
        """候选 → 声明度量:先 metric 名精确匹配(裸名/派生度量),再聚合签名。"""
        m = self._metric_by_name(cand)
        if m is not None:
            return m
        sig = _agg_signature(cand)
        if sig is None:
            return None
        for m in self._model.metrics:
            msig = _agg_signature(m.expression)
            if msig is None:
                continue
            if _sig_compatible(sig, msig):
                return m
        return None

    def _match_metrics(
        self, plan: dict[str, Any],
    ) -> tuple[list[tuple[str, SemanticMetric]], list[str]]:
        """多度量解析:(候选, 度量)有序列表 + 未命中候选列表。

        每个有聚合签名的候选都必须命中声明度量(严格 MISS);无签名的
        候选(占位别名如 ``number(*)``)跳过,与旧单度量行为一致。
        """
        matched: list[tuple[str, SemanticMetric]] = []
        misses: list[str] = []
        for cand in self._agg_candidates(plan):
            metric = self._match_candidate(cand)
            if metric is not None:
                matched.append((cand, metric))
            elif _agg_signature(cand) is not None:
                misses.append(cand)
        return matched, misses

    _DERIVED_TYPES = ("derived", "ratio")
    _MAX_INLINE_DEPTH = 5

    def _inline_metric(
        self, metric: SemanticMetric, stack: frozenset[str] = frozenset(), depth: int = 0,
    ) -> str | CompileMiss:
        """度量表达式内联:derived/ratio 的裸列按「metric 名 → 声明字段」解析。

        - 非派生度量原样返回表达式(单度量旧 plan 字节级不变);
        - 裸标识符先按度量名递归内联(环检测 + 深度≤5),再按声明字段
          补表限定(歧义/未声明 → 严格 MISS,不猜);
        - 表限定列原样保留,交给 JoinResolver/投影表守卫兜底。
        """
        if metric.metric_type not in self._DERIVED_TYPES:
            return metric.expression
        if metric.name in stack:
            return CompileMiss("derived_cycle", metric.name)
        if depth >= self._MAX_INLINE_DEPTH:
            return CompileMiss("derived_depth", metric.name)
        from sqlglot import exp, parse_one

        try:
            tree = parse_one(metric.expression)
        except Exception:
            return CompileMiss("derived_unresolved", f"{metric.name}: unparseable")
        for col in list(tree.find_all(exp.Column)):
            if col.table:
                continue  # 已限定列:交投影表守卫校验
            target = self._metric_by_name(col.name)
            if target is not None:
                sub = self._inline_metric(target, stack | {metric.name}, depth + 1)
                if isinstance(sub, CompileMiss):
                    return sub
                col.replace(parse_one(sub))
                continue
            resolved = self._resolve_field(col.name, self._matched_set)
            if resolved is None:
                return CompileMiss("derived_unresolved", f"{metric.name}: {col.name}")
            col.set("table", exp.to_identifier(resolved[0], quoted=True))
        return tree.sql(dialect=self._dialect)

    @staticmethod
    def _referenced_tables(sql: str) -> set[str]:
        """SQL 引用的表名集合(列限定符 + FROM/JOIN 目标)。"""
        from sqlglot import exp, parse_one

        try:
            tree = parse_one(sql)
        except Exception:
            return set()
        refs = {c.table.lower() for c in tree.find_all(exp.Column) if c.table}
        refs |= {t.name.lower() for t in tree.find_all(exp.Table) if t.name}
        return refs

    def _resolve_field(self, ref: str, matched: set[str]) -> tuple[str, Any] | None:
        """列引用(``col`` / ``table.col``)→ (dataset, field);找不到 → None。

        先按字段名精确匹配;未命中时追加同数据集内 **synonyms 唯一命中**
        (如 ``district.region`` → 字段 ``A3``)——补偿 planner 直接写别名列
        的场景。歧义(多个同义字段)不猜,转 None 走 LLM 通道。
        """
        ref = (ref or "").strip()
        if not ref or ref == "*" or "(" in ref:
            return None

        if "." in ref:
            tbl, col = ref.split(".", 1)
            hit = self._fields.get((tbl, col))
            if hit is not None:
                return (tbl, hit)
            cands = [
                (ds, f) for (ds, _n), f in self._fields.items()
                if ds == tbl
                and any(s.lower() == col.lower() for s in f.synonyms if s)
            ]
            return cands[0] if len(cands) == 1 else None

        hits = [
            (ds, f) for (ds, _n), f in self._fields.items()
            if ds in matched and f.name == ref
        ]
        if len(hits) == 1:
            return hits[0]
        cands = [
            (ds, f) for (ds, _n), f in self._fields.items()
            if ds in matched
            and any(s.lower() == ref.lower() for s in f.synonyms if s)
        ]
        return cands[0] if len(cands) == 1 else None

    def _render_metric_filter(
        self, metric: SemanticMetric, matched_set: set[str],
    ) -> tuple[str, CompileMiss | None]:
        """metric 内建 filter → 表限定的 WHERE 片段(列须解析到声明字段)。

        filter 是行级谓词字符串(``status = 'A'``)。裸列 → 声明字段
        (锚定 matched,与 conditions 同规);不可解析/含子查询/写节点 →
        保守 MISS(建模期应由 lint 拦截,运行时兜底不猜)。
        """
        text = (metric.filter or "").strip()
        if not text:
            return "", None
        from sqlglot import exp, parse_one

        try:
            tree = parse_one(text, dialect=self._dialect)
        except Exception:
            return "", CompileMiss("unresolved_filter_field", metric.name)
        if any(
            isinstance(n, (exp.Select, exp.Subquery, exp.Insert, exp.Update, exp.Delete, exp.Drop))
            for n in tree.walk()
        ):
            return "", CompileMiss("unresolved_filter_field", f"{metric.name}: filter")
        for col in list(tree.find_all(exp.Column)):
            if col.table:
                continue
            resolved = self._resolve_field(col.name, matched_set)
            if resolved is None:
                return "", CompileMiss("unresolved_filter_field", f"{metric.name}: {col.name}")
            col.set("table", exp.to_identifier(resolved[0], quoted=False))
        return f"({tree.sql(dialect=self._dialect)})", None

    # ── compile ────────────────────────────────────────────

    def compile_from_plan(
        self,
        plan: dict[str, Any] | None,
        matched: list[str],
        force_dialect: str = "sqlite",
    ) -> CompileResult | None:
        """旧契约:任何成分 MISS → None(字节级向后兼容,测试不变)。"""
        res = self.compile_detailed(plan, matched, force_dialect)
        return res if isinstance(res, CompileResult) else None

    def compile_detailed(
        self,
        plan: dict[str, Any] | None,
        matched: list[str],
        force_dialect: str = "sqlite",
    ) -> CompileResult | CompileMiss:
        """plan(构件级/extensible)→ 权威 SQL;任何成分 MISS → CompileMiss。

        聚合问题(plan 声明 aggregation 或 answer_columns 含聚合表达式):
        每个含签名的聚合候选都必须命中声明 metric(多度量,严格 MISS),
        非聚合 answer 列 = GROUP BY 维度;列表问题:answer 列 = 直接投影。
        两类都要求列在声明 field 内。派生度量递归内联(环/深度/未解析
        守卫),投影表守卫保证产物不引用连接树外数据集。
        """
        if not plan or not matched:
            return CompileMiss("no_plan_or_matched", "")
        matched_set = {str(t) for t in matched}
        self._matched_set = matched_set
        self._dialect = force_dialect or "sqlite"

        agg_declared = str(plan.get("aggregation") or "").strip().lower() not in ("", "none")
        matched_pairs, miss_candidates = self._match_metrics(plan)
        if miss_candidates:
            # 聚合候选有签名但无兼容度量 → 严格 MISS(逐候选分因)
            return CompileMiss("no_metric_match", ", ".join(miss_candidates))
        is_agg = bool(matched_pairs)
        if agg_declared and not is_agg:
            return CompileMiss("no_metric_match", str(plan.get("aggregation") or ""))
        for _cand, m in matched_pairs:
            if m.datasets and m.datasets[0] not in matched_set:
                # metric 锚定表不在 matched → 生成的 SQL 会引用未覆盖表 → 严格 MISS
                return CompileMiss("metric_anchor_unmatched", m.name)

        # 时间分桶:field 必须声明且为时间字段(严格 MISS),grain 白名单;
        # 「无聚合意图」的判定推迟到投影循环后(裸度量名可中途转为聚合题)。
        tg_field: tuple[str, Any] | None = None  # (dataset, field)
        tg_grain: str = ""
        tg_expr: str = ""  # 方言感知分桶表达式
        tg = plan.get("time_grain")
        if tg:
            if not isinstance(tg, dict):
                return CompileMiss("bad_time_grain", str(tg))
            field_ref = str(tg.get("field") or "").strip()
            grain = str(tg.get("grain") or "").strip().lower()
            if grain not in GRAINS:
                return CompileMiss("bad_time_grain", grain)
            resolved_t = self._resolve_field(field_ref, matched_set)
            if resolved_t is None:
                return CompileMiss("time_field_not_declared", field_ref)
            tf = resolved_t[1]
            if not (tf.is_time or (tf.datatype or "").lower() in _TEMPORAL_DTYPES):
                return CompileMiss("time_field_not_temporal", field_ref)
            tg_field, tg_grain = resolved_t, grain
            tg_expr = date_trunc(
                _qualified(resolved_t[0], tf.expression), grain, self._dialect)

        # 投影按 answer_columns 顺序原位替换聚合项为度量表达式,按度量名
        # 去重(aggregation 与 answer_columns 同度量只投影一次——保旧
        # 单度量 plan 字节级一致);聚合题裸名列在字段解析失败后按度量名
        # 兜底(派生度量裸名引用)。
        out_cols: list[tuple[str, Any]] = []
        projections: list[str] = []
        gb_exprs: list[str] = []  # GROUP BY 表达式(时间分桶字段用分桶表达式)
        seen_metrics: set[str] = set()
        tg_seen = False
        last_dim_idx: int | None = None
        by_candidate = dict(matched_pairs)
        for ac in plan.get("answer_columns") or []:
            ac = str(ac).strip()
            if not ac or ac == "*":
                continue
            if "(" in ac:
                metric = by_candidate.get(ac)
                if metric is None:
                    continue  # 无签名占位表达式(如 number(*))→ 跳过,同旧行为
                proj = self._inline_metric(metric)
                if isinstance(proj, CompileMiss):
                    return proj
                if metric.name not in seen_metrics:
                    seen_metrics.add(metric.name)
                    projections.append(proj)
                continue
            resolved = self._resolve_field(ac, matched_set)
            if resolved is None:
                # 裸度量名兜底(字段优先):命中声明度量即转为聚合题,
                # 支持派生度量名直接出现在 answer_columns(无 aggregation 字段)。
                metric = self._metric_by_name(ac)
                if metric is not None:
                    if metric.datasets and metric.datasets[0] not in matched_set:
                        return CompileMiss("metric_anchor_unmatched", metric.name)
                    proj = self._inline_metric(metric)
                    if isinstance(proj, CompileMiss):
                        return proj
                    matched_pairs.append((ac, metric))  # 供锚点选择/兜底
                    is_agg = True
                    if metric.name not in seen_metrics:
                        seen_metrics.add(metric.name)
                        projections.append(proj)
                    continue
                return CompileMiss("unresolved_answer_column", ac)
            out_cols.append(resolved)
            if tg_field is not None and resolved == tg_field:
                # 原始时间列按字段身份替换为分桶表达式(投影 + GROUP BY)
                projections.append(tg_expr)
                gb_exprs.append(tg_expr)
                tg_seen = True
            else:
                projections.append(_qualified(resolved[0], resolved[1].expression))
                gb_exprs.append(_qualified(resolved[0], resolved[1].expression))
            last_dim_idx = len(projections) - 1
        # 兜底:aggregation 解析的度量未出现在 answer_columns(旧 plan 形态)→ 追加一次
        if is_agg and not seen_metrics:
            m0 = matched_pairs[0][1]
            proj = self._inline_metric(m0)
            if isinstance(proj, CompileMiss):
                return proj
            seen_metrics.add(m0.name)
            projections.append(proj)
        if tg_field is not None:
            if not is_agg:
                # 时间分桶必须伴随聚合意图(裸度量名兜底可能中途转聚合题)
                return CompileMiss("time_grain_without_aggregation", tg_grain)
            if not tg_seen:
                # 时间字段不在 answer_columns → 分桶表达式插在维度列之后、度量之前
                pos = last_dim_idx + 1 if last_dim_idx is not None else 0
                projections.insert(pos, tg_expr)
                gb_exprs.insert(pos, tg_expr)

        filters: list[tuple[str, Any, str, Any]] = []
        for cond in plan.get("conditions") or []:
            if not isinstance(cond, dict):
                return CompileMiss("unresolved_filter_field", str(cond))
            field_ref = str(cond.get("field") or "").strip()
            op = str(cond.get("op") or "=").strip().lower()
            value = cond.get("value")
            resolved = self._resolve_field(field_ref, matched_set)
            if resolved is None:
                return CompileMiss("unresolved_filter_field", field_ref)
            if op not in _COMPILE_OPS:
                return CompileMiss("invalid_op", op)
            if value is None:
                return CompileMiss("missing_filter_value", field_ref)
            filters.append((resolved[0], resolved[1], op, value))

        # 聚合后过滤:having[].metric → HAVING(内联度量表达式);
        # having[].field → 折进 WHERE(行级)。field/metric 必须恰好一个,
        # op/value 与 conditions 同规。
        having_parts: list[str] = []
        for h in plan.get("having") or []:
            if not isinstance(h, dict):
                return CompileMiss("having_metric_unknown", str(h))
            metric_ref = str(h.get("metric") or "").strip()
            field_ref = str(h.get("field") or "").strip()
            if bool(metric_ref) == bool(field_ref):
                return CompileMiss(
                    "having_metric_unknown", f"field={field_ref} metric={metric_ref}")
            op = str(h.get("op") or "=").strip().lower()
            value = h.get("value")
            if op not in _COMPILE_OPS:
                return CompileMiss("invalid_op", op)
            if value is None:
                return CompileMiss("missing_filter_value", field_ref or metric_ref)
            if metric_ref:
                metric = self._metric_by_name(metric_ref)
                if metric is None:
                    return CompileMiss("having_metric_unknown", metric_ref)
                expr = self._inline_metric(metric)
                if isinstance(expr, CompileMiss):
                    return expr
                having_parts.append(f"{expr} {op.upper()} {_literal(value)}")
                continue
            resolved_h = self._resolve_field(field_ref, matched_set)
            if resolved_h is None:
                return CompileMiss("unresolved_filter_field", field_ref)
            filters.append((resolved_h[0], resolved_h[1], op, value))
        if having_parts and not is_agg:
            # 度量级 HAVING 只作用于聚合题;列表题挂 HAVING 是退化计划 → 严格 MISS
            return CompileMiss("having_without_aggregation", "")

        if not projections and not filters:
            # 无可编译成分(简单问题由 fast_match/普通通道覆盖)
            return CompileMiss("nothing_compilable", "")

        # FROM/join:以 **plan 声明的 tables** 为准(LLM 落地校验过的权威表
        # 集),而不是 schema_linking 的 matched 超集——matched 可能被通用
        # 字段名(order.amount 的 amount)或无关数据集污染,导致 join 路径
        # 二义误拒绝。plan tables 过滤到已声明数据集;为空则回退 matched
        # (旧行为,保持兼容)。BFS 从锚表起,树序 JOIN 恒为合法左深序列。
        declared_datasets = {d.name for d in self._model.datasets}
        join_tables = [
            str(t) for t in (plan.get("tables") or [])
            if str(t) in declared_datasets
        ]
        if not join_tables:
            join_tables = [t for t in matched if t in declared_datasets] or list(matched)
        join_set = set(join_tables)
        anchor = join_tables[0]
        for _cand, m in matched_pairs:
            if m.datasets and m.datasets[0] in join_set:
                anchor = m.datasets[0]
                break

        resolution = JoinResolver(self._model).resolve(
            list(join_tables), root=anchor)
        if resolution.fan_out:
            # P5.2:M:N 边在联路径上 → 编译期拒(行倍增),严格 MISS 回 LLM
            return CompileMiss("fan_out", ", ".join(matched))
        if resolution.unknown_cardinality:
            # 联路径上有未声明基数的边 → many→one 无从判定,保守 MISS(不赌安全)
            return CompileMiss("unknown_cardinality", ", ".join(matched))
        if resolution.ambiguous:
            # P2:root→matched 存在多条简单路径,BFS 先到先得不可审计 → 严格 MISS
            return CompileMiss("ambiguous_join_path", ", ".join(matched))
        joins = resolution.tree_edges if (not resolution.empty and resolution.tree_edges) else []

        where_parts = [
            f"{_qualified(tbl, f.expression)} {op.upper()} {_literal(value)}"
            for tbl, f, op, value in filters
        ]
        # metric 内建 filter 并入 WHERE(与条件过滤 AND 连接)。每个被选中
        # metric 的 filter 都要解析;任一失败 → 整体保守 MISS。同一 metric
        # 可能经 aggregation 与 answer_columns 各命中一次 → 按名去重。
        seen_metric_filters: set[str] = set()
        for _cand, m in matched_pairs:
            if not (m.filter or "").strip():
                continue
            if m.name in seen_metric_filters:
                continue
            seen_metric_filters.add(m.name)
            pred, miss = self._render_metric_filter(m, matched_set)
            if miss is not None:
                return miss
            if pred:
                where_parts.append(pred)

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
        if is_agg and gb_exprs:
            sql += f"\nGROUP BY " + ", ".join(gb_exprs)
        if having_parts:
            sql += "\nHAVING " + " AND ".join(having_parts)
        # 排序(呈现层,宽处理):metric 名 → 内联表达式;字段 → 限定表达式
        # (时间分桶字段用分桶表达式);不可解析列丢弃而非 MISS——排序不
        # 产生新拒绝向量,gen_sql 仍会收到 plan 文本里的 ordering 线索。
        order_parts: list[str] = []
        for column, direction in (parse_ordering(plan.get("ordering")) or []):
            metric_o = self._metric_by_name(column)
            if metric_o is not None:
                expr_o = self._inline_metric(metric_o)
                if isinstance(expr_o, CompileMiss):
                    return expr_o
                order_parts.append(f"{expr_o} {direction.upper()}")
                continue
            resolved_o = self._resolve_field(column, matched_set)
            if resolved_o is None:
                continue  # 丢弃(宽处理)
            if tg_field is not None and resolved_o == tg_field:
                order_parts.append(f"{tg_expr} {direction.upper()}")
            else:
                order_parts.append(
                    f"{_qualified(resolved_o[0], resolved_o[1].expression)} {direction.upper()}")
        if order_parts:
            sql += "\nORDER BY " + ", ".join(order_parts)

        # 投影表守卫:派生内联/字段解析后,产物引用的表必须 ⊆ 连接树
        # (防「matched 但无关系边」时 FROM anchor 无 JOIN 的非法/笛卡尔 SQL——
        # guardrail 只查 matched∪声明,这一层查的是连接树可达性)。
        allowed = {anchor.lower()} | {e.from_.lower() for e in joins} | {e.to.lower() for e in joins}
        bad = self._referenced_tables(sql) - allowed
        if bad:
            return CompileMiss(
                "unreachable_table",
                f"tables outside join tree: {', '.join(sorted(bad))}",
            )

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

    # 列级校验:每个带表限定的列,其表必须在 SQL 的 FROM/JOIN 里出现。
    # 表名校验只查「在不在宇宙内」,拦不住「SELECT district.A3 FROM loan」
    # 这类表在 allowed 但没 JOIN、产出笛卡尔/非法引用的编译产物。
    from_tables: set[str] = set()
    for node in tree.find_all(exp.Table):
        # 仅收 FROM/JOIN 位置的表(子查询/CTE 内由各自作用域负责)
        parent = node.parent
        if parent is not None and parent.key in ("from", "join"):
            from_tables.add(node.name.lower())
    dangling = sorted({
        c.table.lower() for c in tree.find_all(exp.Column)
        if c.table and c.table.lower() not in from_tables
    })
    if dangling:
        return [
            "compiled SQL references columns from tables not in FROM/JOIN: "
            f"{dangling}"
        ]
    return []


def _compiled_sql_sig(sql: str, dialect: str = "sqlite"):
    """编译 SQL 的结构签名(照抄校验的判据)。

    忽略列名/星号/别名/格式——只保留「改了就必然改变结果」的形状信号:
      - 投影列数 + 每列聚合函数名(COUNT(*) 与 COUNT(col) 同签名,
        编译器把 count(*) 归一到声明的 COUNT(col) 是合法等价);
      - 引用的表集合 + JOIN 数量;
      - WHERE 条件序列:每个条件 = (操作符, 字面量值元组)(改过滤值必改签名);
      - GROUP BY 列数。
    解析失败/非查询 → None(调用方保守放行)。
    """
    from sqlglot import exp, parse_one

    try:
        tree = parse_one(sql, read=dialect)
    except Exception:
        return None
    if not isinstance(tree, exp.Query):
        return None
    if isinstance(tree, exp.With):
        tree = tree.this
    select = tree if isinstance(tree, exp.Select) else tree
    if not isinstance(select, exp.Select):
        return None

    projections = []
    for e in select.expressions or []:
        agg = next((f for f in e.find_all(exp.AggFunc)), None)
        if agg is not None:
            name = agg.sql().split("(", 1)[0].strip().lower()
            projections.append(("agg", name))
        else:
            projections.append(("plain",))

    tables = set()
    src_nodes = [select.args.get("from_")] + (select.args.get("joins") or [])
    for s in src_nodes:
        if s is None:
            continue
        t = s.this if isinstance(s.this, exp.Table) else s.find(exp.Table)
        if t is not None:
            tables.add(t.name.lower())

    conds: list[tuple[str, tuple[str, ...]]] = []
    where = select.args.get("where")
    if where is not None:
        for node in where.walk():
            if not isinstance(node, exp.Binary):
                continue
            vals = tuple(
                str(l.this) for l in node.expression.find_all(exp.Literal)
            )
            conds.append((node.key, vals))

    group_cols = 0
    group = select.args.get("group")
    if group is not None:
        group_cols = len(group.expressions or [])
    return (
        tuple(projections),
        tuple(sorted(tables)),
        tuple(conds),
        len(select.args.get("joins") or []),
        group_cols,
    )


def compiled_sql_matches(
    compiled: str,
    generated: str,
    generated_dialect: str = "sqlite",
) -> tuple[bool, str]:
    """编译照抄校验:生成的 SQL 是否保留权威编译 SQL 的结果形状。

    结构签名比较(见 _compiled_sql_sig):改聚合/改过滤值/改投影宽度/
    改 join → 打回;COUNT(*) vs COUNT(col)、别名、格式、大小写 → 通过
    (与编译 SQL 语义等价,不是回归)。签名缺失(解析失败等)→ 保守放行。

    保守方向(避免误伤合法微调,返回 (True, "")):
      - 任一 SQL 解析失败/异常或非查询;
      - 目标方言不是 sqlite(编译方言):跨方言的函数/类型转换差异无法
        与实质偏离可靠区分,放行由 execute→rules 兜底。
    """
    if not compiled or not generated:
        return True, ""
    if (generated_dialect or "sqlite").lower() != "sqlite":
        return True, ""
    base = _compiled_sql_sig(compiled, "sqlite")
    other = _compiled_sql_sig(generated, "sqlite")
    if base is None or other is None:
        return True, ""
    if base == other:
        return True, ""
    return False, (
        "generated SQL does not preserve the compiled SQL's result shape "
        "(changed aggregation, filter values, projection, or joins)"
    )
