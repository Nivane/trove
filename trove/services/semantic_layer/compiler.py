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
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from sqlglot import exp, parse_one

from trove.core.logging import get_logger
from trove.services.semantic_layer.contract import (
    PlanContract,
    PlanSignature,
    WhereCond,
)
from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticMetric,
    SemanticModel,
)
from trove.services.semantic_layer import rls
from trove.services.semantic_layer.plan import GRAINS, PlanQuery, parse_ordering
from trove.services.semantic_layer.timegrain import (
    date_trunc,
    spine_fill_expr,
    time_spine_periods,
)

logger = get_logger(__name__)


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
    fan_out: str = ""  # 空 | "dedup" | "bridge:<dataset>"(M:N 显式豁免)


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
    dedup_edges: list["JoinEdge"] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.clauses


class JoinResolver:
    """Resolve authoritative join clauses over the declared join graph."""

    def __init__(self, model: SemanticModel | None = None):
        self._model = model
        # 数据集按名索引(唯一键推断基数用)。
        self._ds_by_name: dict[str, SemanticDataset] = {}
        # 声明基数缺失但 to 侧构成唯一键的边(关系级推断,按列对展开)。
        self._unique_backed: set[tuple[str, str, str, str]] = set()
        if model is not None:
            # getattr 兜底:schema_linking 等消费方可能传入鸭子类型模型
            # (有 datasets/metrics 但无 relationships 的测试替身/代理)。
            for d in getattr(model, "datasets", None) or []:
                self._ds_by_name[d.name] = d
            for r in getattr(model, "relationships", None) or []:
                if self._relationship_to_is_unique(r):
                    for fc, tc in zip(r.from_columns or [], r.to_columns or []):
                        self._unique_backed.add((
                            str(r.from_).lower(), str(fc).lower(),
                            str(r.to).lower(), str(tc).lower()))

    def _relationship_to_is_unique(self, r: Any) -> bool:
        """关系 to 侧列是否构成已声明唯一键(primary_key / unique_keys)。

        唯一键推断只作为**声明基数缺失时**的安全放行依据:to_columns 精确
        (有序)等于 primary_key 或任一 unique_keys → 一行 to 至多对应一行
        from,many→one 数学上确定,无需建模师补 ``cardinality`` 字段。
        """
        if not getattr(r, "to_columns", None):
            return False
        ds = self._ds_by_name.get(r.to)
        if ds is None:
            return False
        to_cols = [str(c) for c in r.to_columns]
        if [str(c) for c in (ds.primary_key or [])] == to_cols:
            return True
        return any(
            [str(c) for c in uk] == to_cols for uk in ds.unique_keys or []
        )

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
                    fan_out=(r.fan_out or "").strip().lower(),
                ))
        return edges

    # ── Cardinality guard ──────────────────────────────────

    def _classify_edges(
        self, edges: list[JoinEdge],
    ) -> tuple[bool, bool, list[JoinEdge]]:
        """逐边判基数:→ (fan_out, unknown_cardinality, dedup_edges)。

        fan_out / unknown_cardinality 是边的**物理性质**(行倍增;many→one 无
        从判定),与「谁选的这条路径」无关:BFS 树和 ``plan.joins`` 显式树都
        必须过这一关。只在 BFS 分支做检查曾让 LLM 自己吐出 joins 即可绕过
        行倍增守卫,静默产出错数 SQL。
        """
        fan_out = False
        unknown_card = False
        dedup_edges: list[JoinEdge] = []
        for edge in edges:
            if _is_many_to_many(edge.cardinality):
                # P5.2:多对多经此边联(在联路径上)→ 除非建模师显式豁免:
                # dedup = 编译期把 from 侧包 SELECT DISTINCT * 子查询消除行倍增;
                # bridge 保留未实现 → 仍拒。否则编译期拒 fan-out。
                if edge.fan_out == "dedup":
                    dedup_edges.append(edge)
                else:
                    fan_out = True  # 含 bridge:<dataset>(豁免未实现 → 保守拒)
            elif not (edge.cardinality or "").strip():
                # 边在联路径上但基数未声明 → many→one 无从判定,保守 MISS
                # (宁可交 LLM,不赌安全)——除非 to 侧构成声明唯一键:
                # unique_keys/primary_key 推断成立时 many→one 确定,放行。
                key = (
                    edge.from_.lower(), edge.from_column.lower(),
                    edge.to.lower(), edge.to_column.lower(),
                )
                if key not in self._unique_backed:
                    unknown_card = True
        return fan_out, unknown_card, dedup_edges

    def guard_edge_cardinality(self, edges: list[JoinEdge]) -> str | None:
        """对一批**已选定**的联表边跑基数守卫 → 首个拒绝原因,放行则 None。

        与 ``resolve()`` 内部同源,供 ``plan.joins`` 显式路径复用:显式路径只
        替代「路径怎么选」,不豁免边本身的基数约束。
        """
        fan_out, unknown_card, _ = self._classify_edges(edges)
        if fan_out:
            return "fan_out"
        if unknown_card:
            return "unknown_cardinality"
        return None

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
        for child, (parent, edge) in parent_edge.items():
            if not leads_to_needed(child):
                continue
            tree.append(edge)
        fan_out, unknown_card, dedup_edges = self._classify_edges(tree)

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
            ambiguous=ambiguous, dedup_edges=dedup_edges)

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


def _join_clauses(joins_value: Any) -> list[str] | None:
    """joins 文本 → 子句列表(逗号/分号/AND 分隔);非 str/list → None。"""
    if isinstance(joins_value, str):
        parts = [joins_value]
    elif isinstance(joins_value, list):
        parts = [str(x) for x in joins_value]
    else:
        return None
    text = " AND ".join(p for p in parts if str(p).strip())
    if text.strip().lower() in _PLACEHOLDER_JOINS:
        return None
    return [
        c for c in re.split(r"\s*,\s*|\s*;\s*|\s+and\s+", text, flags=re.I)
        if c.strip()
    ]


def _table_pairs_from_joins(joins_value: Any) -> list[tuple[str, str]] | None:
    """joins 文本 → 无序表对列表(A3a 表对修复的输入);任一子句不可解析 → None。

    只取**表**这一层:plan.joins 的列名可能写错(0477:``client.client_id =
    account.account_id``,真实边是 client—disp—account),但表对通常是计划真正
    想表达的关系骨架。解析不出干净的两端限定列对 → None(调用方维持硬 MISS)。
    """
    clauses = _join_clauses(joins_value)
    if clauses is None:
        return None
    pairs: list[tuple[str, str]] = []
    for clause in clauses:
        try:
            tree = parse_one(clause)
        except Exception:
            return None
        eqs = list(tree.find_all(exp.EQ))
        if len(eqs) != 1:
            return None
        left, right = eqs[0].left, eqs[0].right
        if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
            return None
        lt = (left.table or "").strip().lower()
        rt = (right.table or "").strip().lower()
        if not lt or not rt:
            return None
        pairs.append((lt, rt))
    return pairs or None


def _relationship_edges(r: Any) -> list[JoinEdge]:
    """声明关系 → 逐列对的 JoinEdge(方向沿用声明:from_=多端 FK 持有者)。"""
    out: list[JoinEdge] = []
    for fc, tc in zip(getattr(r, "from_columns", None) or [],
                      getattr(r, "to_columns", None) or []):
        out.append(JoinEdge(
            r.from_, r.to, str(fc), str(tc), declared=True,
            cardinality=(r.cardinality or "").upper(),
            fan_out=(r.fan_out or "").strip().lower(),
        ))
    return out


def _declared_edges_between(model: "SemanticModel | None", from_: str, to: str) -> list[JoinEdge]:
    """声明图里恰好连接 (from_, to) 这一对表的关系的边(方向无关,大小写不敏感)。"""
    if model is None:
        return []
    want = {from_.lower(), to.lower()}
    out: list[JoinEdge] = []
    for r in model.relationships:
        if {str(r.from_).lower(), str(r.to).lower()} != want:
            continue
        out.extend(_relationship_edges(r))
    return out


def _declared_rels_between(model: "SemanticModel | None", a: str, b: str) -> list[Any]:
    """声明图里连接 (a, b) 这一对表的**关系**列表(判「一对多关系」用)。"""
    if model is None:
        return []
    want = {a.lower(), b.lower()}
    return [
        r for r in model.relationships
        if {str(r.from_).lower(), str(r.to).lower()} == want
        and r.from_columns and r.to_columns
    ]


def _edge_text(edge: JoinEdge) -> str:
    """JoinEdge → 可回喂 ``_explicit_join_edges`` 的 ON 文本。"""
    return f"{edge.from_}.{edge.from_column} = {edge.to}.{edge.to_column}"


def _edge_pair_key(edge: JoinEdge) -> frozenset[str]:
    """边 → 无序列对键(小写 ``表.列``),方向/大小写无关的查重口径。"""
    return frozenset({
        f"{str(edge.from_).lower()}.{str(edge.from_column).lower()}",
        f"{str(edge.to).lower()}.{str(edge.to_column).lower()}",
    })


def _join_pair_keys(joins_value: Any) -> set[frozenset[str]]:
    """joins 文本 → 子句级无序列对键集合(不可解析/非列对列的子句跳过)。

    与 ``_explicit_join_edges`` 同一解析口径(逐子句恰一条 EQ、两侧须为
    限定列),但**不**要求命中声明关系、失败也不作废 —— 它只服务于「这条边
    是不是已经写过了」的查重,不承担权威性判定。
    """
    clauses = _join_clauses(joins_value)
    if not clauses:
        return set()
    keys: set[frozenset[str]] = set()
    for clause in clauses:
        try:
            tree = parse_one(clause)
        except Exception:
            continue
        eqs = list(tree.find_all(exp.EQ))
        if len(eqs) != 1:
            continue
        left, right = eqs[0].left, eqs[0].right
        if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
            continue
        lt = (left.table or "").strip().lower()
        rt = (right.table or "").strip().lower()
        if not (lt and rt):
            continue
        keys.add(frozenset({
            f"{lt}.{(left.name or '').strip().lower()}",
            f"{rt}.{(right.name or '').strip().lower()}",
        }))
    return keys


def _declared_links(model: "SemanticModel | None") -> dict[str, list[tuple[str, list[JoinEdge]]]]:
    """声明关系图(无向)邻接表:表 → [(对端表, 连接边列表)]。"""
    links: dict[str, list[tuple[str, list[JoinEdge]]]] = {}
    if model is None:
        return links
    for r in model.relationships:
        edges = _relationship_edges(r)
        if not edges:
            continue
        links.setdefault(str(r.from_), []).append((str(r.to), edges))
        links.setdefault(str(r.to), []).append((str(r.from_), edges))
    return links


def _declared_paths(
    links: dict[str, list[tuple[str, list[JoinEdge]]]],
    start: str,
    goal: str,
    allow_intermediate: Any,
) -> list[list[JoinEdge]]:
    """start→goal 的简单路径(**最多收集 2 条**,>1 即视为不唯一)。

    中间点(A3a 表对修复的路由桥)须过 ``allow_intermediate``;端点不设限 ——
    纯维度叶(如 district)可以作路径终点,但不能被穿行。多条平行关系
    (同表对多关系)各自成路 → 自然计成 >1 → 调用方判「不唯一」。
    """
    out: list[list[JoinEdge]] = []

    def dfs(node: str, visited: set[str], acc: list[JoinEdge]) -> None:
        if len(out) >= 2:
            return
        if node == goal and acc:
            out.append(list(acc))
            return
        for nxt, edges in links.get(node, ()):
            if nxt in visited:
                continue
            if nxt != goal and not allow_intermediate(nxt):
                continue
            dfs(nxt, visited | {nxt}, acc + edges)

    dfs(start, {start}, [])
    return out


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
    clauses = _join_clauses(joins_value)
    if clauses is None:
        return None, False

    from sqlglot import exp, parse_one

    # joins 是逗号/分号/AND 分隔的多个 ``lhs = rhs`` 子句(query_sketch 输出):
    # 整体 parse 会被逗号卡死,逐子句解析后收集 EQ。分号同理必须切(query_sketch
    # 实测会用 ``;`` 分隔,整串进 parse_one 会得到一个 Block 含两条 EQ,撞上下面
    # 的「每子句恰一条 EQ」判定 → 已声明路径被误判为不可解析)。
    parsed = []
    for clause in clauses:
        try:
            parsed.append(parse_one(clause))
        except Exception:
            return None, True

    rel_edges: list[tuple[str, str, str, str, str, str]] = []
    for r in model.relationships:
        for fc, tc in zip(r.from_columns or [], r.to_columns or []):
            rel_edges.append(
                (r.from_.lower(), r.to.lower(), fc.lower(), tc.lower(),
                 (r.cardinality or "").upper(), (r.fan_out or "").strip().lower()))
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
        for f, t, fc, tc, card, fan in rel_edges:
            if (f == lt and t == rt and fc == lc and tc == rc) or (
                f == rt and t == lt and fc == rc and tc == lc
            ):
                matched = (f, t, fc, tc, card, fan)
                break
        if matched is None:
            return None, True
        out.append(JoinEdge(
            matched[0], matched[1], matched[2], matched[3],
            declared=True, cardinality=matched[4], fan_out=matched[5],
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


def _edge_key(edge: JoinEdge) -> tuple:
    """边的无序规范键(同一条边的不同声明方向/重复声明只算一次)。"""
    return tuple(sorted([
        (edge.from_.lower(), str(edge.from_column).lower()),
        (edge.to.lower(), str(edge.to_column).lower()),
    ]))


def _edge_components(edges: list[JoinEdge]) -> int:
    """显式边集的连通分量数(顶点并查集,大小写不敏感)。"""
    parent: dict[str, str] = {}

    def _find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e in edges:
        ra, rb = _find(e.from_.lower()), _find(e.to.lower())
        if ra != rb:
            parent[ra] = rb
    return len({_find(v) for v in parent})


def _unique_edges(edges: list[JoinEdge]) -> list[JoinEdge]:
    out: list[JoinEdge] = []
    seen: set[tuple] = set()
    for e in edges:
        k = _edge_key(e)
        if k in seen:
            continue
        seen.add(k)
        out.append(e)
    return out


def _clause_refs(clause: str) -> tuple[frozenset[str], tuple[str, str]] | None:
    """单个 ON 子句 → (无序列对键, (左表, 右表));不可解析 → None。"""
    try:
        tree = parse_one(clause)
    except Exception:
        return None
    eqs = list(tree.find_all(exp.EQ))
    if len(eqs) != 1:
        return None
    left, right = eqs[0].left, eqs[0].right
    if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
        return None
    lt = (left.table or "").strip().lower()
    rt = (right.table or "").strip().lower()
    if not (lt and rt):
        return None
    return (
        frozenset({
            f"{lt}.{(left.name or '').strip().lower()}",
            f"{rt}.{(right.name or '').strip().lower()}",
        }),
        (lt, rt),
    )


def _route_capable_tables(model: "SemanticModel | None") -> set[str]:
    """可作声明图路由中间点的表(from_ 端 ∪ M:N 的 to 端)。

    纯维度叶(district:只是 1:N 的 to 端)只能当路径**终点**,不能被穿行
    ——共享维度桥(account—district—client)因此天然出局,所有权链
    (account—disp—client)胜出。与 ``_repair_explicit_joins`` 同口径。
    """
    if model is None:
        return set()
    rels = list(model.relationships)
    capable = {str(r.from_) for r in rels}
    capable |= {str(r.to) for r in rels if _is_many_to_many(r.cardinality)}
    return capable


def declared_join_edge_text(model: "SemanticModel | None", a: str, b: str) -> str | None:
    """两表间**唯一**声明关系的 ON 文本;无关系/多关系 → None。"""
    rels = _declared_rels_between(model, a, b)
    if len(rels) != 1:
        return None
    edges = _relationship_edges(rels[0])
    if not edges:
        return None
    return " AND ".join(_edge_text(e) for e in edges)


def repair_plan_joins(
    plan: dict[str, Any] | None, model: "SemanticModel | None",
) -> dict[str, Any] | None:
    """计划 joins 的声明图合规修复(A5a 表对修复前移到计划层);无改动 → None。

    动机(0493 实测):计划写了一条**未声明**的连接
    (``account.district_id = client.district_id``,共享维度桥),编译器的显式
    joins 通道能在编译内部把它还原成声明路径(account—disp—client);但编译
    若因**其它组件**软 MISS,plan 文本会带着这条坏连接原样交给 gen_sql——
    生成侧照抄,错误被固化。计划层先修一次,编译与生成两侧看到的都是合规
    joins。规则与 ``_repair_explicit_joins`` 同源,逐子句:

      - 子句列对命中声明边 → 原样保留(计划的具体写法是权威,只修不改写);
      - 表对恰有一条声明关系 → 用该关系的边重建子句(列名纠正,0477 型);
      - 表对无直接关系 → 声明图上找**唯一**路径(中间点限 route-capable),
        唯一才替换;多条平行关系(同表对多关系)同样判不唯一;
      - 其余(无路径/多路径/子句不可解析)→ None,整份计划不动(交既有
        硬 MISS + 有界重规划,不猜)。

    修复只增不减:新引入的中间表追加进 ``plan.tables``(生成侧与编译侧的
    FROM 表集都从它来,漏表会让产物引用连接树外的列),不删任何表;
    ``plan_field`` 标记本次改写(最后触发的纠正器胜出,与既有约定一致)。
    """
    if not isinstance(plan, dict) or model is None:
        return None
    clauses = _join_clauses(plan.get("joins"))
    if not clauses:
        return None
    declared: set[frozenset[str]] = set()
    for r in model.relationships:
        for e in _relationship_edges(r):
            declared.add(_edge_pair_key(e))
    canon = {d.name.lower(): d.name for d in model.datasets}
    links = _declared_links(model)
    capable = _route_capable_tables(model)
    kept: list[str] = []
    added: list[JoinEdge] = []
    changed = False
    for clause in clauses:
        refs = _clause_refs(clause)
        if refs is not None and refs[0] in declared:
            kept.append(clause)
            continue
        if refs is None:
            return None
        a, b = refs[1]
        a_c, b_c = canon.get(a), canon.get(b)
        if a_c is None or b_c is None or a == b:
            return None
        rels = _declared_rels_between(model, a_c, b_c)
        if len(rels) > 1:
            return None
        if len(rels) == 1:
            added.extend(_relationship_edges(rels[0]))
            changed = True
            continue
        paths = _declared_paths(links, a_c, b_c, lambda t: t in capable)
        if len(paths) != 1:
            return None
        added.extend(paths[0])
        changed = True
    if not changed:
        return None
    edges = _unique_edges(added)
    if not edges:
        return None
    fixed = dict(plan)
    fixed["joins"] = " AND ".join([*kept, *[_edge_text(e) for e in edges]])
    tables = [str(t) for t in (plan.get("tables") or [])]
    have = {t.lower() for t in tables}
    for e in edges:
        for t in (e.from_, e.to):
            if t.lower() not in have:
                tables.append(t)
                have.add(t.lower())
    fixed["tables"] = tables
    fixed["plan_field"] = "repair_plan_joins"
    return fixed


def _cond_keys_of(
    text: str,
) -> set[tuple[frozenset[tuple[str, str]], str, tuple[str, ...]]]:
    """谓词文本 → 归一化条件键集合(叶子逐条、字面量小写)。

    与 ``_skeleton_where`` 同口径(共用 ``_conds_of``):``A AND B`` 拆成两条叶子
    键、``or`` 整体成一条键 —— A3b 的"条件所有权"对账正是拿它比对计划条件与
    聚合候选内部谓词,口径分叉会让同一谓词在两侧不等。
    """
    return {
        (frozenset(cols), op, tuple(str(v).lower() for v in vals))
        for cols, op, vals in _predicate_conds(text)
    }


def _norm_expr_text(text: Any) -> str:
    """表达式文本归一:小写 + 空白折叠(仅用于"同一表达式"的对账,不改写)。"""
    return " ".join(str(text or "").lower().split())


def _literal_items(value: Any) -> list[Any]:
    """条件值 → 字面量列表(``in`` 的 list/tuple/paren 串;标量 → 单元素)。"""
    if isinstance(value, (list, tuple)):
        return [v for v in value if str(v).strip()]
    if isinstance(value, str):
        s = value.strip()
        if len(s) >= 2 and s[0] == "(" and s[-1] == ")":
            return [p.strip() for p in s[1:-1].split(",") if p.strip()]
        return [s] if s else []
    return [value]


def _safe_display(text: str, fallback: str = "expr") -> str:
    """展示名安全化:非标识符字符折叠为 ``_``。

    投影展示名会被窗口包装(A2 分析)当作**列别名**拼进外层 SELECT,而
    A② 表达式列的尾部(``A13 - district.A12) / district.A12) * 100``)含
    空格与括号——未清洗会拼出非法 SQL 别名。
    """
    cleaned = re.sub(r"[^0-9A-Za-z_]+", "_", str(text or "")).strip("_")
    if not cleaned:
        return fallback
    if cleaned[0].isdigit():
        cleaned = f"c_{cleaned}"
    return cleaned[:48]


def _extreme_rank(ex: dict[str, Any]) -> int:
    """``extreme.rank`` → 正整数序数(1 = 最大/最小本身);缺省/非法/非正 → 1。"""
    try:
        rank = int(str(ex.get("rank") or 1).strip())
    except (TypeError, ValueError):
        return 1
    return rank if rank >= 1 else 1


# ── 标量表达式列通道(闭语法)───────────────────────────────────
#
# 计划把算式/带函数的标量列写进 answer_columns(0482 的
# ``((district.A13 - district.A12) / district.A12) * 100``)时,旧投影循环
# 对任何含 ``(`` 的无签名列一律静默跳过——骨架丢列,生成侧无从知道它被丢。
# 这里给出一条**闭语法**通道:sqlglot 严格解析 → 节点白名单 → 每列经
# ``_resolve_field`` 落到声明字段 → 用自己的渲染器**重建**文本。
#
# 绝不回放 LLM 原文(回放会把注入/坏引用带进权威 SQL);白名单刻意收窄——
# 收窄的代价是软 MISS,放宽的代价是权威错 SQL。聚合(AggFunc)、子查询、
# 别名、未知函数、解析不到声明字段的列一律拒绝;CURRENT_TIMESTAMP /
# CURRENT_DATE 按标准渲染成带括号的调用形式(0498 的年龄派生列)。

#: 白名单函数:节点类型 → (SQL 名, 最少实参, 最多实参)。
_SCALAR_FUNCS: dict[type, tuple[str, int, int]] = {
    exp.Year: ("YEAR", 1, 1),
    exp.Month: ("MONTH", 1, 1),
    exp.Day: ("DAY", 1, 1),
    exp.Round: ("ROUND", 1, 2),
    exp.Abs: ("ABS", 1, 1),
    exp.Coalesce: ("COALESCE", 1, 99),
    exp.Nullif: ("NULLIF", 2, 2),
}
_SCALAR_BINOPS: dict[type, str] = {
    exp.Add: "+", exp.Sub: "-", exp.Mul: "*", exp.Div: "/", exp.Mod: "%",
}
#: CAST 目标类型白名单(数值/字符串/时间);其余类型(如 JSON/ARRAY)不认。
_SCALAR_CAST_TYPES = frozenset({"DOUBLE", "DECIMAL", "SIGNED", "CHAR", "DATETIME"})
_SCALAR_MAX_DEPTH = 32


def _scalar_func_args(node: Any) -> list[Any]:
    """函数实参列表(按节点形状显式取槽:Anonymous 的 ``this`` 是函数名而非
    实参;ROUND 的精度在 ``decimals``、NULLIF 的第二参在 ``expression``)。"""
    if isinstance(node, exp.Anonymous):
        return [a for a in node.expressions if a is not None]
    if isinstance(node, exp.Round):
        args: list[Any] = [node.this, node.args.get("decimals")]
    elif isinstance(node, exp.Coalesce):
        args = [node.this, *node.expressions]
    elif isinstance(node, exp.Nullif):
        args = [node.this, node.args.get("expression")]
    else:
        args = [node.this]
    return [a for a in args if a is not None]


def _unwrap_paren(node: Any) -> Any:
    """剥掉多余的括号层(sqlglot 只在语法需要时保留 Paren,但计划文本
    里的 ``((diff/A12) * 100)`` 会把 Div 包进 Paren——形态识别必须透视)。"""
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _ratio_operands(node: Any) -> tuple[Any, Any, Any] | None:
    """比率形态识别:``(N / D) * K`` 或 ``(N * K) / D``(K 为数字字面量)。

    返回 (N 节点, D 节点, K 字面量节点);非该形态 → None。两侧都试
    (``100 * x / y`` 与 ``x * 100 / y`` 同形),``a*b/c`` 的左结合天然落在
    ``Div(Mul(N,K), D)`` 上,一并认。
    """
    if isinstance(node, exp.Mul):
        left, right = node.this, node.expression
        for k_node, other in ((left, right), (right, left)):
            if (
                isinstance(k_node, exp.Literal) and k_node.is_number
                and isinstance(_unwrap_paren(other), exp.Div)
            ):
                div = _unwrap_paren(other)
                return div.this, div.expression, k_node
        return None
    if isinstance(node, exp.Div) and isinstance(_unwrap_paren(node.this), exp.Mul):
        mul = _unwrap_paren(node.this)
        for k_node, n_node in ((mul.this, mul.expression), (mul.expression, mul.this)):
            if isinstance(k_node, exp.Literal) and k_node.is_number:
                return n_node, node.expression, k_node
    return None


def _scalar_is_double_cast(node: Any) -> bool:
    """节点是否已是 ``CAST(… AS DOUBLE)``(化归时不重复包 CAST)。"""
    if not isinstance(node, exp.Cast):
        return False
    to = node.args.get("to")
    base = str(getattr(getattr(to, "this", None), "value", "") or "").upper()
    return base == "DOUBLE"


def _scalar_ratio_canonical(
    node: Any, compiler: Any, tables: set[str], depth: int,
) -> str | None:
    """比率化归:``(N / D) * K`` / ``(N * K) / D`` → ``CAST(N AS DOUBLE) * K / D``。

    与 KB share 模板(deterministic_gen 的 ``CAST(… AS DOUBLE) * 100 / …``)
    同一渲染规范:先乘后除只引入一次除法舍入;先除后乘在中间量上先舍入一次
    (0482 实测 23/45 行末位偏差,如 ``114.99999999999999`` 对 ``115.0``)。
    零容差对照下同一数学式必须落在同一浮点路径上——这是渲染规范,不是语义
    改写(乘除交换律在实数域成立)。N 已是 DOUBLE CAST 则不重复包(计划自带
    的 CAST 原样保留,同 A2 的"绝不自动补"原则的例外:这里是**规范形态**
    的一部分,只对"除后乘"这一种形状).
    """
    if depth > _SCALAR_MAX_DEPTH:
        return None
    shape = _ratio_operands(node)
    if shape is None:
        return None
    n_node, d_node, k_node = shape
    n_text = _scalar_render(n_node, compiler, tables, depth + 1)
    d_text = _scalar_render(d_node, compiler, tables, depth + 1)
    k_text = _scalar_render(k_node, compiler, tables, depth + 1)
    if n_text is None or d_text is None or k_text is None:
        return None
    n_expr = n_text if _scalar_is_double_cast(n_node) else f"CAST({n_text} AS DOUBLE)"
    return f"({n_expr} * {k_text} / {d_text})"


def _scalar_render(node: Any, compiler: Any, tables: set[str], depth: int = 0) -> str | None:
    """闭语法重建:白名单节点 → 文本;任何越界节点 → None(整列弃用)。

    列一律渲染成 ``dataset.field.expression``(强制表限定,不复用
    ``_qualified``:后者的"已限定就跳过"判定会被计划文本里的错限定词骗过);
    二元运算统一加括号(重建文本与计划文本的括号数可能不同,语义等价)。
    """
    if depth > _SCALAR_MAX_DEPTH:
        return None
    if isinstance(node, exp.Column):
        if node.catalog or node.db or not node.name:
            return None  # 三段以上限定 / 无名列:超出声明模型的两级命名
        ref = f"{node.table}.{node.name}" if node.table else str(node.name)
        resolved = compiler._resolve_field(ref, compiler._matched_set)
        if resolved is None:
            return None  # 解析不到声明字段(歧义/未声明)→ 弃用整列
        tables.add(resolved[0])
        return f"{resolved[0]}.{resolved[1].expression}"
    if isinstance(node, exp.Paren):
        return _scalar_render(node.this, compiler, tables, depth + 1)
    if isinstance(node, exp.Neg):
        inner = _scalar_render(node.this, compiler, tables, depth + 1)
        return f"-{inner}" if inner is not None else None
    if isinstance(node, exp.Literal):
        return str(node.this) if node.is_number else None
    if isinstance(node, exp.CurrentTimestamp):
        return "CURRENT_TIMESTAMP()"
    if isinstance(node, exp.CurrentDate):
        return "CURRENT_DATE()"
    ratio = _scalar_ratio_canonical(node, compiler, tables, depth)
    if ratio is not None:
        return ratio
    op = _SCALAR_BINOPS.get(type(node))
    if op is not None:
        left = _scalar_render(node.this, compiler, tables, depth + 1)
        right = _scalar_render(node.expression, compiler, tables, depth + 1)
        if left is None or right is None:
            return None
        return f"({left} {op} {right})"
    if isinstance(node, exp.Cast):
        to = node.args.get("to")
        # DType 是 str 枚举:``str(DType.DOUBLE)`` 给 "DType.DOUBLE",类型名
        # 取 ``.value``(参数化类型如 DECIMAL(10,2) 的基础类型同样落在 .value)。
        base = str(getattr(getattr(to, "this", None), "value", "") or "").upper()
        if base not in _SCALAR_CAST_TYPES:
            return None
        inner = _scalar_render(node.this, compiler, tables, depth + 1)
        if inner is None:
            return None
        # 忠实保留计划里已有的 CAST(类型与精度原样;绝不自动补 CAST)
        return f"CAST({inner} AS {to.sql()})"
    spec = _SCALAR_FUNCS.get(type(node))
    if spec is not None and not isinstance(node, exp.AggFunc):
        name, lo, hi = spec
        args = _scalar_func_args(node)
        if not (lo <= len(args) <= hi):
            return None
        rendered = [_scalar_render(a, compiler, tables, depth + 1) for a in args]
        if any(r is None for r in rendered):
            return None
        return f"{name}({', '.join(rendered)})"
    return None


def _scalar_expr_analyze(
    expr_text: Any, compiler: Any,
) -> tuple[str, frozenset[str]] | None:
    """标量表达式 → (重建文本, 引用的数据集集);越出闭语法 → None。

    至少引用一个声明字段才认(纯字面量列不是"列");顶层形态(Select/
    Subquery/Alias/Union/Star)一律拒绝。
    """
    text = str(expr_text or "").strip()
    if not text:
        return None
    try:
        tree = parse_one(text)
    except Exception:
        return None
    if tree is None or isinstance(
        tree, (exp.Alias, exp.Subquery, exp.Select, exp.Query, exp.Union, exp.Star)
    ):
        return None
    tables: set[str] = set()
    rendered = _scalar_render(tree, compiler, tables)
    if rendered is None or not tables:
        return None
    return rendered, frozenset(tables)


def scalar_expr_ref(expr_text: Any, compiler: Any, dialect: str = "sqlite") -> str | None:
    """标量表达式列 → **重建后**的 SQL 文本;越出闭语法 → None(A② 入口)。

    ``dialect`` 仅为调用侧签名兼容而保留:重建走本模块自己的渲染器
    (列用声明字段表达式、函数名大写、字面量原样),不依赖方言解析/生成。
    """
    analyzed = _scalar_expr_analyze(expr_text, compiler)
    return analyzed[0] if analyzed is not None else None


def _looks_like_formula(text: Any) -> bool:
    """形态判定:该文本看起来**想表达一个算式/函数列**(而非占位符)。

    只在标量通道解析失败后决定"要不要为这一列记软 MISS":``number(*)`` /
    ``min(*)`` 这类无实参占位符保持静默(计划噪声,同旧行为),而
    ``foo(district.A2)`` / ``a + b`` / ``CAST(...)`` / ``(SELECT ...)`` 是真的
    丢了一个投影,必须记 ``unresolved_answer_column`` 让生成侧看见缺口。
    """
    s = str(text or "").strip()
    if not s:
        return False
    try:
        tree = parse_one(s)
    except Exception:
        return bool(re.search(r"[+\-*/%]", s))
    for node in tree.walk():
        if isinstance(
            node, (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.Neg, exp.Cast)
        ):
            return True
        if isinstance(node, (exp.Subquery, exp.Select, exp.Query, exp.Union)):
            # 子查询列:计划真想投影一个值(不是占位符)——通道外必须记账
            return True
        if isinstance(node, exp.Func):
            args = _scalar_func_args(node)
            if not args or any(not isinstance(a, exp.Star) for a in args):
                return True
    return False


def agg_owned_cond_keys(
    exprs: Iterable[str],
) -> set[tuple[frozenset[tuple[str, str]], str, tuple[str, ...]]]:
    """聚合表达式集 → 其**内部谓词**的归一化条件键集合(条件所有权)。

    这些谓词是聚合定义的一部分(占比/条件聚合的分子条件),不是行级过滤:
    计划里往往同一谓词还有一条孪生 conditions 条目,冻结进骨架 WHERE 后与
    生成侧"分子进聚合"的重写冲突(0476/0495 型)。键口径与 ``_skeleton_where``
    逐字一致(共用 ``_conds_of``),**只用于所有权判定**(命中即跳过该行级
    条件),放宽安全——键比对不会把条件写进 SQL。

    候选内部的 ``AND`` 复合谓词拆成叶子逐条进集合(``_cond_keys_of``):
    计划侧同一谓词可能写成一条孪生条件、也可能拆成多条,两侧都按叶子对账。
    """
    keys: set[tuple[frozenset[tuple[str, str]], str, tuple[str, ...]]] = set()
    for text in exprs:
        sig = _agg_signature(str(text))
        if sig is None:
            continue
        for _fn, _cols, _dist, conds in sig:
            for cond_text in conds:
                keys |= _cond_keys_of(cond_text)
    return keys


#: 值路由(A4b)不认数字字面量:枚举码表里也可能出现纯数字键(如 A7 的
#: '0'..'20'),把数值过滤按字面量路由到别的列是误伤,不是值词表查询。
_NUMERIC_LITERAL_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")


def _index_value(
    idx: dict[str, list[tuple[str, str]]], raw: Any, ds_name: str, field_name: str,
) -> None:
    """值索引追加一条(键:去引号小写;同 (dataset, field) 去重)。

    纯数字串值**不入索引**(收口在构建期,覆盖所有 KB):文本列里存数字串
    是常见的存储习惯(实测 financial 的 ``district.A5``/``A6`` 是县/市数量
    的文本形式,'0'..'101'),它既不具名、又与 id 类取值天然二义 ——
    ``district.district_id = '5'`` 若被路由到"county 数 = 5"就是静默错数。
    值路由只对**具名**值(textual)成立。
    """
    key = str(raw or "").strip().strip("'\"").lower()
    if not key or _NUMERIC_LITERAL_RE.match(key):
        return
    entry = (ds_name, field_name)
    bucket = idx.setdefault(key, [])
    if entry not in bucket:
        bucket.append(entry)

#: having 折叠(A1d)允许的聚合函数:维度表上按实体的 AVG/MIN/MAX 即列值本身,
#: 可折成行级 WHERE;SUM/COUNT 是**跨行聚集**,折叠语义不同,一律不折。
_HAVING_FOLD_FUNCS = frozenset({"avg", "min", "max"})


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
    """编译产物:权威 SQL + 权威交接契约 + 来处。

    散文提示块不再单独存字段 —— 它是 ``render_contract(contract)`` 的纯渲染,
    存成两个字段只会给两者留下漂移的空间(Phase A 要消除的正是这种"同一意图
    多份表示")。

    ``source_plan`` 相反:它不是同一意图的第二份表示,而是**产物的身份**
    (A1-9)——P0-3 要的引用同一性("用这个 metric" = 被校验过的那个 metric)。
    调用方拿到产物即可回答"这份 SQL 是哪份计划编译出来的",不必假设它
    就是自己刚递进去的那一份。只在进程内交接:强类型对象不上 LangGraph
    wire(checkpointer 的 serde 白名单,见 contract.py 的实测表),wire 形状
    仍由 ``contract_to_wire`` 给出。
    """

    sql: str
    contract: PlanContract
    source_plan: "PlanQuery | None" = None


@dataclass
class CompileMiss:
    """编译失败的结构化分因(eval hit-rate 归因用)。

    reason: 稳定 slug(见 MISS_REASONS);component: 人类可读的失败组件。
    消费方(query_sketch/eval)用 reason 聚合;``compile_from_plan`` 旧契约
    将其映射回 None(字节级向后兼容)。
    """

    reason: str
    component: str = ""


@dataclass
class PartialCompile:
    """编译产物(骨架):可解析部分已权威编译,未解析组件留给生成通道补齐。

    分级逃生梯:软 MISS(词表/值/口径未声明)不再整体拒绝——已解析的
    join/过滤/分组编译成骨架 SQL,未解析组件进 ``miss_parts``,消费方
    (query_sketch)注入 gen_sql 让 LLM 补缺,回答照常交付而非硬停。

    - ``sql``:骨架 SQL(权威的 join/过滤/分组,LLM 不得改动)。
    - ``contract``:权威交接契约(注入块 = ``render_contract(contract)``)。
    - ``miss_parts``:未解析组件列表 ``[{reason, component}]``(学习/归因用)。
      与 ``contract.gaps`` 同源同形。
    - ``source_plan``:骨架由哪份计划编译而来(A1-9,同 ``CompileResult``):
      ``None`` = 兜底入口(松 dict / 旧调用方),不是经 IR 校验的计划。
    """

    sql: str
    contract: PlanContract
    miss_parts: list[dict[str, str]] = field(default_factory=list)
    source_plan: "PlanQuery | None" = None


# 编译失败原因全集(新增 MISS 分支必须进此集合,保证 eval 归因闭合)
MISS_REASONS = frozenset({
    "no_plan_or_matched",
    "no_metric_match",
    "metric_anchor_unmatched",
    "unresolved_answer_column",
    "unresolved_filter_field",
    "invalid_op",
    "missing_filter_value",
    "expression_filter_value",
    "nothing_compilable",
    "fan_out",
    "unknown_cardinality",
    "unreachable_table",
    "table_not_allowed",
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
    "extreme_rank_unsupported",
    "extreme_rank_scope_unsupported",
})

# 硬 MISS(结构性,拒绝是保护):放行会产出行倍增/笛卡尔/引用未覆盖表/
# 坏定义(派生环/深度/未解析)或越出逻辑宇宙的 SQL。这些是建模错误或
# 覆盖外,必须暴露给 refuse 扩展流程。**该集合是路由判定唯一权威**——
# 新增 MISS 分支时必须归入其中一边。
HARD_MISS_REASONS = frozenset({
    "no_plan_or_matched",
    "metric_anchor_unmatched",
    "nothing_compilable",
    "fan_out",
    "unknown_cardinality",
    "unreachable_table",
    "table_not_allowed",
    "ambiguous_join_path",
    "derived_cycle",
    "derived_depth",
    "derived_unresolved",
    "limit_without_order",
    "guardrail_rejected",
})

# 软 MISS(词表/值/口径缺失):组件不在声明模型内但**跳过它继续编译**
# 可解析部分是安全的——未解析部分交回生成通道(LLM 按 plan 文本补齐),
# 由骨架保真校验 + rules 链 + 版本回归兜底。软 MISS 不触发拒绝。
SOFT_MISS_REASONS = frozenset({
    "no_metric_match",
    "unresolved_answer_column",
    "unresolved_filter_field",
    "invalid_op",
    "missing_filter_value",
    "expression_filter_value",
    "enum_value_unresolved",
    "having_metric_unknown",
    "having_without_aggregation",
    "bad_time_grain",
    "time_field_not_declared",
    "time_field_not_temporal",
    "time_grain_without_aggregation",
    "analysis_unsupported_type",
    "analysis_metric_unknown",
    "analysis_partition_unresolved",
    "analysis_order_unresolved",
    "analysis_time_required",
    "analysis_invalid",
    # A①:极值声明(plan.extreme)在保守边界外不可消费——极值列不可解析 /
    # 取数集横跨其它表且 scope 未显式声明 global。计划其余部分照常编译。
    "extreme_rank_unsupported",
    "extreme_rank_scope_unsupported",
})


def is_hard_miss(reason: str) -> bool:
    """MISS reason → 是否硬 MISS(结构性,必须拒绝)。未分类原因按硬处理。"""
    if reason in HARD_MISS_REASONS:
        return True
    if reason in SOFT_MISS_REASONS:
        return False
    # 未知 reason 默认硬——宁可拒绝,不冒险产错 SQL;eval 归因会暴露漏分类。
    return True


def _qualified(tbl: str, expr: str, force_qualify: bool = True) -> str:
    """给字段投影加表限定(未限定才加)。"""
    ex = expr.strip()
    if "." in ex or not force_qualify:
        return ex
    return f"{tbl}.{ex}"


def _split_value_list(text: str) -> tuple[list[str], bool]:
    """按顶层逗号拆括号值列表(``'C', 'D'``),尊重引号内的逗号与翻倍引号。

    ``''`` 成对出现视为引号串内的转义(不结束串)。括号不配对或出现空
    元素 → ``([], False)``,调用方整体拒绝。
    """
    parts: list[str] = []
    cur: list[str] = []
    i, n = 0, len(text)
    quote: str | None = None
    while i < n:
        ch = text[i]
        if quote is not None:
            if ch == quote:
                if i + 1 < n and text[i + 1] == quote:
                    cur.append(ch)
                    cur.append(ch)
                    i += 2
                    continue
                quote = None
            cur.append(ch)
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            cur.append(ch)
            i += 1
            continue
        if ch == ",":
            parts.append("".join(cur).strip())
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    if quote is not None:
        return [], False
    parts.append("".join(cur).strip())
    if any(p == "" for p in parts):
        return [], False
    return parts, True


def _canonical_literal(s: str) -> str | None:
    """把"已带字面量形态"的字符串规范重排为转义后的 SQL 字面量。

    支持:引号串 ``'C'``/``"C"``、数值、``NULL/TRUE/FALSE``、括号值列表
    ``('C', 'D')``(逐元素递归经 ``_literal`` 规范化)。任一形态无法
    安全解析 → None,调用方回退保守转义路径。

    这是 ``_literal`` 的安全边界:用户文本**绝不**被当作 SQL 片段原样
    透传——旧实现靠正则整串匹配后放行,一旦匹配(如 ``' OR 1=1 -- '``)
    即把用户字符串原封不动嵌进 SQL;现在一律剥引号 → 反转义 → 重新转义,
    输出恒为单个规范字面量。
    """
    s = s.strip()
    if not s:
        return None
    if len(s) >= 2 and s[0] == "(" and s[-1] == ")":
        parts, ok = _split_value_list(s[1:-1])
        if not ok:
            return None
        items: list[str] = []
        for p in parts:
            lit = _literal(p)
            if lit is None:
                return None
            items.append(lit)
        return "(" + ", ".join(items) + ")"
    q = s[0]
    if len(s) >= 2 and q in "'\"" and s[-1] == q:
        body = s[1:-1]
        if q == "'":
            if body.count("'") % 2:
                return None
            body = body.replace("''", "'")
        elif '"' in body:
            # 双引号串内出现未转义引号 → 无法安全确认形态,保守拒绝
            return None
        return "'" + body.replace("'", "''") + "'"
    if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", s):
        return s
    if s.upper() in {"NULL", "TRUE", "FALSE"}:
        return s.upper()
    return None


def _literal(value: Any) -> str:
    """WHERE 值字面量:字符串加单引号并转义,数值原样。

    plan 的 condition value 可能已带引号(``'C'``)、是值列表(``('C', 'D')``)
    或数字——这些形态经 :func:`_canonical_literal` 解析后**规范重排**,
    其余一律保守转义加引号。绝不原样透传用户文本。
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    canonical = _canonical_literal(s)
    if canonical is not None:
        return canonical
    return "'" + s.replace("'", "''") + "'"


#: 表达式型 filter 值:plan 把算式写进 condition value 的形态(实测 0488:
#: ``(SELECT AVG(amount) FROM trans WHERE ...)``)。
_EXPR_VALUE_RE = re.compile(
    r"^\s*\(*\s*(?:select\b|sum\s*\(|avg\s*\(|count\s*\(|min\s*\(|max\s*\()",
    re.I,
)


def _looks_like_expression_value(value: Any) -> bool:
    """condition value 是否算式文本(子查询/聚合式)而非字面量。

    ``_literal`` 对未知形态一律保守加引号——算式文本被冻成字符串常量后,
    骨架保真校验又要求 gen 逐字复现该坏 SQL,正确写法反被判 drift(0488
    实测:十轮 COMPILE_DRIFT 死循环)。识别到算式即软 MISS:该条件不进
    骨架,交 gen 按 plan 文本补(与 missing_filter_value 同族的**值语义
    缺口**,不是结构错误)。
    """
    if value is None or isinstance(value, (int, float, bool)):
        return False
    s = str(value).strip()
    if not s:
        return False
    # 计划显式带引号的值按字面量看待('1998%');但引号内仍是算式文本的
    # ("'(SELECT ...)'")照样识别——字符串里包 SQL 冻进骨架比软 MISS 更坏。
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        s = s[1:-1].strip()
    return bool(_EXPR_VALUE_RE.match(s))


_VALUE_STOPWORDS = {
    "a", "an", "the", "of", "for", "in", "on", "with", "to", "and", "or",
    "per", "by", "is", "are", "what", "how", "that", "this", "each", "its",
    "from", "at", "as", "all",
}


def _value_tokens(text: str) -> set[str]:
    """值/标签词元:小写、去停用词、去复数 s(statement↔statements 归一)。

    与 provider 的字段同义词 token 化同款朴素处理——单复数差异不再阻断
    "monthly statement issuance" ↔ "monthly statements" 的匹配。
    """
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {
        w[:-1] if w.endswith("s") and not w.endswith("ss") else w
        for w in words if w not in _VALUE_STOPWORDS
    }


def _enum_code_for(
    text: str,
    enum_display: dict[str, str],
    value_aliases: dict[str, list[str]] | None = None,
) -> str | None:
    """人类值/码 → 规范 code(经 enum_display + value_aliases 双向匹配)。

    匹配优先级:
    1. code 键 identity(大小写不敏感)→ 返回原键(保库里存的写法);
    2. label 精确(enum_display 主标签或 value_aliases 别名,全角冒号同义);
    3. 词级兜底:label 词集与输入词集互相子集(**复数/停用词归一后**,
       ``weekly issuance`` ↔ "weekly statements"),唯一命中才采纳;
    4. 多 code 同命中(裸 "withdrawal" 同时是 VYDAJ/VYBER 的子集)→ None,
       调用方保守 MISS(值歧义,不猜)——绝不静默选错 code。

    ``value_aliases``:字段级 ``{code: [业务别名]}``(kb init/建模期从
    证据或人工标注沉淀的多标签值词典),扩展示例问法到存储值的确定性桥。
    """
    low = (text or "").strip().lower()
    if not low:
        return None
    for code in enum_display:
        if str(code).lower() == low:
            return str(code)
    aliases = {str(code): list(labels or []) for code, labels in (value_aliases or {}).items()}
    for code, label in enum_display.items():
        if str(label).strip().lower() == low:
            return str(code)
    for code, labels in aliases.items():
        if any(str(ln).strip().lower() == low for ln in labels):
            return code
    in_tokens = _value_tokens(low)
    if not in_tokens:
        return None
    matches: list[str] = []
    for code, label in enum_display.items():
        label_tokens = _value_tokens(str(label))
        if not label_tokens:
            continue
        if label_tokens <= in_tokens or in_tokens <= label_tokens:
            matches.append(str(code))
    for code, labels in aliases.items():
        for label in labels:
            label_tokens = _value_tokens(str(label))
            if not label_tokens:
                continue
            if label_tokens <= in_tokens or in_tokens <= label_tokens:
                if str(code) not in matches:
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


def _normalize_enum_value(
    value: Any,
    enum_display: dict[str, str],
    value_aliases: dict[str, list[str]] | None = None,
) -> Any | None:
    """枚举字段的 condition 值 → 规范 code;任一元素无法归一 → None。

    - enum_display 为空 → 原样透传(未声明词表,无归一依据);
    - 标量(可带引号)→ 单个归一(经 enum_display + value_aliases);
    - ``('C', 'D')`` 列表 → 逐元素归一。
    归一失败返回 None,调用方保守 MISS——绝不静默产出 ``gender='male'``
    这类 0 行 SQL(值不在声明词表 = 未覆盖,交拒绝/扩展流程)。
    """
    if not enum_display and not value_aliases:
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _enum_code_for(str(value), enum_display, value_aliases)
    s = str(value).strip()
    if len(s) >= 2 and s[0] == "(" and s[-1] == ")":
        parts = [p.strip() for p in s[1:-1].split(",") if p.strip()]
        if not parts:
            return None
        out: list[str] = []
        for p in parts:
            code = _enum_code_for(_strip_quotes(p), enum_display, value_aliases)
            if code is None:
                return None
            out.append(code)
        # 列表元素带引号:后续 _literal 的已字面量正则才能透传,产出
        # IN ('F', 'M') 而非 IN (F, M)(裸标识符会解析成列引用)。
        return "(" + ", ".join(f"'{c.replace(chr(39), chr(39) * 2)}'" for c in out) + ")"
    return _enum_code_for(_strip_quotes(s), enum_display, value_aliases)


# 聚合签名 = 表达式里**全部**聚合函数的有序 (函数名, 全限定列集, DISTINCT)。
#: 每聚合签名:(函数名, 列集, DISTINCT, 条件集)。条件集 = 聚合**内部**的
#: 归一化谓词文本(FILTER (WHERE p) 与 CASE WHEN p THEN v 两种形态统一——
#: 占比题两种拼法都有,必须互认);空集 = 无条件聚合。
_AggSig = tuple[tuple[str, frozenset[str], bool, frozenset[str]], ...]

#: 条件谓词文本归一:空白压缩 + 小写(``loan.status = 'A'`` ≡ ``LOAN.STATUS='A'``,
#: 但表限定与字面量值本身保持——它们是匹配的主键,不能也归一掉)。
_COND_WS_RE = re.compile(r"\s+")


def _norm_cond(text: str) -> str:
    return _COND_WS_RE.sub(" ", str(text).strip()).lower().rstrip(";")


def _agg_entry(f: Any) -> tuple[str, frozenset[str], bool, frozenset[str]] | None:
    """单个聚合节点 → 签名条目(函数名/被测列集/DISTINCT/内部条件集)。

    - 条件来源两处:父节点 ``Filter(WHERE p)``(即 ``AGG(x) FILTER (WHERE p)``)
      与该聚合内的 ``CASE WHEN p THEN v``(``When.this`` 为谓词);
    - CASE 形态的被测列取 **THEN 分支**的列(``SUM(CASE WHEN p THEN
      loan.amount ELSE 0 END)`` 测的是 amount,条件列归条件集,不再混进
      列集)——旧实现把条件列也算进列集,导致条件被完全无视;
    - ``SUM(CASE WHEN p THEN 1 ELSE 0 END)``(无被测列)归一为 COUNT:
      它就是"满足 p 的行数",与 ``COUNT(*) FILTER`` 同一度量。
    """
    from sqlglot import exp

    func = f.sql().split("(", 1)[0].strip().lower()
    distinct = bool(f.find(exp.Distinct))
    conds: list[str] = []
    cases = list(f.find_all(exp.Case))
    if cases:
        then_cols: set[str] = set()
        for case in cases:
            for when in case.args.get("ifs") or []:
                if when.this is not None:
                    conds.append(_norm_cond(when.this.sql()))
                true_branch = when.args.get("true")
                if true_branch is not None:
                    for c in true_branch.find_all(exp.Column):
                        if c.name:
                            then_cols.add(
                                f"{c.table}.{c.name}".lower() if c.table else c.name.lower())
        cols = frozenset(then_cols)
        if func == "sum" and not cols and not distinct:
            func = "count"
    else:
        cols = frozenset(
            (f"{c.table}.{c.name}" if c.table else c.name).lower()
            for c in f.find_all(exp.Column) if c.name
        )
    parent = f.parent
    if isinstance(parent, exp.Filter):
        where = parent.args.get("expression")
        if where is not None and getattr(where, "this", None) is not None:
            conds.append(_norm_cond(where.this.sql()))
    return (func, cols, distinct, frozenset(conds))


def _agg_signature(expr_text: str) -> _AggSig | None:
    """聚合表达式 → 全量聚合签名,用于 metric 对账。无聚合 → None。

    曾只取 ``funcs[0]`` 并把列集按**交集**比对,三条静默错数通道:

    - ``SUM(a)/COUNT(*)`` 只看到首函数 ``SUM(a)`` → 比值题命中声明的
      ``SUM(a)``,plan 整式被丢弃、换成被声明度量的表达式,编译成功而 SQL
      只算了分子(丢其余聚合 = 丢算式本身);
    - ``COUNT(DISTINCT x)`` 与 ``COUNT(x)`` 同签名 → 去重计数被普通计数顶替;
    - ``SUM(a + b)`` 与 ``SUM(a)`` 列集相交 → 两个不同度量判为同一个。

    第四条(2026-10 实测)由条件集堵上:``SUM(x) FILTER (WHERE c)`` 的
    Where 挂在聚合**父节点**上,旧实现看不见——单条件聚合与无条件度量
    同签名,条件被静默丢弃(问"仅 A 的金额",答"全部金额")。条件进签名
    后,条件不同即不同度量。

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
    return tuple(e for e in (_agg_entry(f) for f in funcs) if e is not None)


def _pk_distinct_tolerated(
    a_cols: frozenset[str], b_cols: frozenset[str], pk_of: Any,
) -> bool:
    """``COUNT(col)`` 与 ``COUNT(DISTINCT col)`` 是否同一度量:仅当被计对象是
    **声明主键**且列集全等(逐列判定;``pk_of`` 缺席即不宽容)。

    主键上的行计数与去重行计数数学等价(键唯一)——这正是 0470 型
    「声明 COUNT(pk)、计划写 COUNT(DISTINCT pk)」的对账依据。非主键列上的
    DISTINCT 是**另一个度量**(数不同取值),严格不等:那是既有防线
    (``test_distinct_count_not_matched_by_plain_count`` 的原意图),不在此放宽。
    """
    if pk_of is None or not a_cols or not b_cols:
        return False
    if a_cols != b_cols:
        return False
    return all(pk_of(c) for c in a_cols)


def _sig_compatible(a: _AggSig, b: _AggSig, pk_of: Any = None) -> bool:
    """逐聚合函数比对:个数、函数名、条件集、列集全等(一侧空集即通配)。

    列集用**相等**而非相交:``SUM(a + b)`` 与 ``SUM(a)`` 相交但不等,是不同
    度量。``COUNT(*)`` 的空列集是唯一有意的放宽——行数度量与 ``COUNT(col)``
    互认,是既有设计。

    条件集用**全等**:``SUM(x)`` 与 ``SUM(x) FILTER (WHERE c)`` 是两个度量,
    互不相认;条件文本已归一(空白/大小写),但表限定与字面量值保留
    (``status='A'`` ≠ ``status='B'``——这正是占比题按枚举值区分度量的依据)。

    ``pk_of``(列引用 → 是否声明主键)给出第二处放宽:func/conds/cols 全等、
    仅 DISTINCT 不同、且列集全为主键时视为同一度量(见
    :func:`_pk_distinct_tolerated`)。缺省 ``None`` = 不宽容(旧行为)。
    """
    if len(a) != len(b):
        return False
    for (a_name, a_cols, a_dist, a_cond), (b_name, b_cols, b_dist, b_cond) in zip(a, b):
        if a_name != b_name:
            return False
        if a_cond != b_cond:
            return False
        if a_dist != b_dist and not _pk_distinct_tolerated(a_cols, b_cols, pk_of):
            return False
        if not a_cols or not b_cols:
            continue  # COUNT(*) 通配
        if a_cols != b_cols:
            return False
    return True


#: 条件占比形态:(条件聚合条目, 全量聚合条目, 标度)。标度 = 算式里聚合
#: **之外**的数值字面量含 100 → "percent"(占比题问的是 ×100 的结果),
#: 否则 "fraction"。两者互不匹配——量纲差 100 倍,静默互认就是静默错数。
_ShareShape = tuple[
    tuple[str, frozenset[str], bool, frozenset[str]],
    tuple[str, frozenset[str], bool, frozenset[str]],
    str,
]


def _contains_agg(root: Any, target: Any) -> bool:
    """root 子树(含自身)是否包含 target 聚合节点(按节点身份)。"""
    from sqlglot import exp

    if root is target:
        return True
    return any(n is target for n in root.find_all(exp.AggFunc))


def _share_shape(expr_text: str) -> _ShareShape | None:
    """条件占比算式 → 归一形态;非占比 → None(交回常规签名路径)。

    识别的是「同一度量的**条件版** / **全量版**」这一构造(0480/0481 形状):

        sum(loan.amount) FILTER (WHERE loan.status = 'A') * 100.0 / sum(loan.amount)
        (SUM(CASE WHEN loan.status = 'C' THEN loan.amount ELSE 0 END) / SUM(loan.amount)) * 100

    两种拼法(FILTER/CASE)、两种操作数顺序(``a*100/b`` 与 ``a/b*100``)、
    ``NULLIF(分母, 0)`` 除零守卫与 ``ELSE 0`` 填充——全部归一到同一个
    (条件聚合, 全量聚合, 标度),使 plan 的自由写法能对账到声明的占比
    度量。硬条件(缺一即 None,交常规路径):

    - 恰两个聚合,一个带条件、一个不带;
    - 两侧函数与列集一致(同一度量的条件版/全量版;COUNT(*) 空列集通配
      与 _sig_compatible 同规则);
    - 存在除法,且条件聚合在**分子侧**(分母条件占比是另一个量,不互认);
    - 标度按聚合外字面量是否含 100 判定。
    """
    from sqlglot import exp, parse_one

    try:
        tree = parse_one(expr_text)
    except Exception:
        return None
    aggs = list(tree.find_all(exp.AggFunc))
    if len(aggs) != 2:
        return None
    entries = [(_agg_entry(f), f) for f in aggs]
    if any(e is None for e, _ in entries):
        return None
    cond_side = [(e, f) for e, f in entries if e[3]]
    plain_side = [(e, f) for e, f in entries if not e[3]]
    if len(cond_side) != 1 or len(plain_side) != 1:
        return None
    cond_e, cond_node = cond_side[0]
    plain_e, plain_node = plain_side[0]
    if cond_e[0] != plain_e[0] or cond_e[2] != plain_e[2]:
        return None
    if cond_e[1] and plain_e[1] and cond_e[1] != plain_e[1]:
        return None
    divs = list(tree.find_all(exp.Div))
    if not any(
        _contains_agg(d.this, cond_node) and _contains_agg(d.expression, plain_node)
        for d in divs
    ):
        return None
    percent = False
    for lit in tree.find_all(exp.Literal):
        if not lit.is_number or lit.find_ancestor(exp.AggFunc) is not None:
            continue
        try:
            num = float(lit.this)
        except ValueError:
            continue
        if num == 100:
            percent = True
            break
    return (cond_e, plain_e, "percent" if percent else "fraction")


class SemanticCompiler:
    """把 plan 的构件级成分编译成权威 SQL(只认已声明模型条目)。

    严格模式:metric/group_by/filter 每项都必须解析到模型里的 metric/
    field/relationship,任一项解析失败整体 MISS(返回 None)→ 管线降级
    现有 LLM 生成通道。这保证编译通过的 SQL 永远落在「逻辑宇宙」内。
    """

    def __init__(self, model: SemanticModel, allowed_tables: set[str] | None = None):
        self._model = model
        self._fields: dict[tuple[str, str], Any] = {}  # (dataset, field) → field
        self._datasets: dict[str, SemanticDataset] = {}
        for d in model.datasets:
            self._datasets[d.name] = d
            for f in d.fields:
                self._fields[(d.name, f.name)] = f
        # 执行期表授权白名单(与 registry/执行守卫同一份):非空时,编译期
        # 就拒绝引用越界数据集——把授权从「执行期事后拒」前移到「编译期不产」。
        # None/空 = 不限制(仅执行期元数据表拒绝照旧)。
        self._allowed_tables = (
            {t.lower() for t in allowed_tables} if allowed_tables else None
        )
        # 每次 compile 调用开头赋值(force_dialect / matched):派生内联与
        # 时间分桶的方言渲染、裸列解析锚定都依赖这两个会话态。
        self._dialect: str = "sqlite"
        self._matched_set: set[str] = set()
        # 裸列歧义消歧锚:已命中 metric 表达式的 (table, column) 引用集合。
        # 同名列跨数据集(如 loan.amount vs trans.amount)时,歧义候选优先
        # 锚定到命中度量的数据集(compile_detailed 开头由 matched_pairs 设置)。
        self._field_anchor: set[tuple[str, str]] = set()

    # ── component resolution ─────────────────────────────

    def metrics(self) -> list[SemanticMetric]:
        return list(self._model.metrics)

    def _dataset_allowed(self, name: str) -> bool:
        """数据集(或其物理 source)是否在执行期授权白名单内;无白名单 = 放行。"""
        if self._allowed_tables is None:
            return True
        ds = self._datasets.get(name)
        source = (ds.source or name) if ds is not None else name
        return (
            name.lower() in self._allowed_tables
            or source.lower() in self._allowed_tables
        )

    def _row_filter_sql(self, table: str) -> str | None:
        """数据集 row_filter → 注入用谓词(裸列限定到本表);无声明 → None。

        渲染规则与快径共用一份实现(``rls.render_row_filter``)。此前这里是
        唯一实现,而快径不过编译器 —— 声明层授权在快径上失效(见 rls 模块
        docstring)。
        """
        return rls.render_row_filter(self._datasets.get(table), self._dialect, table)

    def _metric_by_name(self, name: str | None) -> SemanticMetric | None:
        """按度量名精确匹配(大小写不敏感)。派生度量名引用/裸名候选共用。"""
        n = (name or "").strip().lower()
        for m in self._model.metrics:
            if m.name.strip().lower() == n:
                return m
        return None

    # ── 主键判定(A1.2 签名 PK 容忍 / A1.1 扇出提升共用)──────────
    #
    # 主键声明在数据集上是**字段名**(primary_key=["district_id"]),而列引用
    # 可能写字段名、也可能写 expression(物理列)。判定按「名字 ↔ 名字、
    # expression ↔ 主键名」两条都认,与 rls.physical_table 的口径一致
    # (声明层用名字,物理层用 expression)。

    @staticmethod
    def _pk_matches(ds: SemanticDataset, col: str) -> bool:
        pks = {str(k).strip().lower() for k in (ds.primary_key or [])}
        if not pks:
            return False
        col = str(col).strip().lower()
        if not col:
            return False
        if col in pks:
            return True
        for f in ds.fields:
            name = str(f.name).strip().lower()
            expr = str(f.expression).strip().lower()
            if name in pks and col == expr:
                return True
            if expr in pks and col == name:
                return True
        return False

    def _is_pk_ref(self, ref: str) -> bool:
        """列引用(``table.col`` / 裸列)→ 是否落在**声明主键**上。

        带表限定时直接查该数据集;裸列在多数据集同名时要求唯一命中
        (歧义 → False:判不了主键就不放宽)。
        """
        ref = str(ref or "").strip().lower()
        if not ref:
            return False
        if "." in ref:
            tbl, col = ref.split(".", 1)
            ds = self._datasets.get(tbl)
            return ds is not None and self._pk_matches(ds, col)
        hits = [ds for ds in self._datasets.values() if self._pk_matches(ds, ref)]
        return len(hits) == 1

    def _metric_by_expression(self, ref: str) -> SemanticMetric | None:
        """表达式形态的度量引用(``avg(district.A11)``)→ **唯一**命中度量。

        answer_columns/aggregation 早已支持「按表达式形状对账度量」,having 与
        analysis.metric 的引用此前只认名字(``_metric_by_name``),同一份计划里
        表达式引用在 having 上就静默丢弃 —— 这里是那条缺口的补齐。多命中
        (表达式对得上多个度量)不猜 → None(调用方维持各自的既有行为)。
        """
        if not str(ref or "").strip() or "(" not in str(ref):
            return None
        hits = [
            m for m in self._model.metrics
            if self._matches_metric_expression(ref, m)
        ]
        return hits[0] if len(hits) == 1 else None

    def _matches_metric_expression(self, ref: str, m: SemanticMetric) -> bool:
        """引用对账单个度量:名字精确 → True;签名兼容(含 PK 容忍)→ True;
        条件占比形态相同 → True。"""
        if m.name.strip().lower() == str(ref).strip().lower():
            return True
        sig = _agg_signature(ref)
        if sig is not None:
            msig = _agg_signature(m.expression)
            if msig is not None and _sig_compatible(sig, msig, self._is_pk_ref):
                return True
        share = _share_shape(ref)
        if share is not None and _share_shape(m.expression) == share:
            return True
        return False

    # ── 值路由(A4b):字面量落到"真正持有该值"的字段 ─────────────
    #
    # query_sketch 偶尔把值写在**结构上正确、语义上错位**的列上:0476 的
    # ``client.district_id = 'Sokolov'`` —— 区名是 district 表的值,却被放到了
    # client 的外键列。字段解析会成功(列确实声明过),条件于是被权威化进
    # 骨架 WHERE,生成侧再想纠正也要撞骨架保真校验。这里在编译**最前端**
    # 做一次模型级值索引对账:字面量唯一命中别处的值词表 → 重锚该字段。
    #
    # 值来源三处:字段 ``values``(Lane B 回填的实际取值,``getattr`` 防御
    # 读取——接口预冻结 Ⅰ,车道可独立落地)、``enum_display`` 的键与值(码值
    # 与人类可读词)、``value_aliases``(码值与全部别名)。只做 ``=``/``in``:
    # 其余算子(|>|<|like)的值语义是区间/模式,不是词表命中。

    @staticmethod
    def _has_value_vocabulary(f: Any) -> bool:
        """字段是否自带值词表(values / enum_display / value_aliases)。"""
        return bool(
            getattr(f, "values", None)
            or getattr(f, "enum_display", None)
            or getattr(f, "value_aliases", None)
        )

    def _value_index(self) -> dict[str, list[tuple[str, str]]]:
        """值 → [(dataset, field)] 索引(小写、去引号;一个值可命中多字段)。"""
        idx: dict[str, list[tuple[str, str]]] = {}
        for ds in self._model.datasets:
            for f in ds.fields:
                for raw in (getattr(f, "values", ()) or ()):
                    _index_value(idx, raw, ds.name, f.name)
                for k, v in (f.enum_display or {}).items():
                    _index_value(idx, k, ds.name, f.name)
                    _index_value(idx, v, ds.name, f.name)
                for k, aliases in (f.value_aliases or {}).items():
                    _index_value(idx, k, ds.name, f.name)
                    for a in (aliases or ()):
                        _index_value(idx, a, ds.name, f.name)
        return idx

    def _reroute_condition_values(
        self, plan: dict[str, Any], matched_set: set[str],
    ) -> dict[str, Any]:
        """条件字面量按值索引重锚字段;仅在可安全重锚时改写(浅拷贝)。"""
        conditions = plan.get("conditions") or []
        if not conditions:
            return plan
        # 只在**显式 joins 通道**上重锚:重锚会把源→目标的声明边追加进
        # joins,而 joins 缺席时这等于把计划从 BFS 通道拽进显式通道 ——
        # BFS 对同一对表的选边不可审计(0476 会经 account 绕到 district,
        # 语义错的居住区)。无显式路径 = 保持今日行为。
        if _join_clauses(plan.get("joins")) is None:
            return plan
        index = self._value_index()
        if not index:
            return plan
        new_conditions: list[Any] = []
        extra_joins: list[str] = []
        # 已存在的连接子句:重锚要追加的边若**已在 plan.joins 里**,追加就是
        # 重复边 —— 显式路径的左深树判定会因此判「不连通」而硬 MISS,比不重锚
        # 还糟。按(无序)列对查重,方向/格式/分隔符无关。
        existing_keys = _join_pair_keys(plan.get("joins"))
        appended_keys: set[frozenset[str]] = set()
        changed = False
        for cond in conditions:
            rerouted = self._reroute_one(cond, matched_set, index)
            if rerouted is None:
                new_conditions.append(cond)
                continue
            new_cond, rel_edges = rerouted
            new_conditions.append(new_cond)
            for e in (rel_edges or []):
                key = _edge_pair_key(e)
                if key in existing_keys or key in appended_keys:
                    continue
                appended_keys.add(key)
                extra_joins.append(_edge_text(e))
            changed = True
        if not changed:
            return plan
        out = dict(plan)
        out["conditions"] = new_conditions
        if extra_joins:
            # 重锚把条件搬到了另一张表上 → 该表必须进联路径(否则
            # unreachable_table 硬 MISS 取代今日的可编译结果)。
            base = str(plan.get("joins") or "").strip()
            extra = " AND ".join(extra_joins)
            out["joins"] = f"{base} AND {extra}" if base else extra
        return out

    def _reroute_one(
        self, cond: Any, matched_set: set[str],
        index: dict[str, list[tuple[str, str]]],
    ) -> tuple[dict[str, Any], list[JoinEdge] | None] | None:
        """单条条件 → (重锚后的条件, 需追加的连边);不重锚 → None。

        重锚的四道闸(任一不满足即不动,保持今日行为):
          ① 算子 ∈ {=, in},值是纯字面量(算式型值早已在别处软 MISS);
          ② 源字段解析成功且**自身没有值词表** —— 有词表时值归它,索引无权改判;
          ③ 每个字面量唯一命中同 (dataset, field),且全体命中同一目标;
          ④ 跨表时源→目标恰有**一条**声明关系(0 条无路可走,>1 条是二义)。
        """
        if not isinstance(cond, dict):
            return None
        op = str(cond.get("op") or "=").strip().lower()
        if op not in ("=", "==", "in"):
            return None
        value = cond.get("value")
        if value is None or _looks_like_expression_value(value):
            return None
        field_ref = str(cond.get("field") or "").strip()
        resolved = self._resolve_field(field_ref, matched_set)
        if resolved is None:
            return None
        src_ds, src_field = resolved
        if self._has_value_vocabulary(src_field):
            return None
        literals = _literal_items(value)
        if not literals:
            return None
        targets: list[tuple[str, str]] = []
        for lit in literals:
            key = str(lit).strip().strip("'\"").lower()
            if not key or _NUMERIC_LITERAL_RE.match(key):
                return None
            hits = index.get(key) or []
            if len(hits) != 1:
                return None
            if hits[0] not in targets:
                targets.append(hits[0])
        if len(targets) != 1:
            return None
        tgt_ds, tgt_name = targets[0]
        if tgt_ds.lower() == src_ds.lower() and str(tgt_name).lower() == str(src_field.name).lower():
            return None
        tgt_field = self._fields.get((tgt_ds, tgt_name))
        if tgt_field is None:
            return None
        rel_edges: list[JoinEdge] | None = None
        if tgt_ds.lower() != src_ds.lower():
            rels = _declared_rels_between(self._model, src_ds, tgt_ds)
            if len(rels) != 1:
                return None
            rel_edges = _relationship_edges(rels[0])
        new_cond = dict(cond)
        new_cond["field"] = f"{tgt_ds}.{tgt_field.name}"
        return new_cond, rel_edges

    # ── 条件所有权(A3b):软 MISS 聚合候选自带的内部条件 ─────────
    #
    # 占比/条件聚合候选(FILTER/CASE)的谓词是**分子定义的一部分**,不是行级
    # 过滤。计划里往往同一谓词还有一条孪生 conditions 条目:冻结进骨架
    # WHERE 后,生成侧按"分子进聚合"重写时与骨架冲突(0476)。
    # 键口径与 ``_skeleton_where`` 逐字一致(共用 ``_conds_of``)。

    @staticmethod
    def _soft_agg_cond_keys(
        candidates: list[str],
    ) -> set[tuple[frozenset[tuple[str, str]], str, tuple[str, ...]]]:
        """聚合候选内部条件 → 归一化条件键集合。

        薄包装(兼容入口):逻辑在模块级 ``agg_owned_cond_keys`` —— 条件所有权
        只取决于"该谓词是否已是某个候选定义的一部分",与候选是否命中了声明
        度量无关(A④:命中的 share 候选同样持有内部谓词)。
        """
        return agg_owned_cond_keys(candidates)

    # ── 扇出提升(A1.1)/ having 折叠(A1d)─────────────────────────

    def _promote_fanout_counts(
        self, projections: list[str], joins: list[JoinEdge],
    ) -> list[str]:
        """1:N 扇出下的行计数提升:``COUNT(<t>.<pk>)`` → ``COUNT(DISTINCT …)``。

        联路径上 ``<t>`` 位于某条 many→one 边的「1」端时,连接把 t 的行按多端
        重复 —— 声明的 ``COUNT(t.pk)`` 直接内联得到的是**行对数**,不是「t 的
        行数」(0470:2645 vs 77)。PK 上的 DISTINCT 数学等价于行计数(t.pk
        唯一),把数拉回声明的度量语义。范围刻意收窄:非 PK 列不改(DISTINCT
        非键列是另一个度量)、``COUNT(*)`` 不改(无列身份)、M:N 边不参与
        (dedup 豁免只保证单侧去重,行对数仍非 t 的行数)。
        """
        one_end = {e.to.lower() for e in joins if not _is_many_to_many(e.cardinality)}
        if not one_end:
            return projections
        from sqlglot import exp, parse_one

        out: list[str] = []
        for text in projections:
            try:
                tree = parse_one(text)
            except Exception:
                out.append(text)
                continue
            changed = False
            for count in list(tree.find_all(exp.Count)):
                if count.find(exp.Distinct) is not None:
                    continue
                col = count.this
                if not isinstance(col, exp.Column) or not col.table or not col.name:
                    continue
                if str(col.table).lower() not in one_end:
                    continue
                if not self._is_pk_ref(f"{col.table}.{col.name}"):
                    continue
                count.set("this", exp.Distinct(expressions=[col.copy()]))
                changed = True
            out.append(tree.sql(dialect=self._dialect) if changed else text)
        return out

    @staticmethod
    def _single_agg_column(
        expr_text: str, funcs: frozenset[str],
    ) -> tuple[str, str] | None:
        """表达式 = 恰一个 ``funcs`` 聚合,且被测对象是单个带表限定的列 → (表, 列)。"""
        from sqlglot import exp, parse_one

        try:
            tree = parse_one(expr_text)
        except Exception:
            return None
        aggs = list(tree.find_all(exp.AggFunc))
        if len(aggs) != 1:
            return None
        agg = aggs[0]
        if str(agg.key).lower() not in funcs:
            return None
        operand = agg.this
        if not isinstance(operand, exp.Column) or not operand.table or not operand.name:
            return None
        return (str(operand.table), str(operand.name))

    def _is_advisory_plan(self, agg_declared: bool, is_agg: bool) -> bool:
        """骨架降级判定(A3c):计划级退化 → WHERE 只作参考,不做保真硬校验。

        触发面刻意收窄为两类**计划级**缺口:声明了聚合意图却没命中任何度量
        (agg_declared ∧ ¬is_agg:aggregation 自由文本没对账上),或分析组件
        未解析(analysis_*:窗口包装的结构目标缺失)。外围缺口(值词表/单条
        条件的字段/时间分桶)不触发 —— 那些情况下 WHERE 里已解析出的部分
        仍是可信的权威骨架,降级是白放水。
        """
        if agg_declared and not is_agg:
            return True
        return any(
            str(g.get("reason") or "").startswith("analysis_")
            for g in self._soft_misses
        )

    # ── 极值消费(A①,plan.extreme)──────────────────────────────
    #
    # query_sketch 的 extreme 声明("最高的 X / 第二大的 Y")此前被编译器
    # 完全忽略:骨架既没有 ORDER BY+LIMIT 1,也没有极值谓词 —— 计划里的
    # 选择语义被静默丢给生成侧,骨架与计划在"选哪一行"上不一致。
    # 消费规则(保守,越界一律**软 MISS**,绝不硬拒):
    #   投影为空 → 兜底 MAX/MIN(极值列);
    #   投影已含 FUNC(极值列) → 已表达,不动;
    #   rank ≥ 2,或聚合投影不含极值列 → 行级选择谓词
    #     ``col = (SELECT col FROM t [WHERE 同表条件] ORDER BY col DESC LIMIT 1
    #     [OFFSET n-1])``;
    #   纯维度投影 + rank = 1 → ``ORDER BY col DESC`` + LIMIT 1;
    #   显式 ordering / analysis 存在 → 整体跳过(更强的呈现声明优先)。

    #: scope 文本里的显式"全局"标记 → 极值的取数集是**未过滤**的表。
    _EXTREME_GLOBAL_MARKERS = (
        "global", "全局", "among all", "all districts", "all rows",
        "entire table", "whole table", "unfiltered",
    )

    def _extreme_target(
        self, plan: dict[str, Any],
    ) -> tuple[str, str, Any, int, str] | None:
        """plan.extreme → (func, dataset, field, rank, scope);不可消费 → None。

        - 显式 ``ordering`` 非空 → 整体跳过(显式排序是更强的声明,叠加会
          产出互相矛盾的 ORDER BY);
        - ``analysis`` 存在 → 整体跳过(窗口包装自己决定排序/分区;分析题
          普遍带 extreme 噪声,记软 MISS 会把一批分析题无谓降级 → 只记 log);
        - func 只认 max/min(其余形态维持旧行为:忽略);
        - column 必须经 ``_resolve_field`` 落到**声明字段**;聚合/metric
          形态(``avg(district.A11)``)解析不到 → 软 MISS
          ``extreme_rank_unsupported``(极值列不在声明模型内,选择谓词无从构造);
        - rank 取 ``extreme.rank``(节点车道从问句序数填充),缺省/非法 → 1。
        """
        ex = plan.get("extreme")
        if not isinstance(ex, dict) or not ex:
            return None
        if parse_ordering(plan.get("ordering")):
            logger.debug("semantic-layer: extreme skipped, explicit ordering wins")
            return None
        if plan.get("analysis"):
            logger.debug("semantic-layer: extreme ignored under analysis wrapping")
            return None
        func = str(ex.get("func") or "").strip().lower()
        if func not in ("max", "min"):
            return None
        col_ref = str(ex.get("column") or "").strip()
        if not col_ref:
            return None
        resolved = self._resolve_field(col_ref, self._matched_set)
        if resolved is None:
            self._record_soft("extreme_rank_unsupported", col_ref)
            return None
        return (
            func, resolved[0], resolved[1], _extreme_rank(ex),
            str(ex.get("scope") or ""),
        )

    def _extreme_selection_predicate(
        self,
        func: str,
        dataset: str,
        fld: Any,
        rank: int,
        scope: str,
        filters: list[tuple[str, Any, str, Any]],
    ) -> str | None:
        """极值行级选择谓词:外层 ``col = (子查询取第 rank 个极值)``。

        子查询自带 ORDER BY/LIMIT/OFFSET(方言通用);**同表**条件复制进
        子查询(把"极值"限定在计划声明的过滤集内)。条件若落在别的表上而
        scope 未显式声明全局 → 保守放弃(软 MISS ``extreme_rank_scope_
        unsupported``):跨表条件无法复制进子查询,而不复制会在**未过滤集**
        上取极值(张冠李戴,比没有谓词更坏)。
        """
        col = _qualified(dataset, fld.expression)
        global_scope = any(
            m in str(scope or "").lower() for m in self._EXTREME_GLOBAL_MARKERS)
        if not global_scope and any(
            t.lower() != dataset.lower() for t, _f, _op, _v in filters
        ):
            self._record_soft("extreme_rank_scope_unsupported", col)
            return None
        same_conds = [] if global_scope else [
            f"{_qualified(t, f.expression)} {op.upper()} {_literal(v)}"
            for t, f, op, v in filters if t.lower() == dataset.lower()
        ]
        direction = "DESC" if func == "max" else "ASC"
        sub = f"SELECT {col} FROM {dataset}"
        if same_conds:
            sub += " WHERE " + " AND ".join(same_conds)
        sub += f" ORDER BY {col} {direction} LIMIT 1"
        if rank > 1:
            sub += f" OFFSET {rank - 1}"
        return f"{col} = ({sub})"

    def _repair_explicit_joins(
        self, joins_value: Any, needed: set[str],
    ) -> list[JoinEdge] | None:
        """显式 joins 列级校验失败的**表对修复**(A5a)。

        计划写错了列名(0477:``client.client_id = account.account_id``),但
        表对表达了真实的关系骨架:把每一对表还原成声明图上的**唯一**路径
        (client—disp—account),再交回既有显式 join 通道(基数守卫/左深树
        判定照旧,不豁免)。任何一对表无路径或多路径 → None(维持今日硬 MISS,
        不猜)。
        """
        if self._model is None:
            return None
        pairs = _table_pairs_from_joins(joins_value)
        if pairs is None:
            return None
        canon = {d.name.lower(): d.name for d in self._model.datasets}
        edges: list[JoinEdge] = []
        for a, b in pairs:
            a_c, b_c = canon.get(a), canon.get(b)
            if a_c is None or b_c is None or a == b:
                return None
            rels_ab = _declared_rels_between(self._model, a_c, b_c)
            if len(rels_ab) > 1:
                return None  # 同表对多关系 → 路径本身二义,不猜
            if len(rels_ab) == 1:
                edges.extend(_relationship_edges(rels_ab[0]))
                continue
            links = _declared_links(self._model)
            paths = _declared_paths(links, a_c, b_c, self._route_capable_pred())
            if len(paths) != 1:
                return None
            edges.extend(paths[0])
        edges = _unique_edges(edges)
        if not edges:
            return None
        return self._extend_to_needed(edges, needed)

    def _route_capable_pred(self):
        """中间点判定:可作路由桥的表(关系端点 ∪ M:N 关系端点)。"""
        rels = list(self._model.relationships)
        capable = {str(r.from_) for r in rels}
        capable |= {str(r.to) for r in rels if _is_many_to_many(r.cardinality)}
        return lambda t: t in capable

    def _extend_to_needed(
        self, edges: list[JoinEdge], needed: set[str],
    ) -> list[JoinEdge] | None:
        """修复出的边集必须覆盖 needed:未覆盖的表按声明**唯一**路径接入。

        接不上(无路径/多路径) → None(调用方维持硬 MISS)。
        """
        if not needed:
            return edges
        canon = {d.name.lower(): d.name for d in self._model.datasets}
        keep = self._route_capable_pred()
        links = _declared_links(self._model)
        out = list(edges)

        def _covered() -> set[str]:
            return {e.from_.lower() for e in out} | {e.to.lower() for e in out}

        missing = [t for t in sorted(needed) if str(t).lower() not in _covered()]
        for t in missing:
            t_c = canon.get(str(t).lower())
            if t_c is None:
                return None
            # 收集该表接进现有树的全部声明路径(经任意已覆盖表)。路径条数本身
            # 不判二义 —— 同一张表经不同锚点接入时,长路径往往只是同一份边的
            # 延伸(district→account 与 district→account→loan,后者多绕一条)。
            # 判据落在**最短路径是否唯一**:最短长度上有两条不同路径 = 真有
            # 两条可选接边(如菱形里 district 同时挂 client 与 account)→ None。
            candidates: list[list[JoinEdge]] = []
            seen: set[tuple] = set()
            for c in sorted(_covered()):
                c_c = canon.get(c)
                if c_c is None:
                    continue
                for path in _declared_paths(links, t_c, c_c, keep):
                    key = tuple(sorted(_edge_key(e) for e in path))
                    if key in seen:
                        continue
                    seen.add(key)
                    candidates.append(path)
            if not candidates:
                return None
            best_len = min(len(p) for p in candidates)
            best = [p for p in candidates if len(p) == best_len]
            if len(best) != 1:
                return None
            out.extend(best[0])
        return out

    @staticmethod
    def _prune_explicit_to_needed(
        edges: list[JoinEdge], needed: set[str], anchor: str,
    ) -> list[JoinEdge]:
        """显式 joins 通道的 needed 感知剪枝:删掉**只起连接作用**的叶子边。

        BFS 通道一直按 needed 剪枝,显式通道此前完全不看 needed —— 计划把
        共享维度(district/disp)写进 joins 就一律联进去(0492 型:account
        只是 district 的另一个叶,联上纯属多余,还会白白触发基数判定)。
        剪枝规则刻意保守,两条不变量:

        1. **只删叶子**:端点不在 keep 里且在当前边表里度数为 1 的边才可删,
           迭代到不动点 —— 删叶子保连通,两棵子树之间的桥(度数 ≥ 2 的中间
           点)照常保留,也不会产生"树序 JOIN 引用未入表"的非法产物;
        2. **锚表的声明连边一律不剪**:锚表是 FROM 根,它的连边定义了查询的
           粒度与行集(内连接会筛行)—— 计划显式声明它,就没有"它多余"这个
           判断的立足点;而且剪掉锚的连边会破坏"FROM 锚表 + 树序 JOIN"的
           骨架不变量(树从别的表起根)。这条也保证剪枝结果恒包含锚表,
           即恒有锚候选 —— 不存在剪空后静默回退 BFS 的路径。

        needed 为空 → 恒等(无从判断需要什么);显式边集本身**不连通**
        (两棵以上互不相连的树)→ 恒等:计划声明的路由整体不成树,"哪棵
        多余"无从判断,剪掉一整棵等于替计划猜它想联哪个组件 —— 原样返回
        后由左深树构筑失败维持今日 ambiguous_join_path 硬 MISS。
        """
        if not needed:
            return list(edges)
        if _edge_components(edges) > 1:
            return list(edges)
        anchor_l = str(anchor).lower()
        keep = {str(t).lower() for t in needed} | {anchor_l}

        def _anchor_incident(e: JoinEdge) -> bool:
            return anchor_l in (e.from_.lower(), e.to.lower())

        current = list(edges)
        changed = True
        while changed and current:
            changed = False
            degree: dict[str, int] = {}
            for e in current:
                degree[e.from_.lower()] = degree.get(e.from_.lower(), 0) + 1
                degree[e.to.lower()] = degree.get(e.to.lower(), 0) + 1

            def _prunable(e: JoinEdge) -> bool:
                if _anchor_incident(e):
                    return False
                for t in (e.from_, e.to):
                    if t.lower() not in keep and degree[t.lower()] == 1:
                        return True
                return False

            nxt = [e for e in current if not _prunable(e)]
            if len(nxt) != len(current):
                current = nxt
                changed = True
        return current

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
        """候选 → 声明度量:metric 名精确匹配 → 聚合签名 → 条件占比形态。

        第三档(占比)是**结构等价**匹配:plan 的自由拼法(FILTER/CASE、
        ``*100`` 的位置、NULLIF 守卫)归一后对账到声明的占比度量——
        声明的表达式始终是权威,plan 只提供"要的是哪个占比"。

        签名对账带 PK 容忍(pk_of):``COUNT(pk)`` ≡ ``COUNT(DISTINCT pk)``
        (主键唯一 → 行计数等价);非 PK 列的 DISTINCT 仍严格不等。
        """
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
            if _sig_compatible(sig, msig, self._is_pk_ref):
                return m
        share = _share_shape(cand)
        if share is not None:
            for m in self._model.metrics:
                if _share_shape(m.expression) == share:
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
            if not ref or ref == "*":
                return
            if "(" in ref:
                # 表达式形态引用(聚合/算式列):闭语法标量通道能解析出其中
                # 已限定列的**数据集**(0482 的算式列把 district 拉进 needed);
                # 解析不出来(纯聚合式 / 非闭语法)才跳过——补不进来的表由
                # 投影表守卫(unreachable_table)兜底,这里只做"能确定就补"。
                analyzed = _scalar_expr_analyze(ref, self)
                if analyzed is not None:
                    needed.update(analyzed[1])
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
        ex = plan.get("extreme")
        if isinstance(ex, dict):
            # A① 补收:极值列所在表进 needed —— 排序片段/选择谓词都要引用
            # 它,漏表会让产物引用连接树外的列(静默非法 SQL)。
            _add(ex.get("column"))
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

    def _set_field_anchor(
        self, matched_pairs: list[tuple[str, SemanticMetric]],
    ) -> None:
        """按已命中度量的表达式限定列引用构建裸列消歧锚。

        ``SUM(loan.amount)`` → {('loan', 'amount')}:查询里裸 ``amount`` 在
        matched 内跨数据集同名时,优先锚定命中度量所在数据集。只收带表限定
        的列(裸标识符是派生度量名引用,非物理列)。
        """
        self._field_anchor = set()
        for _cand, m in matched_pairs:
            try:
                tree = parse_one(m.expression)
            except Exception:
                continue
            for c in tree.find_all(exp.Column):
                if c.table and c.name:
                    self._field_anchor.add(
                        (str(c.table).lower(), str(c.name).lower()))

    def _resolve_field(self, ref: str, matched: set[str]) -> tuple[str, Any] | None:
        """列引用(``col`` / ``table.col``)→ (dataset, field);找不到 → None。

        先按字段名精确匹配;未命中时追加同数据集内 **synonyms 唯一命中**
        (如 ``district.region`` → 字段 ``A3``)——补偿 query_sketch 直接写别名列
        的场景。歧义(多个同义字段)优先用已命中度量的表达式锚定消歧
        (``_field_anchor``),锚定后仍多个/无锚 → None 走 LLM 通道,不猜。
        """

        def _anchor_unique(cands: list[tuple[str, Any]]) -> tuple[str, Any] | None:
            if len(cands) == 1:
                return cands[0]
            if self._field_anchor:
                anchored = [
                    c for c in cands
                    if (c[0].lower(), str(c[1].name).lower()) in self._field_anchor
                ]
                if len(anchored) == 1:
                    return anchored[0]
            return None

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
            return _anchor_unique(cands)

        hits = [
            (ds, f) for (ds, _n), f in self._fields.items()
            if ds in matched and f.name == ref
        ]
        exact = _anchor_unique(hits)
        if exact is not None:
            return exact
        cands = [
            (ds, f) for (ds, _n), f in self._fields.items()
            if ds in matched
            and any(s.lower() == ref.lower() for s in f.synonyms if s)
        ]
        return _anchor_unique(cands)

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

    def _record_soft(self, reason: str, component: str) -> None:
        """记录一条软 MISS(词表/值/口径缺失),跳过该组件继续编译。

        组件必须属于 SOFT_MISS_REASONS——硬 MISS 走 return 路径不经过这里,
        归类错误会在 eval 归因与 refuse 路由中被发现。同 (reason, component)
        去重:aggregation 与 answer_columns 可能命中同一候选,骨架提示块
        与 miss_parts 不应重复罗列同一缺口。
        """
        entry = {"reason": reason, "component": component}
        if entry not in self._soft_misses:
            self._soft_misses.append(entry)

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
        # 软 MISS 收集器:每次编译重置。软 MISS 只记录组件并跳过,不提前返回;
        # 结束时若有可编译成分 → PartialCompile(骨架),否则以首个软 MISS 为
        # 具体拒绝分因(见 nothing_compilable 分支)。
        self._soft_misses: list[dict[str, str]] = []

        agg_declared = str(plan.get("aggregation") or "").strip().lower() not in ("", "none", "无")
        matched_pairs, miss_candidates = self._match_metrics(plan)
        # 裸列歧义消歧锚:命中度量的表达式限定列 → 同名列跨数据集时锚定。
        self._set_field_anchor(matched_pairs)
        # A4b 值路由:条件字面量落在**没有值词表**的列上、而该值唯一命中
        # 别处字段的词表时,把条件重锚到真正持有该值的字段(0476:
        # client.district_id = 'Sokolov' → district.A2),并把源→目标的声明
        # 连边追加进 joins(否则重锚后的表不在联路径上 → unreachable_table)。
        # 返回浅拷贝,绝不改调用方的 plan dict。
        plan = self._reroute_condition_values(plan, matched_set)
        for cand in miss_candidates:
            # 聚合候选有签名但无兼容度量 → 软 MISS(跳过该候选继续编译,
            # 已命中的度量照常进骨架;全都没命中 → nothing_compilable 兜底)。
            self._record_soft("no_metric_match", cand)
        # A3b/A④ 条件所有权:聚合候选(FILTER/CASE 的分子条件)内部的谓词
        # 不进骨架 WHERE —— 它们属于聚合定义,计划里的孪生行级条件冻结进
        # 骨架后与生成侧的分子重写冲突(骨架保真校验打回正确 SQL)。
        # A④:面扩到**命中的**候选 —— 所有权取决于"该谓词是否已是某个候选
        # 定义的一部分",与候选是否对上声明度量无关;只喂未命中候选时,
        # 计划命中声明 share 度量后孪生行级条件仍会被冻结(0495 型冲突)。
        soft_agg_keys = agg_owned_cond_keys(
            [c for c, _m in matched_pairs] + list(miss_candidates))
        is_agg = bool(matched_pairs)
        if agg_declared and not is_agg:
            self._record_soft("no_metric_match", str(plan.get("aggregation") or ""))
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
                # 时间分桶组件无法解析 → 软 MISS:跳过,时间列按普通维度输出
                self._record_soft("bad_time_grain", str(tg))
                tg = None
            else:
                field_ref = str(tg.get("field") or "").strip()
                grain = str(tg.get("grain") or "").strip().lower()
                if grain not in GRAINS:
                    self._record_soft("bad_time_grain", grain)
                    tg = None
                else:
                    resolved_t = self._resolve_field(field_ref, matched_set)
                    if resolved_t is None:
                        self._record_soft("time_field_not_declared", field_ref)
                        tg = None
                    else:
                        tf = resolved_t[1]
                        if not (tf.is_time or (tf.datatype or "").lower() in _TEMPORAL_DTYPES):
                            self._record_soft("time_field_not_temporal", field_ref)
                            tg = None
                        else:
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
                    # A② 标量表达式列通道:算式/带函数的标量列(0482)按闭语法
                    # 解析 + **重建**后进投影(列强制表限定、函数名大写、
                    # 已有 CAST 原样保留);通道外一律弃用 —— 重建失败且形态
                    # 像算式才记软 MISS(number(*)/min(*) 这类无实参占位符
                    # 保持静默,同旧行为;已进度量通道的候选不重复记账)。
                    analyzed_expr = _scalar_expr_analyze(ac, self)
                    if analyzed_expr is not None:
                        expr_sql, _expr_tables = analyzed_expr
                        if expr_sql not in projections:
                            projections.append(expr_sql)
                            gb_exprs.append(expr_sql)
                            proj_display.append(_safe_display(
                                str(ac).split(".", 1)[-1]))
                            # 与 dim/metric 等长对齐的占位引用(分析包装按位置
                            # 取列名/分区,不解析 __expr__ 内容)
                            proj_ref.append(("__expr__", expr_sql))
                            last_dim_idx = len(projections) - 1
                        continue
                    if _looks_like_formula(ac) and _agg_signature(ac) is None:
                        self._record_soft("unresolved_answer_column", ac)
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
                    self._set_field_anchor(matched_pairs)  # 新命中度量并入消歧锚
                    is_agg = True
                    if metric.name not in seen_metrics:
                        seen_metrics.add(metric.name)
                        projections.append(proj)
                        proj_display.append(metric.name)
                        proj_ref.append(metric)
                    continue
                # 列不在声明字段(且不是度量名)→ 软 MISS:跳过该列,其余投影照常
                self._record_soft("unresolved_answer_column", ac)
                continue
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
                # 时间分桶必须伴随聚合意图(裸度量名兜底可能中途转聚合题);
                # 无聚合 → 软 MISS:不注入分桶列。已在投影里完成的分桶替换保留
                # (按时间分组的列表查询,gen_sql 按 plan 文本补聚合)。
                self._record_soft("time_grain_without_aggregation", tg_grain)
                if not tg_seen:
                    tg_field = None
            elif not tg_seen:
                # 时间字段不在 answer_columns → 分桶表达式插在维度列之后、度量之前
                pos = last_dim_idx + 1 if last_dim_idx is not None else 0
                projections.insert(pos, tg_expr)
                gb_exprs.insert(pos, tg_expr)
                proj_display.insert(pos, tg_grain)
                proj_ref.insert(pos, ("__tg__", tg_grain))

        # ── A① 极值落点(plan.extreme)──────────────────────────────
        # 三条互斥路径(详见 _extreme_target):
        #   投影兜底 / 排序分支(rank=1 + 纯维度投影)/ 选择谓词分支。
        # 排序分支的产物在排序装配处插 order_parts 首位并把 limit 兜成 1;
        # 谓词分支的产物在 having 折叠之后追加进 where_parts(外层语义:
        # "那一行"的极值列 = 子查询选出的极值)。
        ex_target = self._extreme_target(plan)
        extreme_order: str | None = None
        ex_predicate: tuple[str, str, Any, int, str] | None = None
        if ex_target is not None:
            func_e, ds_e, fld_e, rank_e, scope_e = ex_target
            extreme_col = _qualified(ds_e, fld_e.expression)
            if not projections:
                # 投影兜底:计划只给了 extreme(0475 的 min(*)),没有可编译
                # 投影 → 用 FUNC(极值列)作唯一投影,避免整份计划以
                # nothing_compilable 硬 MISS 收场。
                projections.append(f"{func_e.upper()}({extreme_col})")
                proj_display.append(_safe_display(str(fld_e.name)))
                proj_ref.append((ds_e, fld_e))
                is_agg = True
            covered = {
                (ds_e.lower(), str(fld_e.name).lower()),
                (ds_e.lower(), str(fld_e.expression).lower()),
            }
            agg_covered = False
            for p in projections:
                col_p = self._single_agg_column(p, frozenset({func_e}))
                if col_p is not None and (col_p[0].lower(), col_p[1].lower()) in covered:
                    agg_covered = True
                    break
            has_dim = any(
                isinstance(r, tuple) and len(r) == 2
                and r[0] not in ("__tg__", "__expr__")
                for r in proj_ref
            )
            spine_possible = (
                tg_field is not None and self._model.time_spine is not None)
            if agg_covered:
                # 投影已表达极值本身(``FUNC(极值列)``)→ 不做排序/谓词:
                # 叠加 LIMIT 1 会把"每组一条聚合结果"截成一条。
                pass
            elif rank_e >= 2:
                # 第 n 个极值:ORDER BY+LIMIT n 在多组聚合下不可表达,
                # 行级选择谓词(OFFSET n-1)才是无歧义形式。
                ex_predicate = ex_target
            elif not is_agg and has_dim and not spine_possible:
                # 纯维度投影 + rank=1:"取那一行"= 按极值列排序 + LIMIT 1。
                # time_spine 可接管该计划时不走此路(脊柱会自建 ORDER BY,
                # 与之叠加得到的是"最早一期"而非极值行 → 落进谓词分支)。
                extreme_order = f"{extreme_col} {'DESC' if func_e == 'max' else 'ASC'}"
            elif is_agg:
                # 聚合投影但不含极值列(0486 型:count(*) + 第 n 大维度值):
                # 行级选择谓词(rank=1 → OFFSET 0)。
                ex_predicate = ex_target

        filters: list[tuple[str, Any, str, Any]] = []
        for cond in plan.get("conditions") or []:
            if not isinstance(cond, dict):
                self._record_soft("unresolved_filter_field", str(cond))
                continue
            field_ref = str(cond.get("field") or "").strip()
            op = str(cond.get("op") or "=").strip().lower()
            value = cond.get("value")
            resolved = self._resolve_field(field_ref, matched_set)
            if resolved is None:
                self._record_soft("unresolved_filter_field", field_ref)
                continue
            if op not in _COMPILE_OPS:
                self._record_soft("invalid_op", op)
                continue
            if value is None:
                self._record_soft("missing_filter_value", field_ref)
                continue
            if _looks_like_expression_value(value):
                # 算式型值(子查询/聚合式)不是字面量:冻结进骨架会把坏 SQL
                # 权威化(见 _looks_like_expression_value)。跳过该条件,交 gen。
                self._record_soft("expression_filter_value", field_ref)
                continue
            # 枚举字段:值经 enum_display 归一(male/男性 → 'M');无法归一
            # → 软 MISS:跳过该条件,LLM 通道按 plan 文本处理(值语义缺口
            # 是词表问题,不是结构错误——骨架保真校验仍守住其余条件)。
            if resolved[1].enum_display or resolved[1].value_aliases:
                normalized = _normalize_enum_value(
                    value, resolved[1].enum_display, resolved[1].value_aliases)
                if normalized is None:
                    self._record_soft("enum_value_unresolved", field_ref)
                    continue
                value = normalized
            if soft_agg_keys:
                # A3b:该条件与未解析聚合候选的**内部谓词**逐叶对账 → 归聚合
                # 所有,留在 plan 文本交生成侧,不进骨架 WHERE(口径见
                # _soft_agg_cond_keys;键比对只用于"所有权"判定,放宽安全)。
                if _cond_keys_of(
                    f"{_qualified(resolved[0], resolved[1].expression)} "
                    f"{op} {_literal(value)}"
                ) & soft_agg_keys:
                    continue
            filters.append((resolved[0], resolved[1], op, value))

        # 聚合后过滤:having[].metric → HAVING(内联度量表达式);
        # having[].field → 折进 WHERE(行级)。field/metric 必须恰好一个,
        # op/value 与 conditions 同规。
        having_parts: list[str] = []
        # A1d 折叠候选:维度侧单列 AVG/MIN/MAX 的度量级 having —— 是否折成
        # 行级 WHERE 要等联路径解析后才判(需要知道该表在不在「1」端),先按
        # (表, 列, 表达式, 算子, 值) 收集。
        having_folds: list[tuple[str, str, str, str, Any]] = []
        for h in plan.get("having") or []:
            if not isinstance(h, dict):
                self._record_soft("having_metric_unknown", str(h))
                continue
            metric_ref = str(h.get("metric") or "").strip()
            field_ref = str(h.get("field") or "").strip()
            if bool(metric_ref) == bool(field_ref):
                self._record_soft(
                    "having_metric_unknown", f"field={field_ref} metric={metric_ref}")
                continue
            op = str(h.get("op") or "=").strip().lower()
            value = h.get("value")
            if op not in _COMPILE_OPS:
                self._record_soft("invalid_op", op)
                continue
            if value is None:
                self._record_soft("missing_filter_value", field_ref or metric_ref)
                continue
            if _looks_like_expression_value(value):
                self._record_soft("expression_filter_value", field_ref or metric_ref)
                continue
            if metric_ref:
                # 名字精确匹配 → 表达式形态唯一对账(0470 型:计划用
                # ``avg(district.A11)`` 而非度量名引用,旧路径静默丢弃该过滤)。
                metric = (
                    self._metric_by_name(metric_ref)
                    or self._metric_by_expression(metric_ref)
                )
                if metric is None:
                    self._record_soft("having_metric_unknown", metric_ref)
                    continue
                expr = self._inline_metric(metric)
                if isinstance(expr, CompileMiss):
                    return expr  # 派生度量结构坏 → 硬 MISS(度量定义问题)
                fold = self._single_agg_column(metric.expression, _HAVING_FOLD_FUNCS)
                if fold is not None:
                    having_folds.append((fold[0], fold[1], expr, op, value))
                else:
                    having_parts.append(f"{expr} {op.upper()} {_literal(value)}")
                continue
            resolved_h = self._resolve_field(field_ref, matched_set)
            if resolved_h is None:
                self._record_soft("unresolved_filter_field", field_ref)
                continue
            if resolved_h[1].enum_display or resolved_h[1].value_aliases:
                normalized_h = _normalize_enum_value(
                    value, resolved_h[1].enum_display, resolved_h[1].value_aliases)
                if normalized_h is None:
                    self._record_soft("enum_value_unresolved", field_ref)
                    continue
                value = normalized_h
            filters.append((resolved_h[0], resolved_h[1], op, value))
        if having_parts and not is_agg:
            # 度量级 HAVING 只作用于聚合题;列表题挂 HAVING 是退化计划 → 软 MISS
            self._record_soft("having_without_aggregation", "")

        if not projections:
            # 无投影 = 无 SELECT 列(SELECT 恒需要列):计划完全由未解析聚合
            # 构成,骨架 SQL 无从谈起。若唯一成分全是软 MISS → 以**第一个
            # 具体缺口**作为拒绝分因(不笼统返回 nothing_compilable)——
            # refuse/eval 归因到真正缺的 metric/字段。
            if self._soft_misses:
                first = self._soft_misses[0]
                return CompileMiss(first["reason"], first["component"])
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

        # 自愈:计划组件**自己引用**的已声明表必须进 join 集。query_sketch 可能
        # 把表从 plan.tables 漏掉(0483 型:条件/投影/度量锚定仍在引用它),而
        # plan.tables 只是"声明"、组件引用才是"实际需要"——编译器是 join 集的
        # 权威,以引用为准补齐(追加,去重保序:首表与锚选择保持原语义,纯增量)。
        # 补进来的表照样走 resolve 的结构性判定(fan_out / ambiguous / 空路由),
        # 不掩盖真缺口;而 plan.tables 只剩一张表时 resolve 会因 len<2 直接空树,
        # 这里的补齐正是把「引用得到、join 不到」的那一类从 unreachable_table
        # 误伤里救出来。
        needed = self._plan_needed_tables(plan, matched_pairs, matched_set)
        for _t in sorted(needed):
            if _t in declared_datasets and _t not in join_tables:
                join_tables.append(_t)

        # 编译期授权门禁:执行期表 allowlist 前移——越界数据集当场 MISS,
        # 不产出必然被执行守卫拒绝的 SQL(把「执行期事后拒」变成「编译期不产」)。
        if self._allowed_tables is not None:
            for _t in join_tables:
                if not self._dataset_allowed(_t):
                    return CompileMiss("table_not_allowed", _t)

        resolution = None
        joins: list[JoinEdge] = []
        # 权威联表路径(plan.joins):query_sketch 显式声明的路径优先,免 BFS 猜。
        # 每条 join 必须是已声明 relationship(路径选择而非造边);非空但未
        # 声明/不成左深树 → 严格 MISS,不静默忽略后 BFS 改道。
        explicit, joins_present = _explicit_join_edges(plan.get("joins"), self._model)
        resolver = JoinResolver(self._model)
        if joins_present:
            if explicit is None:
                # A5a 表对修复:列名写错(0477)但表对可解析成声明图上的
                # 唯一路径 → 按修复出的路径走同一套显式 channel;修不出来
                # (无路径/多路径/不完整) → 维持今日硬 MISS。
                explicit = self._repair_explicit_joins(plan.get("joins"), needed)
            if explicit is None:
                return CompileMiss(
                    "ambiguous_join_path", "explicit joins reference undeclared edges")
            # A③ needed 感知剪枝:显式通道同样只保留"真正被引用"的子树
            # (BFS 通道一直如此;显式通道此前会把计划误列的共享维度一律联上)。
            # A5a 修复出的边集同样过这道闸。剪枝只删叶子、且锚表连边恒留
            # (见 _prune_explicit_to_needed)—— 显式通道的判定强度
            # (ambiguous_join_path / 基数守卫)与"不回退 BFS"都不变。
            explicit = self._prune_explicit_to_needed(explicit, needed, anchor)
            tree = None
            for cand in _anchor_candidates(anchor, join_tables, explicit):
                tree = _left_deep_tree(explicit, cand)
                if tree is not None:
                    break
            if tree is None:
                return CompileMiss(
                    "ambiguous_join_path", "explicit joins not a connected tree")
            # 基数守卫与 BFS 分支同源:显式路径只替代「路径怎么选」,不豁免边的
            # 物理性质(M:N 行倍增 / many→one 无从判定)。否则 LLM 自己吐 joins
            # 即可绕过行倍增守卫、静默编译出重复计数 SQL。
            cardinality_miss = resolver.guard_edge_cardinality(tree)
            if cardinality_miss is not None:
                return CompileMiss(cardinality_miss, ", ".join(matched))
            joins = tree
        else:
            # 查询实际需要的表 = 组件引用的表(非 query_sketch 全集):决定联表树
            # 保留与歧义作用域,避免误列的共享维度(district)触发虚假二义
            # 或被多余联入。needed 已在 join 集自愈处算好(同一纯函数,同一入参)。
            resolution = resolver.resolve(
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

        # A① 可达性闸:极值列所在的表必须在**最终连接树**里(needed 补收已
        # 尽力),否则排序片段/选择谓词会引用 FROM 里不存在的表(静默非法
        # SQL)。不可达 → 放弃极值消费(记 log),其余编译照常。
        if ex_target is not None and ex_target[1].lower() not in (
            {anchor.lower()}
            | {e.from_.lower() for e in joins}
            | {e.to.lower() for e in joins}
        ):
            logger.debug(
                "semantic-layer: extreme dropped, table %s outside join tree",
                ex_target[1],
            )
            extreme_order = None
            ex_predicate = None

        # A1.1 扇出提升:1:N 联路径把「1」端的行按多端重复,声明的
        # COUNT(t.pk) 直接内联会得到行对数(0470:2645 vs 77)→ PK 上补
        # DISTINCT 拉回声明的行计数语义。proj_ref 索引不变(dim/metric 引用
        # 保持等长),分析包装/脊柱照常。
        projections = self._promote_fanout_counts(projections, joins)
        one_end = {e.to.lower() for e in joins if not _is_many_to_many(e.cardinality)}

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

        # A1d 维度侧 having 折叠:无 GROUP BY(单组聚合)且列所在表在联路径
        # 「1」端时,AVG/MIN/MAX 作用在维度实体行上 = 列值本身 —— HAVING 的
        # 加权聚合在单组查询里只能给整条结果开闸(恒真/恒假),行级 WHERE 才是
        # 实体级过滤(0470:HAVING AVG(district.A11) → WHERE district.A11)。
        # 其余形态一律保持 HAVING:有 GROUP BY(分组后每组的 AVG ≠ 行值)、
        # 事实侧列(多端重复的值,行级过滤语义不同)、SUM/COUNT(跨行聚集)。
        for tbl, col, expr, h_op, h_value in having_folds:
            if not gb_exprs and tbl.lower() in one_end:
                where_parts.append(
                    f"{_qualified(tbl, col)} {h_op.upper()} {_literal(h_value)}")
            else:
                having_parts.append(f"{expr} {h_op.upper()} {_literal(h_value)}")

        # A① 极值选择谓词(rank ≥ 2,或聚合投影不含极值列):外层 WHERE 限定
        # "极值那一行"的极值列 = 子查询选出的第 rank 个极值。放在 here
        # (having 折叠之后、RLS 之前):条件集已定型,复制进子查询的同表
        # 条件才是计划声明的过滤集。
        if ex_predicate is not None:
            extreme_pred = self._extreme_selection_predicate(
                *ex_predicate, filters)
            if extreme_pred:
                where_parts.append(extreme_pred)

        # 声明层行级安全(RLS):数据集 row_filter 注入顶层 WHERE。数据集 JOIN
        # 均为内连接(仅时间轴用 LEFT JOIN,作用在派生表上),顶层过滤与联前
        # 过滤等价;放进顶层还让交接契约覆盖它——gen_sql 丢弃该谓词会被
        # compiled_sql_matches 保真校验拦下,不会静默丢一个安全条件。
        rls_tables: list[str] = [anchor]
        for edge in joins:
            for _t in (edge.from_, edge.to):
                if _t not in rls_tables:
                    rls_tables.append(_t)
        row_filters: list[tuple[tuple[tuple[str, str], ...], str, tuple[str, ...]]] = []
        for _t in rls_tables:
            rf = self._row_filter_sql(_t)
            if rf:
                where_parts.append(rf)
                # 契约里单列 RLS 谓词(A3c/advisory 下仍**恒查**:声明层授权
                # 不因计划降级而放水,见 skeleton_preserved)。
                row_filters.extend(_predicate_conds(rf))

        # M:N dedup 豁免:fan_out="dedup" 的 from 侧包 SELECT DISTINCT * 子查询
        # (关联/桥接表),消除行倍增——编译期确定性,不靠规则链事后兜底。
        dedup_tables = {e.from_ for e in joins if e.fan_out == "dedup"}

        def _src(table: str) -> str:
            if table in dedup_tables:
                return f"(SELECT DISTINCT * FROM {table}) AS {table}"
            return table

        sql = "SELECT " + ", ".join(projections) + f"\nFROM {_src(anchor)}"
        joined = {anchor}
        for edge in joins:
            # BFS 树序:每条边连接一个已入表与一个新表 → JOIN 目标 = 未入者
            new_t = edge.to if edge.from_ in joined else edge.from_
            if new_t in joined:
                continue  # 防御:理论上不触发
            clause = f"{edge.from_}.{edge.from_column} = {edge.to}.{edge.to_column}"
            sql += f"\nJOIN {_src(new_t)} ON {clause}"
            joined.add(new_t)
        if where_parts:
            sql += "\nWHERE " + " AND ".join(where_parts)
        if is_agg and gb_exprs:
            sql += "\nGROUP BY " + ", ".join(gb_exprs)
        if having_parts:
            sql += "\nHAVING " + " AND ".join(having_parts)
        # 排序(呈现层,宽处理):metric 名 → 内联表达式;answer_columns 的
        # 聚合候选(已对账度量,按表达式归一文本对账)→ 内联表达式;字段 →
        # 限定表达式(时间分桶字段用分桶表达式);不可解析列丢弃而非 MISS——
        # 排序不产生新拒绝向量,gen_sql 仍会收到 plan 文本里的 ordering 线索。
        agg_by_norm = {_norm_expr_text(c): m for c, m in by_candidate.items()}
        order_parts: list[str] = []
        if extreme_order is not None:
            # A① rank=1 的"最高/最低那一行":排序首位 + LIMIT 1(limit 装配
            # 处兜底,见下)。计划显式给了 limit 时保留其值(取前 N 名)。
            order_parts.append(extreme_order)
        for column, direction in (parse_ordering(plan.get("ordering")) or []):
            metric_o = (
                self._metric_by_name(column)
                or agg_by_norm.get(_norm_expr_text(column))
            )
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
        if limit is None and extreme_order is not None:
            # A① 排序分支:"最高的那一行" = 排序首位 + LIMIT 1;计划显式
            # 给了 limit 时不覆盖(取前 N 名)。
            limit = 1
        # 时间轴空档补全(time_spine):模型声明 + 时间分桶 + 可从条件推导
        # 时间范围 → 包一层 spine LEFT JOIN 填充缺期。analysis 存在时不
        # 叠加(窗口包装优先);范围不可推/无 spine → 常规路径(无填充)。
        spine_applied = False
        if (
            analysis is None
            and tg_field is not None
            and self._model.time_spine is not None
        ):
            spine_sql = self._apply_spine(
                sql, plan, tg_field, tg_expr, proj_display, proj_ref, limit)
            if isinstance(spine_sql, str):
                sql = spine_sql
                spine_applied = True
        if analysis:
            wrapped = self._apply_analysis(
                sql, plan, matched_set, analysis, limit,
                projections, proj_display, proj_ref, tg_field, order_parts,
            )
            if isinstance(wrapped, CompileMiss):
                # 分析意图无法解析 → 软 MISS:回退内层聚合 SQL,分析意图仍保留
                # 在 plan 文本交给 gen_sql 通道,不整体拒绝。
                self._record_soft(wrapped.reason, wrapped.component)
            else:
                sql = wrapped
        elif not spine_applied:
            if order_parts:
                sql += "\nORDER BY " + ", ".join(order_parts)
            if limit is not None:
                if not order_parts:
                    return CompileMiss("limit_without_order", str(limit))
                sql += f"\nLIMIT {int(limit)}"

        # Phase A0:交接对象在这里成型 —— 结构**此刻**抽取一次(编译器刚拼出的
        # SQL 必然可解析),随契约带下去;执行前的校验改读它,不再从字符串反推。
        # 提示散文不单独存:消费方一律经 render_contract(contract) 取,信息
        # 只减不增,也就不存在"块与契约不一致"的状态。
        if self._soft_misses:
            # 软 MISS 已收集但仍有可编译成分 → 骨架(PartialCompile):
            # 可解析部分权威化,未解析组件留给生成通道,不再整体拒绝。
            # A3c:Aggr 意图未落地 / analysis 未解析这两类**计划级**缺口下,
            # 骨架的 WHERE 只作参考(advisory)——生成侧有权按 plan 文本重写;
            # 外围缺口不降级(见 _is_advisory_plan)。
            miss_parts = list(self._soft_misses)
            return PartialCompile(
                sql=sql,
                contract=_build_contract(
                    sql, dialect=self._dialect, partial=True, gaps=miss_parts,
                    advisory=self._is_advisory_plan(agg_declared, is_agg),
                    row_filters=tuple(row_filters)),
                miss_parts=miss_parts,
            )
        return CompileResult(
            sql=sql,
            contract=_build_contract(
                sql, dialect=self._dialect, row_filters=tuple(row_filters)),
        )

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
            if m is None:
                # 表达式形态引用(0483/0495 型:计划复述内层聚合表达式而非度量
                # 名)—— 唯一签名对账命中才认;认不出就照旧 analysis_metric_unknown
                # (不做"回落到内层度量"的兜底:那会把打错的引用静默洗白)。
                m = self._metric_by_expression(target_metric)
            if m is None or m.name != proj_ref[m_idx].name:
                return CompileMiss("analysis_metric_unknown", target_metric)
        if atype == "share" and not [
            i for i in range(len(proj_ref)) if i != m_idx
        ]:
            # 退化守卫:内层除目标度量外**没有任何其它投影**时,share 的窗口
            # SUM OVER () 恒等于该行自身 → 恒 1.0(0495:单组 COUNT 包 share)
            # —— 无信息窗口不是正确包装。记软 MISS 回退内层聚合,交生成通道
            # 按 plan 文本处理。A② 表达式列/时间分桶列都算"其它投影"(窗口
            # 有信息量),判据从"投影数 == 1"改成"除度量外空"是同义的显式化。
            return CompileMiss(
                "analysis_invalid", "share without dimension projection")

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

    # ── 时间轴空档补全(time_spine)─────────────────────────────
    #
    # 模型声明 time_spine + 时间分桶 + 可从过滤条件推导时间范围时,把聚合
    # 结果 LEFT JOIN 到按 grain 生成的密集周期表(spine),缺期按 fill 策略
    # 补全(0 / previous / none)。任何前置缺失 → None(回退常规路径,无填充)。
    # spine 的 period 与内层分桶表达式同源(同一 date_trunc 套在生成日期上),
    # 保证 ``t._period = spine.period`` 跨方言匹配。

    @staticmethod
    def _parse_date_span(value: Any) -> tuple[str, str] | None:
        """把条件值解析成日期跨度(lo, hi);识别 ISO 日期 / YYYY / YYYY-MM。"""
        from datetime import date as _date, timedelta

        v = str(value or "").strip()
        if not v:
            return None
        try:
            d = _date.fromisoformat(v)
            return d.isoformat(), d.isoformat()
        except ValueError:
            pass
        if re.fullmatch(r"\d{4}", v):
            y = int(v)
            return f"{y:04d}-01-01", f"{y:04d}-12-31"
        if re.fullmatch(r"\d{4}-\d{2}", v):
            y, m = int(v[:4]), int(v[5:7])
            if not 1 <= m <= 12:
                return None
            lo = _date(y, m, 1)
            nxt = _date(y + 1, 1, 1) if m == 12 else _date(y, m + 1, 1)
            hi = nxt - timedelta(days=1)
            return lo.isoformat(), hi.isoformat()
        return None

    def _spine_bounds(
        self, plan: dict[str, Any], tg_field: tuple[str, Any],
    ) -> tuple[str, str] | None:
        """从时间过滤条件推导 spine 范围;缺任一边界 → None(不填充)。"""
        from datetime import date as _date

        lo: str | None = None
        hi: str | None = None
        for cond in plan.get("conditions") or []:
            if not isinstance(cond, dict):
                continue
            resolved = self._resolve_field(
                str(cond.get("field") or "").strip(), self._matched_set)
            if resolved is None or resolved != tg_field:
                continue
            span = self._parse_date_span(cond.get("value"))
            if span is None:
                continue
            s_lo, s_hi = span
            op = str(cond.get("op") or "=").strip().lower()
            if op in ("=", "==", "in", "between"):
                lo = s_lo if lo is None else min(lo, s_lo)
                hi = s_hi if hi is None else max(hi, s_hi)
            elif op in (">=", ">"):
                lo = s_lo if lo is None else max(lo, s_lo)
            elif op in ("<=", "<"):
                hi = s_hi if hi is None else min(hi, s_hi)
        if lo is None or hi is None:
            return None
        try:
            if _date.fromisoformat(hi) < _date.fromisoformat(lo):
                return None
        except ValueError:
            return None
        return lo, hi

    def _apply_spine(
        self,
        inner_sql: str,
        plan: dict[str, Any],
        tg_field: tuple[str, Any],
        tg_expr: str,
        proj_display: list[str],
        proj_ref: list[Any],
        limit: Any,
    ) -> str | None:
        """把聚合内层包成 spine LEFT JOIN;不适用 → None(常规路径)。"""
        spine = self._model.time_spine
        if spine is None:
            return None
        # 时间分桶投影列索引(须存在才能给内层加 _period 别名)
        time_idx = next(
            (i for i, r in enumerate(proj_ref)
             if isinstance(r, tuple) and r and r[0] == "__tg__"),
            None,
        )
        if time_idx is None:
            return None
        bounds = self._spine_bounds(plan, tg_field)
        if bounds is None:
            return None
        lo, hi = bounds
        dialect = self._dialect or "sqlite"
        periods = time_spine_periods(lo, hi, spine.granularity, dialect)
        if periods is None:
            return None

        from sqlglot import exp, parse_one

        try:
            inner = parse_one(inner_sql, read=dialect)
        except Exception:
            return None
        if not isinstance(inner, exp.Select):
            return None
        new_projs = []
        for j, p in enumerate(inner.expressions):
            new_projs.append(
                exp.alias_(p.this if isinstance(p, exp.Alias) else p, f"_c{j}"))
        inner.set("expressions", new_projs)
        inner_text = inner.sql(dialect=dialect)

        fill = spine.fill
        outer_cols: list[str] = []
        for j, pd in enumerate(proj_display):
            if j == time_idx:
                outer_cols.append(f"spine.period AS {pd}")
                continue
            if isinstance(proj_ref[j], SemanticMetric):
                outer_cols.append(
                    f"{spine_fill_expr(f't._c{j}', fill)} AS {pd}")
            else:
                outer_cols.append(f"t._c{j} AS {pd}")
        outer = (
            "SELECT " + ", ".join(outer_cols)
            + f"\nFROM (\n{periods}\n) AS spine"
            + f"\nLEFT JOIN (\n{inner_text}\n) AS t ON t._c{time_idx} = spine.period"
            + "\nORDER BY spine.period"
        )
        if limit is not None:
            outer += f"\nLIMIT {int(limit)}"
        return outer


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
    # 派生表/子查询别名也算合法列限定符(spine/窗口外层 ``t.`` / ``spine.``)
    for node in tree.find_all(exp.Subquery):
        parent = node.parent
        if parent is not None and parent.key in ("from", "join") and node.alias:
            from_tables.add(str(node.alias).lower())
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


def _inner_select(tree):
    """Query → 顶层 SELECT(带 CTE 的取主查询);非查询 → None。"""
    if not isinstance(tree, exp.Query):
        return None
    if isinstance(tree, exp.With):
        tree = tree.this
    return tree if isinstance(tree, exp.Select) else None


def _signature_of(tree) -> PlanSignature | None:
    """已解析的语法树 → 结果形状签名(判据见 :class:`PlanSignature`)。

    这是**生成 SQL 侧**的抽取入口 —— 生成 SQL 是 LLM 输出,只拿得到字符串。
    编译侧走 ``_build_contract`` 在编译期抽一次,不再有这个函数参与。

    投影/过滤的列身份也进签名(``SUM(a)`` → ``SUM(b)``、``WHERE region='A'``
    → ``WHERE status='A'`` 会静默换结果,不能算"保真")。列按 (表, 列) 小写、
    去重排序成规范形,比较时由 ``PlanSignature.matches`` 宽容裸列/限定列差异。
    """
    select = _inner_select(tree)
    if select is None:
        return None

    projections: list[tuple[str | None, tuple[tuple[str, str], ...]]] = []
    for e in select.expressions or []:
        agg = next((f for f in e.find_all(exp.AggFunc)), None)
        if agg is not None:
            fn = agg.sql().split("(", 1)[0].strip().lower()
            cols = tuple(sorted({
                (str(c.table or "").lower(), str(c.name).lower())
                for c in agg.find_all(exp.Column) if c.name
            }))
            projections.append((fn, cols))
        else:
            # 无聚合的投影(维度/表达式):列身份同样是"改了就必然改变结果"
            # 的信号 —— ``SELECT region`` vs ``SELECT country`` 不该放行。
            cols = tuple(sorted({
                (str(c.table or "").lower(), str(c.name).lower())
                for c in e.find_all(exp.Column) if c.name
            }))
            projections.append((None, cols))

    tables: set[str] = set()
    src_nodes = [select.args.get("from_")] + (select.args.get("joins") or [])
    for s in src_nodes:
        if s is None:
            continue
        t = s.this if isinstance(s.this, exp.Table) else s.find(exp.Table)
        if t is not None:
            tables.add(t.name.lower())

    conds: list[tuple[tuple[tuple[str, str], ...], str, tuple[str, ...]]] = []
    where = select.args.get("where")
    if where is not None:
        for node in where.walk():
            if not isinstance(node, exp.Binary):
                continue
            cols = tuple(sorted({
                (str(c.table or "").lower(), str(c.name).lower())
                for c in node.find_all(exp.Column) if c.name
            }))
            vals = tuple(
                str(ln.this) for ln in node.expression.find_all(exp.Literal)
            )
            conds.append((cols, node.key, vals))

    group = select.args.get("group")
    return PlanSignature(
        projections=tuple(projections),
        tables=tuple(sorted(tables)),
        conds=tuple(conds),
        joins=len(select.args.get("joins") or []),
        groups=len(group.expressions or []) if group is not None else 0,
    )


def _generated_signature(sql: str, dialect: str) -> PlanSignature | None:
    """生成 SQL(字符串)→ 签名。解析失败/非查询 → None(调用方判失败)。

    注意这里**不做 transpile 归一**:签名每一项都在 parse 期定形,不随 read
    方言变化(实测 ``DATE_FORMAT``/``date_trunc``/``strftime`` 同签名)。契约
    与生成 SQL 同属一个业务方言 —— 那是 A1 的结构保证,不再靠运行时把两侧
    都拎到 sqlite 对齐。
    """
    try:
        tree = parse_one(sql, read=dialect)
    except Exception:
        return None
    return _signature_of(tree)


# ── 契约构造 + 保真校验 ────────────────────────────────────────
#
# 编译器 → 生成通道的交接与守卫。两条校验都读 :class:`PlanContract`,
# **不再从编译 SQL 反推结构**:
#
# - 全量编译(``compiled_sql_matches``):比结果形状签名 —— 改聚合/改过滤值/改
#   投影宽度/换表/改投影或过滤列身份 → 打回;别名、格式、``COUNT(*)`` vs
#   ``COUNT(col)``、裸列 vs 限定列 → 通过。
# - 软 MISS(``skeleton_preserved``):比 join 边/WHERE 条件/分组宽度 ——
#   LLM 只被允许**补**未覆盖组件,不得改/删权威骨架。投影不比较(要补缺),
#   分桶表达式差异不比较(GROUP BY 只比宽度)。
#
# 两侧的差别在**谁解析**:编译侧的结构在编译期就抽进契约(编译器刚拼出的
# SQL 必然可解析),执行前只剩生成 SQL 需要解析 —— 它是 LLM 输出,只有字符串。


def _canon_edge(edge: set) -> JoinEdge:
    """无序边(``frozenset`` / ``set`` 的 ``{(表,列), (表,列)}``)→ 规范有序元组。

    集合语义不变,只是把迭代顺序固定下来:渲染与 golden 断言才能逐字可复现。
    单元素集合(自连接 ``a.x = a.x``,抽取器长度判定下真会出现)写成两端相同,
    与 ``frozenset({c})`` 等价。
    """
    cols = sorted(edge)
    if len(cols) == 1:
        cols = [cols[0], cols[0]]
    return (cols[0], cols[1])


def _build_contract(
    sql: str,
    *,
    dialect: str = "sqlite",
    partial: bool = False,
    gaps: list[dict[str, str]] | None = None,
    advisory: bool = False,
    row_filters: tuple[WhereCond, ...] = (),
) -> PlanContract:
    """权威 SQL → 契约。结构**此刻**抽取(编译器刚拼出的 SQL 必然可解析)。

    解析失败/非 SELECT 时返回只带 ``skeleton_sql`` 的空结构契约 —— 那是
    **编译器自身有缺陷**(拼出的 SQL 解析不了),不该让下游静默降级:这里
    当场告警,契约上的 ``signature is None`` 则是留给校验侧的独立信号
    (``compiled_sql_matches`` 见到它就记录并放行,而不是伪装成"形状相同")。

    ``advisory``/``row_filters`` 是骨架校验的降级声明(接口Ⅲ):
    advisory 表示"计划本身是退化/未覆盖形态,WHERE 只可参考不可冻结";
    row_filters 是编译器注入的声明层 RLS 谓词 —— 它的校验**不随 advisory
    降级**(安全条件没有"参考"一说,丢了就是丢了)。
    """
    base = PlanContract(
        skeleton_sql=sql, gaps=tuple(gaps or ()), partial=partial,
        advisory=advisory, row_filters=tuple(row_filters),
    )
    try:
        tree = parse_one(sql, read=dialect or "sqlite")
    except Exception as e:
        logger.warning("compiled SQL is unparseable (%s): %r", e, sql[:400])
        return base
    select = _inner_select(tree)
    if select is None:
        logger.warning("compiled SQL is not a SELECT: %r", sql[:400])
        return base
    group = select.args.get("group")
    edges: list[JoinEdge] = sorted({_canon_edge(e) for e in _skeleton_join_edges(select)})
    wheres: list[WhereCond] = sorted(
        (tuple(sorted(cols)), op, vals)
        for cols, op, vals in _skeleton_where(select)
    )
    return replace(
        base,
        join_edges=tuple(edges),
        where=tuple(wheres),
        group_by_width=len(group.expressions or []) if group is not None else 0,
        signature=_signature_of(select),
    )


def build_contract(sql: str, dialect: str = "sqlite") -> PlanContract | None:
    """编译 SQL 字符串 → 契约。**只在契约缺席时用**(旧 checkpoint、wire 形状
    异常):此时结构必须现抽,否则校验无据可依。空 SQL → ``None``(没有权威
    SQL 就谈不上保真,调用方整体跳过 —— 与改造前 ``if not compiled`` 一致)。

    与 ``contract_from_wire`` 的分工:那条是**恢复**(字符串→对象,形状异常
    即 ``None``,绝不部分解出);这条是**重建**(从权威 SQL 现抽,抽不出结构
    时 ``signature is None`` 是明确信号)。两条路都通向同一个对象。
    """
    if not sql:
        return None
    return _build_contract(sql, dialect=dialect or "sqlite")


def _skeleton_join_edges(tree) -> set[frozenset[tuple[str, str]]]:
    """JOIN 边集合:每条边 = {((表,列), (表,列))}(无序对,方向无关)。"""
    from sqlglot import exp

    out: set[frozenset[tuple[str, str]]] = set()
    for j in tree.args.get("joins") or []:
        on = j.args.get("on")
        if on is None:
            continue
        for node in on.walk():
            if not isinstance(node, exp.EQ):
                continue
            sides = []
            for side in (node.this, node.expression):
                if isinstance(side, exp.Column):
                    sides.append((
                        str(side.table or "").lower(),
                        str(side.name).lower(),
                    ))
            if len(sides) == 2:
                out.add(frozenset(sides))
    return out


def _conds_of(node) -> set[tuple[frozenset[tuple[str, str]], str, tuple[str, ...]]]:
    """谓词子树 → 条件集合:(列集, 操作符, 字面量值元组)。

    ``and`` 复合节点**跳过**(A3a):合取的可满足性由叶子逐个隐含,把复合节点
    也收进来只会产出「列集=叶子并集、算子=and」的伪条件 —— 生成侧重排条件
    顺序(``A AND B`` → ``B AND A``)就会让两个复合伪条件对不上,骨架保真
    校验把语义等价的改写误判成「丢了过滤条件」。``or`` 保留:析取是一个整体
    逻辑条件,叶子不分别成立,必须整块比对。
    """
    from sqlglot import exp

    out: set[tuple[frozenset[tuple[str, str]], str, tuple[str, ...]]] = set()
    for n in node.walk():
        if not isinstance(n, exp.Binary) or isinstance(n, exp.And):
            continue
        cols = frozenset(
            (str(c.table or "").lower(), str(c.name).lower())
            for c in n.find_all(exp.Column)
        )
        vals = tuple(sorted(
            str(ln.this) for ln in n.expression.find_all(exp.Literal)))
        out.add((cols, str(n.key), vals))
    return out


def _skeleton_where(tree) -> set[tuple[frozenset[tuple[str, str]], str, tuple[str, ...]]]:
    """WHERE 条件集合:(列集, 操作符, 字面量值元组);``and`` 复合节点跳过。"""
    where = tree.args.get("where")
    if where is None:
        return set()
    return _conds_of(where)


def _predicate_conds(text: str) -> list[tuple[tuple[tuple[str, str], ...], str, tuple[str, ...]]]:
    """单条谓词文本 → 契约形状的条件元组列表(解析失败 → 空)。

    与 ``_skeleton_where`` 同口径(同一 ``_conds_of``),只是入口是文本而非
    子树 —— 契约的 ``row_filters`` 从编译器**注入的谓词字符串**直接抽取,
    而不是解析整条 SQL 后再按表反查(后者会把普通 WHERE 也混进来)。
    """
    s = str(text or "").strip()
    if not s:
        return []
    try:
        tree = parse_one(s)
    except Exception:
        return []
    if tree is None:
        return []
    return sorted(
        (tuple(sorted(cols)), op, vals)
        for cols, op, vals in _conds_of(tree)
    )


def _skeleton_col_in(skel_col: tuple[str, str], gen_cols) -> bool:
    """骨架列是否被生成条件列覆盖:表名一致,或任一侧未限定(按列名匹配)。"""
    st, sc = skel_col
    for gt, gc in gen_cols:
        if gc == sc and (gt == st or not gt or not st):
            return True
    return False


def skeleton_preserved(
    contract: PlanContract,
    generated: str,
    generated_dialect: str = "sqlite",
) -> tuple[bool, str]:
    """Partial 骨架保真校验:join 边/WHERE 条件/分组宽度必须保留。

    骨架侧读契约字段(**编译期已抽好**),只有生成 SQL 需要解析 —— 它是
    LLM 输出。与 ``compiled_sql_matches`` 的差别:投影列不参与比较(LLM 需
    补未解析组件),join 顺序、分桶表达式方言差异被容忍。

    ``contract.advisory``(A3c):计划本身是退化形态(声明了聚合却没命中任何
    度量 / 分析组件未解析)时,编译器拼出的 WHERE 只反映"计划文本恰好可解析
    的部分",把它冻成权威会把生成侧的正确改写打回 —— 语义缺口是计划的问题,
    不是生成的问题。advisory 下**仅跳过** WHERE 子集检查;join ⊇ 与分组宽度
    照旧(结构骨架仍权威)。``contract.row_filters``(RLS)不受 advisory 影响:
    安全谓词是恒校验的硬底线。

    返回 ``(False, 原因)`` 的两种情况都是**打回重生成**:
      - 生成 SQL 解析不了(LLM 输出了非 SQL / 方言不符)——它连解析都不行,
        更谈不上保真;
      - 生成 SQL 丢了骨架的 join 边 / 过滤条件 / 分组维度。
    """
    if not contract.skeleton_sql or not generated:
        return True, ""
    if (
        not contract.join_edges and not contract.where
        and not contract.group_by_width and not contract.row_filters
    ):
        # 编译期就没抽出结构(编译器自己拼的 SQL 解析不了,见 _build_contract
        # 的告警):此处的"全保留"是空转。不伪装成通过,显式记录后放行 ——
        # 拒绝会让这种配置下**所有**问题都失败,那是惩罚用户,不是收口。
        logger.warning(
            "skeleton check skipped: contract carries no extracted structure "
            "(compile-time extraction failed)",
        )
        return True, "contract structure unavailable"

    try:
        gtree = parse_one(generated, read=generated_dialect or "sqlite")
    except Exception as e:
        return False, f"generated SQL is unparseable: {e}"

    g = _inner_select(gtree)
    if g is None:
        return False, "generated SQL is not a SELECT"

    gen_joins = _skeleton_join_edges(g)
    if not {frozenset(e) for e in contract.join_edges} <= gen_joins:
        return False, "generated SQL dropped a skeleton join"

    gen_where = _skeleton_where(g)

    def _missing(conds) -> bool:
        for cols, op, vals in conds:
            if not any(
                gop == op and gvals == vals
                and all(_skeleton_col_in(c, gcols) for c in cols)
                for gcols, gop, gvals in gen_where
            ):
                return True
        return False

    if not contract.advisory and _missing(contract.where):
        return False, "generated SQL dropped a skeleton filter condition"

    # RLS 谓词恒校验:advisory 只对"计划文本恰好解析出的普通条件"降级,声明层
    # 行级安全是编译器注入的硬谓词,丢了不应静默(见 compiler 的 RLS 注释)。
    if contract.row_filters and _missing(contract.row_filters):
        return False, "generated SQL dropped a declared row filter (RLS)"

    group = g.args.get("group")
    gen_groups = len(group.expressions or []) if group is not None else 0
    if gen_groups < contract.group_by_width:
        return False, "generated SQL dropped a grouping dimension from the skeleton"

    return True, ""


def compiled_sql_matches(
    contract: PlanContract,
    generated: str,
    generated_dialect: str = "sqlite",
) -> tuple[bool, str]:
    """编译照抄校验:生成的 SQL 是否保留权威编译 SQL 的结果形状。

    比的是契约里**编译期抽好的**结果形状签名(见 :class:`PlanSignature`):
    改聚合/改过滤值/改投影宽度/换表/改投影或过滤**列身份** → 打回;
    别名、格式、大小写、``COUNT(*)`` vs ``COUNT(col)``、裸列 vs 限定列
    (与编译 SQL 语义等价,不是回归)。

    生成 SQL 解析不了 → **打回**(它连是不是查询都定不下来,不能算保真)。
    这是 A1 收口的 fail-open:此前这种情况静默放行。

    唯一仍放行的情况:契约没有签名 —— 那是编译期就抽不出结构(编译器自身
    缺陷,``_build_contract`` 已当场告警),拒绝会让这种配置下所有问题都失败。
    """
    if not contract.skeleton_sql or not generated:
        return True, ""
    if contract.signature is None:
        logger.warning(
            "copy check skipped: contract has no shape signature "
            "(compile-time extraction failed)",
        )
        return True, ""
    dialect = (generated_dialect or "sqlite").lower()
    other = _generated_signature(generated, dialect)
    if other is None:
        return False, (
            "generated SQL could not be parsed as a query — it cannot be "
            f"verified against the compiled SQL (dialect: {dialect})"
        )
    if contract.signature.matches(other):
        return True, ""
    return False, (
        "generated SQL does not preserve the compiled SQL's result shape "
        "(changed aggregation, filter columns/values, projection, or joins)"
    )
