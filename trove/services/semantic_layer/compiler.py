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

from trove.services.semantic_layer.models import SemanticModel

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

    clauses: authoritative ON-clause strings (``loan.account_id = account.account_id``).
    extra_tables: intermediate tables used by the join tree but not named
        in the matched set — schema blocks for them must be published too.
    """

    clauses: list[str] = field(default_factory=list)
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
    ) -> JoinResolution:
        """ON 子句 + 中间表集合(声明优先,样本验证命名边回退,锚表 BFS)。

        边图 = 全量声明关系 ∪ 已验证命名边;从 anchor 表 BFS 连接所有
        matched 表,路由经过中间表(不在 matched 里的联表)也算——这正是
        ''question 只点名 loan+district、实际要经 account 联''的场景。
        """
        matched = list(tables or [])
        if len(matched) < 2:
            return JoinResolution()
        matched_set = set(matched)

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

        root = matched[0]
        visited = {root}
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

        clauses = sorted(
            f"{e.from_}.{e.from_column} = {e.to}.{e.to_column}"
            for e in tree
        )
        extra = sorted(visited - matched_set)
        return JoinResolution(clauses=clauses, extra_tables=extra)

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