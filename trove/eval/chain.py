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


#: 结构化 plan(未来 eval_bird records):完整 dict → 六个档位全可比。
_PLAN_JSON_RUNGS: frozenset[str] = frozenset(_RUNG_ORDER)
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
        if raw and raw.lower() != "none":
            tables = tuple(sorted(t.strip().lower()
                                  for t in raw.split(",") if t.strip()))

    edges: set[tuple[tuple[str, str], tuple[str, str]]] = set()
    unresolved = 0
    if "join_shape" in sections:
        raw = " ".join(sections["join_shape"]).strip()
        if raw and raw.lower() != "none":
            for frag in raw.split(" AND "):
                m = _JOIN_EDGE_RE.match(frag.strip().lower())
                if not m:
                    unresolved += 1
                    continue
                t1, c1, t2, c2 = m.groups()
                edges.add(tuple(sorted(((t1, c1), (t2, c2)))))

    conds: list[Predicate] = []
    for ln in sections.get("filter_diff", []):
        body = ln.strip()
        if body.startswith("-"):
            body = body[1:].strip()
        if not body or body.lower() == "none":
            continue
        if body.endswith(")") and " (" in body:
            body = body[: body.rindex(" (")]  # 摘掉尾注
        m = _PLAN_COND_RE.match(body)  # 原样匹配:值的大小写是事实,不折
        if not m:
            conds.append(Predicate((), "raw", (body[:60],)))
            continue
        field, op, value = m.groups()
        conds.append(Predicate((_plan_col(field),), op, _plan_value(value)))

    aggs: tuple[tuple[str, tuple[Col, ...]], ...] = ()
    if "agg_diff" in sections:
        raw = " ".join(sections["agg_diff"]).strip().lower()
        if raw and raw != "none":
            m = _PLAN_AGG_RE.match(raw)
            if m:
                fn = m.group(1)
                if "distinct" in raw:
                    fn = f"{fn}_distinct"
                cols = _cols_from_plan_sql_fragment(raw)
                aggs = ((fn, cols if fn != "count_distinct" or cols else ()),)
            else:
                aggs = ((raw[:20], ()),)

    projections: tuple[tuple[str | None, tuple[Col, ...]], ...] = ()
    if "projection_diff" in sections:
        raw = " ".join(sections["projection_diff"]).strip()
        if raw and raw.lower() != "none":
            projections = tuple(
                (None, (_plan_col(c),))
                for c in raw.split(",") if c.strip())

    comparable = {r for _, r in _PLAN_TEXT_SECTION_RUNGS if r in sections}
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
    """结构化 plan(未来 eval_bird records 的 ``plan`` 字段):完整 dict → 全档可比。

    键缺席才是"这个维度没得比":``ordering``/``answer_columns`` 键在(哪怕值为空)
    即代表计划就该维度表过态。取值/排序同样不进可比集(``order_limit_diff`` 只认
    ``ordering`` 键,不认 ``extreme`` —— 极值风格不在这六个结构维度里)。
    """
    tables = tuple(sorted(str(t).strip().lower()
                          for t in (d.get("tables") or ()) if str(t).strip()))
    edges: set[tuple[tuple[str, str], tuple[str, str]]] = set()
    unresolved = 0
    raw_joins = str(d.get("joins") or "").strip()
    if raw_joins and raw_joins.lower() != "none":
        for frag in raw_joins.split(" AND "):
            m = _JOIN_EDGE_RE.match(frag.strip().lower())
            if not m:
                unresolved += 1
                continue
            t1, c1, t2, c2 = m.groups()
            edges.add(tuple(sorted(((t1, c1), (t2, c2)))))
    conds: list[Predicate] = []
    for c in d.get("conditions") or ():
        if not isinstance(c, dict):
            continue
        field = str(c.get("field") or "").strip().lower()
        op = str(c.get("op") or "=").strip().lower()
        if not field:
            continue
        conds.append(Predicate((_plan_col(field),), op,
                               _plan_value(str(c.get("value") or ""))))
    raw_agg = str(d.get("aggregation") or "").strip().lower()
    aggs: tuple[tuple[str, tuple[Col, ...]], ...] = ()
    if raw_agg and raw_agg != "none":
        m = _PLAN_AGG_RE.match(raw_agg)
        if m:
            fn = m.group(1)
            if "distinct" in raw_agg:
                fn = f"{fn}_distinct"
            cols = _cols_from_plan_sql_fragment(raw_agg)
            aggs = ((fn, () if fn == "count_distinct" and not cols else cols),)
        else:
            aggs = ((raw_agg[:20], ()),)
    projections: tuple[tuple[str | None, tuple[Col, ...]], ...] = ()
    if "answer_columns" in d:
        projections = tuple(
            (None, (_plan_col(str(c)),))
            for c in (d.get("answer_columns") or ()) if str(c).strip())
    has_order_key = "ordering" in d
    order_raw = str(d.get("ordering") or "").strip().lower()
    order: tuple[tuple[str, str], ...] = ()
    if order_raw:
        parts = order_raw.replace(",", " ").split()
        if parts:
            order = ((parts[0].split(".")[-1], "desc" if "desc" in parts[1:]
                      else "asc"),)
    facts = SqlFacts(
        ok=True, tables=tables, join_edges=tuple(sorted(edges)),
        unresolved_joins=unresolved, conds=tuple(sorted(conds)), aggs=aggs,
        groups=(), groups_known=False, order=order, has_limit=False,
        projections=projections,
    )
    comparable = set(_PLAN_JSON_RUNGS)
    if not has_order_key:
        comparable.discard("order_limit_diff")
    if "answer_columns" not in d:
        comparable.discard("projection_diff")
    return PlanFacts(facts=facts, comparable=frozenset(comparable))
