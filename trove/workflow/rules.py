"""Deterministic result/SQL validation rules (no LLM involved).

Rule failures route back to gen_sql through the shared error_feedback
correction channel — the principle: what can be checked by code should
not be left to an LLM judge. This module is the mounting point for
business-caliber rules (semantic-layer constraints) as they emerge.

Patterns are deliberately narrow to avoid false positives; questions
matching no rule pass through untouched.
"""

from __future__ import annotations

import re

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


def validate(
    question: str,
    sql: str,
    columns: list,
    rows: list[list],
    row_count: int,
    lang: str = "zh",
) -> str | None:
    """Run deterministic rules; return a failure reason, or None to pass.

    Args:
        question: The user question.
        sql: The SQL that produced the result.
        columns: Result column names.
        rows: Result rows.
        row_count: Number of result rows.
        lang: 反馈语言(zh/en,随配置)。
    """
    # 1. count questions must return a single number
    if is_count_question(question):
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

    # 2. list/top-N questions returning zero rows are almost always wrong
    if is_list_question(question) and row_count == 0:
        return L(
            lang,
            "列表问题返回零行——检查 join/过滤条件/表名",
            "list question returned no rows — check joins/filters/table names",
        )

    # 3. percentage questions must produce a numeric value in [0, 100]
    if is_percent_question(question) and row_count == 1 and rows and len(rows[0]) == 1:
        value = rows[0][0]
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return L(lang, "百分比结果应为数值", "percentage result should be a numeric value")
        if not 0 <= numeric <= 100:
            return L(lang, "百分比结果超出 0-100 范围", "percentage result out of 0-100 range")

    # 3b. 比率题(percentage / "… rate")的整数除法陷阱:MySQL 对整数/聚合
    # 结果相除会截断到 4 位小数(如 27/61*100=44.2623 而非
    # 44.26229508196721),与金标准全精度结果无法精确一致。
    # 含除法时必须显式 CAST 成 DOUBLE。
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

    # 3c. "what is the increase/growth rate" 问的是单一比率值(单行单列)。
    # 多列结果(如附带两个余额)不符合问题所问。
    if re.search(r"\bwhat (?:is|was) the (?:increase|growth) rate\b", question, re.I) \
            and (row_count != 1 or len(columns) != 1):
        return L(
            lang,
            "问题问的是单一比率值——结果应为单行单列,不要附带中间值列。",
            "The question asks for a single rate value — the result must be one "
            "row and one column; drop any intermediate value columns.",
        )

    # 4. top-N/ranking questions with LIMIT need ORDER BY
    if (
        is_ordered_question(question)
        and _LIMIT_RE.search(sql)
        and not _ORDER_BY_RE.search(sql)
    ):
        return "top-N query uses LIMIT without ORDER BY"

    # 5. 极值作用域歧义(MIN/MAX 子查询 + 外层过滤条件)
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
