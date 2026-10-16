"""生成链归因:结构侧 L1 档位 + 轨迹侧 L2 责任跳(零 LLM / 零网络 / 不连库)。

设计:~/hub/trove-design/2026-10-16-generation-chain-attribution-design.md。
消费方:scripts/eval_chain_attribution.py。

口径单点定义(不许私有副本):表/连接复用 attribution.parse_path;列宽容
(裸列≡限定列、COUNT(*)≡COUNT(col))复用 PlanSignature._col_matches/
_cols_match;verdict 集合用 replay.JUDGED_VERDICTS。
"""
from __future__ import annotations

# json/re 由本文件后段(plan 解析 / 责任跳 / 报告)消费 —— 一次导入到位,
# 免得后段各写一份;本轮(L1 档位梯)尚未引用它们,故仍挂 F401 noqa。
import json  # noqa: F401
import re  # noqa: F401
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from trove.eval.attribution import parse_path
from trove.eval.replay import JUDGED_VERDICTS
from trove.services.semantic_layer.contract import Col, PlanSignature

# ── 桶常量(设计 §2.2 / §3.2)─────────────────────────────
L1_BUCKETS: tuple[str, ...] = (
    "no_sql", "pred_unparseable", "exec_error", "table_set", "join_shape",
    "filter_diff", "agg_diff", "order_limit_diff", "projection_diff",
    "structure_equal", "undecidable",
)
L2_BUCKETS: tuple[str, ...] = (
    "linking_missed_table", "planner_chose_differently", "gen_ignored_plan",
    "undecidable",
)
LOOP_STATES: tuple[str, ...] = (
    "detected_but_unfixed", "never_detected", "undecidable",
)

#: 未产出可判 SQL 的 verdict(设计 §2.2 第 1 档)。REFUSED 必须最先判:
#: 拒绝轮没有 SQL 可言,pred_sql 可能是上一轮的残留(eval_bird.py 同款注释)。
_NO_SQL_VERDICTS = frozenset({"REFUSED", "EMPTY_SQL", "GENERATION_ERROR"})

#: 六个结构档位的判定序(设计 §2.2 第 4-9 档;首个命中即桶)。
_RUNG_ORDER: tuple[str, ...] = (
    "table_set", "join_shape", "filter_diff", "agg_diff",
    "order_limit_diff", "projection_diff",
)

_COMPARE_OPS = {
    "EQ": "=", "NEQ": "!=", "GT": ">", "GTE": ">=", "LT": "<", "LTE": "<=",
}


@dataclass(frozen=True, order=True)
class Predicate:
    """一个过滤谓词:(列集升序, 算子, 字面量元组)。

    子查询值归一为 ``"(subquery)"``(与 plan 文本抽取器同款,两侧可比);
    比较不了/其余形状(IS NULL / EXISTS / OR 整体)记 ``op="raw"`` + 原文。
    """

    cols: tuple[Col, ...]
    op: str
    values: tuple[str, ...] = ()


@dataclass(frozen=True)
class SqlFacts:
    """一条 SQL 的结构事实(全小写归一;``ok=False`` = 判不了)。"""

    ok: bool
    tables: tuple[str, ...] = ()
    join_edges: tuple[tuple[tuple[str, str], tuple[str, str]], ...] = ()
    unresolved_joins: int = 0
    conds: tuple[Predicate, ...] = ()
    aggs: tuple[tuple[str, tuple[Col, ...]], ...] = ()
    groups: tuple[tuple[Col, ...], ...] = ()
    groups_known: bool = True
    order: tuple[tuple[str, str], ...] = ()
    has_limit: bool = False
    projections: tuple[tuple[str | None, tuple[Col, ...]], ...] = ()


def _cols_of(node: exp.Expression) -> tuple[Col, ...]:
    out: set[Col] = set()
    for c in node.find_all(exp.Column):
        out.add(((c.table or "").lower(), (c.name or "").lower()))
    return tuple(sorted(out))


def _lit(node: exp.Expression) -> tuple[str, ...]:
    """字面量归一:去引号、原样大小写(设计 §2.3:大小写差异按分歧计)。"""
    if isinstance(node, exp.Literal):
        return (str(node.this),)
    if isinstance(node, exp.Boolean):
        return (str(node.this).lower(),)
    if isinstance(node, (exp.Select, exp.Subquery)):
        return ("(subquery)",)
    if isinstance(node, exp.Neg):
        return tuple("-" + v for v in _lit(node.this))
    return (node.sql(dialect="").lower()[:40],)


def _predicates(cond: exp.Expression) -> list[Predicate]:
    """把一个 WHERE/HAVING 子树摊成谓词表(AND 展平;OR/NOT 整体记一条)。"""
    out: list[Predicate] = []
    stack = [cond]
    while stack:
        e = stack.pop()
        if isinstance(e, exp.Paren):
            stack.append(e.this)
            continue
        if isinstance(e, exp.And):
            stack.extend((e.this, e.expression))
            continue
        if isinstance(e, exp.In):
            if e.args.get("query") is not None:  # IN (SELECT …) 存 args["query"],不在 expressions
                out.append(Predicate(_cols_of(e.this), "in", ("(subquery)",)))
                continue
            vals = tuple(sorted(v for x in e.expressions for v in _lit(x)))
            out.append(Predicate(_cols_of(e.this), "in", vals or ("(empty)",)))
            continue
        if isinstance(e, exp.Between):
            out.append(Predicate(
                _cols_of(e.this), "between",
                _lit(e.args["low"]) + _lit(e.args["high"])))
            continue
        if isinstance(e, exp.Like):
            out.append(Predicate(_cols_of(e.this), "like", _lit(e.expression)))
            continue
        if type(e).__name__ in _COMPARE_OPS:
            op = _COMPARE_OPS[type(e).__name__]
            left, right = e.this, e.expression
            if isinstance(left, (exp.Select, exp.Subquery)) or isinstance(
                    right, (exp.Select, exp.Subquery)):
                # 子查询值:列取**非子查询那一侧**(子查询内部的列不属于本层比较)
                side = right if isinstance(left, (exp.Select, exp.Subquery)) else left
                out.append(Predicate(_cols_of(side), op, ("(subquery)",)))
                continue
            lcols, rcols = _cols_of(left), _cols_of(right)
            if lcols and rcols:  # 列 vs 列
                out.append(Predicate(
                    tuple(sorted(set(lcols + rcols))), op, ()))
                continue
            cols = lcols or rcols
            vals = _lit(right if lcols else left)
            out.append(Predicate(cols, op, tuple(sorted(vals))))
            continue
        # 其余形状(IS NULL / EXISTS / OR 整体…):整条记原文
        out.append(Predicate(_cols_of(e), "raw",
                             (e.sql(dialect="").lower()[:60],)))
    return out


def _outer_selects(stmt: exp.Expression) -> list[exp.Select]:
    if isinstance(stmt, exp.Select):
        return [stmt]
    if isinstance(stmt, exp.Union):
        return _outer_selects(stmt.this) + _outer_selects(stmt.expression)
    if isinstance(stmt, exp.Subquery):
        return _outer_selects(stmt.this)
    return []


def _projection_facts(sel: exp.Select) -> list[tuple[str | None, tuple[Col, ...]]]:
    out: list[tuple[str | None, tuple[Col, ...]]] = []
    for e in sel.expressions:
        e = e.this if isinstance(e, exp.Alias) else e
        name: str | None = None
        for a in e.find_all(exp.AggFunc):
            name = (a.key or "").lower()
            if isinstance(a, exp.Count) and isinstance(a.this, exp.Distinct):
                name = "count_distinct"
            break
        out.append((name, _cols_of(e)))
    return out


def sql_facts(sql: str, dialect: str = "mysql") -> SqlFacts | None:
    """抽取一条 SQL 的结构事实;空串或解析不了 → None(判不了,不伪装成空事实)。"""
    if not sql or not sql.strip():
        return None
    path = parse_path(sql, dialect)
    if not path.parse_ok:
        return None
    try:
        stmts = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except Exception:  # noqa: BLE001 — 解析不了就是判不了
        return None
    if not stmts:
        return None
    edges: set[tuple[tuple[str, str], tuple[str, str]]] = set()
    unresolved = 0
    for j in path.joins:
        if not j.resolved:
            unresolved += 1
            continue
        edges.add(tuple(sorted(((j.left.lower(), j.left_col.lower()),
                                (j.right.lower(), j.right_col.lower())))))
    conds: list[Predicate] = []
    projections: list[tuple[str | None, tuple[Col, ...]]] = []
    groups: list[tuple[Col, ...]] = []
    order: list[tuple[str, str]] = []
    has_limit = False
    for stmt in stmts:
        for w in list(stmt.find_all(exp.Where)) + list(stmt.find_all(exp.Having)):
            conds.extend(_predicates(w.this))
        for sel in _outer_selects(stmt):
            projections.extend(_projection_facts(sel))
            grp = sel.args.get("group")
            if grp:
                groups.extend(_cols_of(g) for g in grp.expressions)
            ord_ = sel.args.get("order")
            if ord_:
                for o in ord_.expressions:
                    col = (o.this.name or o.this.sql(dialect="")).lower()
                    order.append((col, "desc" if o.args.get("desc") else "asc"))
        if stmt.args.get("limit") is not None:
            has_limit = True
    aggs = tuple(sorted({(p[0], p[1]) for p in projections if p[0]}))
    return SqlFacts(
        ok=True,
        tables=tuple(sorted(t.lower() for t in path.tables)),
        join_edges=tuple(sorted(edges)),
        unresolved_joins=unresolved,
        conds=tuple(sorted(conds)),
        aggs=aggs,
        groups=tuple(sorted(groups)),
        order=tuple(order),
        has_limit=has_limit,
        projections=tuple(projections),
    )


# ══ L1 档位梯(设计 §2.2 / §2.3)══════════════════════════


@dataclass(frozen=True)
class LadderVerdict:
    """L1 档位判决:首个分歧档即 ``bucket``,其余进 ``also_differ``。"""

    bucket: str
    also_differ: tuple[str, ...] = ()
    observations: tuple[str, ...] = ()
    reason: str = ""


def _entries_match(
        g: tuple[tuple[str | None, tuple[Col, ...]], ...],
        p: tuple[tuple[str | None, tuple[Col, ...]], ...]) -> bool:
    """项集匹配:顺序无关的多重集比对,**两轮**(先精确列集,通配垫底)。

    列宽容复用 PlanSignature._cols_match(空列集 = 通配,COUNT(*)≡COUNT(col);
    裸列≡限定列);名字相等是前提,配对即移除。第 1 轮只接受**两侧列集都非空**
    的配对,第 2 轮才允许空列集当通配 —— 两轮合起来的可接受配对与单轮
    _cols_match 同集(不新增等价类,只是更好的指派),把通配留给精确配不上的
    那些:否则贪心先配会把通配误消费掉,gold `COUNT(*), COUNT(a.x)` vs pred
    `COUNT(a.x), COUNT(a.y)` 这种**有合法配对**的会被冤枉成分歧。
    **聚合档与投影档共用这一套容差**(设计 §2.1 `aggs` 行 / §2.2 projection
    档),分组档经 :func:`_groups_match` 也走它 —— 匹配规则一处实现。
    """
    if len(g) != len(p):
        return False
    remaining = list(g)
    pending = list(p)
    for wildcard_ok in (False, True):
        still: list[tuple[str | None, tuple[Col, ...]]] = []
        for pe in pending:
            if not wildcard_ok and not pe[1]:
                still.append(pe)  # 空列集 = 通配,留到第 2 轮
                continue
            for i, ge in enumerate(remaining):
                if pe[0] != ge[0] or (not wildcard_ok and not ge[1]):
                    continue
                if PlanSignature._cols_match(ge[1], pe[1]):
                    remaining.pop(i)
                    break
            else:
                still.append(pe)
        pending = still
    return not remaining and not pending


def _groups_match(
        groups_g: tuple[tuple[Col, ...], ...],
        groups_o: tuple[tuple[Col, ...], ...]) -> bool:
    """分组列集比对:逐组列集按 _cols_match 宽容(裸 ≡ 限定),组间顺序无关。

    薄包装 :func:`_entries_match`(条目名一律 None)—— 一套匹配规则一处实现,
    免得分组档自己长出一份逐字相等(设计 §2.1 `groups` 行「同上宽容」)。
    """
    return _entries_match(
        tuple((None, gs) for gs in groups_g),
        tuple((None, ps) for ps in groups_o))


def _predicates_match(g: tuple[Predicate, ...], o: tuple[Predicate, ...]) -> bool:
    """谓词多重集比对:列集按 _cols_match 宽容(裸 ≡ 限定),算子与字面量精确相等。

    字面量刻意不宽容(设计 §2.3:「字面量大小写按分歧计」)—— 宽容的只有"列写
    成什么样",不是"条件说的是什么";长度不同即不匹配。
    """
    if len(g) != len(o):
        return False
    remaining = list(g)
    for pe in o:
        for i, ge in enumerate(remaining):
            if (pe.op == ge.op and pe.values == ge.values
                    and PlanSignature._cols_match(ge.cols, pe.cols)):
                remaining.pop(i)
                break
        else:
            return False
    return True


def _differ(rung: str, g: SqlFacts, o: SqlFacts) -> bool:
    if rung == "table_set":
        return set(g.tables) != set(o.tables)
    if rung == "join_shape":
        return (g.join_edges != o.join_edges
                or g.unresolved_joins != o.unresolved_joins)
    if rung == "filter_diff":
        return not _predicates_match(g.conds, o.conds)
    if rung == "agg_diff":
        if not _entries_match(g.aggs, o.aggs):
            return True
        # plan 侧分组信息不可得(groups_known=False)时只比聚合,不冤枉计划
        if o.groups_known and g.groups_known:
            return not _groups_match(g.groups, o.groups)
        return False
    if rung == "order_limit_diff":
        return g.order != o.order or g.has_limit != o.has_limit
    if rung == "projection_diff":
        return not _entries_match(g.projections, o.projections)
    return False


def _diff_rungs(gold: SqlFacts, other: SqlFacts,
                rungs: frozenset[str] | None = None) -> list[str]:
    """按档位序返回 gold 与 other 有分歧的档(rungs=None → 全档)。"""
    return [r for r in _RUNG_ORDER
            if (rungs is None or r in rungs) and _differ(r, gold, other)]


def _case_insensitive_same(g: SqlFacts, o: SqlFacts) -> bool:
    def key(p: Predicate) -> tuple:
        return (p.cols, p.op)

    def lower(p: Predicate) -> tuple:
        return (p.cols, p.op, tuple(v.lower() for v in p.values))

    gk = {key(p) for p in g.conds}
    if gk != {key(p) for p in o.conds}:
        return False
    return ({lower(p) for p in g.conds} == {lower(p) for p in o.conds}
            and set(g.conds) != set(o.conds))


def _observations(gold: SqlFacts, pred: SqlFacts) -> tuple[str, ...]:
    """跨档观察码(设计 §2.3):不换桶,只给读者折扣尺。"""
    obs: list[str] = []
    if set(gold.tables) - set(pred.tables):
        obs.append("needed_table_never_read")
    if set(pred.tables) - set(gold.tables):
        obs.append("extra_table_read")
    if _case_insensitive_same(gold, pred):
        obs.append("case_only_value_diff")
    if gold.has_limit != pred.has_limit and (
            any(f in ("min", "max") for f, _ in gold.aggs)
            or any(f in ("min", "max") for f, _ in pred.aggs)):
        obs.append("extreme_style_mismatch")
    return tuple(obs)


def ladder(gold: SqlFacts | None, pred: SqlFacts | None, *,
           verdict: str = "", had_sql: bool = True) -> LadderVerdict:
    """L1 档位梯(设计 §2.2):首个分歧即桶;判不了独立成档,绝不混入 structure_equal。"""
    if verdict not in JUDGED_VERDICTS:
        verdict = ""  # 词表单点定义在 replay;表外的 verdict 不参与分桶(不猜)
    if not had_sql or verdict in _NO_SQL_VERDICTS:
        return LadderVerdict(
            "no_sql", reason=f"未产出可判 SQL(verdict={verdict or 'n/a'})")
    if pred is None or not pred.ok:
        return LadderVerdict("pred_unparseable", reason="pred SQL 解析不了")
    if verdict == "EXECUTION_ERROR":
        return LadderVerdict("exec_error", reason="SQL 有形状但执行报错")
    if gold is None or not gold.ok:
        return LadderVerdict("undecidable", reason="gold SQL 缺失或解析不了")
    diffs = _diff_rungs(gold, pred, None)
    obs = _observations(gold, pred)
    if not diffs:
        return LadderVerdict(
            "structure_equal", observations=obs,
            reason="六个结构维度全同——不是这几个结构维度的问题")
    return LadderVerdict(
        diffs[0], also_differ=tuple(diffs[1:]), observations=obs,
        reason=f"首个结构分歧={diffs[0]}")
