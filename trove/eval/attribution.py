"""回顾归因 — 历史失败题 vs 声明层(零 LLM;除注入的探测结果外零查询)。

只回答一个问题:**这些失败里,有多少能被「声明层」解释?**

历史失败题自带 gold SQL —— 也就是「这题本来该怎么走」;语义模型给出
「允许怎么走、前提是什么」。两边都是确定性的结构,归因就是一次集合比对
加一次前提查表:

1. **需要走的路径**(gold)是否在声明内:表 → 数据集、连接等式 → 声明的边
   (列级匹配);两表各自按已声明边连到**同一个父数据集**且列同名时,算
   「组合路径」(它等价于经父表连接,前提是父键唯一);
2. **前提真假**:声明过的边/键由 ``verify_declarations`` 的对库探测给出
   四态,只有 ``violated`` 才把失败归因到声明层;
3. **实际走过的路径**(pred)是否越出声明 —— gen 自己发明的连接、或走了
   编译期本会拒绝的边,都是观察量,单独计数。

六个桶(闭集、互斥,按 ``BUCKETS`` 顺序判定,先命中的先算 —— 与
``workflow/rules.py`` 同款纪律):

- ``premise_false_walked`` 假前提·实际走过 —— 走了一条已被证伪的声明;
- ``premise_false_needed`` 假前提·需要走 —— gold 需要的前提已被证伪;
- ``path_blocked`` 声明拦路 —— 需要的边是 M:N 且没有生效的 fan_out 豁免
  (``bridge:`` 保留未实现,同样走不通),正确答案在这份模型下不可达;
- ``path_undeclared`` 未声明·需要走 —— 表不是数据集,或连接对既非声明边
  也非组合路径;
- ``unattributable`` 判不了 —— gold 解析不了 / 连接解析不到基表 / 声明过
  的前提本次没实测(**判不了 ≠ 通过**,与 verify 同一条纪律);
- ``premise_true`` 前提为真 —— 声明层解释不了这题,原因在别处。

**诚实边界**(为什么这份报告不是因果结论):

- 归因对象是**手头这份 KB**。失败记录的 KB 版本与之不同时,这是反事实
  诊断(「今天的声明遇到这些题会怎样」),不是当时的原因 —— ``meta`` 里
  带 KB 指纹与 ``kb_changed_since``,读的人自己知道该怎么读;
- 「前提为真」不等于「这题没问题」,它说的是**声明层没有解释力**,原因在
  生成/检索/口径侧。负结果同样是结果;
- 探测是快照,数据变了结论就变;
- 组合路径的前提只在**父数据集声明了键**时可判;没声明时它是**覆盖缺口**
  (单独计数),不把整题打成「判不了」—— 没有声明的东西不存在可证伪的
  声明,这是 verify 模块自己的口径。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import sqlglot
from sqlglot import exp

from trove.services.semantic_layer.models import SemanticModel
from trove.services.semantic_layer.verify import (
    ADVISORY,
    UNVERIFIABLE,
    VERIFIED,
    VIOLATED,
    Probe,
)

#: 归因桶 —— 闭集,顺序 = 判定优先级(先命中的先算)
BUCKETS = (
    "premise_false_walked",
    "premise_false_needed",
    "path_blocked",
    "path_undeclared",
    "unattributable",
    "premise_true",
)

BUCKET_LABELS = {
    "premise_false_walked": "假前提·实际走过",
    "premise_false_needed": "假前提·需要走",
    "path_blocked": "声明拦路(连接被拒)",
    "path_undeclared": "未声明·需要走",
    "unattributable": "判不了",
    "premise_true": "前提为真(声明层解释不了)",
}

#: 前提结论的汇总优先级:证伪 > 判不了 > advisory > 通过。
#: 一条边可能有多条探测(1:1 双侧、dedup),混着的时候按这个取最重的一档 ——
#: 把「一侧已证伪、另一侧通过」折成「通过」是这份报告最不能出的错。
_VERDICT_RANK = {VIOLATED: 3, UNVERIFIABLE: 2, ADVISORY: 1, VERIFIED: 0}

#: 前提结论的人话标签(报告里的 ``边[已被证伪]`` 就是这里来的)
VERDICT_LABELS = {
    VERIFIED: "实测成立",
    VIOLATED: "已被证伪",
    ADVISORY: "成立(有提示)",
    UNVERIFIABLE: "判不了",
    "": "未实测",
}


# ── 路径解析(纯 sqlglot)─────────────────────────────────


@dataclass(frozen=True)
class JoinRef:
    """一条连接等式,已归一到基表名(小写)。

    ``resolved=False`` = 两侧至少一边解析不到基表(派生表/CTE 别名、无限定
    列名、``USING``)—— 不猜,记成解析不到。
    """

    left: str
    left_col: str
    right: str
    right_col: str
    resolved: bool = True

    def label(self) -> str:
        if not self.resolved:
            return "解析不到基表的连接"
        return f"{self.left}.{self.left_col}={self.right}.{self.right_col}"


@dataclass(frozen=True)
class SqlPath:
    """一条 SQL 的路径事实(表 / 连接 / 引用到的列名)。"""

    parse_ok: bool
    tables: tuple[str, ...] = ()
    joins: tuple[JoinRef, ...] = ()
    columns: tuple[str, ...] = ()


def _cte_names(stmt: exp.Expression) -> set[str]:
    return {(c.alias_or_name or "").lower()
            for c in stmt.find_all(exp.CTE) if c.alias_or_name}


def _alias_map(stmt: exp.Expression) -> dict[str, str]:
    """别名(小写)→ 基表名(小写);``""`` = 是派生表/CTE,基表未知。

    CTE 与派生表都显式映射到空串而不是缺席 —— 缺席会让 ``.get(table,
    table)`` 把 ``T2`` 当成一张真表,于是「解析不到」被读成「未声明表」,
    把一个解析问题变成一个声明指控。
    """
    ctes = _cte_names(stmt)
    out: dict[str, str] = {}
    for t in stmt.find_all(exp.Table):
        name = (t.name or "").lower()
        if not name or name in ctes:
            continue
        if t.alias:
            out[t.alias.lower()] = name
        else:
            out.setdefault(name, name)
    for sq in stmt.find_all(exp.Subquery):
        if sq.alias:
            out.setdefault(sq.alias.lower(), "")
    for name in ctes:
        out.setdefault(name, "")
    return out


def _derived_map(stmt: exp.Expression) -> dict[tuple[str, str], tuple[str, str]]:
    """(派生表别名, 输出列) → (基表, 基列) —— **只认穿透列**。

    子查询/CTE 是生成侧最常见的形态(``JOIN (SELECT a, b FROM t) AS T2``),
    连接经它发生时,两侧基表必须能复原,否则「路径在不在声明内」就答不了。
    复原规则刻意保守:**输出列与内层被引用的列同名**(直接穿透)才映射;
    聚合/表达式/跨多表的输出列一律不给映射 —— 猜错的映射会把「解析不到」
    变成一条**错指控**(把声明过的边报成未声明),比判不了更糟。
    """
    out: dict[tuple[str, str], tuple[str, str]] = {}
    for node in list(stmt.find_all(exp.Subquery)) + list(stmt.find_all(exp.CTE)):
        alias = (node.alias_or_name or "").lower()
        if not alias:
            continue
        inner = node.this if isinstance(node.this, exp.Select) else node.find(exp.Select)
        if not isinstance(inner, exp.Select):
            continue
        inner_aliases = _alias_map(inner)
        bases = {v for v in inner_aliases.values() if v}
        # 未限定列名只在子查询**只读一张基表**时才有唯一归属(与 lineage
        # 的 single_base 回退同款);多表时不给猜测。
        single_base = next(iter(bases)) if len(bases) == 1 else ""
        for proj in inner.expressions:
            if isinstance(proj, exp.Alias):
                name, expr = proj.alias, proj.this
            elif isinstance(proj, exp.Column):
                name, expr = proj.name, proj
            else:
                continue
            name = (name or "").lower()
            if not name or not isinstance(expr, exp.Column):
                continue  # 聚合/表达式输出:不给映射
            qualifier = (expr.table or "").lower()
            base = inner_aliases.get(qualifier, "") if qualifier else single_base
            if base and (expr.name or "").lower() == name:
                out[(alias, name)] = (base, name)
    return out


def _joins(
    stmt: exp.Expression, aliases: dict[str, str],
    derived: dict[tuple[str, str], tuple[str, str]],
) -> list[JoinRef]:
    """每个 ``JOIN`` 产出一条(或按等式条数多条)``JoinRef``。

    复合键的 ``ON a.x=b.y AND a.z=b.w`` 拆成两条 —— 声明侧也按逐列对匹配,
    两边同构(见 :func:`classify_join`)。
    """
    out: list[JoinRef] = []
    for join in stmt.find_all(exp.Join):
        on = join.args.get("on")
        pairs = []
        if on is not None:
            for eq in on.find_all(exp.EQ):
                left, right = eq.left, eq.right
                if isinstance(left, exp.Column) and isinstance(right, exp.Column):
                    pairs.append((left, right))
        if not pairs:  # CROSS / USING / 非等值条件:解析不到两侧基表
            out.append(JoinRef("", "", "", "", resolved=False))
            continue
        for left, right in pairs:
            lt, lc = _side(left, aliases, derived)
            rt, rc = _side(right, aliases, derived)
            if not lt or not rt or lt == rt:
                out.append(JoinRef("", "", "", "", resolved=False))
                continue
            out.append(JoinRef(lt, lc, rt, rc))
    return out


def _side(
    col: exp.Column, aliases: dict[str, str],
    derived: dict[tuple[str, str], tuple[str, str]],
) -> tuple[str, str]:
    """连接的一侧 → ``(基表, 基列)``;``("", "")`` = 复原不出来。"""
    qualifier = (col.table or "").lower()
    name = (col.name or "").lower()
    if not qualifier or not name:
        return "", ""
    base = aliases.get(qualifier, "")
    if base:
        return base, name
    mapped = derived.get((qualifier, name))
    if mapped:
        return mapped
    return "", ""


def parse_path(sql: str, dialect: str = "mysql") -> SqlPath:
    """解析一条 SQL 的路径事实;解析失败 → ``parse_ok=False``(判不了,不抛)。"""
    if not sql or not sql.strip():
        return SqlPath(parse_ok=False)
    try:
        stmts = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except Exception:  # noqa: BLE001 — 解析不了就是判不了,不是异常
        return SqlPath(parse_ok=False)
    if not stmts:
        return SqlPath(parse_ok=False)
    tables: set[str] = set()
    joins: list[JoinRef] = []
    columns: set[str] = set()
    for stmt in stmts:
        ctes = _cte_names(stmt)
        aliases = _alias_map(stmt)
        for t in stmt.find_all(exp.Table):
            name = (t.name or "").lower()
            if name and name not in ctes:
                tables.add(name)
        for c in stmt.find_all(exp.Column):
            if c.name:
                columns.add(c.name.lower())
        joins += _joins(stmt, aliases, _derived_map(stmt))
    return SqlPath(True, tuple(sorted(tables)), tuple(joins), tuple(sorted(columns)))


# ── 声明索引(模型 + 探测结果)───────────────────────────


@dataclass(frozen=True)
class EdgeSpec:
    """一条声明的边 + 编译期可走性 + 汇总前提结论。"""

    name: str
    from_: str
    to: str
    from_columns: tuple[str, ...]
    to_columns: tuple[str, ...]
    cardinality: str
    fan_out: str
    verdict: str  # "" = 本次未探测

    def blocked_reason(self) -> str:
        """编译期会不会拒绝这条边 —— 空串 = 走得通。

        与 compiler 的 fan_out 分支同款:M:N 无豁免直接拒;``bridge:``
        保留未实现,声明了也仍拒;``dedup`` 是真豁免(其前提由探测判)。
        """
        if self.cardinality != "M:N":
            return ""
        if self.fan_out == "dedup":
            return ""
        if self.fan_out.startswith("bridge:"):
            return f"fan_out={self.fan_out} 为保留语义、编译期未实现"
        return "M:N 未声明 fan_out 豁免,编译期拒 fan-out"


@dataclass(frozen=True)
class DeclIndex:
    """声明层的可查形态:表→数据集、边、以及每个前提的实测结论。"""

    model_name: str
    live: bool
    table_to_dataset: dict[str, str] = field(default_factory=dict)
    dataset_table: dict[str, str] = field(default_factory=dict)  # 反向
    key_columns: dict[str, tuple[str, ...]] = field(default_factory=dict)
    edges: tuple[EdgeSpec, ...] = ()
    key_verdict: dict[str, str] = field(default_factory=dict)  # 数据集 → 四态
    #: 组合路径的父表反查:(数据集, 列) → {(父数据集, 父列)}
    fk_targets: dict[tuple[str, str], frozenset[tuple[str, str]]] = field(
        default_factory=dict)

    def edge(self, name: str) -> EdgeSpec | None:
        for e in self.edges:
            if e.name == name:
                return e
        return None

    def premise(self, target: str) -> str:
        """边名或数据集名的前提结论;``""`` = 没有可用的实测。"""
        e = self.edge(target)
        if e is not None:
            return e.verdict
        return self.key_verdict.get(target, "")


def _aggregate(verdicts: Iterable[str]) -> str:
    best, rank = "", -1
    for v in verdicts:
        if _VERDICT_RANK.get(v, -1) > rank:
            best, rank = v, _VERDICT_RANK[v]
    return best


def build_index(
    model: SemanticModel, probes: Iterable[Probe] = (), *, live: bool = False,
) -> DeclIndex:
    """把模型 + 探测结果折成可查索引。

    ``live=False``(没接库)时所有前提都是 ``""``(未实测)—— 报告据此把
    声明过的前提一律算「判不了」,绝不把「没测」读成「为真」。
    """
    probes = list(probes)
    table_to_dataset: dict[str, str] = {}
    dataset_table: dict[str, str] = {}
    for ds in model.datasets:
        leaf = str(ds.source or "").split(".")[-1].strip().lower()
        if leaf:
            table_to_dataset[leaf] = ds.name
            dataset_table[ds.name] = leaf

    rel_verdicts: dict[str, list[str]] = {}
    key_verdicts: dict[str, list[str]] = {}
    key_targets: dict[str, str] = {}
    key_columns: dict[str, tuple[str, ...]] = {}
    for ds in model.datasets:
        entity = ds.entity
        if entity is not None and entity.natural_keys:
            keys = list(entity.natural_keys)
        elif ds.primary_key:
            keys = list(ds.primary_key)
        else:
            continue
        key_targets[f"{ds.name}.{'+'.join(keys)}"] = ds.name
        key_columns[ds.name] = tuple(c.lower() for c in keys)
    for p in probes:
        if p.kind in ("relationships", "fan_out"):
            rel_verdicts.setdefault(p.target, []).append(p.verdict)
        elif p.kind == "keys":
            ds_name = key_targets.get(p.target)
            if ds_name:
                key_verdicts.setdefault(ds_name, []).append(p.verdict)

    edges: list[EdgeSpec] = []
    fk_targets: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for rel in model.relationships:
        fc = tuple(c.lower() for c in rel.from_columns)
        tc = tuple(c.lower() for c in rel.to_columns)
        edges.append(EdgeSpec(
            name=rel.name, from_=rel.from_, to=rel.to, from_columns=fc,
            to_columns=tc, cardinality=rel.cardinality, fan_out=rel.fan_out,
            verdict=_aggregate(rel_verdicts.get(rel.name, [])) if live else "",
        ))
        for f_col, t_col in zip(fc, tc):
            fk_targets.setdefault((rel.from_, f_col), set()).add((rel.to, t_col))
    # 「声明了键」与「实测了键」分开:没实测时 key_verdict 缺席(而不是 ""),
    # classify 侧据此把「父键没声明」(覆盖缺口)与「父键没实测」(判不了)
    # 分成两件事。
    return DeclIndex(
        model_name=model.name or "semantic_model",
        live=live,
        table_to_dataset=table_to_dataset,
        dataset_table=dataset_table,
        key_columns=key_columns,
        edges=tuple(edges),
        key_verdict=({k: _aggregate(v) for k, v in key_verdicts.items()}
                     if live else {}),
        fk_targets={k: frozenset(v) for k, v in fk_targets.items()},
    )


# ── 路径 vs 声明 ─────────────────────────────────────────


@dataclass(frozen=True)
class PathVerdict:
    """一条路径相对声明层的判定(与 :class:`SqlPath` 分开:一个纯解析,
    一个要查声明索引)。"""

    parsed: bool = True                     # SQL 本身解析成功
    tables: tuple[str, ...] = ()            # 读到的物理基表
    declared: tuple[str, ...] = ()          # 命中的直接声明边
    composed: tuple[str, ...] = ()          # 组合路径(共同父)
    undeclared: tuple[str, ...] = ()        # 未声明且非组合的连接
    unknown_tables: tuple[str, ...] = ()    # 不是任何数据集的物理表
    unresolved: int = 0                     # 解析不到基表的连接数
    blocked: tuple[str, ...] = ()           # 声明了但编译期走不通的边
    violated: tuple[str, ...] = ()          # 前提被证伪的边
    violated_keys: tuple[str, ...] = ()     # 前提被证伪的键(数据集名)
    unprobed: tuple[str, ...] = ()          # 声明过、本次没实测的边前提
    unprobed_keys: tuple[str, ...] = ()     # 声明过、本次没实测的键前提
    composed_undeclared: tuple[str, ...] = ()  # 组合路径的父键没有声明

    def declared_only(self) -> bool:
        """路径全部落在声明内(含组合),没有未声明/未知表/解析不到。"""
        return (not self.undeclared and not self.unknown_tables
                and not self.unresolved)


def classify_join(
    index: DeclIndex, ref: JoinRef,
) -> tuple[str, str, str]:
    """一条连接等式 → ``(类别, 命中的边名/父数据集, 前提结论)``。

    类别闭集:``declared`` / ``composed`` / ``undeclared`` / ``unknown_table``
    / ``unresolved``。
    """
    if not ref.resolved:
        return "unresolved", "", ""
    dl = index.table_to_dataset.get(ref.left, "")
    dr = index.table_to_dataset.get(ref.right, "")
    if not dl or not dr:
        missing = ref.left if not dl else ref.right
        return "unknown_table", missing, ""
    for e in index.edges:
        pair = (ref.left_col, ref.right_col)
        rev = (ref.right_col, ref.left_col)
        pairs = tuple(zip(e.from_columns, e.to_columns))
        if e.from_ == dl and e.to == dr and pair in pairs:
            return "declared", e.name, e.verdict
        if e.from_ == dr and e.to == dl and rev in pairs:
            return "declared", e.name, e.verdict
    # 组合路径:两表各按已声明边连到同一父,且父键列同名 —— 等价于经父表连接,
    # 前提是父键唯一(父数据集声明的键的探测结论)。
    left_targets = index.fk_targets.get((dl, ref.left_col), frozenset())
    right_targets = index.fk_targets.get((dr, ref.right_col), frozenset())
    common = left_targets & right_targets
    if common:
        parent, _parent_col = sorted(common)[0]
        return "composed", parent, index.key_verdict.get(parent, "")
    return "undeclared", f"{dl}.{ref.left_col}={dr}.{ref.right_col}", ""


def key_premises(
    index: DeclIndex, path: SqlPath,
) -> tuple[list[str], list[str]]:
    """键锚定前提:``(被证伪的数据集, 未实测的数据集)``。

    只在**前提真的用上了**时才算数:该数据集的表被这条 SQL 读、且它声明的
    键列全部出现在 SQL 里(键锚定/骨架保真保的就是"这一列唯一标识一行")。
    表没读、或键列没引用 → 声明与这条 SQL 无关,不产生前提。
    """
    violated: list[str] = []
    unprobed: list[str] = []
    if not path.parse_ok:
        return violated, unprobed
    cols = set(path.columns)
    for ds_name, table in index.dataset_table.items():
        keys = index.key_columns.get(ds_name, ())
        if not keys or table not in path.tables or not set(keys) <= cols:
            continue
        verdict = index.key_verdict.get(ds_name, "")
        if verdict == VIOLATED:
            violated.append(ds_name)
        elif not verdict:
            unprobed.append(ds_name)
    return violated, unprobed


def classify_path(index: DeclIndex, path: SqlPath) -> PathVerdict:
    """一条路径的整体判定(表 + 全部连接 + 键锚定前提)。"""
    declared: list[str] = []
    composed: list[str] = []
    undeclared: list[str] = []
    unknown: list[str] = []
    unresolved = 0
    blocked: list[str] = []
    violated: list[str] = []
    unprobed: list[str] = []
    composed_undeclared: list[str] = []
    for table in path.tables:
        if table not in index.table_to_dataset:
            unknown.append(table)
    for ref in path.joins:
        kind, name, verdict = classify_join(index, ref)
        if kind == "declared":
            declared.append(name)
            e = index.edge(name)
            if e is not None:
                if e.blocked_reason():
                    blocked.append(name)
                if verdict == VIOLATED:
                    violated.append(name)
                elif not verdict:
                    unprobed.append(name)
        elif kind == "composed":
            composed.append(name)
            # 「父键没声明」(覆盖缺口,不是错)与「父键没实测」(判不了)是
            # 两件事:前者另有计数,只有后者进桶。
            if name not in index.key_columns:
                composed_undeclared.append(name)
                continue
            if verdict == VIOLATED:
                violated.append(name)
            elif not verdict:
                unprobed.append(name)
        elif kind == "unknown_table":
            # 表未声明已经计进 unknown_tables,连接本身不重复计
            continue
        elif kind == "unresolved":
            unresolved += 1
        else:
            undeclared.append(name)
    violated_keys, unprobed_keys = key_premises(index, path)
    return PathVerdict(
        parsed=path.parse_ok,
        tables=path.tables,
        declared=tuple(sorted(set(declared))),
        composed=tuple(sorted(set(composed))),
        undeclared=tuple(sorted(set(undeclared))),
        unknown_tables=tuple(sorted(set(unknown))),
        unresolved=unresolved,
        blocked=tuple(sorted(set(blocked))),
        violated=tuple(sorted(set(violated))),
        violated_keys=tuple(sorted(set(violated_keys))),
        unprobed=tuple(sorted(set(unprobed))),
        unprobed_keys=tuple(sorted(set(unprobed_keys))),
        composed_undeclared=tuple(sorted(set(composed_undeclared))),
    )


# ── 逐题归因 ─────────────────────────────────────────────


@dataclass(frozen=True)
class FailureRow:
    """一条历史失败(``failures.jsonl`` / ``results.jsonl`` 的公共部分)。"""

    question: str
    gold_sql: str = ""
    pred_sql: str = ""
    error: str = ""
    run_id: str = ""
    verdict: str = ""


@dataclass(frozen=True)
class Finding:
    """一题的归因结论。"""

    question: str
    bucket: str
    reason: str
    gold: PathVerdict
    pred: PathVerdict | None = None
    flags: tuple[str, ...] = ()
    error: str = ""
    run_id: str = ""


def load_rows(path: str | Path) -> list[FailureRow]:
    """读 JSONL 失败/结果条目。

    坏行**响亮失败**(带行号):把截断的文件读成「一切都好」是诊断工具
    最不能有的失败模式。空行跳过。
    """
    rows: list[FailureRow] = []
    for lineno, line in enumerate(
            Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"{path}:{lineno} 不是合法 JSON: {e}") from e
        if not isinstance(data, dict):
            raise ValueError(f"{path}:{lineno} 不是 JSON 对象")
        rows.append(FailureRow(
            question=str(data.get("question") or ""),
            gold_sql=str(data.get("gold_sql") or ""),
            pred_sql=str(data.get("pred_sql") or ""),
            error=str(data.get("error") or ""),
            run_id=str(data.get("run_id") or ""),
            verdict=str(data.get("verdict") or ""),
        ))
    return rows


def attribute_row(
    row: FailureRow, index: DeclIndex, *, dialect: str = "mysql",
) -> Finding:
    """一题 → 一个桶 + 一句话理由 + 两侧路径判定。"""
    gold = parse_path(row.gold_sql, dialect)
    pred = parse_path(row.pred_sql, dialect) if row.pred_sql else None
    gv = classify_path(index, gold) if gold.parse_ok else None
    pv = classify_path(index, pred) if (pred is not None and pred.parse_ok) else None

    flags: list[str] = []
    if pred is not None and not pred.parse_ok:
        flags.append("pred_unparseable")
    if pv is not None:
        if pv.undeclared:
            flags.append("pred_undeclared")
        if pv.blocked:
            flags.append("pred_blocked")
        if pv.violated:
            flags.append("pred_violated_premise")
    if pv is not None and gv is not None:
        needed = set(gold.tables) & set(index.table_to_dataset)
        if needed - set(pred.tables):
            flags.append("pred_missing_table")

    if gv is None:
        return Finding(
            row.question, "unattributable",
            "gold SQL 解析失败,需要的路径无从判定", PathVerdict(parsed=False),
            pv, tuple(flags), row.error, row.run_id)

    if pv is not None and (pv.violated or pv.violated_keys):
        return Finding(
            row.question, "premise_false_walked",
            "实际生成的 SQL 走过前提已被证伪的声明: "
            + "、".join(_target_labels(index, pv.violated)
                       + _target_labels(index, pv.violated_keys, keys=True)),
            gv, pv, tuple(flags), row.error, row.run_id)

    if gv.violated or gv.violated_keys:
        return Finding(
            row.question, "premise_false_needed",
            "gold 需要的声明前提已被证伪: "
            + "、".join(_target_labels(index, gv.violated)
                       + _target_labels(index, gv.violated_keys, keys=True)),
            gv, pv, tuple(flags), row.error, row.run_id)

    if gv.blocked:
        return Finding(
            row.question, "path_blocked",
            "gold 需要的边在编译期走不通: "
            + "、".join(f"{n}({index.edge(n).blocked_reason()})"
                       if index.edge(n) else n for n in gv.blocked),
            gv, pv, tuple(flags), row.error, row.run_id)

    if gv.unknown_tables or gv.undeclared:
        parts = []
        if gv.unknown_tables:
            parts.append("未声明为数据集的表: " + "、".join(gv.unknown_tables))
        if gv.undeclared:
            parts.append("未声明的连接: " + "、".join(gv.undeclared))
        return Finding(
            row.question, "path_undeclared", "; ".join(parts),
            gv, pv, tuple(flags), row.error, row.run_id)

    if gv.unresolved:
        return Finding(
            row.question, "unattributable",
            f"gold 有 {gv.unresolved} 条连接解析不到基表(派生表/CTE 别名),"
            f"无法判定是否在声明内",
            gv, pv, tuple(flags), row.error, row.run_id)

    if gv.unprobed or gv.unprobed_keys:
        why = ("本次未接库,声明前提未实测"
               if not index.live else "声明前提本次未能实测")
        return Finding(
            row.question, "unattributable",
            f"{why}: " + "、".join(_target_labels(index, gv.unprobed)
                                  + _target_labels(index, gv.unprobed_keys, keys=True)),
            gv, pv, tuple(flags), row.error, row.run_id)

    return Finding(
        row.question, "premise_true",
        "gold 需要的路径全部在声明内且前提未被证伪 —— 声明层解释不了这题",
        gv, pv, tuple(flags), row.error, row.run_id)


def _target_labels(index: DeclIndex, targets: Iterable[str], *,
                   keys: bool = False) -> list[str]:
    """把结论标签补成人能读的形态(``Edge[已被证伪]`` / ``数据集.键[判不了]``)。"""
    out = []
    for t in targets:
        target = f"{t}(键)" if keys else t
        label = VERDICT_LABELS.get(index.premise(t), "")
        out.append(f"{target}[{label}]" if label else target)
    return out


# ── 报告 ─────────────────────────────────────────────────

_NUM = re.compile(r"\d+")


def error_signature(err: str, limit: int = 72) -> str:
    """失败签名的确定性归一:数字糊成 ``#``,压掉空白。

    失败原文里逐题变化的只有行数/值 —— 不归一的话 52 条同类失败会变成
    52 个"签名",签名榜就没有信息。
    """
    text = " ".join((err or "").split())
    if not text:
        return "(无 error 字段)"
    text = _NUM.sub("#", text)
    return text[:limit] + ("…" if len(text) > limit else "")


def build_report(
    rows: Iterable[FailureRow], index: DeclIndex, *,
    dialect: str = "mysql", meta: dict[str, Any] | None = None,
    samples: int = 3, signatures: int = 5,
) -> dict[str, Any]:
    """归因报告(纯函数:同一输入逐字节同一输出;时间/指纹由调用方经
    ``meta`` 传入)。"""
    findings = [attribute_row(r, index, dialect=dialect) for r in rows]
    bucket_counts = {b: 0 for b in BUCKETS}
    for f in findings:
        bucket_counts[f.bucket] += 1

    joins_gold = _kind_counts(index, findings, side="gold")
    joins_pred = _kind_counts(index, findings, side="pred")
    observations = {
        "pred_unparseable": _flag_count(findings, "pred_unparseable"),
        "pred_undeclared": _flag_count(findings, "pred_undeclared"),
        "pred_blocked": _flag_count(findings, "pred_blocked"),
        "pred_violated_premise": _flag_count(findings, "pred_violated_premise"),
        "pred_missing_table": _flag_count(findings, "pred_missing_table"),
    }
    composed_gap: set[str] = set()
    for f in findings:
        for v in (f.gold, f.pred):
            if v is not None:
                composed_gap |= set(v.composed_undeclared)

    by_bucket: dict[str, list[Finding]] = {b: [] for b in BUCKETS}
    for f in findings:
        by_bucket[f.bucket].append(f)

    report: dict[str, Any] = {
        "meta": dict(meta or {}),
        "counts": {
            "rows": len(findings),
            "questions": len({f.question for f in findings}),
            "buckets": bucket_counts,
            "joins_gold": joins_gold,
            "joins_pred": joins_pred,
        },
        "coverage": {
            "gold_parsed": sum(1 for f in findings if f.gold.parsed),
            "gold_fully_declared": sum(
                1 for f in findings
                if f.gold.parsed and f.gold.declared_only()),
            "gold_fully_usable": sum(
                1 for f in findings
                if f.gold.parsed and f.gold.declared_only()
                and not f.gold.blocked),
            "gold_tables": _table_counts(index, findings),
        },
        "premises": {
            "live": index.live,
            "edges": {e.name: e.verdict for e in index.edges},
            "keys": dict(index.key_verdict),
            "keys_declared": {k: list(v) for k, v in index.key_columns.items()},
            "violated": sorted({t for f in findings
                                for t in (f.gold.violated + f.gold.violated_keys
                                          + _pred_violated(f))}),
            "unprobed": sorted({t for f in findings
                                for t in (f.gold.unprobed + f.gold.unprobed_keys)}),
            "composed_parents_undeclared": sorted(composed_gap),
        },
        "observations": observations,
        "samples": {b: _sample_rows(by_bucket[b], samples) for b in BUCKETS},
        "error_signatures": {
            b: _signature_counts(by_bucket[b], signatures) for b in BUCKETS
        },
    }
    run_ids = sorted({f.run_id for f in findings if f.run_id})
    if run_ids:
        report["counts"]["by_run"] = {
            rid: sum(1 for f in findings if f.run_id == rid) for rid in run_ids}
    return report


def _sample_rows(bucket_findings: list[Finding], limit: int) -> list[dict[str, Any]]:
    """桶内样例 —— 按提问去重(同一道题可能失败过好几次,重复样例只会
    挤掉别的题;条数仍以 counts 为准,样例是给人看的入口不是分母)。"""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for f in bucket_findings:
        if f.question in seen:
            continue
        seen.add(f.question)
        out.append({
            "question": f.question[:120],
            "reason": f.reason,
            "error": error_signature(f.error, 60),
            "run_id": f.run_id,
        })
        if len(out) >= limit:
            break
    return out


def _pred_violated(f: Finding) -> tuple[str, ...]:
    if f.pred is None:
        return ()
    return f.pred.violated + f.pred.violated_keys


def _flag_count(findings: list[Finding], flag: str) -> int:
    return sum(1 for f in findings if flag in f.flags)


def _kind_counts(index: DeclIndex, findings: list[Finding], *, side: str) -> dict[str, int]:
    """连接等式的分类计数。

    ``blocked_in_declared`` / ``unprobed_in_declared`` 是 ``declared`` 的
    **子集**(一条边走不通或没实测,它照样是声明边),所以 ``joins`` 只由
    互斥的几档相加 —— 把子集也加进去会让总数虚高(每条未实测边算两遍)。
    """
    counts = {"joins": 0, "declared": 0, "composed": 0, "undeclared": 0,
              "unknown_table": 0, "unresolved": 0,
              "blocked_in_declared": 0, "unprobed_in_declared": 0}
    for f in findings:
        v = f.gold if side == "gold" else f.pred
        if v is None:
            continue
        counts["declared"] += len(v.declared)
        counts["composed"] += len(v.composed)
        counts["undeclared"] += len(v.undeclared)
        counts["unknown_table"] += len(v.unknown_tables)
        counts["unresolved"] += v.unresolved
        counts["blocked_in_declared"] += len(v.blocked)
        counts["unprobed_in_declared"] += len(v.unprobed)
    counts["joins"] = (counts["declared"] + counts["composed"]
                       + counts["undeclared"] + counts["unknown_table"]
                       + counts["unresolved"])
    return counts


def _table_counts(index: DeclIndex, findings: list[Finding]) -> dict[str, dict[str, int]]:
    """gold 引用到的物理表 × 是否声明为数据集。"""
    declared: dict[str, int] = {}
    undeclared: dict[str, int] = {}
    for f in findings:
        for t in f.gold.tables:
            tgt = declared if t in index.table_to_dataset else undeclared
            tgt[t] = tgt.get(t, 0) + 1
    return {"declared": dict(sorted(declared.items(), key=lambda kv: -kv[1])),
            "undeclared": dict(sorted(undeclared.items(), key=lambda kv: -kv[1]))}


def _signature_counts(bucket_findings: list[Finding], limit: int) -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in bucket_findings:
        sig = error_signature(f.error)
        counts[sig] = counts.get(sig, 0) + 1
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return dict(top)


def render_report(report: dict[str, Any]) -> str:
    """人读文本(对齐 gate.py 的 render_report 体例)。"""
    meta = report.get("meta") or {}
    counts = report["counts"]
    total = counts["rows"] or 1
    out: list[str] = []
    out.append("回顾归因 — 历史失败题 vs 声明层")
    for key in ("failures", "kb_dir", "kb_rev", "kb_sha256", "kb_last_commit",
                "kb_changed_since", "dialect", "live"):
        if key in meta:
            out.append(f"  {key}: {meta[key]}")
    premises = report["premises"]
    if not premises["live"]:
        out.append("  ⚠ 未接库(--dsn):只判声明覆盖,前提真假未实测 —— "
                   "声明过的前提一律计入「判不了」")
    if meta.get("kb_changed_since"):
        out.append("  ⚠ KB 在这批失败记录之后改过 —— 这是反事实诊断"
                   "(今天的声明遇到这些题),不是当时的原因")

    out.append("")
    n_rows, n_qs = counts["rows"], counts.get("questions", counts["rows"])
    scope = (f"共 {n_rows} 条失败记录"
             + (f"({n_qs} 道不同问题)" if n_qs != n_rows else ""))
    out.append(f"归因桶(互斥,按优先级;{scope})")
    for b in BUCKETS:
        n = counts["buckets"].get(b, 0)
        pct = f"{n / total:5.1%}"
        out.append(f"  {BUCKET_LABELS[b]:<22} {n:>5}  {pct}")

    cov = report["coverage"]
    out.append("")
    out.append("gold 路径覆盖(这题本来该怎么走)")
    out.append(f"  解析成功 {cov['gold_parsed']}/{counts['rows']}"
               f" · 全在声明内 {cov['gold_fully_declared']}"
               f" · 且可走(无 M:N 拦路) {cov['gold_fully_usable']}")
    gj, pj = counts["joins_gold"], counts["joins_pred"]
    out.append(f"  连接等式(gold {gj['joins']} / pred {pj['joins']})"
               f"  声明边 {gj['declared']}/{pj['declared']}"
               f" · 组合 {gj['composed']}/{pj['composed']}"
               f" · 未声明 {gj['undeclared']}/{pj['undeclared']}"
               f" · 表未声明 {gj['unknown_table']}/{pj['unknown_table']}"
               f" · 声明拦路 {gj['blocked_in_declared']}/{pj['blocked_in_declared']}"
               f" · 解析不到 {gj['unresolved']}/{pj['unresolved']}")
    tables = cov["gold_tables"]
    if tables["undeclared"]:
        items = "、".join(f"{t}×{n}" for t, n in tables["undeclared"].items())
        out.append(f"  gold 用了未声明为数据集的表: {items}")

    obs = report["observations"]
    out.append("")
    out.append("实际走过的(pred,观察量 —— 不进桶)")
    out.append(f"  走了未声明连接 {obs['pred_undeclared']} 题"
               f" · 走了被拒的边 {obs['pred_blocked']} 题"
               f" · 走过假前提 {obs['pred_violated_premise']} 题"
               f" · 缺 gold 需要的表 {obs['pred_missing_table']} 题"
               f" · 解析失败 {obs['pred_unparseable']} 题")

    edge_items = "、".join(
        f"{k}={VERDICT_LABELS.get(v, v)}" for k, v in premises["edges"].items())
    key_items = "、".join(
        f"{k}[{'+'.join(cols)}]={VERDICT_LABELS.get(premises['keys'].get(k, ''), '')}"
        for k, cols in (premises.get("keys_declared") or {}).items())
    out.append("")
    out.append("前提(声明过的东西)")
    out.append(f"  边: {edge_items or '(无声明边)'}")
    out.append(f"  键: {key_items or '(无声明键 —— 覆盖率缺口,见 trove validate --live)'}")
    if premises["composed_parents_undeclared"]:
        out.append("  组合路径缺父键声明: "
                   + "、".join(premises["composed_parents_undeclared"]))

    for b in BUCKETS:
        samples = report["samples"].get(b) or []
        sigs = report["error_signatures"].get(b) or {}
        if not samples and not sigs:
            continue
        out.append("")
        out.append(f"[{BUCKET_LABELS[b]}] {counts['buckets'].get(b, 0)} 题")
        for sig, n in sigs.items():
            out.append(f"  签名 ×{n}: {sig}")
        for s in samples:
            label = f"({s['run_id']}) " if s.get("run_id") else ""
            out.append(f"  · {label}{s['question']}")
            out.append(f"    → {s['reason']}")
    return "\n".join(out)
