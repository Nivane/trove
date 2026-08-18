"""Atomic template composition — assemble JOIN skeletons with WHERE filters.

kb init templates are atomic (COUNT / GROUP BY / JOIN / WHERE each alone);
a multi-table question with a filter needs a JOIN + WHERE combination no
single template covers. Composition is deterministic string surgery on our
OWN generated templates only (unparseable SQL is skipped untouched) — never
gold examples, so it stays inside the anti-cheating constraint. The combined
example is a structural reference; gen_sql's check_result tool validates the
final SQL, so a wrong composition (bad join key, odd value) is caught there.

WHERE columns always get a table prefix; filters whose table is not part of
the JOIN (3+ table chains) are skipped at this layer.

Hits are duck-typed (dict or any object with question/sql/tags/score fields)
so this module stays dependency-free — service.py converts as needed.
"""

from __future__ import annotations

import re
from typing import Any

# 自家模板的固定格式,保守正则解析;解析不了就跳过(不改动任何外部 SQL)
_JOIN_RE = re.compile(r"^SELECT COUNT\(\*\) FROM (\w+) JOIN (\w+) ON (.+?)$")
_FILTER_RE = re.compile(r"^SELECT COUNT\(\*\) FROM (\w+) WHERE (\w+) = '([^']*)'$")

# 每类原子模板最多取前几个参与组合,组合总数上限(防候选池爆炸)
_MAX_JOINS = 3
_MAX_FILTERS = 3
_MAX_COMBOS = 4


def _sql_of(hit: Any) -> str:
    return hit["sql"] if isinstance(hit, dict) else getattr(hit, "sql", "")


def parse_join(sql: str) -> tuple[str, str, str] | None:
    """JOIN 模板 → (fact, dim, join_on);非本家模板返回 None。"""
    m = _JOIN_RE.match(sql.strip())
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


def parse_filter(sql: str) -> tuple[str, str, str] | None:
    """WHERE 过滤模板 → (table, column, value);非本家模板返回 None。"""
    m = _FILTER_RE.match(sql.strip())
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


def compose_pair(join_sql: str, filter_sql: str) -> str | None:
    """JOIN 模板 × WHERE 模板 → 组合 SQL(过滤列加表前缀)。

    过滤表必须在 JOIN 涉及的表集合内(3+ 表跨链跳过);任一模板无法
    解析 → None(保守:不碰外部 SQL)。
    """
    parsed = parse_join(join_sql)
    if not parsed:
        return None
    fact, dim, join_on = parsed
    fparsed = parse_filter(filter_sql)
    if not fparsed:
        return None
    f_table, f_col, f_val = fparsed
    if f_table not in (fact, dim):
        return None
    return (
        f"SELECT COUNT(*) FROM {fact} JOIN {dim} ON {join_on} "
        f"WHERE {f_table}.{f_col} = '{f_val}'"
    )


def compose_question(join_q: str, filter_q: str, lang: str = "en") -> str:
    """组合示例的问题文本:两个原子问题拼接(词覆盖 = 两者并集,
    检索打分时能同时命中 join 与 filter 的词汇)。"""
    sep = "；" if lang == "zh" else "; "
    return f"{join_q}{sep}{filter_q}"


def compose_candidates(
    hits: list[Any], lang: str = "en", max_combos: int = _MAX_COMBOS,
) -> list[Any]:
    """从命中示例(按分降序)里组合 JOIN×WHERE,合并回候选池。

    返回原始 hits + 组合示例(score = 两个原子的较高分);组合元素为
    dict,排序由调用方按 score 再做一次。
    """
    joins = [h for h in hits if parse_join(_sql_of(h))]
    filters = [h for h in hits if parse_filter(_sql_of(h))]
    if not joins or not filters:
        return hits

    def base(hit: Any) -> dict:
        if isinstance(hit, dict):
            return hit
        return {
            "question": getattr(hit, "question", ""),
            "sql": getattr(hit, "sql", ""),
            "tags": list(getattr(hit, "tags", []) or []),
            "score": getattr(hit, "score", 0),
        }

    combos: list[dict] = []
    seen: set[str] = set()
    for j in joins[:_MAX_JOINS]:
        for f in filters[:_MAX_FILTERS]:
            sql = compose_pair(_sql_of(j), _sql_of(f))
            if not sql or sql in seen:
                continue
            seen.add(sql)
            jd, fd = base(j), base(f)
            combos.append({
                "question": compose_question(jd["question"], fd["question"], lang),
                "sql": sql,
                "tags": sorted(set(jd["tags"]) | set(fd["tags"])),
                "template": True,
                "score": max(jd["score"], fd["score"]),
            })
    combos.sort(key=lambda c: c["score"], reverse=True)
    return list(hits) + combos[:max_combos]
