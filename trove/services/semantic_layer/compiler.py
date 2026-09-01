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
    allowed: set[str] | None = None,
) -> bool:
    """相关子图内 root→任一 matched 表是否有多条简单路径(节点级去重)。

    边先按无序表对去重(复合键/同对重复声明算一条,不误伤),再做有限 DFS
    (每个目标最多找 2 条路径即提前返回,图规模小,成本可控)。
    图有环(如三角形)或双路由时:同一表对间存在两条不同节点序列 → 二义。

    ``allowed``:DFS 只允许经过的表(查询实际涉及的 plan tables)。绕经
    **查询未涉及**的表的路径(星型 schema 共享维度的二次进入,如 client 与
    account 同连 district 时,查询只提 account→district,绕经 client 的
    第二路由)是虚假路由——对当前查询不可达/无语义,不计入二义,避免把
    正确 BFS 树误判成 ambiguous_join_path。缺省 = 全部相关表(旧行为)。
    """
    pair_adj: dict[str, set[str]] = {}
    for e in edges:
        if e.from_ not in subgraph or e.to not in subgraph:
            continue
        pair_adj.setdefault(e.from_, set()).add(e.to)
        pair_adj.setdefault(e.to, set()).add(e.from_)

    allowed = matched if allowed is None else allowed
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
                if nxt not in allowed:
                    continue  # 绕经查询未涉及的表 → 虚假路由,不计入
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
        needed: set[str] | None = None,
    ) -> JoinResolution:
        """ON 子句 + 中间表集合(纯声明关系图,锚表 BFS)。

        边图 = 全量声明关系;从锚表(root,默认 matched[0])出发 BFS,子图连
        所有可达表——中间表(不在 matched 里的联表)也算,这正是 ''question
        只点名 loan+district、实际要经 account 联'' 的场景。

        ``needed``:查询**实际需要**的表(组件引用的表,见
        SemanticCompiler._plan_needed_tables)。它同时决定:
          - 联表保留(BFS 树剪枝):只保留能到 needed 表的子树——query_sketch 在
            plan.tables 里误列的无关共享维度(如 client/account 同连的
            district)不会多余联入,也不会触发行倍增;
          - 歧义判定作用域:只数「路径节点都在 needed 内」的路径——绕经
            needed 之外表的虚假路由不计入二义。
        缺省 None = matched_set(旧行为,调用方不传时语义不变)。

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
        needed = set(needed) if needed else matched_set
        root = root or matched[0]

        declared = set()
        rel_tables: set[str] = set()
        if self._model is not None:
            declared = {d.name for d in self._model.datasets}
            for r in self._model.relationships:
                rel_tables.add(r.from_)
                rel_tables.add(r.to)
        # 路由能力表:在关系图里作过 many→one 的 from 端(自身拥有 FK),或
        # 是 M:N 边的 to 端——这类表是事实/关联/枢纽表,可作联桥。纯 1:N
        # 维度叶(只作 to 端、无 FK 也无 M:N,如 district)不能作为路由中间
        # 表:经它绕行 = 共享维度二次进入(虚假路由),会让 BFS 把 needed 表
        # 的父节点错赋到维度侧。
        rels = list(self._model.relationships) if self._model else []
        route_capable = {r.from_ for r in rels}
        route_capable |= {r.to for r in rels if _is_many_to_many(r.cardinality)}
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
                # 纯维度表不能作为路由中间表:需要它时才作为目标访问(进 needed),
                # 否则经它绕行 = 共享维度二次进入(虚假路由,会让 BFS 选错父节点)。
                if other not in needed and other not in route_capable:
                    continue
                visited.add(other)
                parent_edge[other] = (table, edge)
                children.setdefault(table, []).append(other)
                queue.append(other)

        # 保留"根→needed 路径上"的边;纯多余叶子(子树不含任何 needed 表)
        # 剪掉——否则无关的 M:N 边会误触发 fan-out,挡住合法编译,且 query_sketch
        # 误列但未被引用的表(如共享维度 district)会被多余联入(行倍增)。
        memo: dict[str, bool] = {}

        def leads_to_needed(node: str) -> bool:
            if node in memo:
                return memo[node]
            if node in needed:
                memo[node] = True
                return True
            memo[node] = any(leads_to_needed(c) for c in children.get(node, []))
            return memo[node]

        tree: list[JoinEdge] = []
        fan_out = False
        unknown_card = False
        for child, (parent, edge) in parent_edge.items():
            if not leads_to_needed(child):
                continue
            tree.append(edge)
            if _is_many_to_many(edge.cardinality):
                # P5.2:多对多经此边联(在 needed 路径上)→ 编译期拒 fan-out
                fan_out = True
            elif not (edge.cardinality or "").strip():
                # 边在联路径上但基数未声明 → many→one 无从判定,保守 MISS
                unknown_card = True

        # P2 路径二义性:相关子图里 root→任一 needed 表存在 >1 条简单路径。
        # BFS 先到先得选边不可审计(图有环/双路由时可能选到语义错误路径),
        # MetricFlow 式做法是把二义暴露在建模期——运行时发现即严格 MISS。
        # 相关子图按「root 可达 ∩ 可到 needed」在**图**上算,不能只依赖 BFS
        # 树:菱形里 client 的 district 被 account 先占,树里像死叶子,图上却是
        # 第二路由。边按无序表对去重后计路径(复合键/重复声明不算二义)。
        graph_edges = [e for e in edges if e.from_ in visited and e.to in visited]
        pair_adj: dict[str, set[str]] = {}
        for e in graph_edges:
            pair_adj.setdefault(e.from_, set()).add(e.to)
            pair_adj.setdefault(e.to, set()).add(e.from_)
        to_needed: set[str] = set()
        stack = list(needed)
        while stack:
            n = stack.pop()
            if n in to_needed:
                continue
            to_needed.add(n)
            for nb in pair_adj.get(n, ()):
                if nb not in to_needed:
                    stack.append(nb)
        relevant_graph = visited & to_needed
        # 只数「路径节点都在查询实际需要表(needed)内」的路径:绕经 needed 之
        # 外表(如 client 与 account 同连的 district 二次进入)的虚假路由不
        # 计入二义——否则正确 BFS 树被误判成 ambiguous_join_path。
        ambiguous = _has_ambiguous_path(
            graph_edges, relevant_graph, root, needed,
            allowed=needed)

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


# ── 权威联表路径(plan.joins, MetricFlow 显式路径) ──────────────
#
# 编译器默认用声明图 BFS 选边(P2:二义 → 严格 MISS)。当 query_sketch 在 plan 里
# 显式声明 ``joins`` 时,按声明路径选边——解决共享维度菱形(如 client 与
# account 同连 district 造成的第二条路由)而不用删关系。每条 join 必须是
# 已声明 relationship 的列对(路径选择而非造边),非法 → 严格 MISS 不静默改道。

_PLACEHOLDER_JOINS = {"", "none", "empty", "-", "(empty if none)", "null"}


def _explicit_join_edges(
    joins_value: Any, model: SemanticModel | None,
) -> tuple[list["JoinEdge"] | None, bool]:
    """plan.joins → (权威 JoinEdge 列表 | None, present)。

    present=False:joins 空/占位 → 调用方回退 BFS(行为不变)。
    present=True, edges=None:joins 非空但含未声明边/不可解析 → 严格 MISS,
        不静默忽略后走 BFS 改道(query_sketch 明确声明了与模型不一致的路径)。
    present=True, edges=[...]:权威路径(方向对齐声明 relationship)。
    """
    if model is None:
        return None, False
    if isinstance(joins_value, str):
        parts = [joins_value]
    elif isinstance(joins_value, list):
        parts = [str(x) for x in joins_value]
    else:
        return None, False
    text = " AND ".join(p for p in parts if str(p).strip())
    stripped = text.strip().lower()
    if stripped in _PLACEHOLDER_JOINS:
        return None, False

    from sqlglot import exp, parse_one

    # joins 是逗号/AND 分隔的多个 ``lhs = rhs`` 子句(query_sketch 输出):
    # 整体 parse 会被逗号卡死,逐子句解析后收集 EQ。
    clauses = [c for c in re.split(r"\s*,\s*|\s+and\s+", text, flags=re.I) if c.strip()]
    parsed = []
    for clause in clauses:
        try:
            parsed.append(parse_one(clause))
        except Exception:
            return None, True

    rel_edges: list[tuple[str, str, str, str, str]] = []
    for r in model.relationships:
        for fc, tc in zip(r.from_columns or [], r.to_columns or []):
            rel_edges.append(
                (r.from_.lower(), r.to.lower(), fc.lower(), tc.lower(),
                 (r.cardinality or "").upper()))
    out: list[JoinEdge] = []
    for tree in parsed:
        eqs = list(tree.find_all(exp.EQ))
        if len(eqs) != 1:
            return None, True
        eq = eqs[0]
        left, right = eq.left, eq.right
        if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
            return None, True
        lt = (left.table or "").strip().lower()
        lc = (left.name or "").strip().lower()
        rt = (right.table or "").strip().lower()
        rc = (right.name or "").strip().lower()
        if not (lt and lc and rt and rc):
            return None, True
        matched = None
        for f, t, fc, tc, card in rel_edges:
            if (f == lt and t == rt and fc == lc and tc == rc) or (
                f == rt and t == lt and fc == rc and tc == lc
            ):
                matched = (f, t, fc, tc, card)
                break
        if matched is None:
            return None, True
        out.append(JoinEdge(
            matched[0], matched[1], matched[2], matched[3],
            declared=True, cardinality=matched[4],
        ))
    return (out if out else None), True


def _left_deep_tree(edges: list[JoinEdge], anchor: str) -> list[JoinEdge] | None:
    """把权威 join 边组装成从 anchor 起的左深树(返回树序边;失败 → None)。

    与 BFS 树序同语义:每条树边双亲先于孩子被访问,保证左深 JOIN 合法。
    anchor 不在任何边 / 不成连通树(环或断开)→ None(调用方严格 MISS)。
    """
    in_tree = {anchor}
    remaining = list(edges)
    ordered: list[JoinEdge] = []
    while remaining:
        progressed = False
        for i, e in enumerate(remaining):
            if e.from_ in in_tree and e.to not in in_tree:
                in_tree.add(e.to)
                ordered.append(e)
                remaining.pop(i)
                progressed = True
                break
            if e.to in in_tree and e.from_ not in in_tree:
                in_tree.add(e.from_)
                ordered.append(e)
                remaining.pop(i)
                progressed = True
                break
        if not progressed:
            return None
    return ordered


def _anchor_candidates(anchor: str, join_tables: list[str], edges: list[JoinEdge]) -> list[str]:
    """权威路径的锚表候选(去重,保序):度量锚表优先,再 join_tables 顺序。

    只保留出现在显式边里的表——锚表不在边内则 _left_deep_tree 必然失败,
    提前剪掉。``anchor`` 通常是度量锚定表,优先尝试它。
    """
    edge_tables = {e.from_ for e in edges} | {e.to for e in edges}
    out: list[str] = []
    for cand in [anchor, *join_tables]:
        if cand in edge_tables and cand not in out:
            out.append(cand)
    return out


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
    消费方(query_sketch/eval)用 reason 聚合;``compile_from_plan`` 旧契约
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
    "enum_value_unresolved",
    "analysis_unsupported_type",
    "analysis_metric_unknown",
    "analysis_partition_unresolved",
    "analysis_order_unresolved",
    "analysis_time_required",
    "analysis_invalid",
    "limit_without_order",
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


def _enum_code_for(text: str, enum_display: dict[str, str]) -> str | None:
    """人类值/码 → 规范 code(经 enum_display 双向匹配,大小写不敏感)。

    先按 code 键匹配(identity 命中返回原键,保 SQL 用库里存的写法),
    再按可读词值匹配(``male``/``男性`` → ``M``),最后词级兜底:label 词集
    与输入词集互相子集(``weekly`` ↔ "weekly statements")且唯一命中才采纳;
    多 label 同命中(如裸 "statements" 同时是 monthly/weekly 的子集)→ None,
    调用方保守 MISS(值歧义,不猜)。
    """
    low = (text or "").strip().lower()
    if not low:
        return None
    for code, _label in enum_display.items():
        if str(code).lower() == low:
            return str(code)
    for code, label in enum_display.items():
        if str(label).strip().lower() == low:
            return str(code)
    in_tokens = set(re.findall(r"[a-z0-9]+", low))
    if not in_tokens:
        return None
    matches: list[str] = []
    for code, label in enum_display.items():
        label_tokens = set(re.findall(r"[a-z0-9]+", str(label).strip().lower()))
        if not label_tokens:
            continue
        if label_tokens <= in_tokens or in_tokens <= label_tokens:
            matches.append(str(code))
    if len(matches) == 1:
        return matches[0]
    return None


def _strip_quotes(text: str) -> str:
    """剥掉外层成对引号(``'M'`` / ``\"M\"`` → ``M``)。"""
    s = text.strip()
    if len(s) >= 2 and s[0] in ("'", '"') and s[-1] == s[0]:
        return s[1:-1]
    return s


def _normalize_enum_value(value: Any, enum_display: dict[str, str]) -> Any | None:
    """枚举字段的 condition 值 → 规范 code;任一元素无法归一 → None。

    - enum_display 为空 → 原样透传(未声明词表,无归一依据);
    - 标量(可带引号)→ 单个归一;
    - ``('C', 'D')`` 列表 → 逐元素归一。
    归一失败返回 None,调用方保守 MISS——绝不静默产出 ``gender='male'``
    这类 0 行 SQL(值不在声明词表 = 未覆盖,交拒绝/扩展流程)。
    """
    if not enum_display:
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _enum_code_for(str(value), enum_display)
    s = str(value).strip()
    if len(s) >= 2 and s[0] == "(" and s[-1] == ")":
        parts = [p.strip() for p in s[1:-1].split(",") if p.strip()]
        if not parts:
            return None
        out: list[str] = []
        for p in parts:
            code = _enum_code_for(_strip_quotes(p), enum_display)
            if code is None:
                return None
            out.append(code)
        # 列表元素带引号:后续 _literal 的已字面量正则才能透传,产出
        # IN ('F', 'M') 而非 IN (F, M)(裸标识符会解析成列引用)。
        return "(" + ", ".join(f"'{c.replace(chr(39), chr(39) * 2)}'" for c in out) + ")"
    return _enum_code_for(_strip_quotes(s), enum_display)


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

    def _plan_needed_tables(
        self, plan: dict[str, Any], matched_pairs: list[tuple[str, SemanticMetric]],
        matched_set: set[str],
    ) -> set[str]:
        """查询**实际需要**的表 = 组件引用的表(answer/条件/聚合度量/having/
        时间/排序),经 _resolve_field 把裸列引用也落到其数据集。

        与 plan.tables 的区别:plan.tables 是 query_sketch 声明的超集,可能误列
        共享维度等无关表(district);这里只取真正被引用/被回答需要的表——
        决定联表树的保留与歧义判定作用域,避免误列表触发虚假二义或被多余
        联入(行倍增)。distinct 是 ratio 的分子/分母都锚定同一数据集。
        """
        needed: set[str] = set()

        def _add(ref: Any) -> None:
            ref = str(ref or "").strip()
            if not ref or ref == "*" or "(" in ref:
                return
            if "." in ref:
                needed.add(ref.split(".", 1)[0].strip())
                return
            resolved = self._resolve_field(ref, matched_set)
            if resolved is not None:
                needed.add(resolved[0])

        if not plan:
            return needed
        for ac in plan.get("answer_columns") or []:
            _add(ac)
        for c in plan.get("conditions") or []:
            if isinstance(c, dict):
                _add(c.get("field"))
        for h in plan.get("having") or []:
            if isinstance(h, dict):
                _add(h.get("field"))
                _add(h.get("metric"))
        tg = plan.get("time_grain")
        if isinstance(tg, dict):
            _add(tg.get("field"))
        for col, _dir in parse_ordering(plan.get("ordering")) or []:
            _add(col)
        analysis = plan.get("analysis")
        if isinstance(analysis, dict):
            _add(analysis.get("metric"))
            _add(analysis.get("order_by"))
            for p in (analysis.get("partition_by") or []):
                _add(p)
        for _cand, m in matched_pairs:
            if m.datasets:
                needed.update(m.datasets)
        return needed

    def _resolve_field(self, ref: str, matched: set[str]) -> tuple[str, Any] | None:
        """列引用(``col`` / ``table.col``)→ (dataset, field);找不到 → None。

        先按字段名精确匹配;未命中时追加同数据集内 **synonyms 唯一命中**
        (如 ``district.region`` → 字段 ``A3``)——补偿 query_sketch 直接写别名列
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

        agg_declared = str(plan.get("aggregation") or "").strip().lower() not in ("", "none", "无")
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
        # 与 projections 等长:投影的展示名与语义引用(分析包装用)。
        # proj_display: 输出列名(dim 取字段尾缀 / metric 取度量名);
        # proj_ref: dim → (dataset, field) 元组;time-grain 插入 → ("__tg__",);
        # metric → SemanticMetric。
        proj_display: list[str] = []
        proj_ref: list[Any] = []
        seen_metrics: set[str] = set()
        tg_seen = False
        last_dim_idx: int | None = None
        by_candidate = dict(matched_pairs)

        def _display_for_ac(ac: str, resolved) -> str:
            """answer 列 → 输出列展示名:取表限定尾缀,去引号/括号。"""
            tail = str(ac).strip().split(".", 1)[-1].strip()
            tail = tail.strip("`\"'() ")
            if not tail:
                tail = str(resolved[1].name) if resolved else f"col{len(projections)}"
            return tail

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
                    proj_display.append(metric.name)
                    proj_ref.append(metric)
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
                        proj_display.append(metric.name)
                        proj_ref.append(metric)
                    continue
                return CompileMiss("unresolved_answer_column", ac)
            out_cols.append(resolved)
            disp = _display_for_ac(ac, resolved)
            if tg_field is not None and resolved == tg_field:
                # 原始时间列按字段身份替换为分桶表达式(投影 + GROUP BY)
                projections.append(tg_expr)
                gb_exprs.append(tg_expr)
                proj_display.append(disp or tg_grain)
                proj_ref.append(("__tg__", tg_grain))
                tg_seen = True
            else:
                projections.append(_qualified(resolved[0], resolved[1].expression))
                gb_exprs.append(_qualified(resolved[0], resolved[1].expression))
                proj_display.append(disp)
                proj_ref.append(resolved)
            last_dim_idx = len(projections) - 1
        # 兜底:aggregation 解析的度量未出现在 answer_columns(旧 plan 形态)→ 追加一次
        if is_agg and not seen_metrics:
            m0 = matched_pairs[0][1]
            proj = self._inline_metric(m0)
            if isinstance(proj, CompileMiss):
                return proj
            seen_metrics.add(m0.name)
            projections.append(proj)
            proj_display.append(m0.name)
            proj_ref.append(m0)
        if tg_field is not None:
            if not is_agg:
                # 时间分桶必须伴随聚合意图(裸度量名兜底可能中途转聚合题)
                return CompileMiss("time_grain_without_aggregation", tg_grain)
            if not tg_seen:
                # 时间字段不在 answer_columns → 分桶表达式插在维度列之后、度量之前
                pos = last_dim_idx + 1 if last_dim_idx is not None else 0
                projections.insert(pos, tg_expr)
                gb_exprs.insert(pos, tg_expr)
                proj_display.insert(pos, tg_grain)
                proj_ref.insert(pos, ("__tg__", tg_grain))

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
            # 枚举字段:值经 enum_display 归一(male/男性 → 'M');无法归一
            # → 保守 MISS(值不在声明词表 = 未覆盖,绝不静默产出 0 行 SQL)。
            if resolved[1].enum_display:
                normalized = _normalize_enum_value(value, resolved[1].enum_display)
                if normalized is None:
                    return CompileMiss("enum_value_unresolved", field_ref)
                value = normalized
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
            if resolved_h[1].enum_display:
                normalized_h = _normalize_enum_value(value, resolved_h[1].enum_display)
                if normalized_h is None:
                    return CompileMiss("enum_value_unresolved", field_ref)
                value = normalized_h
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

        resolution = None
        joins: list[JoinEdge] = []
        # 权威联表路径(plan.joins):query_sketch 显式声明的路径优先,免 BFS 猜。
        # 每条 join 必须是已声明 relationship(路径选择而非造边);非空但未
        # 声明/不成左深树 → 严格 MISS,不静默忽略后 BFS 改道。
        explicit, joins_present = _explicit_join_edges(plan.get("joins"), self._model)
        if joins_present:
            if explicit is None:
                return CompileMiss(
                    "ambiguous_join_path", "explicit joins reference undeclared edges")
            tree = None
            for cand in _anchor_candidates(anchor, join_tables, explicit):
                tree = _left_deep_tree(explicit, cand)
                if tree is not None:
                    break
            if tree is None:
                return CompileMiss(
                    "ambiguous_join_path", "explicit joins not a connected tree")
            joins = tree
        else:
            # 查询实际需要的表 = 组件引用的表(非 query_sketch 全集):决定联表树
            # 保留与歧义作用域,避免误列的共享维度(district)触发虚假二义
            # 或被多余联入。
            needed = self._plan_needed_tables(plan, matched_pairs, matched_set)
            resolution = JoinResolver(self._model).resolve(
                list(join_tables), root=anchor, needed=needed)
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

        # 窗口分析(plan.analysis):把聚合核心包一层窗口计算。内层排序延后
        # 到外层(窗口 ORDER BY 由 analysis 决定),LIMIT 也只落在外层。
        analysis = plan.get("analysis")
        limit = plan.get("limit")
        if analysis:
            wrapped = self._apply_analysis(
                sql, plan, matched_set, analysis, limit,
                projections, proj_display, proj_ref, tg_field, order_parts,
            )
            if isinstance(wrapped, CompileMiss):
                return wrapped
            sql = wrapped
        else:
            if order_parts:
                sql += "\nORDER BY " + ", ".join(order_parts)
            if limit is not None:
                if not order_parts:
                    return CompileMiss("limit_without_order", str(limit))
                sql += f"\nLIMIT {int(limit)}"

        block = (
            "Compiled SQL (authoritative — generate exactly this SQL; only "
            "fix dialect or formatting if the schema demands it):\n"
            f"```sql\n{sql}\n```"
        )
        return CompileResult(sql=sql, block=block)

    # ── 窗口分析编译(plan.analysis)────────────────────────────
    #
    # 把聚合核心包一层窗口函数:share(占比)/running_total(累计)/mom(环比
    # 增量)/yoy(同比增量)/pct_change(环比增长率)/rank(排名)。内层聚合
    # 投影逐列别名 _c{i},外层 SELECT 把窗口表达式投影为新列。任何组件
    # 无法解析到声明模型/内层投影 → 严格 MISS(分析意图不静默丢弃)。
    #
    # 方言:窗口函数在 sqlite≥3.25 / PG / MySQL8 / ClickHouse / DuckDB 均
    # 支持;内层由现有构建逻辑产出(方言感知),外层只包标准窗口语法。

    _YOY_LAG_BY_GRAIN = {"month": 12, "quarter": 4, "week": 52, "day": 365, "year": 1}
    _ANALYSIS_DISPLAY = {
        "share": "share",
        "running_total": "running_total",
        "mom": "mom_delta",
        "yoy": "yoy_delta",
        "pct_change": "pct_change",
        "rank": "rank",
    }

    def _apply_analysis(
        self,
        inner_sql: str,
        plan: dict[str, Any],
        matched_set: set[str],
        analysis: Any,
        limit: Any,
        projections: list[str],
        proj_display: list[str],
        proj_ref: list[Any],
        tg_field: Any,
        order_parts: list[str],
    ) -> str | CompileMiss:
        if not isinstance(analysis, dict):
            return CompileMiss("analysis_invalid", "analysis must be an object")
        atype = str(analysis.get("type") or "").strip().lower()
        if atype not in self._ANALYSIS_DISPLAY:
            return CompileMiss("analysis_unsupported_type", atype)

        # 目标度量:analysis.metric(缺省 = 内层唯一聚合度量);必须恰好一个
        # 度量投影——窗口计算是单度量的。
        metric_idxs = [i for i, r in enumerate(proj_ref) if isinstance(r, SemanticMetric)]
        if len(metric_idxs) != 1:
            return CompileMiss(
                "analysis_metric_unknown",
                str(analysis.get("metric") or f"{len(metric_idxs)} metrics"),
            )
        m_idx = metric_idxs[0]
        target_metric = str(analysis.get("metric") or "").strip()
        if target_metric:
            m = self._metric_by_name(target_metric)
            if m is None or m.name != proj_ref[m_idx].name:
                return CompileMiss("analysis_metric_unknown", target_metric)

        # 窗口排序字段(order_by):时间类分析必需(缺省取内层时间分桶列)。
        order_ref = str(analysis.get("order_by") or "").strip()
        time_idx = next(
            (i for i, r in enumerate(proj_ref)
             if isinstance(r, tuple) and r and r[0] == "__tg__"),
            None,
        )
        order_idx = None
        if order_ref:
            resolved = self._resolve_field(order_ref, matched_set)
            if resolved is None:
                return CompileMiss("analysis_order_unresolved", order_ref)
            order_idx = next(
                (i for i, r in enumerate(proj_ref)
                 if isinstance(r, tuple) and len(r) == 2 and r[1] == "__tg__" and r == resolved),
                None,
            )
            if order_idx is None:
                # 也允许按非时间维度排(如按 region)——窗口序仍需确定列
                order_idx = next(
                    (i for i, r in enumerate(proj_ref)
                     if isinstance(r, tuple) and len(r) == 2 and r == resolved),
                    None,
                )
            if order_idx is None:
                return CompileMiss("analysis_order_unresolved", order_ref)
        elif time_idx is not None:
            order_idx = time_idx

        # 窗口分区字段(partition_by):每个都须解析为内层维度投影。
        part_idx: list[int] = []
        for p in (analysis.get("partition_by") or []):
            resolved = self._resolve_field(str(p), matched_set)
            if resolved is None:
                return CompileMiss("analysis_partition_unresolved", str(p))
            idx = next(
                (i for i, r in enumerate(proj_ref)
                 if isinstance(r, tuple) and len(r) == 2 and r == resolved),
                None,
            )
            if idx is None:
                return CompileMiss("analysis_partition_unresolved", str(p))
            if idx not in part_idx:
                part_idx.append(idx)

        def ref(i: int) -> str:
            return f"_c{i}"

        def partition_sql() -> str:
            return ("PARTITION BY " + ", ".join(ref(i) for i in part_idx)) if part_idx else ""

        def order_sql() -> str:
            if order_idx is None:
                return ""
            direction = (str(analysis.get("direction") or "asc")).upper()
            return f"ORDER BY {ref(order_idx)} {direction}"

        needs_time = atype in ("running_total", "mom", "yoy", "pct_change")
        if needs_time and order_idx is None:
            return CompileMiss("analysis_time_required", atype)

        if atype == "share":
            win = f"SUM({ref(m_idx)}) OVER ({partition_sql()})"
            expr = f"{ref(m_idx)} / NULLIF({win}, 0)"
        elif atype == "running_total":
            win = (
                f"SUM({ref(m_idx)}) OVER ({partition_sql()} {order_sql()} "
                "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)"
            )
            expr = win
        elif atype == "mom":
            win = f"LAG({ref(m_idx)}, 1) OVER ({partition_sql()} {order_sql()})"
            expr = f"{ref(m_idx)} - {win}"
        elif atype == "yoy":
            lag = self._YOY_LAG_BY_GRAIN.get(self._current_grain(plan), 1)
            win = f"LAG({ref(m_idx)}, {lag}) OVER ({partition_sql()} {order_sql()})"
            expr = f"{ref(m_idx)} - {win}"
        elif atype == "pct_change":
            win = f"LAG({ref(m_idx)}, 1) OVER ({partition_sql()} {order_sql()})"
            expr = f"({ref(m_idx)} - {win}) / NULLIF({win}, 0)"
        else:  # rank:排名 = 度量降序的 RANK
            expr = f"RANK() OVER ({partition_sql()} ORDER BY {ref(m_idx)} DESC)"

        display = self._ANALYSIS_DISPLAY[atype]
        # 外层展示名去重(与内层列名冲突时加后缀)
        used = set(proj_display)
        outer_display = display
        suffix = 2
        while outer_display in used:
            outer_display = f"{display}_{suffix}"
            suffix += 1

        from sqlglot import exp, parse_one

        try:
            inner = parse_one(inner_sql, read=self._dialect)
        except Exception as e:
            return CompileMiss("analysis_invalid", f"inner unparseable: {e}")
        if not isinstance(inner, exp.Select):
            return CompileMiss("analysis_invalid", "inner not a SELECT")
        new_projs = []
        for j, p in enumerate(inner.expressions):
            if isinstance(p, exp.Alias):
                new_projs.append(exp.alias_(p.this, f"_c{j}"))
            else:
                new_projs.append(exp.alias_(p, f"_c{j}"))
        inner.set("expressions", new_projs)

        inner_text = inner.sql(dialect=self._dialect)
        outer_cols = [
            f"{ref(j)} AS {pd}" for j, pd in enumerate(proj_display)
        ]
        outer_cols.append(f"({expr}) AS {outer_display}")
        outer = (
            "SELECT " + ", ".join(outer_cols)
            + "\nFROM (\n" + inner_text + "\n) AS _t"
        )

        # 外层排序:plan.ordering 里可引用 metric/字段/分析列;缺省给出
        # 确定性排序(rank → 排名升序;share → 占比降序;时间类 → 时间升序)。
        def outer_order() -> str | None:
            if order_parts:
                mapped: list[str] = []
                for column, direction in (parse_ordering(plan.get("ordering")) or []):
                    if column.lower() in self._ANALYSIS_DISPLAY.values():
                        mapped.append(f"{column} {direction.upper()}")
                        continue
                    metric_o = self._metric_by_name(column)
                    if metric_o is not None and metric_o.name == proj_ref[m_idx].name:
                        mapped.append(f"{ref(m_idx)} {direction.upper()}")
                        continue
                    resolved_o = self._resolve_field(column, matched_set)
                    if resolved_o is None:
                        continue
                    idx = next(
                        (i for i, r in enumerate(proj_ref)
                         if isinstance(r, tuple) and len(r) == 2 and r == resolved_o),
                        None,
                    )
                    if idx is not None:
                        mapped.append(f"{ref(idx)} {direction.upper()}")
                if mapped:
                    return "ORDER BY " + ", ".join(mapped)
                return None
            if atype == "rank":
                return f"ORDER BY {outer_display} ASC"
            if atype == "share":
                return f"ORDER BY {outer_display} DESC"
            if order_idx is not None:
                return f"ORDER BY {ref(order_idx)} ASC"
            return None

        ob = outer_order()
        if ob:
            outer += "\n" + ob
        if limit is not None:
            if ob is None:
                return CompileMiss("limit_without_order", str(limit))
            outer += f"\nLIMIT {int(limit)}"
        return outer

    @staticmethod
    def _current_grain(plan: dict[str, Any]) -> str:
        tg = plan.get("time_grain")
        if isinstance(tg, dict):
            return str(tg.get("grain") or "").strip().lower()
        return ""


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
