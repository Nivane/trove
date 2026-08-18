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
_GROUP_RE = re.compile(
    r"^SELECT (\w+)\.(\w+), COUNT\(\*\) FROM (\w+) JOIN (\w+) ON (.+?) GROUP BY \1\.\2$")
_JOIN_WHERE_RE = re.compile(
    r"^SELECT COUNT\(\*\) FROM (\w+) JOIN (\w+) ON (.+?) WHERE (.+)$")

# 每类原子模板最多取前几个参与组合,组合总数上限(防候选池爆炸)
_MAX_JOINS = 3
_MAX_FILTERS = 3
_MAX_GROUPS = 3
_MAX_COMBOS = 4
_MAX_TRIPLES = 4


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


def parse_group(sql: str) -> tuple[str, str, str, str] | None:
    """JOIN+GROUP 模板 → (fact, dim, group_col, join_on);非本家返回 None。

    正则组:1=SELECT 表(dim) 2=SELECT 列 3=FROM(fact) 4=JOIN(dim) 5=on。
    """
    m = _GROUP_RE.match(sql.strip())
    if not m:
        return None
    return m.group(3), m.group(4), m.group(2), m.group(5)


def parse_join_where(sql: str) -> tuple[str, str, str, str] | None:
    """JOIN×WHERE 组合 SQL → (fact, dim, join_on, where_clause)。"""
    m = _JOIN_WHERE_RE.match(sql.strip())
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3), m.group(4)


def compose_triple(join_where_sql: str, group_sql: str) -> str | None:
    """JOIN×WHERE 组合 × GROUP 模板 → 三层组合 SQL。

    要求 GROUP 模板的 (fact, dim) 表对与组合一致(列前缀/粒度匹配);
    单表 GROUP 模板不组合(表集合不同)。
    """
    jw = parse_join_where(join_where_sql)
    if not jw:
        return None
    fact, dim, join_on, where_clause = jw
    g = parse_group(group_sql)
    if not g or (g[0], g[1]) != (fact, dim):
        return None
    group_col = g[2]
    return (
        f"SELECT {dim}.{group_col}, COUNT(*) FROM {fact} JOIN {dim} "
        f"ON {join_on} WHERE {where_clause} GROUP BY {dim}.{group_col}"
    )


def compose_candidates(
    hits: list[Any], lang: str = "en", max_combos: int = _MAX_COMBOS,
    max_triples: int = 0,
) -> list[Any]:
    """从命中示例(按分降序)里组合 JOIN×WHERE(可选×GROUP),合并回候选池。

    返回原始 hits + 组合示例(score = 最强原子 ×0.85 降权——组合是
    结构推测,绝不能压过真实命中的原子);组合元素为 dict,排序由调用方
    按 score 再做一次。

    max_triples 默认 0(关闭三层):三层组合不带来新列(其列 = 三个原子
    列并集),挤占 top-k 槽位对主流两层题是净负收益(eval_retrieval 实测
    B@5 列覆盖 50%→48%)。三层价值在"单示例完整结构参考",等 eval_bird
    验证后再开。
    """
    joins = [h for h in hits if parse_join(_sql_of(h))]
    filters = [h for h in hits if parse_filter(_sql_of(h))]
    groups = [h for h in hits if parse_group(_sql_of(h))]
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

    def combo_from(parts: list[dict], sql: str, q: str) -> dict:
        # 组合分 = 最强原子 × 0.85 降权:原子是真实词法命中,组合是结构
        # 推测——组合绝不能压过最强原子(实测 max 直取会让组合霸占
        # top-k 槽位,挤掉覆盖独特列的原子,列覆盖回退)
        return {
            "question": q,
            "sql": sql,
            "tags": sorted({t for p in parts for t in p["tags"]}),
            "template": True,
            "score": max(1, int(max(p["score"] for p in parts) * 0.85)),
        }

    combos: list[dict] = []
    triples: list[dict] = []
    seen: set[str] = set()
    for j in joins[:_MAX_JOINS]:
        for f in filters[:_MAX_FILTERS]:
            sql = compose_pair(_sql_of(j), _sql_of(f))
            if not sql or sql in seen:
                continue
            seen.add(sql)
            jd, fd = base(j), base(f)
            two = combo_from([jd, fd], sql, compose_question(
                jd["question"], fd["question"], lang))
            combos.append(two)
            # 三层:JOIN×WHERE × GROUP(表对一致)——"每地区状态A的贷款数"
            if groups:
                for g in groups[:_MAX_GROUPS]:
                    gd = base(g)
                    triple_sql = compose_triple(sql, _sql_of(g))
                    if not triple_sql or triple_sql in seen:
                        continue
                    seen.add(triple_sql)
                    triples.append(combo_from(
                        [jd, fd, gd], triple_sql,
                        compose_question(two["question"], gd["question"], lang)))
    combos.sort(key=lambda c: c["score"], reverse=True)
    triples.sort(key=lambda c: c["score"], reverse=True)
    return list(hits) + combos[:max_combos] + triples[:max_triples]
