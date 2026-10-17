"""生成链归因:结构侧 L1 档位 + 轨迹侧 L2 责任跳(零 LLM / 零网络 / 不连库)。

设计:~/hub/trove-design/2026-10-16-generation-chain-attribution-design.md。
消费方:scripts/eval_chain_attribution.py。

口径单点定义(不许私有副本):表/连接复用 attribution.parse_path;列宽容
(裸列≡限定列、COUNT(*)≡COUNT(col))复用 PlanSignature._col_matches/
_cols_match;verdict 集合用 replay.JUDGED_VERDICTS。列限定词的**别名归一**
(别名→基表名;基表在语句里只出现一次才归、自连接保留别名身份)见 :func:`_alias_map`。
"""
from __future__ import annotations

# re 由下文的 plan 侧解析器消费;json 是**有意的前置声明**(plan JSON 路径收的
# 是已解码的 dict,CLI 自己 import json),目前仍无消费者 → F401 noqa 留着。
import json  # noqa: F401
import re
from collections.abc import Callable
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from trove.eval.attribution import FailureRow, parse_path
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


def _alias_map(stmts: list[exp.Expression]) -> dict[str, str]:
    """别名 → 基表名;基表在整条语句里**只出现一次**才归(含子查询里的出现)。

    列限定词写别名还是基表名是拼写,不是事实(实测语料里最常见的假分歧:
    gold 写 `c.gender`、plan 写 `client.gender`)。单次出现的基表,两侧指的是同一
    张表,归一到基表名后比的才是**列身份**。
    **自连接绝不合并**:同一基表出现两次时两个别名承载两条不同的行流,合并会把
    真实差异抹平(设计 §2.3 的宽容只到"列怎么写",不到"说的是哪张表");同一别名
    在不同子查询里指向不同基表(嵌套复用别名)→ 整个别名不归:宁可不归,不猜。
    **不复用 attribution._alias_map**:那把尺子答"别名指向哪张基表"(派生表/CTE
    归空串、自连接别名同归一张基表),服务于"路径在不在声明内";这把尺子答
    "别名的拼写何时可与基表名互换",自连接的合并正是它必须拒绝的事。
    """
    counts: dict[str, int] = {}
    alias_bases: dict[str, set[str]] = {}
    for stmt in stmts:
        for t in stmt.find_all(exp.Table):
            base = (t.name or "").lower()
            if not base:
                continue
            counts[base] = counts.get(base, 0) + 1
            alias = (t.alias or "").lower()
            if alias:
                alias_bases.setdefault(alias, set()).add(base)
    out: dict[str, str] = {}
    for alias, bases in alias_bases.items():
        if len(bases) != 1:
            continue
        base = next(iter(bases))
        if counts.get(base, 0) == 1:
            out[alias] = base
    return out


def _map_cols(cols: tuple[Col, ...], amap: dict[str, str]) -> tuple[Col, ...]:
    """按别名表归一列限定词(只换表名;升序去重不变式原样保留)。"""
    return tuple(sorted({(amap.get(t, t), c) for t, c in cols}))


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
    # 别名归一(列限定词只有这一处口径):conds/groups/projections 后处理一遍,
    # aggs 由归一后的 projections 派生。没有可归的别名时**整段跳过** ——
    # 无别名 SQL 的事实逐字节不变。
    amap = _alias_map(stmts)
    if amap:
        conds = [Predicate(_map_cols(p.cols, amap), p.op, p.values) for p in conds]
        projections = [(n, _map_cols(c, amap)) for n, c in projections]
        groups = [_map_cols(g, amap) for g in groups]
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
    **空列集不是通配**:谓词的 cols=() 是「无列谓词」(如 `1 = 1`)这个事实,不是
    聚合条目那种「任何列都行」(`COUNT(*)` ≡ `COUNT(col)` 是设计给聚合/投影的
    等价)。故先过一道空/非空一致性门:两侧要么都空、要么都非空,否则不匹配 ——
    否则 `WHERE 1 = 1` 与 `WHERE loans.status = 1` 会被**伪造成相等**,而 filter
    档是整条梯里最承重的判定档。空-空照常走 _cols_match(两边都空 → True,
    即「无列谓词 ≡ 无列谓词」)。

    **先精确去重**:合取谓词幂等 —— `WHERE t.a = 1 AND t.a = 1` 与 `WHERE t.a = 1`
    结果集相同,不算分歧(旧 `set()` 语义)。去重只对 conds 生效:投影/聚合条目
    **相反**,重复的输出列改变结果形状(`SELECT a.x, a.x` 不是 `SELECT a.x`),
    长度不同照旧进 projection_diff —— 这个不对称是刻意的(见 :func:`_entries_match`,
    那里刻意不去重)。
    """
    gd = tuple(dict.fromkeys(g))
    od = tuple(dict.fromkeys(o))
    if len(gd) != len(od):
        return False
    remaining = list(gd)
    for pe in od:
        for i, ge in enumerate(remaining):
            if (pe.op == ge.op and pe.values == ge.values
                    and (not pe.cols) == (not ge.cols)
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


# ══ plan 侧事实(设计 §3.1 保真度 / §3.2 可比深度)═══════════
#
# 唯一口径(R21–R28):**解析不出 ≠ 计划说了「没有」**。读不出来的节**退出可比
# 集**(PlanFacts.comparable),绝不冒充「该维度无分歧」;能从形状里用统一规则读
# 出来的就读(带注解的 none、in 列表、裸函数名),一个字都不猜。


@dataclass(frozen=True)
class PlanFacts:
    """计划侧的(部分)结构事实 + **可比深度**:文本渲染只到它渲染了的节;

    截断(超 600 带标记)时最后一个出现的节可能不完整 → 从可比集里摘掉。
    可比深度不足的档位一律不判(设计 §3.2),绝不把"没渲染"读成"没有"。
    **分组刻意不可知**(``groups_known=False``):plan 不渲染分组,拿它当"没有
    GROUP BY"会冤枉计划(见 :func:`_differ` 的 agg 档)。
    """

    facts: SqlFacts
    comparable: frozenset[str]
    truncated: bool = False


#: 结构化 plan 的**键 → 档位**(R27:键在不在 = 可比不可比;缺 ``joins`` 键 ≠ 计划
#: 说「没有连接」)。键表是这份映射的**单一来源**: ``_PLAN_JSON_RUNGS`` 由它派生,
#: 两张手抄清单不许各写一份(否则迟早漂移)。
_PLAN_JSON_KEY_RUNGS: tuple[tuple[str, str], ...] = (
    ("tables", "table_set"), ("joins", "join_shape"), ("conditions", "filter_diff"),
    ("aggregation", "agg_diff"), ("answer_columns", "projection_diff"),
)
#: 计划侧的**可比档位上限**(R22,设计 §3.2):排序/取值不参与比对 —— 真实
#: ``PlanQuery.to_dict()`` 的 ``ordering`` 是 ``[{"column","direction"}]``,``str()``
#: 出来是 Python repr,拿它比只会造假分歧;``limit`` 同理(文本侧根本不渲染)。
#: gold 侧 L1 梯仍是六档,不受影响。值由键表派生,不另写一份。
_PLAN_JSON_RUNGS: frozenset[str] = frozenset(r for _, r in _PLAN_JSON_KEY_RUNGS)
#: 渲染文本的节标题 → 档位(行序固定为渲染序,截断取"最后一个出现的节"要用它)。
_PLAN_TEXT_SECTION_RUNGS: tuple[tuple[str, str], ...] = (
    ("Tables:", "table_set"),
    ("Joins:", "join_shape"),
    ("Conditions:", "filter_diff"),
    ("Aggregation:", "agg_diff"),
    ("Answer columns:", "projection_diff"),
)
#: 渲染了但**不属于六个结构档位**的节标题(query_sketch._render_plan 的余下节;
#: 设计 §3.2「排序/取值不可比」)。内容整体 parked:既不入事实也不进可比集。
#: **必须显式列名**——这些标题不在映射表里,不识别就会被折进上一个已映射节
#: (实测:`Extreme: max(client.birth_date)` 紧跟聚合节时,它的列名会被
#: :func:`_cols_from_plan_sql_fragment` 收进聚合档的列集,凭空造出分歧)。
_PLAN_TEXT_UNMAPPED_HEADERS: tuple[str, ...] = (
    "Time grain:", "Having:", "Extreme:", "Ordering:", "Analysis:", "Limit:",
)
_JOIN_EDGE_RE = re.compile(
    r"^([a-z0-9_]+)\.([a-z0-9_]+)\s*=\s*([a-z0-9_]+)\.([a-z0-9_]+)$")
_PLAN_COND_RE = re.compile(
    r"^([A-Za-z0-9_.]+)\s*(=|!=|<=|>=|<|>)\s*(.+)$")  # 值不折大小写(见 _plan_value)
_PLAN_AGG_RE = re.compile(r"^([a-z_]+)(?:\s+distinct)?\s*[\s(]([a-z0-9_.]*)")
#: 计划文本里的 `in` 条件(`in ('B','D')` / `in ['1996','1997']`,实测 3 种写法)。
_PLAN_IN_RE = re.compile(r"^([A-Za-z0-9_.]+)\s+in\s*[\(\[](.*)[\)\]]$", re.I)
#: `distinct` 认**词**不认子串(R25):`count_distinct` 里的 `distinct` 没有词边界。
_PLAN_DISTINCT_RE = re.compile(r"\bdistinct\b")
#: 聚合节的**结构化形状**(R28):`fn` / `fn(cols)` / `fn distinct cols` / `fn cols`。
#: 摘掉尾注后仍不落这个形状的 = 散文(`percentage = count(…) / count(*) * 100`),
#: 调用方把 agg 档退出可比集,绝不从散文里抽列。
_PLAN_AGG_SHAPE_RE = re.compile(
    r"^[a-z_]+(?: distinct(?: [a-z0-9_.]+)?|\s+[a-z0-9_.]+"
    r"|\([a-z0-9_.,\s]*\))?$")
#: runlog 的截断标记(`value[:600] + "…(N chars)"`,tracing/runlog.py::_fmt)。
#: 它钉在**最后一行行尾**,实测会把节标题切成两半(8 月语料原文 ``Answ…(627 chars)``)——
#: 当内容读会把它读进列名/节名,故解析前从末行摘掉:标记是 runlog 的注记,不是计划的话。
_PLAN_TRUNC_MARK_RE = re.compile(r"…\(\d+ chars\)$")


def _plan_value(v: str) -> tuple[str, ...]:
    """计划侧字面量:子查询归一为 ``"(subquery)"``(与 SQL 侧同款);**大小写原样**。

    大小写差异是事实(设计 §2.3 对 filter 档的裁量),这里折了它就再也报不出来。
    """
    v = v.strip()
    if v.lstrip("(").lower().startswith("select"):
        return ("(subquery)",)
    return (v,)


def _plan_col(name: str) -> Col:
    """计划侧列名归一:全小写;带限定则拆成 (表, 列),裸列的表留空。"""
    name = name.strip().lower()
    if "." in name:
        t, c = name.split(".", 1)
        return (t, c)
    return ("", name)


def _cols_from_plan_sql_fragment(fragment: str) -> tuple[Col, ...]:
    """从聚合表达式片段里取 ``t.c`` 形态的列(升序去重)。

    只认限定写法;裸列容差(裸 ≡ 限定)归比对层(:func:`_entries_match` 走
    PlanSignature._cols_match),这里不预折 —— 与 SQL 侧 ``_cols_of`` 同姿态。
    """
    cols = re.findall(r"([a-z0-9_]+)\.([a-z0-9_]+)", fragment.lower())
    return tuple(sorted({(t, c) for t, c in cols}))


def _is_plan_none(raw: str) -> bool:
    """渲染态空值可以带注解或尾点(入参须已小写)。

    逐字相等不够 —— 带注解的 `none` 会被读成一个**叫 `none` 的聚合函数**
    (gold 无聚合 → 伪造 agg_diff)。注解只是解释,不改「这一节说了没有」这个事实。

    尾点是**模型原文里的标点**:`_render_plan`(query_sketch.py)把模型的
    `aggregation` 值 ``str(agg)`` 直通渲染,模型写 ``"none."`` 今天就渲染成
    ``Aggregation: none.``(runlog 实测 8 处;48 条可解析计划块内 0 处 ——
    是活路径,不是历史遗留)。归一 = 去首尾空白后剥尾点,规则仍只有下面三条,
    不为尾点形态另开前缀规则。
    """
    norm = raw.strip().rstrip(".")
    return norm == "none" or norm.startswith("none ") or norm.startswith("none(")


def _is_bare_plan_none(raw: str) -> bool:
    """整条**恰为** none 记号(``none`` / ``none.``,入参须已小写)。

    就是 :func:`_is_plan_none` 三条规则里的**第一条**(归一同款:去首尾空白 + 剥尾点);
    后两条(``none `` / ``none(`` 前缀)**恰恰是「记号之后还有东西」**:在表/连接/聚合/
    输出列四节里那是注解(``none (GROUP BY …)``),在**条件行**里却可能是别的内容
    —— ``_render_plan``(query_sketch.py)把缺失的 ``field`` 原样插进
    ``  - {field} {op} {value}``,于是 ``None = 1`` / ``None in ('B','D')`` 的行首
    也 none-ish。那是**读不出的条件**,不是计划说「没有条件」(R37:读不出 ≠ 说没有)。
    """
    return raw.strip().rstrip(".") == "none"


def _plan_note_cuts(body: str) -> tuple[str, ...]:
    """渲染格式 ``field op value (note)`` 的**切法候选**(由短到长,原文殿后)。

    不给"尾注从哪开始"猜**一个**位置 —— 两侧的实测形态都会让位置规则翻车:
    `in ('B','D')` 里的 `in (` 自己就是个 ` (`,而尾注**自己也常带括号**
    (``IN ('B','D') (not eligible (finished unpaid, or running))``),从最后一个
    ` (` 切会切在注解内部(残缺注解会让可读条件降成 raw,实测 6 条),从第一个
    ` (` 切又会切进 IN 列表里。故:每个 ` (` 处给一种切法,由调用方拿各自的识别器
    逐个试,**谁先读出来用谁**;一个都读不出就原样回去(调用方记 raw / 退出该档)。
    值本身可以是子查询(``aa.k = (SELECT MAX(k) FROM aa)``,M2):它的切法读不出
    条件,原文兜底后由 :func:`_plan_value` 归一到 ``"(subquery)"``。
    """
    if not (body.endswith(")") and " (" in body):
        return (body,)
    cuts: list[str] = []
    pos = body.find(" (")
    while pos != -1:
        cuts.append(body[:pos])
        pos = body.find(" (", pos + 2)
    cuts.append(body)
    return tuple(cuts)


def _plan_condition(body: str) -> Predicate | None:
    """一行条件文本 → 谓词(含尾注切法);读不出 → ``None``。"""
    for cand in _plan_note_cuts(body):
        m = _PLAN_COND_RE.match(cand)  # 原样匹配:值的大小写是事实,不折
        if m:
            field, op, value = m.groups()
            return Predicate((_plan_col(field),), op, _plan_value(value))
        mi = _PLAN_IN_RE.match(cand)
        vals = _plan_in_values(mi.group(2)) if mi else None
        if vals:  # `in ('B','D')`:与 SQL 侧 IN 词汇表同口径(排序去引号)
            return Predicate((_plan_col(mi.group(1)),), "in", vals)
    return None


def _plan_in_values(raw: str) -> tuple[str, ...] | None:
    """``in ('B','D')`` / ``in ['1996','1997']`` → 排序、去引号的字面量元组。

    与 SQL 侧 ``_predicates`` 的 ``tuple(sorted(v for x in e.expressions
    for v in _lit(x)))`` 同口径(设计 §2.3);子查询 / 读不出 → None(调用方记
    raw 并把该档退出可比集,R24)。
    """
    if "select" in raw.lower():
        return None
    vals = [v.strip().strip("'\"").strip() for v in raw.split(",")]
    vals = [v for v in vals if v]
    return tuple(sorted(vals)) or None


def _plan_aggs(raw: str) -> tuple[tuple[tuple[str, tuple[Col, ...]], ...], bool]:
    """聚合节 → (aggs, 结构化可读?)。

    实测 11 种写法里,模型常把聚合写成**散文**:`percentage = count(…) / count(*) * 100`、
    `sum(loan.amount) grouped by year(loan.date), then growth rate = …`、
    `count (percentage = …)`。口径(**读不出 ≠ 计划说没有**):某个尾注切法下仍是
    `fn` / `fn(cols)` / `fn distinct col` 形状才可比;哪个切法都不落形状 = 散文
    → ``False``,调用方把 `agg_diff` 从可比集摘掉,绝不从散文里抽列。
    `distinct` 在摘注**前**的原文里认(R25)——`count (distinct client.client_id
    for numerator: …)` 的 distinct 是**声明**,不是注(裸函数名那一支同样认它)。
    裸函数名(`count` / `avg`)可比:它没说列 → 空列集当通配(聚合档既有的宽容
    口径),这是「未表态」而不是「说没有列」。
    """
    low = raw.strip().lower()
    if not low or _is_plan_none(low):
        return (), True
    for cand in _plan_note_cuts(low):
        if not _PLAN_AGG_SHAPE_RE.match(cand):
            continue
        m = _PLAN_AGG_RE.match(cand)
        fn = m.group(1) if m else cand             # 匹配不上 = 裸函数名
        if _PLAN_DISTINCT_RE.search(low):          # 原文里认 distinct(R25)
            fn = f"{fn}_distinct"
        cols = _cols_from_plan_sql_fragment(cand)  # 只从结构部分抽列,不碰散文
        return ((fn, cols if fn != "count_distinct" or cols else ()),), True
    return ((low[:20], ()),), False


def plan_facts_text(text: str, *, truncated: bool = False) -> PlanFacts | None:
    """解析渲染态 plan 文本(实测行序固定,见设计 §1 保真度表)。

    解析不了(空文本 / 一个已映射节都没有,如中文渲染) → None:判不了,
    绝不伪装成"计划什么都没说"。
    ``truncated`` 由调用方的保真度探测给出(设计 §1「静默截断只记录」):它决定
    截断切在节标题中间时怎么算 —— 那一节**出现了**、只是内容被切走(见 :class:`PlanFacts`)。
    """
    if not text or not text.strip():
        return None
    lines = text.splitlines()
    lines[-1] = _PLAN_TRUNC_MARK_RE.sub("", lines[-1])  # 末行的截断标记是 runlog 的注记
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for i, ln in enumerate(lines):
        head = ln.strip()
        last_line = i == len(lines) - 1
        matched = False
        for header, rung in _PLAN_TEXT_SECTION_RUNGS:
            if ln.startswith(header):
                current = rung
                sections.setdefault(rung, [])
                rest = ln[len(header):].strip()
                if rest:
                    sections[rung].append(rest)
                matched = True
                break
            if truncated and last_line and head and header.startswith(head):
                # 截断正好切在节标题中间("Answer col"):该节**出现了**、内容却被切走。
                # 只登记节名(不猜内容),随后由截断规则把这一节从可比集里摘掉 ——
                # 不认它就会把上一节误当"最后一个出现的节"摘掉,白白丢深度。
                current = rung
                sections.setdefault(rung, [])
                matched = True
                break
        if matched:
            continue
        if any(ln.startswith(h) for h in _PLAN_TEXT_UNMAPPED_HEADERS):
            current = None  # 该节(及其内容)整体 parked:不是这六个结构档位的事
            continue
        if current is not None:
            sections[current].append(ln)
    if not sections:
        return None

    tables: tuple[str, ...] = ()
    if "table_set" in sections:
        raw = " ".join(sections["table_set"]).strip()
        if raw and not _is_plan_none(raw.lower()):
            tables = tuple(sorted(t.strip().lower()
                                  for t in raw.split(",") if t.strip()))

    edges: set[tuple[tuple[str, str], tuple[str, str]]] = set()
    unresolved = 0
    if "join_shape" in sections:
        raw = " ".join(sections["join_shape"]).strip()
        if raw and not _is_plan_none(raw.lower()):
            for frag in raw.split(" AND "):
                m = _JOIN_EDGE_RE.match(frag.strip().lower())
                if not m:
                    unresolved += 1
                    continue
                t1, c1, t2, c2 = m.groups()
                edges.add(tuple(sorted(((t1, c1), (t2, c2)))))

    conds: list[Predicate] = []
    unparsed = 0  # 读不出的条件数:>0 时 filter 档整体退出可比集(R24)
    for ln in sections.get("filter_diff", []):
        body = ln.strip()
        if body.startswith("-"):
            body = body[1:].strip()
        low = body.lower()
        if not body or _is_bare_plan_none(low):
            continue    # 恰为 none 记号(可带尾点):这一节说了「没有条件」,档位仍可比
        # 行首 none-ish 但记号之后还有算子/内容(`None = 1` / `None in ('B','D')` /
        # `none (…)`):那是**读不出的条件**,不是计划说「没有条件」—— 缺失的 field 由
        # `_render_plan` 原样渲染成 `None`(非 dict 条件则整条直通),`_plan_condition`
        # 会把它解析成一条**列名叫 `none` 的谓词**。故连它都不进,照 R24 记原文并
        # `unparsed += 1`(R37:JSON 侧对缺 field 的条件同判,两路径一个裁决)。
        pred = None if _is_plan_none(low) else _plan_condition(body)
        if pred is not None:
            conds.append(pred)
            continue
        # 读不出的条件**不冒充计划说过的话**:记原文,并把 filter 档退出可比集
        # (读不出 ≠ 计划说「没有过滤」)。
        conds.append(Predicate((), "raw", (body[:60],)))
        unparsed += 1

    aggs: tuple[tuple[str, tuple[Col, ...]], ...] = ()
    agg_readable = True
    if "agg_diff" in sections:
        aggs, agg_readable = _plan_aggs(" ".join(sections["agg_diff"]))

    projections: tuple[tuple[str | None, tuple[Col, ...]], ...] = ()
    if "projection_diff" in sections:
        raw = " ".join(sections["projection_diff"]).strip()
        if raw and not _is_plan_none(raw.lower()):  # 唯一曾绕过共享函数的站点(尾点/注解同口径)
            projections = tuple(
                (None, (_plan_col(c),))
                for c in raw.split(",") if c.strip())

    comparable = {r for _, r in _PLAN_TEXT_SECTION_RUNGS if r in sections}
    if unparsed:
        comparable.discard("filter_diff")   # 读不出的条件混在里面:整档不判
    if not agg_readable:
        comparable.discard("agg_diff")      # 聚合节是散文:整档不判
    if truncated and comparable:
        last = max(comparable, key=lambda r: [x for _, x in _PLAN_TEXT_SECTION_RUNGS].index(r))
        comparable.discard(last)
    facts = SqlFacts(
        ok=True, tables=tables, join_edges=tuple(sorted(edges)),
        unresolved_joins=unresolved, conds=tuple(sorted(conds)), aggs=aggs,
        groups=(), groups_known=False, order=(), has_limit=False,
        projections=projections,
    )
    return PlanFacts(facts=facts, comparable=frozenset(comparable),
                     truncated=truncated)


def plan_facts_from_json(d: dict) -> PlanFacts:
    """结构化 plan(未来 eval_bird records 的 ``plan`` 字段):按键存在性定可比集。

    键缺席才是"这个维度没得比":键在(哪怕值为空)即代表计划就该维度表过态,
    键缺 ≠ 计划说「没有」。取值/排序**整体**不进可比集(R22:``ordering`` 是
    ``[{"column","direction"}]``,``str()`` 出来是 repr 垃圾,拿它比只造假分歧)。
    """
    tables = tuple(sorted(str(t).strip().lower()
                          for t in (d.get("tables") or ()) if str(t).strip()))
    edges: set[tuple[tuple[str, str], tuple[str, str]]] = set()
    unresolved = 0
    raw_joins = str(d.get("joins") or "").strip()
    if raw_joins and not _is_plan_none(raw_joins.lower()):
        for frag in raw_joins.split(" AND "):
            m = _JOIN_EDGE_RE.match(frag.strip().lower())
            if not m:
                unresolved += 1
                continue
            t1, c1, t2, c2 = m.groups()
            edges.add(tuple(sorted(((t1, c1), (t2, c2)))))
    conds: list[Predicate] = []
    unparsed = 0  # 形态读不出的条件:>0 时 filter 档整体退出可比集(R24)
    for c in d.get("conditions") or ():
        if not isinstance(c, dict):
            unparsed += 1        # 非 dict 条件:读不出 ≠ 计划说「没有条件」
            continue
        field = str(c.get("field") or "").strip().lower()
        if not field:
            unparsed += 1
            continue
        op = str(c.get("op") or "=").strip().lower()
        raw_val = c.get("value")
        if op == "in":
            # 与文本路径同一个 `in` 词汇表(排序去引号);list 形态先摊成同一路
            text = (",".join(map(str, raw_val))
                    if isinstance(raw_val, (list, tuple))
                    else str(raw_val or ""))
            vals = _plan_in_values(text)
            if vals:
                conds.append(Predicate((_plan_col(field),), "in", vals))
            else:
                conds.append(Predicate((_plan_col(field),), "raw", (text[:60],)))
                unparsed += 1
            continue
        conds.append(Predicate((_plan_col(field),), op,
                               _plan_value(str(raw_val or ""))))
    raw_agg = d.get("aggregation")
    if isinstance(raw_agg, (list, tuple)):
        # 多聚合列表形态(PlanQuery.aggregation 允许 str | list[str]):
        # 与 _render_plan 同一条规则摊平 —— 压成 repr 等于凭空判它读不出。
        raw_agg = ", ".join(str(a) for a in raw_agg)
    aggs, agg_readable = _plan_aggs(str(raw_agg or ""))
    projections: tuple[tuple[str | None, tuple[Col, ...]], ...] = ()
    if "answer_columns" in d:
        projections = tuple(
            (None, (_plan_col(str(c)),))
            for c in (d.get("answer_columns") or ()) if str(c).strip())
    facts = SqlFacts(
        ok=True, tables=tables, join_edges=tuple(sorted(edges)),
        unresolved_joins=unresolved, conds=tuple(sorted(conds)), aggs=aggs,
        groups=(), groups_known=False, order=(), has_limit=False,
        projections=projections,
    )
    # R30:可比集 = **键在不在**(缺 `joins` 键 ≠ 计划说「没有连接」)。档位直接取自键表
    # —— 它的值域就是这五个计划侧档,再与 `_PLAN_JSON_RUNGS` 求交是恒真的一层。
    comparable = {rung for key, rung in _PLAN_JSON_KEY_RUNGS if key in d}
    if unparsed:
        comparable.discard("filter_diff")
    if not agg_readable:
        comparable.discard("agg_diff")
    return PlanFacts(facts=facts, comparable=frozenset(comparable))


# ══ 轨迹 + 责任跳 + loop 三态(设计 §3.1–§3.3)══════════════


@dataclass(frozen=True)
class Trajectory:
    """一道题的一条执行轨迹(归一化字段子集;逐字段保真靠 available/state)。

    ``matched_tables_state``/``plan_text_state`` 三态 present|empty|absent:
    "观察到空"是事实(linker 什么都没给),"从未出现"才是判不了——两者混同
    会把空缺洗成通过(设计 §3.1)。
    """

    question: str
    source: str = ""
    matched_tables: tuple[str, ...] = ()
    matched_tables_state: str = "absent"
    plan_facts: PlanFacts | None = None
    plan_text_state: str = "absent"
    plan_truncated: bool = False
    validation_hits_raw: str = ""
    error_analysis: str = ""
    rollback_target: str = ""
    retry_count: int | None = None
    available: frozenset[str] = frozenset()


def responsibility(*, gold: SqlFacts, pred: SqlFacts | None, l1: LadderVerdict,
                   traj: Trajectory | None) -> tuple[str, str]:
    """L2 责任跳(设计 §3.2):互斥,首个命中即桶。

    原理:给 plan 也跑同一把 L1 尺子——plan 是生成器被要求执行的规格。
    """
    if traj is None:
        return "undecidable", "无轨迹"
    if traj.matched_tables_state == "absent" and traj.plan_facts is None:
        return "undecidable", "matched_tables 与 plan 都不可得"
    if traj.matched_tables_state != "absent":
        missed = [t for t in gold.tables
                  if t not in traj.matched_tables
                  and (pred is None or t not in pred.tables)]
        if missed:
            return "linking_missed_table", (
                f"gold 需要的表未进 matched_tables 且 pred 未读: {', '.join(missed)}")
    pf = traj.plan_facts
    if pf is None:
        return "undecidable", "matched_tables 未含缺表,plan 不可得"
    # R26:计划侧输出列**只比个数** —— plan 的 answer_columns 是名字/别名,gold 的
    # 投影带函数标签(`sum`/`count`),逐名比会把「计划本来就对」判成分歧(实测
    # 39/48 行聚合题全中,1 行因此翻桶);个数分歧才是信号:8 月语料 6/48 行
    # plan 写了 2 列、gold 只要 1 列 —— 正是 CLAUDE.md 记的头号失败模式
    # (query_sketch 的 answer_columns 定错输出列)。
    plan_diffs = _diff_rungs(gold, pf.facts, pf.comparable - {"projection_diff"})
    if "projection_diff" in pf.comparable and (
            len(gold.projections) != len(pf.facts.projections)):
        plan_diffs.append("projection_diff")   # 投影档恒在末位,追加保序
    if plan_diffs:
        return "planner_chose_differently", (
            f"plan 相对 gold 首个分歧={plan_diffs[0]}(L1 首个分歧={l1.bucket})")
    if l1.bucket in pf.comparable or l1.bucket in (
            "no_sql", "pred_unparseable", "exec_error"):
        return "gen_ignored_plan", (
            f"plan 在可比深度内与 gold 无分歧,SQL 侧分歧={l1.bucket}")
    if l1.bucket not in (*_RUNG_ORDER, "no_sql", "pred_unparseable", "exec_error"):
        # R17:结构梯看不见的分歧不归给任何一跳,理由如实说——
        # "可比深度不含 X" 这句只对「梯子看得见但 plan 没渲染」成立。
        if l1.bucket == "structure_equal":
            return "undecidable", (
                "六个结构维度全同(SQL 与 gold 无结构分歧):"
                "失败不在结构层,不归给生成链的任何一跳")
        return "undecidable", (
            f"L1 判不了({l1.reason or l1.bucket}),责任跳无从谈起")
    return "undecidable", (
        f"plan 可比深度 {sorted(pf.comparable)} 不含 L1 分歧档 {l1.bucket}")


def loop_state(traj: Trajectory | None) -> str:
    """「链路自知过吗」三态(设计 §3.3):与责任跳正交,单独计。"""
    if traj is None:
        return "undecidable"
    signals = {"validation_hits", "error_analysis", "rollback_target", "retry_count"}
    if not (signals & set(traj.available)):
        return "undecidable"
    detected = (
        traj.validation_hits_raw.strip() not in ("", "[]")
        or bool(traj.error_analysis.strip())
        or bool(traj.rollback_target.strip())
        or (traj.retry_count or 0) > 0
    )
    return "detected_but_unfixed" if detected else "never_detected"


# ══ 逐行归因 + 报告(设计 §4 报告键 / §5 文本分节)═════════════
#
# 报告为「改进决策」而写:同一份语料要能回答两件事 —— (a) 失败集中在链路的哪一
# 跳,(b) 这个数字有多少折扣。故计数成对出现(**行 / 题**)、保真度与观察码并列
# 在覆盖节、样本让读者能回到具体题目。
#
# **行与题双计数是承重的**:97 行只对应 24 题,行数偏袒被反复重试的题 —— 只看行
# 计数会把「一题重试四次」读成「四题都错在这里」。两个数都报,读的人自己选口径。


@dataclass(frozen=True)
class ChainFinding:
    """一题一行(一个 failure 行)的归因结论:L1 档位 + L2 责任跳 + loop 三态。"""

    question: str
    l1: LadderVerdict
    l2: str = "undecidable"
    l2_reason: str = ""
    loop: str = "undecidable"
    source: str = ""
    flags: tuple[str, ...] = ()


def attribute_row(row: FailureRow, traj: Trajectory | None, *,
                  dialect: str = "mysql") -> ChainFinding:
    """一题一行 → L1 档位 + L2 责任跳 + loop 三态(设计 §2/§3)。

    L2 只在 **gold 可判** 时才谈得上:gold 解析不了就无从说「计划本该做什么」,
    故先答「gold 不可判」,再谈有无轨迹 —— 两种判不了的原因不同,不能混成一句。
    ``flags`` 原样承接 ``l1.observations``(跨档观察码,不换桶)。今天它只产出四个
    码(``needed_table_never_read`` / ``extra_table_read`` / ``extreme_style_mismatch``
    / ``case_only_value_diff``);设计 §5 观察块另列的两项**不铺**(R33):``paths``
    是未来 records 的字段(今天没有生产者,铺上去只能是空字典),``compile_miss``
    今天在 failures 语料里没有生产者(编译期被拒的题走 ``query.refused`` 审计与
    results.jsonl)—— 硬写只能从错误文本正则猜,那是把「读到 0」洗成「没有编译期
    失败」。判据与 R20 同源:**没有生产者的字段不许铺**。
    """
    gold = sql_facts(row.gold_sql, dialect)
    pred = sql_facts(row.pred_sql, dialect)
    l1 = ladder(gold, pred, verdict=row.verdict,
                had_sql=bool(row.pred_sql.strip()))
    l2, reason = "undecidable", "gold 不可判" if (gold is None or not gold.ok) else "无轨迹"
    if gold is not None and gold.ok:
        # no_sql 行的 pred_sql 是上一轮残留(failures.jsonl 原样留档的口径):
        # 拿它当「已读表」会把 linking_missed_table 压成 gen_ignored_plan。
        l2, reason = responsibility(
            gold=gold, pred=None if l1.bucket == "no_sql" else pred,
            l1=l1, traj=traj)
    return ChainFinding(
        question=row.question, l1=l1, l2=l2, l2_reason=reason,
        loop=loop_state(traj), source=(traj.source if traj else ""),
        flags=l1.observations,
    )


def _bucket_counts(
        findings: list[ChainFinding], buckets: tuple[str, ...],
        key: Callable[[ChainFinding], str]) -> tuple[dict[str, int], dict[str, int]]:
    """按桶聚合(行数 / 题数)。空桶不发键 —— 报告里「没有」就是键不在。"""
    rows = {b: 0 for b in buckets}
    qs = {b: set() for b in buckets}
    for f in findings:
        b = key(f)
        if b in rows:
            rows[b] += 1
            qs[b].add(f.question)
    return ({b: n for b, n in rows.items() if n},
            {b: len(v) for b, v in qs.items() if v})


def build_report(rows: list[FailureRow], trajs: dict[str, Trajectory], *,
                 dialect: str = "mysql", meta: dict | None = None,
                 samples: int = 3) -> dict:
    """组装报告(纯函数:同输入 → 同输出,零 IO 零 LLM)。

    逐键确定性:桶按常量表序(空桶不发键)、观察块与样本渲染按名字排序、样本按
    **输入序**取每桶前 ``samples`` 条(输入序是语料自己的序,不是集合迭代序)。
    轨迹按 ``row.question.strip()`` 对齐(失败行的问句带空白是常态;对不上就是没
    轨迹,不是"没有 plan")。
    """
    findings: list[ChainFinding] = []
    for r in rows:
        traj = trajs.get(r.question.strip())
        findings.append(attribute_row(r, traj, dialect=dialect))
    l1_rows, l1_q = _bucket_counts(findings, L1_BUCKETS, lambda f: f.l1.bucket)
    l2_rows, l2_q = _bucket_counts(findings, L2_BUCKETS, lambda f: f.l2)
    loop_rows, _ = _bucket_counts(findings, LOOP_STATES, lambda f: f.loop)
    obs: dict[str, int] = {}
    for f in findings:
        for code in f.flags:
            obs[code] = obs.get(code, 0) + 1
    fidelity = {
        "matched_tables": {"present": 0, "empty": 0, "absent": 0},
        "plan": {"present": 0, "empty": 0, "absent": 0},
    }
    for f in findings:
        t = trajs.get(f.question.strip())
        if t is None:
            fidelity["matched_tables"]["absent"] += 1
            fidelity["plan"]["absent"] += 1
            continue
        fidelity["matched_tables"][t.matched_tables_state] = (
            fidelity["matched_tables"].get(t.matched_tables_state, 0) + 1)
        fidelity["plan"][t.plan_text_state] = (
            fidelity["plan"].get(t.plan_text_state, 0) + 1)
    loop_by_l2: dict[str, dict[str, int]] = {}
    for f in findings:
        loop_by_l2.setdefault(f.l2, {})
        loop_by_l2[f.l2][f.loop] = loop_by_l2[f.l2].get(f.loop, 0) + 1
    sample_bag: dict[str, dict[str, list]] = {"l1": {}, "l2": {}}
    if samples:
        for f in findings:
            for bucket_key, bucket in (("l1", f.l1.bucket), ("l2", f.l2)):
                bag = sample_bag[bucket_key].setdefault(bucket, [])
                if len(bag) < samples:
                    bag.append({"question": f.question, "reason": f.l1.reason,
                                "l2_reason": f.l2_reason})
    return {
        "meta": dict(meta or {}),
        "counts": {
            "rows": len(findings),
            "questions": len({f.question for f in findings}),
            "l1": l1_rows, "l1_by_question": l1_q,
            "l2": l2_rows, "l2_by_question": l2_q,
            "loop": loop_rows, "loop_by_l2": loop_by_l2,
        },
        "coverage": {
            "l1_judged_rows": sum(
                n for b, n in l1_rows.items() if b != "undecidable"),
            "l2_judged_rows": sum(
                n for b, n in l2_rows.items() if b != "undecidable"),
            # R20:plan 被静默/带标记截断的行数——保真度的一部分,
            # 不报出来 plan 深度就有一个看不见的天花板
            "plan_truncated_rows": sum(
                1 for f in findings
                if (trajs.get(f.question.strip()) is not None
                    and trajs[f.question.strip()].plan_truncated)),
            "fidelity": fidelity,
        },
        "observations": dict(sorted(obs.items())),
        "samples": sample_bag,
    }


def render_report(report: dict) -> str:
    """确定性的文本渲染(同输入 → 同输出;无时间戳)。

    分节:语料 → L1 表 → L2 表 → loop 交叉 → 覆盖 → 观察 → 样本。每个集合都显式
    排序(或按常量表序);空桶不出行。行计数与题计数同行给出(``行 / 题``)。
    """
    counts = report["counts"]
    lines: list[str] = []
    meta = report.get("meta") or {}
    if meta:
        lines.append("== 语料 ==")
        for k in sorted(meta):
            lines.append(f"  {k}: {meta[k]}")
    lines.append("== L1 结构档位(行数 / 题数)==")
    for b in L1_BUCKETS:
        n = counts["l1"].get(b, 0)
        if n:
            lines.append(f"  {b:20s} {n:4d} / {counts['l1_by_question'].get(b, 0)}")
    lines.append("== L2 责任跳(行数 / 题数)==")
    for b in L2_BUCKETS:
        n = counts["l2"].get(b, 0)
        if n:
            lines.append(f"  {b:28s} {n:4d} / {counts['l2_by_question'].get(b, 0)}")
    lines.append("== loop 三态 × L2 ==")
    for b, m in sorted(counts.get("loop_by_l2", {}).items()):
        detail = " ".join(f"{k}={v}" for k, v in sorted(m.items()))
        lines.append(f"  {b:28s} {detail}")
    lines.append("== 覆盖 ==")
    cov = report["coverage"]
    lines.append(f"  L1 可判行={cov['l1_judged_rows']} · L2 可判行={cov['l2_judged_rows']}"
                 f" · plan 被截断行={cov['plan_truncated_rows']}")
    for field, m in cov["fidelity"].items():
        lines.append(f"  fidelity[{field}] " +
                     " ".join(f"{k}={v}" for k, v in sorted(m.items())))
    lines.append("== 观察(跨档,给读者的折扣尺)==")
    for code, n in sorted((report.get("observations") or {}).items()):
        lines.append(f"  {code}: {n}")
    lines.append("== 样本 ==")
    for kind in ("l1", "l2"):
        for bucket, bag in sorted((report.get("samples") or {}).get(kind, {}).items()):
            if bag:
                lines.append(f"  [{kind}:{bucket}]")
                for s in bag:
                    lines.append(f"    - {s['question'][:80]} · {s['reason']}")
    return "\n".join(lines) + "\n"
