"""Deterministic result/SQL verification rules (no LLM involved).

This is the code-side verify_step assertion layer: what can be checked
deterministically must not be left to an LLM judge. Rule failures route
back to gen_sql through the shared error_feedback correction channel.

Structure: every check is a named `Rule` registered on `_RULES` in
definition order — the first failing rule wins, so order matters (most
specific/cheapest first). `verify()` runs the registry and returns the
reason plus a structured hit record for observability; `validate()` is
a thin string-returning wrapper kept for backward compatibility.

Families:
  - F1  shape    — single-value questions must return one row/column;
                  list questions must not dump unrelated data columns
  - F4  ordering — ORDER BY direction must match the question's intent
  - F2  filters  — entities named in the question (gender / year /
                  region / operation) must appear as SQL conditions
  - F3  values   — dtype probes, ID uniqueness, value ranges

Patterns are deliberately narrow to avoid false positives; questions
matching no rule pass through untouched.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from trove.core.i18n import L

_TOP = r"(?:\d+|[一二三四五六七八九十]+|ten|nine|eight|seven|six|five|four|three|two|one)"

_COUNT_PATTERNS = [
    re.compile(r"\bhow many\b", re.I),
    re.compile(r"\bnumber of\b", re.I),
    re.compile(r"\bwhat is the total\b", re.I),
    re.compile(r"有多少|多少个|总数|总数量"),
]
# grouped counts ("how many X per Y", "哪个地区…总数", "each branch") are not
# single-value count questions
_COUNT_GROUP_GUARD = re.compile(
    r"\bper\b|group by|每个|按.{0,6}分|哪个|哪些|\bwhich\b|\beach\b", re.I,
)

# questions asking for several metrics at once ("总数和平均") are not counts
_MULTI_METRIC_GUARD = re.compile(
    r"总数.{0,8}(?:平均|最大|最小|最高|最低)|(?:平均|最大|最小|最高|最低).{0,8}总数",
    re.I,
)

_LIST_PATTERNS = [
    re.compile(r"\blist\b", re.I),
    re.compile(r"\bwhich\b", re.I),
    re.compile(rf"\btop\s*{_TOP}\b", re.I),
    re.compile(r"\bname\s+(?:the|all|me)\b", re.I),  # "Name the account numbers..."
    re.compile(rf"列出|哪些|前\s*{_TOP}"),
]

_PERCENT_PATTERNS = [
    re.compile(r"percent", re.I),
    re.compile(r"百分比|占比|比例"),
]

_ORDERED_PATTERNS = [
    re.compile(rf"\btop\s*{_TOP}\b", re.I),
    re.compile(rf"前\s*{_TOP}"),
    re.compile(r"排名"),
]

_LIMIT_RE = re.compile(r"\blimit\b", re.I)
_ORDER_BY_RE = re.compile(r"\border\s+by\b", re.I)

_EXTREME_Q_RE = re.compile(
    r"\b(?:lowest|highest|minimum|maximum|smallest|largest|cheapest|most expensive)\b"
    r"|最低|最高|最小|最大",
    re.I,
)


def _scope_ambiguity(sql: str, question: str) -> bool:
    """极值作用域歧义:外层 WHERE 存在"极值子查询内没有等价条件"的过滤。

    即过滤条件被排除在极值作用域外(应先过滤再取极值)。过滤已正确
    放进子查询内的写法(外层冗余重复,别名可不同)不算歧义——按
    列名+字面量归一化等价比较,防误报回查循环。问题须含极值词才介入。
    """
    if not _EXTREME_Q_RE.search(question):
        return False
    try:
        from sqlglot import exp, parse_one
        tree = parse_one(sql)
    except Exception:
        return False
    selects = list(tree.find_all(exp.Select))
    if not selects:
        return False
    outer, subqueries = selects[0], selects[1:]

    def _has_extreme(select) -> bool:
        return any(
            isinstance(n, (exp.Min, exp.Max)) for n in select.find_all(exp.AggFunc)
        )

    extreme_queries = [s for s in subqueries if _has_extreme(s)]
    if not extreme_queries:
        return False
    where = outer.args.get("where")
    if where is None:
        return False
    outer_conds = [
        c for c in _split_where(where)
        if not any(isinstance(n, exp.Subquery) for n in c.walk())
    ]
    in_extreme_conds = []
    for s in extreme_queries:
        sub_where = s.args.get("where")
        if sub_where is not None:
            in_extreme_conds.extend(_split_where(sub_where))
    return any(
        not any(_cond_equivalent(c, e) for e in in_extreme_conds)
        for c in outer_conds
    )


def _cond_equivalent(a, b) -> bool:
    """两个条件是否等价(忽略表别名与写法差异):列名相交 + 字面量集合相等。"""
    from sqlglot import exp

    def _columns(cond) -> set[str]:
        return {c.name.lower() for c in cond.find_all(exp.Column) if c.name}

    def _literals(cond) -> set[str]:
        return {
            str(lit.this).strip().lower()
            for lit in cond.find_all(exp.Literal) if lit.this is not None
        }

    if not _columns(a) or not _columns(b):
        return False
    return bool(_columns(a) & _columns(b)) and _literals(a) == _literals(b)


def _split_where(where) -> list:
    """顶层 AND 拆分 WHERE 条件(OR 保持整体,不跨层拆)。"""
    from sqlglot import exp

    if isinstance(where, exp.Where):
        where = where.this
    conds: list = []
    stack = [where]
    while stack:
        node = stack.pop()
        if isinstance(node, exp.And):
            stack.append(node.this)
            stack.append(node.expression)
        else:
            conds.append(node)
    return conds


def _matches(patterns: list[re.Pattern], question: str) -> bool:
    return any(p.search(question) for p in patterns)


def is_count_question(question: str) -> bool:
    """Single-value count question (excludes grouped and multi-metric shapes).

    List questions win on precedence: "List the no. of X" is a list
    question whose gold result is many rows, not a single count.
    Grouped shapes ("哪个地区…总数", "each branch") and multi-metric
    conjunctions ("总数和平均") are excluded too.
    """
    if _matches(_LIST_PATTERNS, question):
        return False
    if _COUNT_GROUP_GUARD.search(question) or _MULTI_METRIC_GUARD.search(question):
        return False
    return _matches(_COUNT_PATTERNS, question)


def is_list_question(question: str) -> bool:
    """List/top-N question expecting multiple rows."""
    return _matches(_LIST_PATTERNS, question)


def is_percent_question(question: str) -> bool:
    """Percentage/proportion question (result should be a 0-100 number)."""
    return _matches(_PERCENT_PATTERNS, question)


def is_ordered_question(question: str) -> bool:
    """Question whose result order matters (top-N / ranking)."""
    return _matches(_ORDERED_PATTERNS, question)


# ── verify_step 断言注册表 ──────────────────────────────────────


@dataclass(frozen=True)
class Rule:
    """One named deterministic assertion."""

    name: str
    fn: Callable[[str, str, list, list, int, str], str | None]


_RULES: list[Rule] = []


def _rule(name: str):
    """Register a rule (definition order = evaluation order)."""

    def deco(fn):
        _RULES.append(Rule(name=name, fn=fn))
        return fn

    return deco


def verify(
    question: str,
    sql: str,
    columns: list,
    rows: list[list],
    row_count: int,
    lang: str = "zh",
) -> tuple[str | None, list[dict]]:
    """Run the assertion registry; return (reason, hits).

    The first failing rule wins; its reason carries the rule name
    prefix (e.g. ``[F1-b] ...``) so downstream feedback and eval logs
    can attribute the interception. hits records the structured
    (name, reason) pair for observability. A rule bug must never
    crash the pipeline — exceptions degrade to pass.
    """
    hits: list[dict] = []
    for rule in _RULES:
        try:
            reason = rule.fn(question, sql, columns, rows, row_count, lang)
        except Exception:
            reason = None
        if reason:
            prefixed = f"[{rule.name}] {reason}"
            hits.append({"name": rule.name, "reason": prefixed})
            return prefixed, hits
    return None, hits


def validate(
    question: str,
    sql: str,
    columns: list,
    rows: list[list],
    row_count: int,
    lang: str = "zh",
) -> str | None:
    """Backward-compatible wrapper: return only the failure reason."""
    reason, _ = verify(question, sql, columns, rows, row_count, lang)
    return reason


# ── 既有基础规则(保持原语义,仅迁移为注册表) ──────────────────────


@_rule("count-shape")
def _rule_count_shape(
    question: str, sql: str, columns: list, rows: list[list], row_count: int, lang: str,
):
    """计数题必须返回单个数值:单行单列、非 NULL、可解析为数值、非负。"""
    if not is_count_question(question):
        return None
    if row_count != 1 or len(columns) != 1 or not rows or len(rows[0]) != 1:
        return L(
            lang,
            "计数问题应返回单个数字（单行单列）",
            "count question should return a single number (single row, single column)",
        )
    value = rows[0][0]
    if value is None:
        return L(lang, "计数问题返回了 NULL", "count question returned NULL")
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return L(
            lang,
            "计数问题应返回数值（得到的是非数值）",
            "count question should return a numeric value (got a non-number)",
        )
    if numeric < 0:
        return L(lang, "计数问题返回了负值", "count question returned a negative value")
    return None


@_rule("list-zero-rows")
def _rule_list_zero_rows(
    question: str, sql: str, columns: list, rows: list[list], row_count: int, lang: str,
):
    """列表题返回零行几乎必然是 join/过滤/表名错误。"""
    if is_list_question(question) and row_count == 0:
        return L(
            lang,
            "列表问题返回零行——检查 join/过滤条件/表名",
            "list question returned no rows — check joins/filters/table names",
        )
    return None


@_rule("percent-range")
def _rule_percent_range(
    question: str, sql: str, columns: list, rows: list[list], row_count: int, lang: str,
):
    """百分比题的单值结果必须在 [0, 100]。"""
    if is_percent_question(question) and row_count == 1 and rows and len(rows[0]) == 1:
        value = rows[0][0]
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return L(lang, "百分比结果应为数值", "percentage result should be a numeric value")
        if not 0 <= numeric <= 100:
            return L(lang, "百分比结果超出 0-100 范围", "percentage result out of 0-100 range")
    return None


@_rule("ratio-int-division")
def _rule_ratio_int_division(
    question: str, sql: str, columns: list, rows: list[list], row_count: int, lang: str,
):
    """比率题的整数除法陷阱:MySQL 截断到 4 位小数,必须显式 CAST DOUBLE。"""
    ratio_question = is_percent_question(question) or bool(
        re.search(r"\brate\b", question, re.I)
    )
    if ratio_question and "/" in sql and not re.search(r"\b(?:DOUBLE|FLOAT|DECIMAL)\b", sql, re.I):
        return L(
            lang,
            "比率计算疑似整数除法(MySQL 会截断到 4 位小数,如 44.2623 而非 "
            "44.26229508196721)。请改为 CAST(SUM(...) AS DOUBLE) * 100 / COUNT(...),"
            "显式转成浮点再除。",
            "Ratio calculation appears to use integer division (MySQL truncates "
            "to 4 decimal places, e.g. 44.2623 instead of 44.26229508196721). "
            "Rewrite as CAST(SUM(...) AS DOUBLE) * 100 / COUNT(...) — cast to "
            "floating point BEFORE dividing.",
        )
    return None


@_rule("rate-shape")
def _rule_rate_shape(
    question: str, sql: str, columns: list, rows: list[list], row_count: int, lang: str,
):
    """「what is the increase/growth rate」问单一比率值,结果必须单行单列。"""
    if re.search(r"\bwhat (?:is|was) the (?:increase|growth) rate\b", question, re.I) \
            and (row_count != 1 or len(columns) != 1):
        return L(
            lang,
            "问题问的是单一比率值——结果应为单行单列,不要附带中间值列。",
            "The question asks for a single rate value — the result must be one "
            "row and one column; drop any intermediate value columns.",
        )
    return None


@_rule("limit-without-order")
def _rule_limit_without_order(
    question: str, sql: str, columns: list, rows: list[list], row_count: int, lang: str,
):
    """top-N/排名题带 LIMIT 必须带 ORDER BY。"""
    if (
        is_ordered_question(question)
        and _LIMIT_RE.search(sql)
        and not _ORDER_BY_RE.search(sql)
    ):
        return "top-N query uses LIMIT without ORDER BY"
    return None


@_rule("scope-ambiguity")
def _rule_scope_ambiguity(
    question: str, sql: str, columns: list, rows: list[list], row_count: int, lang: str,
):
    """极值作用域歧义(MIN/MAX 子查询 + 外层过滤条件)。"""
    if _scope_ambiguity(sql, question):
        return L(
            lang,
            "疑似范围歧义：存在 MIN/MAX 子查询且外层还有过滤条件——"
            "确认这些条件是否应先进入子查询作用域（先过滤再取极值）",
            "possible scope ambiguity: a MIN/MAX subquery coexists with outer "
            "filters — verify the filters should apply inside the subquery "
            "(filter first, then take the extreme)",
        )
    return None


# ── F1 形状断言 ──────────────────────────────────────────────────

# 「with the biggest number of inhabitants」这类极值限定语 → 单一实体
_SUPERLATIVE_QUALIFIER = re.compile(
    r"\bwith (?:the )?(?:biggest|largest|highest|lowest|smallest|fewest|greatest|most|least)"
    r"(?: number of)?\b",
    re.I,
)
# 分组题守卫:「per district / each branch / 每个」→ 多行合法
_GROUP_GUARD = re.compile(r"\bper\b|\beach\b|for each|按.{0,6}分|每个", re.I)
# 宽输出守卫:「with their dates and amounts」显式要求多列 → 不拦;
# 「account ID, district name and region」逗号枚举多实体 → 不拦。
# 枚举形态限「, X Y and」短名词短语(2-4 词):「in debt, list the district
# of the and the state」是主句分隔逗号,不算枚举。
_WIDE_OUTPUT_GUARD = re.compile(
    r"\bwith their\b|\balong with\b|\bas well as\b|\bincluding\b|\bin addition\b"
    r"|\btogether with\b|,\s*[^\s,]+(?: [^\s,]+){1,3}\s+and\b",
    re.I,
)
# 单值数值问题:「what is the average/total/rate/...」
_NUMERIC_SINGLE_Q = re.compile(
    r"\bwhat (?:is|was) the (?:average|total|sum|amount|rate|growth|increase)\b",
    re.I,
)


@_rule("F1-a")
def _f1a(question: str, sql: str, columns: list, rows: list[list], row_count: int, lang: str):
    """单值题 + 极值限定语 + 返回多行 → 极值限定语指定的是单个实体,结果应是单值。"""
    if not is_percent_question(question) and not re.search(
        r"\b(?:rate|growth|average|total|sum)\b", question, re.I
    ):
        return None
    if not _SUPERLATIVE_QUALIFIER.search(question):
        return None
    if _GROUP_GUARD.search(question):
        return None
    if row_count <= 1:
        return None
    return L(
        lang,
        f"问题含「the ... with the biggest/highest/...」极值限定语,答案应是单值,"
        f"但结果返回了 {row_count} 行——检查是否按分组展开了,应去掉 GROUP BY。",
        f"The question names a single entity with a superlative qualifier "
        f"(biggest/highest/...), so the answer should be a single value, but "
        f"the result has {row_count} rows — drop the GROUP BY.",
    )


@_rule("F1-b")
def _f1b(question: str, sql: str, columns: list, rows: list[list], row_count: int, lang: str):
    """列表题返回过多列 → 问题通常只要求 1-2 个实体列,多列是整表导出/带了中间值列。"""
    if not is_list_question(question):
        return None
    if _GROUP_GUARD.search(question) or _WIDE_OUTPUT_GUARD.search(question):
        return None
    if len(columns) <= 2:
        return None
    return L(
        lang,
        f"列表问题返回了 {len(columns)} 列(期望 ≤2 个实体列)——疑似整表导出"
        f"或附带中间值列。只保留问题问到的实体列。",
        f"List question returned {len(columns)} columns (expected ≤2 entity "
        f"columns) — likely a full-table dump or intermediate value columns. "
        f"Keep only the columns the question asks for.",
    )


@_rule("F1-d")
def _f1d(question: str, sql: str, columns: list, rows: list[list], row_count: int, lang: str):
    """单值数值问题返回 NULL → 聚合/过滤写错或查无数据。"""
    if not _NUMERIC_SINGLE_Q.search(question):
        return None
    if row_count != 1 or len(columns) != 1 or not rows or len(rows[0]) != 1:
        return None
    if rows[0][0] is not None:
        return None
    return L(
        lang,
        "单值数值问题返回了 NULL——检查聚合写法(COUNT 保护)与过滤条件。",
        "Single-value numeric question returned NULL — check the aggregation "
        "and filter conditions.",
    )


# ── F4 排序语义断言 ──────────────────────────────────────────────

_DIRECTION_WORDS = re.compile(
    r"\bdescending\b|\bascending\b|from (?:the )?highest to (?:the )?lowest\b"
    r"|from (?:the )?lowest to (?:the )?highest\b",
    re.I,
)
# ORDER BY 方向:允许括号(COUNT(*))与引号,不看分号
_ORDER_DIR_ASC = re.compile(r"\border\s+by\b[^;]*?\basc\b", re.I)
_ORDER_DIR_DESC = re.compile(r"\border\s+by\b[^;]*?\bdesc\b", re.I)

_TOP_N = re.compile(
    r"\btop\s+(?:(\d+)|(one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|fifty|hundred))\b",
    re.I,
)
_NUM_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "fifteen": 15, "twenty": 20, "fifty": 50, "hundred": 100,
}


@_rule("F4-a")
def _f4a(question: str, sql: str, columns: list, rows: list[list], row_count: int, lang: str):
    """问题的排序方向词与 SQL 的 ORDER BY 方向显式矛盾 → 拦。

    「oldest/youngest」等选择标准不是方向词,不触发;SQL 未写方向(默认
    ASC)也不触发——只拦显式矛盾,防误报。
    """
    if not _DIRECTION_WORDS.search(question):
        return None
    expects_desc = bool(
        re.search(r"\bdescending\b|highest to (?:the )?lowest\b", question, re.I)
    )
    expects_asc = bool(
        re.search(r"\bascending\b|lowest to (?:the )?highest\b", question, re.I)
    )
    if expects_desc == expects_asc:
        return None
    has_asc = bool(_ORDER_DIR_ASC.search(sql))
    has_desc = bool(_ORDER_DIR_DESC.search(sql))
    if expects_desc and has_asc and not has_desc:
        return L(
            lang,
            "问题要求降序(descending/highest to lowest),但 SQL 的 ORDER BY 是 ASC。",
            "The question asks for descending order (descending/highest to "
            "lowest), but the SQL uses ORDER BY ... ASC.",
        )
    if expects_asc and has_desc and not has_asc:
        return L(
            lang,
            "问题要求升序(ascending/lowest to highest),但 SQL 的 ORDER BY 是 DESC。",
            "The question asks for ascending order (ascending/lowest to "
            "highest), but the SQL uses ORDER BY ... DESC.",
        )
    return None


@_rule("F4-b")
def _f4b(question: str, sql: str, columns: list, rows: list[list], row_count: int, lang: str):
    """「top N」与 SQL 的 LIMIT 数量不一致 → 拦。"""
    m = _TOP_N.search(question)
    if not m:
        return None
    n = int(m.group(1)) if m.group(1) else _NUM_WORDS[m.group(2).lower()]
    mlm = re.search(r"\blimit\s+(\d+)", sql, re.I)
    if not mlm or int(mlm.group(1)) == n:
        return None
    return L(
        lang,
        f"问题要求 top {n},但 SQL 用了 LIMIT {mlm.group(1)}——数量不一致。",
        f"The question asks for top {n}, but the SQL uses LIMIT {mlm.group(1)}.",
    )


# ── F2 过滤条件覆盖断言 ──────────────────────────────────────────

_GENDER_Q = re.compile(r"\b(?:male|female|men|women|man|woman|gender|男|女)\b", re.I)
_GENDER_COND = re.compile(r"\b(?:gender|sex)\s*(?:=|<>|!=|<|>|LIKE|IN)", re.I)
# 「female average salary」是度量描述(gender 修饰名词),不是按性别过滤的要求;
# 该题里 gender 条件在数据上可能冗余,不强求 SQL 带 gender 条件
_GENDER_METRIC_GUARD = re.compile(
    r"\b(?:male|female|men|women|man|woman)\s+(?:average|mean|median|avg)\b", re.I,
)

# 具体日期(1/1/1995)出现在问题里 → SQL 必须有日期条件。
# 裸年份(1997)不算:按年分列(如 A15)也可回答,且 BIRD 数据里常出现
# 年分列式的合法无日期条件写法。
_YEAR_Q = re.compile(r"\d{1,2}/\d{1,2}/\d{2,4}\b")
_DATE_COND = re.compile(
    r"\b(?:year|date_format|strftime|date_part|extract)\s*\(|\b(?:19|20)\d{2}\b|\bbetween\b",
    re.I,
)

_REGION_Q = re.compile(
    r"\b(?:east|west|north|south|central)\s+bohemia\b|\bmoravia\b|\bprague\b", re.I,
)
_REGION_COND = re.compile(r"\bbohemia\b|\bmoravia\b|\bprague\b|\bA3\b", re.I)

_CREDIT_CARD_Q = re.compile(r"\bcredit[\s-]?card\b", re.I)
_CREDIT_CARD_COND = re.compile(r"\bkartou\b|\bcard\b|\bcredit\b", re.I)


@_rule("F2-a")
def _f2a(question: str, sql: str, columns: list, rows: list[list], row_count: int, lang: str):
    """问题提到性别但 SQL 没有 gender 条件 → 关键过滤条件缺失。"""
    if not _GENDER_Q.search(question):
        return None
    if _GENDER_METRIC_GUARD.search(question):
        return None
    if _GENDER_COND.search(sql):
        return None
    return L(
        lang,
        "问题提到性别(male/female),但 SQL 没有任何 gender/sex 过滤条件——"
        "检查是否漏了性别条件。",
        "The question mentions gender (male/female) but the SQL has no "
        "gender/sex filter condition.",
    )


@_rule("F2-b")
def _f2b(question: str, sql: str, columns: list, rows: list[list], row_count: int, lang: str):
    """问题限定年份/日期但 SQL 没有日期条件 → 关键过滤条件缺失。"""
    if not _YEAR_Q.search(question):
        return None
    if _DATE_COND.search(sql):
        return None
    return L(
        lang,
        "问题限定了年份/日期,但 SQL 没有任何日期条件"
        "(YEAR()/DATE_FORMAT()/日期字面量)——检查日期过滤是否缺失。",
        "The question specifies a year/date, but the SQL has no date "
        "condition (YEAR()/DATE_FORMAT()/date literal) — check for a missing "
        "date filter.",
    )


@_rule("F2-c")
def _f2c(question: str, sql: str, columns: list, rows: list[list], row_count: int, lang: str):
    """问题提到地区专名但 SQL 没有对应条件 → 关键过滤条件缺失。"""
    if not _REGION_Q.search(question):
        return None
    if _REGION_COND.search(sql):
        return None
    return L(
        lang,
        "问题提到地区(Bohemia/Moravia/Prague),但 SQL 没有对应的地区过滤条件。",
        "The question mentions a region (Bohemia/Moravia/Prague) but the SQL "
        "has no matching region filter.",
    )


@_rule("F2-d")
def _f2d(question: str, sql: str, columns: list, rows: list[list], row_count: int, lang: str):
    """问题提到业务操作词但 SQL 没有对应枚举条件 → 关键过滤条件缺失。

    只保留 credit card 桶:cash 词面不可靠(BIRD 数据里 gold 常以
    operation='VYBER' 表达「cash transactions」,无 pokl/cash 字样)。
    """
    if _CREDIT_CARD_Q.search(question) and not _CREDIT_CARD_COND.search(sql):
        return L(
            lang,
            "问题提到 credit card,但 SQL 没有对应的卡交易条件"
            "(如 operation = 'VYBER KARTOU')。",
            "The question mentions credit card, but the SQL has no matching "
            "card-transaction condition (e.g. operation = 'VYBER KARTOU').",
        )
    return None


# ── F3 值域/类型/唯一性断言 ──────────────────────────────────────

_ID_COL = re.compile(r"(?:^|_)(?:id|no\.?|num|number)(?:_|$)|_id$|^id$", re.I)


@_rule("F3-a")
def _f3a(question: str, sql: str, columns: list, rows: list[list], row_count: int, lang: str):
    """dtype 探测:单值数值问题得到非数值 → 聚合/列选错了。"""
    if not _NUMERIC_SINGLE_Q.search(question):
        return None
    if row_count != 1 or len(columns) != 1 or not rows or len(rows[0]) != 1:
        return None
    value = rows[0][0]
    if value is None:
        return None  # NULL 由 F1-d 处理
    try:
        float(value)
    except (TypeError, ValueError):
        return L(
            lang,
            "单值数值问题得到非数值结果——检查是否选了文本列而非聚合列。",
            "Single-value numeric question returned a non-numeric result — "
            "check that an aggregate column was selected, not a text column.",
        )
    return None


@_rule("F3-b")
def _f3b(question: str, sql: str, columns: list, rows: list[list], row_count: int, lang: str):
    """唯一性:列表题第一列是 ID 列却出现重复值 → join 扩张或缺 DISTINCT。"""
    if not is_list_question(question):
        return None
    if _GROUP_GUARD.search(question) or _WIDE_OUTPUT_GUARD.search(question):
        return None
    if row_count <= 1 or not columns or not rows:
        return None
    if not _ID_COL.search(str(columns[0])):
        return None
    values = [str(r[0]).strip().lower() for r in rows if r and r[0] is not None]
    if len(values) == len(set(values)):
        return None
    return L(
        lang,
        f"列表题第一列({columns[0]})是实体 ID 列却出现重复值——"
        f"疑似 join 扩张产生重复行,应加 DISTINCT 或修正 join 关系。",
        f"The first column ({columns[0]}) is an entity ID column but contains "
        f"duplicate values — likely duplicate rows from a join fan-out; add "
        f"DISTINCT or fix the join.",
    )


@_rule("F3-c")
def _f3c(question: str, sql: str, columns: list, rows: list[list], row_count: int, lang: str):
    """值域:比率/增长率结果绝对值超合理上限 → 单位或计算错误。"""
    if not re.search(r"\b(?:rate|growth|increase)\b", question, re.I):
        return None
    if row_count != 1 or len(columns) != 1 or not rows or len(rows[0]) != 1:
        return None
    value = rows[0][0]
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if abs(numeric) > 100000:
        return L(
            lang,
            "比率结果异常(绝对值超过 100000)——检查单位换算或计算口径。",
            "Rate result out of plausible range (|value| > 100000) — check "
            "unit conversion or the computation.",
        )
    return None
